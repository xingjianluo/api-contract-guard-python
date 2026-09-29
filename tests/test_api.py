"""HTTP 入口端到端测试。

验证：
* 登记 -> 比对 -> 门禁整条链路；
* HTTP 入口结论与直接调用 Service 完全一致；
* 各类失败返回稳定 error.code，不出现底层异常（JSONDecodeError/KeyError 等）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from contract_guard.api import create_app
from contract_guard.service import ContractGuardService
from tests.contract_factory import minimal_contract

logger = logging.getLogger("contract_tests.api")

V1 = minimal_contract(response_fields={
    "id": {"type": "integer"}, "name": {"type": "string"}})
V2 = minimal_contract(response_fields={"id": {"type": "integer"}})  # 删除 name=破坏


@pytest.fixture()
def client(tmp_store: Path) -> TestClient:
    return TestClient(create_app(storage_root=str(tmp_store)))


def _post_baseline(client: TestClient, version: str, raw: dict, *, expected=None):
    url = f"/services/svc/baselines/{version}"
    if expected:
        url += f"?expected_fingerprint={expected}"
    return client.post(url, content=json.dumps(raw),
                       headers={"Content-Type": "application/json"})


def test_full_flow_register_diff_gate(client: TestClient, tmp_store: Path) -> None:
    r1 = _post_baseline(client, "1.0.0", V1)
    r2 = _post_baseline(client, "2.0.0", V2)
    assert r1.status_code == 201 and r2.status_code == 201

    # 重复登记同版本同契约 -> 幂等 201
    r_again = _post_baseline(client, "1.0.0", V1)
    assert r_again.status_code == 201

    rc = client.post("/services/svc/consumers/web",
                     json={"operations": ["GET /users"]})
    assert rc.status_code == 201

    diff = client.get("/services/svc/diff/1.0.0/2.0.0").json()
    gate = client.get("/services/svc/release-check/1.0.0/2.0.0").json()

    logger.info("输入契约: 1.0.0 含 response.name, 2.0.0 删除该字段")
    logger.info("判定依据(HTTP): %s", diff["changes"])
    logger.info("最终结论(HTTP gate): verdict=%s, findings=%s",
                gate["verdict"], [(f["reason_code"], f["change"]["path"]) for f in gate["findings"]])

    assert diff["has_breaking"] is True
    assert gate["verdict"] == "reject"
    assert gate["findings"][0]["reason_code"] == "breaking_consumer_affected"


def test_http_matches_direct_service_call(client: TestClient, tmp_store: Path) -> None:
    _post_baseline(client, "1.0.0", V1)
    _post_baseline(client, "2.0.0", V2)
    client.post("/services/svc/consumers/web", json={"operations": ["GET /users"]})

    http_diff = client.get("/services/svc/diff/1.0.0/2.0.0").json()
    http_gate = client.get("/services/svc/release-check/1.0.0/2.0.0").json()

    svc = ContractGuardService(str(tmp_store))
    direct_diff = svc.diff_versions("svc", "1.0.0", "2.0.0")
    direct_gate = svc.check_release("svc", "1.0.0", "2.0.0")

    assert [(c["path"], c["severity"], c["reason_code"]) for c in http_diff["changes"]] == \
           [(c.path, c.severity.value, c.reason_code) for c in direct_diff.changes]
    assert http_gate["verdict"] == direct_gate.verdict.value
    assert [f["reason_code"] for f in http_gate["findings"]] == \
           [f.reason_code for f in direct_gate.findings]
    logger.info("HTTP 入口与直接调用结论完全一致: %s", http_gate["verdict"])


def test_warn_when_breaking_unused(client: TestClient) -> None:
    _post_baseline(client, "1.0.0", V1)
    _post_baseline(client, "2.0.0", V2)
    client.post("/services/svc/consumers/other", json={"operations": ["GET /orders"]})
    gate = client.get("/services/svc/release-check/1.0.0/2.0.0").json()
    assert gate["verdict"] == "warn"
    assert gate["findings"][0]["reason_code"] == "breaking_unused"


def test_conflict_returns_409_with_stable_code(client: TestClient) -> None:
    assert _post_baseline(client, "1.0.0", V1).status_code == 201
    resp = _post_baseline(client, "1.0.0", V2)
    logger.info("同版本冲突登记 -> HTTP %s body=%s", resp.status_code, resp.json())
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "baseline_conflict"


@pytest.mark.parametrize("payload, expected_code, http_status", [
    ("{not json", "contract_parse_error", 400),
    ({"paths": "x"}, "contract_parse_error", 400),
    ({"openapi": "3.0.0"}, "incomplete_contract", 422),
    ({"paths": {"/x": {"get": {
        "responses": {"200": {"content": {"application/json": {"schema": {
            "type": "object", "properties": {"f": {"type": "uuid"}}}}}}}}}}},
     "unsupported_contract", 422),
])
def test_malformed_contracts_get_distinguishable_errors(
        client: TestClient, payload, expected_code, http_status) -> None:
    resp = _post_baseline(client, "9.9.9", payload)
    logger.info("畸形契约 -> HTTP %s, error=%s", resp.status_code, resp.json().get("error"))
    assert resp.status_code == http_status
    assert resp.json()["error"]["code"] == expected_code


def test_missing_version_returns_404_code(client: TestClient) -> None:
    _post_baseline(client, "1.0.0", V1)
    resp = client.get("/services/svc/diff/1.0.0/9.9.9")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "baseline_not_found"


def test_baselines_listed_sorted(client: TestClient) -> None:
    _post_baseline(client, "1.0.10", V1)
    _post_baseline(client, "1.0.2", V1)
    resp = client.get("/services/svc/baselines")
    versions = [b["version"] for b in resp.json()["baselines"]]
    logger.info("基线列表排序: %s", versions)
    assert versions == ["1.0.2", "1.0.10"]


# --------------------------------------------------------------------------- #
# 多响应结果 + 字段级依赖（新能力 HTTP 端到端）
# --------------------------------------------------------------------------- #


def _multi_response_contract(*, keep_name: bool) -> dict:
    from tests.contract_factory import contract_with_responses, json_response
    props = {"id": {"type": "integer"}}
    if keep_name:
        props["name"] = {"type": "string"}
    return contract_with_responses({
        "200": json_response("200", props, required=["id"] + (["name"] if keep_name else [])),
        "404": json_response("404", {"message": {"type": "string"}}),
    })


def test_field_level_dependency_unused_breaking_warns(client: TestClient) -> None:
    v1 = _multi_response_contract(keep_name=True)
    v2 = _multi_response_contract(keep_name=False)
    _post_baseline(client, "1.0.0", v1)
    rc = client.post("/services/svc/consumers/web", json={
        "operations": [],
        "dependencies": [
            {"operation": "GET /users", "response": "200", "fields": ["id"]}
        ],
    })
    assert rc.status_code == 201, rc.text
    _post_baseline(client, "2.0.0", v2)

    gate = client.get("/services/svc/release-check/1.0.0/2.0.0").json()
    logger.info("字段级依赖(只依赖id, name被删) -> %s", gate)
    assert gate["verdict"] == "warn"
    finding = gate["findings"][0]
    assert finding["reason_code"] == "breaking_operation_only"
    assert finding["consumers"] == ["web"]


def test_field_level_dependency_hit_rejects(client: TestClient) -> None:
    v1 = _multi_response_contract(keep_name=True)
    v2 = _multi_response_contract(keep_name=False)
    _post_baseline(client, "1.0.0", v1)
    rc = client.post("/services/svc/consumers/web", json={
        "operations": ["GET /users"],
        "dependencies": [
            {"operation": "GET /users", "response": "200", "fields": ["name"]}
        ],
    })
    assert rc.status_code == 201
    _post_baseline(client, "2.0.0", v2)

    gate = client.get("/services/svc/release-check/1.0.0/2.0.0").json()
    assert gate["verdict"] == "reject"
    assert gate["findings"][0]["reason_code"] == "breaking_consumer_affected"
    assert gate["findings"][0]["consumers"] == ["web"]


@pytest.mark.parametrize("dependency, expected_prefix", [
    ({"operation": "GET /ghost", "response": "200", "fields": ["id"]},
     "unknown_consumer_operation"),
    ({"operation": "GET /users", "response": "503", "fields": ["id"]},
     "unknown_consumer_response"),
    ({"operation": "GET /users", "response": "200", "fields": ["ghost"]},
     "unknown_consumer_field"),
])
def test_invalid_dependency_rejected_with_distinguishable_code(
        client: TestClient, dependency: dict, expected_prefix: str) -> None:
    _post_baseline(client, "1.0.0", _multi_response_contract(keep_name=True))
    resp = client.post("/services/svc/consumers/web",
                       json={"operations": [], "dependencies": [dependency]})
    logger.info("非法依赖 %s -> HTTP %s %s", dependency, resp.status_code, resp.json())
    assert resp.status_code == 422
    assert resp.json()["error"]["message"].startswith(expected_prefix)


def test_response_removal_shown_in_diff_and_scoped_gate(client: TestClient) -> None:
    v1 = _multi_response_contract(keep_name=True)
    v2 = _multi_response_contract(keep_name=True)
    # v2 删掉 404
    v2["paths"]["/users"]["get"]["responses"].pop("404")
    _post_baseline(client, "1.0.0", v1)
    client.post("/services/svc/consumers/ok-only", json={
        "operations": [],
        "dependencies": [{"operation": "GET /users", "response": "200", "fields": ["id"]}],
    })
    _post_baseline(client, "2.0.0", v2)

    diff = client.get("/services/svc/diff/1.0.0/2.0.0").json()
    assert any(c["reason_code"] == "response_removed" for c in diff["changes"])
    gate = client.get("/services/svc/release-check/1.0.0/2.0.0").json()
    finding = next(f for f in gate["findings"]
                   if f["change"]["reason_code"] == "response_removed")
    # 消费者只依赖 200，不依赖 404 -> 仅操作命中，warn
    assert finding["reason_code"] == "breaking_operation_only"
    assert gate["verdict"] == "warn"
