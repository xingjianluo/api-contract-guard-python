"""同一操作多种响应结果（成功 + 各种失败/边界）的采集与比对测试。

覆盖：
* 新增一种以前没有的响应结果 -> added，不算破坏；
* 删除已声明的响应结果 / 删除响应字段 / 改动响应内容 -> breaking，
  且 reason_code 互不相同、可照着查原因；
* 响应声明顺序变化、同一对版本重复比对 -> 结论逐字节一致。
"""

from __future__ import annotations

import logging

from contract_guard.collector import ContractCollector
from contract_guard.differ import SnapshotDiffer
from contract_guard.models import Severity
from tests.contract_factory import contract_with_responses, json_response

logger = logging.getLogger("contract_tests.responses")

collector = ContractCollector()
differ = SnapshotDiffer()


def _snap(raw: dict, version: str):
    return collector.collect(raw, service="svc", version=version)


def _changes(old_raw: dict, new_raw: dict):
    report = differ.diff(_snap(old_raw, "1.0.0"), _snap(new_raw, "2.0.0"))
    logger.info("输入响应声明 old=%s new=%s",
                sorted(old_raw["paths"]["/users"]["get"]["responses"]),
                sorted(new_raw["paths"]["/users"]["get"]["responses"]))
    logger.info("判定依据 -> 最终结论: %s",
                [(c.path, c.severity.value, c.reason_code) for c in report.changes])
    return report


def test_collects_every_declared_response_sorted() -> None:
    raw = contract_with_responses({
        "404": json_response("404", {"message": {"type": "string"}}),
        "200": json_response("200", {"id": {"type": "integer"}}, required=["id"]),
        "400": json_response("400", {"code": {"type": "integer"}}),
        "default": json_response("default", {"trace": {"type": "string"}}),
    })
    snap = _snap(raw, "1.0.0")
    statuses = [r.status for r in snap.endpoints[0].responses]
    logger.info("输入响应顺序 404/200/400/default -> 归一化顺序 %s", statuses)
    assert statuses == ["200", "400", "404", "default"]


def test_adding_new_response_is_added_not_breaking() -> None:
    old = contract_with_responses({"200": json_response("200", {"id": {"type": "integer"}})})
    new = contract_with_responses({
        "200": json_response("200", {"id": {"type": "integer"}}),
        "404": json_response("404", {"message": {"type": "string"}}),
    })
    report = _changes(old, new)
    added = [c for c in report.changes if c.reason_code == "response_added"]
    assert len(added) == 1
    assert added[0].severity is Severity.ADDED
    assert added[0].path.endswith("#response-404")
    assert report.has_breaking is False


def test_removing_declared_response_is_breaking() -> None:
    old = contract_with_responses({
        "200": json_response("200", {"id": {"type": "integer"}}),
        "409": json_response("409", {"conflict": {"type": "string"}}),
    })
    new = contract_with_responses({"200": json_response("200", {"id": {"type": "integer"}})})
    report = _changes(old, new)
    removed = [c for c in report.changes if c.reason_code == "response_removed"]
    assert len(removed) == 1
    assert removed[0].severity is Severity.BREAKING
    assert removed[0].path.endswith("#response-409")


def test_response_field_removed_vs_response_removed_have_distinct_codes() -> None:
    base = {
        "200": json_response("200", {"id": {"type": "integer"}}),
        "404": json_response("404", {"message": {"type": "string"}}),
    }
    # 情况 A：删掉 404 整个响应（200 保持不变，避免混入其它变更）
    old_a = contract_with_responses(base)
    new_a = contract_with_responses({
        "200": json_response("200", {"id": {"type": "integer"}}),
    })
    codes_a = {c.reason_code for c in _changes(old_a, new_a).changes
               if c.severity is Severity.BREAKING}

    # 情况 B：404 还在，但里面的 message 字段被删
    old_b = contract_with_responses(base)
    new_b = contract_with_responses({
        "200": json_response("200", {"id": {"type": "integer"}}),
        "404": json_response("404", {}),
    })
    codes_b = {c.reason_code for c in _changes(old_b, new_b).changes
               if c.severity is Severity.BREAKING}

    assert codes_a == {"response_removed"}
    # 情况 B：响应还在、字段被删（字段全删空时会附带 response_body_removed）
    assert "response_field_removed" in codes_b
    assert "response_removed" not in codes_b
    assert codes_a.isdisjoint(codes_b)


def test_response_content_type_change_is_breaking() -> None:
    old = contract_with_responses({
        "200": json_response("200", {"id": {"type": "integer"}}),
        "400": json_response("400", {"code": {"type": "integer"}}),
    })
    new = contract_with_responses({
        "200": json_response("200", {"id": {"type": "integer"}}),
        "400": json_response("400", {"code": {"type": "string"}}),
    })
    report = _changes(old, new)
    type_changes = [c for c in report.changes if c.reason_code == "type_changed"]
    assert len(type_changes) == 1
    assert type_changes[0].severity is Severity.BREAKING
    assert type_changes[0].path.endswith("#response-400.code")


def test_response_declaration_order_does_not_change_conclusion() -> None:
    old = contract_with_responses({
        "404": json_response("404", {"message": {"type": "string"}}),
        "200": json_response("200", {"id": {"type": "integer"}}),
        "400": json_response("400", {"code": {"type": "integer"}}),
    })
    # 新契约刻意换一种插入顺序
    new = contract_with_responses({
        "200": json_response("200", {"id": {"type": "integer"}}),
        "400": json_response("400", {"code": {"type": "integer"}}),
        "404": json_response("404", {"message": {"type": "string"}}),
        "500": json_response("500", {"trace": {"type": "string"}}),
    })
    r1 = differ.diff(_snap(old, "1.0.0"), _snap(new, "2.0.0"))
    r2 = differ.diff(_snap(old, "1.0.0"), _snap(new, "2.0.0"))
    tuples1 = [(c.path, c.severity.value, c.reason_code) for c in r1.changes]
    tuples2 = [(c.path, c.severity.value, c.reason_code) for c in r2.changes]
    logger.info("重复判定 + 响应乱序 -> 结论一致: %s", tuples1)
    assert tuples1 == tuples2
    assert r1.changes == r2.changes
    # 只有一条新增 500，无破坏
    assert [c.reason_code for c in r1.changes] == ["response_added"]


def test_error_response_field_removal_is_scoped_to_that_status() -> None:
    """删除 200 的字段不影响 404；两个结果独立参与比对。"""
    old = contract_with_responses({
        "200": json_response("200", {"id": {"type": "integer"}, "name": {"type": "string"}}),
        "404": json_response("404", {"message": {"type": "string"}}),
    })
    new = contract_with_responses({
        "200": json_response("200", {"id": {"type": "integer"}}),
        "404": json_response("404", {"message": {"type": "string"}}),
    })
    report = _changes(old, new)
    breaking = [c for c in report.changes if c.severity is Severity.BREAKING]
    assert len(breaking) == 1
    assert breaking[0].path.endswith("#response-200.name")
