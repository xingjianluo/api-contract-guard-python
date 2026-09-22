# API Contract Guard（Python）

对 HTTP 服务暴露的接口契约做采集、归一化快照、版本基线登记、新旧比对与发布门禁，
让契约漂移在任意入口下得到**一致、可解释、可复现**的结论。

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
