"""归一化错误类型。

底层解析 / 存储 / 输入异常不得直接透出，必须转换为本模块中的类型。
每个错误都带稳定的 ``code``，调用方据此区分失败原因。
"""

from __future__ import annotations


class ContractGuardError(Exception):
    """所有契约守卫错误的基类。"""

    code: str = "guard_error"

    def __init__(self, message: str = "", *, cause: BaseException | None = None) -> None:
        super().__init__(message or self.code)
        self.message = message or self.code
        self.cause = cause

    def __str__(self) -> str:  # pragma: no cover - 调试辅助
        return f"[{self.code}] {self.message}"


# ---------- 采集 / 解析 ----------


class ContractParseError(ContractGuardError):
    """契约无法解析（JSON 非法、结构不是预期形态等）。"""

    code = "contract_parse_error"


class IncompleteContractError(ContractGuardError):
    """契约可解析但不完整（缺路径、缺方法、字段缺类型等）。"""

    code = "incomplete_contract"


class UnsupportedContractError(ContractGuardError):
    """契约引用了系统不支持的特性（未知类型、未知参数位置等）。"""

    code = "unsupported_contract"


# ---------- 基线 / 消费者登记 ----------


class BaselineConflictError(ContractGuardError):
    """并发登记冲突：版本已存在且内容不一致，拒绝静默合并。"""

    code = "baseline_conflict"


class BaselineNotFoundError(ContractGuardError):
    """指定版本基线不存在。"""

    code = "baseline_not_found"


class ConsumerConflictError(ContractGuardError):
    """同名消费者已登记且期望不一致。"""

    code = "consumer_conflict"


class ConsumerDependencyError(IncompleteContractError):
    """消费者细粒度依赖声明不合法的基类（HTTP 统一 422）。"""

    code = "consumer_dependency_invalid"


class ConsumerUnknownOperationError(ConsumerDependencyError):
    """依赖指向当前契约中根本不存在的操作。"""

    code = "consumer_dependency_unknown_operation"


class ConsumerUnknownResponseError(ConsumerDependencyError):
    """依赖指向某操作下根本没有声明的响应结果。"""

    code = "consumer_dependency_unknown_response"


class ConsumerUnknownFieldError(ConsumerDependencyError):
    """依赖指向某响应中根本不存在的字段路径。"""

    code = "consumer_dependency_unknown_field"


class ConsumerNotFoundError(ContractGuardError):
    """指定消费者不存在。"""

    code = "consumer_not_found"


# ---------- 存储 ----------


class StorageError(ContractGuardError):
    """本地存储的通用归一化错误。"""

    code = "storage_error"


class StorageCorruptionError(StorageError):
    """落盘数据损坏（无法反序列化）。"""

    code = "storage_corruption"
