"""契约采集与归一化。

支持的输入是 OpenAPI 3 风格的 JSON 文档（``paths`` 形态），可以传入
已解析的 dict，也可以直接传入 JSON 文本/字节。采集结果归一化为
:class:`~contract_guard.models.ContractSnapshot`：

* 字段统一按名排序（含嵌套对象），与原始声明顺序无关；
* 枚举值按稳定规则排序；
* 必填、可空、默认值（含“未声明 default”与“default: null”的区别）、
  枚举等语义全部显式化；
* 整份快照计算 sha256 指纹，作为幂等登记与重复判定的依据。

拒绝原因（code 稳定、可区分，绝不静默跳过）：

* :class:`ContractParseError`      文档不是合法 JSON / 顶层不是对象 / 结构错位
* :class:`IncompleteContractError` 信息缺失（缺 paths、字段缺类型、数组缺 items …）
* :class:`UnsupportedContractError` 未知类型、未知参数位置、不支持的媒体类型
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from .errors import (
    ContractParseError,
    IncompleteContractError,
    UnsupportedContractError,
)
from .models import (
    ContractSnapshot,
    Direction,
    EndpointModel,
    FieldModel,
    ParameterLocation,
    ResponseModel,
)

_HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})
_JSON_MEDIA = "application/json"


def _incomplete(reason: str) -> IncompleteContractError:
    return IncompleteContractError(reason)


def _unsupported(reason: str) -> UnsupportedContractError:
    return UnsupportedContractError(reason)


class ContractCollector:
    """从原始契约文档采集归一化快照。"""

    SUPPORTED_TYPES: frozenset[str] = frozenset(
        {"string", "integer", "number", "boolean", "array", "object"}
    )

    # ------------------------------------------------------------------ #
    # 入口
    # ------------------------------------------------------------------ #

    def collect(self, raw: object, *, service: str, version: str) -> ContractSnapshot:
        if not isinstance(service, str) or not service.strip():
            raise _incomplete("empty_service")
        if not isinstance(version, str) or not version.strip():
            raise _incomplete("empty_version")

        doc = self._parse_document(raw)
        endpoints = self._collect_endpoints(doc)
        fingerprint = self._fingerprint(service, version, endpoints)
        return ContractSnapshot(
            service=service,
            version=version,
            endpoints=endpoints,
            fingerprint=fingerprint,
        )

    # ------------------------------------------------------------------ #
    # 文档解析
    # ------------------------------------------------------------------ #

    def _parse_document(self, raw: object) -> Mapping[str, object]:
        if isinstance(raw, (bytes, bytearray)):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ContractParseError("document_not_utf8", cause=exc) from exc
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ContractParseError(
                    f"invalid_json: {exc.msg} at line {exc.lineno} col {exc.colno}",
                    cause=exc,
                ) from exc
        if not isinstance(raw, Mapping):
            raise ContractParseError("document_must_be_json_object")
        return raw

    # ------------------------------------------------------------------ #
    # paths / endpoints
    # ------------------------------------------------------------------ #

    def _collect_endpoints(self, doc: Mapping[str, object]) -> tuple[EndpointModel, ...]:
        if "paths" not in doc:
            raise _incomplete("missing_paths")
        paths = doc["paths"]
        if not isinstance(paths, Mapping):
            raise ContractParseError("paths_must_be_object")

        endpoints: list[EndpointModel] = []
        for path, item_raw in paths.items():
            if not isinstance(path, str) or not path.startswith("/"):
                raise _incomplete(f"invalid_path:{path!r}")
            if not isinstance(item_raw, Mapping):
                raise ContractParseError(f"path_item_must_be_object:{path}")
            for method, op_raw in item_raw.items():
                if method not in _HTTP_METHODS:
                    # path 级其它字段（parameters/summary 等）本实现不支持合并，忽略
                    continue
                if not isinstance(op_raw, Mapping):
                    raise ContractParseError(f"operation_must_be_object:{method} {path}")
                op_key = f"{method.upper()} {path}"
                endpoints.append(self._collect_endpoint(op_raw, op_key))

        # 同一 method+path 不可能因 dict 键重复；再按 (path, method) 稳定排序
        endpoints.sort(key=lambda e: (e.path, e.method))
        return tuple(endpoints)

    def _collect_endpoint(
        self, op: Mapping[str, object], op_key: str
    ) -> EndpointModel:
        params = self._collect_parameters(op.get("parameters"), op_key)
        body, body_required = self._collect_body(op.get("requestBody"), op_key)
        responses = self._collect_responses(op.get("responses"), op_key)
        deprecated = bool(op.get("deprecated", False))
        return EndpointModel(
            method=op_key.split(" ", 1)[0],
            path=op_key.split(" ", 1)[1],
            request_params=params,
            request_body=body,
            request_body_required=body_required,
            responses=responses,
            deprecated=deprecated,
        )

    # ------------------------------------------------------------------ #
    # 请求参数
    # ------------------------------------------------------------------ #

    def _collect_parameters(
        self, raw: object, op_key: str
    ) -> tuple[FieldModel, ...]:
        if raw is None:
            return ()
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            raise ContractParseError(f"parameters_must_be_array:{op_key}")

        seen: set[tuple[str, str]] = set()
        fields: list[FieldModel] = []
        for index, param in enumerate(raw):
            ctx = f"{op_key} parameter[{index}]"
            if not isinstance(param, Mapping):
                raise ContractParseError(f"parameter_must_be_object:{ctx}")
            name = param.get("name")
            if not isinstance(name, str) or not name:
                raise _incomplete(f"parameter_missing_name:{ctx}")
            location_raw = param.get("in")
            if not isinstance(location_raw, str):
                raise _incomplete(f"parameter_missing_location:{ctx}:{name}")
            try:
                location = ParameterLocation(location_raw)
            except ValueError:
                raise _unsupported(f"unknown_parameter_location:{location_raw}") from None
            dedupe_key = (location.value, name)
            if dedupe_key in seen:
                raise _incomplete(f"duplicate_parameter:{location.value}:{name}@{op_key}")
            seen.add(dedupe_key)

            required = bool(param.get("required", False))
            if location is ParameterLocation.PATH:
                # OpenAPI 强制 path 参数必填；显式 required=false 属于畸形契约
                if "required" in param and not required:
                    raise _incomplete(f"path_parameter_must_be_required:{name}@{op_key}")
                required = True

            schema = param.get("schema")
            if not isinstance(schema, Mapping):
                raise _incomplete(f"parameter_missing_schema:{ctx}:{name}")
            fields.append(
                self._build_field(
                    schema,
                    name=name,
                    direction=Direction.REQUEST,
                    location=location,
                    required=required,
                    context=ctx,
                )
            )
        return self._sort_fields(fields)

    # ------------------------------------------------------------------ #
    # 请求体
    # ------------------------------------------------------------------ #

    def _collect_body(
        self, raw: object, op_key: str
    ) -> tuple[tuple[FieldModel, ...], bool]:
        if raw is None:
            return (), False
        if not isinstance(raw, Mapping):
            raise ContractParseError(f"request_body_must_be_object:{op_key}")
        content = raw.get("content")
        if not isinstance(content, Mapping):
            raise _incomplete(f"request_body_missing_content:{op_key}")
        media = content.get(_JSON_MEDIA)
        if media is None:
            raise _unsupported(f"unsupported_request_media_type:{op_key}")
        if not isinstance(media, Mapping):
            raise ContractParseError(f"media_type_object_must_be_object:{op_key}")
        schema = media.get("schema")
        if not isinstance(schema, Mapping):
            raise _incomplete(f"request_body_missing_schema:{op_key}")
        required = bool(raw.get("required", False))
        fields = self._schema_fields(schema, Direction.REQUEST, None, op_key, "requestBody")
        return fields, required

    # ------------------------------------------------------------------ #
    # 响应（同一操作声明的每种结果都建模：所有状态码 + default）
    # ------------------------------------------------------------------ #

    def _collect_responses(
        self, raw: object, op_key: str
    ) -> tuple[ResponseModel, ...]:
        if not isinstance(raw, Mapping):
            raise _incomplete(f"missing_responses:{op_key}")

        statuses: list[str] = []
        for code in raw:
            if not isinstance(code, str):
                raise ContractParseError(f"response_code_must_be_string:{op_key}")
            if code == "default":
                statuses.append(code)
            elif self._is_status_code(code):
                statuses.append(code)
            else:
                raise _unsupported(f"unknown_response_status:{code}@{op_key}")
        if not statuses:
            raise _incomplete(f"missing_responses:{op_key}")

        success = [s for s in statuses if s != "default" and s.startswith("2")]
        if not success:
            raise _incomplete(f"missing_success_response:{op_key}")

        responses: list[ResponseModel] = []
        for status in sorted(statuses, key=self._status_sort_key):
            resp = raw[status]
            if not isinstance(resp, Mapping):
                raise ContractParseError(f"response_must_be_object:{op_key}:{status}")
            responses.append(
                ResponseModel(status=status, fields=self._collect_one_response(resp, op_key, status))
            )
        return tuple(responses)

    @staticmethod
    def _is_status_code(code: str) -> bool:
        return len(code) == 3 and code.isdigit()

    @staticmethod
    def _status_sort_key(status: str) -> tuple[int, str]:
        # 数字状态码按数值语义；default 永远排最后
        return (1, "") if status == "default" else (0, status)

    def _collect_one_response(
        self, resp: Mapping[str, object], op_key: str, status: str
    ) -> tuple[FieldModel, ...]:
        content = resp.get("content")
        if content is None:
            return ()  # 204 风格：允许无响应体
        if not isinstance(content, Mapping):
            raise ContractParseError(f"response_content_must_be_object:{op_key}:{status}")
        media = content.get(_JSON_MEDIA)
        if media is None:
            return ()  # 非 JSON 响应体不纳入契约模型
        if not isinstance(media, Mapping):
            raise ContractParseError(f"media_type_object_must_be_object:{op_key}:{status}")
        schema = media.get("schema")
        if not isinstance(schema, Mapping):
            raise _incomplete(f"response_missing_schema:{op_key}:{status}")
        return self._schema_fields(
            schema, Direction.RESPONSE, None, op_key, f"response-{status}"
        )

    # ------------------------------------------------------------------ #
    # schema -> FieldModel
    # ------------------------------------------------------------------ #

    def _schema_fields(
        self,
        schema: Mapping[str, object],
        direction: Direction,
        location: ParameterLocation | None,
        op_key: str,
        where: str,
    ) -> tuple[FieldModel, ...]:
        """把 object schema 的 properties 展开为字段元组（按名排序）。"""
        type_name = schema.get("type")
        if type_name != "object":
            raise _incomplete(f"{where}_schema_must_be_object:{op_key}")
        properties = schema.get("properties", {})
        if not isinstance(properties, Mapping):
            raise ContractParseError(f"properties_must_be_object:{op_key}:{where}")
        required_raw = schema.get("required", [])
        if not isinstance(required_raw, Sequence) or isinstance(required_raw, (str, bytes)):
            raise ContractParseError(f"required_must_be_array:{op_key}:{where}")
        required_names = set(required_raw)
        if not all(isinstance(n, str) for n in required_names):
            raise ContractParseError(f"required_entries_must_be_string:{op_key}:{where}")
        unknown = required_names - set(properties)
        if unknown:
            raise _incomplete(
                f"required_field_not_defined:{op_key}:{where}:{sorted(unknown)[0]}"
            )

        fields = [
            self._build_field(
                sub,
                name=name,
                direction=direction,
                location=location,
                required=name in required_names,
                context=f"{op_key} {where}.{name}",
            )
            for name, sub in properties.items()
        ]
        return self._sort_fields(fields)

    def _build_field(
        self,
        raw: object,
        *,
        name: str,
        direction: Direction,
        location: ParameterLocation | None,
        required: bool,
        context: str,
    ) -> FieldModel:
        if not isinstance(raw, Mapping):
            raise ContractParseError(f"schema_must_be_object:{context}")

        type_name = raw.get("type")
        if not isinstance(type_name, str) or not type_name:
            raise _incomplete(f"missing_type:{context}")
        if type_name not in self.SUPPORTED_TYPES:
            raise _unsupported(f"unknown_type:{type_name}@{context}")

        nullable = bool(raw.get("nullable", False))
        has_default = "default" in raw
        default = raw.get("default") if has_default else None
        if has_default and not nullable and default is None and type_name != "object":
            # 非 nullable 字段却 default: null，语义矛盾，按畸形拒绝
            raise _incomplete(f"default_null_on_non_nullable:{context}")

        enum_values: tuple[object, ...] = ()
        if "enum" in raw:
            enum_values = self._normalize_enum(raw.get("enum"), context)

        item_type: str | None = None
        children: tuple[FieldModel, ...] = ()
        if type_name == "array":
            items = raw.get("items")
            if not isinstance(items, Mapping):
                raise _incomplete(f"array_missing_items:{context}")
            item_type = items.get("type")
            if not isinstance(item_type, str) or item_type not in self.SUPPORTED_TYPES:
                raise _unsupported(f"unknown_array_item_type:{context}")
            if item_type == "array" and not isinstance(items.get("items"), Mapping):
                raise _incomplete(f"nested_array_missing_items:{context}")
        elif type_name == "object":
            children = self._object_children(raw, direction, location, context)

        return FieldModel(
            name=name,
            type_name=type_name,
            required=required,
            nullable=nullable,
            default=default,
            has_default=has_default,
            enum=enum_values,
            item_type=item_type,
            children=children,
            direction=direction,
            location=location,
            deprecated=bool(raw.get("deprecated", False)),
        )

    def _object_children(
        self,
        schema: Mapping[str, object],
        direction: Direction,
        location: ParameterLocation | None,
        context: str,
    ) -> tuple[FieldModel, ...]:
        properties = schema.get("properties", {})
        if not isinstance(properties, Mapping):
            raise ContractParseError(f"properties_must_be_object:{context}")
        required_raw = schema.get("required", [])
        if not isinstance(required_raw, Sequence) or isinstance(required_raw, (str, bytes)):
            raise ContractParseError(f"required_must_be_array:{context}")
        required_names = set(required_raw)
        if not all(isinstance(n, str) for n in required_names):
            raise ContractParseError(f"required_entries_must_be_string:{context}")
        unknown = required_names - set(properties)
        if unknown:
            raise _incomplete(f"required_field_not_defined:{context}:{sorted(unknown)[0]}")
        children = [
            self._build_field(
                sub,
                name=child_name,
                direction=direction,
                location=location,
                required=child_name in required_names,
                context=f"{context}.{child_name}",
            )
            for child_name, sub in properties.items()
        ]
        return self._sort_fields(children)

    # ------------------------------------------------------------------ #
    # 归一化工具
    # ------------------------------------------------------------------ #

    _ENUM_RANK = {type(None): 0, bool: 1, int: 2, float: 3, str: 4}

    def _normalize_enum(self, values: object, context: str) -> tuple[object, ...]:
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
            raise ContractParseError(f"enum_must_be_array:{context}")
        if len(values) == 0:
            raise _incomplete(f"enum_empty:{context}")
        cleaned: list[object] = []
        for value in values:
            if isinstance(value, (str, bool, int, float)) or value is None:
                cleaned.append(value)
            else:
                raise _unsupported(f"unsupported_enum_value:{context}")
        if len(set(cleaned)) != len(cleaned):
            raise _incomplete(f"enum_duplicate_values:{context}")
        try:
            cleaned.sort(key=lambda v: (self._ENUM_RANK[type(v)], repr(v)))
        except TypeError as exc:  # pragma: no cover - 理论不可达
            raise _unsupported(f"enum_not_orderable:{context}") from exc
        return tuple(cleaned)

    def _sort_fields(self, fields: Sequence[FieldModel]) -> tuple[FieldModel, ...]:
        return tuple(sorted(fields, key=lambda f: (f.location.value if f.location else "", f.name)))

    # ------------------------------------------------------------------ #
    # 指纹（规范化 JSON + sha256）
    # ------------------------------------------------------------------ #

    def _fingerprint(
        self, service: str, version: str, endpoints: Sequence[EndpointModel]
    ) -> str:
        payload = {
            "service": service,
            "version": version,
            "endpoints": [self._endpoint_to_primitive(e) for e in endpoints],
        }
        canonical = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _endpoint_to_primitive(self, endpoint: EndpointModel) -> dict[str, Any]:
        return {
            "method": endpoint.method,
            "path": endpoint.path,
            "deprecated": endpoint.deprecated,
            "request_body_required": endpoint.request_body_required,
            "request_params": [self._field_to_primitive(f) for f in endpoint.request_params],
            "request_body": [self._field_to_primitive(f) for f in endpoint.request_body],
            "responses": [
                {
                    "status": r.status,
                    "fields": [self._field_to_primitive(f) for f in r.fields],
                }
                for r in endpoint.responses
            ],
        }

    def _field_to_primitive(self, field: FieldModel) -> dict[str, Any]:
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
            "children": [self._field_to_primitive(c) for c in field.children],
        }


__all__ = [
    "ContractCollector",
    "ContractParseError",
    "IncompleteContractError",
    "UnsupportedContractError",
]
