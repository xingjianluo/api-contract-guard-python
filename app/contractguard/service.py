"""领域服务：基线登记、版本比对、消费者期望、发布门禁。

保证：
- 同一对版本重复比对得到同一结论（进程内缓存 + 输入本身确定性）；
- 底层解析/存储异常归一为 errors.py 中的错误；
- 门禁把“影响已登记消费者的破坏性变更”和“无人使用但仍破坏性的变更”
  分别给出可区分原因。
"""
from __future__ import annotations

from typing import Any

from .diff import ContractDiffer
from .errors import (
    ContractGuardError,
    ContractParseError,
)
from .models import (
    Change,
    ConsumerExpectation,
    ContractSnapshot,
    DiffResult,
    GateVerdict,
)
from .parser import ContractParser
from .storage import LocalStore


def _expectation_error(reason: str, message: str) -> ContractParseError:
    return ContractParseError(message, reason=reason)


class ContractGuardService:
    def __init__(self, store: LocalStore) -> None:
        self._store = store
        self._parser = ContractParser()
        self._differ = ContractDiffer()
        self._diff_cache: dict[tuple[str, str], DiffResult] = {}

    # ---------- 基线 ----------
    def register_baseline(
        self, raw: dict[str, Any], *, version: str, overwrite: bool = False
    ) -> ContractSnapshot:
        # 解析异常本身已经是 ContractParseError；其余意外也做归一
        try:
            snapshot = self._parser.parse(raw, version=version)
            outcome = self._store.save_baseline(snapshot, overwrite=overwrite)
        except ContractGuardError:
            raise
        except Exception as exc:  # pragma: no cover - 兜底归一
            raise ContractGuardError(
                f"登记基线失败: {exc}", reason="register_failed", cause=exc
            ) from exc
        if outcome in ("created", "overwritten"):
            # 新版本写入后，使相关比对缓存失效，保证读到最新结论
            self._diff_cache = {
                k: v for k, v in self._diff_cache.items()
                if version not in k
            }
        return snapshot

    def list_baselines(self) -> list[str]:
        return self._store.list_baselines()

    # ---------- 比对 ----------
    def compare(self, old_version: str, new_version: str) -> DiffResult:
        key = (old_version, new_version)
        if key in self._diff_cache:
            return self._diff_cache[key]
        old = self._store.load_baseline(old_version)
        new = self._store.load_baseline(new_version)
        result = self._differ.diff(old, new)
        self._diff_cache[key] = result
        return result

    # ---------- 消费者期望 ----------
    def register_consumer(
        self, expectation_raw: dict[str, Any], *, consumer: str, overwrite: bool = False
    ) -> ConsumerExpectation:
        expectation = self._parse_expectation(consumer, expectation_raw)
        try:
            self._store.save_consumer(expectation, overwrite=overwrite)
        except ContractGuardError:
            raise
        except Exception as exc:  # pragma: no cover - 兜底归一
            raise ContractGuardError(
                f"登记消费者期望失败: {exc}", reason="register_failed", cause=exc
            ) from exc
        return expectation

    def _parse_expectation(self, consumer: str, raw: Any) -> ConsumerExpectation:
        if not isinstance(consumer, str) or not consumer.strip():
            raise _expectation_error("invalid_consumer", "消费者名称必须是非空字符串")
        if not isinstance(raw, dict):
            raise _expectation_error("expectation_not_object", "消费者期望必须是对象")
        version = raw.get("version")
        if not isinstance(version, str) or not version.strip():
            raise _expectation_error("invalid_expectation_version", "期望必须声明基线 version")

        endpoints: set[tuple[str, str]] = set()
        raw_eps = raw.get("used_endpoints", [])
        if not isinstance(raw_eps, list):
            raise _expectation_error("invalid_used_endpoints", "used_endpoints 必须是数组")
        for i, ep in enumerate(raw_eps):
            if (
                not isinstance(ep, list | tuple)
                or len(ep) != 2
                or not all(isinstance(x, str) and x for x in ep)
            ):
                raise _expectation_error(
                    "invalid_endpoint_ref",
                    f"used_endpoints[{i}] 必须是 [METHOD, PATH] 二元组",
                )
            endpoints.add((ep[0].upper(), ep[1]))

        used_fields: dict[str, frozenset[str]] = {}
        raw_fields = raw.get("used_fields", {})
        if not isinstance(raw_fields, dict):
            raise _expectation_error("invalid_used_fields", "used_fields 必须是对象")
        for key, paths in raw_fields.items():
            if not isinstance(key, str) or " " not in key:
                raise _expectation_error(
                    "invalid_field_key",
                    f"used_fields 键必须形如 'METHOD PATH'，得到 {key!r}",
                )
            method, path = key.split(" ", 1)
            endpoints.add((method.upper(), path.lstrip()))
            if not isinstance(paths, list) or not all(isinstance(p, str) and p for p in paths):
                raise _expectation_error(
                    "invalid_field_paths", f"used_fields[{key!r}] 必须是非空字符串数组"
                )
            used_fields[f"{method.upper()} {path.lstrip()}"] = frozenset(paths)

        if not endpoints:
            raise _expectation_error(
                "empty_expectation", "消费者期望至少声明一个使用的接口或字段"
            )

        return ConsumerExpectation(
            consumer=consumer,
            version=version,
            used_endpoints=frozenset(endpoints),
            used_fields=used_fields,
        )

    def list_consumers(self) -> list[str]:
        return self._store.list_consumers()

    # ---------- 发布门禁 ----------
    def gate(self, old_version: str, new_version: str) -> GateVerdict:
        result = self.compare(old_version, new_version)
        consumers = [self._store.load_consumer(name) for name in self._store.list_consumers()]

        breaking = [c for c in result.changes if c.breaking]
        impacts: list[dict[str, Any]] = []
        claimed_changes: set[int] = set()

        for exp in consumers:
            hit_changes: list[Change] = []
            for idx, change in enumerate(breaking):
                if self._change_touches(change, exp):
                    hit_changes.append(change)
                    claimed_changes.add(idx)
            if hit_changes:
                impacts.append(
                    {
                        "consumer": exp.consumer,
                        "expectation_version": exp.version,
                        "reason": "breaking_change_impacts_registered_consumer",
                        "changes": [
                            {
                                "kind": c.kind.value,
                                "location": c.location,
                                "detail": c.detail,
                            }
                            for c in hit_changes
                        ],
                    }
                )

        unused_breaking = [
            change for idx, change in enumerate(breaking) if idx not in claimed_changes
        ]

        reasons: list[str] = []
        if impacts:
            for impact in impacts:
                locs = ", ".join(c["location"] for c in impact["changes"])
                reasons.append(
                    f"消费者 {impact['consumer']} 受破坏性变更影响（{impact['reason']}）: {locs}"
                )
        if unused_breaking:
            locs = ", ".join(c.location for c in unused_breaking)
            reasons.append(
                f"存在未被任何已登记消费者使用、但仍属破坏性的变更"
                f"（unused_breaking_change）: {locs}"
            )

        return GateVerdict(
            target_version=new_version,
            allowed=not breaking,
            consumer_impacts=tuple(impacts),
            unused_breaking=tuple(unused_breaking),
            reasons=tuple(reasons),
        )

    @staticmethod
    def _endpoint_of(location: str) -> tuple[str, str] | None:
        """从变更位置中解析出 (METHOD, PATH) 与相对字段路径。"""
        parts = location.split(" ", 2)
        if len(parts) < 2:
            return None
        method, path = parts[0], parts[1]
        return method, path

    def _change_touches(self, change: Change, exp: ConsumerExpectation) -> bool:
        endpoint = self._endpoint_of(change.location)
        if endpoint is None or endpoint not in exp.used_endpoints:
            return False

        loc = f"{endpoint[0]} {endpoint[1]}"
        rel = change.location[len(loc):].lstrip(" ")

        # 消费者只声明了接口、未声明字段：整个接口都视为在用
        declared = exp.used_fields.get(loc)
        if not declared:
            return True

        # 接口级变更（如接口移除，rel 为空）必然命中
        if not rel:
            return True

        return any(self._path_touches(rel, f) for f in declared)

    @staticmethod
    def _path_touches(rel: str, field_path: str) -> bool:
        """判断变更相对路径与消费者声明的字段路径是否相交。

        支持点号层级与数组下标的前缀关系：
        声明 response[200].name 命中对该字段本身及其子结构的变更，反之亦然。
        """
        if rel == field_path:
            return True
        boundary = (".", "[")

        def is_prefix(prefix: str, other: str) -> bool:
            return other.startswith(prefix) and (
                len(other) == len(prefix) or other[len(prefix)] in boundary
            )

        return is_prefix(field_path, rel) or is_prefix(rel, field_path)
