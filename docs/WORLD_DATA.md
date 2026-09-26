# 世界数据、授权视图与缓存

复核：2026-09-26，源码 `3fbb6378`。这里只定义数据与观察合同；调用见 [Foundation](FOUNDATION.md)，交互接续见 [AGENT_INTERACTION](AGENT_INTERACTION.md)。下面的容量是当前实现参数，不是产品哲学。

## 数据归属

| 数据 | 保存位置与用途 |
| --- | --- |
| 统一底层身份档案、各世界当前状态 | 服务端的身份／世界事实；统一跨部署验证尚未实现，不能假设独立数据库自动共享身份。 |
| 已提交操作回执、必要的未决事项 | 服务端用于去重、确认结果与接续；不能只保存在客户端缓存或短期通知中。 |
| 已提交消息与证据 | 按世界声明的权限和保留合同存储；文本仅为数据，存储不代表验证其语义或真实性。 |
| 状态历史、通知与表现事件 | 按用途独立保留，不是全部永久保存，也不互相替代。 |
| 用户凭据、私人推理、未提交计划和模型上下文 | 用户／外部 Agent 宿主持有；Runtime 使用必要验证信息，不依赖私人上下文恢复世界。 |
| 视图缓存、检查点、客户端副本 | 可重建的派生数据，不是唯一事实来源。 |

被明确提交、经规则接受的公共计划或第三方意见可以成为世界数据；这不要求上传 Agent 私人记忆。服务端权威状态、某主体获准观察的内容、Agent 自己的认识是不同层次。

## 当前记录结构

`world_state` 保存当前值与删除墓碑版本。`state_changes` / `world_commits` 保存可保留的前后状态与提交来源。`events` 是单独保留的接收者通知，不包含每一次状态变化；共享频道另见 [OBSERVATION_STREAMS](OBSERVATION_STREAMS.md)。

SQLite triggers 记录状态写入，包括没有通知的变化。失败事务不留下本次状态、历史或回执；重放返回原提交引用。直接特权 SQL 可产生历史，但不捏造 actor/commit 来源。禁止原地改状态键身份，需 delete/create。跨 universe 写入受提交检查；本整理分支的 managed raw-write 重校验及其边界见 Foundation，不能推广为任意特权 SQL 的保证。

原始历史是服务端管理数据，没有向 Agent 开放的全库历史 HTTP/MCP。`read_state_history` / `prune_state_history` 仅为受信任操作。一次提交最多 512 条状态变化、2 MiB 前后内容总量；单次历史页也有大小限制。历史仅覆盖 world_state，不自动覆盖全部身份、活动或外部文件。

通知清理不删当前状态，历史清理不替代备份。需要跨清理周期接续的事项必须在当前状态中保留足够信息，或通过明确业务规则终止；不能因视图过期而静默完成或遗忘。数据生命周期未决项集中在 [OPEN_DESIGN](OPEN_DESIGN.md)。

## ViewSpec

`ViewSpec` 声明 name、input_schema、version、同步 projector，以及可选 authorize、visible_to、output_schema 和 renderer hint。每次 snapshot/sync 都在只读保护下重算授权投影。隐藏数据不得先发给客户端再靠 CSS 隐藏；撤销不能抹除已经看到的信息。

投影返回 `entities`、`resources`、`meta` 映射。entity ID 在该视图内稳定，内容是世界定义的 JSON，不强制 HP、坐标、任务、组织或社交关系。renderer 仅是标签，不是可执行代码 URL。

资源引用可包含 uri、media_type、sha256、size、version；仅接受不带凭据的 HTTPS 或 asset:// 引用。Runtime 不抓取资源、不将其转成 HTML，也不因此提供 blob 托管、许可验证或永久资源登记。

当前投影上限为 256 entities、64 resources、96 KiB。更大观察范围通过 selector／分页表达，不要求返回整个世界。ViewSpec 是可选观察接口，不是所有结构化读函数的统一数据模型。

## 快照、增量与检查点

- `GET /v1/views` / `world.list_views` 发现视图，schema 按需获取。
- `POST /v1/views/{view}/snapshot` / `world.view_snapshot` 使用 selector 获取快照。
- `POST /v1/views/sync` / `world.view_sync` 使用检查点返回 upserts/removals 和下一检查点。

快照和内部状态历史锚点来自同一读取事务。公开的 view_revision 只散列可见投影，不披露隐藏状态修改次数。sync 重算当前获准投影再比较，不传递原始历史；可见性撤回导致的删除也必须应用。净增量可能跳过中间变化，不等于动作时间线或确定性重执行。

检查点绑定 universe、role、credential、view、selector 和 world/view version；可跨进程重启，但只有五分钟有效期。当前每 viewer 每 universe 最多 16 个，匿名公开 viewer 256 个，总计 1024 个。这些是可驱逐缓存。

过期、驱逐或绑定不符返回 ViewResetRequired，需要重新取快照。401/403 要清理受保护视图。客户端仅在 base_cursor 匹配时应用增量，丢弃乱序响应；不能跨身份／视图复用。

## 未变化检查点与客户端

非 timeline 视图每次仍重算投影和复核授权；若可见内容不变且原检查点距过期超过 30 秒，可复用同一 cursor，避免写入新缓存。observed_at 与流锚点重新读取，原有效期不因复用延长。内部历史锚点仍表示原基线。timeline 视图保留自己的锚点推进路径。

cursor 相等不表示请求没执行；cursor 不同也不证明世界事实有变化。按 base_cursor 应用响应，事件进度使用独立的流 cursor。

`agent_world/web/world-client.js` 提供 WorldClient / applyViewUpdate，凭据由调用方管理，拒绝乱序响应，不执行返回文本或下载资源代码。未知写结果通过回执恢复。缓存可重建不等于清理后能重建全部历史；每个世界的恢复视图仍需验收。
