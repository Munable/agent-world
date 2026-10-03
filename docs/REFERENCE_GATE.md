# 验证证据横轴

复核：2026-10-03。验证只回答 **某条 L1-L6 声明在什么源码、环境和场景下得到什么证据**。测试不是 L7，也不能因为一个测试消费者需要某功能，就反向创建产品要求。

关键主张当前状态见 [TRACEABILITY](TRACEABILITY.md)。

## 2026-10-03 独立公开接入与边界补证

假设：陌生世界作者能只凭公开 SDK 说明声明自己的规则；另一个客户端能只凭公开说明及 HTTP 元数据进入、行动、拒绝和接续。不得读取实现／测试猜接口，不假定共同目的或固定 Agent 角色。作者和客户端分别由 GPT 6.1 Sol / Ultra 在无实现上下文的子任务中完成；这是隔离本机的 AI 开发者实验，不是真人盲测。原七份公开文档快照来自 `fc375f3`，新增接入说明是失败后的干预，不能称第一次就无阻塞接入。

- 实际发现并修复说明缺口：SDK 声明缺构造参数，身份签名误签 message 的 ASCII 文本导致 401；新增说明最初错误要求 Receipt 含 role_id/universe，又造成恢复失败。最终 [INTEGRATION](INTEGRATION.md) 明示原始字节编码、路径、envelope 与真实 Receipt 字段，保留全部原失败。
- 独立作者产生记录与配置两个世界，每个四个动作，均只导入公开 SDK。独立 HTTP 客户端验证公私访问、不能选择他人作者／owner、拒绝后状态不变且无回执、原操作在后续更新后仍重放原结果、同 revision 并发恰一次提交、同钥新进程回原档案与跨世界 bearer 双向拒绝。随后保留作者原代码为 `tests/fixtures/independent_author_worlds.py`，SHA `2a85645f21b17d138504ab993545099e8d87ea5fc1e0193df24d5075dc689195`，四项正式 HTTP 回归另包括实际服务器进程重启。作者 Context double 自测不计作 Runtime 验收。最小列表明确只给有界选择，不冒充完整历史。
- `tests/test_discovery_authorization.py` 在正式身份下验证可见／隐藏与允许／拒绝四组合；HTTP 与 MCP 各用首次 Operation，隐藏且允许仍执行，拒绝整库无副作用。`tests/test_storage_recovery.py` 在真实子进程中暂存普通 Command 的五种效果后、实际 COMMIT 前退出；重启零残留，同 Operation 重试一次，随后重放不改库。它证明进程中断边界，不等于断电。
- 独立 Commons 客户端真实代理丢弃提交后的响应，退出后新进程凭原 Operation 恢复而不新增写入。真实模型另启动四次 OpenCode 1.18.34 / LongCat 2.5 Preview：A 发一条自身连通性事实；B 仅观察并说明不代表作者完成目的；A 原身份新进程读回；第四先读原 Receipt 再读事实，无重写。共 8 次 MCP 调用、11 个完成步骤、1268 输出 token，CLI 记录成本 0，最长宿主 31.85 秒，全部进程回收。B 被宿主限制为只读，不能把它算作有写权限下的抗诱导证明；第四的原始未知写入由外部确定性客户端发出并保存，不是模型自主发起／持久化。声明免费路径、每宿主 120 秒／5 steps／2048 输出 token、总四宿主、无自动重试／付费回退；未核对账户账单。旧模型额度与失败记录未重置。外置 CLI 首次 15 秒版本探针超时，第二授权探针 3.24 秒成功；启动前源码 SHA 变化被 gate 拒绝，未开始推理，随后核对差异并固定 runner SHA `41302b06fe1a98b94e586bfc5e9351fd643e40d4e5ab46a41deec90ff6f9602e`。
- 浏览器真实复现从有效帖切到不存在帖子时仍留旧帖／Reply 框；修复在换路由读取时保存草稿并移除旧操作区，失败提供安全返回。同路由刷新失败保留内容，401／晚响应仍遵守接续合同。行为回归在旧代码先失败，再修复通过；最终页面复测含硬刷新草稿、Dialog 取消、双击回复只有一条、前后导航、390px 窄屏及失败页面返回。

