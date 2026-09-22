"""版本基线登记服务：采集 -> 归一化 -> 原子落库。"""

from __future__ import annotations

from datetime import UTC, datetime

from .collector import ContractCollector
from .errors import BaselineNotFoundError
from .models import Baseline
from .storage import LocalStore, version_sort_key


class BaselineRegistry:
    """负责把原始契约登记为版本基线。"""

    def __init__(self, store: LocalStore, collector: ContractCollector | None = None) -> None:
        self.store = store
        self.collector = collector or ContractCollector()

    def register(
        self,
        raw_contract: object,
        *,
        service: str,
        version: str,
        expected_fingerprint: str | None = None,
    ) -> Baseline:
        """采集并登记。

        解析类错误（ContractParseError/IncompleteContractError/
        UnsupportedContractError）原样抛出；存储冲突抛 BaselineConflictError。
        同版本同指纹重复登记幂等成功。
        """
        snapshot = self.collector.collect(raw_contract, service=service, version=version)
        baseline = Baseline(
            service=service,
            version=version,
            snapshot=snapshot,
            created_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        )
        self.store.put_baseline(baseline, expected_fingerprint=expected_fingerprint)
        return self.store.get_baseline(service, version)

    def get(self, service: str, version: str) -> Baseline:
        return self.store.get_baseline(service, version)

    def latest(self, service: str) -> Baseline | None:
        baselines = self.store.list_baselines(service)
        if not baselines:
            return None
        return max(baselines, key=lambda b: version_sort_key(b.version))


__all__ = ["BaselineRegistry", "BaselineNotFoundError"]
