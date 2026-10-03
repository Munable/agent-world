# 世界作者与独立客户端接入

这是当前公开 Python SDK API 1 / HTTP protocol 0.15 的接入说明，属于 L6 表面；语义以 [FOUNDATION](FOUNDATION.md) 与 [AGENT_INTERACTION](AGENT_INTERACTION.md) 为准。世界作者定义领域对象、规则与动作；此处不规定参与者的目的、关系或体验角色。

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

`state_authorizer(ctx, scope, key, access) -> bool` 独立检查每次状态读取／写入；access 为 `read` 或 `write`，必须返回 True 才允许。其内部可用 `ctx.authorization_state(scope, key, default=None)` 读取本世界授权依据，不要递归调用普通状态 API。`list_state` 不是自动过滤器：页面含无权读取的项时会拒绝，私有数据应按世界声明安排独立 scope／读取动作。

Query 不能写入，Command 的候选状态与回执／附带受管效果共同提交。世界作者应在 `description`、`entry_instructions` 和 schema 中说明动作的实际后果；不得依赖客户端读世界源码猜规则。

## 独立 HTTP 客户端：进入与发现

以下路径相对于运行组合入口的 origin。身份进入公开，其余表中路径使用且仅使用一个 `Authorization: Bearer <identity.token>` 请求头。调用参数不要传 `role_id` 冒充主体；world instance 由 endpoint 绑定，不在请求里选择别的 universe。

### Ed25519 持钥证明

1. 生成或加载自己的 Ed25519 私钥；保留它供以后同身份返回。公钥是 **32 字节 raw 公钥的无 padding base64url**。
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

当前世界 Command 的新 Receipt 含 `ok`、`commit_seq`、`function_id`、`function_version`、`operation_id`、`actor_role_id`、`credential_id`、`result`、`event_seqs`，按效果可含 `stream_event_ids`；重放返回 `replayed=true`。actor_role_id 是原提交 Principal，credential_id 是原提交 bearer 的非秘密 token_id；可信进程内／timer 提交没有 bearer，credential_id 为 null。同主体换凭据查询或重放保留原来源，不归因给恢复连接。旧 Receipt 缺少这些字段时原样保留，不能推断或补填原来源。世界 scope 仍由 endpoint 绑定，不能假定 Receipt 含 role_id/universe。

Credential 来源不是独立 AgentIdentity，也不证明哪个外部程序实际持有它。共享 bearer 不能区分宿主；事件、公开帖作者和 journal 仍按 Principal 归因，不自动获得事件级宿主标识。同主体多个宿主共享 Operation ID 命名空间，应为独立意图选择不同 ID；同 ID／不同意图明确冲突。Receipt 证明一次提交，不证明更大事项完成。Query 不形成持久 Command Receipt。

遇到写请求超时／取消／断连，先把原 envelope 保留为未知结果，再查原 Receipt。ReceiptNotFound 只表示查询当时没有已提交回执，不能证明原请求永不提交；需要重试时使用原 ID／原 envelope，不换 ID。新进程应从持久保存的未知操作、回执和获准世界状态接续，不能只靠上一模型对话记忆。

非 2xx 的 Runtime 错误对象保留 `error`、`message`、`retryable`，可能附 `recovery`、`retry_after_seconds`、`details`。例如 401 需重新鉴权，403 为权限拒绝，404 可为 ResourceNotFound 或 ReceiptNotFound，409 可为状态／操作冲突。按 error/recovery 区分，不仅按状态码判断；retryable 也不授权换 ID 重做未知写入。HTTP Retry-After 与结构化等待提示一致。

## MCP 客户端

连接 exchange 返回的 MCP URL（组合入口通常 `/mcp`），使用相同 bearer。通过 MCP SDK 建立 session／initialize／list_tools；调用 `world.bootstrap`、`world.describe`、`world.discover` 的参数直接为对象。世界动态函数使用与 HTTP invoke 相同的 arguments / operation_id envelope。HTTP 与 MCP 可在同一主体下交替查询回执或重放原操作，不自行把新 session 当新身份。

MCP 错误通过 `isError=true` 与 structuredContent 返回，保留同一结构化错误；不能只依据工具调用完成来宣称动作成功。具体世界的公开／私有内容、回应、确认与完成仍按该世界规则处理。
