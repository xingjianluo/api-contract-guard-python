"""归一化后的契约领域模型。

所有模型均为不可变值对象；采集器负责把原始契约转换成本模块的类型，
之后比对、门禁等环节只依赖这些稳定模型，保证结论可复现。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


class Direction(enum.Enum):
    """字段 / 参数在 HTTP 消息中的方向。"""

    REQUEST = "request"
    RESPONSE = "response"


def is_success_status(code: str) -> bool:
    """状态码是否为 2xx。"""
    return len(code) == 3 and code.isdigit() and code.startswith("2")


def response_sort_key(code: str) -> tuple[int, object]:
    """响应状态码的确定性排序：数字码按数值，``default`` 排在最后。"""
    if code == "default":
        return (1, code)
    return (0, int(code))


class ParameterLocation(enum.Enum):
    """请求参数位置。"""

    QUERY = "query"
    HEADER = "header"
    PATH = "path"
    COOKIE = "cookie"


@dataclass(frozen=True, slots=True)
class FieldModel:
    """归一化后的字段。

    属性语义：
        name:       字段名（原始名保留，区分大小写）
        type_name:  归一化基础类型名，如 string/integer/array/object
        required:   是否必填
        nullable:   是否允许 null
        default:    默认值；未声明为 None，声明为 null 也是 None
                     （因此用 has_default 区分这两种语义）
        has_default:是否显式声明了 default
        enum:       枚举允许值，按规范化顺序排序后的元组；非枚举字段为空元组
        item_type:  当 type_name == "array" 时的元素类型名，否则 None
        children:   当 type_name == "object" 时的嵌套字段，按名排序
        direction:  位于请求还是响应
        location:   参数位置（query/header/path/cookie）；body 字段为 None
        deprecated: 是否标记废弃
    """

    name: str
    type_name: str
    required: bool
    nullable: bool
    default: object
    has_default: bool
    enum: tuple[object, ...]
    item_type: str | None
    children: tuple["FieldModel", ...]
    direction: Direction
    location: ParameterLocation | None
    deprecated: bool


@dataclass(frozen=True, slots=True)
class ResponseModel:
    """同一个操作下声明的某一种响应结果（按状态码区分）。

    属性语义：
        status_code: 归一化后的状态码键（HTTP 状态码字符串，如 "200"/"404"；
                     OpenAPI 的 default 响应归一化为 "default"）
        fields:      该响应 application/json 体的字段，按名排序；
                     无内容 / 非 JSON 响应为空元组
    """

    status_code: str
    fields: tuple[FieldModel, ...]


@dataclass(frozen=True, slots=True)
class EndpointModel:
    """单个操作（method + path）的归一化模型。"""

    method: str
    path: str
    request_params: tuple[FieldModel, ...]
    request_body: tuple[FieldModel, ...]
    deprecated: bool
    #: 同一操作声明的全部响应结果，按状态码稳定排序
    responses: tuple[ResponseModel, ...] = ()
    #: 整个请求体是否必填（OpenAPI requestBody.required）；无请求体时为 False
    request_body_required: bool = False


@dataclass(frozen=True, slots=True)
class ContractSnapshot:
    """一次采集得到的完整契约快照。"""

    service: str
    version: str
    endpoints: tuple[EndpointModel, ...]
    fingerprint: str


@dataclass(frozen=True, slots=True)
class Baseline:
    """已登记的版本基线。"""

    service: str
    version: str
    snapshot: ContractSnapshot
    created_at: str


@dataclass(frozen=True, slots=True)
class ConsumerDependency:
    """消费者在单个操作上的细粒度依赖声明。

    属性语义：
        operation:    操作键，形如 "GET /users/{id}"
        response:     依赖的响应状态码，如 "200"/"404"
        fields:       依赖的该响应体内字段路径（点分，嵌套字段递归到子级）；
                      空元组表示只依赖“会收到这种响应”这一事实（不依赖体内字段）
    """

    operation: str
    response: str
    fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ConsumerExpectation:
    """消费者对一组操作的使用期望。

    两种粒度可并存：
        operations:  操作级登记（老方式）：调用这些操作，其全部破坏都算命中；
        dependencies: 字段/响应级登记：只依赖某操作的某一种响应里的部分字段。
    """

    service: str
    consumer: str
    operations: tuple[str, ...]
    #: 细粒度依赖；空元组表示纯操作级的老消费者
    dependencies: tuple[ConsumerDependency, ...] = ()


# ---------- 比对 / 门禁结果 ----------


class Severity(enum.Enum):
    """变更分级。"""

    ADDED = "added"          # 纯新增（对既有消费者无影响）
    COMPATIBLE = "compatible"  # 兼容调整
    BREAKING = "breaking"    # 破坏性变更


@dataclass(frozen=True, slots=True)
class Change:
    """单条变更及其判定依据。"""

    severity: Severity
    path: str
    kind: str
    reason_code: str
    message: str
    old: object = None
    new: object = None


@dataclass(frozen=True, slots=True)
class DiffReport:
    """两个版本快照的比对报告（确定、可复现）。"""

    service: str
    old_version: str
    new_version: str
    changes: tuple[Change, ...]
    old_fingerprint: str
    new_fingerprint: str

    @property
    def has_breaking(self) -> bool:
        return any(c.severity is Severity.BREAKING for c in self.changes)


class GateVerdict(enum.Enum):
    """发布门禁结论。"""

    PASS = "pass"
    WARN = "warn"   # 有破坏性变更，但不影响任何已登记消费者
    REJECT = "reject"  # 有影响已登记消费者的破坏性变更


@dataclass(frozen=True, slots=True)
class GateFinding:
    """门禁对单条破坏性变更的判定明细。"""

    affected: bool
    change: Change
    operations: tuple[str, ...]
    reason_code: str
    message: str
    #: 真正被该破坏命中的消费者（命中其依赖内容或操作级登记），按名排序
    affected_consumers: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class GateReport:
    """门禁判定报告。"""

    service: str
    version: str
    verdict: GateVerdict
    findings: tuple[GateFinding, ...]
