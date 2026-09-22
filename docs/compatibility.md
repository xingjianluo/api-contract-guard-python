# API 契约兼容性分级与本地验证指南

本服务对 HTTP 接口契约做**采集 → 归一化快照 → 版本基线登记 → 新旧比对 → 发布门禁**，
所有入口（HTTP / Python 直接调用）走同一条编排
（`contract_guard.service.ContractGuardService`），结论确定、可解释、可复现。

## 1. 输入契约格式

采用 OpenAPI 3 风格文档（`paths` 形态），支持传入已解析的 dict、JSON 文本或 UTF-8 字节。

- 每个操作通过 `METHOD /path` 唯一标识（如 `GET /users/{id}`）；
- 请求参数来自 `parameters`（`in: query/header/path/cookie`），请求体来自
  `requestBody.content["application/json"]`；
- 响应取**数字最小的 2xx 状态码**，只建模 `application/json` 响应体；无内容的
  2xx（如 204）视为无响应字段。

### 归一化（保证可比较）

| 语义 | 归一化规则 |
| --- | --- |
| 字段顺序 | endpoints 按 `(path, method)`，字段按名排序（嵌套对象递归排序），与原始声明顺序无关 |
| 必填 | `required` 显式化；path 参数强制必填（声明 `required:false` 判为畸形） |
| 可空 | `nullable` 显式布尔 |
| 默认值 | `has_default` 区分“未声明 default”与“声明了 `default: null`”两种语义 |
| 枚举 | 枚举值按类型秩（null < bool < int < float < str）+ 表示排序；空枚举/重复值判为畸形 |
| 整份快照 | 规范化 JSON（sort_keys、紧凑分隔符）后取 sha256 指纹 |

### 拒绝原因（不静默跳过，`error.code` 稳定）

| code | 触发场景 |
| --- | --- |
| `contract_parse_error` | 非合法 JSON、顶层不是对象、`paths` 不是对象、结构节点类型错位 |
| `incomplete_contract` | 缺 `paths`、字段缺 `type`、数组缺 `items`、required 引用未定义字段、重复参数、缺少成功响应、path 参数非必填等 |
| `unsupported_contract` | 未知字段类型、未知参数位置、不支持的请求媒体类型、不支持的枚举值 |

## 2. 兼容性分级规则

每条变更带 `severity` 与 `reason_code`（判定依据）。分级是**方向相关**的。

### 请求侧（客户端 → 服务端）

| 变化 | 分级 | reason_code |
| --- | --- | --- |
| 新增必填字段/参数 | 🔴 breaking | `request_required_added` |
| 新增可选字段/参数 | 🟢 compatible | `request_optional_added` |
| 删除字段/参数 | 🟢 compatible | `request_field_removed` |
| 可选 → 必填 | 🔴 breaking | `request_optional_to_required` |
| 必填 → 可选 | 🟢 compatible | `required_to_optional` |
| 类型/数组元素类型改变 | 🔴 breaking | `type_changed` |
| 枚举移除成员（旧值不再被接受） | 🔴 breaking | `enum_value_removed` |
| 枚举新增成员 | 🟢 compatible | `enum_value_added` |
| 可空 → 不可空 | 🔴 breaking | `nullable_to_non_nullable` |
| 不可空 → 可空 | 🟢 compatible | `non_nullable_to_nullable` |
| 移除默认值 | 🔴 breaking | `default_removed` |
| 新增/修改默认值 | 🟢 compatible | `default_added` / `default_changed` |
| 请求体整体移除 | 🟢 compatible | `request_body_removed` |

### 响应侧（服务端 → 客户端）

| 变化 | 分级 | reason_code |
| --- | --- | --- |
| 新增字段（旧客户端忽略未知字段） | 🟢 compatible | `response_field_added` |
| 删除字段 | 🔴 breaking | `response_field_removed` |
| 类型改变 | 🔴 breaking | `type_changed` |
| 枚举新增成员（可能下发未知值） | 🔴 breaking | `enum_value_added` |
| 枚举移除成员 | 🟢 compatible | `enum_value_removed` |
| 不可空 → 可空（null 可能到达旧客户端） | 🔴 breaking | `non_nullable_to_nullable` |
| 可空 → 不可空 | 🟢 compatible | `nullable_to_non_nullable` |
| 默认值变化 | 🟢 compatible | `default_*` |
| 成功响应体整体移除 | 🔴 breaking | `response_body_removed` |

### 端点级

