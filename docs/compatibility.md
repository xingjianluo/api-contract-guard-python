# API 契约兼容性分级与本地验证指南

本服务对 HTTP 接口契约做**采集 → 归一化快照 → 版本基线登记 → 新旧比对 → 发布门禁**，
所有入口（HTTP / Python 直接调用）走同一条编排
（`contract_guard.service.ContractGuardService`），结论确定、可解释、可复现。

## 1. 输入契约格式

采用 OpenAPI 3 风格文档（`paths` 形态），支持传入已解析的 dict、JSON 文本或 UTF-8 字节。

- 每个操作通过 `METHOD /path` 唯一标识（如 `GET /users/{id}`）；
- 请求参数来自 `parameters`（`in: query/header/path/cookie`），请求体来自
  `requestBody.content["application/json"]`，其整体必填性取 `requestBody.required`；
- **同一操作声明的每一种响应结果都进入契约快照**：`responses` 下的所有数字状态码
  （`200/400/404/409/500…`）以及 `default` 各自独立建模；至少要声明一个 2xx 成功响应。
  只建模各响应的 `application/json` 体；无内容的响应（如 204）或非 JSON 体记为空字段集合。

### 归一化（保证可比较）

| 语义 | 归一化规则 |
| --- | --- |
| 端点/字段顺序 | endpoints 按 `(path, method)`；响应按状态码（数字升序，`default` 恒在最后）；字段按名排序（嵌套对象递归排序），与原始声明顺序无关 |
| 必填 | 字段 `required` 显式化；path 参数强制必填（声明 `required:false` 判为畸形） |
| 请求体必填 | `requestBody.required` 归一化为端点级布尔 `request_body_required`（缺省 false） |
| 可空 | `nullable` 显式布尔 |
| 默认值 | `has_default` 区分“未声明 default”与“声明了 `default: null`”两种语义 |
| 枚举 | 枚举值按类型秩（null < bool < int < float < str）+ 表示排序；空枚举/重复值判为畸形 |
| 整份快照 | 规范化 JSON（sort_keys、紧凑分隔符）后取 sha256 指纹（含全部响应结果与 body 必填性） |

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
| 请求体整体移除（必填/可选 → 无 body） | 🟢 compatible | `request_body_removed` |
| 无请求体 → 可选请求体 | 🟢 compatible | `request_body_added_optional` |
| 无请求体 → 必填请求体 | 🔴 breaking | `request_body_added_required` |
| 请求体 可选 → 必填 | 🔴 breaking | `request_body_became_required` |
| 请求体 必填 → 可选 | 🟢 compatible | `request_body_became_optional` |

> “整个请求体必填不必填”（`request_body_became_required` /
> `request_body_added_required` 等）与“请求体里某个字段必填不必填”
> （`request_optional_to_required` / `request_required_added`）是**两组互相独立的
> reason_code**，不会混成一条；无 body ↔ 有 body 之间也只报一条整体级变更，
> 不再逐字段重复计数。

### 多种响应结果（成功 + 失败 + 边界）

同一个操作声明的每一种响应结果（`200/400/404/409/500/default`…）都独立进入快照、
独立参与新老比对，变更路径段形如 `METHOD /path#response-<状态码>.字段…`。
下方“响应侧”的字段级规则（字段增删、类型、枚举、可空、默认值）对每种响应结果
分别适用；响应结果本身的增删规则如下：

| 变化 | 分级 | reason_code |
| --- | --- | --- |
| 新增一种以前没有的响应结果（如新加 404/409/default） | 🟣 added | `response_added` |
| 删除一个已声明的响应结果（如删掉 404） | 🔴 breaking | `response_removed` |
| 某响应体整体从有内容变无内容 | 🔴 breaking | `response_body_removed` |

不同破坏性情况的 reason_code 互不相同：整结果删除是 `response_removed`，
字段删除是 `response_field_removed`，内容改动是 `type_changed` 等，可以照着
原因码直接定位。响应在原始契约中的声明顺序不影响快照与比对结论。

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
| 某响应结果的响应体整体移除 | 🔴 breaking | `response_body_removed` |

以上规则对 200 与 4xx/5xx/default 每一种响应结果**分别**适用。

### 端点级

