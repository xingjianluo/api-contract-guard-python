"""门禁命中判定回归：两类曾被放过（只 warn 不 reject）的破坏。

场景 A：请求体整体必填收紧（可以不带 -> 必须带）
    * 命中所有调用该操作的已登记消费者（含只登记响应依赖的）-> reject；
    * 原因码 request_body_required_added 与字段级必填
      request_optional_to_required 可区分，不混淆；
    * 未登记消费者时仍只是 warn / breaking_unused。

场景 B：消费者只声明依赖某响应里的嵌套字段（如 profile.avatar），
    新版本把上层对象 profile 整体删除或整体改型
    * 视为命中其依赖内容 -> reject / breaking_consumer_dependency_hit；
    * 未声明依赖的响应/字段变化不得误算命中（仍 warn）。

两类命中均与消费者数量、登记条数、登记先后无关；同一对版本反复判定、
响应声明顺序打乱后判定，结论与原因完全一致。
"""

from __future__ import annotations

import logging
from pathlib import Path

from contract_guard.models import GateVerdict
from contract_guard.service import ContractGuardService
from tests.contract_factory import response_contract

logger = logging.getLogger("contract_tests.gate_breaking_hits")


# --------------------------------------------------------------------------- #
# 契约构造
# --------------------------------------------------------------------------- #


def _body_contract(*, body_required: bool, name_required: bool = False) -> dict:
    """POST /users：201 无响应体；请求体 {name}，整体必填性/字段必填性可调。"""
    raw = response_contract(
        {"201": None},
        body={"name": {"type": "string"}},
        body_required=body_required,
        path="/users",
        method="post",
    )
    if name_required:
        schema = raw["paths"]["/users"]["post"]["requestBody"]["content"][
            "application/json"]["schema"]
        schema["required"] = ["name"]
    return raw


_PROFILE_RESPONSES = {
    "200": {
        "id": {"type": "integer"},
        "profile": {
            "type": "object",
            "properties": {"avatar": {"type": "string"}},
        },
        "settings": {
            "type": "object",
            "properties": {"theme": {"type": "string"}},
        },
    },
    "404": {"error": {"type": "string"}},
}


def _profile_contract(responses: dict, *, reverse: bool = False) -> dict:
    """GET /users；reverse=True 时故意打乱响应声明顺序。"""
    items = list(responses.items())
    if reverse:
        items.reverse()
    return response_contract(dict(items), path="/users", method="get")


def _profile_v1(*, reverse: bool = False) -> dict:
    return _profile_contract(_PROFILE_RESPONSES, reverse=reverse)


def _dep(fields, operation="GET /users", response="200"):
    return {"operation": operation, "response": response, "fields": list(fields)}


def _gate(svc: ContractGuardService, new_raw, version="2.0.0"):
    svc.register_baseline(new_raw, service="shop", version=version)
    report = svc.check_release("shop", "1.0.0", version)
    logger.info(
        "判定依据: breaking=%s",
        [(f.change.path, f.change.reason_code, f.reason_code,
          list(f.affected_consumers)) for f in report.findings],
    )
    logger.info("最终结论: verdict=%s", report.verdict.value)
    return report


def _signature(report) -> tuple:
    return (
        report.verdict,
        [(f.reason_code, f.change.path, f.change.reason_code)
         for f in report.findings],
    )


# --------------------------------------------------------------------------- #
# 场景 A：请求体整体必填收紧
# --------------------------------------------------------------------------- #


def test_body_required_tightening_rejects_dependency_consumer(
    tmp_store: Path,
) -> None:
    """只登记了响应依赖的消费者，请求体可选->必填仍命中它 -> reject。"""
    svc = ContractGuardService(tmp_store)
    svc.register_baseline(_body_contract(body_required=False),
                          service="shop", version="1.0.0")
    svc.register_consumer("shop", "web", dependencies=[
        _dep([], operation="POST /users", response="201")])
    logger.info("输入契约: POST /users 请求体 可选->必填; 消费者 web 依赖 201 响应")
    report = _gate(svc, _body_contract(body_required=True))
    assert report.verdict is GateVerdict.REJECT
    hit = [f for f in report.findings if f.affected]
    assert len(hit) == 1
    assert hit[0].reason_code == "breaking_consumer_dependency_hit"
    assert hit[0].change.reason_code == "request_body_required_added"
    assert hit[0].affected_consumers == ("web",)


