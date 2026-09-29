"""发布门禁：结合消费者期望对差异报告做放行判定。

结论分三档：

* ``pass``   没有任何破坏性变更；
* ``warn``   存在破坏性变更，但没有一条真正命中任何消费者声明依赖的内容；
* ``reject`` 至少一条破坏性变更命中了消费者声明依赖的内容。

消费者登记有两种粒度（可并存）：

* 操作级（老方式）：登记 ``operations``，视为依赖该操作的全部内容，
  该操作上任何破坏都命中它（行为与历史完全一致）；
* 依赖级：登记 ``dependencies``（操作 + 响应状态码 + 字段路径集合），
  只有真正命中其所声明响应/字段的响应侧破坏才算命中；请求侧破坏
  （调用方发不出合法请求）也会影响所有调用该操作的消费者。

每条破坏对应一个 finding，原因码三选一、互相可区分：

* ``breaking_consumer_dependency_hit``   命中了某消费者声明依赖的内容（reject）；
* ``breaking_operation_used_not_depended`` 破坏所在操作被消费者调用，
  但没有命中它声明依赖的内容（warn 档明细）；
* ``breaking_unused``                    破坏所在操作没有任何消费者登记（warn）。

判定是确定性的：findings 按 (reason_code, path, kind) 排序，
affected_consumers 按名排序；消费者数量、声明条数与登记先后不影响结论。
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

# 兼容老调用方的原因码别名（历史门禁结论码保持不变）
REASON_DEPENDENCY_HIT = "breaking_consumer_dependency_hit"
REASON_OPERATION_USED_NOT_DEPENDED = "breaking_operation_used_not_depended"
REASON_UNUSED = "breaking_unused"
# 操作级登记（老消费者）命中时沿用历史原因码，保证行为不变
REASON_OPERATION_LEVEL_HIT = "breaking_consumer_affected"


def operation_key_of(change_path: str) -> str:
    """从变更路径提取操作键，例如 ``"GET /users#response-200.id"`` -> ``"GET /users"``。"""
    return change_path.split("#", 1)[0]


class ReleaseGate:
    """消费 DiffReport + 已登记消费者，产出可解释的 GateReport。"""

    def __init__(self, store: LocalStore) -> None:
        self.store = store

    def evaluate(self, diff: DiffReport) -> GateReport:
        consumers = self.store.list_consumers(diff.service)

        findings: list[GateFinding] = []
        breaking_changes = [c for c in diff.changes if c.severity is Severity.BREAKING]
        for change in breaking_changes:
            findings.append(self._classify_change(change, consumers))

        findings.sort(
            key=lambda f: (f.reason_code, f.change.path, f.change.kind)
        )

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

    def _classify_change(
        self, change: Change, consumers: list[ConsumerExpectation]
    ) -> GateFinding:
        op = operation_key_of(change.path)
        callers = [c for c in consumers if self._calls_operation(c, op)]
        if not callers:
            return GateFinding(
                affected=False,
                change=change,
                operations=(op,),
                reason_code=REASON_UNUSED,
                message=f"破坏性变更未被任何已登记消费者使用；操作 {op} 无消费者登记",
                affected_consumers=(),
            )

        section, detail = self._split_change_path(change.path)
        caller_names = sorted(c.consumer for c in callers)

        operation_level_hit = sorted(
            c.consumer for c in callers if op in c.operations
        )
        dependency_hit = sorted(
            c.consumer
            for c in callers
            if any(
                self._dependency_hits(dep, section, detail, change)
                for dep in c.dependencies
                if dep.operation == op
            )
        )
        # 请求侧破坏（含端点删除/请求体必填）：任何调用该操作的消费者都发不出
        # 合法请求，操作级登记与依赖级登记都算命中。
        request_side_hit = sorted(
            c.consumer for c in callers if self._is_request_side_breaking(section, change)
        )

        hit_consumers = sorted(
            set(operation_level_hit) | set(dependency_hit) | set(request_side_hit)
        )

        if hit_consumers:
            if operation_level_hit:
                # 老消费者（操作级）命中沿用历史原因码，行为保持不变
                reason = REASON_OPERATION_LEVEL_HIT
                message = (
                    f"破坏性变更影响 {len(hit_consumers)} 个已登记消费者"
                    f"({', '.join(hit_consumers)})；操作 {op} 仍在被使用"
                )
            else:
                reason = REASON_DEPENDENCY_HIT
                message = (
                    f"破坏性变更命中 {len(hit_consumers)} 个消费者声明依赖的内容"
                    f"({', '.join(hit_consumers)})：{change.path}"
                )
            return GateFinding(
                affected=True,
                change=change,
                operations=(op,),
                reason_code=reason,
                message=message,
                affected_consumers=tuple(hit_consumers),
            )

        # 有消费者调用该操作，但破坏没有命中任何它声明依赖的内容
        return GateFinding(
            affected=False,
            change=change,
            operations=(op,),
            reason_code=REASON_OPERATION_USED_NOT_DEPENDED,
            message=(
                f"破坏性变更所在操作 {op} 被消费者 ({', '.join(caller_names)}) 调用，"
                "但未命中其声明依赖的响应/字段，不影响这些消费者"
            ),
            affected_consumers=(),
        )

    @staticmethod
    def _calls_operation(consumer: ConsumerExpectation, op: str) -> bool:
        if op in consumer.operations:
            return True
        return any(dep.operation == op for dep in consumer.dependencies)

    @staticmethod
    def _is_request_side_breaking(
        section: str | None, change: Change
    ) -> bool:
        """请求侧（或端点级）破坏：调用方发不出合法请求，影响所有调用者。"""
        if section is None:
            # 端点级破坏只有 endpoint_removed（BREAKING）
            return change.kind == "endpoint_removed"
        return section in {"request-param", "request-body"} or \
            change.kind == "request_body_required_added"

    @staticmethod
    def _split_change_path(path: str) -> tuple[str | None, str]:
        """拆分 ``"OP#section.detail"`` -> (section, detail)。

        端点级破坏（路径中无 ``#``）section 为 None；
        section 形如 ``response-200`` / ``request-body`` / ``request-param``。
        """
        if "#" not in path:
            return None, ""
        _, rest = path.split("#", 1)
        if "." in rest:
            section, detail = rest.split(".", 1)
            return section, detail
        return rest, ""

    @staticmethod
    def _dependency_hits(
        dep: ConsumerDependency,
        section: str | None,
        detail: str,
        change: Change,
    ) -> bool:
        """判断某条依赖声明是否被单条*响应侧*破坏命中。

        请求侧/端点级破坏的命中在 :meth:`_is_request_side_breaking` 统一处理，
        这里只负责响应字段依赖。
        """
        if section is None or not section.startswith("response-"):
            return False
        expected_section = f"response-{dep.response}"
        if section != expected_section:
            return False

        # 整个该响应被删 / 响应体整体移除：依赖该响应的消费者必然命中
        if change.kind in {"response_removed", "response_body_removed"}:
            return True

        if not dep.fields:
            # 只声明依赖“这种响应”，未声明字段：不命中具体字段级破坏
            return False

        if not detail:
            return False

        # detail 为点分字段路径；声明字段是破坏路径本身或其上层前缀即命中
        return any(detail == f or detail.startswith(f + ".") for f in dep.fields)


__all__ = ["ReleaseGate", "operation_key_of"]
