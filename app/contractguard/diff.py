"""新旧版本契约比对：分级、判定依据、确定性结论。

请求方向（客户端 -> 服务端）与响应方向（服务端 -> 客户端）的
放宽 / 收紧语义相反，因此分别按方向判定。所有输出按
(location, kind) 稳定排序，保证同一输入永远得到同一结论。
"""
from __future__ import annotations

import json

from .models import (
    Change,
    ChangeKind,
    ChangeLevel,
    ContractSnapshot,
    DiffResult,
    EndpointContract,
    FieldSchema,
    _NO_DEFAULT,
)

_REQUEST = "request"
_RESPONSE = "response"

# 各方向下每种属性变化的分级
# (kind, is_breaking_for_request, is_breaking_for_response)
_FIELD_RULES: dict[ChangeKind, tuple[bool, bool, ChangeLevel, ChangeLevel]] = {
    ChangeKind.REQUIRED_TO_OPTIONAL: (False, True, ChangeLevel.COMPATIBLE, ChangeLevel.BREAKING),
    ChangeKind.OPTIONAL_TO_REQUIRED: (True, False, ChangeLevel.BREAKING, ChangeLevel.COMPATIBLE),
    ChangeKind.NULLABLE_TO_NON_NULLABLE: (True, False, ChangeLevel.BREAKING, ChangeLevel.COMPATIBLE),
    ChangeKind.NON_NULLABLE_TO_NULLABLE: (False, True, ChangeLevel.COMPATIBLE, ChangeLevel.BREAKING),
    ChangeKind.TYPE_CHANGED: (True, True, ChangeLevel.BREAKING, ChangeLevel.BREAKING),
    ChangeKind.DEFAULT_VALUE_CHANGED: (False, False, ChangeLevel.COMPATIBLE, ChangeLevel.COMPATIBLE),
    ChangeKind.ENUM_VALUE_ADDED: (False, True, ChangeLevel.COMPATIBLE, ChangeLevel.BREAKING),
    ChangeKind.ENUM_VALUE_REMOVED: (True, False, ChangeLevel.BREAKING, ChangeLevel.COMPATIBLE),
}


def _value_key(value: object) -> str:
    """稳定的值标识：无默认值哨兵也有确定文本。"""
    if value is _NO_DEFAULT:
        return "<no_default>"
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _display(value: object) -> str:
    """面向日志/解释的可读取值。"""
    if value is _NO_DEFAULT:
        return "<no_default>"
    if isinstance(value, str):
        return value
    return _value_key(value)


def _repr(value: object) -> str:
    return _value_key(value)


