# EigenFlux 对照与社交优先收敛记录

日期：2026-09-27。性质：带日期的参考分析与范围变更记录，不覆盖六层现行合同。

## 基线与证据范围

我方对照基线为 `36beab23a55ce65604dd864204a82b7671e1ed7c`（`review/layered-baseline-20260926`），不是较旧的 main。该分支已经恢复 managed-world legacy raw-state 提交校验，不再把这项已修问题列为当前缺口。

参考项目为 Phronesis 的 `phronesis-io/eigenflux`。核对官方 README、架构文档、Communication Skill 及 message／stream 参考文档，并检查 `rpc/pm/handler.go`；代码检索固定到 `3a3612d1ca78b62508f2863b51f88728d9e1d188`。不是完整安全审计，没有安装、注册账号、发送消息或运行对方服务。

旧架构页仍包含旧邮箱登录说明；身份比较采用当前 README 的本地 Ed25519 身份、Agent Home 与 Console 人工确认描述，不把旧页当当前身份协议。也不由 README 的隐私宣传推断不可泄漏的安全保证。

## 核心判断

值得学习的是清晰的接入、发现和通信闭环，不是照搬一套服务栈，也不是把 Agent World 改名成另一个 EigenFlux。

EigenFlux 的公开定位以 Agent 广播、发现和匹配为中心；其现有私信、关系与结构化授权说明它并非只有无状态推荐。Agent World 的目标是外部参与者在领域规则下接续同一权威持久世界。基础社交是当前产品切片，不等于通用 Runtime 必须内置全部社交规则。

双方共同点包括外部 Agent、结构化 API、持久信息和授权边界。不能声称 EigenFlux 只靠自然语言授权、没有持久状态、没有用户确认，或服务器托管了所有用户 Agent。

## 原则与机制对照

| 维度 | 官方材料／已读代码说明什么 | 我方取舍 |
| --- | --- | --- |
| 产品目标 | 广播信息、需求和能力，由 Hub 做匹配；Communication 准则强调具体结果、避免无效往返。 | 基础社交不强制交易或任务收益；持久交流和关系本身可以有价值。 |
| 模型边界 | Hub 有异步 LLM 内容加工及匹配。 | Runtime 不调用模型推理；将来推荐可由外部可选组件提出建议，不能直接制造授权、确认或裁决。 |
| 身份 | 当前接入使用本地 Agent identity、稳定隔离的 Agent Home 和 Console 人工确认。 | 保留用户持有、跨世界通用的底层身份目标；当前 universe 范围验证仍未完成该目标。不能由对方接入方式推断跨 Hub 身份已经通用。 |
| 已读与接续 | message 参考文档说 fetch 会标记已读；stream 文档说同账号只保留一条流连接，新连接替换旧连接。 | 不直接照搬为多客户端合同；分别定义投递、客户端读取、用户已读和显式接受，任何读取进度都不能等于业务完成。 |
| 屏蔽响应 | PM 设计与 SendPM 代码都存在被屏蔽时返回 success 且不创建消息的分支。 | 可以保护屏蔽原因，但成功字段和 UI 不得声称不存在的对方收取／同意。可接受不披露投递结果的合同，不可伪造结果。 |
| 重试 | 已读 SendPM 入口按 sender、target、content 的 fingerprint 做发送防重，返回已有 msg_id／conv_id。 | 保持我方 operation_id 标识逻辑操作的合同；不能用相同文本自动认定用户在重试，也不据此断言对方所有接口都没有幂等性。 |
| 隐私与信任 | Skill 明确说对方请求不等于许可；官方标志不允许静默替用户执行操作。README 也说明广播隐私边界依靠 Agent 指令。 | 学其清晰说明，但服务器可执行的身份、可见性、结构化授权和限流必须由代码保障；对客户端出站隐私仍需宿主侧约束，不能声称 Runtime 可识别所有敏感文本。 |
| 架构规模 | 官方栈包含 Go 微服务、PostgreSQL、Redis、Elasticsearch、etcd 和异步处理。 | 当前不为相似产品外观换栈；先证明已有 Runtime 上的社交闭环，观察到容量问题再改变部署形态。 |

## 学什么，保留什么，暂停什么

学习稳定接入与清晰操作入口、公开内容到会话的关联、关系接受／拒绝、拒收与反刷屏、按需加载的工具说明、游标续接和有界退避。限制值与一账号单连接等策略需按我方需求验证，不逐字照搬。

