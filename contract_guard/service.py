"""外观服务：采集、登记、比对、门禁的唯一业务编排入口。

HTTP 路由与库内直接调用都经由本类，避免不同入口出现结论分叉。
"""

from __future__ import annotations

from pathlib import Path

from .collector import ContractCollector
from .differ import SnapshotDiffer
from .errors import (
    ConsumerUnknownFieldError,
    ConsumerUnknownOperationError,
    ConsumerUnknownResponseError,
    IncompleteContractError,
)
from .gate import ReleaseGate
from .models import (
    Baseline,
    ConsumerDependency,
    ConsumerExpectation,
    DiffReport,
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
        operations: list[str] | tuple[str, ...] | None = None,
        dependencies: list[dict] | tuple[dict, ...] | None = None,
    ) -> ConsumerExpectation:
        """登记消费者。

        支持两种粒度（至少提供一种，二者可并存）：
            operations:   操作级登记（老方式），如 ``["GET /users"]``；
            dependencies: 依赖级登记，每条形如
                ``{"operation": "GET /users/{id}", "response": "200",
                   "fields": ["id", "name"]}``，
                ``fields`` 可省略/为空，表示只依赖“会收到该响应”这一事实。

        依赖必须指向最新基线中真实存在的操作、响应与字段，否则按
        可区分的原因码显式拒绝（绝不静默放行）。
        """
        if not isinstance(consumer, str) or not consumer.strip():
            raise IncompleteContractError("empty_consumer_name")

        normalized_ops = [self._normalize_operation(op) for op in (operations or [])]
        normalized_deps = [
            self._normalize_dependency(service, dep) for dep in (dependencies or [])
        ]

        if not normalized_ops and not normalized_deps:
            raise IncompleteContractError(
                "consumer_must_declare_operations_or_dependencies"
            )

        expectation = ConsumerExpectation(
            service=service,
            consumer=consumer,
            operations=tuple(normalized_ops),
            dependencies=tuple(normalized_deps),
        )
        self.store.put_consumer(expectation)
        return self.store.get_consumer(service, consumer)

    @staticmethod
    def _normalize_operation(op: object) -> str:
        if not isinstance(op, str) or " " not in op:
            raise IncompleteContractError(f"invalid_operation_key:{op!r}")
        method, path = op.split(" ", 1)
        if not method.isalpha() or not path.startswith("/"):
            raise IncompleteContractError(f"invalid_operation_key:{op!r}")
        return f"{method.upper()} {path}"

    def _normalize_dependency(self, service: str, raw: object) -> ConsumerDependency:
        if not isinstance(raw, dict):
            raise IncompleteContractError("dependency_must_be_object")
        operation = self._normalize_operation(raw.get("operation"))
        response = raw.get("response")
        if not isinstance(response, str) or not response:
            raise IncompleteContractError(
                f"dependency_missing_response:{operation}"
            )
        fields_raw = raw.get("fields", [])
        if not isinstance(fields_raw, (list, tuple)) or isinstance(fields_raw, str):
            raise IncompleteContractError(
                f"dependency_fields_must_be_array:{operation}:{response}"
            )
        fields: list[str] = []
        for f in fields_raw:
            if not isinstance(f, str) or not f.strip():
                raise IncompleteContractError(
                    f"dependency_invalid_field:{operation}:{response}:{f!r}"
                )
            fields.append(f)

        # 按最新基线校验：不存在的操作/响应/字段必须显式拒绝，原因可区分
        baseline = self.registry.latest(service)
        if baseline is None:
            raise ConsumerUnknownOperationError(
                f"no_baseline_for_service:{service}:{operation}"
            )
        endpoint = next(
            (e for e in baseline.snapshot.endpoints
             if f"{e.method} {e.path}" == operation),
            None,
        )
        if endpoint is None:
            raise ConsumerUnknownOperationError(
                f"unknown_operation:{service}:{operation}"
            )
        resp = next((r for r in endpoint.responses if r.status_code == response), None)
        if resp is None:
            raise ConsumerUnknownResponseError(
                f"unknown_response:{service}:{operation}:{response}"
            )
        known_field_paths = self._collect_field_paths(resp)
        for f in fields:
            if f not in known_field_paths:
                raise ConsumerUnknownFieldError(
                    f"unknown_field:{service}:{operation}:{response}:{f}"
                )
        return ConsumerDependency(
            operation=operation,
            response=response,
            fields=tuple(fields),
        )

    @staticmethod
    def _collect_field_paths(response) -> frozenset[str]:
        """收集响应体内所有字段的点分路径（含嵌套）。"""
        paths: set[str] = set()

        def walk(fields, prefix: str) -> None:
            for field_ in fields:
                full = f"{prefix}.{field_.name}" if prefix else field_.name
                paths.add(full)
                walk(field_.children, full)

        walk(response.fields, "")
        return frozenset(paths)

    def list_baselines(self, service: str) -> list[Baseline]:
        return self.store.list_baselines(service)

    def list_consumers(self, service: str) -> list[ConsumerExpectation]:
        return self.store.list_consumers(service)


__all__ = ["ContractGuardService"]
