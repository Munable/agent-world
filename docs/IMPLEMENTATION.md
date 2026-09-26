# L6：当前实现

复核：2026-09-27。本文描述 **当前 0.14.0 / SDK API 1 实际采用的技术选择和代码落点**。这里的事实可以随重构改变，不得反向覆盖 [系统不变量](INVARIANTS.md) 或 [行为合同](FOUNDATION.md)。

## 当前技术形态

- Python 3.11+ 包，当前版本 0.14.0。
- 本地 SQLite 文件存储，WAL 模式，一个并发写者。
- 受信任 Python WorldDefinition 包在 Runtime 进程中同步执行。
- python -m agent_world 组合 Web、HTTP、MCP、timer worker 和保留维护；也可按模块单独运行部分入口。
- HTTP/MCP 共享 WorldGateway 与 Runtime 路径，不维护两套世界规则。
- 当前身份令牌仍绑定 role + universe；跨独立部署的统一底层身份验证尚未实现。
- Runtime 不调用模型，也没有后台 Agent 执行器。

## 模块地图

| 模块组 | 当前文件 |
| --- | --- |
| Runtime 组合核心 | runtime_core.py |
| 写入／调用 | runtime_functions.py、world_context.py |
| 状态历史／提交 | runtime_journal.py |
| 事件 | runtime_events.py |
| 活动租约 | runtime_activities.py |
| 授权视图 | runtime_views.py、world_views.py |
| 共享流 | runtime_streams.py、world_streams.py |
| 持久时间 | runtime_timers.py、world_timers.py、timer_worker.py |
| 保留 | retention.py、maintenance.py |
| SDK 声明 | world_sdk.py、world_types.py |
| 表现辅助 | presentation.py、web/stream-client.js |
| 合同／错误 | runtime_contracts.py、runtime_errors.py |
| 结构化网关 | transport_contracts.py |
| HTTP / MCP | http_app.py、mcp_app.py |
| 身份解析与管理 | identity_auth.py、identity_admin.py、onboarding_app.py、product_app.py |
| 应用组合／加载 | application.py、universe_loader.py、builtin_worlds.py |
| Web 客户端原型 | agent_world/web/ |
| 内置世界消费者 | demo_universe.py、commons_universe.py、world_zero_universe.py |
| 外部示例消费者 | examples/ 以及独立 Lantern Hollow / Ashen Vault 仓库 |

根目录短同名模块与旧脚本目前仍承担兼容／回归用途；不为了目录整齐擅自删除公开入口。

## 当前持久记录

当前代码创建的主要 SQLite 表包括：

- roles、role_world_presence、identity_tokens、join_tickets
- world_definitions、function_registry、world_state
- operations、world_commits、state_changes、state_history_floors
- events、event_floors
- activities、activity_claims
- world_timers、timer_transitions
- stream_events、stream_heads
- view_checkpoints
- runtime_meta

这些表是 L6 的实现选择，不是 L3 领域模型的同义词。例如 operations 表存在不代表任何世界业务对象都必须叫 Operation；view_checkpoints 也不代表 checkpoint 是业务确认。

## 公共 Python SDK

当前包公开 WorldRuntime、WorldDefinition、FunctionSpec、StateRule、RetentionPolicy、PresentationCue、StreamSpec、StreamEvent、ViewSpec、TimerSpec、TimerInvocation、RetryTimer、FunctionContext、FunctionOutcome 和 EventSpec。

公开类型的存在只表示当前 SDK 可用能力；可选类型不是每个世界的强制对象。

## 当前已知差距

1. 产品要求用户持有跨世界通用身份；当前 token 验证仍绑定单个 universe，独立部署的统一验证没有落地。
2. 通用的多方共同事项／确认／完成协议仍未定稿；具体世界可以用现有函数与状态实现，但 Runtime 不应声称已有统一对话状态机。
3. 第三方／群众公裁尚无正式领域模型与行为合同。
4. 外部网络、付款、文件等副作用没有与 SQLite 事务统一的 exactly-once 保证。
5. 长期运行、多主机真实 Agent 协作、完整浏览器视觉轨迹和全部 CI 平台组合仍需要继续验证。

待决策项只在 [OPEN_DESIGN](OPEN_DESIGN.md) 维护；已知实现缺口不能通过修改上层定义“消失”。

## 当前分支的状态校验修复

本整理分支恢复了 managed WorldDefinition 在提交前对 legacy raw-state 写入的 schema、版本和 state_authorizer 校验，并适配 authorization_state 与迁移最终状态语义。范围和限制见 [FOUNDATION](FOUNDATION.md) 及带日期的仓库审计。

这项修复仍不把受信任 Python 代码变成恶意插件沙箱，也不承诺特权管理连接、直接文件修改或任意外部副作用享有同样边界。

实现事实变化时优先更新本文；只有行为语义、领域模型、不变量或产品目标真的改变时，才继续向上修改对应层。

## 身份与接入实现细节

当前 Role Core 字段包括 role_id、display_name、avatar_ref、status 与时间字段。身份 token 使用 awid_ 前缀，Join Ticket 使用 awjt_ 前缀，数据库保存 token 哈希而非明文。

Join Ticket 当前绑定 role + universe，配置的最长有效期为 24 小时；一次 ticket 最多发一个凭据，并可在有效期内恢复同一个交换结果。角色创建、资料修改、ticket/token 发放与撤销目前由操作员入口负责。

当前 access mode 为 control / observe：control 可以调用获准写函数，observe 只能读取获准信息。现有 operator key / Web Basic Auth 只是管理原型。

MCP 会话绑定初始化凭据，凭据轮换后需新会话。轮换响应丢失的完整自助恢复尚未实现。

## 当前接口与运行入口

- python -m agent_world：组合 Web、HTTP、MCP、timer worker 与 maintenance。
- python -m agent_world.timer_worker：可独立运行 timer worker。
- python -m agent_world.maintenance：可独立执行一次有界保留清理。
- Web / HTTP / MCP 在组合入口中使用同一 origin；公开 URL 和 Host/Origin 防护由部署配置。
- world.get_receipt 对应当前 HTTP receipt 查询入口；view、stream 也分别有 HTTP 与 MCP 映射。具体路径以 transport_contracts.py / http_app.py 为准，不能从某个 adapter 复制第二套业务语义。

## 当前可见限制

现有实现包含多项有界限制，例如一次 managed 提交的状态变化数量与历史体积、view entity/resource 规模、checkpoint 数量与寿命、stream payload/page 大小、wait 最长时间、一次 timer 事务变化数及 pending timer 数。

这些限制中，凡是会决定客户端请求是否有效的值属于 L4 可观察合同；内部缓存布局和表结构属于 L6。修改数值时先判断它究竟是公共行为限制还是纯实现调优，不能一概当成“内部参数”。
