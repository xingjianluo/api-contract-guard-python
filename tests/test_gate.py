"""发布门禁测试。

* 影响已登记消费者的破坏 -> reject / breaking_consumer_affected
* 未被使用的破坏         -> warn   / breaking_unused
* 无破坏                 -> pass
结论必须可复现。
"""

from __future__ import annotations

import logging
from pathlib import Path

from contract_guard.collector import ContractCollector
from contract_guard.differ import SnapshotDiffer
from contract_guard.gate import ReleaseGate
from contract_guard.models import ConsumerExpectation, GateVerdict
from contract_guard.storage import LocalStore
from tests.contract_factory import minimal_contract

logger = logging.getLogger("contract_tests.gate")


def _run_gate(tmp_store: Path, old_raw: dict, new_raw: dict, consumers: list[tuple[str, tuple[str, ...]]],
              old_v="1.0.0", new_v="2.0.0"):
    store = LocalStore(tmp_store)
    collector = ContractCollector()
    differ = SnapshotDiffer()
    old = collector.collect(old_raw, service="svc", version=old_v)
    new = collector.collect(new_raw, service="svc", version=new_v)
    for name, ops in consumers:
        store.put_consumer(ConsumerExpectation("svc", name, ops))
    diff = differ.diff(old, new)
    report = ReleaseGate(store).evaluate(diff)
    logger.info("输入破坏性变更: %s",
                [(c.path, c.reason_code) for c in diff.changes if c.severity.value == "breaking"])
    logger.info("判定依据: 消费者操作=%s", {n: list(o) for n, o in consumers})
    logger.info("最终结论: verdict=%s findings=%s",
                report.verdict.value,
                [(f.reason_code, f.change.path) for f in report.findings])
    return diff, report


def test_pass_when_no_breaking(tmp_store: Path) -> None:
    # 仅新增响应字段（兼容）
    old = minimal_contract(response_fields={"id": {"type": "integer"}})
    new = minimal_contract(response_fields={
        "id": {"type": "integer"}, "name": {"type": "string"}})
    _, report = _run_gate(tmp_store, old, new, [("web", ("GET /users",))])
    assert report.verdict is GateVerdict.PASS
    assert report.findings == ()


def test_reject_when_consumer_affected(tmp_store: Path) -> None:
    # 删除响应字段=破坏，且 web 消费者在用 GET /users
    old = minimal_contract(response_fields={
        "id": {"type": "integer"}, "name": {"type": "string"}})
    new = minimal_contract(response_fields={"id": {"type": "integer"}})
    diff, report = _run_gate(tmp_store, old, new, [("web", ("GET /users",))])
    assert diff.has_breaking
    assert report.verdict is GateVerdict.REJECT
    assert {f.reason_code for f in report.findings} == {"breaking_consumer_affected"}
    assert all(f.affected for f in report.findings)


def test_warn_when_breaking_but_unused(tmp_store: Path) -> None:
    # 破坏发生在 GET /users，但消费者只用 GET /orders
    old = minimal_contract(response_fields={
        "id": {"type": "integer"}, "name": {"type": "string"}})
    new = minimal_contract(response_fields={"id": {"type": "integer"}})
    diff, report = _run_gate(tmp_store, old, new, [("billing", ("GET /orders",))])
    assert diff.has_breaking
    assert report.verdict is GateVerdict.WARN
    assert {f.reason_code for f in report.findings} == {"breaking_unused"}
    assert all(not f.affected for f in report.findings)


def test_mixed_affected_and_unused_is_reject_with_distinct_reasons(tmp_store: Path) -> None:
    """两个破坏：一个被使用、一个没被使用 -> reject，但原因码各自可区分。"""
    old = minimal_contract(response_fields={
        "id": {"type": "integer"}, "name": {"type": "string"}})
    old["paths"]["/orders"] = {"get": {
        "responses": {"200": {"content": {"application/json": {"schema": {
            "type": "object", "properties": {"order_id": {"type": "integer"}},
            "required": ["order_id"]}}}}}}}
    new = minimal_contract(response_fields={"id": {"type": "integer"}})
    new["paths"]["/orders"] = {"get": {"responses": {"200": {"description": "empty"}}}}

    _, report = _run_gate(tmp_store, old, new, [("web", ("GET /users",))])
    reasons = {f.reason_code for f in report.findings}
    assert report.verdict is GateVerdict.REJECT
    assert reasons == {"breaking_consumer_affected", "breaking_unused"}


def test_gate_evaluation_is_reproducible(tmp_store: Path) -> None:
    old = minimal_contract(response_fields={
        "id": {"type": "integer"}, "name": {"type": "string"}})
    new = minimal_contract(response_fields={"id": {"type": "integer"}})
    _, r1 = _run_gate(tmp_store, old, new, [("web", ("GET /users",))])
    store = LocalStore(tmp_store)
    collector = ContractCollector()
    differ = SnapshotDiffer()
    diff2 = differ.diff(
        collector.collect(old, service="svc", version="1.0.0"),
        collector.collect(new, service="svc", version="2.0.0"),
    )
    r2 = ReleaseGate(store).evaluate(diff2)
    assert [(f.reason_code, f.change.path) for f in r1.findings] == \
           [(f.reason_code, f.change.path) for f in r2.findings]
    assert r1.verdict is r2.verdict
    logger.info("重复门禁结论一致: %s", r1.verdict.value)
