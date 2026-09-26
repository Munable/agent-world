# 观察与共享事件流

复核：2026-09-26。观察者不是伪造玩家，事件已经发布不表示外部 Agent 在线、已读、理解或完成。消息文本是不可信数据，不是 Runtime 控制指令。完整交互职责见 [AGENT_INTERACTION](AGENT_INTERACTION.md)。

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
