# L6：当前实现

复核：2026-10-13。本文描述当前分支候选代码与公开表面，不记录本轮验收结论。当前源码范围、模型实验和交付检查统一见[SHARED_GROVE 当前研发记录](SHARED_GROVE.md#当前研发记录)，旧交付结果保留原范围。L6 可以随重构变化，但不得反向覆盖 [L2 系统不变量](INVARIANTS.md)、[L3 领域模型](DOMAIN_MODEL.md) 或 [L4 行为合同](FOUNDATION.md)。

## 版本与运行形态

当前可发现世界轮候选为0.34.0／world37／state25：package 版本来自`pyproject.toml`和`agent_world.__version__`，世界及状态版本来自`shared_grove.world.make_world()`。`simulation.execution`继续从原canonical定义推导读写与休眠计划；`shared_grove.execution`维护持久成员、依赖索引和活动前沿。新增`domains`保存真实相互作用域及局部活动索引，`scheduling`持久协调同一公共逻辑轮，Runtime current checkpoint分别提交协调与域工作。`events`保存局部消费者待处理序列与共享事件事实，`commit`仍拥有单次行动或单个域任务的候选批次和统一落库。运行实例与新源码的范围分别记录，见[当前研发记录](SHARED_GROVE.md#当前研发记录)。

- Python package 要求Python 3.11+。当前源码 `/Users/mubin/Developer/agent-world/grove-composable-rules`，分支`codex/grove-composable-rules`，候选版本0.34.0。实现、测试及打包输入按每轮实际源码冻结，连接器、Runtime与UI的检查保留各自范围；唯一当前记录见[SHARED_GROVE](SHARED_GROVE.md#当前研发记录)，旧结果不自动适用于新源码。
- 物件整版本保持持久CAS；服务器交互修订区分自动派生输出和受保护事实变化。固定意图携带实际观察，提交仍裁决当前条件；原操作、资源、拓扑与定义不因暂忙或后续读取而替换。旧实体实际写入时补充修订，旧轮未结算任务通过state24同代次后继接续。
- Runtime protocol 常量：0.15；SDK API：1。
- 持久存储：file-backed SQLite，WAL，foreign keys 开启；`:memory:` 被拒绝。
- 同一 Runtime 的写事务由 SQLite `BEGIN IMMEDIATE` 与可重入 FIFO `WriterGate` 协调，等待者不被下一笔后台工作越过；规则注册另用 RLock。SQLite 同时只允许一个实际 writer，排队公平不能消除落盘延迟。
- World Definition 以受信任、同步 Python 回调在 Runtime 进程内执行。
- 组合入口可同时挂载 Web 产品原型、HTTP API、MCP、timer worker 和 retention maintenance。
- Runtime 本身不调用模型，也没有用户 Agent 执行器。

普通 HTTP 每次从请求 bearer 解析主体，不依赖上一段模型对话。当前 MCP 使用 `stateless_http=False`，`BearerIdentityMiddleware.sessions` 在进程内绑定 session_id 与 token_id；服务重启后客户端重新 initialize，但不改变用户身份或持久世界事实。组合入口将 HTTP、MCP、产品页及后台推进接到同一 Runtime，复用规则注册、写入排队和等待唤醒；独立进程仍由 SQLite 协调，世界状态和回执以数据库为准。这些通信／执行状态不保存用户的提示词、目的、私人记忆或模型推理进度。

world37／state25 的 `shared_grove.discovery` 维护公开创作身份索引，按作者／名称分页；位置与公开资格每次从当前实体复核。旧实体按保存区块有界回填，目录不携带操作版本或授予远程操作。`simulation.presentation` 从显式声明的 int／bool 字段解析颜色、字样、姿态及数字标记；旧 canonical 定义与摘要不改。Agent 查看容器先获得可见子物索引，子物件完整状态和操作版本需单独 inspect；选中的物件与动作预览保持完整。区域历史通过真实引用按需读取，省略不等于不存在。

原 Native 主会话的推理副本保留当前 user 及其完整工具轮，旧历史连同可选来源索引至多64KiB，并按当前请求让出空间；索引／旧外壳仍超可用空间时只省去完整旧推理parts，正式本人原会话历史工具仍可按需读取。无正式历史入口则拒绝发送，非白名单旧工具不承诺API原文可恢复。正式 SDK 发送前测量整个 JSON，硬上限128KiB。原 Native 输入、结果及用户原文保持，历史按本人身份和原会话有界查回。一次原生工具轮仍可能累计超过总预算，此时暂停而不截断当前结果；按字节提前收束与区分准备性读取／重复空转仍是后续调度问题。实际结论见当前研发记录，字节预算不冒充 token 上限。

**以下0.20—0.22段落为历史工作轮说明，不是0.32候选验收结果。**

0.20 新增 [River Camp 有界世界实验](WORLD_EXPERIMENT.md)：自由意图、裁判、本人接受和资源后果由独立 World Definition 消费现有 SDK。组合应用公开身份默认签发 24 小时凭据；没有新增 Runtime 原语或托管推理循环。

0.21 的 `examples.camp_campaign` 是独立的有限章节 World Definition，组合原 River Camp 的初桥规则。增加两趟固定外部补给、一次剧情涨水、共享仓库、一次应急箱和两种修复；共同目标与个人真实贡献分别派生，货运通行只是一项里程碑。第一趟补给关闭自由桥梁提案，后续使用已定义的章节行动；不宣称完整跑团或自动生成后续章节。`examples.river_camp_app --campaign` 选择独立 world_id／universe／8772，重用数据库前只读核对世界类型，不把原实验库改成新世界。前端显示当前阶段、可用行动与阻塞原因；回访对比仅缓存本浏览器的公开库存／路线／服务端时间，不缓存私注、权限或模型思考。该章节本身未改变 Runtime／protocol／SQLite schema。

0.22 修复多宿主接入暴露的 L6 缺口：身份持钥证明签入 control／observe，组合入口公开自己的凭据列表与逐枚撤销，新世界 Command Receipt 保存原提交 Principal／Credential。protocol 保持 0.15，SQLite schema 升为 8；不新增 AgentIdentity 或托管推理循环。当前验证范围见 [REFERENCE_GATE](REFERENCE_GATE.md)。

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
| Receipt | `operations.receipt_json`；新世界 Command 回执保存原 `actor_role_id / credential_id`。 |
| Commit | `world_commits` 及关联 `state_changes`。 |
| Notification Event | `events` / `EventSpec`。 |
| Shared Stream | `StreamSpec` / `StreamEvent` / `stream_events`。 |
| View | `ViewSpec` / `view_checkpoints`。 |
| Sync Position | recipient event seq、signed stream cursor、opaque view checkpoint。 |
| Control Lease | `activities` + `activity_claims`。 |
| Scheduled Effect（one-shot） | `TimerSpec` / `world_timers` / `timer_transitions`，执行 actor 为 `system:timer`；原ID、回执和重试合同不变。 |
| Scheduled Effect（current checkpoint） | `CheckpointSpec` / `world_checkpoints`，执行 actor 为 `system:checkpoint`；按key保存当前代次和最新精确回执，不为每代写Operation或one-shot Timer。 |
| Presentation | `PresentationCue`，event kind 为 `world.presentation`。 |

## Identity 当前状态

User Identity 的现行根事实是 Ed25519 公钥。客户端先调用 key identity challenge 接口；Runtime 返回一次性 nonce 和要签名的精确字节串。客户端用对应私钥签名后提交，Runtime 验签成功才承认“当前调用者持有这把身份密钥”。

`identity_keys` 按 `universe + public_key` 保存本世界映射。世界第一次见到某公钥时，在同一 SQLite 写事务里创建新的 `roles` 档案、保存映射并签发本地 `identity_tokens` Credential；以后同一公钥再次完成持钥证明时回到原来的本地 role。不同公钥永远创建新的 User Identity / 本地档案，不存在换钥继承、身份合并或密钥找回映射。

Participant Profile 完全由世界本地保存。身份公钥不会携带 display name、avatar、资产、关系或其他世界档案；同一公钥在 World A 与 World B 可以对应不同 `role_id`、不同本地资料和不同业务权限。

`identity_key_challenges` 保存短期 challenge、public key、过期时间、access_mode 和已签发的本地 token id。新 challenge 全为 `agent-world-key-identity-v2`：精确签名串是 `[version, universe, challenge_id, nonce, access_mode]` 以 NUL 分隔后的 UTF-8 字节，返回无 padding base64url 编码的 message。control 为默认，observe 必须在 challenge 时选择；exchange 按已签名的存储模式发行，不接受 bearer mint 或交换时改模式。相同 challenge 在网络响应丢失后可重放并恢复同一个 world-local Credential；challenge 不能把身份映射到另一公钥。当前身份 challenge 最长 300 秒。

schema 8 的新增 nullable access_mode 列保留原数据库 pending／used v1 challenge：NULL 继续使用四段 v1 control 签名串，已有 profile、key mapping 与 credential 不变；新请求不会发行 v1 challenge。

当前 world-local identity token 使用 `awid_` 前缀并只绑定 `role_id + universe`；不同世界不能互相接受 bearer token。Token revoke / rotate 只管理本地 Credential，不改变 User Identity 公钥。Join Ticket 仍是 operator 管理的本地角色接入工具，不定义 User Identity。

`GET /api/me/credentials` 要求有效 control Credential，只列同 universe、同 Principal 的元数据：`token_id / access_mode / created_at / expires_at / revoked_at / is_current / status` 及 scope，status 为 active／expired／revoked，不返回 bearer／hash。`DELETE /api/me/credentials/{token_id}` 在写事务取得锁后检查调用者及目标 scope，重复撤销成功；撤当前凭据为 422，未知／跨主体／跨世界均为相同 404。`POST /api/me/revoke-other-credentials` 继续撤销自己的其他未过期有效 Credential，保留当前 Credential；返回 `revoked_count / kept_token_id / expires_at`。

同一 Principal 的所有 control Credential 都有这些管理权限；外部宿主的 control 不低于浏览器 control，没有仅人类 owner 的管理边界。observe 能读取获准内容，不能写／重放写、改档案或管理凭据。用户可保留私钥并为每个宿主独立签发 bearer；凭据管理本身不能签发新 bearer，持有原私钥者仍能重新进入。宿主密钥保管及同 UID 进程隔离不是 Runtime 提供的保证。

世界 Command 在提交前解析有效 Credential，将 `actor_role_id / credential_id` 与结果一起写入原 Receipt；可信进程内调用、timer和current checkpoint的credential_id为null。同 Principal 换 bearer 查回执或重放保留原提交来源，旧 Receipt 不补填来源字段。Operation identity 仍为 `universe + actor_role_id + operation_id`，不是每 Credential 各自命名空间。事件、共享流和 journal 继续保存 Principal 归因，未新增事件级宿主作者；credential_id 不定义 AgentIdentity。

**密钥丢失没有恢复路径。** 没有原私钥就不能再次完成该公钥的持钥证明；旧本地档案可以作为历史事实保留，但对该用户而言成为不可再控制的死档。Runtime 当前没有、也不计划隐式提供密钥恢复、换钥继承或人工“认人”接口。

## 主要模块地图

| 责任 | 当前模块 |
| --- | --- |
| 包入口／CLI | `__init__.py`、`__main__.py`、`application.py`、`connect.py` |
| 应用生命周期／加载 | `asgi_lifecycle.py`、`universe_loader.py`、`builtin_worlds.py` |
| Runtime 组合与 identity core | `runtime_core.py` |
| Command / Query 执行 | `runtime_functions.py`、`world_context.py` |
| 状态 journal / commit | `runtime_journal.py` |
| 定向通知 | `runtime_events.py` |
| Control Lease | `runtime_activities.py` |
| View | `runtime_views.py`、`world_views.py` |
| Shared Stream | `runtime_streams.py`、`world_streams.py` |
| one-shot Scheduled Effect | `runtime_timers.py`、`world_timers.py` |
| current checkpoint与共用worker | `runtime_checkpoints.py`、`world_checkpoints.py`、`runtime_timers.py`、`timer_worker.py` |
| Retention | `retention.py`、`maintenance.py` |
| 作者 SDK 声明 | `world_sdk.py`、`world_types.py` |
| Presentation | `presentation.py`、`web/stream-client.js` |
| 共享 schema / errors | `runtime_contracts.py`、`runtime_errors.py`、`errors.py` |
| Gateway | `transport_contracts.py` |
| HTTP / MCP adapters | `http_app.py`、`mcp_app.py` |
| Identity / onboarding adapters | `identity_auth.py`、`identity_admin.py`、`onboarding_app.py`、`product_app.py` |
| Web 安全／诊断 | `web_safety.py`、`diagnostics.py` |
| Web prototype | `agent_world/web/` |
| 可选可信模拟辅助层 | `agent_world/simulation/`：注册、组合编译、材料批次、固定阶段快照与类型化效果；中立 Runtime 不依赖它。 |
| 当前官方世界 | `agent_world/worlds/shared_grove/`：材料参数与构造数据、准入、动作、过程、公共内容接续；`domains.py`拥有相互作用域和活动索引，`scheduling.py`拥有公共逻辑轮与分页协调。 |
| 自己的Agent／玩家页面 | `grove_connector.py`、`agent_world/grove_web/`；私人意图本机持久化，显式目标驱动委托，服务器描述驱动真实操作。 |
| 仓库内 demo/test consumers | `demo_universe.py`、`commons_universe.py`、`world_zero_universe.py`、`examples/` |

仓库内 demo、示例和 Reference Application 都不是架构来源。只有仍被当前测试执行的场景才提供当前证据；失去当前用途的消费者直接删除。

### Shared Grove 候选实现

world33／state23保留原不可变定义、真实材料数量、归属、空间操作、规则阶段和候选提交责任。`world_facts`提供固定定义来源的公开字段，`interactions`处理正式容器安装／卸载，`contact`沿有限安装引用和真实材料身份发现候选；不扫描所有发表定义。Agent和真人继续通过相同正式动作改变同一世界，Runtime不承担模型推理。

| 当前责任 | 实现位置与边界 |
| --- | --- |
| 相互作用域 | `domains.py`点查对象、可变读写依赖、容器关系、材料／机械关联及过程覆盖，保存域目录、归属、区域、ready／dirty索引；共享区块本身不等于同一作用域。 |
| 公共逻辑轮 | `scheduling.py`保存目标tick、准备／计算阶段、目录high-water、公共事件cutoff、分页游标和当前待结算页；每页最多8个域，不在协调事务中计算所有域。 |
| 局部规则计算 | `processes.advance_domain`在已接纳的完整域中推进物理和声明规则；`simulation`保留具体机制的快照、效果、冲突与数量语义，`commit`统一提交该次候选批次。 |
| 持久执行与回执 | Runtime的`grove.frame`与`grove.domain.<id>` current checkpoint独立提交；保存当前任务状态、代次水位和最新精确回执，协调器读取真实终态，不能仅等世界回调。 |
| 公共事实接续 | `events.py`保存共享事件和局部待处理序列；新帧固定事件cutoff，失败不提交材料变化或消费者游标，已有不可变事件事实不充当可变依赖边。 |

原来世界级32个活跃声明参与者的限制改为完整相互作用域的有限预算；域之间通过ready目录分页推进，不把世界人口或同一区块所有物件直接当作执行前沿。域内的对象、材料、区块、事实及Runtime单事务预算仍保留，不能把一个不可分的超大相互作用图强行拆开后声称结果等价。

容量或规则任务失败只回滚该域任务，已提交的其他域后果保留。协调器将该域记为pressure／fault并移出ready，关闭其本帧工作；没有实际计算时`last_applied_tick`、物质及事件消费游标保持。`last_tick`用于帧协调，不证明物质已推进。实际输入或关系发生变化后重验并重新唤醒，当前帧中新唤醒的域从下一目标tick计算一次，不补做暂停期间的步数。当前地图和inspect读取域状态、最后实际计算步和错误事实。

world33不再为每个物理步创建新的one-shot Timer。`grove.frame`协调公共轮，`grove.domain.<id>`执行局部工作；这些任务复用同一Runtime事务与原TimerWorker。已有one-shot Timer及其历史转移、用户Operation、已发表定义和物件状态保留；state23迁移只交接调度与建立域索引，不重建现场或把历史文字转换成物件。

停机期间不推进世界。`WorldDefinition.resume(ctx)`在同一个Runtime首次接入已有世界时，于受管安装事务内接续保存的调度；新initialize和同Runtime重复安装不再执行该hook。迁移已恢复工作时以`ctx.world_resumed=True`避免再次恢复。Shared Grove的`resume`继续未完成帧的未完成工作，已关闭帧保留步数并安排当前时间之后的下一步，不追赶停机墙钟；普通队列迟到不再被当作停机丢弃。公共合同期限仍按同一个`clock.sequence`，不引入每域独立时间。

### 历史材料接触与0.26组合执行阶段

以下保留原工作轮说明与证据范围，不作为world33的当前调度说明；当前范围见 [SHARED_GROVE 当前研发记录](SHARED_GROVE.md#当前研发记录)。当时world24／state15／definition_publish4／rule_catalog2／rule_action5；state15沿原 `resume_clock` 正式交接唯一兼容计时任务，保留tick、期限和全部世界状态，不追赶墙钟。`contact.py`按真实同槽canonical材料读取原不可变包，保留物性快照的有限身份，不增加全局可执行规则池。编译器增加可选顶层接触声明，旧定义缺此字段时保持原canonical形状／摘要；MaterialDef不承载执行代码。旧 authored reaction→mechanical→after 后，接触共用剩余输入，输出下一步参与；统一账户及候选批次提交。普通容器入料和树关系变动沿共同commit准入，预留4材料、8批次、8KiB、可达产物及子容器破裂后可能到达父容器的液体组合，完整步共享164事实／512写／2MiB。默认不逐容器写账户；私有和合盖内部接触只保存授权可读事实，不发布内部过程。网页与Agent详细读取当前条件、缺口和历史实际数量；场景仅保留安全摘要及按需inspect入口。

**0.26.0候选引擎已冻结，普通玩家组合执行完成，自主完整体验未通过。** 新表面为 `grove.rule_catalog / grove.definition_publish / grove.definition_read / grove.rule_action / grove.control_grant / grove.control_revoke / grove.control_status`。规则目录、定义读取和控制状态提供获准事实；合法声明发表自动完成，无人工审批；控制委托与收回作用于同一角色的未来行动，不改变 Ed25519 身份、世界本地档案或凭据体系。

可选`agent_world/simulation/`已接入有界声明解释器，定义不可变且对象固定引用版本。反应按真实量／残余／原子extent／容量结算，独立竞争与同族排他分开，产物到后续逻辑步参与。机械统一freeze network snapshot→solve→commit，一网络一source，有界`load_field / speed_field`提供实际输入，真实木燃料提供有限能量；循环／双source／过载／耗尽保留位置、角度并停止，不产生新耗能。程序只写获准authored fields，owner／position／security禁止，机械位置仅solver写入。运行越界只标记该实例`process.error`，不停止全世界；机械成功后after声明错误保留已求解机械结果，回滚该实例的声明字段／材料。实际字段错误需合法declared action修复或新版本实例，不改已发表定义。

公共journal、启用后记录器及固定条款合同复用世界状态和原事务。`recorder.window_ticks`与`contract.duration_ticks`都按`clock.sequence`，不使用模拟`*_seconds`；停机暂停、重启接续，不按墙钟追赶。事件`at`仅来源时间，生成机会lease和连接timeout才是运维墙钟。occurred／recorded／read_by分别保存；actor真实接受、材料delivery／event、deadline的完成／失败／release分别成立。`contract.completion_when`读取服务器实际条件并结合材料交付；托管量排除规则反应消费。真实Agent发表的混合器合同已由普通模拟人完成2u交付与记录读取，现场验证及未验证条件保留各自范围。

外部连接器接入显式目标授权下的read→plan→act→readback→adapt，单人单任务；没有目标时不因行为／探索／发表自动推理。取消未来未执行动作，未知提交仍恢复原Receipt，已完成不回滚。普通问句、内容创作与委托分开。服务器不跑模型，也不在每physics step唤醒模型。声明执行每phase非事件规则按rule.id一次，事件规则按事件sequence外层及同事件rule.id排序；when读phase快照，effect逐条更新，产物输入额度下一逻辑step才开放。两个身份原key接续成立；实际委托、工具操作、取消与恢复各按已发生证据验收，不由发表成功推定全链通过。

**以下为此前0.26组合执行阶段的证据范围；当前服务入口、源码冻结及验证结论统一见[SHARED_GROVE当前研发记录](SHARED_GROVE.md#当前研发记录)。** 该阶段世界8900、A8901、B8902。独立数据库为`/Volumes/DevDisk/DeveloperData/agent-world/test-work/shared-grove/creative-rules/world.sqlite3`；两侧private profiles为`/Volumes/DevDisk/DeveloperData/agent-world/private/shared-grove/creative-rules/connector-a`与`/Volumes/DevDisk/DeveloperData/agent-world/private/shared-grove/creative-rules/connector-b`，native runtimes为`/Users/mubin/Developer/agent-world/work/creative-runtime-a`与`/Users/mubin/Developer/agent-world/work/creative-runtime-b`。这些阶段端口和验证数量不是当前启动指引或最终整树结论。

本轮先前完整Python568项通过；随后connector／runtime_core／UI及新回归更新，最新变更由38项针对检查、creative7项、材料accounting单项与两份Node文件通过覆盖，不能把先前全量结果写成最新整树通过。最终包build／install与probe通过。A真实发表echo、机械v1与修正v2，v1开闭／扭矩错误保留；主任务模拟人通过正式`content_submit`放置4物件，不改记为自主操作。

本轮10次模型请求全部结束：A第9次真实发表合法混合器与合同，B第10次原Operation接受成功（commit_seq3982）后推理超时，无未知写，Agent自主完整目标未通过。普通模拟人沿同一世界、同一真实Agent不可变定义转移wood1＋cloth1形成产物2u；关桥交付拒绝且不扣材料，运行中仅StateConflict而未验通条件。最终stop source后真实走(5,0)／(4,0)／(5,0)，交付2u，原contract为complete、delivered2，recorder读取成功，普通玩家组合执行完成。旧开桥／不变持续运动拒绝未完成现场验证；逐轮事实与失败仅在唯一研发记录接续。

**既有0.25实现与历史范围：**

官方世界通过原 SDK、事务、版本、Receipt、Stream 与持久 Timer 消费可选模拟辅助层。材料定义、构造组件／数量／成本／缩放和规则版本留在世界的 `definitions.py`；编译器根据这些数据生成已有能力组合，新构造键不需要新增名称分支。

权威物质是整数数量的 `matter` 批次，分别属于本体、内容、表面和吸附槽；旧水量、湿度、燃料字段只作派生投影。物理父关系唯一，装卸、绑定、过载、燃烧与破损共用同一对象 ID 与当前保护边界。固定阶段只处理有界活动闭包；预算和准确行为合同集中在 [SHARED_GROVE](SHARED_GROVE.md)。

`grove.generation_context / grove.content_submit / grove.content_history` 提供真实材料预算、注册组合、公共锚点、贡献与后果的分页接续；本轮沿此路径实例化新定义，扣真实区域资源与机会预算，不另造独立演示。创建本身不隐式绑定或使用，后续人或获准Agent按实际行动规则操作。私人连接器的 `/connector/context` 保存本机意图和偏好，未作为服务器事实发布。前端由服务器动作描述生成安全操作，使用当前版本并保留未知提交原回执；新对象共享默认表现和逻辑比例fallback，完整上传未实现。

原独立构造失败、549首跑失败、分页fixture漏读16／17及96根timer拒绝保留；第二side_pouch仅加数据的4项获认可，阶段557与后续相关结果保留源码范围。最终完整562项／186.956秒、0 fail／error／skip通过；此前同562项／351.363秒有2 fail为旧runner 0.3秒输出截止，owned child均reaped，断言源码未改、两单项同断言4.014秒通过。最终两Node文件、9个401／403／404 target／item／combination实际函数回归及JS语法通过；当前缓存 `composable-object-access`。最终文字版本的wheel build／install全部probe通过，记录为 `composable-package-release-final.log`，SHA只在唯一外置索引。

两真实 entry、原 join Receipt 200 与同身份／主会话零推理 recover 已成立，冷启动 45 秒超时仍在。A 布兜装真实 40u 木到 (4,2)，B 公共可读、携带时 403；B 输出长度与纠正机会超时的失败后，单请求实际接续 3 条 outcome、content 4→5 并发表两部件。实际附接与浅溪吸水使原布兜过载破损，bag／cargo／float IDs 保留公开可取，未成功运过溪。

模型最终 **14＝A5＋B9**。第13是目录／路由ID不匹配；第14正式Zen路径单次回答改善，但已读水壶载荷160＞木箱承载48、过载112／不能漂浮后仍建议装入，原A5归责／容量及B6／B7浮材死重错误保留，可靠指导未完整通过、best0.24不变。原Go／LongCat／main／计数已恢复，两auto false、零新推理，两页最终ready；45秒冷启动与健康后读取超时保留。实际UI将A原干布store进B袋，B close／open／pickup时A403，移动／drop后原袋v6、32／16／32／100和原干布ID保持，双方content18／2贡献／16后果。子物件刷新选择、关闭拒绝／打开可读与草稿历史接续通过；桌面／窄屏截图已检查，只是代理局部覆盖。独立8899关闭timer、原credentials HTTP恢复两人player／inventory／content、5实体和5原Receipts精确一致，备份20000768 bytes；恢复服务TERM退出143，临时恢复副本已清理。最后页面读回确认原身份／会话／计数保留，两页ready；最终包记录及本地交付提交见唯一外置索引，不push。

### Commons Reference Application

当前 `commons` 内置世界合同版本为 3，state version 仍为 1，定义的领域对象仅有：

- `Post`：公开持久内容，函数 `commons.post.create/get/list`。
- `Reply`：公开且引用一个 Post，函数 `commons.reply.create/list`。
- `Conversation`：两个本地 Participant Profile 之间的私有持久容器，函数 `commons.conversation.open/list`。
- `Message`：Conversation 内的私有持久消息，函数 `commons.message.send/list`。

Conversation/Message 的参与者授权、Post/Reply 的公开读取、Operation replay、recipient Event 通知和重启恢复都复用现有 Runtime 能力。Conversation 打开或 Message 通知只说明对应世界事实／通知已经提交，不自动形成好友关系、已读、接受或业务确认。当前切片没有 Friend、Block、Read Receipt 或通用 Request/Response 状态机。

四个 `*.list` Query 的 function version 为 2；按 `updated_at DESC, state_key DESC` 稳定排序，接受 `limit` 和可选 `after`（1..4096 字符），保留原结果数组并返回 `has_more / next_cursor`。把 `next_cursor` 作为下一页 `after` 可读取更早历史；游标校验 universe、scope、prefix 和排序，私聊每页重新检查参与者资格。默认页大小依次为 Post 20、Reply 50、Conversation 50、Message 100，上限均为 100。序列化结果超出 255 KiB 时有界缩页，为 Runtime/transport envelope 保留 1 KiB；游标始终对应实际末条，不跳过缩页后的记录。

`commons.post.get` 按 ID 点查。Post 点查、Reply 创建／列表引用不存在的 Post 时，返回 `ResourceNotFound`：HTTP 为 404，MCP 为 `isError=true` 并保留同一结构化错误，另附 tool 名称。

## 当前 SQLite schema

当前 schema `user_version` 为8；原`identity_key_challenges.access_mode`保留已有v1证明，current checkpoint表、回收元数据和索引以加法方式初始化，不改用户Operation身份。主要表：

- identity / entry：`roles`、`identity_keys`、`identity_key_challenges`、`role_world_presence`、`identity_tokens`、`join_tickets`、`identity_token_rotations`。
- world registry：`world_definitions`、`function_registry`。
- current state / operations：`world_state`、`operations`。
- commit journal：`world_commits`、`state_changes`、`state_history_floors`；新checkpoint来源另有`checkpoint_history_floors`和`world_commits.checkpoint_reclaimable`。
- recipient events：`events`、`event_floors`。
- coordination：`activities`、`activity_claims`。
- timers：`world_timers`、`timer_transitions`。
- current checkpoint：`world_checkpoints`，每个`universe + checkpoint_key`一行，保存当前generation、completed_through、最新终态代次／精确Receipt／commit引用及当前状态。
- shared streams：`stream_events`、`stream_heads`。
- view cache：`view_checkpoints`。
- runtime metadata：`runtime_meta`。

表名只是 L6 存储选择，不等于 L3 概念必须一一对应。

current checkpoint代次不写入`operations`、`world_timers`或`timer_transitions`；其状态、后果及最新回执仍与`source='checkpoint'`的commit在同一事务提交。`latest_commit_seq`引用、共享流和原timer转移保护仍需引用的commit；引用释放在自身事务更新回收资格，不依赖下次worker恰好看到那次释放。live state及各类commit引用有独立索引，待执行队列按索引取有限候选。

## 当前公共 Python SDK

`agent_world.__all__` 当前导出：WorldRuntime、WorldDefinition、FunctionSpec、StateRule、RetentionPolicy、PresentationCue、StreamSpec、StreamEvent、ViewSpec、TimerSpec、TimerInvocation、CheckpointSpec、CheckpointInvocation、RetryTimer、FunctionContext、FunctionOutcome、EventSpec。

包根通过 `__getattr__` 按需从原模块导入并缓存这些对象，`TYPE_CHECKING` 保留静态类型导入；`__all__`、star import、原对象同一性与未知名称的 `AttributeError` 行为保持。单独导入 `agent_world.connect` 不再预加载 Runtime／jsonschema 依赖图。延迟导出不创建代理对象、不更改 Identity／Credential 语义，也不启动服务。

WorldDefinition当前可声明functions、state rules、state authorizer、state forget authorizer、bootstrap/initialize/migrations/resume、views、timers、checkpoints、retention、streams等；这些可选能力不是所有世界的必选模型。FunctionContext对世界作者暴露受管state／timer／checkpoint／stream／role等helper，不再暴露公共数据库连接。

`CheckpointSpec`声明同步handler及其输入／可选输出schema、版本、授权和有限重试参数；handler以`(ctx, args)`执行，通过`ctx.checkpoint`读取本代调用信息。`schedule_checkpoint(..., expected_generation=...)`严格排下一代，`get_checkpoint`可读取本事务已排的后继，`cancel_checkpoint`取消当前任务；当前正在执行的同代不能自行取消。提交重新检查代次，旧代缺失的精确回执为`CheckpointReceiptExpired`，不能据此换编号重做。`get_checkpoint_receipt()`默认返回最新终态回执，可能仍是上一代失败；当前代结论必须核对generation和status。watermark表示旧代已被处理或取代，不等于旧代全部成功执行。

`forget_state(scope, key, expected_version=...)`只物理回收已删除、版本匹配且受信`state_forget_authorizer`明确允许的墓碑；授权回调只读，普通handler不能直接删除state。Shared Grove仅允许已保存序号水位证明永不复用的公共事件事实key。墓碑回收不制造第二次事实删除、版本或history，物件、身份、不可变定义和用户回执不沿此路径清理。

### 历史本地验证与有界模型工具

以下保留0.21等历史宿主和验证基础设施说明；不将其模型结果或检查数量套用到当前候选。

0.21 新阶段仅执行四个有界语义宿主，前三组六条有效输出、第四组超时；真实执行封存旧 7f helper。超时暴露 watchdog 与主动回收重叠，当前修复将主动停止汇入关闭 SIGALRM 后的唯一有界回收，保留权限失败诊断；新 helper 2da199da 的 28 项离线回归通过，不声称再次真实推理验证。新语义 runner 的真实阶段仍封存执行摘要并拒绝新源码复用，fixture 单独封存当前 helper。下述十次 GM／A／B 与清理记录属于原初桥实验，预算不重置。

`tests/live_server.py` 的 async 生命周期在线程中启动／停止自有服务；Commons 网络测试使用异步 HTTP，持钥初始化在线程完成。失败与取消保留 server log、原异常及清理 cause；自有进程未回收时不删除数据库目录。启动取消继续等待清理，原 `CancelledError` 保留 caller message／notes／cause，不吞掉残留进程诊断。原 60 秒启动、5 秒 HTTP、10 秒连接模块 import 限制未放宽；这些是验证基础设施修复，不改变 Runtime 行为合同。

`tools/run_river_camp_models.py` 是固定本机实验工具，非 Runtime Agent 宿主。每次显式续接只启动一个选择的模型步骤，原尝试与续接／小子集共同受十次推理用 CLI 宿主限制。异常、取消与 deadline 统一终止／回收自有进程组、关闭资源并保存脱敏失败；不能回收就保留 profile、标 unfinished、拒绝后续推理。构造 `Popen` 时先延后信号取消，取得 child handle 后立即检查原期限，避免在已创建子进程但尚无句柄时丢失清理能力；阻塞构造不声称受 Python watchdog 硬抢占。正常 EOF 在原剩余期限内等待最多 3 秒，再必要终止；TERM／KILL 各配置 3 秒回收等待。账本原子替换并有失败 fallback，metadata refresh 与旧冻结证据分开；记录仅为宿主／CLI 时间与已报告 usage，provider 请求次数、未报告尾段及账户账单仍未知。

B 的无 CLI 事件超时与无凭据版本检查暴露本机启动路径阻塞后，工具增加显式定向存储选项：内部 binary 须与官方 SHA 完全一致、本人所有且 0700，真实路径及全部祖先无符号链接；私有 profile 的任何 Git 祖先在写凭据前拒绝。B 特例仍用原提示／逻辑 Operation，检查回执缺失、无 B intent 和 A 既有状态，旧计数与失败不重置。models.dev 连接失败后仅新增显式官方 GitHub 同源元数据选择，默认源不变；固定 provider／base TOML 三项费用显式零、工具能力继承或覆盖，Go 价格 DOM 绑定正确 tab／tabpanel、六表头及唯一免费行。专用 no-host proof 绑定源码／价格文件摘要与 300 秒时效，profile 准备后和 ledger 写完、启动前再次核对；过期不启动模型、不计新宿主。

第八宿主实际 GM 格式错误只新增显式恢复入口：保留精确 SchemaRejected、八次计数及原证据；第九只允许同 Operation、新 stage，原提示追加三句外层格式说明，不提供裁判答案。第九失败阻止后续推理；第十只能由 B 同钥新进程接受真实已获准意图，先核第九成功、同外层 Operation／参数内无 Operation 与 B 意图、持久事实一致。实际 GM9／B10 已完成，全部十次额度用完；两个语义子集及旧探针入口停用，原 28 条有效覆盖 0。实际执行源码 e10e08d7 与测试 99ea1831 已封存在唯一源码 ZIP，后续代码仅补 macOS 清理库存，不改变模型结果、Runtime 或世界规则。

多位置清理按各 run 的精确记录路径，在全部 PID 与一次当前 UID 文件占用盘点通过后才删除。实际 macOS 输出使旧清理两次安全停止：自动 f 字段与非文件系统空名称；最终 `-Fptfn` 严格关联 p／f／t／n，仅数字描述符的 NPOLICY／NEXUS／PIPE／rte 可以无名称，REG／DIR／未知空名称和异常完整性仍保留。非空名称始终检查精确目录／子路径及内部 binary，不保存无关文件名。现场只读库存先通过，随后七个 profile 和独立核验的临时内部 CLI／公开 HTML 已精确清理；活动环境、服务、密钥、数据库、唯一备份和证据保留。最终清理源码 7f2ed1da、测试 d419ca83，24 项 synthetic 回归 0.817 秒通过；条目分配量与观察到的可用空间变化分开，不宣称 APFS 净释放。

当前本机性能环境原路径为 `repo/work/latency-runtime`，与原外置环境共享相同 base Python 和 35 个相同依赖版本，通过已有缓存离线建立；小型身份／数据库测试目录放 Git 外的任务 `work/test-work`，不弱化私有文件保护。原外置缓存、环境、大型资产、真实数据库与固定服务不迁移。该环境与 0.21 最终代码／fixture 共同通过 398 项完整 Python（50.229 秒，`campaign/final-python-verified.log`）、五份 Node 与 0.21.0 package 检查；测量、失败原记录、缓存／源码混杂限制和启动方法统一见 [WORLD_EXPERIMENT](WORLD_EXPERIMENT.md)，不把环境选择当 Runtime 原语或存储持久性策略改变。

## 当前 core tool surface

固定 core tools 当前包括：

- entry/discovery：`world.bootstrap`、`world.describe`、`world.discover`。
- receipt：`world.get_receipt`。
- recipient events：`world.get_changes`、`world.wait_changes`。
- activities：`world.start_activity`、`world.claim_activity`、`world.renew_claim`、`world.finish_activity`。
- views：`world.list_views`、`world.view_snapshot`、`world.view_sync`、`world.view_timeline`。
- streams：`world.list_streams`、`world.read_stream`、`world.wait_stream`。

WorldDefinition 中声明的 functions 另外动态映射为可调用 world tools。HTTP adapter 映射到同一 Gateway 语义；完整 route 以 `http_app.py` 为代码事实。

MCP 写工具的 description 与 input schema 都明确要求 `operation_id` 位于调用 envelope 顶层，与 `arguments` 并列，例如 `{"operation_id":"stable-id","arguments":{"post_id":"p1","text":"hello"}}`。同意图重试复用原 ID；缺少顶层 ID 的错误同样提示该位置。

## 当前 HTTP / 产品入口

HTTP Runtime API 包含 health/whoami、function discovery/invoke、bootstrap/describe、views、streams、public views/streams、receipt、activities、changes 等路径。组合应用与 standalone onboarding 都公开 key identity challenge / exchange，用于“公钥 + 含权限模式的持钥签名 → 本世界档案 + world-local Credential”；这是正常用户／Agent 的默认公开入场路径，不要求 operator 预建 Role 或 Join Ticket。组合产品入口另有 own credential list／single revoke／revoke others，MCP 不增加凭据管理 tool。standalone onboarding 的 operator 管理面仍保留 role、join ticket、token rotate/revoke 与 rotation receipt，不能当作普通用户接口。鉴权请求的主体只来自 credential，`role_id` 不属于鉴权调用参数。

`agent-world-connect` 默认 control，可用 `--access-mode observe` 申请只读凭据；helper 验证 v2 challenge 的模式和签名串，并核对 exchange 的模式。Commons 连接权限选择器使用相同证明；下载名为 `agent-world-opencode-{mode}-{token_id末8位}.json`，便于与列表 ID 对应。关闭／取消／离开或换身份会取消当前 UI 请求并拒绝迟到响应；下载或列表不证明宿主已运行。helper 管理的 POSIX owner-only 文件也不证明同 UID 宿主之间存在 OS 沙箱。

HTTP Runtime API 与 MCP 均拒绝重复 Authorization 请求头；已鉴权的目录入口同样要求省略 role_id。不可 UTF-8 编码的 JSON／标识符和越界时长以结构化请求错误拒绝。

这些路径是当前 L6 表面，不应被 L1-L3 当成概念定义。

## 当前关键上限

| 项目 | 当前值／范围 |
| --- | --- |
| structured arguments | 64 KiB |
| 单个 state value | 256 KiB |
| result / event batch budget | 256 KiB；单次 outcome 最多 128 events |
| managed state changes per commit | 512；历史前后内容合计 2 MiB |
| history page payload | 1 MiB |
| WorldDefinition declarations | views ≤64，timer handlers ≤64，checkpoint handlers ≤64，streams ≤64 |
| View | ≤256 entities、≤64 resources、总 payload 96 KiB |
| View checkpoint | TTL 300s；每 viewer/universe 16；全局 1024 |
| Stream | read page ≤100 events，page payload 192 KiB；cursor lifetime 86400s |
| Stream declaration | retention ≤365d，max_events ≤100000 |
| wait | 当前最大 30s |
| timer＋checkpoint commands per transaction | 合计32 |
| timer／checkpoint argument payload | 各64 KiB |
| pending one-shot timers per universe | 10000；不是current checkpoint的全世界活动上限 |
| timer／checkpoint retries | max_attempts 1..10，retry delay ≤86400s；仅显式RetryTimer有限重试 |
| Runtime due-work sweep | limit 1..100；可选monotonic deadline在任务之间让出 |
| Shared Grove域调度 | 每页8域；单个完整域的32声明参与者等预算仍有限 |
| retention config | event/history seconds ≤315360000；row limits ≤10000000 |

这些数值可能同时具有“当前公共行为限制”和“实现容量保护”两种性质。修改前需要判断是否会改变 L4 客户端可观察合同。

## worker 与 maintenance

组合入口默认timer poll interval为1s，可关闭；TimerWorker默认batch为25，interval最低0.05s。one-shot仍使用单次sweep开始时固定的候选和原timer_id身份，后续排出的one-shot需下一次sweep；current checkpoint可以在同次sweep的有限预算中执行后继，按持久ready顺序及工作类别公平取候选。

原worker每个时间片先以`limit=1`且不传deadline给一个到期任务原子处理机会；即使候选选择writer等待已超过100ms，也不会反复只读队首、永不推进。之后最多4个批次，共≤100个选中工作，按原约100ms的monotonic deadline在任务之间让出；`run_due_timers`显式deadline语义不变。已到期工作让出约10ms再续，空闲按`next_due_work`的索引队首时间等待、不超过配置interval，异常至多约100ms后复查。单个可信规则调用不能硬抢占，不能把该时间片写成绝对响应保证。每个任务都有独立事务，worker不为整个逻辑帧持有写锁，也不调用模型。

定义checkpoints的世界在原TimerWorker内接入checkpoint历史回收，按实际checkpoint提交、state change和stream event数量累计两倍扫描／删除credit。累计64个提交、4096条change或512条流事件预算时触发，未达到批次也每30s最迟补扫，持续ready时同样执行。每轮最多6次有界pass；单pass扫描≤256个提交／5000条流事件、删除≤106496条change，在成功拿锁和BEGIN后计算约25ms计算预算，SQL单元间让出；空闲／尾批至少提供8个提交、8条流事件及4096条change的恢复预算。未完成credit和backlog保留至后续pass，触及fresh政策窗口时释放不再需要的credit。跨流按单个内存游标轮转，首流积压不能饿死其他流；重启丢失游标不会丢失持久候选资格。

`apply_work_retention`以同一事务先释放过期stream引用，再回收checkpoint journal；既有`apply_stream_retention`／`apply_checkpoint_retention`复用相同事务helper，公共返回与各自策略保持原合同。域任务继续独立事务，没有合并世界帧写入或降低FULL。默认旧提交超过近期4096个checkpoint commits的数量窗口或86400秒时具备回收资格，最新精确回执及其他实际引用继续保护。旧用户`operations`和精确Receipt不清理，checkpoint回收只删除新checkpoint来源的旧commit和关联`state_changes`；独立completeness floor明确标记历史已不完整，不伪装完整历史或降低同步revision。最新世界状态与代次水位不清理，因此删观察历史不重新执行旧代。

Shared Grove保存独立`cadence_anchor_at`：正常轮打开时锚定，封帧后后继due为`max(封帧时刻, 锚点+1s)`；恢复时只重锚节拍，不改原`opened_at`、逻辑目标、事件cutoff及已结算域。单个长事务或FULL持久化仍可能造成真实迟到，不提供硬实时保证。

活动Runtime的`work_diagnostics(limit=32)`与现有HTTP `/health?diagnostics=true`提供有界主机计时；默认health不包含诊断；带诊断的入口仅接受实际loopback连接源，不依据转发头放行，远程请求返回403。最近128笔宿主工作以内存保存，读取最多32笔，累计phase仅含固定阶段：连接、writer等待、BEGIN、handler wall／thread CPU、managed、COMMIT、回滚、两类维护及WAL回填。WAL回填独立于世界写锁，另记忙状态、总帧、已回填帧和待回填帧。记录只含受信handler标签、代次／提交水位、计数和时间，不保存调用参数、凭据、对象正文或异常详情；超过一秒的动作／定时／checkpoint／维护写出同类无参数慢日志。诊断读取不用数据库写锁，重启清零，不另建持久trace或调试服务。

现有宿主维护线程同时承担SQLite官方PASSIVE回填，约一秒通过官方NOOP查询真实待回填量；空积压跳过，前台繁忙时至多延后5秒获得一次维护机会，未变的受读快照限制积压不每秒重复回填。宿主SQLite不支持NOOP时沿原PASSIVE路径；其它查询错误不伪装成空积压。回填独占租用连接但不取得Runtime写锁；世界声明retention或streams时仍每30s按原政策有界维护。仅该维护生命周期内，全部写连接在租用时设置wal_autocheckpoint=0；正常关闭或线程异常结束后后续租用恢复默认1000，裸Runtime保持默认策略。同步FULL不变、动作与域任务事务不合并，不在线强制RESTART/TRUNCATE或删WAL；PASSIVE的busy、log、checkpointed和pending帧及耗时进入有界主机诊断，busy=0不等于完整回填。长读快照仍可能阻止回填并使WAL增长，约一秒的检查间隔不是硬时间／空间保证。方法采用[SQLite官方WAL说明](https://sqlite.org/wal.html)与[PASSIVE接口语义](https://sqlite.org/c3ref/wal_checkpoint_v2.html)，不是外置盘专属逻辑。普通retention policy及one-shot历史契约保持原责任。

也可以分别运行 timer worker 和 retention maintenance CLI。导入 package 本身不会安装常驻系统服务。

## Presentation 当前映射

`PresentationCue` schema version 为 1，channel 当前为 `action / speech / intent`，phase 为 `start / finish / cancel`，单 cue payload 上限 16 KiB。它可以生成定向 EventSpec 或发布为 StreamEvent。

timeline 复用 View checkpoint，只返回当前 viewer 获准且 subject 当前可见的 presentation events。这个机制是表现辅助，不是业务动作状态机。

## World Package 数据访问边界

`FunctionContext`不再公开数据库连接。World Package的受支持数据路径是声明过的state／timer／checkpoint／stream／role helper；实现内部仍持有私有transaction handle以完成这些操作，但它不是SDK合同。checkpoint排队、取消和state墓碑回收分别通过受管API，不能绕过代次、CAS、权限或提交预算。

`FunctionContext.get_role` 按 `identity_keys / identity_tokens / join_tickets / role_world_presence` 中的现有 world 关联限制档案读取：已关联其他 universe、未关联当前 universe 的 role 返回 `RoleNotFound`。完全没有 world 关联的可信 SDK 角色仍可读取；`WorldRuntime.get_role` 保留 operator 全局查询能力。这里不新增成员表，也不把 world 关联当作具体动作授权。

当前 World Package 仍是部署方信任、与 Runtime 同进程执行的 Python 代码，因此这项收缩只是减少数据库形状耦合，并 **不把当前架构升级成恶意插件沙箱**。若未来要执行互不信任的第三方世界代码，仍需要独立的进程／权限／存储或真正的沙箱边界。

## 结构化错误与 Capability Harness

当前实现包含通用结构化恢复错误合同：

- `runtime_errors.py` 的 `WorldRuntimeError` 可携带可选 `recovery`、`retry_after_seconds` 与 `details`；显式 `retryable` 必须是 boolean。
- `details` 当前要求为 JSON object，递归深度有界，序列化后最多 8 KiB；非有限数字、非字符串 key 和任意 Python 对象会在构造错误时被拒绝。
- `transport_contracts.py` 将相同字段映射到 HTTP/MCP 结构化错误；未知内部异常仍被清洗为通用 `InternalError`。
- HTTP 在存在 `retry_after_seconds` 时额外发送整数秒 `Retry-After`；401 仍只在鉴权错误路径发送 `WWW-Authenticate: Bearer`。
- 常见恢复提示已覆盖缺少／失效身份、scope 不匹配、无效输入、Join Ticket 失效、函数版本／发现变化、observe/control 不匹配和 StorageBusy；世界规则也可在 `RuleViolation` 等 Runtime error 上附带安全的恢复元数据。

当前仓库使用 `tests/capability_harness.py` 和 `tests/fixtures/capability_matrix_world.py`。它们是 **确定性的 Runtime 测试基础设施，不是新的 Reference Application 或领域模型**。Harness 参与者现在通过正式 Key Identity challenge / signature / exchange 进入，而不是直接预造 Role / token。当前矩阵覆盖：

- 1→N 定向 fan-out，并验证同 Operation 重放与 Runtime 重启后 Receipt／事件仍可恢复。
- N→1 并发 fan-in，验证多个写者向同一目标聚合且非目标主体不能读取私有聚合。
- N→N mesh，验证多主体同时 fan-out；撤销其中一个凭据后，该主体立即失效而其他主体继续运行。
- 同一密钥获取多个本地 Credential、单 Credential 撤销后持钥重进、Runtime 重启后回原档，以及同一密钥跨独立 Runtime 只共享身份而不共享本地 profile / bearer。
- 同一新密钥并发首次进入只创建一个本地 profile；32 个不同密钥可并发进入且 profile / credential 保持唯一。
- 同一密钥的两枚本地 Credential 可同时建立两个 MCP session，并归因到同一 profile。
- 同对象 CAS 并发竞争恰好一个提交者成功，另一个收到 `StateConflict`。
- HTTP/MCP 对同一个带 recovery/retry/details 的规则错误保持结构化语义一致。

这批 fixture 不创建 Conversation、Friend、Party 或其他产品对象，因此不能反向定义 Runtime 领域模型。以后新增拓扑／故障组合时优先扩展 Harness，而不是为每个实验重新造一个大场景。


## 当前明确实现差距

1. **外部系统副作用交付没有通用合同。** Runtime store 内部提交不能自动覆盖支付、第三方 API、文件等外部写入；尚无统一 outbox / delivery / compensation 能力。
2. **历史0.26.0冻结候选的自主完整体验未通过。** 普通模拟人已完成真实Agent定义的混合器合同组合执行，但B接受后推理超时，Agent未自行完成全目标；运行中交付条件、旧开桥／不变持续运动拒绝仍未完成现场验证。分层检查与最终包probe不覆盖这些体验及验证缺口。后续候选的实际范围与结论仅在[当前研发记录](SHARED_GROVE.md#当前研发记录)接续。
3. **完整美术上传未实现。** 当前方向为既有本地皮肤和新对象共享默认比例fallback，不宣称已建立上传系统。

共同事项模型、第三方／群众公裁等仍属于 [OPEN_DESIGN](OPEN_DESIGN.md) 的产品／领域设计问题，不能因为“尚未实现”就假装它们已经被证明应该进入 Runtime。

跨机器／跨主机 Agent、网络分区与重连、长期 soak、生产数据库迁移／备份恢复、浏览器轨迹和更广平台矩阵属于验证缺口而不是 L6 功能缺口，统一记录在 [REFERENCE_GATE](REFERENCE_GATE.md)。
