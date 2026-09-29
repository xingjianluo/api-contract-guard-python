"""领域模型 <-> 可 JSON 持久化的原始结构。

持久化结构带 ``schema_version``，读取时校验；无法识别或损坏的数据
统一抛 :class:`StorageCorruptionError`。
"""

from __future__ import annotations

from typing import Any

from .errors import StorageCorruptionError
from .models import (
    Baseline,
    ConsumerDependency,
    ConsumerExpectation,
    ContractSnapshot,
    Direction,
    EndpointModel,
    FieldModel,
    ParameterLocation,
    ResponseModel,
)

SCHEMA_VERSION = 2


def field_to_dict(field: FieldModel) -> dict[str, Any]:
    return {
        "name": field.name,
        "type": field.type_name,
        "required": field.required,
        "nullable": field.nullable,
        "has_default": field.has_default,
        "default": field.default,
        "enum": list(field.enum),
        "item_type": field.item_type,
        "location": field.location.value if field.location else None,
        "direction": field.direction.value,
        "deprecated": field.deprecated,
        "children": [field_to_dict(c) for c in field.children],
    }


def endpoint_to_dict(ep: EndpointModel) -> dict[str, Any]:
    return {
        "method": ep.method,
        "path": ep.path,
        "deprecated": ep.deprecated,
        "request_body_required": ep.request_body_required,
        "request_params": [field_to_dict(f) for f in ep.request_params],
        "request_body": [field_to_dict(f) for f in ep.request_body],
        "responses": [
            {"status": r.status, "fields": [field_to_dict(f) for f in r.fields]}
            for r in ep.responses
        ],
    }


def snapshot_to_dict(snap: ContractSnapshot) -> dict[str, Any]:
    return {
        "service": snap.service,
        "version": snap.version,
        "fingerprint": snap.fingerprint,
        "endpoints": [endpoint_to_dict(e) for e in snap.endpoints],
    }


def baseline_to_dict(b: Baseline) -> dict[str, Any]:
    return {
        "service": b.service,
        "version": b.version,
        "created_at": b.created_at,
        "snapshot": snapshot_to_dict(b.snapshot),
    }


def consumer_to_dict(c: ConsumerExpectation) -> dict[str, Any]:
    return {
        "service": c.service,
        "consumer": c.consumer,
        "operations": list(c.operations),
        "dependencies": [
            {"operation": d.operation, "response": d.response, "fields": list(d.fields)}
            for d in c.dependencies
        ],
    }


# --------------------------------------------------------------------------- #
# 反向
# --------------------------------------------------------------------------- #


def _require(mapping: object, key: str, ctx: str) -> Any:
    if not isinstance(mapping, dict):
        raise StorageCorruptionError(f"expected_object:{ctx}")
    if key not in mapping:
        raise StorageCorruptionError(f"missing_key:{ctx}:{key}")
    return mapping[key]


def field_from_dict(data: object, *, direction_default: Direction | None = None) -> FieldModel:
    if not isinstance(data, dict):
        raise StorageCorruptionError("expected_object:field")
    try:
        direction = Direction(_require(data, "direction", "field"))
        location_raw = data.get("location")
        location = ParameterLocation(location_raw) if location_raw else None
        children_raw = data.get("children", [])
        children = tuple(field_from_dict(c) for c in children_raw)
        return FieldModel(
            name=_require(data, "name", "field"),
            type_name=_require(data, "type", "field"),
            required=bool(data.get("required", False)),
            nullable=bool(data.get("nullable", False)),
            has_default=bool(data.get("has_default", False)),
            default=data.get("default"),
            enum=tuple(data.get("enum", [])),
            item_type=data.get("item_type"),
            children=children,
            direction=direction,
            location=location,
            deprecated=bool(data.get("deprecated", False)),
        )
    except StorageCorruptionError:
        raise
    except (ValueError, TypeError) as exc:
        raise StorageCorruptionError("invalid_field_record", cause=exc) from exc


def endpoint_from_dict(data: object) -> EndpointModel:
    if not isinstance(data, dict):
        raise StorageCorruptionError("expected_object:endpoint")
    try:
        responses_raw = data.get("responses")
        if responses_raw is not None:
            responses = tuple(
                ResponseModel(
                    status=_require(item, "status", "response"),
                    fields=tuple(
                        field_from_dict(x) for x in item.get("fields", [])
                    ),
                )
                for item in responses_raw
            )
        else:
            # 向后兼容 schema v1：单个成功响应字段集合，归一化为 "200"
            responses = (
                ResponseModel(
                    status="200",
                    fields=tuple(
                        field_from_dict(x) for x in data.get("response_fields", [])
                    ),
                ),
            )
        return EndpointModel(
            method=_require(data, "method", "endpoint"),
            path=_require(data, "path", "endpoint"),
            deprecated=bool(data.get("deprecated", False)),
            request_body_required=bool(data.get("request_body_required", False)),
            request_params=tuple(field_from_dict(x) for x in data.get("request_params", [])),
            request_body=tuple(field_from_dict(x) for x in data.get("request_body", [])),
            responses=responses,
        )
    except StorageCorruptionError:
        raise
    except (ValueError, TypeError) as exc:
        raise StorageCorruptionError("invalid_endpoint_record", cause=exc) from exc


def snapshot_from_dict(data: object) -> ContractSnapshot:
    if not isinstance(data, dict):
        raise StorageCorruptionError("expected_object:snapshot")
    try:
        return ContractSnapshot(
            service=_require(data, "service", "snapshot"),
            version=_require(data, "version", "snapshot"),
            fingerprint=_require(data, "fingerprint", "snapshot"),
            endpoints=tuple(
                endpoint_from_dict(x) for x in _require(data, "endpoints", "snapshot")
            ),
        )
    except StorageCorruptionError:
        raise
    except (ValueError, TypeError) as exc:
        raise StorageCorruptionError("invalid_snapshot_record", cause=exc) from exc


def baseline_from_dict(data: object) -> Baseline:
    if not isinstance(data, dict):
        raise StorageCorruptionError("expected_object:baseline")
    try:
        return Baseline(
            service=_require(data, "service", "baseline"),
            version=_require(data, "version", "baseline"),
            created_at=_require(data, "created_at", "baseline"),
            snapshot=snapshot_from_dict(_require(data, "snapshot", "baseline")),
        )
    except StorageCorruptionError:
        raise
    except (ValueError, TypeError) as exc:
        raise StorageCorruptionError("invalid_baseline_record", cause=exc) from exc


def consumer_from_dict(data: object) -> ConsumerExpectation:
    if not isinstance(data, dict):
        raise StorageCorruptionError("expected_object:consumer")
    try:
        dependencies = tuple(
            ConsumerDependency(
                operation=_require(item, "operation", "dependency"),
                response=_require(item, "response", "dependency"),
                fields=tuple(item.get("fields", [])),
            )
            for item in data.get("dependencies", [])
        )
        return ConsumerExpectation(
            service=_require(data, "service", "consumer"),
            consumer=_require(data, "consumer", "consumer"),
            operations=tuple(_require(data, "operations", "consumer")),
            dependencies=dependencies,
        )
    except StorageCorruptionError:
        raise
    except (ValueError, TypeError) as exc:
        raise StorageCorruptionError("invalid_consumer_record", cause=exc) from exc


__all__ = [
    "SCHEMA_VERSION",
    "baseline_to_dict",
    "baseline_from_dict",
    "consumer_to_dict",
    "consumer_from_dict",
]
