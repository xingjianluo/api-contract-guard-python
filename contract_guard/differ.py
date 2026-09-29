"""两个版本快照的确定性比对。

兼容性分级（方向相关）：

请求侧（客户端 -> 服务端）
    请求可选字段变必填          BREAKING  request_required_added/request_optional_to_required
    请求新增必填字段            BREAKING  request_required_added
    请求新增可选字段            COMPATIBLE request_optional_added（老客户端不传也成立）
    请求删除字段                COMPATIBLE request_field_removed（老客户端继续发，通常被忽略）
    必填变可选                  COMPATIBLE request_required_to_optional
    类型收窄/改变               BREAKING  type_changed
    枚举移除成员                BREAKING  enum_value_removed（旧值不再被接受）
    枚举新增成员                COMPATIBLE enum_value_added
    可空变不可空                BREAKING  nullable_to_non_nullable（null 不再被接受）
    不可空变可空                COMPATIBLE non_nullable_to_nullable
    移除默认值                  BREAKING  default_removed
    新增默认值/默认值改变        COMPATIBLE default_added/default_changed（对既有请求无强制）

响应侧（服务端 -> 客户端）
    新增字段                    COMPATIBLE response_field_added（老客户端忽略未知字段）
    删除字段                    BREAKING  response_field_removed
    类型改变                    BREAKING  type_changed
    枚举移除成员                COMPATIBLE enum_value_removed（老客户端仍认得旧成员）
    枚举新增成员                BREAKING  enum_value_added（旧客户端收到未知值）
    可空变不可空                COMPATIBLE nullable_to_non_nullable
    不可空变可空                BREAKING  non_nullable_to_nullable（null 可能到达旧客户端）
    默认值变化                  COMPATIBLE default_changed/added/removed（响应默认值不约束消费者）

端点级
    新增端点                    ADDED     endpoint_added
    删除端点                    BREAKING  endpoint_removed

请求体（整个 body 的必填性是独立语义，与字段必填分开判定）
    无 body -> 可选 body        COMPATIBLE request_body_added_optional
    无 body -> 必填 body        BREAKING   request_body_added_required
    body 可选 -> 必填           BREAKING   request_body_became_required
    body 必填 -> 可选 / 整体移除 COMPATIBLE request_body_became_optional / request_body_removed

响应结果（同一操作的每种状态码分别建模：200/400/404/default…）
    新增一种响应结果            ADDED     response_added（以前没有过的结果，不算破坏）
    删除一种已声明响应          BREAKING  response_removed
    响应内删字段                BREAKING  response_field_removed
    响应内字段类型等变化        BREAKING  按字段级 reason_code（type_changed 等）

所有变更按固定规则排序后输出，同一对版本重复比对（含从不同入口触发、
无论各响应声明的顺序如何）结论完全一致。
"""

from __future__ import annotations

from .models import (
    Change,
    ContractSnapshot,
    DiffReport,
    Direction,
    EndpointModel,
    FieldModel,
    ResponseModel,
    Severity,
)

_SEVERITY_ORDER = {Severity.BREAKING: 0, Severity.COMPATIBLE: 1, Severity.ADDED: 2}


