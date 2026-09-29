"""端到端可复现性：同一对版本反复比对/门禁，HTTP 与库入口结论逐字节一致。"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi.testclient import TestClient

from contract_guard.api import create_app
from contract_guard.service import ContractGuardService
from tests.contract_factory import response_contract

logger = logging.getLogger("contract_tests.repro_e2e")


def test_repeated_release_check_identical_across_entries(tmp_path: Path) -> None:
    root = tmp_path / "data"
    client = TestClient(create_app(storage_root=str(root)))

    v1 = response_contract({
        "200": {"id": {"type": "integer"}, "total": {"type": "integer"}},
        "404": {"error": {"type": "string"}},
    }, path="/orders", method="get")
    v2 = response_contract({
        "200": {"id": {"type": "string"}},  # 删 total + id 改类型
    }, path="/orders", method="get")
    for version, raw in (("1.0.0", v1), ("2.0.0", v2)):
        r = client.post(f"/services/shop/baselines/{version}", json=raw)
        assert r.status_code == 201
    # 一个操作级老消费者 + 一个字段级消费者
    client.post("/services/shop/consumers/legacy", json={"operations": ["GET /orders"]})
    client.post("/services/shop/consumers/web", json={"dependencies": [
        {"operation": "GET /orders", "response": "200", "fields": ["id"]}]})

    def http_gate():
        body = client.get(
            "/services/shop/release-check/1.0.0/2.0.0").json()
        return (
            body["verdict"],
            sorted((f["reason_code"], f["change"]["path"],
                    tuple(f["affected_consumers"])) for f in body["findings"]),
        )

    g1, g2, g3 = http_gate(), http_gate(), http_gate()
    assert g1 == g2 == g3

    svc = ContractGuardService(str(root))
    direct = svc.check_release("shop", "1.0.0", "2.0.0")
    direct_sig = (
        direct.verdict.value,
        sorted((f.reason_code, f.change.path, f.affected_consumers)
               for f in direct.findings),
    )
    logger.info("判定依据: HTTP x3 + 库调用; 最终结论 verdict=%s findings=%d",
                g1[0], len(g1[1]))
    assert (g1[0], [(rc, p, tuple(ac)) for rc, p, ac in g1[1]]) == direct_sig
