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
# 读取端兼容的历史版本：v1 只有成功响应（response_fields）、无请求体必填性、
# 消费者只有操作级登记。
READABLE_SCHEMA_VERSIONS = frozenset({1, 2})


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


def response_to_dict(resp: ResponseModel) -> dict[str, Any]:
    return {
        "status_code": resp.status_code,
        "fields": [field_to_dict(f) for f in resp.fields],
    }


def endpoint_to_dict(ep: EndpointModel) -> dict[str, Any]:
    return {
        "method": ep.method,
        "path": ep.path,
        "deprecated": ep.deprecated,
        "request_body_required": ep.request_body_required,
        "request_params": [field_to_dict(f) for f in ep.request_params],
        "request_body": [field_to_dict(f) for f in ep.request_body],
        "responses": [response_to_dict(r) for r in ep.responses],
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


def dependency_to_dict(dep: ConsumerDependency) -> dict[str, Any]:
    return {
        "operation": dep.operation,
        "response": dep.response,
        "fields": list(dep.fields),
    }


def consumer_to_dict(c: ConsumerExpectation) -> dict[str, Any]:
    return {
        "service": c.service,
        "consumer": c.consumer,
        "operations": list(c.operations),
        "dependencies": [dependency_to_dict(d) for d in c.dependencies],
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


def response_from_dict(data: object) -> ResponseModel:
    if not isinstance(data, dict):
        raise StorageCorruptionError("expected_object:response")
    try:
        return ResponseModel(
            status_code=_require(data, "status_code", "response"),
            fields=tuple(field_from_dict(x) for x in data.get("fields", [])),
        )
    except StorageCorruptionError:
        raise
    except (ValueError, TypeError) as exc:
        raise StorageCorruptionError("invalid_response_record", cause=exc) from exc


def endpoint_from_dict(data: object) -> EndpointModel:
    if not isinstance(data, dict):
        raise StorageCorruptionError("expected_object:endpoint")
    try:
        if "responses" in data:
            responses = tuple(response_from_dict(x) for x in data["responses"])
        else:
            # v1 旧记录：只有成功响应字段，归一成单一 "200" 响应
            legacy_fields = tuple(
                field_from_dict(x) for x in data.get("response_fields", [])
            )
            responses = (ResponseModel(status_code="200", fields=legacy_fields),) \
                if legacy_fields else ()
        return EndpointModel(
            method=_require(data, "method", "endpoint"),
            path=_require(data, "path", "endpoint"),
            deprecated=bool(data.get("deprecated", False)),
            request_params=tuple(field_from_dict(x) for x in data.get("request_params", [])),
            request_body=tuple(field_from_dict(x) for x in data.get("request_body", [])),
            responses=responses,
            request_body_required=bool(data.get("request_body_required", False)),
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


def dependency_from_dict(data: object) -> ConsumerDependency:
    if not isinstance(data, dict):
        raise StorageCorruptionError("expected_object:dependency")
    try:
        return ConsumerDependency(
            operation=_require(data, "operation", "dependency"),
            response=_require(data, "response", "dependency"),
            fields=tuple(data.get("fields", [])),
        )
    except StorageCorruptionError:
        raise
    except (ValueError, TypeError) as exc:
        raise StorageCorruptionError("invalid_dependency_record", cause=exc) from exc


def consumer_from_dict(data: object) -> ConsumerExpectation:
    if not isinstance(data, dict):
        raise StorageCorruptionError("expected_object:consumer")
    try:
        return ConsumerExpectation(
            service=_require(data, "service", "consumer"),
            consumer=_require(data, "consumer", "consumer"),
            operations=tuple(_require(data, "operations", "consumer")),
            dependencies=tuple(
                dependency_from_dict(x) for x in data.get("dependencies", [])
            ),
        )
    except StorageCorruptionError:
        raise
    except (ValueError, TypeError) as exc:
        raise StorageCorruptionError("invalid_consumer_record", cause=exc) from exc


__all__ = [
    "SCHEMA_VERSION",
    "READABLE_SCHEMA_VERSIONS",
    "baseline_to_dict",
    "baseline_from_dict",
    "consumer_to_dict",
    "consumer_from_dict",
    "dependency_to_dict",
    "dependency_from_dict",
    "response_to_dict",
    "response_from_dict",
]
