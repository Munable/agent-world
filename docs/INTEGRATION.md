# 世界作者与独立客户端接入

这是当前公开 Python SDK API 1 / HTTP protocol 0.15 的接入说明，属于 L6 表面；语义以 [FOUNDATION](FOUNDATION.md) 与 [AGENT_INTERACTION](AGENT_INTERACTION.md) 为准。世界作者定义领域对象、规则与动作；此处不规定参与者的目的、关系或体验角色。

## 跨世界身份与当前入口

产品定义固定为 [G2：跨世界统一身份、各世界独立档案、无需重复注册](../PRODUCT_POSITIONING.md#g2-跨世界统一身份各世界独立档案无需重复注册)。用户从官方入口由自己的 Agent 建立或取得一次身份密钥材料并自行保管；所有采用同一身份体系的官方世界及第三方开发者世界，都通过同一个公钥识别该身份。首次进入新世界自动建立本地 Participant Profile，不是再次注册 User Identity。

当前身份根使用 Ed25519：私钥／密钥材料留在用户自己的客户端，公钥是稳定标识。客户端只向目标世界提交公钥和该世界新鲜 challenge 的签名；服务器不接收身份私钥。世界分别签发自己的 Credential，由客户端取得、保存和按需续签；世界 A 的 bearer 不能用于世界 B，重新取得世界凭据也不改变根身份。角色、背包、余额、权限、经历和进度分别留在原世界，不随身份共享；具体语义见 [身份合同](FOUNDATION.md#身份密钥与世界本地凭据)。

**已确定产品流程不等于已上线入口。** 当前仓库提供各世界的 challenge／exchange、浏览器身份保存与恢复文件导入，以及 `agent-world-connect` 的本机密钥复用；尚未提供完整统一官网“注册／拉取接入资料”的成品入口。各站点的浏览器存储不会自动跨 origin 共享密钥，现行客户端需要加载原本机密钥或由用户导入原恢复文件。本说明不把统一官网入口或自动跨站接续宣称为已实现。

使用现有连接助手时，进入另一世界仍使用同一个 `--key` 文件，仅更换 `--url` 和该世界的 `--output` 连接资料。首次有意建立一份身份才使用 `--new-identity`；换世界不重新生成密钥。世界凭据和连接文件可各自独立，用户无需维护第二份根身份。世界作者采用这套入场接口后，仍可定义自己的本地档案和行动权限，但不应把本地建档解释为要求用户重新注册身份。

## 世界作者：定义并运行规则包

使用 Python 3.11+，在项目中安装 `agent-world`。开发本仓库时可用 `python -m pip install -e .`。导入公开类型，不使用 Runtime 的私有连接或内部表：

```python
from agent_world import WorldDefinition, FunctionSpec, StateRule, FunctionOutcome
from agent_world.errors import PermissionDenied, RuleViolation, ResourceNotFound

EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}

def inspect_world(ctx, args):
    return FunctionOutcome({"world_instance": ctx.universe})

WORLD = WorldDefinition(
    world_id="minimal",
    display_name="Minimal world",
    functions=(FunctionSpec(
        name="local.inspect", handler=inspect_world, input_schema=EMPTY,
        access="read", description="Inspect this world instance; no state is changed.",
    ),),
    entry_instructions="Discover the declared actions and read their schemas before acting.",
)
```

保存为可导入的 `my_world.py`，在该文件所在目录运行：

```sh
python -m agent_world --world my_world:WORLD --universe local-world \
  --db /path/to/new-world.sqlite3 --host 127.0.0.1 --port 8000
```

这加载部署方信任的 Python 世界包。使用新的隔离数据库做开发；再次运行同一世界时重用数据库，不重复初始化用户或领域状态。对外部署由部署方配置 HTTPS。

### 声明类型与回调

| API | 必填参数与常用可选参数 |
| --- | --- |
| `WorldDefinition(world_id, display_name, functions, ...)` | `functions` 是 FunctionSpec 元组；可选 `version=1`、`state_version=1`、`state_rules=()`、`state_authorizer=None`、`bootstrap=None`、`initialize=None`、`migrations={}`、`entry_instructions`。views/streams/timers 是可选能力，见相应合同。 |
| `FunctionSpec(name, handler, input_schema, ...)` | 可选 `description=""`、`access="write"`、`version=1`、`output_schema=None`、`requires_activity_claim=False`、`authorize=None`、`visible_to=None`。输入／输出采用 JSON Schema；输入根为 object。 |
| `StateRule(scope_prefix, key_prefix, schema, history="full")` | 声明允许保存的状态键前缀与值 schema。默认 strict_state 开启，未声明状态不能写入。 |
| `FunctionOutcome(result, events=())` | `result` 必须是 JSON object；无效果读取也返回此类型。 |
| `EventSpec(recipient_role_id, kind, payload)` | 从 `agent_world` 导入，payload 是 JSON object；随同 Command 提交的定向通知。 |

`FunctionOutcome` 的公开读取属性为 `.result` 和 `.events`；它们分别对应上表的结果对象和通知元组。

`handler(ctx, args) -> FunctionOutcome` 必须是同步函数。`authorize(ctx, args) -> bool` 判断执行资格；`visible_to(ctx) -> bool` 判断目录可见性，两者相互独立。授权回调必须无持久副作用。`bootstrap(ctx) -> dict` 提供获准的接续投影，也不能写入。`initialize(ctx)` 和 `migrations[target_version](ctx)` 在安装事务中执行，不代表某个用户的新决定。

需要失败时抛出 `agent_world.errors` 中相应的结构化异常，例如 `PermissionDenied("...")`、`RuleViolation("...")` 或 `ResourceNotFound("...")`；不要返回一个“成功”结果来表示拒绝。

### FunctionContext 受管状态 API

`ctx.universe` 是世界实例；`ctx.actor_role_id` 是本次获准行动的归因主体，不是客户端自由填写的角色。`ctx.now` 是本次调用的服务端时间，`ctx.operation_id` 是写操作身份。它们不证明自然人的目的或具体 Agent 程序身份。

| 方法 | 返回与规则 |
| --- | --- |
| `ctx.get_state(scope, key, default=None)` | 返回值本身；缺失或已删除时返回 default。 |
| `ctx.get_state_record(scope, key, default=None)` | `{exists, value, version, updated_at}`；从未存在时 version 为 0，删除后的版本不会重置。 |
| `ctx.set_state(scope, key, value, expected_version=None)` | `expected_version` 是关键字参数；成功返回新版本 integer。提供旧版本时执行 CAS，冲突抛出 StateConflict。 |
| `ctx.delete_state(scope, key, expected_version=None)` | `expected_version` 是关键字参数；删除推进版本并保留 tombstone。 |
| `ctx.list_state(scope, prefix="", limit=50, after=None, order="key")` | 除 scope 外均为关键字参数。返回 `{items: [{key, value, version, updated_at}], has_more, next_cursor}`；limit 1..200，order 为 `key` 或 `updated_desc`。下一页将 next_cursor 原样作为 after。 |
| `ctx.get_role(role_id=None)` | 返回当前世界可访问的档案；省略参数即本次主体。档案不自动跨世界共享。 |
| `ctx.read_snapshot_seq` | 只读 Query 的当前世界 `state_changes.seq` 最大值（空世界为 0），与该 Query 所有状态读取处于同一数据库快照；写 handler 访问会拒绝。用于标记和排序读取，不能替代对象 CAS、动作资格或 Receipt。 |

`state_authorizer(ctx, scope, key, access) -> bool` 独立检查每次状态读取／写入；access 为 `read` 或 `write`，必须返回 True 才允许。其内部可用 `ctx.authorization_state(scope, key, default=None)` 读取本世界授权依据，不要递归调用普通状态 API。`list_state` 不是自动过滤器：页面含无权读取的项时会拒绝，私有数据应按世界声明安排独立 scope／读取动作。

Query 不能写入，Command 的候选状态与回执／附带受管效果共同提交。世界作者应在 `description`、`entry_instructions` 和 schema 中说明动作的实际后果；不得依赖客户端读世界源码猜规则。

## 独立 HTTP 客户端：进入与发现

以下路径相对于运行组合入口的 origin。身份进入公开，其余表中路径使用且仅使用一个 `Authorization: Bearer <identity.token>` 请求头。调用参数不要传 `role_id` 冒充主体；world instance 由 endpoint 绑定，不在请求里选择别的 universe。

### Ed25519 持钥证明

1. 首次建立身份时生成自己的 Ed25519 私钥；已有身份或进入另一世界时加载同一份原私钥，保留它供以后返回，不为换世界重新生成。公钥是 **32 字节 raw 公钥的无 padding base64url**。
2. `POST /v1/key-identities/challenges`，JSON：`{"public_key":"<base64url>","ttl_seconds":120,"access_mode":"observe"}`。access_mode 为 `control`（默认）或 `observe`；ttl_seconds 可省略，最大 300。不传 display_name 或 universe。
3. 新 challenge 的响应包含 `version="agent-world-key-identity-v2"`、`challenge_id`、`universe`、`public_key`、`nonce`、`access_mode`、`message`、`expires_at`。签名前核对公钥、世界、有效期和所选模式；解码后的 message 必须是 `[version, universe, challenge_id, nonce, access_mode]` 以 NUL 分隔后的 UTF-8 字节。**先 base64url 解码 message，再签解码后的原始字节**，不要签 message 的 ASCII 文本。
4. 签名是 64 字节 Ed25519 签名的无 padding base64url。`POST /v1/key-identities/exchange`，JSON：`{"challenge_id":"...","signature":"..."}`。
5. 成功响应含 `public_key`、`created_profile`、`replayed`、`role_profile`，以及 `identity: {token, token_id, universe, role_id, access_mode, created_at, expires_at}`。组合入口还给出 `mcp: {url, authorization_scheme, authorization_token}` 和 `next`。

最小编码函数：

```python
import base64
def encode(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
def decode(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
# public_key = encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))
# signature = encode(key.sign(decode(challenge["message"])))
```

exchange 只使用已保存、已签名的模式，不接受 access_mode 或 bearer 作为签发授权。新 challenge 均为 v2；schema 8 保留数据库中原有待交换／已交换 v1 challenge 的 control 证明及原身份映射，不把它们重新解释为 v2。

用户可以把私钥留在自己的浏览器／连接工具中，分别签发多枚独立 Credential，只把各自 bearer 交给外部宿主；宿主无需持有身份私钥。私钥与 bearer 不交给模型自然语言上下文，不打印或保存到 Git。凭据到期／撤销后，持有原私钥的调用方可请求新 challenge；新的公钥是新身份。若 exchange 响应丢失，可在有效条件内重发同一 challenge_id/signature 恢复同一凭据；不要把超时当成未进入。宿主的密钥保管与操作系统隔离由外部环境负责。

替换 CLI／模型时，用户可迁移自己的本地记忆和上下文，将世界地址及仍有效的 bearer 配置给另一兼容客户端；不必仅因换工具重新签发凭据。新的连接仍归因到原 Principal，并可读取原档案、获准历史和 Receipt。Runtime 不验证工具是否为同一个 Agent，也不迁移私人记忆。未决写入的原 operation_id 和完整 envelope 必须随本地接续资料保留。

本地 `agent_world.connect` 助手与 Frontier 网页保存同一种独立 OpenCode JSON：顶层为 `$schema` 和 `mcp`，`mcp.agent_world` 的 remote 包含 `type`、`url`、`oauth: false` 和 Bearer `headers`。配置不另加 `enabled` 字段；已有 Codex 启动器也读取这份文件。正常同钥续签仅替换连接凭据，不改变配置形状或用户身份。

### 路径、请求与主要返回

| HTTP | 请求 | 主要返回 |
| --- | --- | --- |
| `GET /health` | 无鉴权 | `ok, version, universe, auth_required`。 |
| `GET /v1/whoami` | bearer | 当前 Credential 的归因／scope 元数据，不返回新私钥。 |
| `GET /v1/bootstrap` | 可选 `include_catalog=true` | `protocol_version, universe, role_id, role_profile, world, world_entry_state, world_guide, latest_event_seq, snapshot_cursor`。不是完整历史，也不自动启动 Agent。 |
| `GET /v1/describe` | 无额外参数 | `{world: {id, name, version, state_version, api_version}, entry_instructions, discovery}`。 |
| `GET /v1/discover` | `prefix=""`, `after=""`, `limit=50`, `include_schemas=false` | `{functions, has_more, next_cursor}`。schema 需要 `include_schemas=true`；limit 1..200，下一页把 next_cursor 原样作为 after。 |
| `GET /v1/functions` | 无额外参数 | `{universe, functions}`，完整可见目录。 |
| `POST /v1/functions/{function_id}/invoke` | 下述 envelope | Query / Command 结果；领域数据位于 `result`。 |
| `GET /v1/receipts/{operation_id}` | 无额外参数 | 该主体的原 Command Receipt；未找到为 ReceiptNotFound。 |
| `GET /v1/changes` | `after=0`, `limit=50` | `{events, next_cursor, has_more, ...}`；定向通知不等于所有世界变化。 |
| `GET /v1/changes/wait` | `after=0`, `limit=50`, `timeout=30` | 同一通知页形状；timeout 最大 30 秒，超时不产生接受／拒绝。 |
| `GET /api/me/credentials` | 当前 control bearer | `{universe, role_id, current_token_id, credentials}`，只列自己的本世界凭据元数据。 |
| `DELETE /api/me/credentials/{token_id}` | 当前 control bearer，无 body | `{token_id, revoked:true}`，撤销自己的另一枚凭据。 |
| `POST /api/me/revoke-other-credentials` | 当前 control bearer，`{}` | `{revoked_count, kept_token_id, expires_at}`，撤销自己的其他有效凭据。 |

发现项使用 `function_id`，包含 `version`、`description`、`access`，加载 schema 时还含 `input_schema`、可选 `output_schema` 等。目录可见不代表获准执行；已知动作也必须通过授权。描述与 schema 来自世界作者，不能作为其他人的授权。

observe 可以读取该 Principal 获准内容，包括 bootstrap、Query 和 Receipt；不能提交 Command、重放写操作、改变档案或管理凭据。组合入口的 control bootstrap 会记录入场 presence，控制活动也可能按现有合同失效／回收；需要只读连接时在 challenge 选择 observe。

凭据列表的每项包含 `token_id, universe, role_id, access_mode, created_at, expires_at, revoked_at, is_current, status`；status 为 `active / expired / revoked`。不返回 bearer 或 token hash。单撤销在同一事务内重新检查调用凭据和目标 scope：重复撤同一枚成功，当前凭据为 422，未知／他主体／他世界的 ID 均为 404。同一 Principal 的所有 control 凭据有相同管理权限，包括外部宿主的 control；当前没有“仅人类 owner 可管理”的额外权限。凭据管理不能凭 bearer 签发 replacement，持钥者仍可重新进入。上述管理路径属于组合 HTTP 产品入口，MCP 没有对应管理工具。

Query：`{"arguments":{...}}`。Command：`{"operation_id":"stable-id","arguments":{...}}`。二者均使用上述 POST invoke 路径，实际 read/write 由动作声明决定。可选顶层 `expected_version` 核对 **函数合同版本**，它不同于领域参数内的状态 CAS 版本。operation_id 1..128 字符，不能嵌进 arguments；同一意图重试保留同一 ID 与完整 envelope。

Query envelope 为 `ok, read_only, function_id, function_version, snapshot_seq, result`。`snapshot_seq` 是本世界同一读取事务的当前修订水位，取保留的 `state_changes.seq` 与历史清理下界的最大值，不是函数版本、逻辑 tick、对象版本、通知序号或 Receipt 的 `commit_seq`；这些序号不能相互比较。一个只读结果不产生新的水位。读取期间的后续提交不拼入旧结果。

当前世界 Command 的新 Receipt 含 `ok`、`commit_seq`、`function_id`、`function_version`、`operation_id`、`actor_role_id`、`credential_id`、`result`、`event_seqs`，按效果可含 `stream_event_ids`；重放返回 `replayed=true`。actor_role_id 是原提交 Principal，credential_id 是原提交 bearer 的非秘密 token_id；可信进程内／timer 提交没有 bearer，credential_id 为 null。同主体换凭据查询或重放保留原来源，不归因给恢复连接。旧 Receipt 缺少这些字段时原样保留，不能推断或补填原来源。世界 scope 仍由 endpoint 绑定，不能假定 Receipt 含 role_id/universe。

Credential 来源不是独立 AgentIdentity，也不证明哪个外部程序实际持有它。共享 bearer 不能区分宿主；事件、公开帖作者和 journal 仍按 Principal 归因，不自动获得事件级宿主标识。同主体多个宿主共享 Operation ID 命名空间，应为独立意图选择不同 ID；同 ID／不同意图明确冲突。Receipt 证明一次提交，不证明更大事项完成。Query 不形成持久 Command Receipt。

遇到写请求超时／取消／断连，先把原 envelope 保留为未知结果，再查原 Receipt。ReceiptNotFound 只表示查询当时没有已提交回执，不能证明原请求永不提交；需要重试时使用原 ID／原 envelope，不换 ID。新进程应从持久保存的未知操作、回执和获准世界状态接续，不能只靠上一模型对话记忆。

非 2xx 的 Runtime 错误对象保留 `error`、`message`、`retryable`，可能附 `recovery`、`retry_after_seconds`、`details`。例如 401 需重新鉴权，403 为权限拒绝，404 可为 ResourceNotFound 或 ReceiptNotFound，409 可为状态／操作冲突。按 error/recovery 区分，不仅按状态码判断；retryable 也不授权换 ID 重做未知写入。HTTP Retry-After 与结构化等待提示一致。

## Shared Grove 可替换前端契约 v1

world38 提供三个只读函数，沿用上述 Query 通道与成员授权。它们给地图、物件详情、动作表单和蓝图浏览器提供正式 DTO；服务器输出 schema 随函数目录发现并在返回前校验。`map.viewport / grove.inspect / grove.definition_read / grove.fabrication_options` 继续作为既有 Agent 的事实／创作读表面，网页不从这些原始领域观察解析 `state.program`、组件或能力名。不同 renderer 可以任意选择布局、字体、相机和动画，不必认识规则 AST、存储键或机械／合同机制的名称。

| 函数（version=1） | 输入 | 正式结果 |
| --- | --- | --- |
| `grove.client_viewport` | 现有视野参数：可选 `cx, cy, radius, inventory_after, inventory_limit`；以发现的 schema 为准。 | `context, player, chunks, players, entities, inventory, inventory_total, inventory_next, bounds, truncated, clock, changes, opportunities, navigation, coverage, weather, processes, public_content, projection`。|
| `grove.client_inspect` | `entity_id`；可选 `action, candidate_id` 展开一个实际动作／候选。或互斥的 `tile:{x,y}` 读取当前附近格子。 | 物件：`context, player, entity, contents, explanation, projection`；格子：`context, player, tile, entities, actions`。|
| `grove.client_definitions` | `mode:"list"`（默认）：`author, key, limit, after, installable_only`；`mode:"detail"`：`definition_ref`；`mode:"fabrication"`：`definition_ref, type_id`。 | 三个互斥、完整声明的 mode schema：分页蓝图目录；固定版本的显示说明／类型选择；实际来源、输入 schema 和成型调用意图。|

每个结果的 `context` 必含 `projection_version:1, world_instance, world_id:"shared-grove", world_version, viewer_role_id, snapshot_seq, observation_clock, watermark_source:"query_envelope.snapshot_seq"`。浏览器核对调用函数及版本、endpoint 世界、当前身份和 envelope／context 相同水位，再接受结果。只在同一世界、同一主体、同一世界代码版本内按 `snapshot_seq` 比较响应；较旧响应不能覆盖新读取。不同选择目标还应有客户端请求代次，避免同水位的旧详情覆盖新的选择。身份改变或撤权后取消旧读取、清掉受保护缓存；未来操作不能沿旧身份发送。未知 DTO 版本或世界版本变化停止新写入，重新发现／连接；原未决 Operation 仍沿原 Receipt 恢复，不能因 UI 升级丢掉。

`snapshot_seq` 来自同一读取事务的当前修订水位，取仍保留的历史与两类历史清理下界的最大值；合法清理历史不会让水位倒退成零。逻辑 tick 不能替代该水位，因为同一 tick 内也可能有多个玩家提交。

`clock` 与 `context.observation_clock` 使用同一个有限时钟投影：必含 `epoch, sequence, last_tick_at`，可含 `completed_sequence, frame_id, frame_status`；不透传保存时钟里的调度器扩展元数据（例如旧世界的 `rules_version`）。世界代码版本由 `context.world_version` 表达，时钟字段不能代替接口版本或读取水位。

`EntityDTO` 是有界的授权结果，不是保存实体的镜像：

| 字段 | 含义与消费约束 |
| --- | --- |
| `id, name, version, interaction_revision, x, y, location, custody` | 稳定身份、已读位置及归属；名称不是身份。对象整版本和交互修订保留各自原动作语义，不把当前水位填进这些字段。|
| `appearance` | 已解析的 `size, shape, color, glyph, pose, container_open, overlays`；可选 `badge, binding_status, motion`。badge 的 `known:false,value:null` 是未知，不是 0／false；burning、water、broken 等是服务器事实，作者显示绑定不能授予物理能力。motion 只复播已保存观察，不能在本地积分出新权威角度。|
| `summary_facts, fact_sections` | 有标签的树状 `FactDTO`：`id, label, availability, type, temporal_scope, source`，叶子 `value`／可选 `display_value, unit`，组 `rows`。type 为 group／boolean／number／text，时间范围为 current／historical／published。历史保存值、作者声明与当前事实分别显示；无权、隐藏和缺失读为 unknown，不能用旧缓存补值。|
| `materials, relations` | 已获准的真实材料身份、数量、槽位标签及关系索引。关系索引与可见子物件不授予其完整状态或操作资格，按 `read_request` 分别读取。|
| `navigation, read_request, definition_request` | 服务器已判断的阻挡与正式后续读取，前端无需根据容器、配方或能力名猜测。蓝图引用固定不可变版本；署名不是产权。|
| `actions` | 通用 ActionDTO；地图省略详细动作，详情可能返回只读 outline，只有显式展开的描述能构造参数。|

地图 `navigation.cells` 给出 `x,y,passable`，值 null 或缺失均为未知，`planning_only:true` 表明这是路径规划观察。每步 `grove.move` 仍在服务器重验真实格子，路径规划不预留路线。`coverage` 与 `weather` 是已解析的有界现场显示；`processes` 只含允许显示的身份、标签、状态和参与引用。`projection.truncated / omitted_sections`、背包分页和公开锚点省略明确说明读取范围，省略不表示不存在。DTO 超预算先省去重复锚点详情，再缩小地图实体范围并使规划通行未知；单对象的事实仍不能完整呈现时明确拒绝，不伪造完整返回。

`ActionDTO` 的 `function_id`／`action` 是不透明调用键，客户端依据 `label, description, available, reason` 显示选择，不根据键名补规则。展开项含 `fixed_arguments, input_schema, controls, preconditions, confirmation_keys, read_request`。`controls` 是有限控件描述：

| 控件字段 | 规则 |
| --- | --- |
| `id, path, label, kind, required, schema, default?` | path 是提交参数中的明确路径；kind 为 choice／multi_choice／integer／number／boolean／string／array。客户端只校验该控件 schema，不能把未识别结构当自由输入。|
| `choices:[{value,label,arguments_patch,when?,maximum?,preview?}]` | 选中的实际值及其服务器提供的版本／组合指纹补丁；when 的有限依赖按指定控件值筛选，空候选集合仍是空选择器，不能允许猜 ID。preview 为带标签的只读事实，不能证明已经执行。|
| `maximum_from_control_id?` | 指定另一个选择控件的 `maximum` 绑定本控件上限；未声明时不得根据 `lot_id`／`amount` 名称猜数量关联。|
| `discovery_request?` | 正式分页目录请求；本页没有候选不证明全目录没有，沿相同过滤条件继续游标。|

展开过的表单保留实际选择、服务器固定参数及选中候选的原版本／指纹／条款。定时刷新不悄悄替换它们。提交前可以重新读取并核对当前资格；`confirmation_keys` 明确哪些参数变化需要重新确认，客户端按这些键核对固定参数与候选补丁，不猜动作名。已有交互修订的固定意图只核对交互修订，自动派生输出的整版本增长不会阻断；明确数量／槽位的倒水由原服务器 handler 重验当前来源和容量，也不因自动版本增长要求换意图。需确认的护栏变化时先显示差异并要求明确重新确认输入，不能把“刷新”解释成用户同意新的来源、目标、数量、槽位或条款。提交保留原护栏，本人提交版本按当前本人读取更新，不悄悄替换候选。`expanded:false` outline 没有参数／候选权威，先执行它的 `read_request`。任何 `available:true` 都只证明本次读取资格，最终 Command 仍重验当前世界。

需要进一步缩小动作观察时，`candidate_requests:[{id,label,read_request}]` 提供实际候选的正式窄读入口，不能据其 ID 构造缺失护栏。展开后的 read_request 保留选定 candidate_id，提交前仍读取相同候选，其他候选和动作继续明确标记未展开。

蓝图目录的每项有 `definition_ref, author, author_name, key, name, version, hash, published_tick, type_ids, read_request`；`author_name` 是当前公共名字，不能替代作者 ID。list 保留 `has_more, next_cursor, filters, order, complete, last_page, scope` 及明确存储统计范围。`installable_only` 仅过滤所选分页，`filter_scope:"selected_page_only"` 时空页仍须继续游标。detail 返回 `definition_ref, name, author, author_name, version, types, installable, fact_sections, projection`；每个类型有 `id,label,can_fabricate,creation_request`，前端直接使用这些服务器选择。声明说明按深度 6／每组 40 项展开，省略明示；它是 published 内容，不能被解释成实例已经运行。

fabrication 返回固定 `definition_ref,type_id`、真实配方 `cost_u,materials`、当前本人 `player`、可用与不可用来源及有界获取来源。`inputs:{path:["sources"],schema,choices:{sources}}` 精确定义可选择的真实 entity／lot，`intent:{function_id,fixed_arguments,input_schema,selection_path,guard_keys}` 定义调用；原 `action` 含可用性、理由及同一选择菜单。按 canonical material_id 合并数量，同一 ID 的别名行不能重复累加；只提交所选真实来源版本、lot_id 和 quantity_u，不自动换料。附近获取来源只表示可发现，须正式 inspect 后采取原动作并重新读成型菜单，不是本人现货或预留量。定义发表、安装资格、成型资格和实例存在分别成立。

一次普通物件选择的调用链为：

```json
{"arguments":{"entity_id":"the-actual-object"}}
```

将该 Query 发送给 `grove.client_inspect`，选取其已展开 `entity.actions`，复制 `fixed_arguments`，按 `controls.path` 加入选值及该选值的 `arguments_patch`，再用原动作 `function_id` 和新意图的稳定 `operation_id` 提交 Command。服务器负责授权、参数、当前条件与原子后果。客户端只接受 Receipt 证明提交，并随后读 DTO 显示当前后果；写返回中的旧原始 viewport 不是 v1 DTO。响应未知保留完整原 envelope，查询原 Receipt；403／404／规则冲突只拒绝相应读取或动作，不冒充凭据失效，也不证明原未知操作没发生。错误 helper 保留 canonical `retryable, retry_after_seconds, details`，等待提示不能变成自动换 ID 重做。

Agent 面板的历史执行 readback 保留原证据语义。网页当前行动目标只有身份索引和 `client_read_request`，点击后取得当前 DTO；当前详情不能覆盖或伪装成原行动的历史读回。

## MCP 客户端

连接 exchange 返回的 MCP URL（组合入口通常 `/mcp`），使用相同 bearer。通过 MCP SDK 建立 session／initialize／list_tools；调用 `world.bootstrap`、`world.describe`、`world.discover` 的参数直接为对象。世界动态函数使用与 HTTP invoke 相同的 arguments / operation_id envelope。HTTP 与 MCP 可在同一主体下交替查询回执或重放原操作，不自行把新 session 当新身份。

MCP 错误通过 `isError=true` 与 structuredContent 返回，保留同一结构化错误；不能只依据工具调用完成来宣称动作成功。具体世界的公开／私有内容、回应、确认与完成仍按该世界规则处理。
