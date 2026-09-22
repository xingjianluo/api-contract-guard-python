"""从服务实际暴露的接口契约（类 OpenAPI 描述）采集并归一化。

解析策略为“严格、不静默跳过”：
- 任何结构缺失、类型不符、不支持的引用都会抛 ContractParseError；
- reason 给出可区分的拒绝原因；
- 成功路径上的所有语义（required/nullable/default/enum）都被显式归一。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .errors import ContractParseError
from .models import ContractSnapshot, EndpointContract, FieldSchema, _NO_DEFAULT

_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}
_PRIMITIVES = {"string", "integer", "number", "boolean", "array", "object", "null"}


def _err(reason: str, message: str) -> ContractParseError:
    return ContractParseError(message, reason=reason)


class ContractParser:
    """把原始契约描述解析为 ContractSnapshot。"""

    def parse(self, raw: Any, *, version: str) -> ContractSnapshot:
        if not isinstance(version, str) or not version.strip():
            raise _err("invalid_version", "版本号必须是非空字符串")
        if not isinstance(raw, dict):
            raise _err("not_an_object", "契约根节点必须是对象")
        if "paths" not in raw:
            raise _err("missing_paths", "契约缺少 paths 字段，无法采集接口")
        paths = raw["paths"]
        if not isinstance(paths, dict):
            raise _err("paths_not_object", "paths 必须是对象")
        if not paths:
            raise _err("empty_contract", "paths 为空，采集不到任何接口契约")

        endpoints: list[EndpointContract] = []
        for path, item in paths.items():
            if not isinstance(path, str) or not path.startswith("/"):
                raise _err("invalid_path", f"非法路径: {path!r}，必须以 / 开头")
            if not isinstance(item, dict):
                raise _err("invalid_path_item", f"路径 {path} 的描述必须是对象")
            for method, op in item.items():
                if method.lower() not in _HTTP_METHODS:
                    # path 级公共参数等非操作字段直接拒绝，避免静默丢弃
                    raise _err("unsupported_path_field", f"路径 {path} 下出现不支持的字段: {method}")
                if not isinstance(op, dict):
                    raise _err("invalid_operation", f"{method.upper()} {path} 的操作描述必须是对象")
                endpoints.append(self._parse_endpoint(method, path, op))

        return ContractSnapshot(
            version=version,
            endpoints=tuple(endpoints),
            captured_at=datetime.now(timezone.utc).isoformat(),
        )

    def _parse_endpoint(self, method: str, path: str, op: dict[str, Any]) -> EndpointContract:
        loc = f"{method.upper()} {path}"

        request_body: FieldSchema | None = None
        if "requestBody" in op:
            rb = op["requestBody"]
            if not isinstance(rb, dict):
                raise _err("invalid_request_body", f"{loc} 的 requestBody 必须是对象")
            schema = self._resolve_schema(rb, loc)
            request_body = self._parse_schema("body", schema, loc, implicit_required=bool(rb.get("required", False)))

        query_params: list[FieldSchema] = []
        for i, param in enumerate(op.get("parameters", [])):
            if not isinstance(param, dict):
                raise _err("invalid_parameter", f"{loc} parameters[{i}] 必须是对象")
            if param.get("in") != "query":
                raise _err("unsupported_parameter_in", f"{loc} 仅支持 query 参数，得到 {param.get('in')!r}")
            name = param.get("name")
            if not isinstance(name, str) or not name:
                raise _err("invalid_parameter_name", f"{loc} parameters[{i}] 缺少合法 name")
            if "schema" not in param or not isinstance(param["schema"], dict):
                raise _err("missing_parameter_schema", f"{loc} 参数 {name} 缺少 schema")
            schema = self._parse_schema(name, param["schema"], loc)
            # OpenAPI 语义：未显式声明 required 时为必填（参数默认 required=true 不成立，
            # 实际默认 false）；这里以显式声明为准，默认 False。
            query_params.append(
                FieldSchema(
                    name=schema.name,
                    type=schema.type,
                    required=bool(param.get("required", False)),
                    nullable=schema.nullable,
                    default=schema.default,
                    enum=schema.enum,
                    item=schema.item,
                    properties=schema.properties,
                )
            )

        responses: list[tuple[int, FieldSchema]] = []
        if "responses" not in op or not isinstance(op["responses"], dict) or not op["responses"]:
            raise _err("missing_responses", f"{loc} 必须声明至少一个响应")
        for status_text, resp in op["responses"].items():
            try:
                status = int(status_text)
            except (TypeError, ValueError):
                raise _err("invalid_response_status", f"{loc} 非法响应状态码: {status_text!r}")
            if not isinstance(resp, dict):
                raise _err("invalid_response", f"{loc} {status_text} 响应必须是对象")
            content = resp.get("content")
            if content is None:
                # 无 body（如 204）：用空对象结构表示
                body_schema = self._parse_schema("body", {"type": "object"}, loc)
            else:
                schema = self._resolve_schema(resp, f"{loc} {status_text}")
                body_schema = self._parse_schema("body", schema, loc)
            responses.append((status, body_schema))

        return EndpointContract(
            method=method.upper(),
            path=path,
            request_body=request_body,
            query_params=tuple(query_params),
            responses=tuple(responses),
        )

    def _resolve_schema(self, holder: dict[str, Any], loc: str) -> dict[str, Any]:
        """从 requestBody/response 的 content 中取出 JSON schema。

        不支持 $ref / components 复用：引用会被明确拒绝而不是静默忽略。
        """
        content = holder.get("content")
        if not isinstance(content, dict) or not content:
            raise _err("missing_content", f"{loc} 缺少 content 定义")
        json_media = content.get("application/json")
        if not isinstance(json_media, dict):
            raise _err("unsupported_media_type", f"{loc} 仅支持 application/json 内容")
        schema = json_media.get("schema")
        if not isinstance(schema, dict):
            raise _err("missing_schema", f"{loc} 的 application/json 缺少 schema")
        return schema

    def _parse_schema(
        self,
        name: str,
        schema: dict[str, Any],
        loc: str,
        *,
        implicit_required: bool = False,
    ) -> FieldSchema:
        if "$ref" in schema:
            raise _err("unsupported_ref", f"{loc} 字段 {name} 使用了不支持的 $ref: {schema['$ref']}")
        typ = schema.get("type")
        if not isinstance(typ, str) or not typ:
            raise _err("missing_type", f"{loc} 字段 {name} 缺少 type")
        if typ not in _PRIMITIVES:
            raise _err("unknown_type", f"{loc} 字段 {name} 使用未知类型 {typ!r}")

        nullable = bool(schema.get("nullable", False))
        if typ == "null" and not nullable:
            nullable = True

        default: Any = _NO_DEFAULT
        if "default" in schema:
            default = schema["default"]
            if default is None and not nullable:
                raise _err("invalid_default", f"{loc} 字段 {name} 默认值为 null 但未声明 nullable")

        enum_values: tuple[Any, ...] = ()
        if "enum" in schema:
            raw_enum = schema["enum"]
            if not isinstance(raw_enum, list) or not raw_enum:
                raise _err("invalid_enum", f"{loc} 字段 {name} 的 enum 必须是非空数组")
            if len({repr(v) for v in raw_enum}) != len(raw_enum):
                raise _err("duplicate_enum_value", f"{loc} 字段 {name} 的 enum 存在重复值")
            enum_values = tuple(raw_enum)

        item: FieldSchema | None = None
        if typ == "array":
            if "items" not in schema or not isinstance(schema["items"], dict):
                raise _err("missing_items", f"{loc} 字段 {name} 为 array 但缺少 items")
            item = self._parse_schema(f"{name}[]", schema["items"], loc)

        properties: list[FieldSchema] = []
        required_names = schema.get("required", [])
        if required_names is None:
            required_names = []
        if not isinstance(required_names, list) or not all(isinstance(n, str) for n in required_names):
            raise _err("invalid_required", f"{loc} 字段 {name} 的 required 必须是字符串数组")
        required_set = set(required_names)

        if typ == "object":
            raw_props = schema.get("properties", {})
            if not isinstance(raw_props, dict):
                raise _err("invalid_properties", f"{loc} 字段 {name} 的 properties 必须是对象")
            declared = set(raw_props)
            missing = required_set - declared
            if missing:
                raise _err(
                    "required_without_property",
                    f"{loc} 字段 {name} 标记为必填但未声明: {sorted(missing)}",
                )
            for prop_name, prop_schema in raw_props.items():
                if not isinstance(prop_schema, dict):
                    raise _err("invalid_property", f"{loc} 字段 {name}.{prop_name} schema 必须是对象")
                child = self._parse_schema(prop_name, prop_schema, loc)
                properties.append(
                    FieldSchema(
                        name=child.name,
                        type=child.type,
                        required=prop_name in required_set,
                        nullable=child.nullable,
                        default=child.default,
                        enum=child.enum,
                        item=child.item,
                        properties=child.properties,
                    )
                )

        if not isinstance(name, str) or not name:
            raise _err("invalid_field_name", f"{loc} 存在非法字段名")

        return FieldSchema(
            name=name,
            type=typ,
            required=implicit_required,
            nullable=nullable,
            default=default,
            enum=enum_values,
            item=item,
            properties=tuple(properties),
        )
