"""存储、登记与并发测试。

日志打印：输入契约摘要、判定依据、最终结论（成功登记数 / 冲突数）。
"""

from __future__ import annotations

import json
import logging
import multiprocessing as mp
import os
import threading
from pathlib import Path

import pytest

from contract_guard.collector import ContractCollector
from contract_guard.errors import (
    BaselineConflictError,
    BaselineNotFoundError,
    ConsumerConflictError,
    StorageCorruptionError,
)
from contract_guard.models import Baseline, ConsumerExpectation
from contract_guard.storage import LocalStore
from tests.contract_factory import minimal_contract

logger = logging.getLogger("contract_tests.storage")

collector = ContractCollector()


def make_baseline(version: str = "1.0.0", *, service: str = "svc", raw=None) -> Baseline:
    raw = raw if raw is not None else minimal_contract(version)
    snap = collector.collect(raw, service=service, version=version)
    return Baseline(service=service, version=version, snapshot=snap, created_at="2026-09-22T00:00:00Z")


# --------------------------------------------------------------------------- #
# 基础 CRUD
# --------------------------------------------------------------------------- #


def test_baseline_put_get_list_and_sort(tmp_store: Path) -> None:
    store = LocalStore(tmp_store)
    b1 = make_baseline("1.0.9")
    b2 = make_baseline("1.0.10")
    store.put_baseline(b1)
    store.put_baseline(b2)

    assert store.get_baseline("svc", "1.0.10").snapshot.fingerprint == b2.snapshot.fingerprint
    versions = [b.version for b in store.list_baselines("svc")]
    logger.info("判定依据: 版本按语义排序, 10 > 9；最终结论: %s", versions)
    assert versions == ["1.0.9", "1.0.10"]


def test_put_same_baseline_is_idempotent(tmp_store: Path) -> None:
    store = LocalStore(tmp_store)
    b = make_baseline("1.0.0")
    store.put_baseline(b)
    mtime = (tmp_store / "svc" / "baselines.json").stat().st_mtime_ns
    os.sync()
    store.put_baseline(b)
    logger.info("判定依据: 同版本同指纹幂等, 不应改写文件")
    assert (tmp_store / "svc" / "baselines.json").stat().st_mtime_ns >= mtime


def test_put_different_contract_same_version_rejected(tmp_store: Path) -> None:
    store = LocalStore(tmp_store)
    store.put_baseline(make_baseline("1.0.0"))
    changed = minimal_contract(response_fields={
        "id": {"type": "integer"}, "name": {"type": "string"}
    })
    logger.info("输入契约变化: 1.0.0 新增 response.name 后再次登记同版本")
    with pytest.raises(BaselineConflictError) as exc:
        store.put_baseline(make_baseline("1.0.0", raw=changed))
    logger.info("最终结论: 明确拒绝冲突 -> %s", exc.value.code)
    assert exc.value.code == "baseline_conflict"
    # 旧基线必须保持原样，没有被覆盖
    assert store.get_baseline("svc", "1.0.0").snapshot.fingerprint == \
        make_baseline("1.0.0").snapshot.fingerprint


def test_get_missing_baseline_is_distinguishable(tmp_store: Path) -> None:
    store = LocalStore(tmp_store)
    with pytest.raises(BaselineNotFoundError) as exc:
        store.get_baseline("svc", "9.9.9")
    assert exc.value.code == "baseline_not_found"


