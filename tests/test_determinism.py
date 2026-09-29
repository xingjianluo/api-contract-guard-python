"""确定性专项：消费者数量、声明条数、登记顺序都不得影响结论与排序。"""

from __future__ import annotations

import logging
from pathlib import Path

from contract_guard.service import ContractGuardService
from tests.contract_factory import contract_with_responses, json_response

logger = logging.getLogger("contract_tests.determinism")


def _old_contract() -> dict:
    return contract_with_responses({
        "200": json_response("200", {
            "id": {"type": "integer"},
            "name": {"type": "string"},
        }, required=["id", "name"]),
        "404": json_response("404", {"message": {"type": "string"}}),
    })


def _new_contract() -> dict:
    return contract_with_responses({
        "200": json_response("200", {"id": {"type": "integer"}}, required=["id"]),
        "404": json_response("404", {"message": {"type": "string"}}),
    })


def _build(root: Path, registrations):
    svc = ContractGuardService(root)
    svc.register_baseline(_old_contract(), service="svc", version="1.0.0")
    for name, deps in registrations:
        svc.register_consumer("svc", name, [], dependencies=deps)
    svc.register_baseline(_new_contract(), service="svc", version="2.0.0")
    return svc.check_release("svc", "1.0.0", "2.0.0")


def _digest(report) -> list:
    return sorted(
        (f.reason_code, f.change.path, f.change.kind, tuple(sorted(f.consumers)))
        for f in report.findings
    )


def test_registration_order_does_not_change_findings(
        tmp_store: Path, tmp_path: Path) -> None:
    deps = [
        ("web", [{"operation": "GET /users", "response": "200", "fields": ["id"]}]),
        ("mobile", [{"operation": "GET /users", "response": "200",
                     "fields": ["id", "name"]}]),
        ("api", [{"operation": "GET /users", "response": "404", "fields": ["message"]}]),
    ]
    store_a = tmp_store
    store_b = tmp_path / "guard-b"
    store_b.mkdir()

    report_a = _build(store_a, deps)
    # 故意打乱登记顺序，消费者集合完全相同
    report_b = _build(store_b, list(reversed(deps)))

    logger.info("顺序A findings=%s", _digest(report_a))
    logger.info("顺序B findings=%s", _digest(report_b))
    assert _digest(report_a) == _digest(report_b)
    assert report_a.verdict is report_b.verdict


def test_extra_consumers_do_not_change_existing_findings(
        tmp_store: Path, tmp_path: Path) -> None:
    base_deps = [
        ("web", [{"operation": "GET /users", "response": "200", "fields": ["id"]}]),
    ]
    store_b = tmp_path / "guard-b"
    store_b.mkdir()
    report_one = _build(tmp_store, base_deps)
    svc = ContractGuardService(store_b)
    svc.register_baseline(_old_contract(), service="svc", version="1.0.0")
    # 加入更多消费者：一个老格式（不相关操作）、一个新格式（更多声明条数）
    svc.register_consumer("svc", "web", [], dependencies=base_deps[0][1])
    svc.register_consumer("svc", "billing", ["GET /orders"])
    svc.register_consumer("svc", "analytics", [], dependencies=[
        {"operation": "GET /users", "response": "404", "fields": []},
        {"operation": "GET /users", "response": "200", "fields": ["id"]},
    ])
    svc.register_baseline(_new_contract(), service="svc", version="2.0.0")
    report_many = svc.check_release("svc", "1.0.0", "2.0.0")

    digest_one = _digest(report_one)
    digest_many = _digest(report_many)
    logger.info("单消费者=%s", digest_one)
    logger.info("多消费者=%s", digest_many)
    # web 对应的 name 删除 finding：两种规模下原因码与目标变更一致
    name_one = [x for x in digest_one if x[1].endswith("response-200.name")]
    name_many = [x for x in digest_many if x[1].endswith("response-200.name")]
    assert name_one[0][0] == "breaking_operation_only"
    assert name_many[0][0] == "breaking_operation_only"
    assert report_one.verdict is report_many.verdict


def test_same_pair_evaluated_repeatedly_is_identical(tmp_store: Path) -> None:
    report = _build(tmp_store, [
        ("web", [{"operation": "GET /users", "response": "200",
                  "fields": ["id", "name"]}]),
    ])
    svc = ContractGuardService(tmp_store)
    again = svc.check_release("svc", "1.0.0", "2.0.0")
    third = svc.check_release("svc", "1.0.0", "2.0.0")
    assert _digest(report) == _digest(again) == _digest(third)
    logger.info("同一对版本重复 3 次门禁结论一致: verdict=%s", report.verdict.value)
