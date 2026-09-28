# L6：当前实现

复核：2026-09-27。本文描述当前分支实际代码与公开表面。L6 可以随重构变化，但不得反向覆盖 [L2 系统不变量](INVARIANTS.md)、[L3 领域模型](DOMAIN_MODEL.md) 或 [L4 行为合同](FOUNDATION.md)。

## 版本与运行形态

- Python package：`agent-world` 0.16.0，要求 Python 3.11+。
- Runtime protocol 常量：0.15；SDK API：1。
- 持久存储：file-backed SQLite，WAL，foreign keys 开启；`:memory:` 被拒绝。
- 同一数据库写事务由 SQLite `BEGIN IMMEDIATE` 与进程内 RLock 协调；SQLite 同时只允许一个实际 writer。
- World Definition 以受信任、同步 Python 回调在 Runtime 进程内执行。
- 组合入口可同时挂载 Web 产品原型、HTTP API、MCP、timer worker 和 retention maintenance。
- Runtime 本身不调用模型，也没有用户 Agent 执行器。

## L3 语义到当前代码的映射

| 语义概念 | 当前实现映射 |
| --- | --- |
| User Identity | Ed25519 身份公钥；同一公钥跨世界表示同一底层身份。Runtime 通过 challenge signature 验证私钥持有。 |
| Participant Profile | `identity_keys(universe, public_key) → role_id` 映射到 `roles`；同一公钥在不同 World Instance 可拥有不同本地档案。 |
| Credential | `identity_tokens`；在持钥证明后由世界签发并绑定 `role_id + universe`，access mode 为 `control` / `observe`。它不是 User Identity 密钥。 |
| World Definition | `WorldDefinition`。 |
| World Instance | `universe` 字符串及所有按 universe 分区的持久记录。 |
| Query / Command | `FunctionSpec(access="read" / "write")`。 |
| State Fact | `world_state` + `StateRule` / state_authorizer。 |
| Operation | 调用方 `operation_id`，持久化到 `operations.idempotency_key`。 |
| Receipt | `operations.receipt_json`。 |
| Commit | `world_commits` 及关联 `state_changes`。 |
| Notification Event | `events` / `EventSpec`。 |
| Shared Stream | `StreamSpec` / `StreamEvent` / `stream_events`。 |
| View | `ViewSpec` / `view_checkpoints`。 |
| Sync Position | recipient event seq、signed stream cursor、opaque view checkpoint。 |
| Control Lease | `activities` + `activity_claims`。 |
| Scheduled Effect | `TimerSpec` / `world_timers` / `timer_transitions`，执行 actor 为 `system:timer`。 |
| Presentation | `PresentationCue`，event kind 为 `world.presentation`。 |

## Identity 当前状态

User Identity 的现行根事实是 Ed25519 公钥。客户端先调用 key identity challenge 接口；Runtime 返回一次性 nonce 和要签名的精确字节串。客户端用对应私钥签名后提交，Runtime 验签成功才承认“当前调用者持有这把身份密钥”。

`identity_keys` 按 `universe + public_key` 保存本世界映射。世界第一次见到某公钥时，在同一 SQLite 写事务里创建新的 `roles` 档案、保存映射并签发本地 `identity_tokens` Credential；以后同一公钥再次完成持钥证明时回到原来的本地 role。不同公钥永远创建新的 User Identity / 本地档案，不存在换钥继承、身份合并或密钥找回映射。

Participant Profile 完全由世界本地保存。身份公钥不会携带 display name、avatar、资产、关系或其他世界档案；同一公钥在 World A 与 World B 可以对应不同 `role_id`、不同本地资料和不同业务权限。

`identity_key_challenges` 保存短期 challenge、public key、过期时间和已签发的本地 token id。相同 challenge 在网络响应丢失后可重放并恢复同一个 world-local Credential；challenge 不能把身份映射到另一公钥。当前身份 challenge 最长 300 秒。

当前 world-local identity token 使用 `awid_` 前缀并只绑定 `role_id + universe`；不同世界不能互相接受 bearer token。Token revoke / rotate 只管理本地 Credential，不改变 User Identity 公钥。Join Ticket 仍是 operator 管理的本地角色接入工具，不定义 User Identity。

**密钥丢失没有恢复路径。** 没有原私钥就不能再次完成该公钥的持钥证明；旧本地档案可以作为历史事实保留，但对该用户而言成为不可再控制的死档。Runtime 当前没有、也不计划隐式提供密钥恢复、换钥继承或人工“认人”接口。

## 主要模块地图