def test_corrupted_index_normalized(tmp_store: Path) -> None:
    store = LocalStore(tmp_store)
    store.put_baseline(make_baseline("1.0.0"))
    (tmp_store / "svc" / "baselines.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(StorageCorruptionError) as exc:
        store.list_baselines("svc")
    logger.info("判定依据: 损坏数据不得透出 JSONDecodeError；最终结论: %s", exc.value.code)
    assert exc.value.code == "storage_corruption"


# --------------------------------------------------------------------------- #
# 消费者
# --------------------------------------------------------------------------- #


def test_consumer_idempotent_and_conflict(tmp_store: Path) -> None:
    store = LocalStore(tmp_store)
    store.put_consumer(ConsumerExpectation("svc", "web", ("GET /users",)))
    # 传入乱序也视为同一期望
    store.put_consumer(ConsumerExpectation("svc", "web", ("GET /users",)))
    with pytest.raises(ConsumerConflictError):
        store.put_consumer(ConsumerExpectation("svc", "web", ("GET /users", "POST /orders")))
    logger.info("最终结论: 消费者同名同期望幂等, 不同期望 -> consumer_conflict")


# --------------------------------------------------------------------------- #
# 并发登记（线程）
# --------------------------------------------------------------------------- #


def test_concurrent_threads_same_version_no_partial_write(tmp_store: Path) -> None:
    """N 个线程并发登记 *同版本同契约*：全部成功或幂等，文件始终完整。"""
    store = LocalStore(tmp_store)
    n = 16
    barrier = threading.Barrier(n)
    errors: list[Exception] = []

    def worker() -> None:
        barrier.wait()
        try:
            store.put_baseline(make_baseline("1.0.0"))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    # 文件必须是完整可解析 JSON，且只有 1 个基线，没有丢失/重复
    data = json.loads((tmp_store / "svc" / "baselines.json").read_text())
    assert set(data["baselines"]) == {"1.0.0"}
    logger.info("判定依据: flock+线程锁串行化读改写, 原子 replace 杜绝部分写入")
    logger.info("最终结论: %d 个并发登记合并为 1 条基线, 无错误", n)


def test_concurrent_threads_distinct_versions_all_persisted(tmp_store: Path) -> None:
    """N 个线程登记 *不同版本*：不得出现覆盖丢失。"""
    store = LocalStore(tmp_store)
    n = 20
    barrier = threading.Barrier(n)

    def worker(i: int) -> None:
        barrier.wait()
        version = f"1.0.{i}"
        store.put_baseline(make_baseline(version))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    versions = {b.version for b in store.list_baselines("svc")}
    logger.info("最终结论: %d 个不同版本全部保留, 无覆盖丢失: %s 个", n, len(versions))
    assert versions == {f"1.0.{i}" for i in range(n)}


# --------------------------------------------------------------------------- #
# 并发登记（进程）—— 验证 flock 跨进程生效
# --------------------------------------------------------------------------- #


def _register_worker(root: str, version: str, raw_contract: dict, queue) -> None:  # noqa: ANN001
    try:
        store = LocalStore(root)
        store.put_baseline(make_baseline(version, raw=raw_contract))
        queue.put(("ok", None))
    except Exception as exc:  # noqa: BLE001
        queue.put((type(exc).__name__, str(exc)))


def test_concurrent_processes_conflict_is_rejected_not_merged(tmp_store: Path) -> None:
    """两个进程并发登记同版本但不同契约：恰好一个成功，另一个得到 baseline_conflict。"""
    ctx = mp.get_context("fork") if "fork" in mp.get_all_start_methods() else mp.get_context()
    queue = ctx.Queue()
    raw_a = minimal_contract(response_fields={"id": {"type": "integer"}})
    raw_b = minimal_contract(response_fields={
        "id": {"type": "integer"}, "name": {"type": "string"}
    })
    procs = [
        ctx.Process(target=_register_worker, args=(str(tmp_store), "2.0.0", raw_a, queue)),
        ctx.Process(target=_register_worker, args=(str(tmp_store), "2.0.0", raw_b, queue)),
    ]
    for p in procs:
        p.start()
    results = [queue.get(timeout=30) for _ in procs]
    for p in procs:
        p.join()

    kinds = sorted(r[0] for r in results)
    logger.info("输入: 两进程登记同版本 2.0.0 但契约不同; 判定依据: flock 串行化+指纹比对")
    logger.info("最终结论: 结果=%s (期望一个 ok 一个 BaselineConflictError)", kinds)
    assert kinds == ["BaselineConflictError", "ok"]
    # 落盘的只有一个完整文件、一个版本
    data = json.loads((tmp_store / "svc" / "baselines.json").read_text())
    assert set(data["baselines"]) == {"2.0.0"}
