"""本地存储。

目录布局::

    <root>/<service>/baselines.json
    <root>/<service>/consumers.json
    <root>/<service>/.locks/{baselines,consumers}.lock

并发与一致性保证：

* 写入采用 临时文件 + fsync + os.replace，读方要么看到旧文件要么新文件，
  绝不会读到部分写入；
* 读-改-写在独立的锁文件上以 ``flock(LOCK_EX)`` 串行化（锁不跟随数据文件
  的 replace，避免锁丢失）；
* 同版本重复登记：指纹一致 -> 幂等成功；指纹不一致 -> BaselineConflictError；
  绝不静默覆盖或合并。
* 任何底层 OSError / JSON 损坏都归一化为本包 errors 中的类型。
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .errors import (
    BaselineConflictError,
    BaselineNotFoundError,
    ConsumerConflictError,
    ConsumerNotFoundError,
    IncompleteContractError,
    StorageCorruptionError,
    StorageError,
)
from .models import Baseline, ConsumerDependency, ConsumerExpectation
from .serializers import (
    READABLE_SCHEMA_VERSIONS,
    SCHEMA_VERSION,
    baseline_from_dict,
    baseline_to_dict,
    consumer_from_dict,
    consumer_to_dict,
)

_SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_VERSION_PART_RE = re.compile(r"^\d+$")


def validate_service_name(service: str) -> None:
    if not isinstance(service, str) or not _SERVICE_NAME_RE.match(service):
        raise IncompleteContractError(f"invalid_service_name:{service!r}")


def version_sort_key(version: str) -> tuple:
    """语义版本排序：``1.2.10`` > ``1.2.9``；非数字段退化为按文本比较。"""
    parts = version.split(".")
    key: list[tuple[int, object]] = []
    for part in parts:
        if _VERSION_PART_RE.match(part):
            key.append((1, int(part)))
        else:
            key.append((0, part))
    return tuple(key)


class LocalStore:
    """基于本地文件系统的存储。"""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        # 进程内互斥：flock 锁进程，threading.Lock 锁同进程线程；
        # 每个 (service, 资源) 一把，避免不相关资源互相阻塞。
        self._thread_locks: dict[tuple[str, str], threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #

    def _service_dir(self, service: str) -> Path:
        validate_service_name(service)
        return self.root / service

    @contextlib.contextmanager
    def _locked(self, service: str, name: str) -> Iterator[Path]:
        """对 <service>/.locks/<name>.lock 加排他锁（进程内 + 跨进程双重）。"""
        service_dir = self._service_dir(service)
        with self._locks_guard:
            key = (service, name)
            thread_lock = self._thread_locks.setdefault(key, threading.Lock())
        thread_lock.acquire()
        try:
            try:
                service_dir.mkdir(parents=True, exist_ok=True)
                lock_dir = service_dir / ".locks"
                lock_dir.mkdir(exist_ok=True)
                lock_path = lock_dir / f"{name}.lock"
                fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            except OSError as exc:
                raise StorageError(f"lock_prepare_failed:{name}", cause=exc) from exc
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield service_dir
            except OSError as exc:
                raise StorageError(f"lock_failed:{name}", cause=exc) from exc
            finally:
                with contextlib.suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
        finally:
            thread_lock.release()

    def _read_json(self, path: Path) -> object:
        """读取 JSON；文件缺失返回 None；损坏归一化为 StorageCorruptionError。"""
        try:
            if not path.exists():
                return None
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise StorageError(f"read_failed:{path.name}", cause=exc) from exc
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise StorageCorruptionError(
                f"corrupt_json:{path.name}:{exc.msg}@line{exc.lineno}", cause=exc
            ) from exc

    def _atomic_write_json(self, path: Path, data: object) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp_name, path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_name)
                raise
        except OSError as exc:
            raise StorageError(f"write_failed:{path.name}", cause=exc) from exc

    def _load_index(self, service_dir: Path, filename: str, key: str) -> dict[str, Any]:
        data = self._read_json(service_dir / filename)
        if data is None:
            return {"schema_version": SCHEMA_VERSION, key: {}}
        if not isinstance(data, dict) or data.get("schema_version") not in READABLE_SCHEMA_VERSIONS:
            raise StorageCorruptionError(f"unexpected_index_format:{filename}")
        records = data.get(key)
        if not isinstance(records, dict):
            raise StorageCorruptionError(f"index_section_must_be_object:{filename}:{key}")
        return data

    # ------------------------------------------------------------------ #
    # 基线
    # ------------------------------------------------------------------ #

    def put_baseline(
        self,
        baseline: Baseline,
        *,
        expected_fingerprint: str | None = None,
    ) -> None:
        service = baseline.service
        with self._locked(service, "baselines") as service_dir:
            path = service_dir / "baselines.json"
            data = self._load_index(path.parent, "baselines.json", "baselines")
            records: dict[str, Any] = data["baselines"]
            existing_raw = records.get(baseline.version)

            if existing_raw is not None:
                existing = baseline_from_dict(existing_raw)
                if existing.snapshot.fingerprint != baseline.snapshot.fingerprint:
                    raise BaselineConflictError(
                        f"version_exists_with_different_contract:{baseline.version}:"
                        f"existing={existing.snapshot.fingerprint[:12]}:"
                        f"incoming={baseline.snapshot.fingerprint[:12]}"
                    )
                # 完全一致：幂等，不改写 created_at
                return

            if expected_fingerprint is not None:
                # 调用方预期是在某个旧指纹之上更新，但旧版本并不存在
                raise BaselineConflictError(
                    f"expected_prior_version_missing:{baseline.version}:"
                    f"expected={expected_fingerprint[:12]}"
                )

            records[baseline.version] = baseline_to_dict(baseline)
            self._atomic_write_json(path, data)

    def get_baseline(self, service: str, version: str) -> Baseline:
        service_dir = self._service_dir(service)
        data = self._load_index(service_dir, "baselines.json", "baselines")
        raw = data["baselines"].get(version)
        if raw is None:
            raise BaselineNotFoundError(f"{service}:{version}")
        return baseline_from_dict(raw)

    def has_baseline(self, service: str, version: str) -> bool:
        service_dir = self._service_dir(service)
        data = self._load_index(service_dir, "baselines.json", "baselines")
        return version in data["baselines"]

    def list_baselines(self, service: str) -> list[Baseline]:
        service_dir = self._service_dir(service)
        data = self._load_index(service_dir, "baselines.json", "baselines")
        items = [baseline_from_dict(raw) for raw in data["baselines"].values()]
        items.sort(key=lambda b: version_sort_key(b.version))
        return items

    # ------------------------------------------------------------------ #
    # 消费者
    # ------------------------------------------------------------------ #

    def put_consumer(self, expectation: ConsumerExpectation) -> None:
        service = expectation.service
        with self._locked(service, "consumers") as service_dir:
            path = service_dir / "consumers.json"
            data = self._load_index(path.parent, "consumers.json", "consumers")
            records: dict[str, Any] = data["consumers"]
            normalized = self._normalize_consumer(expectation, service)
            existing_raw = records.get(normalized.consumer)
            if existing_raw is not None:
                existing = consumer_from_dict(existing_raw)
                if existing.operations != normalized.operations or \
                        existing.dependencies != normalized.dependencies:
                    raise ConsumerConflictError(
                        f"consumer_exists_with_different_expectations:"
                        f"{normalized.consumer}:existing="
                        f"ops={list(existing.operations)},"
                        f"deps={[(d.operation, d.response, list(d.fields)) for d in existing.dependencies]}:"
                        f"incoming="
                        f"ops={list(normalized.operations)},"
                        f"deps={[(d.operation, d.response, list(d.fields)) for d in normalized.dependencies]}"
                    )
                return
            records[normalized.consumer] = consumer_to_dict(normalized)
            self._atomic_write_json(path, data)

    @staticmethod
    def _normalize_consumer(
        expectation: ConsumerExpectation, service: str
    ) -> ConsumerExpectation:
        """操作集合排序去重；依赖按 (操作,响应) 合并、字段取并集、整体稳定排序。

        同一 (操作, 响应) 的多条声明字段取并集，声明顺序/消费者数量不影响结论。
        """
        merged: dict[tuple[str, str], set[str]] = {}
        for dep in expectation.dependencies:
            merged.setdefault((dep.operation, dep.response), set()).update(dep.fields)
        dependencies = tuple(
            ConsumerDependency(
                operation=op,
                response=resp,
                fields=tuple(sorted(fields)),
            )
            for (op, resp), fields in sorted(merged.items())
        )
        return ConsumerExpectation(
            service=service,
            consumer=expectation.consumer,
            operations=tuple(sorted(set(expectation.operations))),
            dependencies=dependencies,
        )

    def get_consumer(self, service: str, consumer: str) -> ConsumerExpectation:
        service_dir = self._service_dir(service)
        data = self._load_index(service_dir, "consumers.json", "consumers")
        raw = data["consumers"].get(consumer)
        if raw is None:
            raise ConsumerNotFoundError(f"{service}:{consumer}")
        return consumer_from_dict(raw)

    def list_consumers(self, service: str) -> list[ConsumerExpectation]:
        service_dir = self._service_dir(service)
        data = self._load_index(service_dir, "consumers.json", "consumers")
        items = [consumer_from_dict(raw) for raw in data["consumers"].values()]
        items.sort(key=lambda c: c.consumer)
        return items


__all__ = ["LocalStore", "StorageError", "StorageCorruptionError", "validate_service_name"]
