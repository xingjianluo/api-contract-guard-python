"""整个请求体必填性的契约语义测试。

* 无 body -> 必填 body：breaking / request_body_added_required
* 无 body -> 可选 body：compatible / request_body_added_optional
* 可选 -> 必填：      breaking / request_body_became_required
* 必填 -> 可选：      compatible / request_body_became_optional
* “整个请求体必填”与“某个字段必填”必须产生不同 reason_code，不能混成一条。
"""

from __future__ import annotations

import logging

from contract_guard.collector import ContractCollector
from contract_guard.differ import SnapshotDiffer
from contract_guard.models import Severity
from tests.contract_factory import contract_with_responses, json_response, minimal_contract

logger = logging.getLogger("contract_tests.request_body")

collector = ContractCollector()
differ = SnapshotDiffer()

_BODY_PROPS = {"name": {"type": "string"}}
_OK = {"200": json_response("200", {"id": {"type": "integer"}})}


def _snap(raw: dict, version: str):
    return collector.collect(raw, service="svc", version=version)


def _report(old_raw: dict, new_raw: dict):
    report = differ.diff(_snap(old_raw, "1.0.0"), _snap(new_raw, "2.0.0"))
    logger.info("最终结论: %s",
                [(c.path.split("#")[0] + ("#" + c.path.split("#")[1].split(".")[0] if "#" in c.path else ""),
                  c.severity.value, c.reason_code) for c in report.changes])
    return report


def test_no_body_to_required_body_is_breaking() -> None:
    old = contract_with_responses(_OK)
    new = contract_with_responses(_OK, request_body=(_BODY_PROPS, ["name"], True))
    codes = {c.reason_code: c.severity for c in _report(old, new).changes}
    assert codes["request_body_added_required"] is Severity.BREAKING


def test_no_body_to_optional_body_is_compatible() -> None:
    old = contract_with_responses(_OK)
    new = contract_with_responses(_OK, request_body=(_BODY_PROPS, [], False))
    report = _report(old, new)
    codes = {c.reason_code: c.severity for c in report.changes}
    assert codes["request_body_added_optional"] is Severity.COMPATIBLE
    assert report.has_breaking is False


def test_optional_to_required_body_is_breaking() -> None:
    old = contract_with_responses(_OK, request_body=(_BODY_PROPS, [], False))
    new = contract_with_responses(_OK, request_body=(_BODY_PROPS, [], True))
    codes = {c.reason_code: c.severity for c in _report(old, new).changes}
    assert codes["request_body_became_required"] is Severity.BREAKING


def test_required_to_optional_body_is_compatible() -> None:
    old = contract_with_responses(_OK, request_body=(_BODY_PROPS, ["name"], True))
    new = contract_with_responses(_OK, request_body=(_BODY_PROPS, ["name"], False))
    report = _report(old, new)
    codes = {c.reason_code: c.severity for c in report.changes}
    assert codes["request_body_became_optional"] is Severity.COMPATIBLE
    assert report.has_breaking is False


def test_body_required_distinct_from_field_required() -> None:
    """整个 body 变必填与 body 内某字段变必填，原因码必须可区分。"""
    # 整个 body 变必填
    old_body = contract_with_responses(_OK, request_body=(_BODY_PROPS, [], False))
    new_body = contract_with_responses(_OK, request_body=(_BODY_PROPS, [], True))
    body_codes = {c.reason_code for c in _report(old_body, new_body).changes}

    # body 始终可选存在，但内部字段 name 从可选变必填
    old_field = contract_with_responses(_OK, request_body=(_BODY_PROPS, [], False))
    new_field = contract_with_responses(_OK, request_body=(_BODY_PROPS, ["name"], False))
    field_codes = {c.reason_code for c in _report(old_field, new_field).changes}

    logger.info("整体 body 必填=%s; 字段必填=%s", body_codes, field_codes)
    assert body_codes == {"request_body_became_required"}
    assert field_codes == {"request_optional_to_required"}
    assert body_codes.isdisjoint(field_codes)


def test_legacy_optional_body_collected_without_required_flag() -> None:
    """minimal_contract 造的旧形态 body（无 required 标记）归一化为可选。"""
    raw = minimal_contract(
        response_fields={"id": {"type": "integer"}},
        body_fields={"name": {"type": "string"}},
        method="post",
    )
    snap = _snap(raw, "1.0.0")
    ep = snap.endpoints[0]
    assert ep.request_body_required is False
    assert [f.name for f in ep.request_body] == ["name"]
