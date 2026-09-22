"""本地存储：原子写、并发冲突拒绝、异常归一化。

并发安全策略：
- 进程内用 RLock 串行化“读-判定-写”序列；
- 创建新文件使用 O_CREAT|O_EXCL 独占创建，跨进程并发时只有一方成功，
  其余方读到既有内容后按指纹判定为“幂等成功”或“明确冲突”；
- 写入采用“同目录临时文件 + fsync + os.replace”，绝不出现部分写入；
- 内容不同且未显式 overwrite 时抛 BaselineConflictError / ConsumerConflictError，
  绝不静默合并；
- 所有 IO/JSON 异常一律包装为 StorageError，不直接透出底层异常。
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from .errors import (
    BaselineConflictError,
    BaselineNotFoundError,
    ConsumerConflictError,
    ConsumerNotFoundError,
    StorageError,
)
from .models import (
    ContractSnapshot,
    ConsumerExpectation,
    EndpointContract,
    FieldSchema,
    _NO_DEFAULT,
)

_SENTINEL_TOKEN = {"__sentinel__": "no_default"}


def _guard_io(func):
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except StorageError as e:
            raise e from None
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            reasons = {
                OSError: "io_failure",
                ValueError: "data_corruption",
                json.JSONDecodeError: "data_corruption",
            }
            reason = next(
                (r for typ, r in reasons.items() if isinstance(exc, typ)),
                "io_failure",
            )
            raise StorageError(
                f"本地存储操作失败: {exc}", reason=reason, cause=exc
            ) from exc

    return wrapper


class LocalStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self._lock = threading.RLock()

    # ---------- 目录与原子写 ----------
    @_guard_io
    def _ensure_dirs(self) -> None:
        (self.root / "baselines").mkdir(parents=True, exist_ok=True)
        (self.root / "consumers").mkdir(parents=True, exist_ok=True)

    def _write_new_exclusively(self, path: Path, payload: str) -> str:
        """独占创建文件。返回 'created'；若已存在则返回 'exists'。"""
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            return "exists"
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(path)
            raise
        return "created"

    def _atomic_replace(self, path: Path, payload: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise

    # ---------- 基线 ----------
    @_guard_io
    def save_baseline(self, snapshot: ContractSnapshot, *, overwrite: bool = False) -> str:
        with self._lock:
            self._ensure_dirs()
            path = self.root / "baselines" / f"{snapshot.version}.json"
            payload = json.dumps(
                self._encode_snapshot(snapshot),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                indent=2,
            )
            fp = snapshot.fingerprint()

            if path.exists():
                if self._fingerprint_file(path) == fp:
                    return "idempotent"
                if not overwrite:
                    raise BaselineConflictError(
                        f"基线版本 {snapshot.version} 已存在且契约内容不同，拒绝静默覆盖",
                        reason="baseline_already_exists",
                    )
                self._atomic_replace(path, payload)
                return "overwritten"

            outcome = self._write_new_exclusively(path, payload)
            if outcome == "exists":
                # 与其它线程/进程并发登记：必须以磁盘内容重新判定
                if self._fingerprint_file(path) == fp:
                    return "idempotent"
                raise BaselineConflictError(
                    f"并发登记冲突：版本 {snapshot.version} 已被其它写入者以不同内容登记",
                    reason="concurrent_baseline_conflict",
                )
            return "created"

    @_guard_io
    def load_baseline(self, version: str) -> ContractSnapshot:
        with self._lock:
            path = self.root / "baselines" / f"{version}.json"
            if not path.exists():
                raise BaselineNotFoundError(
                    f"基线版本不存在: {version}", reason="unknown_version"
                )
            return self._decode_snapshot(self._read_json(path))

    @_guard_io
    def list_baselines(self) -> list[str]:
        with self._lock:
            d = self.root / "baselines"
            if not d.exists():
                return []
            return sorted(p.stem for p in d.glob("*.json"))

    # ---------- 消费者 ----------
    @_guard_io
    def save_consumer(self, expectation: ConsumerExpectation, *, overwrite: bool = False) -> str:
        with self._lock:
            self._ensure_dirs()
            path = self.root / "consumers" / f"{expectation.consumer}.json"
            payload = json.dumps(
                expectation.to_canonical(),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                indent=2,
            )
            if path.exists():
                if self._same_json(path, expectation.to_canonical()):
                    return "idempotent"
                if not overwrite:
                    raise ConsumerConflictError(
                        f"消费者 {expectation.consumer} 的期望已存在且内容不同，拒绝静默覆盖",
                        reason="consumer_already_exists",
                    )
                self._atomic_replace(path, payload)
                return "overwritten"

            outcome = self._write_new_exclusively(path, payload)
            if outcome == "exists":
                if self._same_json(path, expectation.to_canonical()):
                    return "idempotent"
                raise ConsumerConflictError(
                    f"并发登记冲突：消费者 {expectation.consumer} 已被其它写入者以不同内容登记",
                    reason="concurrent_consumer_conflict",
                )
            return "created"

    @_guard_io
    def load_consumer(self, consumer: str) -> ConsumerExpectation:
        with self._lock:
            path = self.root / "consumers" / f"{consumer}.json"
            if not path.exists():
                raise ConsumerNotFoundError(
                    f"消费者不存在: {consumer}", reason="unknown_consumer"
                )
            data = self._read_json(path)
            return ConsumerExpectation(
                consumer=data["consumer"],
                version=data["version"],
                used_endpoints=frozenset(
                    (m, p) for m, p in data.get("used_endpoints", [])
                ),
                used_fields={
                    k: frozenset(v) for k, v in data.get("used_fields", {}).items()
                },
            )

    @_guard_io
    def list_consumers(self) -> list[str]:
        with self._lock:
            d = self.root / "consumers"
            if not d.exists():
                return []
            return sorted(p.stem for p in d.glob("*.json"))

    # ---------- JSON 与编码 ----------
    def _read_json(self, path: Path) -> dict[str, Any]:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except json.JSONDecodeError as exc:
            raise StorageError(
                f"存储文件损坏 {path}: {exc}", reason="data_corruption", cause=exc
            ) from exc
        if not isinstance(data, dict):
            raise StorageError(f"存储文件结构非法: {path}", reason="data_corruption")
        return data

    def _fingerprint_file(self, path: Path) -> str:
        data = self._read_json(path)
        body = json.dumps(
            data["contract"],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        )
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    def _same_json(self, path: Path, canonical: dict[str, Any]) -> bool:
        data = self._read_json(path)
        a = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
        b = json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str)
        return a == b

    def _encode_snapshot(self, snapshot: ContractSnapshot) -> dict[str, Any]:
        return {
            "version": snapshot.version,
            "captured_at": snapshot.captured_at,
            "contract": snapshot.to_canonical(),
        }

    def _decode_snapshot(self, data: dict[str, Any]) -> ContractSnapshot:
        contract = data.get("contract")
        if not isinstance(contract, dict):
            raise StorageError("基线文件缺少 contract 结构", reason="data_corruption")
        endpoints = tuple(self._decode_endpoint(item) for item in contract["endpoints"])
        return ContractSnapshot(
            version=data["version"],
            endpoints=endpoints,
            captured_at=data.get("captured_at", ""),
        )

    def _decode_endpoint(self, item: dict[str, Any]) -> EndpointContract:
        rb = item.get("request_body")
        return EndpointContract(
            method=item["method"],
            path=item["path"],
            request_body=self._decode_field(rb) if rb else None,
            query_params=tuple(self._decode_field(p) for p in item.get("query_params", [])),
            responses=tuple(
                (int(r["status"]), self._decode_field(r["schema"]))
                for r in item.get("responses", [])
            ),
        )

    def _decode_field(self, item: dict[str, Any]) -> FieldSchema:
        default: Any = _NO_DEFAULT
        if "default" in item:
            default = _NO_DEFAULT if item["default"] == _SENTINEL_TOKEN else item["default"]
        return FieldSchema(
            name=item["name"],
            type=item["type"],
            required=bool(item.get("required", False)),
            nullable=bool(item.get("nullable", False)),
            default=default,
            enum=tuple(item.get("enum", [])),
            item=self._decode_field(item["item"]) if item.get("item") else None,
            properties=tuple(self._decode_field(p) for p in item.get("properties", [])),
        )
