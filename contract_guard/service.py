"""外观服务：采集、登记、比对、门禁的唯一业务编排入口。

HTTP 路由与库内直接调用都经由本类，避免不同入口出现结论分叉。
"""

from __future__ import annotations

from pathlib import Path

from .collector import ContractCollector
from .differ import SnapshotDiffer
from .errors import IncompleteContractError
from .gate import ReleaseGate
from .models import Baseline, ConsumerExpectation, DiffReport, GateReport
from .registry import BaselineRegistry
from .storage import LocalStore


class ContractGuardService:
    """单实例聚合所有子系统。"""

    def __init__(self, storage_root: str | Path) -> None:
        self.root = Path(storage_root)
        self.store = LocalStore(self.root)
        self.collector = ContractCollector()
        self.registry = BaselineRegistry(self.store, self.collector)
        self.differ = SnapshotDiffer()
        self.gate = ReleaseGate(self.store)

    def register_baseline(
        self,
        raw_contract: object,
        *,
        service: str,
        version: str,
        expected_fingerprint: str | None = None,
    ) -> Baseline:
        return self.registry.register(
            raw_contract,
            service=service,
            version=version,
            expected_fingerprint=expected_fingerprint,
        )

    def diff_versions(self, service: str, old_version: str, new_version: str) -> DiffReport:
        old = self.store.get_baseline(service, old_version)
        new = self.store.get_baseline(service, new_version)
        return self.differ.diff(old.snapshot, new.snapshot)

    def check_release(
        self, service: str, old_version: str, new_version: str
    ) -> GateReport:
        return self.gate.evaluate(self.diff_versions(service, old_version, new_version))

    def register_consumer(
        self,
        service: str,
        consumer: str,
        operations: list[str] | tuple[str, ...],
    ) -> ConsumerExpectation:
        if not isinstance(consumer, str) or not consumer.strip():
            raise IncompleteContractError("empty_consumer_name")
        normalized_ops: list[str] = []
        for op in operations:
            if not isinstance(op, str) or " " not in op:
                raise IncompleteContractError(f"invalid_operation_key:{op!r}")
            method, path = op.split(" ", 1)
            if not method.isalpha() or not path.startswith("/"):
                raise IncompleteContractError(f"invalid_operation_key:{op!r}")
            normalized_ops.append(f"{method.upper()} {path}")
        expectation = ConsumerExpectation(
            service=service,
            consumer=consumer,
            operations=tuple(normalized_ops),
        )
        self.store.put_consumer(expectation)
        return self.store.get_consumer(service, consumer)

    def list_baselines(self, service: str) -> list[Baseline]:
        return self.store.list_baselines(service)

    def list_consumers(self, service: str) -> list[ConsumerExpectation]:
        return self.store.list_consumers(service)


__all__ = ["ContractGuardService"]
