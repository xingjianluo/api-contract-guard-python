"""两类曾被告门禁放过的破坏性变更的回归测试。

1. 请求体整体必填收紧（可以不带 -> 必须带）：调用该操作的已登记消费者
   必须被判 reject，且与“某个字段变必填”（字段级必填）在原因上可区分；
2. 消费者只声明依赖某响应里的嵌套字段，其上层对象被整体删除/整体改掉
   时，必须算命中其依赖内容并判 reject；未声明依赖的响应/字段变化
   仍不得被误算成受影响。

结论不得随消费者数量、登记条数、登记先后、响应声明顺序或重复判定而改变。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from contract_guard.models import GateReport, GateVerdict
from contract_guard.service import ContractGuardService
from tests.conftest import dump
from tests.contract_factory import response_contract

logger = logging.getLogger("contract_tests.gate_breaking_hits")


# --------------------------------------------------------------------------- #
# 契约构造
# --------------------------------------------------------------------------- #


def _post_users(body_required: bool, *, field_required: bool = False) -> dict:
    """POST /users：带 name 字段的请求体 + 201 无响应体。"""
    raw = response_contract(
        {"201": None},
        body={"name": {"type": "string"}},
        body_required=body_required,
        path="/users",
        method="post",
    )
    if field_required:
        raw["paths"]["/users"]["post"]["requestBody"]["content"][
            "application/json"]["schema"]["required"] = ["name"]
    return raw


def _get_users_v1() -> dict:
    """GET /users：200(id/profile.name/profile.avatar) + 404(error)。"""
    return response_contract(
        {
            "200": {
                "id": {"type": "integer"},
                "profile": {
                    "type": "object",
                    "properties": {
                        "avatar": {"type": "string"},
                        "name": {"type": "string"},
                    },
                },
            },
            "404": {"error": {"type": "string"}},
        },
        path="/users",
        method="get",
    )


def _dep(operation: str, response: str, fields=()) -> dict:
    return {"operation": operation, "response": response, "fields": list(fields)}


def _gate(svc: ContractGuardService, new_raw: dict, label: str) -> GateReport:
    svc.register_baseline(new_raw, service="shop", version="2.0.0")
    diff = svc.diff_versions("shop", "1.0.0", "2.0.0")
    report = svc.gate.evaluate(diff)
    logger.info("判定依据: 破坏性变更=%s 消费者=%s",
                [(c.path, c.reason_code) for c in diff.changes
                 if c.severity.value == "breaking"],
                {c.consumer: {
                    "operations": list(c.operations),
                    "dependencies": [
                        (d.operation, d.response, list(d.fields))
                        for d in c.dependencies
                    ],
                } for c in svc.list_consumers("shop")})
    logger.info("最终结论[%s]: verdict=%s findings=%s", label, report.verdict.value,
                [(f.reason_code, f.change.path, f.change.reason_code,
                  list(f.affected_consumers)) for f in report.findings])
    return report


@pytest.fixture()
def body_svc(tmp_store: Path) -> ContractGuardService:
    svc = ContractGuardService(tmp_store)
    svc.register_baseline(_post_users(body_required=False),
                          service="shop", version="1.0.0")
    logger.info("输入契约 v1:\n%s", dump("v1", _post_users(body_required=False)))
    return svc


@pytest.fixture()
def nested_svc(tmp_store: Path) -> ContractGuardService:
    svc = ContractGuardService(tmp_store)
    svc.register_baseline(_get_users_v1(), service="shop", version="1.0.0")
    logger.info("输入契约 v1:\n%s", dump("v1", _get_users_v1()))
    return svc


# --------------------------------------------------------------------------- #
# 场景一：请求体整体必填收紧
# --------------------------------------------------------------------------- #


def test_body_required_tightening_rejects_dependent_consumer(
    body_svc: ContractGuardService,
) -> None:
    """请求体可以不带 -> 必须带：依赖该操作的消费者判 reject。"""
    body_svc.register_consumer(
        "shop", "web", dependencies=[_dep("POST /users", "201")])
    new_raw = _post_users(body_required=True)
    logger.info("输入契约 v2:\n%s", dump("v2", new_raw))
    report = _gate(body_svc, new_raw, "请求体整体必填收紧")
    assert report.verdict is GateVerdict.REJECT
    hit = [f for f in report.findings if f.affected]
    assert len(hit) == 1
    assert hit[0].reason_code == "breaking_consumer_dependency_hit"
    assert hit[0].affected_consumers == ("web",)
    # 变更自身原因码是整体级，而非字段级必填
    assert hit[0].change.reason_code == "request_body_required_added"
    assert "request_body_required_added" in hit[0].message


def test_body_required_tightening_distinguished_from_field_level(
    tmp_store: Path,
) -> None:
    """整体必填收紧与“某字段变必填”都会 reject，但原因码明确区分。"""
    reports = {}
    for label, new_raw in (
        ("body", _post_users(body_required=True)),
        ("field", _post_users(body_required=False, field_required=True)),
    ):
        root = tmp_store / label
        root.mkdir()
        svc = ContractGuardService(root)
        svc.register_baseline(_post_users(body_required=False),
                              service="shop", version="1.0.0")
        svc.register_consumer(
            "shop", "web", dependencies=[_dep("POST /users", "201")])
        logger.info("输入契约 v2[%s]:\n%s", label, dump(f"v2-{label}", new_raw))
        reports[label] = _gate(svc, new_raw, label)

    assert reports["body"].verdict is GateVerdict.REJECT
    assert reports["field"].verdict is GateVerdict.REJECT
    body_reasons = {f.change.reason_code for f in reports["body"].findings}
    field_reasons = {f.change.reason_code for f in reports["field"].findings}
    logger.info("最终结论: 整体级=%s 字段级=%s", body_reasons, field_reasons)
    assert body_reasons == {"request_body_required_added"}
    assert field_reasons == {"request_optional_to_required"}
    assert body_reasons.isdisjoint(field_reasons)


def test_body_tightening_without_any_consumer_only_warns(
    body_svc: ContractGuardService,
) -> None:
    """同样的收紧，没有任何消费者登记时仍然只是 warn。"""
    report = _gate(body_svc, _post_users(body_required=True), "无消费者")
    assert report.verdict is GateVerdict.WARN
    assert {f.reason_code for f in report.findings} == {"breaking_unused"}


def test_body_tightening_legacy_operation_consumer_still_rejects(
    body_svc: ContractGuardService,
) -> None:
    """操作级老消费者行为不变：整体必填收紧照样命中，沿用历史原因码。"""
    body_svc.register_consumer("shop", "legacy", operations=["POST /users"])
    report = _gate(body_svc, _post_users(body_required=True), "老消费者")
    assert report.verdict is GateVerdict.REJECT
    assert {f.reason_code for f in report.findings} == {"breaking_consumer_affected"}


# --------------------------------------------------------------------------- #
# 场景二：依赖嵌套字段，上层对象被整体删除/改掉
# --------------------------------------------------------------------------- #


def _v2_without_profile() -> dict:
    raw = _get_users_v1()
    del raw["paths"]["/users"]["get"]["responses"]["200"]["content"][
        "application/json"]["schema"]["properties"]["profile"]
    return raw


def _v2_profile_type_changed() -> dict:
    raw = _get_users_v1()
    raw["paths"]["/users"]["get"]["responses"]["200"]["content"][
        "application/json"]["schema"]["properties"]["profile"] = {
        "type": "string"}
    return raw


def test_nested_field_parent_object_removed_is_reject(
    nested_svc: ContractGuardService,
) -> None:
    """依赖 200.profile.name；整个 profile 对象被删 -> 命中依赖，reject。"""
    nested_svc.register_consumer(
        "shop", "web",
        dependencies=[_dep("GET /users", "200", ["profile.name"])])
    new_raw = _v2_without_profile()
    logger.info("输入契约 v2:\n%s", dump("v2", new_raw))
    report = _gate(nested_svc, new_raw, "上层对象整体删除")
    assert report.verdict is GateVerdict.REJECT
    hit = [f for f in report.findings if f.affected]
    assert len(hit) == 1
    assert hit[0].reason_code == "breaking_consumer_dependency_hit"
    assert hit[0].affected_consumers == ("web",)
    assert hit[0].change.reason_code == "response_field_removed"
    assert hit[0].change.path.endswith("response-200.profile")


def test_nested_field_parent_object_type_changed_is_reject(
    nested_svc: ContractGuardService,
) -> None:
    """依赖 200.profile.name；profile 整体被改成 string -> 命中，reject。"""
    nested_svc.register_consumer(
        "shop", "web",
        dependencies=[_dep("GET /users", "200", ["profile.name"])])
    new_raw = _v2_profile_type_changed()
    logger.info("输入契约 v2:\n%s", dump("v2", new_raw))
    report = _gate(nested_svc, new_raw, "上层对象整体改型")
    assert report.verdict is GateVerdict.REJECT
    hit = [f for f in report.findings if f.affected]
    assert any(f.change.reason_code == "type_changed" and f.affected_consumers
               for f in hit)


def test_unrelated_field_removal_not_counted_as_hit(
    nested_svc: ContractGuardService,
) -> None:
    """依赖 200.profile.name；被删的是未声明依赖的 200.id -> 不误算。"""
    nested_svc.register_consumer(
        "shop", "web",
        dependencies=[_dep("GET /users", "200", ["profile.name"])])
    new_raw = _get_users_v1()
    schema = new_raw["paths"]["/users"]["get"]["responses"]["200"]["content"][
        "application/json"]["schema"]
    del schema["properties"]["id"]
    schema["required"] = []
    logger.info("输入契约 v2:\n%s", dump("v2", new_raw))
    report = _gate(nested_svc, new_raw, "未依赖字段被删")
    assert report.verdict is GateVerdict.WARN
    assert {f.reason_code for f in report.findings} == \
        {"breaking_operation_used_not_depended"}
    assert all(not f.affected for f in report.findings)


def test_unrelated_response_removal_not_counted_as_hit(
    nested_svc: ContractGuardService,
) -> None:
    """依赖 200.profile.name；被删的是未声明依赖的 404 -> 不误算。"""
    nested_svc.register_consumer(
        "shop", "web",
        dependencies=[_dep("GET /users", "200", ["profile.name"])])
    new_raw = _get_users_v1()
    del new_raw["paths"]["/users"]["get"]["responses"]["404"]
    logger.info("输入契约 v2:\n%s", dump("v2", new_raw))
    report = _gate(nested_svc, new_raw, "未依赖响应被删")
    assert report.verdict is GateVerdict.WARN
    assert {f.reason_code for f in report.findings} == \
        {"breaking_operation_used_not_depended"}


def test_sibling_nested_field_removal_not_counted_as_hit(
    nested_svc: ContractGuardService,
) -> None:
    """依赖 200.profile.name；被删的是同层兄弟 profile.avatar -> 不误算。"""
    nested_svc.register_consumer(
        "shop", "web",
        dependencies=[_dep("GET /users", "200", ["profile.name"])])
    new_raw = _get_users_v1()
    del new_raw["paths"]["/users"]["get"]["responses"]["200"]["content"][
        "application/json"]["schema"]["properties"]["profile"][
        "properties"]["avatar"]
    logger.info("输入契约 v2:\n%s", dump("v2", new_raw))
    report = _gate(nested_svc, new_raw, "兄弟嵌套字段被删")
    assert report.verdict is GateVerdict.WARN
    assert {f.reason_code for f in report.findings} == \
        {"breaking_operation_used_not_depended"}


# --------------------------------------------------------------------------- #
# 确定性：数量/顺序/重复判定/响应声明顺序均不改变结论
# --------------------------------------------------------------------------- #


def _signature(report: GateReport):
    return (
        report.verdict,
        [(f.reason_code, f.change.path, f.change.reason_code)
         for f in report.findings],
    )


def test_hit_conclusion_independent_of_consumer_count_and_order(
    tmp_store: Path,
) -> None:
    """命中结论不随消费者数量、登记条数与登记先后改变。"""
    plans = [
        [("web", [_dep("GET /users", "200", ["profile.name"])])],
        [("b", [_dep("GET /users", "200", ["profile.name"])]),
         ("a", [_dep("GET /users", "200", ["profile.name"])])],
        [("z", [_dep("GET /users", "200", ["profile.name", "profile"])]),
         ("m", [_dep("GET /users", "200", ["profile.name"])]),
         ("a", [_dep("GET /users", "200", ["profile.name"])])],
    ]
    signatures = []
    for index, plan in enumerate(plans):
        root = tmp_store / f"store-{index}"
        root.mkdir()
        svc = ContractGuardService(root)
        svc.register_baseline(_get_users_v1(), service="shop", version="1.0.0")
        for name, deps in plan:
            svc.register_consumer("shop", name, dependencies=deps)
        report = _gate(svc, _v2_without_profile(), f"plan-{index}")
        signatures.append(_signature(report))
        for f in report.findings:
            assert list(f.affected_consumers) == sorted(f.affected_consumers)
    assert signatures[0] == signatures[1] == signatures[2]
    assert signatures[0][0] is GateVerdict.REJECT
    logger.info("最终结论: 三种登记计划结论完全一致: %s", signatures[0][0].value)


def test_repeated_evaluation_is_byte_identical(
    nested_svc: ContractGuardService,
) -> None:
    """同一对版本反复判定，结论与原因完全一致。"""
    nested_svc.register_consumer(
        "shop", "web",
        dependencies=[_dep("GET /users", "200", ["profile.name"])])
    nested_svc.register_baseline(
        _v2_without_profile(), service="shop", version="2.0.0")
    reports = [nested_svc.check_release("shop", "1.0.0", "2.0.0")
               for _ in range(3)]
    sigs = [
        (r.verdict, [(f.reason_code, f.change.path, f.message,
                      f.affected_consumers) for f in r.findings])
        for r in reports
    ]
    assert sigs[0] == sigs[1] == sigs[2]
    logger.info("最终结论: 重复判定 x3 完全一致: %s", sigs[0][0].value)


def test_response_declaration_order_shuffle_does_not_change_conclusion(
    tmp_store: Path,
) -> None:
    """响应声明顺序被打乱后，判定结果与原因完全一致。"""
    signatures = []
    for index, codes in enumerate((("200", "404"), ("404", "200"))):
        root = tmp_store / f"order-{index}"
        root.mkdir()
        svc = ContractGuardService(root)
        v1 = _get_users_v1()
        responses = v1["paths"]["/users"]["get"]["responses"]
        v1["paths"]["/users"]["get"]["responses"] = {
            code: responses[code] for code in codes
        }
        svc.register_baseline(v1, service="shop", version="1.0.0")
        svc.register_consumer(
            "shop", "web",
            dependencies=[_dep("GET /users", "200", ["profile.name"])])
        report = _gate(svc, _v2_without_profile(), f"声明顺序-{codes}")
        signatures.append(_signature(report))
    assert signatures[0] == signatures[1]
    assert signatures[0][0] is GateVerdict.REJECT
    logger.info("最终结论: 响应声明顺序重排后结论一致: %s", signatures[0][0].value)
