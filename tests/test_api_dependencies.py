"""细粒度消费者依赖的 HTTP 端到端测试。

验证：依赖登记/校验/门禁结论经 HTTP 入口可用；非法依赖返回可区分的 422 code；
老的操作级登记方式继续可用。
"""

from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

from contract_guard.api import create_app
from tests.contract_factory import response_contract

logger = logging.getLogger("contract_tests.api_deps")


@pytest.fixture()
def client(tmp_path) -> TestClient:
    return TestClient(create_app(storage_root=str(tmp_path / "data")))


def _baseline(client: TestClient, version: str, raw: dict) -> None:
    r = client.post(f"/services/shop/baselines/{version}",
                    content=json.dumps(raw),
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 201, r.text


V1 = response_contract({
    "200": {"id": {"type": "integer"}, "total": {"type": "integer"}},
    "404": {"error": {"type": "string"}},
}, path="/orders", method="get")


def test_dependency_flow_hit_reject(client: TestClient) -> None:
    _baseline(client, "1.0.0", V1)
    new = response_contract({
        "200": {"id": {"type": "string"}, "total": {"type": "integer"}},
        "404": {"error": {"type": "string"}},
    }, path="/orders", method="get")
    _baseline(client, "2.0.0", new)

    r = client.post("/services/shop/consumers/web", json={
        "dependencies": [
            {"operation": "GET /orders", "response": "200", "fields": ["id"]}
        ]
    })
    assert r.status_code == 201, r.text
    assert r.json()["dependencies"][0]["fields"] == ["id"]

    gate = client.get("/services/shop/release-check/1.0.0/2.0.0").json()
    logger.info("最终结论(HTTP): %s",
                [(f["reason_code"], f["change"]["path"], f["affected_consumers"])
                 for f in gate["findings"]])
    assert gate["verdict"] == "reject"
    assert gate["findings"][0]["reason_code"] == "breaking_consumer_dependency_hit"
    assert gate["findings"][0]["affected_consumers"] == ["web"]


def test_dependency_flow_not_hit_warns(client: TestClient) -> None:
    _baseline(client, "1.0.0", V1)
    new = response_contract({
        "200": {"id": {"type": "integer"}},  # 删除 total，web 只依赖 id
        "404": {"error": {"type": "string"}},
    }, path="/orders", method="get")
    _baseline(client, "2.0.0", new)
    client.post("/services/shop/consumers/web", json={
        "dependencies": [
            {"operation": "GET /orders", "response": "200", "fields": ["id"]}
        ]
    })
    gate = client.get("/services/shop/release-check/1.0.0/2.0.0").json()
    assert gate["verdict"] == "warn"
    assert gate["findings"][0]["reason_code"] == \
        "breaking_operation_used_not_depended"


@pytest.mark.parametrize("dep, expected_code", [
    ({"operation": "GET /ghost", "response": "200", "fields": []},
     "consumer_dependency_unknown_operation"),
    ({"operation": "GET /orders", "response": "503", "fields": []},
     "consumer_dependency_unknown_response"),
    ({"operation": "GET /orders", "response": "200", "fields": ["ghost"]},
     "consumer_dependency_unknown_field"),
])
def test_invalid_dependency_returns_distinct_422(
    client: TestClient, dep: dict, expected_code: str,
) -> None:
    _baseline(client, "1.0.0", V1)
    r = client.post("/services/shop/consumers/web", json={"dependencies": [dep]})
    logger.info("非法依赖 HTTP %s body=%s", r.status_code, r.json())
    assert r.status_code == 422
    assert r.json()["error"]["code"] == expected_code


def test_empty_consumer_body_rejected(client: TestClient) -> None:
    _baseline(client, "1.0.0", V1)
    r = client.post("/services/shop/consumers/web", json={})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "incomplete_contract"


def test_legacy_operations_registration_still_supported(client: TestClient) -> None:
    _baseline(client, "1.0.0", V1)
    r = client.post("/services/shop/consumers/web",
                    json={"operations": ["GET /orders"]})
    assert r.status_code == 201
    assert r.json()["operations"] == ["GET /orders"]
    assert r.json()["dependencies"] == []


def test_conflicting_dependency_registration_409(client: TestClient) -> None:
    _baseline(client, "1.0.0", V1)
    body = {"dependencies": [
        {"operation": "GET /orders", "response": "200", "fields": ["id"]}]}
    assert client.post("/services/shop/consumers/web", json=body).status_code == 201
    body2 = {"dependencies": [
        {"operation": "GET /orders", "response": "200", "fields": ["id", "total"]}]}
    r = client.post("/services/shop/consumers/web", json=body2)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "consumer_conflict"
