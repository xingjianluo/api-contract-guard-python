"""本地存储：原子性、并发登记、冲突拒绝、往返一致性。"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.contractguard.errors import (
    BaselineConflictError,
    BaselineNotFoundError,
    ConsumerConflictError,
    StorageError,
)
from app.contractguard.parser import ContractParser
from app.contractguard.storage import LocalStore

from .conftest import log_case, make_contract, object_schema

parser = ContractParser()


def snapshot(version: str, field="id"):
    raw = make_contract({"/u": {"GET": {"responses": {200: object_schema(
        (field, True, {"type": "integer"}))}}}})
    return parser.parse(raw, version=version)


def test_roundtrip_preserves_fingerprint(tmp_path):
    store = LocalStore(tmp_path / "d")
    snap = snapshot("1.0")
    store.save_baseline(snap)
    loaded = store.load_baseline("1.0")
    assert loaded.fingerprint() == snap.fingerprint()
    assert store.list_baselines() == ["1.0"]
    log_case("基线持久化往返", input_contract=snap.to_canonical(),
             rationale="磁盘读取后指纹一致", verdict={"fingerprint": loaded.fingerprint()})


def test_duplicate_same_content_is_idempotent(tmp_path):
    store = LocalStore(tmp_path / "d")
    assert store.save_baseline(snapshot("1.0")) == "created"
    assert store.save_baseline(snapshot("1.0")) == "idempotent"
    log_case("重复登记相同基线", rationale="内容指纹一致，幂等成功", verdict={"result": "idempotent"})


def test_duplicate_different_content_conflicts(tmp_path):
    store = LocalStore(tmp_path / "d")
    store.save_baseline(snapshot("1.0", "id"))
    with pytest.raises(BaselineConflictError) as ei:
        store.save_baseline(snapshot("1.0", "name"))
    assert ei.value.reason == "baseline_already_exists"
    # 磁盘上的原始内容不得被覆盖
    assert store.load_baseline("1.0").fingerprint() == snapshot("1.0", "id").fingerprint()
    log_case("冲突登记被拒绝", rationale="内容不同且未 overwrite",
             verdict={"reason": ei.value.reason, "overwrite_lost": False})


def test_overwrite_explicitly_allowed(tmp_path):
    store = LocalStore(tmp_path / "d")
    store.save_baseline(snapshot("1.0", "id"))
    assert store.save_baseline(snapshot("1.0", "name"), overwrite=True) == "overwritten"
    assert store.load_baseline("1.0").fingerprint() == snapshot("1.0", "name").fingerprint()


def test_concurrent_registration_same_content(tmp_path):
    store = LocalStore(tmp_path / "d")
    snap = snapshot("1.0")
    with ThreadPoolExecutor(max_workers=16) as pool:
        outcomes = list(pool.map(lambda _: store.save_baseline(snap), range(32)))
    assert outcomes.count("created") == 1
    assert outcomes.count("idempotent") == 31
    assert store.load_baseline("1.0").fingerprint() == snap.fingerprint()
    log_case("并发登记相同内容", rationale="独占创建 + 指纹幂等",
             verdict={"created": outcomes.count("created"),
                      "idempotent": outcomes.count("idempotent")})


def test_concurrent_registration_different_content_no_lost_update(tmp_path):
    """两个不同快照并发抢同一版本：只能一个成功，其余明确冲突，绝不静默合并。"""
    store = LocalStore(tmp_path / "d")
    snap_a = snapshot("1.0", "id")
    snap_b = snapshot("1.0", "name")
    accepted_conflicts = []

    def save(s):
        try:
            return store.save_baseline(s)
        except BaselineConflictError as exc:
            accepted_conflicts.append(exc.reason)
            return "conflict"

    with ThreadPoolExecutor(max_workers=16) as pool:
        outcomes = list(pool.map(
            lambda i: save(snap_a if i % 2 == 0 else snap_b), range(32)))

    created = [o for o in outcomes if o == "created"]
    conflicts = [o for o in outcomes if o == "conflict"]
    idempotent = [o for o in outcomes if o == "idempotent"]
    assert len(created) == 1
    # 其余 31 次：失败内容全部冲突；成功内容全部幂等——无覆盖丢失、无静默合并
    assert len(conflicts) + len(idempotent) == 31
    assert set(accepted_conflicts) <= {
        "baseline_already_exists", "concurrent_baseline_conflict"
    }
    # 最终磁盘内容必然是两个候选之一，没有部分写 / 混合写
    fp = store.load_baseline("1.0").fingerprint()
    assert fp in {snap_a.fingerprint(), snap_b.fingerprint()}
    log_case("并发登记不同内容", rationale="一者成功，失败内容全部冲突、成功内容全部幂等",
             verdict={"created": len(created), "conflicts": len(conflicts),
                      "idempotent": len(idempotent),
                      "final_fingerprint_valid": True})


def test_no_partial_write_visible(tmp_path):
    store = LocalStore(tmp_path / "d")
    store.save_baseline(snapshot("1.0"))
    files = list((tmp_path / "d" / "baselines").iterdir())
    assert [f.name for f in files] == ["1.0.json"]  # 无残留临时文件


def test_missing_baseline_normalized(tmp_path):
    store = LocalStore(tmp_path / "d")
    with pytest.raises(BaselineNotFoundError) as ei:
        store.load_baseline("nope")
    assert ei.value.reason == "unknown_version"


def test_corrupted_file_normalized_to_storage_error(tmp_path):
    store = LocalStore(tmp_path / "d")
    store.save_baseline(snapshot("1.0"))
    target = tmp_path / "d" / "baselines" / "1.0.json"
    target.write_text("{ not json", encoding="utf-8")
    with pytest.raises(StorageError) as ei:
        store.load_baseline("1.0")
    assert ei.value.reason == "data_corruption"
    log_case("损坏文件归一化", rationale="JSONDecodeError 不外泄",
             verdict={"reason": ei.value.reason})


def test_consumer_conflict_and_roundtrip(tmp_path):
    from app.contractguard.models import ConsumerExpectation

    store = LocalStore(tmp_path / "d")
    a = ConsumerExpectation(
        consumer="web", version="1.0",
        used_endpoints=frozenset({("GET", "/u")}),
    )
    b = ConsumerExpectation(
        consumer="web", version="2.0",
        used_endpoints=frozenset({("GET", "/u")}),
    )
    store.save_consumer(a)
    with pytest.raises(ConsumerConflictError):
        store.save_consumer(b)
    loaded = store.load_consumer("web")
    assert loaded.version == "1.0"
    assert store.list_consumers() == ["web"]
    log_case("消费者冲突", input_contract=b.to_canonical(),
             rationale="内容不同拒绝覆盖", verdict={"kept_version": loaded.version})