class SnapshotDiffer:
    """快照比对器（纯函数、无副作用）。"""

    def diff(self, old: ContractSnapshot, new: ContractSnapshot) -> DiffReport:
        if old.service != new.service:
            raise ValueError(f"service_mismatch:{old.service}!={new.service}")

        old_map = {(e.method, e.path): e for e in old.endpoints}
        new_map = {(e.method, e.path): e for e in new.endpoints}
        changes: list[Change] = []

        for key in sorted(old_map.keys() | new_map.keys()):
            old_ep = old_map.get(key)
            new_ep = new_map.get(key)
            changes.extend(self._diff_endpoint(old_ep, new_ep))

        changes.sort(key=lambda c: (_SEVERITY_ORDER[c.severity], c.path, c.kind))
        return DiffReport(
            service=old.service,
            old_version=old.version,
            new_version=new.version,
            changes=tuple(changes),
            old_fingerprint=old.fingerprint,
            new_fingerprint=new.fingerprint,
        )

    # ------------------------------------------------------------------ #
    # 端点级
    # ------------------------------------------------------------------ #

    def _diff_endpoint(
        self,
        old: EndpointModel | None,
        new: EndpointModel | None,
    ) -> list[Change]:
        if old is None and new is not None:
            return [
                Change(
                    severity=Severity.ADDED,
                    path=f"{new.method} {new.path}",
                    kind="endpoint_added",
                    reason_code="endpoint_added",
                    message="新增端点，对既有消费者无破坏",
                )
            ]
        if new is None and old is not None:
            return [
                Change(
                    severity=Severity.BREAKING,
                    path=f"{old.method} {old.path}",
                    kind="endpoint_removed",
                    reason_code="endpoint_removed",
                    message="端点被删除，调用方将无法访问",
                )
            ]
        assert old is not None and new is not None
        prefix = f"{new.method} {new.path}"
        changes: list[Change] = []

        if not old.deprecated and new.deprecated:
            changes.append(
                Change(
                    severity=Severity.COMPATIBLE,
                    path=prefix,
                    kind="endpoint_deprecated",
                    reason_code="endpoint_deprecated",
                    message="端点被标记为废弃（暂时仍可用）",
                )
            )

        changes.extend(
            self._diff_fields(old.request_params, new.request_params, f"{prefix}#request-param")
        )
        changes.extend(self._diff_request_body(old, new, prefix))
        changes.extend(self._diff_responses(old, new, prefix))
        return changes

    # ------------------------------------------------------------------ #
    # 请求体（整体必填性）
    # ------------------------------------------------------------------ #

    def _diff_request_body(
        self, old: EndpointModel, new: EndpointModel, prefix: str
    ) -> list[Change]:
        changes: list[Change] = []

        # body 同时存在：先比字段，再比整体必填性
        if old.request_body and new.request_body:
            changes.extend(
                self._diff_fields(old.request_body, new.request_body, f"{prefix}#request-body")
            )
            if not old.request_body_required and new.request_body_required:
                changes.append(
                    Change(
                        severity=Severity.BREAKING,
                        path=prefix,
                        kind="request_body_required_changed",
                        reason_code="request_body_became_required",
                        message="请求体由可选变为必填，不带请求体的旧客户端会被拒绝",
                        old=False,
                        new=True,
                    )
                )
            elif old.request_body_required and not new.request_body_required:
                changes.append(
                    Change(
                        severity=Severity.COMPATIBLE,
                        path=prefix,
                        kind="request_body_required_changed",
                        reason_code="request_body_became_optional",
                        message="请求体由必填放宽为可选",
                        old=True,
                        new=False,
                    )
                )
            return changes

        # 一侧有 body、一侧没有：字段集合整体新增/移除只报一条整体级变更，
        # 不再逐字段重复计数。
        if not old.request_body and new.request_body:
            if new.request_body_required:
                changes.append(
                    Change(
                        severity=Severity.BREAKING,
                        path=prefix,
                        kind="request_body_added",
                        reason_code="request_body_added_required",
                        message="新增必填请求体，不带请求体的旧客户端会被拒绝",
                        old=None,
                        new="required",
                    )
                )
            else:
                changes.append(
                    Change(
                        severity=Severity.COMPATIBLE,
                        path=prefix,
                        kind="request_body_added",
                        reason_code="request_body_added_optional",
                        message="新增可选请求体，旧客户端继续不带请求体仍可调用",
                        old=None,
                        new="optional",
                    )
                )
        elif old.request_body and not new.request_body:
            changes.append(
                Change(
                    severity=Severity.COMPATIBLE,
                    path=prefix,
                    kind="request_body_removed",
                    reason_code="request_body_removed",
                    message="服务端不再要求请求体，对旧客户端兼容",
                    old="required" if old.request_body_required else "optional",
                    new=None,
                )
            )
        return changes

    # ------------------------------------------------------------------ #
    # 多种响应结果（按状态码）
    # ------------------------------------------------------------------ #

    def _diff_responses(
        self, old: EndpointModel, new: EndpointModel, prefix: str
    ) -> list[Change]:
        old_map = {r.status: r for r in old.responses}
        new_map = {r.status: r for r in new.responses}
        changes: list[Change] = []

        for status in sorted(old_map.keys() | new_map.keys(), key=self._status_sort_key):
            old_resp = old_map.get(status)
            new_resp = new_map.get(status)
            if old_resp is None and new_resp is not None:
                changes.append(
                    Change(
                        severity=Severity.ADDED,
                        path=f"{prefix}#response-{status}",
                        kind="response_added",
                        reason_code="response_added",
                        message=f"新增响应结果 {status}，旧消费者不会收到该结果，不构成破坏",
                        old=None,
                        new=status,
                    )
                )
            elif new_resp is None and old_resp is not None:
                changes.append(
                    Change(
                        severity=Severity.BREAKING,
                        path=f"{prefix}#response-{status}",
                        kind="response_removed",
                        reason_code="response_removed",
                        message=f"已声明的响应结果 {status} 被删除，处理该结果的旧消费者分支失效",
                        old=status,
                        new=None,
                    )
                )
            else:
                assert old_resp is not None and new_resp is not None
                changes.extend(self._diff_response_body(old_resp, new_resp, prefix, status))
        return changes

    @staticmethod
    def _status_sort_key(status: str) -> tuple[int, str]:
        return (1, "") if status == "default" else (0, status)

    def _diff_response_body(
        self,
        old_resp: ResponseModel,
        new_resp: ResponseModel,
        prefix: str,
        status: str,
    ) -> list[Change]:
        section = f"{prefix}#response-{status}"
        changes = self._diff_fields(old_resp.fields, new_resp.fields, section)
        if old_resp.fields and not new_resp.fields:
            changes.append(
                Change(
                    severity=Severity.BREAKING,
                    path=section,
                    kind="response_body_removed",
                    reason_code="response_body_removed",
                    message=f"响应结果 {status} 的响应体被移除，依赖其字段的消费者会失败",
                    old="body",
                    new=None,
                )
            )
        return changes

    # ------------------------------------------------------------------ #
    # 字段级
    # ------------------------------------------------------------------ #

    def _diff_fields(
        self,
        old_fields: tuple[FieldModel, ...],
        new_fields: tuple[FieldModel, ...],
        prefix: str,
    ) -> list[Change]:
        old_map = {f.name: f for f in old_fields}
        new_map = {f.name: f for f in new_fields}
        changes: list[Change] = []

        for name in sorted(old_map.keys() | new_map.keys()):
            path = f"{prefix}.{name}"
            old_f = old_map.get(name)
            new_f = new_map.get(name)
            if old_f is None and new_f is not None:
                changes.append(self._classify_added_field(new_f, path))
                changes.extend(self._nested_additions(new_f, path))
            elif new_f is None and old_f is not None:
                changes.append(self._classify_removed_field(old_f, path))
            else:
                assert old_f is not None and new_f is not None
                changes.extend(self._classify_field_change(old_f, new_f, path))
        return changes

    def _nested_additions(self, field: FieldModel, path: str) -> list[Change]:
        out: list[Change] = []
        for child in field.children:
            child_path = f"{path}.{child.name}"
            out.append(self._classify_added_field(child, child_path))
            out.extend(self._nested_additions(child, child_path))
        return out

    def _classify_added_field(self, field: FieldModel, path: str) -> Change:
        if field.direction is Direction.RESPONSE:
            return Change(
                severity=Severity.COMPATIBLE,
                path=path,
                kind="field_added",
                reason_code="response_field_added",
                message="响应新增字段，旧客户端忽略未知字段即可",
                old=None,
                new=field.type_name,
            )
        if field.required:
            return Change(
                severity=Severity.BREAKING,
                path=path,
                kind="field_added",
                reason_code="request_required_added",
                message="请求新增必填字段，未提供该字段的旧客户端会被拒绝",
                old=None,
                new=field.type_name,
            )
        return Change(
            severity=Severity.COMPATIBLE,
            path=path,
            kind="field_added",
            reason_code="request_optional_added",
            message="请求新增可选字段，旧客户端不传仍可正常调用",
            old=None,
            new=field.type_name,
        )

    def _classify_removed_field(self, field: FieldModel, path: str) -> Change:
        if field.direction is Direction.RESPONSE:
            return Change(
                severity=Severity.BREAKING,
                path=path,
                kind="field_removed",
                reason_code="response_field_removed",
                message="响应删除字段，依赖该字段的旧客户端会失败",
                old=field.type_name,
                new=None,
            )
        return Change(
            severity=Severity.COMPATIBLE,
            path=path,
            kind="field_removed",
            reason_code="request_field_removed",
            message="请求字段被删除，旧客户端继续发送通常被忽略",
            old=field.type_name,
            new=None,
        )

    def _classify_field_change(
        self, old: FieldModel, new: FieldModel, path: str
    ) -> list[Change]:
        if old.direction is not new.direction:
            raise ValueError(f"direction_mismatch:{path}")
        changes: list[Change] = []

        # 类型 / 数组元素类型
        if old.type_name != new.type_name or old.item_type != new.item_type:
            changes.append(
                Change(
                    severity=Severity.BREAKING,
                    path=path,
                    kind="type_changed",
                    reason_code="type_changed",
                    message=(
                        f"字段类型改变: {self._type_label(old)} -> {self._type_label(new)}，"
                        "收发双方编解码不再兼容"
                    ),
                    old=self._type_label(old),
                    new=self._type_label(new),
                )
            )

        # 必填性
        if not old.required and new.required:
            changes.append(
                Change(
                    severity=Severity.BREAKING if new.direction is Direction.REQUEST
                    else Severity.COMPATIBLE,
                    path=path,
                    kind="required_changed",
                    reason_code=(
                        "request_optional_to_required"
                        if new.direction is Direction.REQUEST
                        else "response_optional_to_required"
                    ),
                    message="字段由可选变为必填"
                    + ("，旧客户端的请求会被拒绝" if new.direction is Direction.REQUEST
                       else "（响应字段必填性对客户端无强制）"),
                    old=False,
                    new=True,
                )
            )
        elif old.required and not new.required:
            changes.append(
                Change(
                    severity=Severity.COMPATIBLE,
                    path=path,
                    kind="required_changed",
                    reason_code="required_to_optional",
                    message="字段由必填变为可选，放宽约束",
                    old=True,
                    new=False,
                )
            )

        # 可空性
        if old.nullable != new.nullable:
            if new.direction is Direction.REQUEST:
                breaking = old.nullable and not new.nullable
                reason = "nullable_to_non_nullable" if breaking else "non_nullable_to_nullable"
                msg = ("请求不再接受 null，发送 null 的旧客户端会被拒绝" if breaking
                       else "请求开始接受 null，约束放宽")
            else:
                breaking = not old.nullable and new.nullable
                reason = "non_nullable_to_nullable" if breaking else "nullable_to_non_nullable"
                msg = ("响应可能下发 null，未做 null 处理的旧客户端会失败" if breaking
                       else "响应不再下发 null，约束收紧")
            changes.append(
                Change(
                    severity=Severity.BREAKING if breaking else Severity.COMPATIBLE,
                    path=path,
                    kind="nullable_changed",
                    reason_code=reason,
                    message=msg,
                    old=old.nullable,
                    new=new.nullable,
                )
            )

        # 默认值
        changes.extend(self._diff_default(old, new, path))

        # 枚举
        changes.extend(self._diff_enum(old, new, path))

        # 废弃标记（不构成兼容性事件，只在语义变化时说明）
        # 嵌套子字段递归
        changes.extend(self._diff_fields(old.children, new.children, path))
        return changes

    def _diff_default(self, old: FieldModel, new: FieldModel, path: str) -> list[Change]:
        if old.has_default == new.has_default and old.default == new.default:
            return []
        if not old.has_default and new.has_default:
            kind, reason, breaking, msg = (
                "default_added",
                "default_added",
                False,
                "新增默认值",
            )
        elif old.has_default and not new.has_default:
            # 请求侧移除默认值：旧客户端若依赖省略该字段，行为可能改变 -> 破坏
            breaking = old.direction is Direction.REQUEST
            kind, reason = "default_removed", "default_removed"
            msg = ("请求字段默认值被移除，省略该字段的旧客户端行为不再有保证"
                   if breaking else "响应字段默认值被移除")
        else:
            kind, reason, breaking, msg = (
                "default_changed",
                "default_changed",
                False,
                "默认值发生改变",
            )
        return [
            Change(
                severity=Severity.BREAKING if breaking else Severity.COMPATIBLE,
                path=path,
                kind=kind,
                reason_code=reason,
                message=msg,
                old=old.default if old.has_default else "<undeclared>",
                new=new.default if new.has_default else "<undeclared>",
            )
        ]

    def _diff_enum(self, old: FieldModel, new: FieldModel, path: str) -> list[Change]:
        if not old.enum and not new.enum:
            return []
        old_values = set(old.enum)
        new_values = set(new.enum)
        removed = old_values - new_values
        added = new_values - old_values
        changes: list[Change] = []

        def _enum_change(values: set, is_add: bool) -> Change:
            if new.direction is Direction.REQUEST:
                # 请求：移除可发送值=破坏；新增可发送值=兼容
                breaking = not is_add
            else:
                # 响应：新增可能收到的值=破坏；移除=兼容
                breaking = is_add
            reason = (
                ("enum_value_added" if is_add else "enum_value_removed")
            )
            msg = (
                "请求枚举新增可接受值，旧客户端不会发送新值"
                if new.direction is Direction.REQUEST and is_add
                else "请求枚举移除可接受值，发送旧值的客户端会被拒绝"
                if new.direction is Direction.REQUEST
                else "响应枚举新增取值，旧客户端可能无法识别"
                if is_add
                else "响应枚举不再下发部分取值，旧客户端仍认得保留值"
            )
            return Change(
                severity=Severity.BREAKING if breaking else Severity.COMPATIBLE,
                path=path,
                kind="enum_changed",
                reason_code=reason,
                message=msg,
                old=sorted(old_values, key=repr) if old.enum else None,
                new=sorted(new_values, key=repr) if new.enum else None,
            )

        if removed:
            changes.append(_enum_change(removed, is_add=False))
        if added:
            changes.append(_enum_change(added, is_add=True))
        return changes

    @staticmethod
    def _type_label(field: FieldModel) -> str:
        return f"array<{field.item_type}>" if field.type_name == "array" else field.type_name


__all__ = ["SnapshotDiffer"]
