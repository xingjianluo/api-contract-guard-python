"""归一化后的稳定可比较契约模型。

语义约定：
- 字段顺序不影响等价性（集合按 (method, path, status) / 字段名 排序后比较）；
- required / nullable / default / enum 均显式归一；
- 枚举归一为排序后的元组；
- default 使用哨兵区分“未声明默认值”与“默认值为 null”。
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ChangeLevel(str, Enum):
    """变更分级。"""

    ADDED = "added"            # 纯新增，不影响既有消费者
    COMPATIBLE = "compatible"  # 兼容调整（可选字段、必填转可选等）
    BREAKING = "breaking"      # 破坏性变更


class ChangeKind(str, Enum):
    """变更的具体种类，作为可解释的判定依据。"""

    ENDPOINT_ADDED = "endpoint_added"
    ENDPOINT_REMOVED = "endpoint_removed"
    FIELD_ADDED_REQUIRED = "field_added_required"
    FIELD_ADDED_OPTIONAL = "field_added_optional"
    FIELD_REMOVED = "field_removed"
    REQUIRED_TO_OPTIONAL = "required_to_optional"
    OPTIONAL_TO_REQUIRED = "optional_to_required"
    NULLABLE_TO_NON_NULLABLE = "nullable_to_non_nullable"
    NON_NULLABLE_TO_NULLABLE = "non_nullable_to_nullable"
    TYPE_CHANGED = "type_changed"
    DEFAULT_VALUE_CHANGED = "default_value_changed"
    ENUM_VALUE_ADDED = "enum_value_added"
    ENUM_VALUE_REMOVED = "enum_value_removed"
    RESPONSE_STATUS_REMOVED = "response_status_removed"
    RESPONSE_STATUS_ADDED = "response_status_added"
    NONE = "none"


# 哨兵：区分“未声明默认值”与“显式默认值为 None”
_NO_DEFAULT = object()
_NO_DEFAULT_TOKEN = {"__sentinel__": "no_default"}


def _canon_default(value: Any) -> Any:
    if value is _NO_DEFAULT:
        # 深拷贝语义上的独立 token
        return dict(_NO_DEFAULT_TOKEN)
    return value


def _stable_json(obj: Any) -> str:
    """sort_keys + 紧凑分隔，保证可复现的字节序列。"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


@dataclass(frozen=True)
class FieldSchema:
    """字段 / 参数的归一化结构。"""

    name: str
    type: str
    required: bool = False
    nullable: bool = False
    default: Any = _NO_DEFAULT
    enum: tuple[Any, ...] = ()
    item: "FieldSchema | None" = None
    properties: tuple["FieldSchema", ...] = ()

    def to_canonical(self) -> Any:
        """转为稳定的、可 JSON 序列化的规范结构。"""
        # 子属性按字段名排序，消除输入顺序影响
        props = sorted((p.to_canonical() for p in self.properties), key=_stable_json)
        return {
            "name": self.name,
            "type": self.type,
            "required": bool(self.required),
            "nullable": bool(self.nullable),
            "default": _canon_default(self.default),
            # 枚举值排序后归一，顺序变化不构成差异
            "enum": sorted(self.enum, key=lambda v: _stable_json(v)),
            "item": self.item.to_canonical() if self.item is not None else None,
            "properties": props,
        }

    def fingerprint(self) -> str:
        """字段内容指纹（与顺序无关的稳定哈希）。"""
        return hashlib.sha256(_stable_json(self.to_canonical()).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EndpointContract:
    """单个接口（method + path）的契约。"""

    method: str
    path: str
    request_body: FieldSchema | None = None
    query_params: tuple[FieldSchema, ...] = ()
    responses: tuple[tuple[int, FieldSchema], ...] = ()

    def to_canonical(self) -> Any:
        return {
            "method": self.method.upper(),
            "path": self.path,
            "request_body": self.request_body.to_canonical() if self.request_body is not None else None,
            "query_params": sorted(
                (p.to_canonical() for p in self.query_params), key=lambda c: c["name"]
            ),
            "responses": [
                {"status": status, "schema": schema.to_canonical()}
                for status, schema in sorted(self.responses, key=lambda pair: pair[0])
            ],
        }

    def fingerprint(self) -> str:
        return hashlib.sha256(_stable_json(self.to_canonical()).encode("utf-8")).hexdigest()

    @property
    def key(self) -> tuple[str, str]:
        return self.method.upper(), self.path


@dataclass(frozen=True)
class ContractSnapshot:
    """一份服务契约的完整归一化快照。"""

    version: str
    endpoints: tuple[EndpointContract, ...] = ()
    captured_at: str = ""

    def to_canonical(self) -> Any:
        return {
            "endpoints": sorted(
                (e.to_canonical() for e in self.endpoints),
                key=lambda c: (c["method"], c["path"]),
            ),
        }

    def fingerprint(self) -> str:
        """整份契约的内容指纹（不含版本号与采集时间）。"""
        return hashlib.sha256(_stable_json(self.to_canonical()).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Change:
    """单条变更及其判定依据。"""

    level: ChangeLevel
    kind: ChangeKind
    location: str
    detail: str
    breaking: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level.value,
            "kind": self.kind.value,
            "location": self.location,
            "detail": self.detail,
            "breaking": bool(self.breaking),
        }


@dataclass(frozen=True)
class DiffResult:
    """两个版本之间的比对结论。"""

    old_version: str
    new_version: str
    changes: tuple[Change, ...]
    compatible: bool
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "old_version": self.old_version,
            "new_version": self.new_version,
            "compatible": bool(self.compatible),
            "rationale": self.rationale,
            "changes": [c.to_dict() for c in self.changes],
        }


@dataclass(frozen=True)
class ConsumerExpectation:
    """消费者对契约的使用期望。

    used_endpoints 为该消费者实际使用的 (METHOD, PATH) 集合；
    used_fields 记录每个 "METHOD PATH" 下被消费的字段路径（点号路径）。
    """

    consumer: str
    version: str
    used_endpoints: frozenset[tuple[str, str]] = frozenset()
    used_fields: dict[str, frozenset[str]] = field(default_factory=dict)

    def to_canonical(self) -> Any:
        return {
            "consumer": self.consumer,
            "version": self.version,
            "used_endpoints": sorted(
                [[m.upper(), p] for m, p in self.used_endpoints]
            ),
            "used_fields": {
                key: sorted(paths) for key, paths in sorted(self.used_fields.items())
            },
        }


@dataclass(frozen=True)
class GateVerdict:
    """发布门禁判定结论。"""

    target_version: str
    allowed: bool
    consumer_impacts: tuple[dict[str, Any], ...] = ()
    unused_breaking: tuple[Change, ...] = ()
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_version": self.target_version,
            "allowed": bool(self.allowed),
            "reasons": list(self.reasons),
            "consumer_impacts": list(self.consumer_impacts),
            "unused_breaking": [c.to_dict() for c in self.unused_breaking],
        }