def test_body_required_tightening_distinct_from_field_level_required(
    tmp_store: Path,
) -> None:
    """整体必填收紧与“某字段变必填”都能 reject，但原因码可区分、不混淆。"""
    # 整体级：请求体可选 -> 必填
    svc_body = ContractGuardService(tmp_store / "body-level")
    svc_body.register_baseline(_body_contract(body_required=False),
                               service="shop", version="1.0.0")
    svc_body.register_consumer("shop", "web", dependencies=[
        _dep([], operation="POST /users", response="201")])
    body_report = _gate(svc_body, _body_contract(body_required=True))

    # 字段级：请求体始终可选，仅字段 name 可选 -> 必填
    svc_field = ContractGuardService(tmp_store / "field-level")
    svc_field.register_baseline(_body_contract(body_required=False),
                                service="shop", version="1.0.0")
    svc_field.register_consumer("shop", "web", dependencies=[
        _dep([], operation="POST /users", response="201")])
    field_report = _gate(
        svc_field, _body_contract(body_required=False, name_required=True))

    assert body_report.verdict is GateVerdict.REJECT
    assert field_report.verdict is GateVerdict.REJECT
    body_reasons = {f.change.reason_code for f in body_report.findings}
    field_reasons = {f.change.reason_code for f in field_report.findings}
    logger.info("最终结论: 整体级=%s 字段级=%s", body_reasons, field_reasons)
    assert body_reasons == {"request_body_required_added"}
    assert field_reasons == {"request_optional_to_required"}
    assert body_reasons.isdisjoint(field_reasons)


def test_body_required_tightening_without_consumers_only_warns(
    tmp_store: Path,
) -> None:
    svc = ContractGuardService(tmp_store)
    svc.register_baseline(_body_contract(body_required=False),
                          service="shop", version="1.0.0")
    report = _gate(svc, _body_contract(body_required=True))
    assert report.verdict is GateVerdict.WARN
    assert {f.reason_code for f in report.findings} == {"breaking_unused"}


def test_body_required_tightening_legacy_consumer_reason_unchanged(
    tmp_store: Path,
) -> None:
    """操作级老消费者命中时沿用历史原因码 breaking_consumer_affected。"""
    svc = ContractGuardService(tmp_store)
    svc.register_baseline(_body_contract(body_required=False),
                          service="shop", version="1.0.0")
    svc.register_consumer("shop", "legacy", operations=["POST /users"])
    report = _gate(svc, _body_contract(body_required=True))
    assert report.verdict is GateVerdict.REJECT
    assert all(
        f.reason_code == "breaking_consumer_affected" for f in report.findings
    )


def test_body_tightening_on_other_operation_is_unused(tmp_store: Path) -> None:
    """收紧发生在 POST /users，消费者只调用 GET /orders -> 不误算命中。"""
    svc = ContractGuardService(tmp_store)
    svc.register_baseline(_body_contract(body_required=False),
                          service="shop", version="1.0.0")
    svc.register_consumer("shop", "billing", operations=["GET /orders"])
    report = _gate(svc, _body_contract(body_required=True))
    assert report.verdict is GateVerdict.WARN
    assert {f.reason_code for f in report.findings} == {"breaking_unused"}


def test_body_tightening_verdict_independent_of_consumers_and_repeat(
    tmp_store: Path,
) -> None:
    """消费者数量/登记顺序不影响结论；同一对版本反复判定结果一致。"""
    plans = [
        [("web", [_dep([], operation="POST /users", response="201")])],
        [("z", [_dep([], operation="POST /users", response="201")]),
         ("a", [_dep([], operation="POST /users", response="201")]),
         ("m", [_dep([], operation="POST /users", response="201")])],
    ]
    signatures = []
    for index, plan in enumerate(plans):
        svc = ContractGuardService(tmp_store / f"store-{index}")
        svc.register_baseline(_body_contract(body_required=False),
                              service="shop", version="1.0.0")
        for name, deps in plan:
            svc.register_consumer("shop", name, dependencies=deps)
        report = _gate(svc, _body_contract(body_required=True))
        # 同一对版本反复判定，结论与原因完全一致
        assert _signature(report) == _signature(
            svc.check_release("shop", "1.0.0", "2.0.0"))
        for f in report.findings:
            assert list(f.affected_consumers) == sorted(f.affected_consumers)
        signatures.append(_signature(report))
    assert signatures[0] == signatures[1]
    logger.info("最终结论不受消费者数量/登记顺序影响: %s", signatures[0][0].value)


