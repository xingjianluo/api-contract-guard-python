"""HTTP 入口：错误映射、不同入口结论一致、底层异常不外泄。"""
from __future__ import annotations

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient

from app.contractguard.http_api import create_app

from .conftest import log_case, make_contract, object_schema

V1 = make_contract({"/u": {"GET": {"responses": {200: object_schema(
    ("id", True, {"type": "integer"}))}}}})
V2 = make_contract({"/u": {"GET": {"responses": {200: object_schema(
    ("id", True, {"type": "string"}))}}}})


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(str(tmp_path / "data")), raise_server_exceptions=True)


def test_register_list_diff_gate_flow(client):
    r = client.post("/baselines/1.0", json=V1)
    assert r.status_code == 200, r.text
    assert client.post("/baselines/2.0", json=V2).status_code == 200
    assert client.get("/baselines").json()["versions"] == ["1.0", "2.0"]

    d = client.get("/diff/1.0/2.0")
    assert d.status_code == 200
    body = d.json()
    assert body["compatible"] is False
    assert body["changes"][0]["kind"] == "type_changed"

    # 无消费者：破坏存在但归入 unused 原因
    g = client.get("/gate/1.0/2.0").json()
    assert g["allowed"] is False
    assert any("unused_breaking_change" in r for r in g["reasons"])

    # HTTP 重复判定结论一致
    assert client.get("/diff/1.0/2.0").json() == body
    log_case("HTTP 登记/比对/门禁", input_contract=[V1, V2],
             rationale=body["rationale"], verdict=g)


def test_conflict_returns_409(client):
    client.post("/baselines/1.0", json=V1)
    r = client.post("/baselines/1.0", json=V2)
    assert r.status_code == 409
    payload = r.json()
    assert payload["error"] == "baseline_conflict"
    assert payload["reason"] == "baseline_already_exists"
    log_case("HTTP 冲突拒绝", input_contract=V2, rationale="409 + 稳定 error code",
             verdict=payload)


def test_malformed_contract_returns_422_with_reason(client):
    r = client.post("/baselines/x", json={"paths": {}})
    assert r.status_code == 422
    payload = r.json()
    assert payload["error"] == "contract_parse_error"
    assert payload["reason"] == "empty_contract"
    log_case("HTTP 畸形契约", input_contract={"paths": {}},
             rationale="422 + 可区分 reason", verdict=payload)


def test_unknown_baseline_returns_404(client):
    r = client.get("/diff/1.0/2.0")
    assert r.status_code == 404
    assert r.json()["error"] == "baseline_not_found"


def test_consumer_endpoints(client):
    client.post("/baselines/1.0", json=V1)
    r = client.post("/consumers/web", json={
        "version": "1.0", "used_endpoints": [["GET", "/u"]]})
    assert r.status_code == 200
    assert client.get("/consumers").json()["consumers"] == ["web"]
    g = client.get("/gate/1.0/2.0").json() if client.post(
        "/baselines/2.0", json=V2).status_code == 200 else None
    g = client.get("/gate/1.0/2.0").json()
    assert g["allowed"] is False
    assert g["consumer_impacts"][0]["consumer"] == "web"
    log_case("HTTP 消费者门禁", expectation={"consumer": "web"},
             rationale="已登记消费者受影响", verdict=g)
