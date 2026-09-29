"""v2 持久化：多响应快照、消费者依赖规范化与 v1 旧数据兼容。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from contract_guard.collector import ContractCollector
from contract_guard.models import (
    Baseline,
    ConsumerDependency,
    ConsumerExpectation,
)
from contract_guard.serializers import (
    SCHEMA_VERSION,
    baseline_from_dict,
    baseline_to_dict,
    consumer_from_dict,
    consumer_to_dict,
)
from contract_guard.storage import LocalStore
from tests.contract_factory import response_contract

logger = logging.getLogger("contract_tests.storage_v2")

collector = ContractCollector()


def test_snapshot_round_trips_multiple_responses(tmp_store: Path) -> None:
    raw = response_contract({
        "200": {"id": {"type": "integer"}},
        "404": {"error": {"type": "string"}},
        "default": None,
    }, path="/x", method="get")
    snap = collector.collect(raw, service="svc", version="1.0.0")
    baseline = Baseline(service="svc", version="1.0.0", snapshot=snap,
                        created_at="2026-09-30T00:00:00Z")
    store = LocalStore(tmp_store)
    store.put_baseline(baseline)
    loaded = store.get_baseline("svc", "1.0.0")
    codes = [r.status_code for r in loaded.snapshot.endpoints[0].responses]
    assert codes == ["200", "404", "default"]
    data = json.loads((tmp_store / "svc" / "baselines.json").read_text())
    assert data["schema_version"] == SCHEMA_VERSION
    logger.info("最终结论: 多响应落盘往返一致, schema_version=%s", data["schema_version"])


def test_consumer_dependencies_normalized_and_round_tripped(tmp_store: Path) -> None:
    store = LocalStore(tmp_store)
    expectation = ConsumerExpectation(
        service="svc", consumer="web",
        operations=("GET /z", "GET /a"),
        dependencies=(
            ConsumerDependency("GET /a", "200", ("b", "a")),
            ConsumerDependency("GET /a", "200", ("a", "c")),  # 同(操作,响应)合并
            ConsumerDependency("GET /z", "404", ()),
        ),
    )
    store.put_consumer(expectation)
    saved = store.get_consumer("svc", "web")
    assert saved.operations == ("GET /a", "GET /z")
    assert len(saved.dependencies) == 2
    dep_200 = next(d for d in saved.dependencies if d.response == "200")
    assert dep_200.fields == ("a", "b", "c")
    dep_404 = next(d for d in saved.dependencies if d.response == "404")
    assert dep_404.fields == ()
    logger.info("最终结论: 依赖按(操作,响应)合并字段并稳定排序: %s",
                [(d.operation, d.response, list(d.fields)) for d in saved.dependencies])


def test_serializer_back_compat_v1_consumer_record() -> None:
    legacy = {"service": "svc", "consumer": "old", "operations": ["GET /users"]}
    result = consumer_from_dict(legacy)
    assert result.operations == ("GET /users",)
    assert result.dependencies == ()
    logger.info("最终结论: v1 消费者记录可读, dependencies=%s", result.dependencies)


def test_storage_reads_v1_index_without_corruption(tmp_store: Path) -> None:
    # 手工写一份 v1 baselines.json（旧格式：response_fields），存储应能读
    svc_dir = tmp_store / "svc"
    svc_dir.mkdir(parents=True)
    v1_record = {
        "schema_version": 1,
        "baselines": {
            "1.0.0": {
                "service": "svc",
                "version": "1.0.0",
                "created_at": "2026-01-01T00:00:00Z",
                "snapshot": {
                    "service": "svc",
                    "version": "1.0.0",
                    "fingerprint": "x",
                    "endpoints": [{
                        "method": "GET", "path": "/users", "deprecated": False,
                        "request_params": [], "request_body": [],
                        "response_fields": [{
                            "name": "id", "type": "integer", "required": True,
                            "nullable": False, "has_default": False,
                            "default": None, "enum": [], "item_type": None,
                            "location": None, "direction": "response",
                            "deprecated": False, "children": [],
                        }],
                    }],
                },
            }
        },
    }
    (svc_dir / "baselines.json").write_text(json.dumps(v1_record))
    store = LocalStore(tmp_store)
    baseline = store.get_baseline("svc", "1.0.0")
    responses = baseline.snapshot.endpoints[0].responses
    assert [r.status_code for r in responses] == ["200"]
    assert [f.name for f in responses[0].fields] == ["id"]
    logger.info("最终结论: v1 基线可读, 旧成功响应归一为 %s",
                [r.status_code for r in responses])