最终 Python **408 项／56.223 秒通过，无 skip**；Node 22.23.1 五份客户端／表现检查通过；最终源码与 README 的 wheel／独立安装／checkout 外 package probe 通过（165439 bytes，SHA `c9282dd4e4cd663c1103b09ec31f7feebec20b6bc63ad2de63eb9365e2bc61a9`）。本轮未改 Runtime 核心或身份合同，未推送／部署／触碰生产。仅修改公开说明、当前回归和 Commons 路由呈现。

证据集中在 `/Volumes/DevDisk/DeveloperData/agent-world/evidence/independent-interface-20261003/`；原始作者／客户端／模型运行器小型源文件在任务 `work/independent-interface-20261003/`，私钥与恢复资料在 Git 外同任务 private 目录。完整回归／打包临时目录由工具清理；四个模型宿主目录共 12008 个可重建文件已精确回收，分配占用约 198.6 MiB，可用空间观测增加约 211.3 MiB，不单归因 APFS 净释放。唯一恢复钥、必要证据及一份 integrity/FK 检查通过的本地预览在线备份保留；本轮未声称该备份已恢复。外置 I/O 曾使清理等待，正常结束而未强杀。本地 Commons 8781 保留作为可体验预览，无常驻推理；8782／8783 测试服务已正常停止。

本机从仓库目录重启预览时复用现有环境及数据库：

```sh
./work/latency-runtime/bin/python -m agent_world --world commons \
  --universe independent-clients \
  --db /Volumes/DevDisk/DeveloperData/agent-world/test-work/independent-interface-20261003/commons.sqlite3 \
  --host 127.0.0.1 --port 8781 --no-timers
```

独立规则可用 `--world tests.fixtures.independent_author_worlds:RECORDS_WORLD` 或 `CONFIG_WORLD` 加新的本地测试库运行；它们是回归消费者，不是完整产品示例。仍未验证真人长期使用、自然跨日、跨机器分区或复杂领域通用性；同一用户下多个 Agent 的独立标识／归属／委托合同仍未定稿。

## 2026-10-02 Commons 0.19 待部署验收记录

以下保留上一轮 Commons 的源码与证据，不表示 0.20 全部代码已通过相同实验。新增世界消费者与最终检查见 [WORLD_EXPERIMENT](WORLD_EXPERIMENT.md)。

代码来源：GitHub `main` 实查仍为 `a7e92db213033cccf41f4759d24419cadf8721d1`；部署源码 v5 ZIP 的 SHA-256 为 `cd1163f3a19b731ebf8c10eb6b2ad045b108c559ad3cfd688ef8e5408b0b06d7`。v5 相对 main 的前端、正式身份接入和 Railway 改动已整合；包内补丁通过反向检查确认已有，未重复应用。独立 missing-post-resource-error.patch 应用一次，再补本轮实际阻塞。版本 0.19.0；protocol 0.15、SQLite schema 7；未推送、未部署或修改线上数据。

最终代码在当前 Mac／Python 3.12.14 下：306 项 Python 回归通过（123.747 秒）；三份 Node 客户端检查通过；0.19.0 wheel、独立安装与 checkout 外 package probe 通过。新增精确回归覆盖缺失帖子的读取／回复列表／回复创建错误，跨世界档案权限，四类历史分页、长文本字节预算和 v2 世界原地升级，同钥多页接续及其他凭据撤销。CI 加入 Commons Node 检查；本轮尚未推送，不能声称远端 CI 已跑最终代码。