保留六层结构、用户身份目标、外部 Agent 边界、结构化权威、事务与回执、读取和业务完成非等价、领域规则与 Runtime 分离，以及现有修复／兼容回归。

暂停新增 RPG 剧情、职业、战斗、经济、任务链和复杂场景表现；复杂群组、推荐、信誉、交易、公裁及跨网络联邦不成为基础社交切片的隐含前置。公裁仍是待设计方向，不从产品历史中删除。

暂停功能扩张不是删除已存在的基础能力。timer、Activity、streams 等保留为可选能力；普通消息不强制用 Activity，不因收敛而移除旧世界消费者或回归测试。

## 当前代码暴露的下一步

Commons 当前提供 `commons.board.post`、`commons.board.list`、`commons.note.send`。post 是持久状态；note 主要返回定向 recipient event，没有在该模块内定义完整会话、回复关联、关系请求或业务确认模型。`delivered_to` 不能被 UI 或 Agent 解释成对方已读／已接受。

因此先补首个社交世界的 L3/L4：内容与回应关联、私有会话、关系请求与拒收、未决状态及恢复，再用现有 HTTP/MCP、函数注册表、授权、状态与回执落地。只有实际欠缺且确实通用的原语才进入 Runtime。

跨世界身份与轮换丢响应恢复仍是明确缺口；前者不能靠移除 universe 检查修复，后者不能靠文案承诺恢复。具体工作仅在 OPEN_DESIGN 维护，不在此复制第二份路线图。

## 首轮范围收敛记录

L1：在 PRODUCT_POSITIONING 明确社交优先阶段范围，暂停新增 RPG。L2：未修改。L3/L4：未发布新模型或 API，仅在 OPEN_DESIGN 列出社交切片需先解决的问题。L5/L6：未修改代码、存储、鉴权或依赖。证据横轴：新增明确标注为目标的社交验收场景。

README 只提供现行入口。本记录不宣称社交能力已经补齐、跨世界身份已经实现或真实 Agent 联调已经通过。

本次验证限于文档内容、相对链接与 git diff 检查；不以纯文档变更重跑计费 CI，没有重新执行功能、长期运行或真实外部 Agent 测试。独立游戏仓库、真实数据库、凭据和运行进程未改动。

## 开发参考来源与落点

第二轮起点为我方 `e33a2adb079cab3fc7df7d79df28a9dedbcc4415`，在同一社交收敛分支继续，不覆盖首轮范围决定。2026-09-27 再次核对对方 main 仍指向 `3a3612d1ca78b62508f2863b51f88728d9e1d188`；以下来源都固定到该提交，不随上游 main 漂移。

官方 README 声明仓库是线上服务使用的生产代码，并列出多种宿主接入；这支持把它作为已有落地实践的参考，而不是只有概念的设想。推广、多宿主支持、仓库活跃和测试文件存在，不能单独证明真实活跃用户规模、用户测试结果、生产事故率或长期可靠性。本轮没有取得这些运行数据，也未安装、注册、调用其线上消息接口或执行其测试。

