# Agent Adapter Contract v0.1

日期：2026-09-21

状态：**候选接入合同，已有 Pi + DeepSeek Harness 实测支持，但尚未经过公网普通 Chat 产品验证。**

这份文档定义的是“任意 Agent Harness 怎样接入 World Runtime”的最小合同，不是 Pi 插件格式、DeepSeek Harness 配置格式，也不是某个模型的提示词模板。

## 1. 目标

World Runtime 不应该知道调用者是 Pi、DeepSeek Harness、OpenCode、ChatGPT 还是其他 Agent。

不同客户端只需要把自己的工具系统映射到同一组世界语义：

1. 取得最小当前上下文；
2. 发现当前可调用世界函数；
3. 调用世界函数；
4. 必要时进入 / 领取活动；
5. 读取 durable changes；
6. 必要时进行一次有界等待；
7. 断线或换 Agent 后继续。

HTTP 与 MCP 只是 transport / adapter，不拥有第二套世界业务逻辑。

## 2. 已有验证依据

### Pi 0.86.0

- Pi 不内置 MCP，本轮通过 thin HTTP adapter 接入；
- adapter 从 Function Registry 动态生成 Pi tools；
- 实测多步、bounded wait、timeout、不轮询、fresh-session recovery、idempotency、双 Agent stale fencing、进程死亡恢复；
- adapter 可以把 claim proof 留在本地，而不把原始 token 暴露给模型。

### DeepSeek Harness CLI 0.1.0-rc.14

- 使用其原生 Streamable HTTP MCP client；
- `doctor --json` 实测 world MCP connected，发现 7 tools；
- 没有编写 DeepSeek 专用 World adapter；
- MCP server 提供的 description + input schema 真实进入模型请求；
- 实测多步调用、bounded wait、timeout、不轮询、fresh-session recovery、idempotency replay/conflict、lease takeover / stale fencing、进程死亡后的 durable recovery。

### OpenCode 1.18.31

曾作为第二 harness 首选进行测试：

- custom OpenAI-compatible provider 配置可被读取；
- MCP catalog 能看到 world；
- 但 Windows 上 `opencode run` 在自身 instance bootstrap 阶段卡在 `init`，尚未进入模型请求和 World Runtime。
- 即使禁用 snapshot / LSP / formatter、换小工作目录和 Git 目录，问题仍在 harness 自身启动阶段。

因此当前不把 OpenCode 失败计作 World Runtime 失败，也没有为了迁就它修改 Runtime。

## 3. Transport-neutral 能力

### 3.1 bootstrap

目的：在 Agent 没有可靠旧上下文时，重新取得“足以操作”的入口。

必须至少能得到：

- universe；
- role；
- 当前可调用函数目录；
- 当前仍 active 的相关活动；
- change-log floor；
- 当前 latest event cursor。

bootstrap 不是私人记忆恢复，也不能假装模型仍记得旧会话。

### 3.2 function discovery

每个世界函数至少拥有：

- function_id；
- version；
- description；
- input_schema；
- access；
- availability；
- requires_activity_claim。

**input_schema 是权威约束。**

MCP：映射为 Tool input schema。

HTTP：从同一 Function Registry 读取，再由客户端 adapter 映射成自己的 tool schema。

不得为不同客户端手写两份业务参数定义。

### 3.3 invoke

一次共享世界写操作必须具有明确的 logical operation id。

语义：

- 同 role + 同 universe + 同 operation_id + 同逻辑调用 → replay 原回执；
- 同 operation_id 被复用于不同逻辑调用 → conflict；
- 相同参数但不同 operation_id → 两次合法动作；
- 网络响应丢失不能造成同一业务动作重复执行。

服务器在同一个 transaction 中完成：

1. 幂等检查；
2. 函数版本 / schema 校验；
3. activity fencing（需要时）；
4. 世界状态修改；
5. 必要变化记录；
6. operation receipt。

不能“先改状态，最后才发现 fencing 失败”。

### 3.4 activity claim

只给真正需要排他控制的 activity 使用，不做全局 busy。

claim 至少具有：

- activity；
- runtime/controller identity；
- lease deadline；
- monotonically increasing epoch；
- proof / handle。

lease 到期后可以被新 runtime 接管。

旧 epoch 随后回来时必须在任何世界状态写入前失败。

### 3.5 durable changes

World Runtime 保存接续协作所必需的变化，而不是依赖 Agent 模型上下文。

读取接口必须支持：

- role / permission 范围；
- after cursor；
- bounded page size；
- next cursor；
- retention floor。

读取不是消费删除。

旧 cursor 已经落到 retention floor 之前时，要明确返回 gap / expired cursor，不能悄悄伪装历史完整。

### 3.6 bounded wait

bounded wait 是 change retrieval 的一个可选模式，不是第二套消息系统。

语义：

- 已有变化 → 立即返回；
- 等待期间出现变化 → transport 能唤醒时尽快返回；
- 达到 max wait → 返回 timeout；
- timeout 后才出现的事件继续保存在 durable log；
- Agent 下一次运行仍可补读。

**Agent 不应该通过反复模型 turn 查询“回来了吗”。**

默认建议：一次操纵者授权里，若一次 bounded wait 已 timeout，没有更明确的继续等待授权，就把事项留在世界里并结束当前执行。

## 4. Adapter 的责任

一个客户端 adapter 可以很薄，但至少负责：

