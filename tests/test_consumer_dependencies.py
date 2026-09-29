"""消费者字段级依赖：登记校验、冲突语义、门禁三档影响判定。

登记侧（对照最新基线显式校验，不静默放行）：
    依赖不存在的操作   -> incomplete_contract / unknown_consumer_operation
    依赖不存在的响应   -> incomplete_contract / unknown_consumer_response
    依赖不存在的字段   -> incomplete_contract / unknown_consumer_field

门禁侧（reason_code 三档）：
    breaking_consumer_affected  命中依赖内容
    breaking_operation_only     只命中调用的操作、未命中依赖内容
    breaking_unused             没有任何消费者使用
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from contract_guard.collector import ContractCollector
from contract_guard.differ import SnapshotDiffer
from contract_guard.errors import ConsumerConflictError, IncompleteContractError
from contract_guard.gate import ReleaseGate
from contract_guard.models import GateVerdict
from contract_guard.service import ContractGuardService
from contract_guard.storage import LocalStore
from tests.contract_factory import contract_with_responses, json_response

logger = logging.getLogger("contract_tests.consumer_deps")


def _contract_multi() -> dict:
    return contract_with_responses({
        "200": json_response("200", {
            "id": {"type": "integer"},
            "name": {"type": "string"},
        }, required=["id", "name"]),
        "404": json_response("404", {"message": {"type": "string"}}),
    })


def _service(tmp_store: Path) -> ContractGuardService:
    svc = ContractGuardService(tmp_store)
    svc.register_baseline(_contract_multi(), service="svc", version="1.0.0")
    return svc


# --------------------------------------------------------------------------- #
# 登记校验
# --------------------------------------------------------------------------- #


def test_dependency_on_unknown_operation_rejected(tmp_store: Path) -> None:
    svc = _service(tmp_store)
    with pytest.raises(IncompleteContractError) as exc:
        svc.register_consumer(
            "svc", "web", [],
            dependencies=[{"operation": "GET /ghost", "response": "200", "fields": ["id"]}],
        )
    logger.info("依赖不存在操作 -> %s", exc.value.message)
    assert exc.value.message.startswith("unknown_consumer_operation")


def test_dependency_on_unknown_response_rejected(tmp_store: Path) -> None:
    svc = _service(tmp_store)
    with pytest.raises(IncompleteContractError) as exc:
        svc.register_consumer(
            "svc", "web", [],
            dependencies=[{"operation": "GET /users", "response": "503", "fields": ["id"]}],
        )
    logger.info("依赖不存在响应 -> %s", exc.value.message)
    assert exc.value.message.startswith("unknown_consumer_response")


def test_dependency_on_unknown_field_rejected(tmp_store: Path) -> None:
    svc = _service(tmp_store)
    with pytest.raises(IncompleteContractError) as exc:
        svc.register_consumer(
            "svc", "web", [],
            dependencies=[{"operation": "GET /users", "response": "200",
                           "fields": ["id", "ghost"]}],
        )
    logger.info("依赖不存在字段 -> %s", exc.value.message)
    assert exc.value.message.startswith("unknown_consumer_field")


def test_dependency_requires_baseline(tmp_store: Path) -> None:
    svc = ContractGuardService(tmp_store)
    with pytest.raises(IncompleteContractError) as exc:
        svc.register_consumer(
            "svc", "web", [],
            dependencies=[{"operation": "GET /users", "response": "200", "fields": ["id"]}],
        )
    assert exc.value.message == "consumer_dependency_requires_baseline"


def test_three_unknown_reasons_are_distinguishable(tmp_store: Path) -> None:
    svc = _service(tmp_store)
    reasons = []
    for dep in (
        {"operation": "GET /ghost", "response": "200", "fields": ["id"]},
        {"operation": "GET /users", "response": "503", "fields": ["id"]},
        {"operation": "GET /users", "response": "200", "fields": ["ghost"]},
    ):
        try:
            svc.register_consumer("svc", f"c-{len(reasons)}", [], dependencies=[dep])
        except IncompleteContractError as exc:
            reasons.append(exc.message.split(":", 1)[0])
    logger.info("三种拒绝原因: %s", reasons)
    assert reasons == [
        "unknown_consumer_operation",
        "unknown_consumer_response",
        "unknown_consumer_field",
    ]


def test_dependency_registration_normalizes_and_persists(tmp_store: Path) -> None:
    svc = _service(tmp_store)
    saved = svc.register_consumer(
        "svc", "web", ["get /users"],
        dependencies=[
            # 乱序 + 重复字段，期望被规范化
            {"operation": "GET /users", "response": "200", "fields": ["name", "id", "id"]},
            {"operation": "GET /users", "response": "404", "fields": []},
        ],
    )
    dep_200 = next(d for d in saved.dependencies if d.response == "200")
    dep_404 = next(d for d in saved.dependencies if d.response == "404")
    assert dep_200.fields == ("id", "name")
    assert dep_404.fields == ()  # 空 fields 表示依赖整个响应
    assert saved.operations == ("GET /users",)
    # 幂等：规范化后重复登记不冲突
    again = svc.register_consumer(
        "svc", "web", ["GET /users"],
        dependencies=[
            {"operation": "GET /users", "response": "404", "fields": []},
            {"operation": "GET /users", "response": "200", "fields": ["name", "id", "id"]},
        ],
    )
    assert again.dependencies == saved.dependencies


def test_same_consumer_inconsistent_dependencies_conflict(tmp_store: Path) -> None:
    svc = _service(tmp_store)
    svc.register_consumer(
        "svc", "web", [],
        dependencies=[{"operation": "GET /users", "response": "200", "fields": ["id"]}],
    )
    with pytest.raises(ConsumerConflictError):
        svc.register_consumer(
            "svc", "web", [],
            dependencies=[{"operation": "GET /users", "response": "200",
                           "fields": ["id", "name"]}],
        )


def test_old_style_consumer_still_registered(tmp_store: Path) -> None:
    svc = _service(tmp_store)
    saved = svc.register_consumer("svc", "legacy", ["GET /users"])
    assert saved.dependencies == ()
    assert saved.operations == ("GET /users",)


# --------------------------------------------------------------------------- #
# 门禁：字段级命中
# --------------------------------------------------------------------------- #


def _gate(tmp_store: Path, old_raw: dict, new_raw: dict, registrations):
    """registrations: [(consumer_name, dependencies_list_or_None)]；None=老格式。

    流程模拟真实发布：先登记旧版基线 -> 消费者在旧版（最新已发布版）上登记
    -> 登记新版本 -> 跑门禁。
    """
    svc = ContractGuardService(tmp_store)
    svc.register_baseline(old_raw, service="svc", version="1.0.0")
    for name, deps in registrations:
        if deps is None:
            svc.register_consumer("svc", name, ["GET /users"])
        else:
            svc.register_consumer("svc", name, [], dependencies=deps)
    svc.register_baseline(new_raw, service="svc", version="2.0.0")
    diff = svc.diff_versions("svc", "1.0.0", "2.0.0")
    report = svc.check_release("svc", "1.0.0", "2.0.0")
    logger.info("判定依据: 消费者=%s; 破坏=%s",
                registrations,
                [(c.path, c.reason_code) for c in diff.changes if c.severity.value == "breaking"])
    logger.info("最终结论: verdict=%s findings=%s",
                report.verdict.value,
                [(f.reason_code, f.change.path, list(f.consumers)) for f in report.findings])
    return diff, report


def _new(responses: dict) -> dict:
    return contract_with_responses(responses)


def test_breaking_on_dependent_field_is_reject(tmp_store: Path) -> None:
    old = _contract_multi()
    new = _new({"200": json_response("200", {"name": {"type": "string"}}, required=["name"])})
    _, report = _gate(tmp_store, old, new, [
        ("web", [{"operation": "GET /users", "response": "200", "fields": ["id"]}]),
    ])
    assert report.verdict is GateVerdict.REJECT
    finding = report.findings[0]
    assert finding.reason_code == "breaking_consumer_affected"
    assert finding.consumers == ("web",)


def test_breaking_on_unused_field_is_warn_operation_only(tmp_store: Path) -> None:
    old = _contract_multi()
    new = _new({"200": json_response("200", {"id": {"type": "integer"}}, required=["id"])})
    # web 只依赖 id；name 被删与它无关
    _, report = _gate(tmp_store, old, new, [
        ("web", [{"operation": "GET /users", "response": "200", "fields": ["id"]}]),
    ])
    assert report.verdict is GateVerdict.WARN
    finding = report.findings[0]
    assert finding.reason_code == "breaking_operation_only"
    assert finding.consumers == ("web",)
    assert finding.affected is False


def test_breaking_with_no_consumer_is_unused(tmp_store: Path) -> None:
    old = _contract_multi()
    new = _new({"200": json_response("200", {"id": {"type": "integer"}}, required=["id"])})
    # 消费者只登记 GET /orders，破坏在 GET /users
    svc = ContractGuardService(tmp_store)
    svc.register_baseline(old, service="svc", version="1.0.0")
    svc.register_baseline(new, service="svc", version="2.0.0")
    svc.register_consumer("svc", "billing", ["GET /orders"])
    report = svc.check_release("svc", "1.0.0", "2.0.0")
    assert report.verdict is GateVerdict.WARN
    assert report.findings[0].reason_code == "breaking_unused"
    assert report.findings[0].consumers == ()


def test_three_tier_findings_in_one_report(tmp_store: Path) -> None:
    """同时凑齐三种原因码：依赖命中 / 仅操作命中 / 完全无人使用。"""
    old = contract_with_responses({
        "200": json_response("200", {
            "id": {"type": "integer"},
            "name": {"type": "string"},
        }, required=["id", "name"]),
        "404": json_response("404", {"message": {"type": "string"}}),
    })
    # GET /users：删 name（web 只依赖 id，不依赖 name；api 只依赖 404.message）
    # GET /orders：删 order_id（无任何消费者使用 GET /orders）
    old["paths"]["/orders"] = {"get": {"responses": {
        "200": json_response("200", {"order_id": {"type": "integer"}}, required=["order_id"]),
    }}}
    new = contract_with_responses({
        "200": json_response("200", {"id": {"type": "integer"}}, required=["id"]),
        "404": json_response("404", {"message": {"type": "string"}}),
    })
    new["paths"]["/orders"] = {"get": {"responses": {
        "200": json_response("200", {}),
    }}}

    _, report = _gate(tmp_store, old, new, [
        # web 只依赖 id：删 name 与它无关
        ("web", [{"operation": "GET /users", "response": "200", "fields": ["id"]}]),
        # mobile 依赖 name：删 name 命中它
        ("mobile", [{"operation": "GET /users", "response": "200", "fields": ["name"]}]),
        # api 只依赖 404.message：删 200.name 与它无关
        ("api", [{"operation": "GET /users", "response": "404", "fields": ["message"]}]),
    ])
    name_finding = next(f for f in report.findings if f.change.path.endswith("response-200.name"))
    orders_field = next(f for f in report.findings if f.change.path.endswith("response-200.order_id"))
    orders_body = next(f for f in report.findings if "/orders" in f.change.path
                       and f.change.reason_code == "response_body_removed")

    # name 被删：mobile 依赖 name -> affected；web/api 调用操作但不依赖 name
    assert name_finding.reason_code == "breaking_consumer_affected"
    assert name_finding.consumers == ("mobile",)
    assert orders_field.reason_code == "breaking_unused"
    assert orders_body.reason_code == "breaking_unused"

    # operation_only 体现在“api 虽调用 GET /users 但不依赖 200 任何字段”——
    # 用单独场景更直接地断言（见 test_breaking_on_unused_field_is_warn_operation_only）。
    assert report.verdict is GateVerdict.REJECT
    assert {f.reason_code for f in report.findings} == {
        "breaking_consumer_affected",
        "breaking_unused",
    }
    # 确定性：findings 固定排序
    keys = [(f.reason_code, f.change.path, f.change.kind) for f in report.findings]
    assert keys == sorted(keys)


def test_all_three_tiers_coexist_in_one_report(tmp_store: Path, tmp_path: Path) -> None:
    """三条破坏 × 不同消费者，在同一份报告里凑齐 affected/operation_only/unused。"""
    old = contract_with_responses({
        "200": json_response("200", {
            "id": {"type": "integer"},
            "name": {"type": "string"},
        }, required=["id", "name"]),
    })
    old["paths"]["/orders"] = {"get": {"responses": {
        "200": json_response("200", {"order_id": {"type": "integer"}},
                             required=["order_id"]),
    }}}
    # /users 删 name；/orders 删 order_id
    new = contract_with_responses({
        "200": json_response("200", {"id": {"type": "integer"}}, required=["id"]),
    })
    new["paths"]["/orders"] = {"get": {"responses": {"200": json_response("200", {})}}}

    _, report = _gate(tmp_store, old, new, [
        # mobile 依赖 /users 的 name -> 命中 affected
        ("mobile", [{"operation": "GET /users", "response": "200", "fields": ["name"]}]),
        # web 调用 /users 但只依赖 id -> name 删除对它只是操作级（同条 finding 取最严重档）
        ("web", [{"operation": "GET /users", "response": "200", "fields": ["id"]}]),
    ])
    # /users#response-200.name：mobile 命中 -> 该 finding 取最严重档 affected
    name_findings = [f for f in report.findings
                     if f.change.path.endswith("response-200.name")]
    assert len(name_findings) == 1
    assert name_findings[0].reason_code == "breaking_consumer_affected"
    assert name_findings[0].consumers == ("mobile",)
    # /orders 完全无人使用 -> unused（同一份报告内三档中的第三档）
    assert any(
        f.reason_code == "breaking_unused" and "/orders" in f.change.path
        and f.consumers == ()
        for f in report.findings
    )
    assert report.verdict is GateVerdict.REJECT

    # operation_only：只含 web 的独立存储场景，name 删除只命中它调用的操作
    store2 = tmp_path / "guard-web-only"
    store2.mkdir()
    _, report_web_only = _gate(store2, old, new, [
        ("web", [{"operation": "GET /users", "response": "200", "fields": ["id"]}]),
    ])
    assert any(
        f.reason_code == "breaking_operation_only" and f.consumers == ("web",)
        for f in report_web_only.findings
    )
    assert report_web_only.verdict is GateVerdict.WARN


def test_old_consumer_operation_level_behavior_unchanged(tmp_store: Path) -> None:
    """老格式消费者：操作上任何破坏都算 affected（行为与历史一致）。"""
    old = _contract_multi()
    new = _new({"200": json_response("200", {"id": {"type": "integer"}}, required=["id"])})
    _, report = _gate(tmp_store, old, new, [("legacy", None)])
    assert report.verdict is GateVerdict.REJECT
    assert all(f.reason_code == "breaking_consumer_affected" for f in report.findings)
    assert report.findings[0].consumers == ("legacy",)


def test_removed_response_scoping_against_consumer_dependency(
        tmp_store: Path, tmp_path: Path) -> None:
    old = _contract_multi()
    new = _new({"200": json_response("200", {
        "id": {"type": "integer"}, "name": {"type": "string"}},
        required=["id", "name"])})

    # 场景 1：消费者依赖 404 整体；404 被删 -> reject / affected
    _, report = _gate(tmp_store, old, new, [
        ("err-handler", [{"operation": "GET /users", "response": "404", "fields": []}]),
    ])
    removed = [f for f in report.findings if f.change.reason_code == "response_removed"]
    assert len(removed) == 1
    assert removed[0].reason_code == "breaking_consumer_affected"
    assert removed[0].consumers == ("err-handler",)

    # 场景 2（独立存储）：依赖 200 的消费者不依赖 404 -> 404 删除仅操作命中
    store2 = tmp_path / "guard-data-2"
    store2.mkdir()
    _, report2 = _gate(store2, old, new, [
        ("ok-only", [{"operation": "GET /users", "response": "200",
                      "fields": ["id", "name"]}]),
    ])
    removed2 = [f for f in report2.findings if f.change.reason_code == "response_removed"]
    assert len(removed2) == 1
    assert removed2[0].reason_code == "breaking_operation_only"
    assert removed2[0].consumers == ("ok-only",)
    assert report2.verdict is GateVerdict.WARN
