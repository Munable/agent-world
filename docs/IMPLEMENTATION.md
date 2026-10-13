# L6：当前实现

复核：2026-10-13。本文描述当前源码0.37.0／world40／state25及公开表面；本轮本地实现检查已通过，实际范围见当前研发记录。当前源码范围、模型实验和交付检查统一见[SHARED_GROVE 当前研发记录](SHARED_GROVE.md#当前研发记录)，旧交付结果保留原范围。L6 可以随重构变化，但不得反向覆盖 [L2 系统不变量](INVARIANTS.md)、[L3 领域模型](DOMAIN_MODEL.md) 或 [L4 行为合同](FOUNDATION.md)。

## 版本与运行形态

当前源码为0.37.0／world40／state25，版本实际值以源码为准：package 版本来自`pyproject.toml`和`agent_world.__version__`，世界及状态版本来自`shared_grove.world.make_world()`。`simulation.execution`继续从原canonical定义推导读写与休眠计划；`shared_grove.execution`维护持久成员、依赖索引和活动前沿。`domains`保存真实相互作用域及局部活动索引，`scheduling`持久协调同一公共逻辑轮，Runtime current checkpoint以独立逻辑结果组成有限物理持久批次。`events`保存局部消费者待处理序列与共享事件事实，`commit`仍拥有单次行动或单个域任务的候选批次和统一落库。运行实例与新源码的范围分别记录，见[当前研发记录](SHARED_GROVE.md#当前研发记录)。

- Python package 要求Python 3.11+。当前源码 `/Users/mubin/Developer/agent-world/grove-composable-rules`，分支`codex/grove-composable-rules`，接手基线为759e221／0.36.0；本轮源码不自动替换原活动服务。实现、测试及打包输入按每轮实际源码冻结，连接器、Runtime与UI的检查保留各自范围；唯一当前记录见[SHARED_GROVE](SHARED_GROVE.md#当前研发记录)，旧结果不自动适用于新源码。
- 物件整版本保持持久CAS；服务器交互修订区分自动派生输出和受保护事实变化。固定意图携带实际观察，提交仍裁决当前条件；原操作、资源、拓扑与定义不因暂忙或后续读取而替换。旧实体实际写入时补充修订，旧轮未结算任务通过state24同代次后继接续。
- Runtime protocol 常量：0.15；SDK API：1。
- 持久存储：file-backed SQLite，WAL，foreign keys 开启；`:memory:` 被拒绝。
- 同进程、同一实际数据库路径的Runtime共享可重入FIFO `WriterGate`，SQLite `BEGIN IMMEDIATE`仍为实际写者边界；规则注册另用RLock。有限后台批次在项间检查前台等待，不能越过已排队的下一笔事务；跨进程仅保证SQLite排他，不声称FIFO。排队公平不能消除磁盘落盘延迟。
- World Definition 以受信任、同步 Python 回调在 Runtime 进程内执行。
- 组合入口可同时挂载 Web 产品原型、HTTP API、MCP、timer worker 和 retention maintenance。
- Runtime 本身不调用模型，也没有用户 Agent 执行器。

普通 HTTP 每次从请求 bearer 解析主体，不依赖上一段模型对话。当前 MCP 使用 `stateless_http=False`，`BearerIdentityMiddleware.sessions` 在进程内绑定 session_id 与 token_id；服务重启后客户端重新 initialize，但不改变用户身份或持久世界事实。组合入口将 HTTP、MCP、产品页及后台推进接到同一 Runtime，复用规则注册、写入排队和等待唤醒；独立进程仍由 SQLite 协调，世界状态和回执以数据库为准。这些通信状态不保存私人提示词、记忆或模型推理进度；世界可保存用户明确提交的有限授权目标摘要，不能把它当完整私人上下文。

`shared_grove.discovery` 维护公开创作身份索引，按作者／名称分页；位置与公开资格每次从当前实体复核。旧实体按保存区块有界回填，目录不携带操作版本或授予远程操作。`simulation.presentation` 从显式声明的 int／bool 字段解析颜色、字样、姿态及数字标记；旧 canonical 定义与摘要不改。Agent 查看容器先获得可见子物索引，子物件完整状态和操作版本需单独 inspect；选中的物件与动作预览保持完整。区域历史通过真实引用按需读取，省略不等于不存在。

原 Native 主会话的推理副本保留当前user及完整工具轮，旧历史连同来源索引至多64KiB，并按当前请求让出空间；正式SDK全JSON硬上限128KiB。执行段已有完整终态工具对时在96KiB边界可信让出，依次封段、确认原会话空闲、排空写入、恢复原回执和读回，再沿原任务接续；首包超限或来源无法核验仍暂停。首次有效准备读取另记并跨段去重，不增加动作、回执或完成证据。普通问答只有读工具，并使用独立宿主请求状态；历史目标仅作背景，不能授予当前控制。分段使用推理副本，不PATCH原Native记录。每次明确接续最多追加8次，原任务累计上限32次，动作额度及已用请求不归零。实际结论见当前研发记录，字节预算不冒充token上限。

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
| Receipt | `operations.receipt_json`；新世界 Command 回执保存原 `actor_role_id / credential_id`，原意图证明使用持久 `operations.args_hash`。 |
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

新回执、回执查询与原操作重放携带 `intent_fingerprint`，由原持久参数摘要证明 function 与 arguments；查询旧记录不改写原 receipt_json。共享客户端和宿主原记录绑定端点、角色、真实 bootstrap.universe 及完整封包摘要；恢复前重新核对世界归属，回执验真后才登记结果、解除未知闭锁。旧本地未决记录缺少归属或原封包时保留未知，不猜测、不自动重放；当前授权已停止仍可只读查询原回执。宿主的推理预算、撤控与排空条件继续由原执行控制器管理。

**密钥丢失没有恢复路径。** 没有原私钥就不能再次完成该公钥的持钥证明；旧本地档案可以作为历史事实保留，但对该用户而言成为不可再控制的死档。Runtime 当前没有、也不计划隐式提供密钥恢复、换钥继承或人工“认人”接口。

## 五个责任的模块地图

下列模块路径相对 `agent_world/`。

| 责任 | 当前实现位置与边界 |
| --- | --- |
| 世界内核与世界规则 | `runtime_*.py`、`world_context.py`、`world_sdk.py`与`world_types.py`提供身份、受管事实、授权、事务、Operation／Receipt和定时；`simulation`提供受信机制原语，`worlds/shared_grove`定义材料、作用域、规则与共同候选提交。内核不依赖宿主或renderer。 |
| Agent交互契约与可复用客户端 | `worlds/shared_grove/agent_contract.py`提供发现引导、读结果上下文及护栏；`grove_agent_client.py`复用读取、观察绑定、稳定意图与未知结果恢复；`grove_agent_observation.py`管理实际已呈现事实，`grove_agent_ports.py`定义HTTP／MCP transport及OperationJournal，`grove_agent_errors.py`分类错误。这些模块不导入OpenCode宿主。 |
| 宿主adapter | `grove_connector.py`的GroveSession、`grove_execution.py`的ExecutionController及`grove_native_history.py`的NativeToolHistory管理具体OpenCode接入、私人上下文、有限授权目标、推理预算、取消与本机请求接续；通过可复用客户端读写世界，不复制世界状态权威。 |
| 人类交互API | `worlds/shared_grove/client_projection.py`给授权事实、外观、动作、控件、分页候选及结构化输入；`building.py`的共同build_plan提供建设菜单与提交前置条件。输入仍交原动作handler重验、原事务提交，投影不新增业务规则。 |
| renderer | `grove_web/grove-client.js`、`grove-world-ui.js`、`grove.js`及样式消费版本化语义、保留人的选择、输入并呈现回执／恢复状态；不解析作者AST或根据能力名重算资格。 |

组合入口由 `application.py`、`universe_loader.py`、`asgi_lifecycle.py`组装；`http_app.py / mcp_app.py`经共同Gateway编码传输，`identity_auth.py / identity_admin.py`及产品／入场adapter提供持钥进入。`timer_worker.py / maintenance.py / retention.py`执行有界持久工作，`web_safety.py / diagnostics.py`承担安全渲染和有界主机诊断。这些支撑模块不产生另一套规则或提交。

仓库内demo、示例和Reference Application只服务当前用途，不成为架构来源；技术、模型和UI的实际证据统一见当前研发记录。

### Shared Grove 当前计算与存储

当前世界保留不可变定义、真实材料数量、归属、空间操作、规则阶段和候选提交责任。`world_facts`提供固定定义来源的公开字段，`interactions`处理正式容器安装／卸载，`contact`沿有限安装引用和真实材料身份发现候选；不扫描所有发表定义。Agent和真人继续通过相同正式动作改变同一世界，Runtime不承担模型推理。

| 当前责任 | 实现位置与边界 |
| --- | --- |
| 相互作用域 | `domains.py`点查对象、可变读写依赖、容器关系、材料／机械关联及过程覆盖，保存域目录、归属、区域、ready／dirty索引；共享区块本身不等于同一作用域。 |
| 公共逻辑轮 | `scheduling.py`保存目标tick、准备／计算阶段、目录high-water、公共事件cutoff、分页游标和当前待结算页；每页最多8个域，不在协调事务中计算所有域。 |
| 局部规则计算 | `processes.advance_domain`在已接纳的完整域中推进物理和声明规则；`simulation`保留具体机制的快照、效果、冲突与数量语义，`commit`统一提交该次候选批次。 |
| 持久执行与回执 | Runtime的`grove.frame`与`grove.domain.<id>` current checkpoint保留独立逻辑提交和准确回执，以有限批次共用FULL物理提交；整批持久后才对外确认，协调器读取真实终态。 |
| 公共事实接续 | `events.py`保存共享事件和局部待处理序列；新帧固定事件cutoff，失败不提交材料变化或消费者游标，已有不可变事件事实不充当可变依赖边。 |

原来世界级32个活跃声明参与者的限制改为完整相互作用域的有限预算；域之间通过ready目录分页推进，不把世界人口或同一区块所有物件直接当作执行前沿。域内的对象、材料、区块、事实及Runtime单事务预算仍保留，不能把一个不可分的超大相互作用图强行拆开后声称结果等价。

容量或规则任务失败只回滚该域任务，已提交的其他域后果保留。协调器将该域记为pressure／fault并移出ready，关闭其本帧工作；没有实际计算时`last_applied_tick`、物质及事件消费游标保持。`last_tick`用于帧协调，不证明物质已推进。实际输入或关系发生变化后重验并重新唤醒，当前帧中新唤醒的域从下一目标tick计算一次，不补做暂停期间的步数。当前地图和inspect读取域状态、最后实际计算步和错误事实。

每个物理步不创建新的one-shot Timer。`grove.frame`协调公共轮，`grove.domain.<id>`执行局部工作；这些任务复用同一Runtime事务与原TimerWorker。已有one-shot Timer及其历史转移、用户Operation、已发表定义和物件状态保留，不重建现场或把历史文字转换成物件。本轮state25保持，不为接口整理迁移已有事实。

停机期间不推进世界。`WorldDefinition.resume(ctx)`在同一个Runtime首次接入已有世界时，于受管安装事务内接续保存的调度；新initialize和同Runtime重复安装不再执行该hook。迁移已恢复工作时以`ctx.world_resumed=True`避免再次恢复。Shared Grove的`resume`继续未完成帧的未完成工作，已关闭帧保留步数并安排当前时间之后的下一步，不追赶停机墙钟；普通队列迟到不再被当作停机丢弃。公共合同期限仍按同一个`clock.sequence`，不引入每域独立时间。

### Shared Grove 声明、材料与行动

`definitions.py`维护材料、构造组件、数量、成本和缩放；`simulation`编译有界声明并解释受限效果，`creative.py`管理固定版本定义与实例行动。`contact.py`只按有限安装引用、真实同槽canonical材料和不可变包发现候选，不扫描所有发表定义；`world_facts.py`读取固定来源公开事实，`interactions.py`处理正式安装／卸载。候选沿physics→reaction→mechanical→after→contact阶段结算，后续产物不在同一步递归消费。

`matter`整数批次是材料权威，水量、湿度和燃料只作派生投影。物理父关系唯一，装卸、绑定、过载、燃烧和破损保留原对象身份与保护边界；`commit.py`共同处理数量、空间、守恒、实际回执与状态提交。实例准入预留4材料、8批次、8KiB及后续可达产物，每域共享164过程事实／512写／2MiB预算；公开与私人内部过程分别按授权投影。

`grove.definition_publish / definition_read / definition_list`提供不可变发表与按需读取，`source_definition_ref`固定公开改作来源但不迁移旧实例。`cost`定义经 `generation_context / content_submit`消耗区域资源与机会；`cost_u`定义经 `fabrication_options / grove.fabricate`消耗本人选定真实批次。发表、成型、启用和实际功能后果分别成立；`content_history`按cursor接续原公共贡献与结果。

`building.build_plan`共同裁定建设预览和真实提交前置，`grove.build_options`只读提供当前菜单、材料候选、有限预览及正式后续读取，不物化地块、不造资源。`grove.build`仍在原事务内重验当前条件、真实转移、最终载荷和域容量。结构化 `failure / precondition / read_requests / recovery`由规则提供，不靠解析拒绝文案恢复；`available`只表示当前已检查条件通过，未检查的最终容量明确为unknown。

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

### 可复用Agent客户端与世界引导

`grove.agent_contract`是只读version1函数，bootstrap的world_entry_state.view提供 `agent_contract_request`。合同给当前Agent函数与access、`grove.fixed-observation.v1`写绑定政策、声明schema推导的guard_paths、entry_requests和receipt-first恢复要求；精确input/output schema仍由发现目录给出。map.viewport、grove.inspect、definition_list/read、fabrication_options和build_options原事实结果附 `agent_context / read_requests`，保持Agent自己的事实粒度，不经过人类DTO。

`GroveAgentClient`从bootstrap正式请求读取合同并分页发现schema，返回真实bootstrap的固定observation_id供明确首次grove.join使用，复用 `AgentObservations`固定实际呈现事实后构造写入护栏。它接收调用方已初始化的HTTP／MCP transport，显式注入OperationJournal与可选before_write授权钩子；不管理模型、会话、目标、预算或停止。原function/envelope及role/world scope在发送前持久保存，未知结果阻止新写；recover默认只查原Receipt，显式replay才重发原envelope。JsonOperationJournal用调用方指定稳定路径，MemoryOperationJournal不提供重启恢复；换transport不换原操作身份，重启后旧observation失效，未决envelope仍可恢复。可执行用法见 [Agent客户端接入](INTEGRATION.md#shared-grove-agent-客户端接入)。

### 本机固定运行边界

已复用的Python环境为 `/Users/mubin/Developer/agent-world/repo/work/latency-runtime`；原依赖、OpenCode可执行文件、各人的profile与Native运行目录保持固定位置。源码在本机活动工作树，数据库、大型验证物及必要证据沿现有DevDisk Agent World目录。使用当前模块命令会加载当前cwd的源码，固定解释器不冻结代码版本；实施中的工作树不得直接重启原库。实际运行范围、原argv及恢复条件见 [当前入口与恢复保护](SHARED_GROVE.md#当前入口与恢复保护)。

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

current checkpoint按固定ready候选页组成物理持久批次，每批至多8项；多项批次聚合界为2048条state change和4MiB journal before/value字节（在metadata历史剥离前计实际变化字节）。每项复用原handler与managed效果路径、独立journal marker、逻辑`world_commits`、代次和精确Receipt，普通handler失败在SAVEPOINT内隔离。已有结果后，后续合法项超过本批聚合界时连同其回执／后继整体回滚并保留原待执行代次，下批重新校验；不会留半项或消耗尝试次数。首个合法项若单独超过物理聚合策略，则完整保留为单项FULL提交并立即收批，diagnostic记`oversized-singleton`，不拆项或无限延期。原逻辑单项512条change／2MiB before/value上限继续由`_record_commit_tx`校验，本轮没有改变；当前合法项因此不会触及生产聚合界，单项退路确保以后收紧物理策略仍不改变合法项语义。批内计算预算从成功BEGIN后约25ms计起，项间检查原deadline和前台等待，有前台排队便收批。规则调用及FULL提交不能硬抢占，单项最坏时长仍非硬上界。只有外层FULL提交成功后才返回整批结果、唤醒等待者并产生维护credit；数据库错误、BaseException或外层COMMIT错误不得被吞成某域失败回执。实际提交后响应丢失保持未知结果恢复语义。普通Operation及one-shot Timer不入此批；one-shot候选尚未处理时checkpoint每次只选一项，保留工作类别轮换。

同一进程的Runtime按`Path.resolve()`后的数据库路径共享可重入FIFO WriterGate，弱引用注册仅在没有Runtime／等待者持有该gate后自动释放；关闭一实例不关闭其他实例的队列。不同进程仍由SQLite `BEGIN IMMEDIATE`及代次重验协调，不承诺跨进程FIFO。批内新后继只在下个物理批次重新选择，sweep的seen继续阻止立即重试同代。

worker每个时间片给首个到期工作一次准入机会：checkpoint世界首调用至多8项并沿原约100ms monotonic deadline，`ensure_initial_work=True`只允许候选选择晚到后仍处理首项，后续项／批次不重置期限；无checkpoint世界首调用仍为`limit=1`。之后最多4次调用，共≤100个选中工作。普通`run_due_timers`未指定ensure_initial_work时保持既有deadline行为。已到期工作让出约10ms再续，空闲按`next_due_work`的索引队首时间等待、不超过配置interval，异常至多约100ms后复查。worker不为整个逻辑帧持有写锁，也不调用模型。

定义checkpoints的世界在原TimerWorker内接入checkpoint历史回收，按实际checkpoint提交、state change和stream event数量累计两倍扫描／删除credit。累计64个提交、4096条change或512条流事件预算时触发，未达到批次也每30s最迟补扫，持续ready时同样执行。每轮最多6次有界pass；单pass扫描≤256个提交／5000条流事件、删除≤106496条change，在成功拿锁和BEGIN后计算约25ms计算预算，SQL单元间让出；空闲／尾批至少提供8个提交、8条流事件及4096条change的恢复预算。未完成credit和backlog保留至后续pass，触及fresh政策窗口时释放不再需要的credit。跨流按单个内存游标轮转，首流积压不能饿死其他流；重启丢失游标不会丢失持久候选资格。

`apply_work_retention`以同一事务先释放过期stream引用，再回收checkpoint journal；既有`apply_stream_retention`／`apply_checkpoint_retention`复用相同事务helper，公共返回与各自策略保持原合同。维护不加入世界效果批次，checkpoint维护credit仍按成功持久批次中的独立逻辑提交计算，FULL不降低。默认旧提交超过近期4096个checkpoint commits的数量窗口或86400秒时具备回收资格，最新精确回执及其他实际引用继续保护。旧用户`operations`和精确Receipt不清理，checkpoint回收只删除新checkpoint来源的旧commit和关联`state_changes`；独立completeness floor明确标记历史已不完整，不伪装完整历史或降低同步revision。最新世界状态与代次水位不清理，因此删观察历史不重新执行旧代。

Shared Grove保存独立`cadence_anchor_at`：正常轮打开时锚定，封帧后后继due为`max(封帧时刻, 锚点+1s)`；恢复时只重锚节拍，不改原`opened_at`、逻辑目标、事件cutoff及已结算域。单个长事务或FULL持久化仍可能造成真实迟到，不提供硬实时保证。

活动Runtime的`work_diagnostics(limit=32)`与现有HTTP `/health?diagnostics=true`提供有界主机计时；默认health不包含诊断；带诊断的入口仅接受实际loopback连接源，不依据转发头放行，远程请求返回403。最近128笔宿主工作以内存保存，读取最多32笔，累计phase仅含固定阶段：连接、writer等待、BEGIN、handler wall／thread CPU、managed、COMMIT、回滚、两类维护及WAL回填。`checkpoint-batch`单样本记录`batch_candidates/logical_results/batch_changes/batch_history_bytes/batch_stop_reason/batch_status`；status明确区分staging、staged、committed、rolled_back与unknown。按kind累计`physical_commit_attempts/physical_commits/logical_results_committed`，前两项仅计写连接的SQLite COMMIT调用尝试／正常完成，不计只读事务结束，也不把API调用次数直接等同设备fsync次数；最后一项只在外层COMMIT正常成功后累加，不把内层RELEASE当作落盘。WAL回填独立于世界写锁，另记忙状态、总帧、已回填帧和待回填帧。记录只含受信handler标签、代次／提交水位、计数和时间，不保存调用参数、凭据、对象正文或异常详情；超过一秒的动作／定时／checkpoint-batch／维护写出同类无参数慢日志。诊断读取不用数据库写锁，重启清零，不另建持久trace或调试服务。

现有宿主维护线程同时承担SQLite官方PASSIVE回填，约一秒通过官方NOOP查询真实待回填量；空积压跳过，前台繁忙时至多延后5秒获得一次维护机会，未变的受读快照限制积压不每秒重复回填。宿主SQLite不支持NOOP时沿原PASSIVE路径；其它查询错误不伪装成空积压。回填独占租用连接但不取得Runtime写锁；世界声明retention或streams时仍每30s按原政策有界维护。仅该维护生命周期内，全部写连接在租用时设置wal_autocheckpoint=0；正常关闭或线程异常结束后后续租用恢复默认1000，裸Runtime保持默认策略。同步FULL不变，普通动作不加入checkpoint物理批次，不在线强制RESTART/TRUNCATE或删WAL；PASSIVE的busy、log、checkpointed和pending帧及耗时进入有界主机诊断，busy=0不等于完整回填。长读快照仍可能阻止回填并使WAL增长，约一秒的检查间隔不是硬时间／空间保证。方法采用[SQLite官方WAL说明](https://sqlite.org/wal.html)与[PASSIVE接口语义](https://sqlite.org/c3ref/wal_checkpoint_v2.html)，不是外置盘专属逻辑。普通retention policy及one-shot历史契约保持原责任。

也可以分别运行 timer worker 和 retention maintenance CLI。导入 package 本身不会安装常驻系统服务。

## Presentation 当前映射

### Shared Grove 双向人类契约与消费路径

本轮 `shared_grove.client_projection` 的projection_version为2；`grove.client_viewport / grove.client_inspect`函数版本为2，`grove.client_definitions`为3，`grove.client_choices`为1。它们沿现有 Query／成员授权读取，公开输出 JSON Schema 校验 context、EntityDTO、FactDTO、appearance、ActionDTO／controls／choices和蓝图 list／detail／fabrication 三模式。`context.snapshot_seq` 引用 `ctx.read_snapshot_seq`，与 Query envelope 同一个 state_changes 读取水位；不使用 tick 或 Receipt commit_seq 排序。新增 DTO 不改旧不可变定义、保存实体或 Agent 的原事实接口，完整合同见 [INTEGRATION](INTEGRATION.md#shared-grove-双向人类契约-v2)。

`grove-client.js`核对 Query 与 context 的函数版本、世界、主体和水位，拒绝旧响应及未知契约的新写入，原未决操作继续使用 CommonsClient 的原 Operation／Receipt 锁与恢复。`grove-world-ui.js`只消费正式材料、fact_sections、appearance、navigation、controls及制作来源菜单；原从 state/program/能力名猜显示与行动的 helper 已移除。`grove.js`地图、选中详情、动作表单和蓝图浏览器实际迁移到新函数，写回复中的旧原始 viewport 不直接进入新地图；提交后重新读取 DTO。服务器仍提供动作资格、候选、预览、固定护栏和写入裁决。

动作表单按 controls 的明确 path 收集参数、应用选中候选 arguments_patch，按schema及声明的最大值依赖校验；目录候选沿discovery_request／next_request读取、用原choice.read_request重验，保留未发送草稿、原选择和护栏；定时刷新不能自动改变用户意图。超预算动作以 `expanded:false` 返回 outline，点击后按正式 read_request 展开。蓝图 detail 返回服务器类型选择与安装资格；fabrication 的inputs.schema与intent.input_schema的selection_path定义同一输入，指定真实来源批次、数量与正式成型调用，现有精确数量计划和恢复路径保留。

地图先画图形，再以稳定身份统一排布物件标记和人物名，同格多件物品有独立行并避开邻近文字；完整选择器保留每件身份和独立数字。详情以当前摘要和折叠事实分组控制密度，unknown／historical／published 显式区分。连接器网页 readback 只提供行动目标索引与 client_read_request，原历史执行证据保留；点击得到当前 DTO，不能冒充历史行动读回。本节描述当前实现；本轮没有进行新的视觉或真人体验验收。

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
2. **自然创作的完整功能正确性仍有缺口。** 接手基线的新湿润作品没有成功实现自动变色／计次，固定实例与历史结果仍被模型误认；本轮接口和模块收敛的实现检查不补判该自然创作功能缺口。实际范围只在[当前研发记录](SHARED_GROVE.md#当前研发记录)接续。
3. **完整美术上传未实现。** 当前方向为既有本地皮肤和新对象共享默认比例fallback，不宣称已建立上传系统。

共同事项模型、第三方／群众公裁等仍属于 [OPEN_DESIGN](OPEN_DESIGN.md) 的产品／领域设计问题，不能因为“尚未实现”就假装它们已经被证明应该进入 Runtime。

跨机器／跨主机 Agent、网络分区与重连、长期 soak、生产数据库迁移／备份恢复、浏览器轨迹和更广平台矩阵属于验证缺口而不是 L6 功能缺口，统一记录在 [REFERENCE_GATE](REFERENCE_GATE.md)。
