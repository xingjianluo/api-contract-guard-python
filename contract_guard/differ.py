"""两个版本快照的确定性比对。

兼容性分级（方向相关）：

请求侧（客户端 -> 服务端）
    请求可选字段变必填          BREAKING  request_optional_to_required
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

请求体整体必填性（与“单个字段必填”明确区分，原因码不同）
    无请求体/可选 -> 必须带      BREAKING  request_body_required_added
    必须带 -> 可选/可以不带      COMPATIBLE request_body_required_relaxed

响应侧（服务端 -> 客户端），按操作声明的*每一种*响应结果（状态码）分别比对
    新增一种响应结果            ADDED     response_added（老消费者不依赖它，无破坏）
    删除一种已声明的响应结果     BREAKING  response_removed（依赖该结果的消费者会失败）
    某响应体从无到有            COMPATIBLE response_body_added
    某响应体整体移除            BREAKING  response_body_removed
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

所有变更按固定规则排序后输出，同一对版本重复比对（含从不同入口触发、
响应声明顺序重排）结论完全一致。
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
    response_sort_key,
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
        changes.extend(
            self._diff_request_body(old, new, prefix)
        )
        changes.extend(
            self._diff_responses(old.responses, new.responses, prefix)
        )
        return changes

    # ------------------------------------------------------------------ #
    # 请求体（整体必填性与字段级变化分开）
    # ------------------------------------------------------------------ #

    def _diff_request_body(
        self, old: EndpointModel, new: EndpointModel, prefix: str
    ) -> list[Change]:
        changes: list[Change] = []

        # 整个请求体必填性：原因码独立于“单个字段必填”（request_optional_to_required）
        if not old.request_body_required and new.request_body_required:
            changes.append(
                Change(
                    severity=Severity.BREAKING,
                    path=prefix,
                    kind="request_body_required_added",
                    reason_code="request_body_required_added",
                    message=(
                        "请求体由可以不带变为必须携带：不带请求体的旧客户端会被拒绝"
                        if old.request_body
                        else "新增了必须携带的请求体：不带请求体的旧客户端会被拒绝"
                    ),
                    old=old.request_body_required,
                    new=True,
                )
            )
        elif old.request_body_required and not new.request_body_required and new.request_body:
            changes.append(
                Change(
                    severity=Severity.COMPATIBLE,
                    path=prefix,
                    kind="request_body_required_relaxed",
                    reason_code="request_body_required_relaxed",
                    message="请求体由必须携带放宽为可以不带，旧客户端继续携带不受影响",
                    old=True,
                    new=False,
                )
            )

        if old.request_body and not new.request_body:
            changes.append(
                Change(
                    severity=Severity.COMPATIBLE,
                    path=prefix,
                    kind="request_body_removed",
                    reason_code="request_body_removed",
                    message="服务端不再要求请求体，对旧客户端兼容",
                )
            )
            return changes

        if not old.request_body and new.request_body:
            if new.request_body_required:
                # 新增必填请求体的破坏已由整体级 request_body_required_added 表达，
                # 不再重复逐字段计数。
                return changes
            # 新增可选请求体：字段级新增按既有规则给出（均为兼容）
            changes.extend(
                self._diff_fields((), new.request_body, f"{prefix}#request-body")
            )
            return changes

        # 两侧都有请求体：字段级照常比对
        changes.extend(
            self._diff_fields(old.request_body, new.request_body, f"{prefix}#request-body")
        )
        return changes

    # ------------------------------------------------------------------ #
    # 多种响应结果
    # ------------------------------------------------------------------ #

    def _diff_responses(
        self,
        old_responses: tuple[ResponseModel, ...],
        new_responses: tuple[ResponseModel, ...],
        prefix: str,
    ) -> list[Change]:
        old_map = {r.status_code: r for r in old_responses}
        new_map = {r.status_code: r for r in new_responses}
        changes: list[Change] = []

        for code in sorted(old_map.keys() | new_map.keys(), key=response_sort_key):
            old_r = old_map.get(code)
            new_r = new_map.get(code)
            section = f"{prefix}#response-{code}"
            if old_r is None and new_r is not None:
                changes.append(
                    Change(
                        severity=Severity.ADDED,
                        path=section,
                        kind="response_added",
                        reason_code="response_added",
                        message=f"新增响应结果 {code}，既有消费者不依赖该结果，无破坏",
                        old=None,
                        new=code,
                    )
                )
                # 新响应内部字段也逐条登记为新增（纯新增，不破坏）
                changes.extend(self._nested_additions_root(new_r, section))
            elif new_r is None and old_r is not None:
                changes.append(
                    Change(
                        severity=Severity.BREAKING,
                        path=section,
                        kind="response_removed",
                        reason_code="response_removed",
                        message=(
                            f"已声明的响应结果 {code} 被删除：依赖该结果的消费者将无法"
                            "再按契约处理它"
                        ),
                        old=code,
                        new=None,
                    )
                )
            else:
                assert old_r is not None and new_r is not None
                changes.extend(self._diff_one_response(old_r, new_r, section))
        return changes

    def _nested_additions_root(
        self, response: ResponseModel, section: str
    ) -> list[Change]:
        out: list[Change] = []
        for field_ in response.fields:
            path = f"{section}.{field_.name}"
            out.append(self._classify_added_field(field_, path))
            out.extend(self._nested_additions(field_, path))
        return out

    def _diff_one_response(
        self, old: ResponseModel, new: ResponseModel, section: str
    ) -> list[Change]:
        changes: list[Change] = []
        if old.fields and not new.fields:
            changes.append(
                Change(
                    severity=Severity.BREAKING,
                    path=section,
                    kind="response_body_removed",
                    reason_code="response_body_removed",
                    message=(
                        f"响应 {new.status_code} 的响应体被移除，依赖其字段的消费者会失败"
                    ),
                )
            )
            return changes
        if not old.fields and new.fields:
            changes.append(
                Change(
                    severity=Severity.COMPATIBLE,
                    path=section,
                    kind="response_body_added",
                    reason_code="response_body_added",
                    message=f"响应 {new.status_code} 新增响应体，旧客户端忽略未知内容即可",
                )
            )
            changes.extend(self._nested_additions_root(new, section))
            return changes
        # 两侧都有 body（或都没有）：字段级比对
        changes.extend(self._diff_fields(old.fields, new.fields, section))
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
