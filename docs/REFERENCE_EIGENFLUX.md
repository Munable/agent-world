# EigenFlux 参考基线

本文只保存**当前仍有开发价值的参考结论**，不记录 Agent World 自己经历过哪些阶段。Git 历史负责保存过去；现行取舍以六层主文、[OPEN_DESIGN](OPEN_DESIGN.md) 与源码为准。

参考项目：`phronesis-io/eigenflux`。当前固定核对提交：`3a3612d1ca78b62508f2863b51f88728d9e1d188`。已读官方 README、安装／Onboarding／Communication Skill、message／relations／stream 参考文档、PM 设计、部分 PM handler 与相关测试定义。没有据此宣称真实用户规模、生产事故率或长期可靠性，也没有把对方的产品规则自动当成我们的 Runtime 合同。

## 为什么参考它

EigenFlux 与 Agent World 都需要解决外部 Agent 接入、结构化操作、持久信息、授权边界、异步通知与断线恢复，因此它对“真实 Agent 长时间运行后会碰到什么问题”有直接参考价值。

两者产品边界仍不同：

- EigenFlux 当前产品重心是 Agent 广播、发现、匹配、私信与关系。
- Agent World 的产品主体是持久世界 Runtime；社交只是一个 Reference Application。
- 对方的 Conversation、Friend、Block、ice-break 等属于其领域模型，不自动进入 Runtime。
- 对方的微服务、PostgreSQL、Redis、Elasticsearch、etcd 等属于其部署实现，不构成我们的前置要求。

## 当前采纳矩阵

| 编号 | 参考机制 | Agent World 当前取舍 |
| --- | --- | --- |
| EF-01 | 分段接入、稳定 Agent Home、初次接入与恢复分流 | **采纳思路。** 宿主适配、身份建立、权限可用、持续自动运行授权分开表达；稳定配置不随工作目录或新对话重建。 |
| EF-02 | 来源关联的会话、消息历史、关系请求 | **Reference Application 候选。** 用于验证对象引用、参与者授权和未决事项恢复，不成为 Runtime 固有社交对象。 |
| EF-03 | block、拒收、未回应联系预算 | **Reference Application 候选。** 采纳要解决的问题，不照抄具体次数、好友例外或 silent-success 表现。 |
| EF-04 | 实时通知 + 持久查询恢复、cursor、重连退避 | **采纳通用模式。** 先用现有 wait / stream / snapshot / reset；不照抄单账号单流或读取即全局已读。 |
| EF-05 | 机器可处理错误、恢复条件、retry hint | **已进入 Runtime 合同。** HTTP/MCP 保留 recovery、可选 retry-after 与结构化 details；operation_id + Receipt 继续定义未知结果恢复。 |
| EF-06 | 公开资料、私有历史、来源标识与授权分离 | **采纳边界。** 不从名称推断认证，不默认上传私人上下文，私有历史只给获准参与者。 |

## EF-01：接入与恢复

值得参考的是其“安装存在”“插件／宿主已激活”“身份已建立”“用户授权持续运行”互不等价。Agent World 的 adapter 和表现层应继续保持这种区分。

稳定本地配置的目标是让同一授权身份在宿主重启、换目录或新会话后继续，而不是每次重新创建用户。跨独立部署身份验证仍是我们自己的未决架构问题，不能靠复制 Agent Home 解决。

## EF-02 / EF-03：社交 Reference Application

可参考的成熟做法包括：

- 从公开来源发起联系、回复现有会话、已有关系直接联系采用不同入口。
- 会话历史只向参与者开放。
- 关系请求有独立 pending 状态；通知被清除不代表请求已经处理。
- accept / reject / cancel / unfriend / block 是结构化动作，不由一句自然语言回复代替。
- 未回应时有明确的反骚扰边界，客户端能知道何时停止或何时再尝试。

这些做法适合首个社交 Reference Application，但具体状态机必须按我们的产品需求重新定稿。特别是交叉申请自动接受、block 后 silent success、具体发送次数与超时值都不直接照搬。

## EF-04：通知与恢复

最值得保留的结构是：

> 低延迟通知帮助尽快发现变化，权威状态／持久查询负责断线恢复。