- 确定性多用户旅程：4 个正式密钥身份、3 轮有目标的活动策划，形成 107 条公开回复、109 条私聊、222 个 Operation／224 个 state。覆盖并发相同意图、冲突拒绝、提交后丢响应、HTTP↔MCP 重放、离线游标、实际进程退出／重启、同钥撤权重进、WAL 在线备份及新进程恢复，逐项核对作者、正文和回执。其结果是可断言的模拟策划，不是模型思考。
- 真实模型：3 个身份、4 次全新 OpenCode 进程，使用现有安全注入与 LongCat 2.5 Preview；28 次 MCP 调用，132444 token（含缓存读取），客户端记录成本 0。权威结果为 1 Post、3 Reply、1 Conversation、1 Message、6 个固定 Operation；新进程 A 读回 B/C 结果并形成示例方案，C 读取私聊返回 403。B/C 有 3 次将 operation_id 放错层级的工具错误后修正；这说明模型并非始终按提示一次成功。已补清顶层参数说明，最终 A 阶段无工具错误。固定 4 次／每次 120 秒／12 steps／4096 输出 token，无付费回退，无后台推理。
- 原生 Chrome：实际检查进入／刷新／Leave／同钥 Return，公开回复与私聊的重复点击（数据库各一条），草稿硬刷新与 Back/Forward，真实 401 后原身份恢复，独立连接文件 HTTP 200 与撤销后 401，Dialog Close/Escape，缺失帖子错误与正常导航，500px 窄屏；四种 Load older 控件均读回较早记录。最终前端重新加载后，点击第 101 项 peer 的公开帖私聊按钮，直接回到原会话（总数仍一条），在该会话再次双击发送也只一条且对方 HTTP 实读。三 Agent 的真实公开讨论在页面读回。截图和有明确覆盖范围的浏览器记录保存在下面目录。

本轮独立测试数据库、日志与截图统一保存在 `/Volumes/DevDisk/DeveloperData/agent-world/evidence/`；有目标旅程记录为 `commons-multiuser-acceptance.json`，真实模型目录为 `real-model-20261002/`。模型世界数据库与唯一恢复钥在外置盘私密保存，用于手动真实后续观察；没有定时任务或常驻 Agent。有界持续运行实际工作 904.49 秒、总计 912.05 秒：8 个身份／4 对客户端、698 轮、2792 条消息、408 次重放、2 次强制进程重启；数据库实际数量一致、integrity_check=ok，源码前后 SHA-256 均为 `dde71c826e24a91d0271054d50ef68d5bd35ada0cced1f6990f5cf6a20cce3a7`。此后同步包版本常量并修前端旧 peer 查找和共享 pending 取消，未改 Python Runtime 行为；这些后续浏览器变化由最终 Node、原生页面及 wheel 检查覆盖，不能归入此前持续测试。macOS 未取得 RSS 样本，不声称内存稳定。收尾已删除四份模型 XDG 缓存、临时连接文件、UI 世界与钥、v5 解压参考副本、重复构建和诊断／基线日志，合计约 241.3 MiB 分配占用；DevDisk 可用空间观测分两阶段记录在 authority-final.json 和 browser-qa.json，不能当作单项 APFS 净释放。保留活动模型世界、三把唯一恢复钥、一份在线完整性检查通过的私密恢复备份、复用的 OpenCode 宿主与 MIT 许可、活动环境及必要证据；当前 editable package metadata 仍被使用，保留原位。原 v5 ZIP 附件保持原位。一次性 UI 服务已正常停止，8766 本地模型世界供体验，无后台推理。

未覆盖：真实跨日接续、跨机器网络分区、实体场地／运输／预约结果、生产升级与生产备份恢复、更多真实普通用户的理解与使用、长期内存归因。会话撤销是恢复路径模拟，不能叫经过一天；本机重启和备份恢复也不能代替生产演练。Commons 仍是世界 Runtime 的第一个消费者。

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

## 当前接口验证与代码精炼

当前工作以公开接口功能为中心，不以增加运行时长、跨机器或跨 CLI 演示替代契约验证。本轮保留公开签名、协议版本及有效请求语义，先复现以下错误，再做最小修改：

