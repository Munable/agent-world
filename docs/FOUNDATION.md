# Runtime / SDK 合同

文档复核：2026-09-26，源码 `3fbb6378`，Runtime 0.14.0 / SDK API 1。文档日期不是 API 版本。本整理分支在上述文档基线上修复 managed-state 校验，包版本暂不变；具体提交与验证见[本次审计](REPOSITORY_AUDIT_2026-09-26.md)。产品目标见[产品目标](../PRODUCT_POSITIONING.md)，不可破坏的系统性质见[系统不变量](INVARIANTS.md)，概念边界见[领域模型](DOMAIN_MODEL.md)。本文件属于 L4，只描述 Runtime/SDK 行为合同及已知偏差，不宣称生产能力齐备。

## 结构化调用与责任

HTTP / MCP 使用同一函数注册表和执行路径。外部调用指定函数及符合其 schema 的 JSON 对象参数，身份由鉴权提供。服务器执行授权和规则代码，不解释消息文本来推断确认、同意或裁决；任意 JSON 字段也不能绕过函数合同。第三方裁决尚待设计，见 [OPEN_DESIGN](OPEN_DESIGN.md)。

世界包提供领域规则、数据 schema、授权策略和恢复视图。Runtime 不托管 Agent；模型推理、私人计划和记忆在外部。世界函数、权限钩子、初始化和迁移都是同步的受信任 Python 回调，不得在事务中等待模型、网络或用户输入。

## 最小世界包

```python
from agent_world import WorldDefinition, FunctionSpec, StateRule, FunctionOutcome

EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}

def advance(ctx, arguments):
    record = ctx.get_state_record("world", "count", 0)
    value = record["value"] + 1
    ctx.set_state("world", "count", value, expected_version=record["version"])
    return FunctionOutcome({"value": value})

def snapshot(ctx):
    return {"count": ctx.get_state("world", "count", 0)}

WORLD = WorldDefinition(
    world_id="counter-world", display_name="Counter World",
    functions=(FunctionSpec("counter.advance", advance, EMPTY),),
    state_rules=(StateRule("world", "count", {"type": "integer"}),),
    bootstrap=snapshot,
)
```

WorldDefinition 由部署配置加载；universe 选择隔离的持久实例，不是规则包本身。模块加载不能由普通 Agent 工具参数动态安装代码。当前命令行形态见 [IMPLEMENTATION](IMPLEMENTATION.md)。

## 函数与状态

`FunctionSpec` 声明 name、version、对象 input_schema、可选 output_schema、read/write、handler，以及可选 `authorize(ctx, arguments)` / `visible_to(ctx)`。授权必须精确返回 `True`。隐藏函数不等于拒绝调用；执行权限须由 authorize 检查。

`FunctionContext` 的可移植接口按 universe 限定：状态的 get/get_record/set/delete/list，公开角色档案 `get_role`、实例内 `get_activity`，`now`、actor/operation 身份及 `random_int`。持久时间与共享流接口见各自合同。

`set_state` / `delete_state` 支持 `expected_version`。删除保留墓碑版本，避免旧版本操作因重建对象而误入。`list_state` 有界分页，不保证跨请求的冻结快照。`StateRule` 按 scope/key 的字面前缀匹配，所有匹配规则都适用；`strict_state=True` 拒绝未声明的 SDK 写入。

`state_authorizer` 控制 SDK 读写；需要读取权限依据时，可在该回调内调用 `authorization_state(scope,key)`。它只允许在授权回调执行期间使用，普通函数直接调用被拒绝。它不内置 Party、Group 或成员资格语义。

**本整理分支的修复：** managed WorldDefinition 的普通操作和 timer 在提交前重校验状态变化，恢复 legacy `ctx.conn` 写入的 schema、版本及 state_authorizer 检查。SDK 已经获准的写入不在事后重复授权；raw 写入的授权回调支持 `authorization_state`，且按只读方式执行。初始化／迁移按最终存活状态验证目标 schema，允许合法的中间数据形状。修复前复现、回归范围与未覆盖项见[本次审计](REPOSITORY_AUDIT_2026-09-26.md)。

`ctx.conn` 仍是受信任、不可移植的兼容入口，不是任意 SQL 与 SDK 全面等价的承诺，也不是对外 SQL 接口或恶意 Python 插件沙箱。特权管理连接、直接文件操作及未声明 WorldDefinition 的旧注册路径不属于此 managed-write 校验范围。

## 事务、重试与业务完成

读函数不需要 operation_id，不创建回执或事件，不使用写活动 claim，并在物理只读连接中运行。写入必须通过写函数；输入、输出、事件或 SDK 校验失败回滚事务。回调提供的连接禁止事务控制、ATTACH、PRAGMA 及凭据表写入。读结果包含 ok、function_id、function_version、result 和 read_only=true，不创建写回执。

写操作的 operation_id 在 actor + universe 内唯一。同函数、参数及活动目标重试返回已提交回执，改变意图则冲突；同一活动的 claim epoch 变化不改变原操作身份。成功提交的随机取值进入回执，重放不重掷。

