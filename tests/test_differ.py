"""快照比对测试：分级正确、依据明确、结论可复现。"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from contract_guard.collector import ContractCollector
from contract_guard.differ import SnapshotDiffer
from contract_guard.models import DiffReport, Severity
from tests.contract_factory import minimal_contract

logger = logging.getLogger("contract_tests.differ")
collector = ContractCollector()
differ = SnapshotDiffer()


def _snapshot(raw: dict, version: str = "1.0.0", service: str = "svc"):
    return collector.collect(raw, service=service, version=version)


def _codes(report: DiffReport, field_path_part: str | None = None) -> dict[str, str]:
    """返回 {path 包含片段: severity}，便于按字段断言。"""
    out = {}
    for c in report.changes:
        if field_path_part is None or field_path_part in c.path:
            out[c.path] = c.severity.value
    return out


def _reason_codes(report: DiffReport) -> list[str]:
    return sorted(c.reason_code for c in report.changes)


# --------------------------------------------------------------------------- #
# 可复现性
# --------------------------------------------------------------------------- #


def test_repeated_diff_is_identical() -> None:
    old = _snapshot(minimal_contract(), "1.0.0")
    new_raw = minimal_contract(response_fields={
        "id": {"type": "integer"}, "name": {"type": "string"}
    })
    new = _snapshot(new_raw, "1.0.1")
    r1 = differ.diff(old, new)
    r2 = differ.diff(old, new)
    r3 = differ.diff(old, new)
    logger.info("输入: 1.0.0 -> 1.0.1 (响应新增 name)")
    logger.info("判定依据: 纯函数比对 + 固定排序; 最终结论: %s",
                [(c.path, c.severity.value, c.reason_code) for c in r1.changes])
    assert [(c.path, c.severity.value, c.reason_code) for c in r1.changes] == \
           [(c.path, c.severity.value, c.reason_code) for c in r3.changes]
    assert r1.changes == r3.changes


def test_no_changes_for_equivalent_contracts() -> None:
    raw = minimal_contract()
    r = differ.diff(_snapshot(raw, "1.0.0"), _snapshot(raw, "1.0.1"))
    assert r.changes == ()
    assert r.has_breaking is False


# --------------------------------------------------------------------------- #
# 响应侧分级
# --------------------------------------------------------------------------- #


def test_response_field_added_is_compatible_and_removed_is_breaking() -> None:
    old = _snapshot(minimal_contract(response_fields={
        "id": {"type": "integer"}, "name": {"type": "string"}
    }))
    new = _snapshot(minimal_contract(response_fields={"id": {"type": "integer"}}), "1.0.1")
    r = differ.diff(old, new)
    codes = _codes(r)
    path = [p for p in codes if p.endswith("response.name")][0]
    logger.info("响应删除 name -> %s: %s", path, codes[path])
    assert codes[path] == "breaking"

    # 反向：新增响应字段兼容
    r_rev = differ.diff(_snapshot(minimal_contract(response_fields={"id": {"type": "integer"}}), "1.0.0"),
                        _snapshot(minimal_contract(response_fields={
                            "id": {"type": "integer"}, "name": {"type": "string"}}), "1.0.1"))
    rev_path = [p for p in _codes(r_rev) if p.endswith("response.name")][0]
    assert _codes(r_rev)[rev_path] == "compatible"


@pytest.mark.parametrize("old_schema,new_schema,expected", [
    # 响应枚举新增成员=破坏；移除=兼容
    ({"type": "string", "enum": ["a", "b"]}, {"type": "string", "enum": ["a"]}, "compatible"),
    ({"type": "string", "enum": ["a"]}, {"type": "string", "enum": ["a", "b"]}, "breaking"),
    # 响应不可空变可空=破坏
    ({"type": "string"}, {"type": "string", "nullable": True}, "breaking"),
    # 可空变不可空=兼容
    ({"type": "string", "nullable": True}, {"type": "string"}, "compatible"),
])
def test_response_enum_and_nullability(old_schema, new_schema, expected) -> None:
    old = _snapshot(minimal_contract(response_fields={"id": {"type": "integer"}, "x": old_schema}))
    new = _snapshot(minimal_contract(response_fields={"id": {"type": "integer"}, "x": new_schema}), "1.0.1")
    r = differ.diff(old, new)
    severity = [c.severity.value for c in r.changes if ".x" in c.path]
    logger.info("响应字段 x 变化 -> %s, reasons=%s", severity,
                [c.reason_code for c in r.changes if ".x" in c.path])
    assert expected in severity


# --------------------------------------------------------------------------- #
# 请求侧分级
# --------------------------------------------------------------------------- #


def test_request_required_added_is_breaking_optional_compatible() -> None:
    base = minimal_contract(response_fields={"id": {"type": "integer"}})
    old = _snapshot(base)
    # 新增必填 query 参数 -> 破坏
    breaking_raw = minimal_contract(response_fields={"id": {"type": "integer"}}, params=[
        {"name": "token", "in": "query", "required": True, "schema": {"type": "string"}}
    ])
    r = differ.diff(old, _snapshot(breaking_raw, "1.0.1"))
    sev = [c.severity for c in r.changes if c.path.endswith("request-param.token")]
    assert sev == [Severity.BREAKING]

    # 新增可选 query 参数 -> 兼容
    compat_raw = minimal_contract(response_fields={"id": {"type": "integer"}}, params=[
        {"name": "token", "in": "query", "schema": {"type": "string"}}
    ])
    r2 = differ.diff(old, _snapshot(compat_raw, "1.0.1"))
    sev2 = [c.severity for c in r2.changes if c.path.endswith("request-param.token")]
    logger.info("新增请求参数: 必填=%s 可选=%s", sev, sev2)
    assert sev2 == [Severity.COMPATIBLE]


@pytest.mark.parametrize("old_schema,new_schema,expected", [
    # 请求枚举移除成员=破坏；新增成员=兼容
    ({"type": "string", "enum": ["a", "b"]}, {"type": "string", "enum": ["a"]}, "breaking"),
    ({"type": "string", "enum": ["a"]}, {"type": "string", "enum": ["a", "b"]}, "compatible"),
    # 请求可空变不可空=破坏；反之兼容
    ({"type": "string", "nullable": True}, {"type": "string"}, "breaking"),
    ({"type": "string"}, {"type": "string", "nullable": True}, "compatible"),
    # 请求移除默认值=破坏
    ({"type": "string", "default": "x"}, {"type": "string"}, "breaking"),
    ({"type": "string"}, {"type": "string", "default": "x"}, "compatible"),
])
def test_request_enum_nullable_default_rules(old_schema, new_schema, expected) -> None:
    def raw_with_param(schema):
        return minimal_contract(
            response_fields={"id": {"type": "integer"}},
            params=[{"name": "x", "in": "query", "schema": schema}],
        )
    r = differ.diff(_snapshot(raw_with_param(old_schema)),
                    _snapshot(raw_with_param(new_schema), "1.0.1"))
    sev = [c.severity.value for c in r.changes if ".x" in c.path]
    reasons = [c.reason_code for c in r.changes if ".x" in c.path]
    logger.info("判定依据 reasons=%s -> 最终结论 %s", reasons, sev)
    assert expected in sev


def test_type_change_is_always_breaking() -> None:
    old = _snapshot(minimal_contract(response_fields={
        "id": {"type": "integer"}}))
    new = _snapshot(minimal_contract(response_fields={
        "id": {"type": "string"}}), "1.0.1")
    r = differ.diff(old, new)
    assert "type_changed" in _reason_codes(r)
    assert all(c.severity is Severity.BREAKING for c in r.changes)


def test_endpoint_added_is_separate_severity() -> None:
    old = _snapshot(minimal_contract(path="/a"))
    new_raw = minimal_contract(path="/a")
    new_raw["paths"]["/b"] = {"get": {"responses": {"200": {"description": "ok"}}}}
    r = differ.diff(old, _snapshot(new_raw, "1.0.1"))
    kinds = {c.kind: c.severity for c in r.changes}
    logger.info("新增端点分级: %s", kinds)
    assert kinds["endpoint_added"] is Severity.ADDED


def test_endpoint_removed_is_breaking() -> None:
    old = _snapshot(minimal_contract(path="/a"))
    new_raw = {"paths": {}}
    r = differ.diff(old, _snapshot(new_raw, "1.0.1"))
    assert r.has_breaking
    assert "endpoint_removed" in _reason_codes(r)


def test_changes_sorted_deterministically() -> None:
    old = _snapshot(minimal_contract(response_fields={
        "id": {"type": "integer"}, "name": {"type": "string"}}))
    new = _snapshot(minimal_contract(response_fields={
        "id": {"type": "string"}}), "1.0.1")
    r1 = differ.diff(old, new)
    paths = [(c.severity.value, c.path) for c in r1.changes]
    assert paths == sorted(paths, key=lambda x: ({"breaking": 0, "compatible": 1, "added": 2}[x[0]], x[1]))
