"""畸形 / 不完整契约的拒绝原因必须可区分，且不得静默跳过。"""
from __future__ import annotations

import pytest

from app.contractguard.errors import ContractParseError
from app.contractguard.parser import ContractParser

from .conftest import log_case, make_contract, object_schema

parser = ContractParser()

SIMPLE = make_contract({
    "/users": {"GET": {"responses": {200: object_schema(("id", True, {"type": "integer"}))}}},
})


def test_parse_ok_and_normalizes():
    snap = parser.parse(SIMPLE, version="1.0")
    ep = snap.endpoints[0]
    assert ep.key == ("GET", "/users")
    assert ep.responses[0][0] == 200
    log_case("正常契约归一化", input_contract=SIMPLE,
             rationale="所有字段显式归一 required/nullable/default/enum",
             verdict={"fingerprint": snap.fingerprint()})


def test_field_order_does_not_change_fingerprint():
    a = make_contract({"/u": {"GET": {"responses": {200: object_schema(
        ("a", True, {"type": "string"}), ("b", False, {"type": "integer"}))}}}})
    b = make_contract({"/u": {"GET": {"responses": {200: object_schema(
        ("b", False, {"type": "integer"}), ("a", True, {"type": "string"}))}}}})
    fa = parser.parse(a, version="x").fingerprint()
    fb = parser.parse(b, version="x").fingerprint()
    log_case("字段顺序无关性", input_contract=[a, b], rationale="规范结构按名称排序",
             verdict={"fingerprint_equal": fa == fb})
    assert fa == fb


def test_required_nullable_default_enum_semantics_stable():
    s1 = make_contract({"/u": {"GET": {"responses": {200: object_schema(
        ("role", False, {"type": "string", "enum": ["b", "a"]}))}}}})
    s2 = make_contract({"/u": {"GET": {"responses": {200: object_schema(
        ("role", False, {"type": "string", "enum": ["a", "b"]}))}}}})
    assert parser.parse(s1, version="x").fingerprint() == parser.parse(s2, version="x").fingerprint()
    log_case("枚举顺序归一", input_contract=[s1, s2], rationale="枚举排序后比较",
             verdict={"fingerprint_equal": True})


@pytest.mark.parametrize(
    "raw,reason",
    [
        (None, "not_an_object"),
        ({}, "missing_paths"),
        ({"paths": []}, "paths_not_object"),
        ({"paths": {"users": {}}}, "invalid_path"),
        ({"paths": {"/u": "x"}}, "invalid_path_item"),
        ({"paths": {"/u": {"trace": {}}}}, "unsupported_path_field"),
        ({"paths": {"/u": {"get": "x"}}}, "invalid_operation"),
        (make_contract({"/u": {"GET": {}}}), "missing_responses"),
    ],
)
def test_malformed_top_level(raw, reason):
    with pytest.raises(ContractParseError) as ei:
        parser.parse(raw, version="1.0")
    assert ei.value.reason == reason
    log_case(f"畸形契约拒绝 {reason}", input_contract=raw,
             rationale=f"拒绝原因必须为 {reason}", verdict={"reason": ei.value.reason})


@pytest.mark.parametrize(
    "schema,reason",
    [
        ({}, "missing_type"),
        ({"type": "datetime"}, "unknown_type"),
        ({"type": "array"}, "missing_items"),
        ({"type": "object", "required": ["ghost"], "properties": {}},
         "required_without_property"),
        ({"type": "string", "enum": []}, "invalid_enum"),
        ({"type": "string", "enum": ["a", "a"]}, "duplicate_enum_value"),
        ({"type": "integer", "default": None}, "invalid_default"),
        ({"type": "object", "$ref": "#/x"}, "unsupported_ref"),
    ],
)
def test_malformed_schemas(schema, reason):
    raw = make_contract({"/u": {"GET": {"responses": {200: schema}}}})
    with pytest.raises(ContractParseError) as ei:
        parser.parse(raw, version="1.0")
    assert ei.value.reason == reason
    log_case(f"畸形 schema 拒绝 {reason}", input_contract=raw,
             rationale=f"拒绝原因必须为 {reason}", verdict={"reason": ei.value.reason})


def test_missing_content_and_bad_parameter():
    raw = {"paths": {"/u": {"get": {
        "parameters": [{"in": "header", "name": "X"}],
        "responses": {"200": {"content": {"application/json": {"schema": {"type": "object"}}}}},
    }}}}
    with pytest.raises(ContractParseError) as ei:
        parser.parse(raw, version="1.0")
    assert ei.value.reason == "unsupported_parameter_in"
    log_case("不支持的参数位置", input_contract=raw,
             rationale="仅支持 query 参数", verdict={"reason": ei.value.reason})