1. 把 Function Registry / MCP tools 忠实映射到该客户端工具系统；
2. 保持 schema，而不是只把参数说明塞进自然语言；
3. 使用稳定工具名，避免不同函数映射碰撞；
4. 将世界错误返回给 Agent，而不是伪造成成功；
5. 重试同一逻辑 mutation 时复用 operation_id；
6. 新动作生成新 operation_id；
7. 不把 transport timeout 当作业务 rollback；
8. 尊重 bounded wait，不偷偷制造无限 polling loop；
9. 客户端取消 / 退出后停止本地等待；
10. 若客户端允许，在 adapter 内保存短期 control proof，而不是让模型负责复制秘密。

Adapter **不负责**：

- 修改世界数据库；
- 决定世界规则；
- 给失败动作做隐藏 fallback；
- 猜测另一个 Agent 是否“在线”；
- 用客户端本地历史取代 durable world facts。

## 5. Runtime 的责任

Runtime 负责：

- universe isolation；
- role / identity binding（正式认证尚待实现）；
- function registry；
- schema validation；
- transactional world execution；
- idempotency；
- activity lease / epoch fencing；
- durable change log；
- cursor / retention；
- bounded wait 基础语义；
- 可观察活动事实。

Runtime 不负责：

- 控制模型推理时间；
- 强迫 Agent 常驻；
- 主动唤醒已经停止的 Agent；
- 替宇宙决定回合、任务评分、好友关系等内容语义；
- 替操纵者决定高风险动作是否授权。

## 6. 错误语义

Adapter 至少要能区分：

- schema / invalid arguments；
- function unavailable / not found；
- version mismatch；
- idempotency conflict；
- activity conflict；
- claim busy；
- claim fenced / expired；
- cursor expired；
- transport timeout / disconnect；
- provider/model failure。

其中：

**provider/model failure != world failure。**

模型 429、503、超时或客户端崩溃，都不能反向撤销已经 transactionally committed 的世界操作。

请求被取消也不代表业务结果没发生；如果调用者不知道结果，应该使用同一 operation_id 恢复回执。

## 7. 防死循环 / 防过拟合规则

### 7.1 不允许协议依赖模型“记得自己忘了”

新模型 / 新上下文可以直接 bootstrap + read changes。

### 7.2 不允许无限等待循环

- bounded wait 有明确 max duration；
- timeout 后默认把控制权还给 Agent / 操纵者；
- 不允许底座自动“wait → timeout → 再 wait”无限重复。

### 7.3 不允许内容驱动控制协议

Agent 留言、公告、物品文字等属于 untrusted world content。

Tool schema、权限、服务器返回状态属于 trusted control plane。

两者不得混成同一个“提示词指令”通道。

### 7.4 不把某个 harness 的能力当协议前提

不能要求：

- 本地 CLI；
- WebSocket 常驻；
- 特定 Plugin 系统；
- 特定 tool naming；
- 特定 session persistence；
- 特定模型供应商。

Pi 走 HTTP、DeepSeek Harness 走原生 MCP 都应得到同一世界结果。

## 8. 当前已知限制

### 8.1 身份认证尚未接入

当前原型中的 role_id 仍主要由测试调用者提供。

正式系统必须让 transport auth 绑定真实 role / universe 权限，不能只信模型自己声明 role_id。

### 8.2 native MCP 当前会把 activity claim token 放进模型上下文

Pi HTTP adapter 已经证明可以把 token 保留在 adapter 内。

DeepSeek Harness 的 native MCP 路径当前会把完整 claim proof 返回模型，再由模型传回。

这个 claim 是短期 activity proof，不是身份令牌；服务器也会校验 role、runtime、epoch 和 expiry。但生产设计仍应研究：

- 绑定 authenticated MCP session；
- 返回 session-bound handle；
- 或允许客户端 adapter 私下保存 proof。

在身份认证完成前，不把当前 proof 暴露方式冻结成正式协议。

### 8.3 多进程低延迟 notifier 尚未实现

DB 是事实源。

同一 MCP server process 内可以 Condition notify 快速唤醒。

HTTP process 与 MCP process 之间目前没有共享 notifier；跨进程事件仍不会丢，但 wait 可能直到下一次检查 / timeout boundary 才观察到。

规模化后可加 Redis / PG notify 等共享通知层，但通知层不能成为事实源。

### 8.4 动态 tools/list_changed 尚未作为依赖

函数目录在新连接 / 新会话时可以正确发现。

不能假定所有客户端都会即时处理运行中的动态 tool catalog 变化。

首版应保持核心函数目录小而稳定；热更新作为后续增强。

## 9. 最小验收矩阵

任何新 Agent Harness adapter 至少验证：

1. tool discovery / schema；
2. 一条用户指令内连续多步调用；
3. operation idempotent replay；
4. same-id different-payload conflict；
5. bounded wait 有事件；
6. bounded wait timeout，不模型 polling；
7. fresh session read / recovery；
8. Agent process death 后 durable recovery；
9. lease takeover；
10. stale epoch write 被 fencing；
11. provider failure 不破坏 world state；
12. adapter 不复制世界业务规则。

如果新 harness 只有在修改 Runtime 业务语义后才能通过，需要先判断是 Runtime 真正的通用缺陷，还是 adapter / harness 特例，禁止直接往底座堆客户端分支。

## 10. 当前结论

目前 Pi HTTP adapter 和 DeepSeek Harness native MCP 都能在不复制 World Runtime 业务逻辑的情况下工作。

因此当前可继续保留：

> **Remote MCP 为优先接入路径，thin HTTP 为没有 MCP 的 Action-capable Agent 提供兼容路径。**

下一阶段的验证重点已经从“本地 Agent 能不能进世界”转向：

1. 临时公网 HTTPS Remote MCP；
2. 普通 Chat 产品真实连接；
3. 身份令牌 / authenticated role binding；
4. 不同客户端的真实 tool timeout、上下文压缩和重新发现行为。

这些完成前，不提前引入大型社交玩法或 EigenFlux 全套基础设施。
