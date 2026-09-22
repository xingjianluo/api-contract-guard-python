"""发布门禁：结合消费者期望对差异报告做放行判定。

结论分三档：

* ``pass``   没有任何破坏性变更；
* ``warn``   存在破坏性变更，但不影响任何已登记消费者（未被使用），
             原因码 ``breaking_unused``；
* ``reject`` 存在影响已登记消费者的破坏性变更，
             原因码 ``breaking_consumer_affected``。

“影响”的判定是确定性的：变更路径形如 ``"METHOD /path#section.field..."``，
其操作键前缀（``METHOD /path``）命中某个消费者声明使用的操作即受影响。
findings 按 (reason_code, path, change.kind) 排序，结论可复现。
"""

from __future__ import annotations

from .models import Change, DiffReport, GateFinding, GateReport, GateVerdict, Severity
from .storage import LocalStore


def operation_key_of(change_path: str) -> str:
    """从变更路径提取操作键，例如 ``"GET /users#response.id"`` -> ``"GET /users"``。"""
    return change_path.split("#", 1)[0]


class ReleaseGate:
    """消费 DiffReport + 已登记消费者，产出可解释的 GateReport。"""

    def __init__(self, store: LocalStore) -> None:
        self.store = store

    def evaluate(self, diff: DiffReport) -> GateReport:
        consumers = self.store.list_consumers(diff.service)
        used_operations = {op for c in consumers for op in c.operations}

        findings: list[GateFinding] = []
        affected_operations_by_change: list[str] = []

        breaking_changes = [c for c in diff.changes if c.severity is Severity.BREAKING]
        for change in breaking_changes:
            op = operation_key_of(change.path)
            affected_consumers = [
                c.consumer for c in consumers if op in c.operations
            ]
            affected = bool(affected_consumers)
            if affected:
                reason = "breaking_consumer_affected"
                msg = (
                    f"破坏性变更影响 {len(affected_consumers)} 个已登记消费者"
                    f"({', '.join(sorted(affected_consumers))})；操作 {op} 仍在被使用"
                )
                affected_operations_by_change.append(op)
            else:
                reason = "breaking_unused"
                msg = f"破坏性变更未被任何已登记消费者使用；操作 {op} 无消费者登记"
            findings.append(
                GateFinding(
                    affected=affected,
                    change=change,
                    operations=(op,),
                    reason_code=reason,
                    message=msg,
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


__all__ = ["ReleaseGate", "operation_key_of"]