证据分开标记为“官方文档说明”“已读代码分支”“已读测试定义（未运行）”。下面记录借鉴依据，不维护第二份候选状态；取舍和当前状态只见 [OPEN_DESIGN](OPEN_DESIGN.md#eigenflux-参考采纳清单)。

### EF-01 接入分段与身份接续

来源：[install.md](https://github.com/phronesis-io/eigenflux/blob/3a3612d1ca78b62508f2863b51f88728d9e1d188/skills/install.md)、[Communication Skill](https://github.com/phronesis-io/eigenflux/blob/3a3612d1ca78b62508f2863b51f88728d9e1d188/skills/ef-communication/SKILL.md) 与 [README](https://github.com/phronesis-io/eigenflux/blob/3a3612d1ca78b62508f2863b51f88728d9e1d188/README.md)。证据：官方文档说明，未验证安装器或任何宿主实际激活。

可借鉴：只配置当前宿主，稳定隔离本地身份／配置，分别检查安装与激活，初次接入与恢复分流，按需加载通信说明；安装不等于同意自动任务。落点：[AGENT_INTERACTION](AGENT_INTERACTION.md#eigenflux-参考建议)、[ARCHITECTURE](ARCHITECTURE.md#eigenflux-参考建议)、[PRESENTATION](PRESENTATION.md#eigenflux-参考建议)。我方保留用户底层身份目标，不把每个宿主隔离目录直接等同于独立用户身份。

### EF-02 来源关联的会话与关系

来源：[message.md](https://github.com/phronesis-io/eigenflux/blob/3a3612d1ca78b62508f2863b51f88728d9e1d188/skills/ef-communication/references/message.md)、[relations.md](https://github.com/phronesis-io/eigenflux/blob/3a3612d1ca78b62508f2863b51f88728d9e1d188/skills/ef-communication/references/relations.md)、[PM handler](https://github.com/phronesis-io/eigenflux/blob/3a3612d1ca78b62508f2863b51f88728d9e1d188/rpc/pm/handler.go)。证据：前两者是文档；已读 handler 的 SendPM、来源建会话与会话回复入口，核对服务端来源路由、参与者检查及会话／消息写入分支，没有完整审计关系子系统。

可借鉴：公开内容、现有会话与好友直聊分别定位；消息和会话分配稳定标识；关系请求可单独查询和处理。关系文档明确指出：通知被清除不等于请求被处理，口头表示接受／拒绝不替代 handle 操作。落点：[DOMAIN_MODEL](DOMAIN_MODEL.md#eigenflux-参考建议)、[AGENT_INTERACTION](AGENT_INTERACTION.md#eigenflux-参考建议)、[WORLD_DATA](WORLD_DATA.md#eigenflux-参考建议)、[PRESENTATION](PRESENTATION.md#eigenflux-参考建议)。其交叉申请自动接受、关闭规则与共享 topic 状态不自动成为我方合同。

### EF-03 拒收与联系预算

来源：[message.md](https://github.com/phronesis-io/eigenflux/blob/3a3612d1ca78b62508f2863b51f88728d9e1d188/skills/ef-communication/references/message.md)、[relations.md](https://github.com/phronesis-io/eigenflux/blob/3a3612d1ca78b62508f2863b51f88728d9e1d188/skills/ef-communication/references/relations.md)、[PM handler](https://github.com/phronesis-io/eigenflux/blob/3a3612d1ca78b62508f2863b51f88728d9e1d188/rpc/pm/handler.go)。证据：文档列出拒收、破冰限制和屏蔽；已读代码确认来源可用性、拒收和屏蔽分支，检索定位了 PM_WAITING_FOR_PEER_REPLY 分支，未执行并发或依赖失效测试。

可借鉴：可拒绝陌生联系，未回应时限制继续打扰，失败给出等待／停止线索。落点：[AGENT_INTERACTION](AGENT_INTERACTION.md#eigenflux-参考建议)、[FOUNDATION](FOUNDATION.md#eigenflux-参考建议)。不把具体次数、好友豁免、忽略检查错误的实现细节或屏蔽时的 silent success 直接搬入我方；服务器执行规则与 UI 如实表达仍须我方验证。

### EF-04 增量通知与断线恢复

来源：[stream.md](https://github.com/phronesis-io/eigenflux/blob/3a3612d1ca78b62508f2863b51f88728d9e1d188/skills/ef-communication/references/stream.md)、[message.md](https://github.com/phronesis-io/eigenflux/blob/3a3612d1ca78b62508f2863b51f88728d9e1d188/skills/ef-communication/references/message.md)。证据：官方文档说明；未核验 WebSocket 故障恢复实现或多设备运行表现。

可借鉴：推送提供低延迟提示，查询提供补取入口，保存续接位置，重连使用有上限退避。落点：[OBSERVATION_STREAMS](OBSERVATION_STREAMS.md#eigenflux-参考建议)、[AGENT_INTERACTION](AGENT_INTERACTION.md#eigenflux-参考建议)、[ARCHITECTURE](ARCHITECTURE.md#eigenflux-参考建议)。我方优先复用已有有界 wait，不照搬单账号单流、读取即已读或固定时间参数，也不承诺无限历史补齐。

### EF-05 可处理错误与重试

来源：[message.md](https://github.com/phronesis-io/eigenflux/blob/3a3612d1ca78b62508f2863b51f88728d9e1d188/skills/ef-communication/references/message.md)、[PM handler](https://github.com/phronesis-io/eigenflux/blob/3a3612d1ca78b62508f2863b51f88728d9e1d188/rpc/pm/handler.go)、[msg_error_test.go](https://github.com/phronesis-io/eigenflux/blob/3a3612d1ca78b62508f2863b51f88728d9e1d188/cli/cmd/msg_error_test.go)。证据：已读文档、发送防重代码入口及测试 TestFormatMessageSendErrorPreservesStructuredDetails 的定义；测试未运行，不能据此宣称通过。

可借鉴：业务错误附带可用的范围、恢复条件或重试提示，客户端保留结构化错误。落点：[FOUNDATION](FOUNDATION.md#eigenflux-参考建议)。测试定义具体检查了错误码、重试时间和会话明细的保留，比仅写“遇错重试”更有参考价值；我方仍以 operation_id 与回执定义未知结果恢复，不照搬按内容指纹去重。

### EF-06 数据可见性与来源标识

来源：[message.md](https://github.com/phronesis-io/eigenflux/blob/3a3612d1ca78b62508f2863b51f88728d9e1d188/skills/ef-communication/references/message.md)、[relations.md](https://github.com/phronesis-io/eigenflux/blob/3a3612d1ca78b62508f2863b51f88728d9e1d188/skills/ef-communication/references/relations.md)、[Communication Skill](https://github.com/phronesis-io/eigenflux/blob/3a3612d1ca78b62508f2863b51f88728d9e1d188/skills/ef-communication/SKILL.md)。证据：官方操作文档及行为提示；未完整审计历史读取 ACL、身份标记来源或客户端出站隐私。

可借鉴：公开联系标识与内部标识区分，私信历史仅向参与者提供，来源标记不由昵称推断；通知或对方要求不等于操作授权。落点：[WORLD_DATA](WORLD_DATA.md#eigenflux-参考建议)、[ARCHITECTURE](ARCHITECTURE.md#eigenflux-参考建议)、[PRESENTATION](PRESENTATION.md#eigenflux-参考建议)。我方不把被验证来源的内容视为必然正确，不允许官方消息越过用户授权，也不以提示词宣传不可泄漏保证。

## 第二轮变更与验证

本轮将建议分别写入 L3 领域模型、L4 接入／调用／数据／观察合同旁的候选章节、L5 架构和交互表现文档；候选状态统一在 OPEN_DESIGN，EF-V1 至 EF-V6 的待执行验收统一在 REFERENCE_GATE。本记录仅补来源和取舍依据，没有再新增文档文件或平行路线图。

行为影响：未修改 L1 产品范围、L2 不变量、现行正式合同、代码、依赖、存储、接口版本或运行配置。没有把候选消息 schema、错误字段、联系预算或宿主能力写成已经实现。需要新语义时先在对应层定稿并更新版本／兼容说明；Commons、游戏示例及既有测试不删除。

实际验证：10 份修改文档的 UTF-8、112 处相对链接与章节锚点、10 个唯一固定版本外部来源链接的格式检查、6 组来源／候选／待执行验收对应检查及 git diff --check 均通过；原有正式条款保留检查通过，唯一删除是将首轮变更记录标题明确为历史记录。外部来源链接已通过 GitHub 读取或首轮核对，不把本轮格式检查冒充线上可达性测试。功能、真实宿主、对方服务、长期运行及用户试点均未执行；不触发纯文档计费 CI。

## 可追溯来源

以下是已核对官方资料的固定源码入口；各文档中的旧日期和示例不自动代表全部当前实现。

- [EigenFlux README](https://github.com/phronesis-io/eigenflux/blob/3a3612d1ca78b62508f2863b51f88728d9e1d188/README.md)
- [Architecture overview](https://github.com/phronesis-io/eigenflux/blob/3a3612d1ca78b62508f2863b51f88728d9e1d188/docs/architecture_overview.md)
- [Communication Skill](https://github.com/phronesis-io/eigenflux/blob/3a3612d1ca78b62508f2863b51f88728d9e1d188/skills/ef-communication/SKILL.md)
- [Private messages](https://github.com/phronesis-io/eigenflux/blob/3a3612d1ca78b62508f2863b51f88728d9e1d188/skills/ef-communication/references/message.md)
- [Stream](https://github.com/phronesis-io/eigenflux/blob/3a3612d1ca78b62508f2863b51f88728d9e1d188/skills/ef-communication/references/stream.md)
- [PM and relations design](https://github.com/phronesis-io/eigenflux/blob/3a3612d1ca78b62508f2863b51f88728d9e1d188/docs/pm_relations_design.md)
- [PM handler implementation](https://github.com/phronesis-io/eigenflux/blob/3a3612d1ca78b62508f2863b51f88728d9e1d188/rpc/pm/handler.go)
- [我方六层基线](BASELINE.md)、[系统不变量](INVARIANTS.md)、[当前实现](IMPLEMENTATION.md)、[Commons 源码](../agent_world/commons_universe.py)
