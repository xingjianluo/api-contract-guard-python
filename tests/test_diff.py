"""兼容性分级、判定依据与结论可复现性。"""
from __future__ import annotations

from app.contractguard.models import ChangeKind, ChangeLevel
from app.contractguard.parser import ContractParser
from app.contractguard.diff import ContractDiffer

from .conftest import log_case, make_contract, object_schema

parser = ContractParser()
differ = ContractDiffer()


def diff_contracts(v1: dict, v2: dict):
    return differ.diff(parser.parse(v1, version="v1"), parser.parse(v2, version="v2"))


def kinds(result, level=None):
    out = [c.kind for c in result.changes if level is None or c.level == level]
    return sorted(out, key=lambda k: k.value)


def test_identical_contracts():
    c = make_contract({"/u": {"GET": {"responses": {200: object_schema(
        ("id", True, {"type": "integer"}))}}}})
    r = diff_contracts(c, c)
    assert r.compatible and r.changes == ()
    log_case("相同契约", input_contract=c, rationale=r.rationale, verdict=r.to_dict())


def test_added_endpoint_is_added_level():
    v1 = make_contract({"/u": {"GET": {"responses": {200: object_schema()}}}})
    v2 = make_contract({
        "/u": {"GET": {"responses": {200: object_schema()}}},
        "/v": {"GET": {"responses": {200: object_schema()}}},
    })
    r = diff_contracts(v1, v2)
    assert r.compatible
    assert kinds(r) == [ChangeKind.ENDPOINT_ADDED]
    assert r.changes[0].level == ChangeLevel.ADDED
    log_case("新增接口", input_contract=[v1, v2], rationale=r.rationale, verdict=r.to_dict())


def test_removed_endpoint_is_breaking():
    v1 = make_contract({
        "/u": {"GET": {"responses": {200: object_schema()}}},
        "/k": {"GET": {"responses": {200: object_schema()}}},
    })
    v2 = make_contract({"/k": {"GET": {"responses": {200: object_schema()}}}})
    r = diff_contracts(v1, v2)
    assert not r.compatible
    assert ChangeKind.ENDPOINT_REMOVED in kinds(r)
    log_case("移除接口", input_contract=[v1, v2], rationale=r.rationale, verdict=r.to_dict())


def test_request_direction_rules():
    v1 = make_contract({"/u": {"POST": {
        "request": object_schema(
            ("name", True, {"type": "string"}),
            ("age", False, {"type": "integer", "nullable": True}),
            ("note", False, {"type": "string", "nullable": True}),
        ),
        "responses": {201: object_schema()},
    }}})
    # age: 可选->必填 且 nullable->non-nullable（破坏）；
    # 新增可选字段 tag（兼容）；新增必填字段 code（破坏）
    v2 = make_contract({"/u": {"POST": {
        "request": object_schema(
            ("name", True, {"type": "string"}),
            ("age", True, {"type": "integer"}),
            ("note", False, {"type": "string", "nullable": True}),
            ("tag", False, {"type": "string"}),
            ("code", True, {"type": "string"}),
        ),
        "responses": {201: object_schema()},
    }}})
    r = diff_contracts(v1, v2)
    assert not r.compatible
    assert ChangeKind.OPTIONAL_TO_REQUIRED in kinds(r)
    assert ChangeKind.NULLABLE_TO_NON_NULLABLE in kinds(r)
    assert ChangeKind.FIELD_ADDED_OPTIONAL in [c.kind for c in r.changes]
    assert ChangeKind.FIELD_ADDED_REQUIRED in [c.kind for c in r.changes]
    # 可选新增字段分级必须是 compatible
    add_opt = next(c for c in r.changes if c.kind == ChangeKind.FIELD_ADDED_OPTIONAL)
    assert add_opt.level == ChangeLevel.COMPATIBLE
    log_case("请求方向分级", input_contract=[v1, v2], rationale=r.rationale, verdict=r.to_dict())


def test_response_direction_is_reversed():
    # 响应字段 必填->可选 = 破坏；新增响应字段 = added
    v1 = make_contract({"/u": {"GET": {"responses": {200: object_schema(
        ("id", True, {"type": "integer"}))}}}})
    v2 = make_contract({"/u": {"GET": {"responses": {200: object_schema(
        ("id", False, {"type": "integer"}),
        ("extra", False, {"type": "string"}))}}}})
    r = diff_contracts(v1, v2)
    assert not r.compatible
    k = kinds(r)
    assert ChangeKind.REQUIRED_TO_OPTIONAL in k
    assert ChangeKind.FIELD_ADDED_OPTIONAL in k
    log_case("响应方向反向分级", input_contract=[v1, v2], rationale=r.rationale, verdict=r.to_dict())


def test_type_default_and_enum_changes():
    v1 = make_contract({"/u": {"GET": {"responses": {200: object_schema(
        ("kind", False, {"type": "string", "enum": ["a", "b"], "default": "a"}))}}}})
    v2 = make_contract({"/u": {"GET": {"responses": {200: object_schema(
        ("kind", False, {"type": "integer", "enum": ["a", "b", "c"], "default": "b"}))}}}})
    r = diff_contracts(v1, v2)
    k = kinds(r)
    assert ChangeKind.TYPE_CHANGED in k
    assert ChangeKind.DEFAULT_VALUE_CHANGED in k
    assert ChangeKind.ENUM_VALUE_ADDED in k
    # 响应方向枚举新增 = 破坏
    enum_add = next(c for c in r.changes if c.kind == ChangeKind.ENUM_VALUE_ADDED)
    assert enum_add.breaking is True
    log_case("类型/默认值/枚举", input_contract=[v1, v2], rationale=r.rationale, verdict=r.to_dict())


def test_response_status_added_removed():
    v1 = make_contract({"/u": {"GET": {"responses": {200: object_schema()}}}})
    v2 = make_contract({"/u": {"GET": {"responses": {404: object_schema()}}}})
    r = diff_contracts(v1, v2)
    k = kinds(r)
    assert ChangeKind.RESPONSE_STATUS_REMOVED in k
    assert ChangeKind.RESPONSE_STATUS_ADDED in k
    assert not r.compatible
    log_case("响应状态码增删", input_contract=[v1, v2], rationale=r.rationale, verdict=r.to_dict())


def test_deterministic_across_repeated_and_reordered_inputs():
    v1 = make_contract({
        "/a": {"GET": {"responses": {200: object_schema(("x", True, {"type": "integer"}))}}},
        "/b": {"GET": {"responses": {200: object_schema(("y", True, {"type": "integer"}))}}},
    })
    v2 = make_contract({
        "/b": {"GET": {"responses": {200: object_schema(("z", True, {"type": "integer"}))}}},
        "/a": {"GET": {"responses": {200: object_schema(("w", True, {"type": "integer"}))}}},
    })
    r1 = diff_contracts(v1, v2)
    r2 = diff_contracts(v1, v2)
    r3 = diff_contracts(v1, v2)
    assert r1.to_dict() == r2.to_dict() == r3.to_dict()
    log_case("重复判定可复现", input_contract=[v1, v2], rationale=r1.rationale,
             verdict=r1.to_dict())
