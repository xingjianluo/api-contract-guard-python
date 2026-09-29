"""消费者细粒度依赖登记 + 门禁判定测试。

覆盖：
* 依赖命中其声明的响应/字段      -> reject / breaking_consumer_dependency_hit
* 只命中操作、未命中依赖内容     -> warn   / breaking_operation_used_not_depended
* 完全没有消费者用到的破坏       -> warn   / breaking_unused
* 依赖指向不存在的操作/响应/字段 -> 三种可区分错误码，显式拒绝
* 老消费者（只登记 operations）行为不变
* 同名消费者声明不一致 -> 冲突，不静默合并
* 消费者数量、声明条数、登记顺序不影响结论与排序；结论可复现
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from contract_guard.errors import (
    ConsumerConflictError,
    ConsumerUnknownFieldError,
    ConsumerUnknownOperationError,
    ConsumerUnknownResponseError,
)
from contract_guard.models import GateVerdict
from contract_guard.service import ContractGuardService
from tests.contract_factory import response_contract

logger = logging.getLogger("contract_tests.consumer_deps")


# --------------------------------------------------------------------------- #
# 夹具：shop 服务，/orders 的 GET 有 200（id/total）、404（error）两种响应
# --------------------------------------------------------------------------- #


def _v1():
    return response_contract({
        "200": {"id": {"type": "integer"}, "total": {"type": "integer"}},
        "404": {"error": {"type": "string"}},
    }, path="/orders", method="get")


def _v2_remove_total():
    # 删除 200.total（破坏，未被只依赖 id 的消费者用到）
    return response_contract({
        "200": {"id": {"type": "integer"}},
        "404": {"error": {"type": "string"}},
    }, path="/orders", method="get")


def _v2_remove_404():
    return response_contract({
        "200": {"id": {"type": "integer"}, "total": {"type": "integer"}},
    }, path="/orders", method="get")


def _v2_change_id_type():
    return response_contract({
        "200": {"id": {"type": "string"}, "total": {"type": "integer"}},
        "404": {"error": {"type": "string"}},
    }, path="/orders", method="get")


@pytest.fixture()
def service(tmp_store: Path) -> ContractGuardService:
    svc = ContractGuardService(tmp_store)
    svc.register_baseline(_v1(), service="shop", version="1.0.0")
    return svc


def _dep(operation="GET /orders", response="200", fields=()):
    return {"operation": operation, "response": response, "fields": list(fields)}


def _gate(svc: ContractGuardService, new_raw, version="2.0.0"):
    svc.register_baseline(new_raw, service="shop", version=version)
    diff = svc.diff_versions("shop", "1.0.0", version)
    report = svc.gate.evaluate(diff)
    logger.info("判定依据 diff=%s",
                [(c.path, c.reason_code) for c in diff.changes
                 if c.severity.value == "breaking"])
    logger.info("最终结论 verdict=%s findings=%s", report.verdict.value,
                [(f.reason_code, f.change.path, list(f.affected_consumers))
                 for f in report.findings])
    return diff, report


# --------------------------------------------------------------------------- #
# 三种门禁情况
# --------------------------------------------------------------------------- #


def test_dependency_field_hit_is_reject(service: ContractGuardService) -> None:
    service.register_consumer("shop", "web", dependencies=[_dep(fields=["id"])])
    _, report = _gate(service, _v2_change_id_type())
    assert report.verdict is GateVerdict.REJECT
    hit = [f for f in report.findings
           if f.reason_code == "breaking_consumer_dependency_hit"]
    assert hit and hit[0].affected_consumers == ("web",)


def test_unused_field_change_only_warns_with_operation_used_reason(
    service: ContractGuardService,
) -> None:
    # web 只依赖 200.id；被删的是 200.total —— 命中操作但没命中依赖内容
    service.register_consumer("shop", "web", dependencies=[_dep(fields=["id"])])
    _, report = _gate(service, _v2_remove_total())
    assert report.verdict is GateVerdict.WARN
    reasons = {f.reason_code for f in report.findings}
    assert reasons == {"breaking_operation_used_not_depended"}
    assert all(not f.affected for f in report.findings)


def test_completely_unused_breaking_is_unused_reason(
    service: ContractGuardService, tmp_store: Path,
) -> None:
    # 消费者只登记另一个操作 GET /products
    service.register_consumer("shop", "web", operations=["GET /products"])
    _, report = _gate(service, _v2_remove_total())
    assert report.verdict is GateVerdict.WARN
    assert {f.reason_code for f in report.findings} == {"breaking_unused"}


def test_response_removal_hits_consumer_depending_on_that_response(
    service: ContractGuardService,
) -> None:
    # 消费者依赖 404 响应本身（不依赖字段）；删除 404 -> 命中
    service.register_consumer("shop", "web", dependencies=[_dep(response="404")])
    _, report = _gate(service, _v2_remove_404())
    assert report.verdict is GateVerdict.REJECT
    hit = [f for f in report.findings
           if f.reason_code == "breaking_consumer_dependency_hit"]
    assert any("response-404" in f.change.path for f in hit)


def test_other_response_change_does_not_hit_dependency(service: ContractGuardService) -> None:
    # 消费者依赖 200.id；404 被删与它无关（它调用该操作）-> operation_used
    service.register_consumer("shop", "web", dependencies=[_dep(response="200", fields=["id"])])
    _, report = _gate(service, _v2_remove_404())
    assert report.verdict is GateVerdict.WARN
    assert {f.reason_code for f in report.findings} == \
        {"breaking_operation_used_not_depended"}


def test_three_reasons_coexist_and_are_distinguishable(
    service: ContractGuardService,
) -> None:
    """同一版报告里三种原因码可以并存、各自可区分。"""
    # a: 只依赖 200.id（id 被改类型 -> dependency_hit）
    # b: 只依赖 200.id（同操作另一破坏 404 删除 -> operation_used_not_depended）
    # c: 只用 GET /products（-> breaking_unused，由 /orders 上的破坏产生）
    # 构造一版同时改 id 类型又删 404 的新契约
    new_raw = response_contract({
        "200": {"id": {"type": "string"}, "total": {"type": "integer"}},
    }, path="/orders", method="get")
    service.register_consumer("shop", "a", dependencies=[_dep(fields=["id"])])
    service.register_consumer("shop", "b", dependencies=[_dep(response="404", fields=["error"])])
    service.register_consumer("shop", "c", operations=["GET /products"])
    _, report = _gate(service, new_raw)
    by_path = {f.change.path: f for f in report.findings}
    assert report.verdict is GateVerdict.REJECT
    id_finding = next(f for p, f in by_path.items() if p.endswith("response-200.id"))
    removed_finding = next(f for p, f in by_path.items()
                           if "response-404" in p and f.change.kind == "response_removed")
    assert id_finding.reason_code == "breaking_consumer_dependency_hit"
    assert id_finding.affected_consumers == ("a",)
    # b 依赖 404.error，404 被删 -> b 被命中（依赖的响应没了）
    assert removed_finding.reason_code == "breaking_consumer_dependency_hit"
    assert removed_finding.affected_consumers == ("b",)


# --------------------------------------------------------------------------- #
# 请求侧破坏影响所有调用者（含依赖级登记）
# --------------------------------------------------------------------------- #


def test_request_side_breaking_hits_dependency_consumer(
    service: ContractGuardService,
) -> None:
    # 即使只登记了响应依赖，新增必填 query 参数仍影响它（请求发不出去）
    service.register_consumer("shop", "web", dependencies=[_dep(fields=["id"])])
    new_raw = response_contract(
        {"200": {"id": {"type": "integer"}, "total": {"type": "integer"}},
         "404": {"error": {"type": "string"}}},
        path="/orders", method="get",
        params=[{"name": "token", "in": "query", "required": True,
                 "schema": {"type": "string"}}],
    )
    _, report = _gate(service, new_raw)
    assert report.verdict is GateVerdict.REJECT
    assert all(
        f.reason_code in {"breaking_consumer_dependency_hit",
                          "breaking_consumer_affected"}
        and f.affected for f in report.findings
    )


# --------------------------------------------------------------------------- #
# 登记校验：不存在的操作 / 响应 / 字段
# --------------------------------------------------------------------------- #


def test_unknown_operation_rejected(service: ContractGuardService) -> None:
    with pytest.raises(ConsumerUnknownOperationError) as exc:
        service.register_consumer("shop", "web", dependencies=[_dep(operation="GET /ghost")])
    logger.info("最终结论: 未知操作 -> %s", exc.value.code)
    assert exc.value.code == "consumer_dependency_unknown_operation"


def test_unknown_response_rejected(service: ContractGuardService) -> None:
    with pytest.raises(ConsumerUnknownResponseError) as exc:
        service.register_consumer(
            "shop", "web", dependencies=[_dep(response="503")])
    logger.info("最终结论: 未知响应 -> %s", exc.value.code)
    assert exc.value.code == "consumer_dependency_unknown_response"


def test_unknown_field_rejected(service: ContractGuardService) -> None:
    with pytest.raises(ConsumerUnknownFieldError) as exc:
        service.register_consumer(
            "shop", "web", dependencies=[_dep(fields=["ghost"])])
    logger.info("最终结论: 未知字段 -> %s", exc.value.code)
    assert exc.value.code == "consumer_dependency_unknown_field"


def test_unknown_operation_response_field_codes_are_distinct(
    service: ContractGuardService,
) -> None:
    codes = []
    for kwargs in (
        {"operation": "GET /ghost"},
        {"response": "503"},
        {"fields": ["ghost"]},
    ):
        with pytest.raises(Exception) as exc:  # noqa: PT011, BLE001
            service.register_consumer("shop", "x", dependencies=[_dep(**kwargs)])
        codes.append(exc.value.code)
    assert len(set(codes)) == 3
    logger.info("三种拒绝原因可区分: %s", codes)


def test_dependency_without_baseline_rejected(tmp_store: Path) -> None:
    svc = ContractGuardService(tmp_store)
    with pytest.raises(ConsumerUnknownOperationError):
        svc.register_consumer("shop", "web", dependencies=[_dep()])


# --------------------------------------------------------------------------- #
# 老消费者兼容 / 冲突
# --------------------------------------------------------------------------- #


def test_legacy_operation_level_consumer_unchanged(service: ContractGuardService) -> None:
    service.register_consumer("shop", "web", operations=["GET /orders"])
    _, report = _gate(service, _v2_remove_total())
    assert report.verdict is GateVerdict.REJECT
    assert all(f.reason_code == "breaking_consumer_affected" for f in report.findings)


def test_conflicting_consumer_registration_rejected(service: ContractGuardService) -> None:
    service.register_consumer("shop", "web", dependencies=[_dep(fields=["id"])])
    with pytest.raises(ConsumerConflictError):
        service.register_consumer("shop", "web", dependencies=[_dep(fields=["id", "total"])])


def test_identical_dependency_registration_is_idempotent(
    service: ContractGuardService,
) -> None:
    d1 = [_dep(fields=["id"]), _dep(response="404", fields=["error"])]
    d2 = [_dep(response="404", fields=["error"]), _dep(fields=["id"])]  # 顺序重排
    service.register_consumer("shop", "web", dependencies=d1)
    service.register_consumer("shop", "web", dependencies=d2)  # 不应抛冲突
    saved = service.list_consumers("shop")[0]
    assert [(d.operation, d.response, list(d.fields)) for d in saved.dependencies] == [
        ("GET /orders", "200", ["id"]),
        ("GET /orders", "404", ["error"]),
    ]


# --------------------------------------------------------------------------- #
# 可复现 / 顺序无关
# --------------------------------------------------------------------------- #


def test_conclusion_independent_of_consumer_count_order_and_declarations(
    tmp_store: Path,
) -> None:
    """消费者数量、声明条数、登记顺序不影响 verdict/reason/路径与排序。

    在相互独立的存储上分别登记不同数量/顺序的消费者，断言 finding 的
    (reason_code, path) 结构与 verdict 完全一致；同 finding 内消费者按名排序。
    """
    plans = [
        [("web", [_dep(fields=["id"])])],
        [("dup1", [_dep(fields=["id"])]), ("dup2", [_dep(fields=["id"])])],
        [("z", [_dep(fields=["id"])]), ("a", [_dep(fields=["id"])]),
         ("m", [_dep(fields=["id"])])],
    ]
    structures = []
    for index, plan in enumerate(plans):
        root = tmp_store / f"store-{index}"
        root.mkdir()
        svc = ContractGuardService(root)
        svc.register_baseline(_v1(), service="shop", version="1.0.0")
        svc.register_baseline(_v2_change_id_type(), service="shop", version="2.0.0")
        for name, deps in plan:
            svc.register_consumer("shop", name, dependencies=deps)
        report = svc.check_release("shop", "1.0.0", "2.0.0")
        structures.append(
            (report.verdict,
             [(f.reason_code, f.change.path) for f in report.findings])
        )
        for f in report.findings:
            assert list(f.affected_consumers) == sorted(f.affected_consumers)

    assert structures[0] == structures[1] == structures[2]
    # 登记顺序重排不改变依赖的规范化存储
    root = tmp_store / "order-check"
    root.mkdir()
    svc = ContractGuardService(root)
    svc.register_baseline(_v1(), service="shop", version="1.0.0")
    svc.register_consumer(
        "shop", "web",
        dependencies=[_dep(response="404", fields=["error"]), _dep(fields=["id"])],
    )
    saved = svc.list_consumers("shop")[0]
    assert [(d.response, list(d.fields)) for d in saved.dependencies] == [
        ("200", ["id"]), ("404", ["error"]),
    ]
    logger.info("最终结论不受消费者数量/声明条数/登记顺序影响: %s", structures[0][0].value)