- 新增端点：`added` / `endpoint_added`（独立的第三档，表示纯新增）；
- 删除端点：🔴 breaking / `endpoint_removed`；
- 端点标记 deprecated：🟢 compatible / `endpoint_deprecated`。

报告中的变更按 `(severity 秩, path, kind)` 固定排序，因此同一对版本
**无论重复多少次、从哪个入口触发，结论逐字节一致**。

## 3. 发布门禁

| verdict | 条件 | finding 原因码 |
| --- | --- | --- |
| `pass` | 无破坏性变更 | （无 findings） |
| `warn` | 有破坏性变更，但**不影响任何已登记消费者**（未被使用的操作） | `breaking_unused` |
| `reject` | 至少一条破坏性变更命中某消费者声明使用的操作 | `breaking_consumer_affected`（同时存在未使用破坏时，findings 中两种原因码都会出现，可区分） |

影响判定是确定性的前缀匹配：变更路径 `METHOD /path#section.field…` 的操作键
`METHOD /path` 命中消费者登记的操作即视为受影响。

## 4. 基线保留与冲突处理策略

### 保留策略（本地存储）

```
<storage-root>/<service>/baselines.json      # 版本基线索引（保留全部版本，不做覆盖回收）
<storage-root>/<service>/consumers.json      # 消费者期望索引
<storage-root>/<service>/.locks/*.lock       # 跨进程文件锁
```

- 基线一旦写入**永久保留**，同版本不会被再次写改；列表按语义版本排序
  （`1.0.10 > 1.0.2`）。
- 持久化 JSON 带 `schema_version`，读取时校验；无法识别 / 损坏抛 `storage_corruption`。

### 一致性保证

- **原子写**：临时文件 + `fsync` + `os.replace`，读方只看到完整的旧版或新版，不存在部分写入。
- **双重锁**：`fcntl.flock(LOCK_EX)`（跨进程）+ `threading.Lock`（同进程线程）
  串行化“读—改—写”；锁文件独立于数据文件，避免 replace 导致锁丢失。

### 冲突策略（显式拒绝，绝不静默合并/覆盖）

- 同版本 + 同指纹重复登记 → **幂等成功**，不改写 `created_at`；
- 同版本 + 不同指纹 → **409 `baseline_conflict`**，消息同时给出双方指纹前缀；
- 消费者同名 + 操作集合一致（顺序无关）→ 幂等成功；
- 消费者同名 + 操作集合不同 → **409 `consumer_conflict`**；
- 乐观并发参数 `expected_fingerprint` 不满足 → 同样返回 `baseline_conflict`。

## 5. HTTP API

| 方法 & 路径 | 说明 |
| --- | --- |
| `POST /services/{service}/baselines/{version}` | 登记基线（body 为契约文档；可带 `?expected_fingerprint=`） |
| `GET  /services/{service}/baselines` | 按语义版本排序列出基线 |
| `POST /services/{service}/consumers/{consumer}` | 登记消费者，body：`{"operations": ["GET /users"]}` |
| `GET  /services/{service}/diff/{old}/{new}` | 两版本确定性比对 |
| `GET  /services/{service}/release-check/{old}/{new}` | 发布门禁判定 |

错误统一形如 `{"error": {"code": "...", "message": "..."}}`；底层异常
（JSONDecodeError / OSError 等）一律归一化，不直接透出。

## 6. 本地验证方法

```bash
# 1) 安装依赖（含 dev：pytest / httpx）
uv sync --group dev

# 2) 运行全部单测（实时日志会打印输入契约、判定依据、最终结论）
.venv/bin/python -m pytest -q

# 3) 启动真实服务
CONTRACT_GUARD_STORAGE=./.guard_data .venv/bin/python -m uvicorn main:app --port 8000

# 4) 典型调用
curl -X POST localhost:8000/services/shop/baselines/1.0.0 \
     -H 'Content-Type: application/json' --data @contract_v1.json
curl -X POST localhost:8000/services/shop/consumers/web \
     -H 'Content-Type: application/json' -d '{"operations":["GET /users"]}'
curl localhost:8000/services/shop/release-check/1.0.0/2.0.0
```

测试覆盖矩阵：并发登记（线程 + 跨进程冲突）、重复判定一致性、
请求/响应两侧各类破坏性与兼容变更、枚举/可空/默认值语义、
畸形契约的可区分拒绝原因、损坏存储归一化、HTTP 与库调用入口结论一致性。