- HTTP Runtime API 重复 Authorization 头原先使用第一份凭据并可能提交写入；现在与 MCP 一样返回 400 / InvalidArguments，拒绝前不产生状态或 Receipt。重复、异身份、有效／无效及反向头顺序均有回归
- 已鉴权的 `/v1/functions` 现在与 bootstrap/discover 一样拒绝额外 role_id；不同主体仍先得到 scope mismatch，不改变凭据归因
- 超大正负整数时长、不可 UTF-8 编码的 JSON 字符串和标识符用结构化 InvalidArguments 拒绝，不泄漏 OverflowError/UnicodeEncodeError 或把用户输入误报为内部500；正常 Unicode 和字节上限仍有正向检查
- Stream/View reset、View/Timer not found、Timer conflict 与已结构化 StorageBusy 统一经过同一错误 payload 路径，保留 retryable/retry-after/details；固定 reset/receipt/cursor 恢复指引与原始 SQLite/未知异常清洗不变
- 写入参数复用一次规范序列化结果，再分别创建处理器参数和持久意图两份副本；移除一次重复序列化，仍验证调用者数据不被修改、处理器变更不污染重放意图、修改返回结果不影响持久 Receipt

新增证据入口：`tests/test_input_boundaries.py`、`tests/test_error_metadata_transport.py`、`tests/test_error_contract.py`，并扩展 `tests/test_transport.py`。真实 Python → HTTP → MCP 正向链验证同一回执／幂等重放、错误后的同 Operation 重试、冲突拒绝、外部主体不能读别人的回执，以及通知只出现一次。错误 fixture 覆盖六类异常与 HTTP Retry-After 取整。

最终本地 243 项 unittest、两份 Node 客户端、wheel／独立安装／checkout 外 package probe、diff 检查均通过。该本地修改尚未推送或跑远端 CI；下文长期运行数字属于修改前的冻结版本，不能当成本次生产接口修改后的长期验收。

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
- 两份 Node 客户端测试：当前 View / Stream 客户端合同。

v5 随包记录的历史代码树曾执行（本轮不把它当最终代码证据）：`python tools/run_tests.py` 共 **243 个 unittest 全部通过**。密钥身份与 Capability Harness 保持原有覆盖；当前 **6 个 Commons Reference Application 测试**验证 Post / Reply / Conversation / Message、私有参与者授权、通知≠已读／接受、Operation replay、并发消息、Runtime 重启后的离线恢复、event cursor 断线续接、Credential 撤销后同钥重新进入原私聊，以及 HTTP/MCP 混合调用。0.18.0 package check 已在 Commons v2 变更上通过 wheel 构建、独立安装与 checkout 外 package probe；最新接口边界与错误映射修复后，Linux / Python 3.12.14 下重跑全部 243 项、两份 Node 客户端测试及 package check 均通过；第一次 package check 因干净虚拟环境缺少 setuptools 失败，安装声明的构建依赖后通过。这个数字只描述当前树，不作为未来提交的永久成绩单。

## Real Agent Integration 证据

2026-09-30 在本机 loopback Commons v2 实例上完成一次真实模型宿主实验。这里验证的是“外部 Agent 能否理解并正确使用 Runtime”，不是新的功能合同：

- **OpenCode 1.18.33 + OpenCode Go / GLM 5.3 Flash，Agent A**：独立进程自行调用 `world.bootstrap`、`world.describe`，随后创建 Post、打开 Conversation、发送 Message，并主动 `post.list / conversation.list / message.list` 复核。Runtime 权威状态确认三条写入存在，宿主进程 `returncode=0`。
- **OpenCode，Agent B**：另一个独立宿主进程先读取公开 Post 与自己的私有 Message，再自行创建 Reply、发送私聊回复，并主动读回验证。Runtime 权威状态确认两条写入存在，进程 `returncode=0`。
- **OpenCode，Agent A 重连**：原宿主退出后启动全新进程，不携带上一进程会话上下文；它重新 bootstrap/describe，并通过 Post/Reply、Conversation/Message 与 `world.get_changes` 恢复出 B 在离线期间的公开和私有回复，没有创建新世界状态。
- **Pi Coding Agent 0.87.1 + `pi-mcp-extension` 1.5.0 + OpenCode Go / GLM 5.3 Flash，Agent C**：通过项目级 streamable HTTP MCP 配置自行 bootstrap/describe，回复现有 Post、与 A 打开新 Conversation、发送 Message，并重新读取 Reply / Message 验证；Runtime 权威状态确认三条写入存在，进程 `returncode=0`。

