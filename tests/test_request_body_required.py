"""请求体整体必填性的比对测试。

要求：可以不带请求体 -> 必须带 = 破坏（request_body_required_added）；
必须带 -> 可以不带 = 兼容（request_body_required_relaxed）；
“整个请求体必填”与“某个字段必填”（request_optional_to_required）
是两种可区分的原因码。
"""

from __future__ import annotations

import logging

from contract_guard.collector import ContractCollector
from contract_guard.differ import SnapshotDiffer
from contract_guard.models import Severity
from tests.contract_factory import response_contract

logger = logging.getLogger("contract_tests.request_body_required")

collector = ContractCollector()
differ = SnapshotDiffer()


def _snap(raw: dict, version: str):
    return collector.collect(raw, service="shop", version=version)


def _reasons(report):
    return {c.reason_code for c in report.changes}


def test_collector_marks_request_body_required() -> None:
    raw = response_contract(
        {"201": None},
        body={"name": {"type": "string"}}, body_required=True,
        path="/users", method="post",
    )
    snap = _snap(raw, "1")
    ep = snap.endpoints[0]
    assert ep.request_body_required is True
    logger.info("最终结论: request_body_required=%s", ep.request_body_required)


def test_optional_to_required_body_is_breaking() -> None:
    body_schema = {"name": {"type": "string"}}
    old = _snap(response_contract({"201": None}, body=body_schema, body_required=False,
                                  path="/users", method="post"), "1")
    new = _snap(response_contract({"201": None}, body=body_schema, body_required=True,
                                  path="/users", method="post"), "2")
    report = differ.diff(old, new)
    logger.info("判定依据: 请求体可选->必填; 最终结论 reasons=%s", _reasons(report))
    assert "request_body_required_added" in _reasons(report)
    assert report.has_breaking
    # 不应与字段级必填原因混淆
    assert "request_optional_to_required" not in _reasons(report)


def test_missing_to_required_body_is_breaking() -> None:
    # 原本完全没有请求体（可以不带），新版本新增必填请求体
    old = _snap(response_contract({"201": None}, path="/users", method="post"), "1")
    new = _snap(response_contract(
        {"201": None}, body={"name": {"type": "string"}}, body_required=True,
        path="/users", method="post"), "2")
    report = differ.diff(old, new)
    body_reasons = [
        c.reason_code for c in report.changes
        if c.reason_code == "request_body_required_added"
    ]
    logger.info("判定依据: 无请求体->必填请求体; 最终结论=%s", body_reasons)
    assert body_reasons == ["request_body_required_added"]
    assert report.has_breaking


def test_required_to_optional_body_is_compatible() -> None:
    body_schema = {"name": {"type": "string"}}
    old = _snap(response_contract({"201": None}, body=body_schema, body_required=True,
                                  path="/users", method="post"), "1")
    new = _snap(response_contract({"201": None}, body=body_schema, body_required=False,
                                  path="/users", method="post"), "2")
    report = differ.diff(old, new)
    logger.info("判定依据: 请求体必填->可选; 最终结论 reasons=%s", _reasons(report))
    assert "request_body_required_relaxed" in _reasons(report)
    assert report.has_breaking is False
    assert all(c.severity is not Severity.BREAKING for c in report.changes)


def test_field_level_required_is_distinct_from_body_required() -> None:
    # 请求体始终可选；只是体内某字段从可选变必填 -> 字段级原因码
    old = _snap(response_contract(
        {"201": None}, body={"name": {"type": "string"}}, body_required=False,
        path="/users", method="post"), "1")
    new_raw = response_contract(
        {"201": None}, body={"name": {"type": "string"}}, body_required=False,
        path="/users", method="post")
    schema = new_raw["paths"]["/users"]["post"]["requestBody"]["content"][
        "application/json"]["schema"]
    schema["required"] = ["name"]
    new = _snap(new_raw, "2")
    report = differ.diff(old, new)
    reasons = _reasons(report)
    logger.info("判定依据: 仅体内字段 name 可选->必填; 最终结论 reasons=%s", reasons)
    assert "request_optional_to_required" in reasons
    assert "request_body_required_added" not in reasons


def test_body_required_diff_reproducible() -> None:
    body_schema = {"name": {"type": "string"}}
    old = _snap(response_contract({"201": None}, body=body_schema, body_required=False,
                                  path="/users", method="post"), "1")
    new = _snap(response_contract({"201": None}, body=body_schema, body_required=True,
                                  path="/users", method="post"), "2")
    r1 = differ.diff(old, new)
    r2 = differ.diff(old, new)
    assert [(c.path, c.reason_code) for c in r1.changes] == \
           [(c.path, c.reason_code) for c in r2.changes]
    logger.info("重复判定结论一致: %s", r1.has_breaking)
