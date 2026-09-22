# API Contract Guard（Python）

为 HTTP 服务提供 **API 契约快照采集、基线登记、版本兼容性判定、消费者期望与发布门禁**。
所有结论在不同入口（领域服务 / HTTP API）下一致、可解释、可复现；
数据保存在本地文件存储中，不依赖外部数据库。

## 能力概览

1. **契约采集与归一化**：从服务实际暴露的类 OpenAPI 契约采集为稳定模型；
   字段顺序、必填/可选、可空性、默认值、枚举取值的语义被显式归一；
   不完整或无法解析的契约**不会被静默跳过**，而是以可区分的 `reason` 拒绝。
2. **基线登记与版本比对**：变更按 **新增 / 兼容调整 / 破坏性变更** 三级分类，
   每条变更带 `kind + location + detail` 作为判定依据；
   同一对版本重复判定结论完全一致。
3. **消费者期望与发布门禁**：区分
   - `breaking_change_impacts_registered_consumer`：影响已登记消费者的破坏性变更；
   - `unused_breaking_change`：当前无人使用、但仍属破坏性的变更（不因此放行）。
4. **错误归一化**：解析 / 存储错误统一为领域错误（稳定的 `code` 与 `reason`），
   底层 `OSError`、`JSONDecodeError` 等不会直接透出。

## 兼容性分级规则

判定按 **请求方向**（客户端 → 服务端）与 **响应方向**（服务端 → 客户端）分别进行，
两个方向“放宽 / 收紧”的语义相反：

| 变更 | 请求方向 | 响应方向 |
| --- | --- | --- |
| 新增接口 | added（放行） | — |
| 移除接口 | **breaking** | — |
| 新增必填字段 / 必填 query 参数 | **breaking**（旧调用方不会提供） | compatible |
| 新增可选字段 | compatible | added / compatible |
| 移除字段 / query 参数 | **breaking**（旧调用方可能仍发送/依赖） | **breaking** |
| 可选 → 必填 | **breaking** | compatible |
| 必填 → 可选 | compatible | **breaking**（消费者原本可依赖其必存在） |
| nullable → 非空（收紧） | **breaking** | compatible |
| 非空 → nullable（放宽） | compatible | **breaking** |
| 字段类型变化 | **breaking** | **breaking** |
| 默认值变化 | compatible | compatible |
| 枚举新增取值 | compatible | **breaking**（消费者可能无法识别新值） |
| 枚举移除取值 | **breaking**（旧调用方可能仍发送） | compatible |
| 响应状态码新增 | added | 兼容 |
| 响应状态码移除 | **breaking** | — |

- `added`：纯新增，不影响既有调用方。
- `compatible`：存在语义调整但向后兼容。
- `breaking`：破坏性变更；只要存在任一 breaking，版本对即判为不兼容、门禁拒绝。

> 设计取保守策略：无法证明安全的“移除/收紧”一律判为破坏性，避免漏报漂移。

### 可复现性

- 规范结构对端点按 `(method, path)`、对子属性按字段名、对枚举按取值排序；
- 变更列表按 `(location, kind, detail)` 稳定排序并去重；
- 契约指纹为规范化 JSON 的 SHA-256（不含版本号与采集时间）；
- 服务层缓存同一对版本的比对结果；结论为纯函数，无随机/时间因素参与判定。

## 基线保留与冲突处理策略

本地存储目录（默认 `./.contractguard_data`）：

```
.contractguard_data/
├── baselines/<version>.json    # 每个版本一份不可变基线
└── consumers/<consumer>.json   # 每个消费者一份期望
```

- **基线永久保留**：登记后不做自动清理或覆盖，新版本用新版本号另存。
- **原子写入**：同目录临时文件 + `fsync` + `os.replace`，不会出现部分写入；
  新建文件用 `O_CREAT|O_EXCL` 独占创建。
- **并发登记**：
  - 内容相同（指纹一致）→ 幂等成功（`idempotent`），可安全重试；
  - 内容不同且未显式 `overwrite=true` → 明确拒绝
    （`baseline_already_exists` / `concurrent_baseline_conflict`），
    **绝不静默合并，也不发生覆盖丢失**；最终磁盘内容必为某个完整候选。
- 消费者期望同理：重复内容幂等，不同内容抛 `consumer_conflict`。

## HTTP API

| 方法与路径 | 说明 |
| --- | --- |
| `POST /baselines/{version}` | 登记契约基线（`?overwrite=true` 显式覆盖） |
| `GET  /baselines` | 列出全部基线版本 |
| `GET  /diff/{old}/{new}` | 两版本比对，返回分级变更与判定依据 |
| `POST /consumers/{name}` | 登记消费者期望（`?overwrite=true` 覆盖） |
| `GET  /consumers` | 列出消费者 |
| `GET  /gate/{old}/{new}` | 发布门禁，返回消费者影响与未使用破坏性变更 |

消费者期望载荷示例：

```json
{
  "version": "1.0",
  "used_endpoints": [["GET", "/users"]],
  "used_fields": {"GET /users": ["response[200].name"]}
}
```

错误响应统一形如：

```json
{"error": "baseline_conflict", "reason": "baseline_already_exists", "message": "..."}
```

| error | HTTP | 典型 reason |
| --- | --- | --- |
| `contract_parse_error` | 422 | `empty_contract` / `missing_responses` / `unknown_type` / `invalid_enum` … |
| `baseline_conflict` / `consumer_conflict` | 409 | `*_already_exists` / `concurrent_*_conflict` |
| `baseline_not_found` / `consumer_not_found` | 404 | `unknown_version` / `unknown_consumer` |
| `storage_error` | 500 | `io_failure` / `data_corruption` |

## 本地验证

```bash
# 安装依赖（含 pytest）
uv sync --dev

# 全量单测（覆盖并发登记、重复判定、破坏性变更、畸形契约等）
uv run pytest

# 查看单测中打印的“输入契约 -> 判定依据 -> 最终结论”
cat test-output.log

# 启动服务
uv run uvicorn main:app --reload
```

示例：

```bash
curl -s -X POST localhost:8000/baselines/1.0 \
  -H 'content-type: application/json' -d @examples/v1.json
curl -s -X POST localhost:8000/baselines/2.0 \
  -H 'content-type: application/json' -d @examples/v2.json
curl -s localhost:8000/diff/1.0/2.0
curl -s localhost:8000/gate/1.0/2.0
```

## 代码结构

```
app/contractguard/
├── errors.py    # 归一化错误类型
├── models.py    # 归一化契约模型 / 指纹 / 变更与结论
├── parser.py    # 严格解析（畸形契约拒绝原因可区分）
├── diff.py      # 双向语义的兼容性分级
├── storage.py   # 原子写、并发冲突拒绝、持久化
├── service.py   # 基线/比对/消费者/门禁领域服务
└── http_api.py  # FastAPI 入口与错误映射
```
