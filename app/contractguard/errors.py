"""归一化的错误类型。

所有底层解析 / 存储异常都必须转换为本模块定义的错误，
不得把底层实现的异常直接透出到调用方。
"""
from __future__ import annotations


class ContractGuardError(Exception):
    """契约守卫领域错误的基类。"""

    code: str = "guard_error"

    def __init__(self, message: str, *, reason: str = "", cause: Exception | None = None) -> None:
        super().__init__(message)
        self.message = message
        # 可区分的细分原因，便于调用方做稳定分支判断
        self.reason = reason or self.code
        self.__cause__ = cause


class ContractParseError(ContractGuardError):
    """契约无法解析或不完整（畸形契约）。"""

    code = "contract_parse_error"


class BaselineConflictError(ContractGuardError):
    """基线登记冲突：同一版本已存在且内容不同，拒绝静默覆盖。"""

    code = "baseline_conflict"


class BaselineNotFoundError(ContractGuardError):
    """指定的基线版本不存在。"""

    code = "baseline_not_found"


class ConsumerConflictError(ContractGuardError):
    """消费者期望重复登记且内容不一致。"""

    code = "consumer_conflict"


class ConsumerNotFoundError(ContractGuardError):
    """指定的消费者不存在。"""

    code = "consumer_not_found"


class StorageError(ContractGuardError):
    """本地存储读写异常的归一化包装。"""

    code = "storage_error"
