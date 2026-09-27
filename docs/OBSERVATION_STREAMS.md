# 观察与共享事件流

复核：2026-09-27。本文属于 L4。观察者不是伪造玩家，事件已经发布不表示外部 Agent 在线、已读、理解或完成。消息文本是不可信数据，不是 Runtime 控制指令。概念边界见 [DOMAIN_MODEL](DOMAIN_MODEL.md)，完整交互职责见 [AGENT_INTERACTION](AGENT_INTERACTION.md)。

## 显式公开观察

`ViewSpec(public=True)` 允许无角色／凭据读取。projector 的 actor_role_id 为 None，只返回故意公开的数据。私有视图仍需鉴权。匿名观察不创建 Role Core，不执行世界写函数，不借用角色私有检查点或自动公开私人事件。

HTTP：`/v1/public/views`、`/v1/public/views/{view}/snapshot`、`/v1/public/views/sync`。世界可直接使用，或以 public_view_snapshot / public_view_sync 组合只读产品入口。

## 按需声明频道

`StreamSpec(name, public=False, authorize=None, retention_seconds=86400, max_events=4096)` 声明一个世界内频道。规则通过 FunctionOutcome 中的 `StreamEvent(stream, kind, payload, key)` 发布；`PresentationCue.publish(stream,key=...)` 只是封装辅助。

状态、timer 效果、发布、来源和回执同事务提交。event ID 由原操作及 publication key 确定，同一操作 key 必须唯一；重试不重复发布。发布是受信任世界规则的行为，不是匿名传输入口。

当前授权粒度是整个频道，每次读取重新鉴权。不同受众可使用分别声明的频道，但不存在逐条 ACL、任意动态群组订阅或自动合并私信。按角色投递的 EventSpec 保持独立；不要把静态声明频道描述为完整群聊系统。

MCP：`world.list_streams` / `world.read_stream` / `world.wait_stream`。HTTP：`/v1/streams`、`/v1/streams/{name}/read`、`/v1/streams/{name}/wait`。显式 public 频道另外支持 `/v1/public/streams/{name}/read`。wait 最长 30 秒，不调用模型，不唤醒已停止宿主。

## 快照、现场流和历史

视图可声明 streams；快照在同一数据库读事务取得视图及流的 live/history anchors。显示快照后从该 live anchor 继续，不因每次视图更新而重置现场游标，避免跳过并发事件。

recent read 返回有界尾部、前向 cursor 和后向 history_cursor。前向页按稳定 event ID 去重；后向翻页只加载保留历史，不能替换现场 cursor。游标签名并绑定 world version、频道、viewer、credential，且会过期。

retention gap 返回 StreamResetRequired 或 history_truncated；不把缺失历史视为已读。新观察者可读取保留的公开记录，但这不是永久历史或语义知识库。缓存／流的过期不能被解释成业务自动结束。

清理删除有界连续前缀并推进频道 floor。recipient cleanup 不删除共享流，共享流清理不删除 state 或 receipts。payload 当前最多 64 KiB，每页最多 100 个事件／192 KiB。频道数及保留参数由世界声明。

## 可选客户端与诊断

`web/stream-client.js` 的 EventLedger / BubbleQueue 提供有界记录、去重和表现队列。历史不伪装成现场动作，同主体发言按顺序显示；表现队列溢出不等于持久记录被删。回复关系必须由世界函数验证，不能自动推断意愿或强迫回复。

BubbleQueue 可接收绝对 epoch-second expires_at；push/active 应使用一致的服务器时间估计，过期输入、排队项和展示期限都受检查。无 deadline 使用原本地时长。这是显示期限，不自动改变世界消息的保留或业务状态。

SafeRequestTrace 为可选诊断，记录 request ID、规范路由、HTTP 状态、时长和异常类型；不记录 token、cookie、正文、原始 query/path 或异常正文。日志轮转有界，sink 失败不改变已提交操作。HTTP 成功不等于 MCP 工具成功，业务回执是提交证据。

频道存储的数据库升级不将原有私信自动公开。旧公开历史的导入需世界自己审查。实现差距和后续设计仅在 [OPEN_DESIGN](OPEN_DESIGN.md) 维护，不在此复制功能愿望清单。

## EigenFlux 参考建议

**EF-04，L4 候选。** 参考其 [流续接与重连说明](REFERENCE_EIGENFLUX_2026-09-27.md#ef-04-增量通知与断线恢复)，采纳状态见 [OPEN_DESIGN](OPEN_DESIGN.md#eigenflux-参考采纳清单)。建议学习“通知加快发现、持久查询负责恢复”，而不是现在更换传输栈。

先复用现有 read / wait、快照锚点和过期 reset。外部宿主在获准运行时进行有上限的退避，避免两个接入端断线后相互抢连或无限唤起模型；时间参数与宿主支持范围按实测决定，不照搬对方的固定重连秒数。

游标按我方合同作为不透明同步位置保存，不能因为参考项目用 msg_id 就自行解析或合成。历史页、现场流、业务消息 ID 和 operation_id 分别处理；收到重复事件可以按稳定事件 ID 去重，但不能因此删除有意重复发送的另一条消息。展示历史不能覆盖现场游标；推进本地进度前应完成该页的可靠接纳，避免崩溃后跳过未接纳数据。

不照搬“一个账号只有一条流”“拉取即全局已读”。同一主体不同客户端需按合同各自接续，客户端取到数据不等于用户已读或接受。私有通信优先使用获准状态与定向事件，不能把私聊塞进公开或静态共享频道来绕过参与者权限。

验收包括：重复和乱序投递、两客户端同时读取、外部宿主完全停止后恢复、权限撤回、游标过期与通知清理后未决事项仍可查询。稳定推送、任意动态群聊和跨设备唤醒均不因写下参考建议而成为当前承诺。