| 责任 | 当前模块 |
| --- | --- |
| 包入口／CLI | `__init__.py`、`__main__.py`、`application.py` |
| 应用生命周期／加载 | `asgi_lifecycle.py`、`universe_loader.py`、`builtin_worlds.py` |
| Runtime 组合与 identity core | `runtime_core.py` |
| Command / Query 执行 | `runtime_functions.py`、`world_context.py` |
| 状态 journal / commit | `runtime_journal.py` |
| 定向通知 | `runtime_events.py` |
| Control Lease | `runtime_activities.py` |
| View | `runtime_views.py`、`world_views.py` |
| Shared Stream | `runtime_streams.py`、`world_streams.py` |
| Scheduled Effect | `runtime_timers.py`、`world_timers.py`、`timer_worker.py` |
| Retention | `retention.py`、`maintenance.py` |
| 作者 SDK 声明 | `world_sdk.py`、`world_types.py` |
| Presentation | `presentation.py`、`web/stream-client.js` |
| 共享 schema / errors | `runtime_contracts.py`、`runtime_errors.py`、`errors.py` |
| Gateway | `transport_contracts.py` |
| HTTP / MCP adapters | `http_app.py`、`mcp_app.py` |
| Identity / onboarding adapters | `identity_auth.py`、`identity_admin.py`、`onboarding_app.py`、`product_app.py` |
| Web 安全／诊断 | `web_safety.py`、`diagnostics.py` |
| Web prototype | `agent_world/web/` |
| 仓库内 demo/test consumers | `demo_universe.py`、`commons_universe.py`、`world_zero_universe.py`、`examples/` |

仓库内 demo、示例和 Reference Application 都不是架构来源。只有仍被当前测试执行的场景才提供当前证据；失去当前用途的消费者直接删除。

## 当前 SQLite schema

当前 schema `user_version` 为 7。主要表：

- identity / entry：`roles`、`identity_keys`、`identity_key_challenges`、`role_world_presence`、`identity_tokens`、`join_tickets`、`identity_token_rotations`。
- world registry：`world_definitions`、`function_registry`。
- current state / operations：`world_state`、`operations`。
- commit journal：`world_commits`、`state_changes`、`state_history_floors`。
- recipient events：`events`、`event_floors`。
- coordination：`activities`、`activity_claims`。
- timers：`world_timers`、`timer_transitions`。
- shared streams：`stream_events`、`stream_heads`。
- view cache：`view_checkpoints`。
- runtime metadata：`runtime_meta`。

表名只是 L6 存储选择，不等于 L3 概念必须一一对应。

## 当前公共 Python SDK

`agent_world.__all__` 当前导出：WorldRuntime、WorldDefinition、FunctionSpec、StateRule、RetentionPolicy、PresentationCue、StreamSpec、StreamEvent、ViewSpec、TimerSpec、TimerInvocation、RetryTimer、FunctionContext、FunctionOutcome、EventSpec。

WorldDefinition 当前可声明 functions、state rules、state authorizer、bootstrap/initialize/migrations、views、timers、retention、streams 等；这些可选能力不是所有世界的必选模型。FunctionContext 对世界作者暴露受管 state / timer / stream / role 等 helper，不再暴露公共数据库连接。

## 当前 core tool surface

固定 core tools 当前包括：

- entry/discovery：`world.bootstrap`、`world.describe`、`world.discover`。
- receipt：`world.get_receipt`。
- recipient events：`world.get_changes`、`world.wait_changes`。
- activities：`world.start_activity`、`world.claim_activity`、`world.renew_claim`、`world.finish_activity`。
- views：`world.list_views`、`world.view_snapshot`、`world.view_sync`、`world.view_timeline`。
- streams：`world.list_streams`、`world.read_stream`、`world.wait_stream`。

WorldDefinition 中声明的 functions 另外动态映射为可调用 world tools。HTTP adapter 映射到同一 Gateway 语义；完整 route 以 `http_app.py` 为代码事实。

## 当前 HTTP / 产品入口

HTTP Runtime API 包含 health/whoami、function discovery/invoke、bootstrap/describe、views、streams、public views/streams、receipt、activities、changes 等路径。onboarding adapter 公开 key identity challenge / exchange，用于“公钥 + 持钥签名 → 本世界档案 + world-local Credential”；operator 管理面另外保留 role、join ticket、token rotate/revoke 与 rotation receipt。鉴权请求的主体只来自 credential，`role_id` 不属于鉴权调用参数。

这些路径是当前 L6 表面，不应被 L1-L3 当成概念定义。

## 当前关键上限

