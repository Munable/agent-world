# 六层基线与仓库入口

复核：2026-09-30。六层的目的不是增加审批，而是避免目标、原则、模型、合同、架构、代码和测试再次互相冒充。

## 当前仓库快照

当前工作树代表现行事实：

- Python package：`agent-world` **0.18.0**；Runtime protocol **0.15**；SQLite schema `user_version` **7**。
- User Identity：**只认密钥，不认人**；Key Identity 已是组合应用默认公开入场路径。
- Runtime Capability Harness：正式 Key Identity 参与者下已覆盖 1→N、N→1、N→N、并发 CAS、撤权后持钥重进、重启回档、多 session、独立 Runtime 隔离和批量进入。
- Commons Reference Application v2：当前只定义 `Post / Reply / Conversation / Message`，用于验证公开／私有、持久历史、参与者授权、通知与恢复；这些对象不属于 Runtime 固有模型。
- 故障／恢复：Operation replay、Runtime 重启、离线恢复、Credential 失效后同钥重进、event cursor 续接、HTTP/MCP 混合调用均有当前回归。
- Real Agent Integration：OpenCode 与 Pi 已在本机独立进程中通过 MCP 自主发现并调用 Commons；OpenCode 新进程可恢复离线期间事实。该证据是本机 loopback 短时实验，不承担身份鉴权或跨机器／soak 证明。
- 最近记录的确定性证据：**227 个 unittest 全部通过**；0.18.0 wheel 构建、独立安装与 checkout 外 package probe 已通过。
- 当前唯一已确认的 Runtime 实现缺口：**外部系统副作用交付**（outbox / delivery / compensation）。
- 当前主要证据缺口：跨机器／跨主机 Agent、网络分区、长期 soak、生产数据库迁移和真实用户使用。

## 六层语义主轴

| 层级 | 只回答一个问题 | 当前权威入口 |
| --- | --- | --- |
| L1 产品目标 | Agent World 要成为怎样的产品？ | [PRODUCT_POSITIONING](../PRODUCT_POSITIONING.md) |
| L2 系统不变量 | 不论技术怎样变化，哪些性质必须成立？ | [INVARIANTS](INVARIANTS.md) |
| L3 领域模型 | 系统里有哪些语义概念，它们是什么关系？ | [DOMAIN_MODEL](DOMAIN_MODEL.md) |
| L4 行为契约 | 什么行动合法，什么条件下形成什么结果，失败与恢复意味着什么？ | [FOUNDATION](FOUNDATION.md) 及能力合同 |
| L5 技术架构 | 哪些技术责任由哪些组件承担，信任与依赖边界在哪里？ | [ARCHITECTURE](ARCHITECTURE.md) |
| L6 具体实现 | 当前版本具体用什么代码、存储、接口和参数兑现架构？ | [IMPLEMENTATION](IMPLEMENTATION.md) 与源码 |

上层约束下层，但不是瀑布审批。实现、测试和 UX 可以发现上层设计有问题；真正的问题必须显式回到所属层修改，不能靠改测试或改文案偷偷重定义语义。

## 内容归层判定

1. 如果一句话在更换语言、数据库和传输后仍必须成立，它通常属于 L1-L4，而不是 L6。
2. 如果一句话描述“系统里是什么东西”，先放 L3；如果描述“它什么时候允许变化”，放 L4。
3. 如果一句话描述组件责任、信任边界、依赖方向或进程职责，放 L5。
4. 文件名、类名、表名、端点、具体数值和当前版本通常属于 L6；只有当它们本身就是对外稳定合同的一部分时，L4 才描述其语义要求。
5. 测试结果、示例项目和临时消费者永远不能单独创造 L1-L5 的要求。

## 横轴 A：验证证据

测试、实验、运行观察、兼容性探针和长期运行记录不是第七层。它们只能说明某条声明在某个源码、环境和场景下得到了什么证据。

