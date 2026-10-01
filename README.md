# API Contract Guard（Python）

对 HTTP 服务暴露的接口契约做采集、归一化快照、版本基线登记、新旧比对与发布门禁，
让契约漂移在任意入口下得到**一致、可解释、可复现**的结论。

## 本期新增能力

- **一个操作的多种响应结果全部入契**：`200/400/404/409/default` 等每一种声明的
  响应（状态码 + 响应体字段）都会进入快照并参与版本比对。新增一种响应结果不破坏；
  删除已声明的响应、移除其响应体、改动其字段，按可区分的原因码判为破坏性。
- **消费者可登记字段/响应级依赖**：除了“用了哪个操作”，消费者还可声明
  “依赖某操作的哪一种响应里的哪些字段”。没用到的内容即使被破坏也不算影响它。
  门禁结论细分为：命中依赖内容（reject）、只命中操作未命中依赖（warn）、
  完全无消费者使用（warn），原因码各自可区分。
- **请求体整体必填性入契**：`requestBody.required` 从“可以不带”变“必须带”判破坏
  （`request_body_required_added`），放宽为兼容（`request_body_required_relaxed`），
  与“某个字段从可选变必填”（`request_optional_to_required`）明确区分。
  整体必填收紧属于请求侧破坏，**所有调用该操作的已登记消费者（含只登记响应
  依赖的）都会被判命中并 reject**。
- **嵌套字段依赖的整体命中**：消费者只声明依赖某响应里的嵌套字段
  （如 `profile.avatar`）时，若新版本把上层对象 `profile` 整体删除或整体改型，
  同样算命中其依赖内容并 reject；未声明依赖的响应/字段变化不会误算命中。
- 依赖若指向不存在的操作 / 响应 / 字段，登记时即以三种可区分的错误码显式拒绝。

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
