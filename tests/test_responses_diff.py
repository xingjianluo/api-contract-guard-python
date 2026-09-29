"""多种响应结果的采集与比对测试。

覆盖：同一操作下每一种响应结果都进入快照并参与比对；
新增响应不破坏、删除/修改已声明响应判破坏且原因码可区分；
重复比对与响应声明顺序重排结论逐字节一致。
"""

from __future__ import annotations

import logging

from contract_guard.collector import ContractCollector
from contract_guard.differ import SnapshotDiffer
from contract_guard.models import Severity
from tests.contract_factory import response_contract

logger = logging.getLogger("contract_tests.responses_diff")

collector = ContractCollector()
differ = SnapshotDiffer()


def _snap(raw: dict, version: str):
    return collector.collect(raw, service="shop", version=version)


def _change_map(report):
    return {(c.path, c.kind): c for c in report.changes}


def test_every_declared_response_enters_snapshot() -> None:
    raw = response_contract({
        "200": {"id": {"type": "integer"}},
        "404": {"error": {"type": "string"}},
        "409": {"conflict": {"type": "string"}},
        "default": None,
    })
    snap = _snap(raw, "1.0.0")
    ep = snap.endpoints[0]
    codes = [r.status_code for r in ep.responses]
    logger.info("输入契约声明响应: 200/404/409/default; 最终结论: 采集结果=%s", codes)
    assert codes == ["200", "404", "409", "default"]
    assert [f.name for f in ep.responses[1].fields] == ["error"]


def test_adding_new_response_is_not_breaking() -> None:
    old = _snap(response_contract({"200": {"id": {"type": "integer"}}}), "1")
    new = _snap(response_contract({
        "200": {"id": {"type": "integer"}},
        "404": {"error": {"type": "string"}},
    }), "2")
    report = differ.diff(old, new)
    added = [c for c in report.changes if c.kind == "response_added"]
    logger.info("判定依据: 新增 404; 最终结论: %s",
                [(c.severity.value, c.reason_code) for c in added])
    assert len(added) == 1
    assert added[0].severity is Severity.ADDED
    assert report.has_breaking is False


def test_removing_declared_response_is_breaking() -> None:
    old = _snap(response_contract({
        "200": {"id": {"type": "integer"}},
        "404": {"error": {"type": "string"}},
    }), "1")
    new = _snap(response_contract({"200": {"id": {"type": "integer"}}}), "2")
    report = differ.diff(old, new)
    removed = [c for c in report.changes if c.kind == "response_removed"]
    logger.info("判定依据: 删除已声明 404; 最终结论: %s",
                [(c.severity.value, c.reason_code, c.path) for c in removed])
    assert len(removed) == 1
    assert removed[0].severity is Severity.BREAKING
    assert removed[0].reason_code == "response_removed"
    assert report.has_breaking


def test_modifying_response_field_is_breaking_with_distinct_reason() -> None:
    old = _snap(response_contract({
        "200": {"id": {"type": "integer"}},
        "404": {"error": {"type": "string"}},
    }), "1")
    new = _snap(response_contract({
        "200": {"id": {"type": "integer"}},
        "404": {"error": {"type": "integer"}},  # 类型改变
    }), "2")
    report = differ.diff(old, new)
    type_changes = [
        c for c in report.changes
        if c.path.endswith("response-404.error") and c.kind == "type_changed"
    ]
    logger.info("判定依据: 404.error 类型 string->integer; 最终结论: %s",
                [(c.severity.value, c.reason_code) for c in type_changes])
    assert len(type_changes) == 1
    assert type_changes[0].severity is Severity.BREAKING
    assert type_changes[0].reason_code == "type_changed"
    # 删除整个响应与改响应内容是两种可区分原因
    assert "response_removed" not in {c.reason_code for c in report.changes}


def test_response_body_removed_is_distinct_from_response_removed() -> None:
    old = _snap(response_contract({"200": {"id": {"type": "integer"}}}), "1")
    # 200 仍然存在，但不再有响应体
    new = _snap(response_contract({"200": None}), "2")
    report = differ.diff(old, new)
    reasons = {c.reason_code for c in report.changes}
    logger.info("判定依据: 200 保留但响应体移除; 最终结论 reasons=%s", reasons)
    assert "response_body_removed" in reasons
    assert "response_removed" not in reasons
    assert report.has_breaking


def test_response_diff_is_order_independent_and_reproducible() -> None:
    # 新旧两侧都刻意打乱响应声明顺序；重复比对多次结论必须逐字节一致
    old_a = response_contract({
        "404": {"error": {"type": "string"}},
        "200": {"id": {"type": "integer"}},
    })
    old_b = response_contract({
        "200": {"id": {"type": "integer"}},
        "404": {"error": {"type": "string"}},
    })
    new_a = response_contract({
        "default": None,
        "404": {"error": {"type": "string"}, "trace": {"type": "string"}},
        "200": {"id": {"type": "integer"}},
    })
    new_b = response_contract({
        "200": {"id": {"type": "integer"}},
        "404": {"error": {"type": "string"}, "trace": {"type": "string"}},
        "default": None,
    })
    r1 = differ.diff(_snap(old_a, "1"), _snap(new_a, "2"))
    r2 = differ.diff(_snap(old_b, "1"), _snap(new_b, "2"))
    r3 = differ.diff(_snap(old_a, "1"), _snap(new_b, "2"))
    tuples = lambda r: [(c.severity.value, c.path, c.kind, c.reason_code) for c in r.changes]
    logger.info("判定依据: 声明顺序重排 + 重复比对; 最终结论条数=%d", len(r1.changes))
    assert tuples(r1) == tuples(r2) == tuples(r3)
    assert r1 == r2 == r3