验证失败后必须先分类：实现缺陷、行为合同含糊、领域模型缺项、测试假设错误、环境问题，或上层目标／不变量确实需要修改。不能为了让测试变绿，把原语义改成当前代码碰巧做得到的样子。

关键跨层主张及当前证据状态见 [TRACEABILITY](TRACEABILITY.md)，测试范围和证据规则见 [REFERENCE_GATE](REFERENCE_GATE.md)。

## 横轴 B：交互与表现

交互／表现负责让人和 Agent 正确理解已经存在的模型与事实，但不拥有独立业务权威。文案、动画、缓存、模型总结和读取进度可以表达世界事实，不能创造世界事实。具体规则见 [PRESENTATION](PRESENTATION.md)。

## 未决设计

[OPEN_DESIGN](OPEN_DESIGN.md) 不是第七层。每个未决问题都必须标回真正所属的 L1-L6 或横轴；决定前不得写成现行合同。

## 现行文档职责

| 文档 | 位置 | 唯一职责 |
| --- | --- | --- |
| PRODUCT_POSITIONING | L1 | 产品目标与产品责任边界。 |
| INVARIANTS | L2 | 技术变化后仍必须成立的系统性质。 |
| DOMAIN_MODEL | L3 | 语义概念、关系与非等价关系。 |
| FOUNDATION | L4 | Runtime 核心行动、状态、提交、恢复、身份与版本语义。 |
| AGENT_INTERACTION | L4 | 外部 Agent 的交互、回应、等待和断线接续语义。 |
| WORLD_DATA | L4 | 权威数据、授权投影、同步与缓存语义。 |
| OBSERVATION_STREAMS | L4 | 公开观察、共享流、现场／历史读取语义。 |
| DURABLE_TIME | L4 | 持久定时事项的调度、执行、失败和版本语义。 |
| RETENTION | L4 | 数据保留、清理与恢复边界。 |
| ARCHITECTURE | L5 | 组件责任、依赖、事务与信任边界。 |
| IMPLEMENTATION | L6 | 当前代码、数据库、公开表面、限制与已知实现差距。 |
| PRESENTATION | 横轴 | UI／客户端怎样忠实表达已存在事实。 |
| TRACEABILITY | 证据横轴 | 核心跨层主张当前实现／证据状态。 |
| REFERENCE_GATE | 证据横轴 | 测试与实验什么能证明、什么不能证明。 |
| OPEN_DESIGN | 附着项 | 尚未定稿的模型、合同、架构或实现问题。 |

示例世界、Reference Application 和临时测试消费者都不能覆盖上述现行入口；失去当前验证用途的材料直接删除，不在工作树维护历史副本。

## 两个典型问题

### “确认”不是一句文本

L2 要求意愿必须可归因；L3 区分 Operation、Receipt、业务对象和观察记录；L4 才定义某个世界的确认行动、资格、前置状态和状态变化；L5/L6 决定组件和当前接口如何实现。UI 最后只能显示已经成立的确认事实。

### 第三方／群众公裁

L1 只要求产品能够承载世界定义的第三方决策；L2 要求争议方不能单方面伪造他人或裁决者意愿；L3/L4 在机制确定后定义概念和成立条件；L5/L6 再决定组件和实现。当前候选项留在 OPEN_DESIGN。

## 变更规则

1. 改 L1/L2：明确为什么目标或不变量本身改变，以及哪些下层材料受影响。
2. 改 L3/L4：说明旧模型／语义、新模型／语义及兼容／迁移影响；不能只改测试期待值。
3. 改 L5：说明责任、依赖或信任边界为何改变；文件拆分本身不自动算架构变化。
4. 改 L6：普通修复与重构只需保持上层合同并提供证据。
5. 改表现：不能通过文案或动画制造服务器没有形成的事实。
6. 改测试：删除或弱化已复现回归时，说明对应合同为何失效或由什么等价证据替代。

最小变更记录：**影响层、原因、行为影响、实际验证、尚未验证什么。**