# --------------------------------------------------------------------------- #
# 场景 B：依赖嵌套字段，上层对象被整体删除/改型
# --------------------------------------------------------------------------- #


def test_nested_field_parent_object_removed_is_reject(tmp_store: Path) -> None:
    """依赖 200.profile.avatar；新版本整体删除 profile -> reject。"""
    svc = ContractGuardService(tmp_store)
    svc.register_baseline(_profile_v1(), service="shop", version="1.0.0")
    svc.register_consumer("shop", "web",
                          dependencies=[_dep(["profile.avatar"])])
    logger.info("输入契约: GET /users 200 删除整个 profile 对象; "
                "消费者 web 依赖 200.profile.avatar")
    v2 = _profile_contract({
        "200": {
            "id": {"type": "integer"},
            "settings": _PROFILE_RESPONSES["200"]["settings"],
        },
        "404": {"error": {"type": "string"}},
    })
    report = _gate(svc, v2)
    assert report.verdict is GateVerdict.REJECT
    hit = [f for f in report.findings if f.affected]
    assert len(hit) == 1
    assert hit[0].reason_code == "breaking_consumer_dependency_hit"
    assert hit[0].change.reason_code == "response_field_removed"
    assert hit[0].change.path.endswith("#response-200.profile")
    assert hit[0].affected_consumers == ("web",)


def test_nested_field_parent_object_retyped_is_reject(tmp_store: Path) -> None:
    """依赖 200.profile.avatar；profile 由 object 整体改为 string -> reject。"""
    svc = ContractGuardService(tmp_store)
    svc.register_baseline(_profile_v1(), service="shop", version="1.0.0")
    svc.register_consumer("shop", "web",
                          dependencies=[_dep(["profile.avatar"])])
    v2 = _profile_contract({
        "200": {
            "id": {"type": "integer"},
            "profile": {"type": "string"},
            "settings": _PROFILE_RESPONSES["200"]["settings"],
        },
        "404": {"error": {"type": "string"}},
    })
    report = _gate(svc, v2)
    assert report.verdict is GateVerdict.REJECT
    assert report.findings
    assert all(f.affected for f in report.findings)
    assert all(
        f.reason_code == "breaking_consumer_dependency_hit"
        for f in report.findings
    )
    assert any(f.change.reason_code == "type_changed" for f in report.findings)


def test_unrelated_object_removal_does_not_hit(tmp_store: Path) -> None:
    """依赖 200.profile.avatar；被删的是 settings -> 不误算命中，仅 warn。"""
    svc = ContractGuardService(tmp_store)
    svc.register_baseline(_profile_v1(), service="shop", version="1.0.0")
    svc.register_consumer("shop", "web",
                          dependencies=[_dep(["profile.avatar"])])
    v2 = _profile_contract({
        "200": {
            "id": {"type": "integer"},
            "profile": _PROFILE_RESPONSES["200"]["profile"],
        },
        "404": {"error": {"type": "string"}},
    })
    report = _gate(svc, v2)
    assert report.verdict is GateVerdict.WARN
    assert {f.reason_code for f in report.findings} == \
        {"breaking_operation_used_not_depended"}
    assert all(not f.affected for f in report.findings)


def test_unrelated_response_change_does_not_hit(tmp_store: Path) -> None:
    """依赖 200.profile.avatar；404 响应被删 -> 不误算命中，仅 warn。"""
    svc = ContractGuardService(tmp_store)
    svc.register_baseline(_profile_v1(), service="shop", version="1.0.0")
    svc.register_consumer("shop", "web",
                          dependencies=[_dep(["profile.avatar"])])
    v2 = _profile_contract({"200": dict(_PROFILE_RESPONSES["200"])})
    report = _gate(svc, v2)
    assert report.verdict is GateVerdict.WARN
    assert {f.reason_code for f in report.findings} == \
        {"breaking_operation_used_not_depended"}