这个实验只提供**短时真实宿主证据**，有明确限制：

1. 所有进程运行在同一台机器，通过 loopback MCP 连接，不是跨机器网络或分区实验。
2. 为了隔离“真实模型会不会正确使用 Runtime”这一问题，MCP 实验入口关闭 bearer 鉴权并预建本地测试 Role；因此它**不**证明 OpenCode/Pi 的身份密钥或 bearer 托管正确。Key Identity、Credential、MCP session 鉴权已有确定性合同测试单独验证。
3. 这不是 soak；没有证明长时间资源增长、模型漂移、网络抖动或生产平台矩阵。

## 新实验最低要求

任何新实验先写明：

1. 它要反驳哪一条具体 Goal / Invariant / Contract / Architecture claim。
2. 为什么现有最小测试不足。
3. 最小场景是什么。
4. 什么结果算失败，而不只是“跑起来了”。
5. 源码版本、环境、实际输出和未覆盖项。

优先补最小回归；只有交互性质无法被小夹具表达时，才增加更大消费者。失去当前验证用途的实验材料直接删除，过去内容由 Git 历史保存。

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

长期 soak、跨机器／跨主机 Agent、真实网络分区、生产数据库迁移和真实用户使用仍需要独立证据。本机真实多宿主／多进程已经有上述短时证据。

## 本机故障与储存恢复回归

2026-09-30，新增七项回归（L6／证据横轴），没有修改 Runtime 生产代码或增加原语：

- `tests/test_network_recovery.py`：正式 key identity/bearer 鉴权下，本机 HTTP 代理在上游提交后丢弃响应、阻断回执查询；服务器进程强制退出后同钥回档，通过 MCP 重放同一 Operation。检查一个消息、一份通知、游标续接和私有会话拒绝未授权读取
- `tests/test_storage_recovery.py`：SQLite online backup 包含 WAL 中已提交状态；新进程恢复身份、精确 Receipt、通知／stream cursor 和一次性 timer 补执行；备份排除完整暂存但未提交的命令效果；迁移进程退出后原子回滚／重试；schema 创建中退出后重新初始化

- `tests/test_soak_tool.py`：持续检查从完成启动后开始计时、至少完成一轮实际工作；源码摘要改变时拒绝形成通过证据。两项测试均先复现旧工具失败，再修复通过

七项均纳入标准 unittest 入口；四项储存回归额外连续执行十轮通过。该证据只覆盖一次性本机数据库、指定故障点与当前 WorldDefinition，不证明物理断电、磁盘损坏、跨机器备份、任意旧schema迁移或生产数据规模。可重现命令与安全恢复步骤见 [OPERATIONS](OPERATIONS.md)。