class ContractDiffer:
    def diff(self, old: ContractSnapshot, new: ContractSnapshot) -> DiffResult:
        changes: list[Change] = []

        old_map = {e.key: e for e in old.endpoints}
        new_map = {e.key: e for e in new.endpoints}

        for key in sorted(old_map.keys() | new_map.keys()):
            method, path = key
            loc = f"{method} {path}"
            if key not in new_map:
                changes.append(
                    Change(
                        level=ChangeLevel.BREAKING,
                        kind=ChangeKind.ENDPOINT_REMOVED,
                        location=loc,
                        detail="接口被移除，既有调用方将无法访问",
                        breaking=True,
                    )
                )
                continue
            if key not in old_map:
                changes.append(
                    Change(
                        level=ChangeLevel.ADDED,
                        kind=ChangeKind.ENDPOINT_ADDED,
                        location=loc,
                        detail="新增接口，不影响既有调用方",
                        breaking=False,
                    )
                )
                continue
            changes.extend(self._diff_endpoint(old_map[key], new_map[key]))

        # 稳定排序：结论与输入字段顺序无关
        changes.sort(key=lambda c: (c.location, c.kind.value, c.detail))
        changes = self._dedupe(changes)

        breaking = [c for c in changes if c.breaking]
        added = [c for c in changes if c.level == ChangeLevel.ADDED]
        compatible_adj = [c for c in changes if c.level == ChangeLevel.COMPATIBLE]
        compatible = not breaking
        if not changes:
            rationale = "两个版本契约内容完全一致，无任何变更"
        else:
            parts = [
                f"新增 {len(added)} 项",
                f"兼容调整 {len(compatible_adj)} 项",
                f"破坏性变更 {len(breaking)} 项",
            ]
            if breaking:
                reasons = "; ".join(f"[{c.kind.value}] {c.location}" for c in breaking)
                parts.append(f"破坏性依据: {reasons}")
            rationale = "；".join(parts)

        return DiffResult(
            old_version=old.version,
            new_version=new.version,
            changes=tuple(changes),
            compatible=compatible,
            rationale=rationale,
        )

    def _dedupe(self, changes: list[Change]) -> list[Change]:
        seen: set[tuple[str, str, str]] = set()
        out: list[Change] = []
        for c in changes:
            sig = (c.location, c.kind.value, c.detail)
            if sig not in seen:
                seen.add(sig)
                out.append(c)
        return out

    def _diff_endpoint(self, old: EndpointContract, new: EndpointContract) -> list[Change]:
        loc = f"{old.method} {old.path}"
        changes: list[Change] = []

        # 请求体
        changes.extend(
            self._diff_fields(
                old.request_body,
                new.request_body,
                f"{loc} request.body",
                _REQUEST,
            )
        )

        # query 参数
        old_q = {p.name: p for p in old.query_params}
        new_q = {p.name: p for p in new.query_params}
        for name in sorted(old_q.keys() | new_q.keys()):
            qloc = f"{loc} request.query[{name}]"
            if name not in new_q:
                changes.append(
                    Change(ChangeLevel.BREAKING, ChangeKind.FIELD_REMOVED, qloc,
                           "query 参数被移除，仍在发送该参数的调用方可能被拒绝", True)
                )
            elif name not in old_q:
                level = (
                    ChangeLevel.BREAKING
                    if new_q[name].required
                    else ChangeLevel.COMPATIBLE
                )
                kind = (
                    ChangeKind.FIELD_ADDED_REQUIRED
                    if new_q[name].required
                    else ChangeKind.FIELD_ADDED_OPTIONAL
                )
                changes.append(
                    Change(level, kind, qloc,
                           "新增必填 query 参数，旧调用方不会提供" if new_q[name].required
                           else "新增可选 query 参数，旧调用方不受影响",
                           new_q[name].required)
                )
            else:
                changes.extend(self._diff_attrs(old_q[name], new_q[name], qloc, _REQUEST))

        # 响应
        old_r = {s: sch for s, sch in old.responses}
        new_r = {s: sch for s, sch in new.responses}
        for status in sorted(old_r.keys() | new_r.keys()):
            rloc = f"{loc} response[{status}]"
            if status not in new_r:
                changes.append(Change(
                    ChangeLevel.BREAKING, ChangeKind.RESPONSE_STATUS_REMOVED, rloc,
                    f"响应状态码 {status} 被移除", True))
            elif status not in old_r:
                changes.append(Change(
                    ChangeLevel.ADDED, ChangeKind.RESPONSE_STATUS_ADDED, rloc,
                    f"新增响应状态码 {status}", False))
            else:
                changes.extend(self._diff_fields(old_r[status], new_r[status], rloc, _RESPONSE))

        return changes

    def _diff_fields(
        self,
        old: FieldSchema | None,
        new: FieldSchema | None,
        location: str,
        direction: str,
    ) -> list[Change]:
        # 容器（请求体 / 响应体）整体出现或消失
        if old is None and new is None:
            return []
        if old is None and new is not None:
            if direction == _REQUEST:
                return [Change(
                    ChangeLevel.BREAKING, ChangeKind.FIELD_ADDED_REQUIRED, location,
                    "新增请求体要求，旧调用方不会发送", True)]
            return [Change(
                ChangeLevel.ADDED, ChangeKind.FIELD_ADDED_OPTIONAL, location,
                "新增响应体字段，消费者可忽略", False)]
        if old is not None and new is None:
            if direction == _REQUEST:
                return [Change(
                    ChangeLevel.COMPATIBLE, ChangeKind.FIELD_REMOVED, location,
                    "请求体被移除，旧调用方发送的内容将被忽略", False)]
            return [Change(
                ChangeLevel.BREAKING, ChangeKind.FIELD_REMOVED, location,
                "响应体被移除，依赖该内容的消费者将失败", True)]

        assert old is not None and new is not None
        changes = self._diff_attrs(old, new, location, direction)

        # 同类型对象：递归比较子属性
        if old.type == "object" and new.type == "object":
            old_p = {p.name: p for p in old.properties}
            new_p = {p.name: p for p in new.properties}
            for name in sorted(old_p.keys() | new_p.keys()):
                ploc = f"{location}.{name}"
                if name in old_p and name in new_p:
                    changes.extend(self._diff_fields(old_p[name], new_p[name], ploc, direction))
                elif name in new_p:
                    changes.append(self._field_added(new_p[name], ploc, direction))
                else:
                    changes.append(
                        Change(ChangeLevel.BREAKING, ChangeKind.FIELD_REMOVED, ploc,
                               f"字段 {name} 被移除，仍使用该字段的一方受影响", True))
        # 同类型数组：递归比较元素
        if old.type == "array" and new.type == "array" and old.item and new.item:
            changes.extend(self._diff_fields(old.item, new.item, f"{location}[]", direction))
        return changes

    def _field_added(self, field: FieldSchema, location: str, direction: str) -> Change:
        if direction == _REQUEST:
            if field.required:
                return Change(ChangeLevel.BREAKING, ChangeKind.FIELD_ADDED_REQUIRED, location,
                              "新增必填字段，旧调用方不会提供", True)
            return Change(ChangeLevel.COMPATIBLE, ChangeKind.FIELD_ADDED_OPTIONAL, location,
                          "新增可选字段，旧调用方不受影响", False)
        return Change(ChangeLevel.ADDED, ChangeKind.FIELD_ADDED_OPTIONAL, location,
                      "响应新增字段，消费方可忽略未知字段", False)

    def _diff_attrs(
        self, old: FieldSchema, new: FieldSchema, location: str, direction: str
    ) -> list[Change]:
        changes: list[Change] = []

        def emit(kind: ChangeKind, detail: str) -> None:
            breaks_req, breaks_resp, req_level, resp_level = _FIELD_RULES[kind]
            if direction == _REQUEST:
                breaking, level = breaks_req, req_level
            else:
                breaking, level = breaks_resp, resp_level
            changes.append(Change(level, kind, location, detail, breaking))

        if old.required != new.required:
            if old.required and not new.required:
                emit(ChangeKind.REQUIRED_TO_OPTIONAL,
                     f"必填性变化: 必填 -> 可选 (required {old.required} -> {new.required})")
            else:
                emit(ChangeKind.OPTIONAL_TO_REQUIRED,
                     f"必填性变化: 可选 -> 必填 (required {old.required} -> {new.required})")

        if old.nullable != new.nullable:
            if old.nullable and not new.nullable:
                emit(ChangeKind.NULLABLE_TO_NON_NULLABLE,
                     f"可空性收紧: nullable -> non-nullable")
            else:
                emit(ChangeKind.NON_NULLABLE_TO_NULLABLE,
                     f"可空性放宽: non-nullable -> nullable")

        if old.type != new.type:
            emit(ChangeKind.TYPE_CHANGED, f"类型变化: {old.type} -> {new.type}")

        if _repr(old.default) != _repr(new.default):
            emit(ChangeKind.DEFAULT_VALUE_CHANGED,
                 f"默认值变化: {_display(old.default)} -> {_display(new.default)}")

        old_enum = {_value_key(v): v for v in old.enum}
        new_enum = {_value_key(v): v for v in new.enum}
        if old_enum or new_enum:
            added_keys = sorted(
                _display(v) for v in new_enum.values() if _value_key(v) not in old_enum
            )
            removed_keys = sorted(
                _display(v) for v in old_enum.values() if _value_key(v) not in new_enum
            )
            if added_keys:
                emit(ChangeKind.ENUM_VALUE_ADDED, f"枚举新增取值: {added_keys}")
            if removed_keys:
                emit(ChangeKind.ENUM_VALUE_REMOVED, f"枚举移除取值: {removed_keys}")

        return changes
