# 验证证据横轴

复核：2026-10-13。验证只回答 **某条 L1-L6 声明在什么源码、环境和场景下得到什么证据**。测试不是 L7，也不能因为一个测试消费者需要某功能，就反向创建产品要求。

关键主张当前状态见 [TRACEABILITY](TRACEABILITY.md)。服务器验收针对接口可用、身份归因、世界规则、数据隔离、提交与恢复；模型是否遵循用户提示词、完成其目的或给出正确总结，属于用户侧宿主表现，应与服务器结果分别记录，不把它提升为 Runtime 的功能门槛。用户自行迁移本地记忆不构成服务器恢复失败。

## 当前证据入口

当前0.38.0／world42／state25覆盖通用依赖诊断、编译器语法发现、精确实例效果读取、MCP成功／错误输出schema及Native实际工具名映射；最终Python／Node及实际安装包检查已通过。四次Native创作尝试未形成新实例，GPT公开SDK创作与原Native阿岚接续的有界真实链已有回执和同实例变化证据。具体检查、正反模型证据、原数据恢复与11条不可恢复历史元数据缺口，统一沿[SHARED_GROVE 当前研发记录](SHARED_GROVE.md#当前研发记录)及已有外置 `implementation.json` 的 `agent_effects_round`。

上一轮0.37.0／world40模块化覆盖独立协议消费与恢复，未启动真实模型，其证据沿原 `agent_first_modularity`；此前真实模型创作的成功与功能失败保留原范围，不覆盖本轮新作。过去阶段的版本、端口、请求数和截图由Git历史及原外置证据承担，不在当前规则页重复维护运行快照。

## 核心必需验证

| 范围 | 检查什么 |
| --- | --- |
| 当前 unittest | Query/Command、原子提交、Receipt、身份／授权、state、迁移、view、stream、timer、retention、结构化错误等 Runtime 合同。 |
| 当前回归保护 | 仍属于现行合同的 schema / authorization / version / recovery 等缺陷不能被无声删除测试。 |
| 架构静态边界 | Runtime/SDK 不反向 import adapter、UI、示例世界或测试代码。 |
| 客户端合同测试 | snapshot/delta、乱序、stream 去重、表现队列等客户端行为。 |
| package check | wheel 能构建、独立安装、导入资源，并由 checkout 外的 package probe 组合使用公开 SDK。 |

这些检查也只证明自己的覆盖范围。单次全绿不是长期运行、任意世界通用性或生产安全性的证明。

## 不同证据各自能说明什么

| 证据 | 可以说明 | 不能说明 |
| --- | --- | --- |
| 单元／合同回归 | 某个明确语义在当前代码下成立。 | 整个产品已经完成。 |
| 当前 transport / integration 测试 | 当前 HTTP/MCP、身份和进程边界在被测路径中成立。 | 真实外部 Agent 长期行为或全部平台。 |
| Real Agent Integration | 真实模型宿主能够通过 MCP 自己发现、理解、调用世界能力，并在新进程中重新读取持久事实。 | 跨机器网络、长期 soak、所有宿主兼容性或身份鉴权宿主实现。 |
| checkout 外 package probe | 安装包、公开 import 与若干可选能力可以脱离源码目录组合使用。 | 它不是 Reference Application，也不能证明复杂领域天然适配。 |
| Capability Harness | 多参与者拓扑、并发、撤权、重放、恢复等明确能力组合成立。 | 某个具体产品体验已经合理。 |
| Reference Application | 一组真实领域规则能够消费 Runtime，并可暴露通用缺口。 | 该领域对象应该进入 Runtime。 |
| 浏览器视觉／轨迹 | 特定 UI 组合的呈现和最终事实一致。 | 服务器合同本身正确。 |
| soak / 长期运行 | 积累、恢复、保留、timer、升级组合是否出现时间相关问题。 | 未覆盖的业务语义。 |

## 接口边界回归

公开接口的身份、输入、错误和原意图边界保留最小回归。它们保护当前合同，不以增加运行时长、跨机器或跨 CLI 演示替代契约验证：

- HTTP Runtime API 重复 Authorization 头原先使用第一份凭据并可能提交写入；现在与 MCP 一样返回 400 / InvalidArguments，拒绝前不产生状态或 Receipt。重复、异身份、有效／无效及反向头顺序均有回归
- 已鉴权的 `/v1/functions` 现在与 bootstrap/discover 一样拒绝额外 role_id；不同主体仍先得到 scope mismatch，不改变凭据归因
- 超大正负整数时长、不可 UTF-8 编码的 JSON 字符串和标识符用结构化 InvalidArguments 拒绝，不泄漏 OverflowError/UnicodeEncodeError 或把用户输入误报为内部500；正常 Unicode 和字节上限仍有正向检查
- Stream/View reset、View/Timer not found、Timer conflict 与已结构化 StorageBusy 统一经过同一错误 payload 路径，保留 retryable/retry-after/details；固定 reset/receipt/cursor 恢复指引与原始 SQLite/未知异常清洗不变
- 写入参数复用一次规范序列化结果，再分别创建处理器参数和持久意图两份副本；移除一次重复序列化，仍验证调用者数据不被修改、处理器变更不污染重放意图、修改返回结果不影响持久 Receipt

证据入口为 `tests/test_input_boundaries.py`、`tests/test_error_metadata_transport.py`、`tests/test_error_contract.py` 和 `tests/test_transport.py`。Python → HTTP → MCP 正向链检查同一回执／幂等重放、错误后的同 Operation 重试、冲突拒绝、外部主体不能读别人的回执，以及通知只出现一次；错误 fixture 检查异常元数据与 HTTP Retry-After 取整。实际源码、验证、推送和运行状态统一沿当前研发记录，不用过去的测试数量或冻结结果代替本轮证据。

## Runtime Capability Harness

验证不固定为“两名 Agent 对话”。底层能力按拓扑与故障维度组合：

| 维度 | 至少覆盖 |
| --- | --- |
| 参与者拓扑 | 1 个主体、1→1、1→N、N→1、N→N、多个权限组。 |
| 写入／竞争 | 单写、不同 scope 并发、同对象竞争、事务回滚、同 ID 重放、未知结果恢复。 |
| 观察 | 多消费者、快慢消费者、后加入、离线后恢复、重复／乱序、cursor 过期。 |
| 身份／权限 | observe/control、撤权、凭据失效、错误身份、跨 world instance 拒绝。 |
| 时间／保留 | timeout、timer、通知清理、状态／历史保留边界、进程重启。 |
| transport | HTTP/MCP 语义一致、多个 session/client、取消、唤醒与恢复。 |

确定性协议客户端负责精确复现与断言；真实外部 Agent 只用于验证发现、理解、宿主接续和实际使用体验，不能替代底层确定性测试。

## Reference Application 验证

社交是第一个现实消费者之一，可以验证公开发布、关联回应、私有会话、关系／拒收等具体流程，但它的对象和规则不自动上升为 Runtime 要求。

任何从 Reference Application 提升的 Runtime 能力都必须回答：

1. 它在什么非本应用场景中同样成立？
2. 现有 Runtime 能力为什么不足？
3. 最小可复现反例是什么？
4. 引入新原语后如何验证不会把领域规则塞进核心？

EigenFlux 参考机制的当前取舍见 [REFERENCE_EIGENFLUX](REFERENCE_EIGENFLUX.md)。

## 当前必须保留的证据

当前仓库不保存阶段编号测试或旧审计快照。某个过去出现过的缺陷如果仍对应现行合同，就把最小断言保留在当前命名的测试文件中。

当前关键证据入口包括：

- `tests/test_foundation.py`：事务、Operation/Receipt、本地 Credential、Join Ticket、credential rotation receipt、撤权、并发与基础恢复。
- `tests/test_key_identity.py`：身份公钥持钥证明、首次建档、同钥回档、新钥新身份、跨世界本地档案／本地 bearer、错误签名、过期 challenge 与禁用档案。
- `tests/test_onboarding.py`：operator credential rotation 的 operation_id、丢响应重放与 receipt 查询。
- `tests/test_transport.py`：HTTP/MCP 同源语义、会话身份绑定、严格主体归因、wait/cancel/wakeup、跨 transport replay。
- `tests/test_error_contract.py`、`tests/test_structured_error_transport.py`：结构化 recovery / retry / details 及 HTTP/MCP 保真。
- `tests/test_runtime_capability_matrix.py`：正式 Key Identity 参与者下的 1→N、N→1、N→N、撤权、重放、重启回档、并发首次进入、批量进入、跨独立 Runtime 隔离与同对象 CAS 竞争；`tests/test_transport.py` 另覆盖同一密钥的双 MCP session。
- `tests/test_commons_reference.py`、`tests/test_commons_transport.py`：Commons v2 的 Post / Reply / Conversation / Message，覆盖公开读取、私有参与者授权、Operation replay、并发双向消息、重启后的离线通知恢复，以及真实 HTTP/MCP 混合调用。
- view / stream / timer / world-data / SDK 测试：各自现行能力合同。
- `tools/check_package.py`：当前 package 构建与 checkout 外 probe。
- `tests/*.test.mjs`：当前11份客户端脚本，覆盖View、Stream、Commons、Grove、River Camp及表现合同。

## 新实验最低要求

任何新实验先写明：

1. 它要反驳哪一条具体 Goal / Invariant / Contract / Architecture claim。
2. 为什么现有最小测试不足。
3. 最小场景是什么。
4. 什么结果算失败，而不只是“跑起来了”。
5. 源码版本、环境、实际输出和未覆盖项。

优先补最小回归；只有交互性质无法被小夹具表达时，才增加更大消费者。失去当前验证用途的实验材料直接删除，过去内容由 Git 历史保存。

## 真实 Agent 创作与效果证据门槛

机器语法、能力依赖诊断与结果读取分别验证草稿形状、声明和现场。最小合同回归覆盖编译器语法节点与诊断正式续读、不同机制的静态依赖、动态绑定／输入未知、非法谓词的合法事实发现，以及诊断和证据读取无副作用；还须保留定义／hash／旧实例不变、身份与授权、原意图恢复和材料守恒。MCP错误须经声明schema保留真实原因；宿主映射只列实际工具且不能执行建议读取。不能只为某件失败作品添加一个专用判断，也不能把 `valid:true`、诊断无警告或当前谓词成立写成机制已经执行。

真实任务使用普通目标，由 Agent 自选机制、材料、形式及输入；不提供定义草稿、配方、对象 ID、调用顺序或成功答案。最小完整路径要求：

1. Agent 在触发前说明选定作品应响应什么输入、出现什么可检查变化，再通过公开发现、验证和实际行动自行实现。
2. 发表不可变定义并以真实成本形成实例，核对原 Receipt、实体、固定定义与类型；发表、成型及功能成立分别记录。
3. 对正确实例施加真实输入，读取同实例的当前事实及有来源的变化证据，区分输入已提交、条件成立、历史效果和本次观察。逻辑步、读取水位及操作提交序号不混用。
4. 另一原身份仅凭公共事实找到作品，实际使用、组合或改作并读回后果；不得复制创作者私人上下文，或由开发者替模型修好定义后计为自主完成。
5. 原始模型回复与工具证据同时保留。服务器匹配有限谓词只证明该读取的事实；宿主完成检查点仍是 Agent 对自然语言目标的声明。

每个主体分别记录实际模型、宿主／SDK、身份和世界行动。GPT经公开SDK自主创作与Native参与者接续可以证明这条跨消费者路径；它不能反向证明Native创作者已成功，也不能把开发者代写或修好的定义记为模型自主创作。本轮有限链已保留按主体区分的真实回执和0→1→2实例变化；授权恢复、目标纠偏、错误完成声明与被拒绝动作也保留，不能外推为无需干预的Native通用创作。

失败按静态能力缺项、现场绑定／部署／输入未准备、世界拒绝、运行异常、效果不符或证据不足分别记录。尚未触发、事实未知和部分历史不能靠猜测补成成功，也不自动证明机制永远不能工作。旧真实机制失败只有在对应路径获得新证据后才能改判；其它主张仍保留原范围。实际结果沿 [SHARED_GROVE 当前研发记录](SHARED_GROVE.md#当前研发记录)，本节只规定门槛。

## 密钥身份验证证据

当前正式合同只验证“密钥就是身份”：

1. Ed25519 公钥作为 User Identity；世界通过一次性 challenge + signature 验证私钥持有。
2. 同一公钥第一次进入某 World Instance 时创建本地 Participant Profile；以后同钥回到同一档案。
3. 新公钥创建新身份／新档案，不允许通过名字、档案内容或声明认领旧公钥档案。
4. 同一公钥进入两个独立 Runtime 时，两边可以保存不同本地 profile / role_id；不会搬运 display name、avatar、资产或权限。
5. 每个世界独立签发 bearer Credential；A 的 Credential 在 B 无效。
6. challenge 过期、错误私钥签名和禁用本地档案都会拒绝进入。
7. 同一 challenge 的网络重试恢复同一个本地 Credential，不重复创建档案。
8. 身份密钥没有恢复／换钥继承接口；这不是待补功能，而是当前 L2 明确边界。


## 当前仍缺的验证

长期 soak、跨机器／跨主机 Agent、真实网络分区、生产数据库迁移和真实用户使用仍需要独立证据。既有真实宿主短时证据保留原范围，最新状态统一见当前研发记录。

## 本机故障与储存恢复回归

仍属于当前合同的故障保护纳入标准unittest：

- `tests/test_network_recovery.py`：正式身份鉴权、上游提交后丢响应、分区、服务器进程退出、原Operation跨HTTP／MCP恢复；核对唯一消息、通知、游标及私有读取授权。
- `tests/test_storage_recovery.py`：SQLite在线WAL备份、身份及精确回执恢复、未提交效果排除、迁移失败原子回滚与重试。
- `tests/test_runtime_intent_receipts.py`：原参数摘要、旧回执补证明而不改原body、同编号不同意图冲突。
- `tests/test_grove_agent_client.py`及`tests/test_grove_revoked_recovery.py`：原世界归属、原封包、停止／撤控、错误回执、未知结果及精确恢复。
- `tests/test_soak_tool.py`：有效工作范围与源码摘要；工具通过不能替代长期运行的实际证据。

本轮逐值恢复范围见当前研发记录；这些检查不证明物理断电、磁盘损坏、任意旧schema迁移或生产恢复。可重现操作与安全步骤见[OPERATIONS](OPERATIONS.md)。远端CI没有运行本轮源码；仓库Actions仍关闭，workflow只接受显式手动触发，本轮采用本地验证。
