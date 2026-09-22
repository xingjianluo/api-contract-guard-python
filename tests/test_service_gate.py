"""发布门禁：影响面判定、原因可区分、重复判定一致。"""
from __future__ import annotations

import pytest

from app.contractguard.errors import (
    BaselineNotFoundError,
    ContractParseError,
)

from .conftest import log_case, make_contract, object_schema


V1 = make_contract({
    "/users": {"GET": {"responses": {200: object_schema(
        ("id", True, {"type": "integer"}),
        ("name", True, {"type": "string"}),
        ("role", False, {"type": "string", "enum": ["a", "b"]}),
    )}}},
    "/orders": {"GET": {"responses": {200: object_schema(
        ("oid", True, {"type": "integer"}))}}},
})

# users: id 类型改变（破坏，web 只消费 name，故不影响 web）；
# name 必填->可选（响应方向破坏，web 消费 name => 影响 web）；
# orders 整个接口移除（破坏，无人登记 => unused）。
V2 = make_contract({
    "/users": {"GET": {"responses": {200: object_schema(
        ("id", True, {"type": "string"}),
        ("name", False, {"type": "string"}),
        ("role", False, {"type": "string", "enum": ["a", "b"]}),
    )}}},
})


def test_gate_distinguishes_consumer_impact_and_unused_breaking(service):
    service.register_baseline(V1, version="1.0")
    service.register_baseline(V2, version="2.0")
    expectation = {
        "version": "1.0",
        "used_fields": {"GET /users": ["response[200].name"]},
    }
    service.register_consumer(expectation, consumer="web")

    verdict = service.gate("1.0", "2.0")
    assert verdict.allowed is False

    impact_consumers = {i["consumer"] for i in verdict.consumer_impacts}
    assert impact_consumers == {"web"}
    web = verdict.consumer_impacts[0]
    assert web["reason"] == "breaking_change_impacts_registered_consumer"
    assert all(c["location"].endswith("response[200].name") or
               "response[200].name" in c["location"] for c in web["changes"])

    unused_locs = {c.location for c in verdict.unused_breaking}
    assert any(c.kind.value == "type_changed" and "id" in c.location
               for c in verdict.unused_breaking)
    assert any("GET /orders" in loc for loc in unused_locs)

    reason_text = "\n".join(verdict.reasons)
    assert "breaking_change_impacts_registered_consumer" in reason_text
    assert "unused_breaking_change" in reason_text

    log_case("门禁区分两类破坏原因", input_contract=[V1, V2], expectation=expectation,
             rationale="name 影响已登记消费者；id/orders 属未被使用的破坏性变更",
             verdict=verdict.to_dict())


def test_field_scope_not_over_reported(service):
    """消费者只使用 name，则 id 的类型变化不得算到该消费者头上。"""
    service.register_baseline(V1, version="1.0")
    service.register_baseline(V2, version="2.0")
    service.register_consumer(
        {"version": "1.0", "used_fields": {"GET /users": ["response[200].id"]}},
        consumer="api-client",
    )
    verdict = service.gate("1.0", "2.0")
    client = next(i for i in verdict.consumer_impacts if i["consumer"] == "api-client")
    locs = {c["location"] for c in client["changes"]}
    assert all("name" not in loc for loc in locs)
    assert any("id" in loc for loc in locs)


def test_gate_allows_when_only_compatible_changes(service):
    v1 = make_contract({"/u": {"POST": {
        "request": object_schema(("n", True, {"type": "string"})),
        "responses": {201: object_schema()}}}})
    # 请求新增可选字段 => 兼容
    v2 = make_contract({"/u": {"POST": {
        "request": object_schema(("n", True, {"type": "string"}),
                                 ("m", False, {"type": "string"})),
        "responses": {201: object_schema()}}}})
    service.register_baseline(v1, version="1.0")
    service.register_baseline(v2, version="2.0")
    service.register_consumer(
        {"version": "1.0", "used_endpoints": [["POST", "/u"]]}, consumer="c")
    verdict = service.gate("1.0", "2.0")
    assert verdict.allowed is True
    assert verdict.reasons == ()
    log_case("纯兼容变更放行", input_contract=[v1, v2], rationale="请求新增可选字段",
             verdict=verdict.to_dict())


def test_repeated_compare_returns_identical_result(service):
    service.register_baseline(V1, version="1.0")
    service.register_baseline(V2, version="2.0")
    r1 = service.compare("1.0", "2.0").to_dict()
    r2 = service.compare("1.0", "2.0").to_dict()
    r3 = service.compare("2.0", "1.0").to_dict()
    assert r1 == r2
    # 反向比对也是确定性的独立结论
    assert r3["old_version"] == "2.0"
    log_case("重复判定一致", input_contract=[V1, V2], rationale="缓存 + 确定性排序",
             verdict={"forward_equal": r1 == r2, "reverse_deterministic": True})


def test_malformed_contract_via_service(service):
    with pytest.raises(ContractParseError) as ei:
        service.register_baseline({"paths": {}}, version="x")
    assert ei.value.reason == "empty_contract"
    log_case("服务层透归一化解析错误", input_contract={"paths": {}},
             rationale="底层错误不外泄，reason 可区分", verdict={"reason": ei.value.reason})


def test_compare_unknown_baseline(service):
    service.register_baseline(V1, version="1.0")
    with pytest.raises(BaselineNotFoundError) as ei:
        service.compare("1.0", "9.9")
    assert ei.value.reason == "unknown_version"


@pytest.mark.parametrize("payload,reason", [
    ({"version": "1.0", "used_endpoints": [["GET"]]}, "invalid_endpoint_ref"),
    ({"version": "1.0", "used_endpoints": []}, "empty_expectation"),
    ({"version": ""}, "invalid_expectation_version"),
    ({"version": "1.0", "used_fields": {"BADKEY": ["x"]}}, "invalid_field_key"),
])
def test_malformed_expectations(service, payload, reason):
    with pytest.raises(ContractParseError) as ei:
        service.register_consumer(payload, consumer="web")
    assert ei.value.reason == reason
    log_case(f"畸形消费者期望 {reason}", input_contract=payload,
             rationale=f"拒绝原因 {reason}", verdict={"reason": ei.value.reason})


def test_consumer_conflict_rejected(service):
    from app.contractguard.errors import ConsumerConflictError

    service.register_baseline(V1, version="1.0")
    service.register_consumer({"version": "1.0", "used_endpoints": [["GET", "/users"]]},
                              consumer="web")
    with pytest.raises(ConsumerConflictError):
        service.register_consumer({"version": "1.0", "used_endpoints": [["GET", "/orders"]]},
                                  consumer="web")
