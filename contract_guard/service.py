"""外观服务：采集、登记、比对、门禁的唯一业务编排入口。

HTTP 路由与库内直接调用都经由本类，避免不同入口出现结论分叉。
"""

from __future__ import annotations

from pathlib import Path

from .collector import ContractCollector
from .differ import SnapshotDiffer
from .errors import IncompleteContractError
from .gate import ReleaseGate
from .models import (
    Baseline,
    ConsumerDependency,
    ConsumerExpectation,
    DiffReport,
    EndpointModel,
    GateReport,
)
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
        dependencies: list[dict] | tuple[dict, ...] | None = None,
    ) -> ConsumerExpectation:
        """登记消费者。

        ``operations``：老格式，声明调用的操作（行为与历史版本一致）。
        ``dependencies``：可选的新格式条目列表，每项
        ``{"operation": "GET /users", "response": "200", "fields": ["id"]}``。
        fields 省略/为空表示依赖该响应整体。

        依赖必须能在**最新已登记基线**上解析，否则按可区分原因拒绝：
            unknown_consumer_operation  依赖的操作在契约中不存在
            unknown_consumer_response   操作存在，但声明的响应状态码不存在
            unknown_consumer_field      响应存在，但声明的字段路径不存在
        """
        if not isinstance(consumer, str) or not consumer.strip():
            raise IncompleteContractError("empty_consumer_name")
        normalized_ops = [self._normalize_operation_key(op) for op in operations]

        raw_deps = list(dependencies) if dependencies else []
        normalized_deps: list[ConsumerDependency] = []
        for index, dep in enumerate(raw_deps):
            ctx = f"dependency[{index}]"
            if not isinstance(dep, dict):
                raise IncompleteContractError(f"consumer_dependency_must_be_object:{ctx}")
            op_raw = dep.get("operation")
            status_raw = dep.get("response")
            fields_raw = dep.get("fields", [])
            if not isinstance(op_raw, str):
                raise IncompleteContractError(f"consumer_dependency_missing_operation:{ctx}")
            if not isinstance(status_raw, str) or not status_raw:
                raise IncompleteContractError(f"consumer_dependency_missing_response:{ctx}")
            if not isinstance(fields_raw, (list, tuple)) or isinstance(fields_raw, (str, bytes)):
                raise IncompleteContractError(f"consumer_dependency_fields_must_be_array:{ctx}")
            if not all(isinstance(f, str) and f for f in fields_raw):
                raise IncompleteContractError(f"consumer_dependency_field_must_be_string:{ctx}")
            op_key = self._normalize_operation_key(op_raw)
            normalized_deps.append(
                ConsumerDependency(
                    operation=op_key,
                    response=status_raw,
                    fields=tuple(fields_raw),
                )
            )
            normalized_ops.append(op_key)

        if normalized_deps:
            self._validate_dependencies(service, normalized_deps)

        expectation = ConsumerExpectation(
            service=service,
            consumer=consumer,
            operations=tuple(normalized_ops),
            dependencies=tuple(normalized_deps),
        )
        self.store.put_consumer(expectation)
        return self.store.get_consumer(service, consumer)

    @staticmethod
    def _normalize_operation_key(op: object) -> str:
        if not isinstance(op, str) or " " not in op:
            raise IncompleteContractError(f"invalid_operation_key:{op!r}")
        method, path = op.split(" ", 1)
        if not method.isalpha() or not path.startswith("/"):
            raise IncompleteContractError(f"invalid_operation_key:{op!r}")
        return f"{method.upper()} {path}"

    def _validate_dependencies(
        self, service: str, dependencies: list[ConsumerDependency]
    ) -> None:
        latest = self.registry.latest(service)
        if latest is None:
            raise IncompleteContractError("consumer_dependency_requires_baseline")
        snapshot = latest.snapshot
        endpoints = {(e.method, e.path): e for e in snapshot.endpoints}
        for dep in dependencies:
            method, path = dep.operation.split(" ", 1)
            endpoint = endpoints.get((method, path))
            if endpoint is None:
                raise IncompleteContractError(
                    f"unknown_consumer_operation:{dep.operation}"
                )
            response = self._find_response(endpoint, dep.response)
            if response is None:
                raise IncompleteContractError(
                    f"unknown_consumer_response:{dep.operation}:{dep.response}"
                )
            for field_path in dep.fields:
                if not self._field_exists(response, field_path):
                    raise IncompleteContractError(
                        f"unknown_consumer_field:{dep.operation}:{dep.response}:{field_path}"
                    )

    @staticmethod
    def _find_response(endpoint: EndpointModel, status: str):
        return next((r for r in endpoint.responses if r.status == status), None)

    @staticmethod
    def _field_exists(response, field_path: str) -> bool:
        parts = field_path.split(".")
        fields = response.fields
        current = None
        for part in parts:
            current = next((f for f in fields if f.name == part), None)
            if current is None:
                return False
            fields = current.children
        return current is not None

    def list_baselines(self, service: str) -> list[Baseline]:
        return self.store.list_baselines(service)

    def list_consumers(self, service: str) -> list[ConsumerExpectation]:
        return self.store.list_consumers(service)


__all__ = ["ContractGuardService"]