| 项目 | 当前值／范围 |
| --- | --- |
| structured arguments | 64 KiB |
| 单个 state value | 256 KiB |
| result / event batch budget | 256 KiB；单次 outcome 最多 128 events |
| managed state changes per commit | 512；历史前后内容合计 2 MiB |
| history page payload | 1 MiB |
| WorldDefinition declarations | views ≤64，timer handlers ≤64，streams ≤64 |
| View | ≤256 entities、≤64 resources、总 payload 96 KiB |
| View checkpoint | TTL 300s；每 viewer/universe 16；全局 1024 |
| Stream | read page ≤100 events，page payload 192 KiB；cursor lifetime 86400s |
| Stream declaration | retention ≤365d，max_events ≤100000 |
| wait | 当前最大 30s |
| timer commands per transaction | 32 |
| timer argument payload | 64 KiB |
| pending timers per universe | 10000 |
| timer retries | max_attempts 1..10，retry delay ≤86400s |
| retention config | event/history seconds ≤315360000；row limits ≤10000000 |

这些数值可能同时具有“当前公共行为限制”和“实现容量保护”两种性质。修改前需要判断是否会改变 L4 客户端可观察合同。

## worker 与 maintenance

组合入口默认 timer poll interval 1s，可关闭；TimerWorker 默认 batch 25，interval 最低 0.05s。Retention maintenance 在世界声明 retention 或 streams 时启动后台线程，当前每 30s sweep 一次。

也可以分别运行 timer worker 和 retention maintenance CLI。导入 package 本身不会安装常驻系统服务。

## Presentation 当前映射

`PresentationCue` schema version 为 1，channel 当前为 `action / speech / intent`，phase 为 `start / finish / cancel`，单 cue payload 上限 16 KiB。它可以生成定向 EventSpec 或发布为 StreamEvent。

timeline 复用 View checkpoint，只返回当前 viewer 获准且 subject 当前可见的 presentation events。这个机制是表现辅助，不是业务动作状态机。

## World Package 数据访问边界

`FunctionContext` 不再公开数据库连接。World Package 的受支持数据路径是声明过的 state / timer / stream / role helper；实现内部仍持有私有 transaction handle 以完成这些操作，但它不是 SDK 合同。

当前 World Package 仍是部署方信任、与 Runtime 同进程执行的 Python 代码，因此这项收缩只是减少数据库形状耦合，并 **不把当前架构升级成恶意插件沙箱**。若未来要执行互不信任的第三方世界代码，仍需要独立的进程／权限／存储或真正的沙箱边界。

## 结构化错误与 Capability Harness

当前实现包含通用结构化恢复错误合同：

- `runtime_errors.py` 的 `WorldRuntimeError` 可携带可选 `recovery`、`retry_after_seconds` 与 `details`；显式 `retryable` 必须是 boolean。
- `details` 当前要求为 JSON object，递归深度有界，序列化后最多 8 KiB；非有限数字、非字符串 key 和任意 Python 对象会在构造错误时被拒绝。
- `transport_contracts.py` 将相同字段映射到 HTTP/MCP 结构化错误；未知内部异常仍被清洗为通用 `InternalError`。
- HTTP 在存在 `retry_after_seconds` 时额外发送整数秒 `Retry-After`；401 仍只在鉴权错误路径发送 `WWW-Authenticate: Bearer`。
- 常见恢复提示已覆盖缺少／失效身份、scope 不匹配、无效输入、Join Ticket 失效、函数版本／发现变化、observe/control 不匹配和 StorageBusy；世界规则也可在 `RuleViolation` 等 Runtime error 上附带安全的恢复元数据。

当前仓库使用 `tests/capability_harness.py` 和 `tests/fixtures/capability_matrix_world.py`。它们是 **确定性的 Runtime 测试基础设施，不是新的 Reference Application 或领域模型**。当前矩阵用 6 个动态参与者覆盖：

- 1→N 定向 fan-out，并验证同 Operation 重放与 Runtime 重启后 Receipt／事件仍可恢复。
- N→1 并发 fan-in，验证多个写者向同一目标聚合且非目标主体不能读取私有聚合。
- N→N mesh，验证多主体同时 fan-out；撤销其中一个凭据后，该主体立即失效而其他主体继续运行。
- HTTP/MCP 对同一个带 recovery/retry/details 的规则错误保持结构化语义一致。

这批 fixture 不创建 Conversation、Friend、Party 或其他产品对象，因此不能反向定义 Runtime 领域模型。以后新增拓扑／故障组合时优先扩展 Harness，而不是为每个实验重新造一个大场景。


## 当前明确实现差距

1. **外部系统副作用交付没有通用合同。** Runtime store 内部提交不能自动覆盖支付、第三方 API、文件等外部写入；尚无统一 outbox / delivery / compensation 能力。

共同事项模型、第三方／群众公裁等仍属于 [OPEN_DESIGN](OPEN_DESIGN.md) 的产品／领域设计问题，不能因为“尚未实现”就假装它们已经被证明应该进入 Runtime。

长期 soak、浏览器轨迹、更多平台矩阵属于验证缺口而不是 L6 功能缺口，统一记录在 [REFERENCE_GATE](REFERENCE_GATE.md)。