- 新增端点：`added` / `endpoint_added`（独立的第三档，表示纯新增）；
- 删除端点：🔴 breaking / `endpoint_removed`；
- 端点标记 deprecated：🟢 compatible / `endpoint_deprecated`。

报告中的变更按 `(severity 秩, path, kind)` 固定排序，因此同一对版本
**无论重复多少次、从哪个入口触发，结论逐字节一致**。

## 3. 发布门禁

### verdict（三档总体结论）

| verdict | 条件 |
| --- | --- |
| `pass` | 无任何破坏性变更 |
| `warn` | 有破坏性变更，但没有一条命中任何消费者**声明依赖的内容** |
| `reject` | 至少一条破坏性变更命中某消费者声明依赖的内容 |

### 每条破坏性变更的 finding 原因码（三种、可区分）

| reason_code | 含义 |
| --- | --- |
| `breaking_consumer_affected` | 命中某消费者真正依赖的内容；finding 的 `consumers` 列出命中的消费者 |
| `breaking_operation_only` | 破坏所在操作有消费者调用，但没有任何消费者声明依赖被改的那段内容（只用了该操作的其它响应/字段） |
| `breaking_unused` | 所在操作没有任何消费者登记使用 |

同一份报告里三种原因码可以同时出现，按 `(reason_code, path, kind)` 固定排序，
`consumers` 名单按消费者名排序。

> 一条破坏在同一次判定中只产生一条 finding，取“最严重”的一档：只要有任一消费者
> 声明依赖命中该变更即为 `breaking_consumer_affected`，`consumers` 只列出真正命中
> 依赖的消费者（仅调用该操作、未命中依赖的消费者不在此列，其视角可由
> `breaking_operation_only` 场景单独复现）；否则若操作有消费者调用则为
> `breaking_operation_only`；再否则为 `breaking_unused`。

### 消费者登记的两种格式（老格式完全兼容）

**老格式（操作级，行为与历史版本逐字一致）**

```json
{"operations": ["GET /users"]}
```

只要破坏发生在被登记的操作上（无论哪个响应、哪个字段、还是请求侧），一律判
`breaking_consumer_affected`。

**新格式（字段级依赖；`operations` 仍需给出，可与 dependencies 并存）**

```json
{
  "operations": ["GET /users"],
  "dependencies": [
    {"operation": "GET /users", "response": "200", "fields": ["id", "name"]},
    {"operation": "GET /users", "response": "404", "fields": ["message"]},
    {"operation": "GET /users", "response": "409", "fields": []}
  ]
}
```

* `fields` 列出真正读取的响应字段（支持点分嵌套，如 `address.city`）；
* `fields` 为空数组/缺省表示“依赖这一整个响应结果”；
* 消费者没用到的内容即使发生破坏，也只记 `breaking_operation_only`（操作有调用），
  不会因此 reject；
* 请求侧破坏（请求参数、请求体必填性、端点删除）对“会调用该操作”的消费者必然成立，
  因此只要该消费者登记了该操作即命中。

### 依赖登记的显式校验（绝不悄悄放行）

字段级依赖必须能在登记时的**最新已登记基线**上解析，否则返回 422
`incomplete_contract`，消息前缀可区分：

| 消息前缀 | 触发场景 |
| --- | --- |
| `consumer_dependency_requires_baseline` | 该服务还没有任何基线，无法校验依赖 |
| `unknown_consumer_operation:<op>` | 依赖的操作在契约中根本不存在 |
| `unknown_consumer_response:<op>:<status>` | 操作存在，但该响应状态码未声明 |
| `unknown_consumer_field:<op>:<status>:<field>` | 响应存在，但该字段路径不存在 |

### 冲突与确定性

* 同名消费者 + 规范化后期望一致（依赖条目/字段顺序无关）→ 幂等成功；
* 同名消费者 + 期望不一致 → **409 `consumer_conflict`**，绝不悄悄合并；
* 结论只取决于“消费者集合 + 依赖集合”，与消费者数量、每人声明条数、登记先后
  完全无关；同一对版本重复判定结论逐字节一致。

## 4. 基线保留与冲突处理策略

### 保留策略（本地存储）