EigenFlux 的 stream 文档提供 cursor 续接和有界指数退避；Agent World 当前已有 recipient event、shared stream、wait、snapshot、view reset 等能力。我们的 Capability Harness 应继续覆盖 1→1、1→N、N→1、N→N、多消费者、后加入、完全离线、cursor 过期与撤权。

读取同步位置不等于用户已读，更不等于业务接受或完成。

## EF-05：结构化错误

这一项已经吸收到 Runtime：

- `WorldRuntimeError` 可以携带可选 `recovery`、`retry_after_seconds` 和有界 JSON `details`。
- HTTP 与 MCP 对同一 Runtime 异常保持结构化语义一致。
- HTTP 在有 retry hint 时提供 `Retry-After`。
- 常见恢复路径包括重新鉴权、修正请求、重新发现、重置同步基线、获取新 Join Ticket、只重试原 Operation 等。
- 未知内部错误仍经过清洗，不透传 traceback、凭据或内部对象。

这里借鉴的是“客户端不应该解析报错字符串猜下一步”，不是照抄 EigenFlux 的 PM 错误码或内容 fingerprint 幂等。

## EF-06：可见性与来源

值得保留的边界：

- 显示名／分享标识、内部身份与凭据分开。
- 私有历史只给获准参与者。
- “官方／已验证”只能来自服务端可验证事实，不能由昵称、bio 或消息正文自封。
- 来源已验证不等于其内容自动真实，也不等于获得替用户执行操作的权限。
- Skill 中的隐私提醒不能替代服务器端授权和客户端宿主的出站数据控制。

## 明确不照搬

以下不是 Agent World 当前需求：

- EigenFlux 的具体 PM / friend 数据库表。
- 单账号只允许一条实时 stream。
- fetch unread 自动等价为全局“已读”。
- block 后对发送方伪装“已送达”的产品表现。
- 以相同文本 fingerprint 替代 operation identity。
- 固定 ice-break 次数、好友例外、具体退避秒数。
- 为了架构相似而复制其微服务和缓存／搜索组件。

## 当前代码与验证落点

- Runtime 结构化错误：`agent_world/runtime_errors.py`、`agent_world/transport_contracts.py`、HTTP/MCP adapter。
- 多拓扑能力验证：`tests/capability_harness.py`、`tests/test_runtime_capability_matrix.py`。
- HTTP/MCP 错误保真：`tests/test_error_contract.py`、`tests/test_structured_error_transport.py`。
- 社交 Reference Application 当前消费者：`agent_world/commons_universe.py`；它仍只是最小帖子／定向通知实现，不代表 EF-02/03 已完成。

## 固定来源

- [EigenFlux README](https://github.com/phronesis-io/eigenflux/blob/3a3612d1ca78b62508f2863b51f88728d9e1d188/README.md)
- [Install](https://github.com/phronesis-io/eigenflux/blob/3a3612d1ca78b62508f2863b51f88728d9e1d188/skills/install.md)
- [Communication Skill](https://github.com/phronesis-io/eigenflux/blob/3a3612d1ca78b62508f2863b51f88728d9e1d188/skills/ef-communication/SKILL.md)
- [Private messages](https://github.com/phronesis-io/eigenflux/blob/3a3612d1ca78b62508f2863b51f88728d9e1d188/skills/ef-communication/references/message.md)
- [Relations](https://github.com/phronesis-io/eigenflux/blob/3a3612d1ca78b62508f2863b51f88728d9e1d188/skills/ef-communication/references/relations.md)
- [Stream](https://github.com/phronesis-io/eigenflux/blob/3a3612d1ca78b62508f2863b51f88728d9e1d188/skills/ef-communication/references/stream.md)
- [PM / relations design](https://github.com/phronesis-io/eigenflux/blob/3a3612d1ca78b62508f2863b51f88728d9e1d188/docs/pm_relations_design.md)
- [PM handler](https://github.com/phronesis-io/eigenflux/blob/3a3612d1ca78b62508f2863b51f88728d9e1d188/rpc/pm/handler.go)
- [Structured message error test](https://github.com/phronesis-io/eigenflux/blob/3a3612d1ca78b62508f2863b51f88728d9e1d188/cli/cmd/msg_error_test.go)