`world.get_receipt` / `GET /v1/receipts/{operation_id}` 可在原函数移除后恢复原结果。响应丢失或请求取消不证明回滚；回执暂不存在也不证明一个在途请求以后不能提交。只重试原 operation_id，不用新 ID 处理未知结果。

operation_id 解决传输重试，不替代业务对象唯一性。不同 ID 的请求是否重复领取、重复确认，由世界函数按业务对象和当前状态检查。事务回执只描述该次提交，不自动等于跨多次调用的事项已经结束；后续进展从获准状态／事件读取，不篡改旧回执。

保证仅覆盖本地 SQLite 提交及保留的回执。网络、付款、文件等外部副作用不在该事务保证中，不得直接混入世界回调；需要独立的交付合同。

## 身份：目标与当前实现

产品已确定用户持有跨世界通用的身份令牌，对应统一底层身份与档案。当前代码的令牌验证仍绑定 role + universe；跨独立部署验证未完成，不能通过简单移除 universe 检查宣称实现了目标。

角色档案保存稳定参与身份与显示资料，不包含各世界的钱、等级、关系和物品；修改显示资料不应隐式改变参与身份。调用身份必须来自鉴权结果，客户端不能靠业务参数冒充另一角色。当前字段、token 格式与存储方式见 [IMPLEMENTATION](IMPLEMENTATION.md)。

Join Ticket 是当前短期配置／交换机制。一次 ticket 不能产生多个相互独立的调用身份；已经撤销、轮换或过期的凭据不能被 ticket 复活，撤销 ticket 也不自动撤销此前已发凭据。角色状态、token revoke/rotate 是分别执行的管理动作。当前时限、连接包和操作员入口形态见 [IMPLEMENTATION](IMPLEMENTATION.md)；这些都不是跨世界统一验证方案。

`control` 凭据可执行获准写入；`observe` 只能读取角色获准信息，不能写、重放写操作或修改活动。轮换保持访问模式。公开观察是独立的显式访问模式，不借用他人角色凭据。当前管理原型与轮换恢复缺口见 [IMPLEMENTATION](IMPLEMENTATION.md)。

鉴权在事务内复核，不能忽略等写锁期间的失效。MCP 会话绑定初始化凭据，轮换后需新会话；身份来自当前消息的 HTTP request，而不是继承的会话上下文。撤销不能收回已发送的信息。

## 接入、活动与恢复

`world.bootstrap` 返回有界世界视图及同事务取得的 snapshot_cursor，并指向 `world.describe` 的作者入口说明。`world.discover` 分页发现函数，按需取 schema；不要求每次 bootstrap 重复完整目录。

`world.get_changes` / `world.wait_changes` 读取按角色投递的有界事件页；wait 最长 30 秒、可取消，跨进程依靠持久数据检查。它不启动远端 Agent。过期游标明确报错；当前事实由 bootstrap / view 恢复，已清理的历史不能重建。具体交互接续责任见 [AGENT_INTERACTION](AGENT_INTERACTION.md)。

Activity 只用于声明需要排他控制的过程。`world.claim_activity` / `world.renew_claim` / `world.finish_activity` 提供有限租约和递增 epoch，拒绝过期控制者；不是全局角色 busy，也不是共享资源锁。一般发消息或读取不需要强制创建活动、领租约或等待另一方。

## 版本与升级

api_version 是 SDK API；world version 是规则／策略合同；state_version 是持久数据 schema；function version 是函数描述合同；view version 是投影合同。不要以文档修改代替代码版本变更。

规则／策略改变须提升 world version，描述改变须提升 function version，read/write 模式变化须换函数 ID。需要数据迁移时提升 state_version 并提供所有中间 `migrations={target_version: callback}`。

初始化只执行一次。迁移、现存状态验证、注册表及 manifest 原子提交，失败保留旧版本；缺失步骤、降级、同版本 manifest 漂移被拒绝。旧进程拒绝执行新版世界规则，但保留回执仍能取回。真实库升级前做可恢复备份。旧数据只能作为明确 baseline，不能捏造此前历史。

## 可选能力与部署

[世界数据](WORLD_DATA.md)、[共享流](OBSERVATION_STREAMS.md)、[定时事项](DURABLE_TIME.md)、[数据保留](RETENTION.md) 是 L4 中按需声明的能力，不强制每个世界使用全部功能。[交互与表现](PRESENTATION.md) 是横向表达约束，不是业务能力层。语义正确不以有地图、动画或完整游戏为前提。

传输适配器必须复用同一 Runtime 行为合同，部署不得通过关闭 Host/Origin 等边界保护来规避配置问题。当前组合入口、wheel 与兼容入口见 [IMPLEMENTATION](IMPLEMENTATION.md)。

世界规则代码当前处于受信任执行边界，不提供恶意插件隔离；具体存储和进程模型见 [IMPLEMENTATION](IMPLEMENTATION.md)，架构信任边界见 [ARCHITECTURE](ARCHITECTURE.md)。其他实现差距、设计工作及待验证假设集中在 [OPEN_DESIGN](OPEN_DESIGN.md)，不要把它们全部变成每个世界的先决条件。
