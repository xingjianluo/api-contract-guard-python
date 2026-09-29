"""发布门禁：结合消费者期望对差异报告做放行判定。

结论分三档：

* ``pass``   没有任何破坏性变更；
* ``warn``   存在破坏性变更，但不影响任何已登记消费者（连调用都没有），
             原因码 ``breaking_unused``；
* ``reject`` 存在影响已登记消费者的破坏性变更。

每条破坏性变更的 finding 原因码三选一、可区分：

* ``breaking_consumer_affected``
      命中某消费者声明依赖的**具体内容**：旧格式消费者登记的整个操作、
      新格式消费者依赖的某种响应/某些字段，或请求侧（请求方一定会发送/
      要求的约束）变化；
* ``breaking_operation_only``
      破坏所在操作有消费者调用，但没有任何消费者声明依赖被命中的内容；
* ``breaking_unused``
      所在操作没有任何消费者登记使用。

消费者登记两种格式：

* 老格式（只给 operations）：行为与历史版本完全一致——操作级命中即 affected；
* 新格式（带 dependencies: operation+response+fields）：只有真正依赖的
  响应/字段被破坏才算 affected；未被依赖的部分破坏只记 operation_only。

findings 按 (reason_code, path, change.kind) 排序，finding 内消费者按名排序，
结论可复现，与消费者数量、声明条数、登记先后无关。
"""

from __future__ import annotations

from .models import (
    Change,
    ConsumerDependency,
    ConsumerExpectation,
    DiffReport,
    GateFinding,
    GateReport,
    GateVerdict,
    Severity,
)
from .storage import LocalStore


def operation_key_of(change_path: str) -> str:
    """从变更路径提取操作键，例如 ``"GET /users#response-200.id"`` -> ``"GET /users"``。"""
    return change_path.split("#", 1)[0]


def _section_of(change_path: str) -> str:
    """提取 ``#`` 后的段名，如 ``response-404.id`` -> ``response-404``。"""
    if "#" not in change_path:
        return ""
    section = change_path.split("#", 1)[1]
    return section.split(".", 1)[0]


def _field_path_of(change_path: str) -> str | None:
    """``...#response-200.address.city`` -> ``address.city``；非字段级变更为 None。"""
    if "#" not in change_path:
        return None
    section = change_path.split("#", 1)[1]
    if "." not in section:
        return None
    return section.split(".", 1)[1]


class ReleaseGate:
    """消费 DiffReport + 已登记消费者，产出可解释的 GateReport。"""

    def __init__(self, store: LocalStore) -> None:
        self.store = store

    def evaluate(self, diff: DiffReport) -> GateReport:
        consumers = self.store.list_consumers(diff.service)
        findings: list[GateFinding] = []

        breaking_changes = [c for c in diff.changes if c.severity is Severity.BREAKING]
        for change in breaking_changes:
            op = operation_key_of(change.path)
            op_consumers = [c for c in consumers if op in c.operations]
            affected_names: list[str] = []
            operation_only_names: list[str] = []

            for consumer in op_consumers:
                if self._dependency_hit(consumer, change):
                    affected_names.append(consumer.consumer)
                else:
                    operation_only_names.append(consumer.consumer)

            if affected_names:
                affected_names.sort()
                operation_only_names.sort()
                reason = "breaking_consumer_affected"
                msg = (
                    f"破坏性变更命中 {len(affected_names)} 个消费者声明依赖的内容"
                    f"({', '.join(affected_names)})"
                )
                finding_consumers = tuple(affected_names)
                affected = True
            elif operation_only_names:
                operation_only_names.sort()
                reason = "breaking_operation_only"
                msg = (
                    f"操作 {op} 有 {len(operation_only_names)} 个消费者调用"
                    f"({', '.join(operation_only_names)})，但其声明依赖均未命中该变更内容"
                )
                finding_consumers = tuple(operation_only_names)
                affected = False
            else:
                reason = "breaking_unused"
                msg = f"破坏性变更未被任何已登记消费者使用；操作 {op} 无消费者登记"
                finding_consumers = ()
                affected = False

            findings.append(
                GateFinding(
                    affected=affected,
                    change=change,
                    operations=(op,),
                    reason_code=reason,
                    message=msg,
                    consumers=finding_consumers,
                )
            )

        findings.sort(key=lambda f: (f.reason_code, f.change.path, f.change.kind))

        if not breaking_changes:
            verdict = GateVerdict.PASS
        elif any(f.affected for f in findings):
            verdict = GateVerdict.REJECT
        else:
            verdict = GateVerdict.WARN

        return GateReport(
            service=diff.service,
            version=diff.new_version,
            verdict=verdict,
            findings=tuple(findings),
        )

    # ------------------------------------------------------------------ #
    # 依赖命中判定
    # ------------------------------------------------------------------ #

    def _dependency_hit(self, consumer: ConsumerExpectation, change: Change) -> bool:
        """单条破坏性变更是否命中某消费者真正依赖的内容。"""
        op = operation_key_of(change.path)

        # 老格式消费者：没有字段级声明，操作级命中即视为受影响（行为不变）。
        if not consumer.dependencies:
            return True

        # 非响应段（端点删除 / 请求参数 / 请求体）：调用该操作的请求方必然受影响。
        section = _section_of(change.path)
        if not section.startswith("response-"):
            return any(d.operation == op for d in consumer.dependencies)

        status = section[len("response-"):]
        deps = [
            d for d in consumer.dependencies
            if d.operation == op and d.response == status
        ]
        if not deps:
            return False

        field_path = _field_path_of(change.path)
        # 整个响应被删 / 响应体被移除（无字段路径）：依赖该响应结果即命中。
        if field_path is None:
            return True
        # 字段级变更：消费者依赖该字段或其上层/下层对象才算命中。
        return any(self._field_dependency_hit(dep, field_path) for dep in deps)

    @staticmethod
    def _field_dependency_hit(dep: ConsumerDependency, changed_field: str) -> bool:
        if not dep.fields:
            # 声明依赖某种响应但未细化字段 -> 依赖整个响应
            return True
        for declared in dep.fields:
            if declared == changed_field:
                return True
            if changed_field.startswith(declared + "."):
                # 被依赖的父对象内部发生变化
                return True
            if declared.startswith(changed_field + "."):
                # 被依赖的子字段随上层对象一起变化
                return True
        return False


__all__ = ["ReleaseGate", "operation_key_of"]
