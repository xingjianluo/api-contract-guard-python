# API Contract Guard（Python）

对 HTTP 服务暴露的接口契约做采集、归一化快照、版本基线登记、新旧比对与发布门禁，
让契约漂移在任意入口下得到**一致、可解释、可复现**的结论。

## 能力概览

- **多响应结果建模**：同一操作声明的每一种响应（`200/400/404/409/default`…）都进入
  契约快照并参与新老版本比对。新增一种响应结果算纯新增（不破坏）；删除已声明响应、
  删除响应字段、改动响应内容分别给出 `response_removed` / `response_field_removed` /
  `type_changed` 等可区分原因码。
- **字段级消费者依赖**：登记消费者时可声明“在某操作上真正依赖哪种响应的哪些字段”。
  门禁把破坏拆成三档可区分结论——命中依赖内容（`breaking_consumer_affected`）、
  只命中调用的操作但未命中依赖内容（`breaking_operation_only`）、没有任何消费者使用
  （`breaking_unused`）；依赖指向不存在的操作/响应/字段会被明确拒绝。
  **只登记操作的老格式消费者继续支持，行为不变。**
- **请求体必填语义**：请求体整体“可选→必填”判破坏（`request_body_became_required` /
  `request_body_added_required`），“必填→可选/移除”判兼容，与“某个字段必填”
  （`request_optional_to_required`）明确区分，不混成一条。
- 同一对版本无论比对多少次、响应与消费者声明顺序如何，结论逐字节一致。

## 快速开始

```bash
uv sync --group dev
.venv/bin/python -m pytest -q                      # 全部测试（含实时判定日志）
CONTRACT_GUARD_STORAGE=./.guard_data \
  .venv/bin/python -m uvicorn main:app --port 8000 # 启动服务
```

## 模块构成

| 模块 | 职责 |
| --- | --- |
| `contract_guard/collector.py` | OpenAPI 风格契约采集与归一化，畸形契约按可区分原因拒绝 |
| `contract_guard/models.py` | 不可变领域模型（字段/端点/快照/变更/门禁报告） |
| `contract_guard/storage.py` | 本地存储：原子写 + flock/线程锁 + 冲突检测 |
| `contract_guard/registry.py` | 基线登记：采集 → 落库，幂等/冲突语义 |
| `contract_guard/differ.py` | 方向相关的确定性兼容分级比对 |
| `contract_guard/gate.py` | 发布门禁（pass / warn / reject，原因可区分） |
| `contract_guard/service.py` | 唯一业务编排入口（HTTP 与库调用共用） |
| `contract_guard/api.py` | FastAPI 路由与错误归一化 |

详细分级规则、冲突与保留策略、本地验证步骤见
[`docs/compatibility.md`](docs/compatibility.md)。