```
<storage-root>/<service>/baselines.json      # 版本基线索引（保留全部版本，不做覆盖回收）
<storage-root>/<service>/consumers.json      # 消费者期望索引
<storage-root>/<service>/.locks/*.lock       # 跨进程文件锁
```

- 基线一旦写入**永久保留**，同版本不会被再次写改；列表按语义版本排序
  （`1.0.10 > 1.0.2`）。
- 持久化 JSON 带 `schema_version`（当前为 2），读取时校验；无法识别 / 损坏抛
  `storage_corruption`。**schema v1 的旧数据文件仍可直接读取**：旧的单成功响应
  字段集合自动归一化为 `200` 响应、旧消费者（无 dependencies）按老格式处理。

### 一致性保证

- **原子写**：临时文件 + `fsync` + `os.replace`，读方只看到完整的旧版或新版，不存在部分写入。
- **双重锁**：`fcntl.flock(LOCK_EX)`（跨进程）+ `threading.Lock`（同进程线程）
  串行化“读—改—写”；锁文件独立于数据文件，避免 replace 导致锁丢失。

### 冲突策略（显式拒绝，绝不静默合并/覆盖）

- 同版本 + 同指纹重复登记 → **幂等成功**，不改写 `created_at`；
- 同版本 + 不同指纹 → **409 `baseline_conflict`**，消息同时给出双方指纹前缀；
- 消费者同名 + 规范化后操作集合与字段级依赖都一致（顺序无关）→ 幂等成功；
- 消费者同名 + 操作集合或依赖不一致 → **409 `consumer_conflict`**；
- 乐观并发参数 `expected_fingerprint` 不满足 → 同样返回 `baseline_conflict`。

## 5. HTTP API

| 方法 & 路径 | 说明 |
| --- | --- |
| `POST /services/{service}/baselines/{version}` | 登记基线（body 为契约文档；可带 `?expected_fingerprint=`） |
| `GET  /services/{service}/baselines` | 按语义版本排序列出基线 |
| `POST /services/{service}/consumers/{consumer}` | 登记消费者，body：`{"operations": [...]}`（老格式）或额外带 `"dependencies": [{"operation","response","fields"}]`（新格式，字段级依赖） |
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

# 老格式消费者（只登记操作，行为不变）
curl -X POST localhost:8000/services/shop/consumers/web \
     -H 'Content-Type: application/json' -d '{"operations":["GET /users"]}'

# 新格式消费者（字段级依赖：只读 200.id 与 404.message）
curl -X POST localhost:8000/services/shop/consumers/app \
     -H 'Content-Type: application/json' -d '{
       "operations": ["GET /users"],
       "dependencies": [
         {"operation": "GET /users", "response": "200", "fields": ["id"]},
         {"operation": "GET /users", "response": "404", "fields": ["message"]}
       ]}'

curl localhost:8000/services/shop/release-check/1.0.0/2.0.0
```

门禁 finding JSON 示例（三种原因码、含受影响消费者名单）：

```json
{
  "verdict": "reject",
  "findings": [
    {"reason_code": "breaking_consumer_affected", "consumers": ["mobile"],
     "operations": ["GET /users"],
     "change": {"path": "GET /users#response-200.name", "reason_code": "response_field_removed", "..." : "..."}},
    {"reason_code": "breaking_operation_only", "consumers": ["web"],
     "operations": ["GET /users"], "change": {"..." : "..."}},
    {"reason_code": "breaking_unused", "consumers": [],
     "operations": ["GET /orders"], "change": {"..." : "..."}}
  ]
}
```

测试覆盖矩阵：并发登记（线程 + 跨进程冲突）、同一对版本重复判定/响应乱序/
消费者乱序与数量无关的一致性、多种响应结果（新增=added / 删除 / 字段改动原因码可区分）、
请求体整体必填 vs 字段必填原因码区分、请求/响应两侧各类破坏性与兼容变更、
枚举/可空/默认值语义、字段级依赖三档门禁（affected/operation_only/unused）、
依赖指向不存在操作/响应/字段的可区分拒绝、老格式消费者行为不变、同名消费者冲突、
畸形契约的可区分拒绝原因、损坏存储归一化（含 schema v1 旧文件读取）、
HTTP 与库调用入口结论一致性。