def test_sibling_field_dependency_not_falsely_hit(tmp_store: Path) -> None:
    """只依赖 200.id；profile 被整体删除 -> 不误算命中，仅 warn。"""
    svc = ContractGuardService(tmp_store)
    svc.register_baseline(_profile_v1(), service="shop", version="1.0.0")
    svc.register_consumer("shop", "web", dependencies=[_dep(["id"])])
    v2 = _profile_contract({
        "200": {
            "id": {"type": "integer"},
            "settings": _PROFILE_RESPONSES["200"]["settings"],
        },
        "404": {"error": {"type": "string"}},
    })
    report = _gate(svc, v2)
    assert report.verdict is GateVerdict.WARN
    assert {f.reason_code for f in report.findings} == \
        {"breaking_operation_used_not_depended"}


def test_nested_hit_stable_across_response_order_and_repeat(
    tmp_store: Path,
) -> None:
    """响应声明顺序打乱、同一对版本反复判定：结论与原因完全一致。"""
    svc = ContractGuardService(tmp_store / "ordered")
    svc.register_baseline(_profile_v1(), service="shop", version="1.0.0")
    svc.register_consumer("shop", "web",
                          dependencies=[_dep(["profile.avatar"])])
    v2 = _profile_contract({
        "404": {"error": {"type": "string"}},
        "200": {
            "id": {"type": "integer"},
            "settings": _PROFILE_RESPONSES["200"]["settings"],
        },
    })
    ordered = _gate(svc, v2)

    shuffled_svc = ContractGuardService(tmp_store / "shuffled")
    shuffled_svc.register_baseline(_profile_v1(reverse=True),
                                   service="shop", version="1.0.0")
    shuffled_svc.register_consumer("shop", "web",
                                   dependencies=[_dep(["profile.avatar"])])
    shuffled = _gate(shuffled_svc, _profile_contract({
        "200": {
            "id": {"type": "integer"},
            "settings": _PROFILE_RESPONSES["200"]["settings"],
        },
        "404": {"error": {"type": "string"}},
    }, reverse=True))

    assert _signature(ordered) == _signature(shuffled)
    # 反复判定一致
    again = svc.check_release("shop", "1.0.0", "2.0.0")
    assert _signature(ordered) == _signature(again)
    assert ordered.verdict is GateVerdict.REJECT
    logger.info("响应顺序打乱/反复判定结论一致: %s", ordered.verdict.value)


def test_nested_hit_independent_of_consumer_count_and_order(
    tmp_store: Path,
) -> None:
    """嵌套命中结论不因消费者数量、登记条数、登记先后而改变。"""
    v2 = _profile_contract({
        "200": {
            "id": {"type": "integer"},
            "settings": _PROFILE_RESPONSES["200"]["settings"],
        },
        "404": {"error": {"type": "string"}},
    })
    plans = [
        [("web", [_dep(["profile.avatar"])])],
        [("dup1", [_dep(["profile.avatar"])]),
         ("dup2", [_dep(["profile.avatar", "id"])])],
        [("z", [_dep(["profile.avatar"])]),
         ("a", [_dep(["profile.avatar"])]),
         ("m", [_dep(["profile.avatar"])])],
    ]
    signatures = []
    for index, plan in enumerate(plans):
        svc = ContractGuardService(tmp_store / f"store-{index}")
        svc.register_baseline(_profile_v1(), service="shop", version="1.0.0")
        for name, deps in plan:
            svc.register_consumer("shop", name, dependencies=deps)
        report = _gate(svc, v2)
        for f in report.findings:
            assert list(f.affected_consumers) == sorted(f.affected_consumers)
        signatures.append(_signature(report))
    assert signatures[0] == signatures[1] == signatures[2]
    assert signatures[0][0] is GateVerdict.REJECT
    logger.info("最终结论不受消费者数量/声明条数/登记顺序影响: %s",
                signatures[0][0].value)