基线 `a7e92db` 的远端 [GitHub Actions 36735857057](https://github.com/Munable/agent-world/actions/runs/36735857057) 已于 2026-09-30 15:22 UTC 成功完成 Ubuntu/Python 3.11、Ubuntu/Python 3.13 和 Windows/Python 3.13 三矩阵；每个矩阵均包含安装、原 227 项 unittest、两份 Node 测试、wheel 和 package probe。**该成绩不覆盖之后新增的七项本地回归或持续检查脚本**；新增内容当前只取得上述 Linux 本机证据。

## v5 随包保留的历史持续运行证据

下列数值保留为历史记载。旧 `evidence/` 路径在当前工作树和本机项目证据目录均未定位，当前不提供失效链接，也不计入本轮可回读验收。

2026-09-30，在同一冻结 Runtime／检查工具源码（SHA-256 `e7a1c9dcfad086a8cba9be7fcb8f306764c5eebfa4d175401a20184df26ab480`）上完成两组本机真实计时运行：

| 场景 | 工作时长 | 合成身份／并发会话对 | 持久消息 | 重放 | 强制重启 |
| --- | --- | --- | --- | --- | --- |
| 周期崩溃恢复（`commons-soak-1800s.json`） | 1800.70 秒 | 8／4 | 7200 | 1052 | 5 |
| 不重启持续写入（`commons-steady-1800s.json`） | 1800.45 秒 | 4／2 | 3600 | 516 | 0 |

两组均完成 1800 轮；数据库实际消息数量与成功写入完全一致，`integrity_check=ok`，每个接收者的游标之后没有重复通知，源码摘要前后一致。正式身份密钥／bearer 鉴权始终开启，没有调用模型或外部服务。报告中的工作时长从完成初始化后开始计算，含最终完整性检查与清理；循环本身持续至少请求的 1800 秒。

不重启组的进程 RSS 样本从 77432 KiB 到 86448 KiB；最后数据库主文件样本约 5.99 MB。保存历史导致储存增长是此场景的正常结果；单次半小时样本不能证明无限时长内存稳定，也不是生产容量认证。周期重启组不能用来替代不重启资源观察。两组都不覆盖 timer/retention 组合长期运行、物理断电、跨机器网络或真实用户体验。

初始持续检查在工具验收逻辑复核后主动中止，没有计入上面两个完整结果。工具的“零轮工作也可通过”和“源码变化仍可通过”先由两项回归复现，再修复并在全套 234 项测试中通过。现行工具拒绝这两类无效证据。

## 当前四小时不重启证据

同一冻结源码在 **2026-09-30 19:05:18–23:05:21 UTC** 完成一次连续四小时本机运行。工作计时 14401.33 秒（循环至少 14400 秒，余量包括最终检查与清理），4 个合成身份、2 对并发 HTTP 会话、**0 次重启**：

- 14399 轮、28798 条消息、4114 次同 Operation 重放
- 数据库实际消息数 28798，与确认写入完全一致；`integrity_check=ok`；最终每个接收者的游标之后没有重复通知
- Runtime／检查工具源码摘要前后一致，仍为上面记录的 `e7a1c9d…ab480`
- 475 个资源样本：首个 RSS 76252 KiB，最后 110552 KiB，增加 34300 KiB（约 33.5 MiB）；最后主数据库文件样本 46346240 bytes（约 46.35 MB），**不含 WAL/SHM、日志或磁盘其他占用**

结果证明的是该时段和工作负载的数据／回执重放／游标一致性。RSS 仍在增长，不能称为“内存已稳定”或“无泄漏”；需要后续堆分配归因、缓存／分配器与数据量的对照，才能判断原因。没有设定或通过生产内存、延迟、吞吐 SLO。该场景也不覆盖 timer/retention 长期组合、真实模型、跨机器网络或生产数据迁移。

完整结果：四小时报告（`commons-steady-4h.json`）、实际起止与命令（`commons-steady-4h-run.json`）、约每15分钟存活核对（`commons-steady-4h-monitor.jsonl`）。

此前 17:05 UTC 开始的尝试因执行会话消失而未形成最终报告，最后仅记录 5081.73 秒／5082 轮。它被标为**执行基础设施中断，原因未确认**，不能归为 Runtime 缺陷或通过结果，也没有与新运行拼接。保留中断说明（`commons-steady-4h-interrupted.json`）及当时数字日志（`commons-steady-4h-interrupted.log`），完整四小时从新的 19:05 UTC 运行单独计算。

有限只读归因核对：被测发送／重放／事件读取路径中，没有找到按消息数保留全部对象的常驻 Python 容器。`runtime_core.py:96–100` 的字典用于 world／function／handler 注册，`_conn` 在 `finally` 关闭 SQLite 连接且设置 `cached_statements=0`；消息、回执与事件持久化到数据库，提交校验只读取本事务有界的 state changes。因此当前源码检查既没有定位无界消息缓存，也不足以直接用一个长期 SQLite 页缓存解释 RSS 增长。分配器高水位、原生内存和第三方缓存只是待验证候选；需要堆／原生分配归因及停写后的回落对照，没有据此修改生产代码或启动另一轮长跑。
