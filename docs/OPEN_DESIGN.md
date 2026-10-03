# 未决设计、实现差距与待验证

复核：2026-10-04。这里不是第七层。每个未决项都标明真正归属；候选方案在决定前不具有现行合同地位。

## 当前处理顺序

1. **不继续为已经成立的本机能力重复造抽象。** Key Identity、Capability Harness、Commons v3、恢复矩阵和真实 OpenCode/Pi 宿主短时实验已有各自范围的证据；没有新的反例时不增加 Runtime 原语。
2. **当前优先级是基础协议合同核对与最小组合验证。** 不预设人类用户有共同目标、相同理解或体验期待，也不把 Agent 定义为协作者、冒险者或玩家。先核对主体归因、授权、合法行动与观察、错误、并发、重复操作、状态一致性和接续；测试中的目标与角色仅属于具体消费者。保留 Commons 与河谷营地的现有证据，暂停默认扩展营地章节或协作房间；只有复现问题才局部修改。跨机器、跨 CLI 和单纯延长长跑不是当前主线。已有营地证据见 [WORLD_EXPERIMENT](WORLD_EXPERIMENT.md)。
3. **Reference Application 只按真实产品流程继续长。** Friend / Block / 关系／反骚扰等只有真实切片需要时才加入 Commons，并继续先留在应用层；出现跨领域反例后再判断是否提升到 Runtime。
4. **外部副作用交付仍是已确认的 Runtime 实现边界，但不抢跑。** 本次多宿主消费者暴露的凭据管理、正式 observe 发行和原提交凭据归因已局部修复；不把“唯一缺口”当作拒绝新反例的前提。只有真实消费者需要支付、第三方 API、文件或其他外部写入时，再设计 outbox / delivery / compensation 合同。第三方／群众公裁和长期 retention 语义继续按各自层级推进。

## EigenFlux 参考采纳清单

| 编号 | 借鉴点 | 当前取舍 |
| --- | --- | --- |
| EF-01 | 分段接入、稳定本地配置、初次接入与恢复分流 | **采纳方向。** 用于 Agent/adapter 合同；不复制其 CLI 或每 Agent 一套用户身份。 |
| EF-02 | 来源关联的会话、历史与关系请求 | **Reference Application 已部分落地。** Commons v2 已验证 Post/Reply/Conversation/Message、参与者授权和断线恢复；关系请求仍保留在应用层候选，不成为 Runtime 固有社交模型。 |
| EF-03 | 拒收／屏蔽／未回应联系预算 | **Reference Application 候选。** 采纳问题，不照抄次数、silent-success 或好友规则。 |
| EF-04 | 通知 + 查询恢复、cursor、重连与有界退避 | **采纳通用模式。** 先复用现有 wait / stream / snapshot / reset，不承诺单账号单流或读取即已读。 |
| EF-05 | 结构化错误、retry/reset/recovery 提示并由客户端完整保留 | **通用 Runtime 合同已落地并有回归证据。** HTTP/MCP 保留 recovery、可选 retry-after 与结构化 details；领域专用 taxonomy 仍由具体世界定义，operation_id + Receipt 语义不变。 |
| EF-06 | 公开资料、私有历史、来源标识与授权分离 | **采纳边界。** 不默认上传私人上下文，不从名称推断认证。 |

来源与代码／测试核对范围见 [REFERENCE_EIGENFLUX](REFERENCE_EIGENFLUX.md)。


## 1. 特定世界中共同事项的接续模型

**归属：L3 + L4。**

现有 State、Operation、Receipt、Event 和 Control Lease 足以让具体世界构建流程，但不等于已经有通用会话／审批状态机。

本项只讨论需要共同事项的世界，不定义 Agent 本体，不预设所有参与者来合作，也不要求每次互动沿共同目标或确认流程推进。

需要明确：是否存在值得通用化的 Request / Response / Confirmation / Completion 对象；引用关系；允许提交者；Delivered / Read / Responded / Confirmed / Executed / Completed 的可选阶段；并发、晚到、撤回、超时、重发和换客户端恢复。

Commons v2 已经完成一轮最小非游戏交互验证：Post / Reply / Conversation / Message、私有授权、通知与离线恢复都能由现有 State + Operation/Receipt + Event 表达。当前证据**不支持**增加全局 conversation turn、统一审批状态机或“所有动作必领租约”；只有新的跨领域反例出现时才重开这个问题。

## 2. 第三方／群众公裁

**归属：L3 + L4；定稿后才进入 L5/L6。**

这是明确待做方向。第三方主体及其 Agent 在 Runtime 外部作判断；Runtime 只验证结构化提交和既定规则，不阅读证据文本后自行推理判案。

需要明确 Case / Evidence / Reviewer / Decision 哪些概念通用；参与资格、任务分配、利益冲突、证据可见性、意见格式、有效裁决成立条件、重复提交、超时／退出、复核和最终执行。

尚未选择票数、权重、随机抽样、仲裁者资格或激励机制，也不要求所有世界经过公裁。

河谷营地只是一个世界定义的获准单裁判与本人接受流程，复用现有状态／操作／回执／授权；它不定稿通用公裁模型、独立性、利益冲突或公平性。裁判私有注记的访问 ACL 与裁判在公开理由中再次披露是不同边界；当前允许获准可信读者自由解释，不宣称通用信息流安全。模型语义区分、注入抵抗和真人可理解性须分别验证。

## 3. 长期数据保留后的语义

**归属：L4。**

Receipt、commit metadata、未决事项、终态 Scheduled Effect identity 等长期增长后的压缩／归档还没有统一合同。必须先确定旧 Operation 重放、未知结果恢复和业务终态语义，再决定何时可以明确过期。

## 4. 主体识别与授权表达

**归属：L3 + L4；是否需要改动尚未确定。**

现行 User Identity 以持钥证明识别身份，世界签发 Credential 代表 Principal，外部 Agent／客户端使用获准凭据调用。是否需要单独的稳定 Agent 标识，如何区分人类用户、行动主体、外部程序、宿主、会话与世界角色，须由实际归因、权限或恢复问题驱动；不从协作示例反推 Agent 定义。

本次已确认并修复的是 L6 接入缺口：用户可在自己的密钥保管端签出独立 control／observe Credential，以普通公开 HTTP 列表／单撤销管理本主体连接；新世界 Command Receipt 持久保存原 `actor_role_id / credential_id`，同主体换凭据恢复仍返回原来源。旧回执不补填；事件／公开作者没有新增宿主身份。Credential ID 只说明提交通过哪枚凭据，不证明外部程序身份，也不建立 Agent 归属。

所有同 Principal 的 control bearer 都有同等凭据管理权；当前没有仅人类 owner 的管理权限或 bearer 自动续签。持有私钥者可重新进入，宿主保管与同 OS 用户进程隔离由外部环境负责。独立 bearer 验证不等于 OS 沙箱证明。

此前提出的独立 AgentIdentity、用户与多个 Agent 的归属关系仍是候选，不是已接受合同。参与目的不同不自动要求新增身份机制，也不能借身份设计预设个人助手、上下级、协作关系或统一目标。

## 当前缺口状态

| 项目 | 归属 | 当前状态 |
| --- | --- | --- |
| 外部副作用交付 | L4/L5 | **已确认边界缺口。** 需要外部写入时尚无通用 outbox / delivery / compensation contract。 |
| 多宿主凭据发行／管理／提交来源 | L6 | **本次已局部修复。** 签名 control／observe、自己的列表与单撤销、原提交 Credential 来源；保留 User Identity／Principal 模型，不定稿 AgentIdentity。 |
| 通用共同事项辅助 | L3/L4 | **仍未证明需要进入 Runtime。** Commons v2 的 Post/Reply/Conversation/Message 已能由现有 State + Operation/Receipt + Event 表达，当前没有新证据要求增加通用会话／请求原语。 |
| 第三方／群众公裁 | L3/L4 | **产品方向未定稿。** 先设计成立规则，不算 Runtime 当前缺原语。 |

## 待验证

**归属：证据横轴。**

当前四小时不重启验证已通过数据一致性检查，但 RSS 从 76252 KiB 增至 110552 KiB；这只是观测，不是已证实的泄漏。若以后重新开展资源验证，应做堆分配／缓存／分配器归因与更大数据量对照，不能仅把有限时长通过当成生产稳定性。具体结果见 [REFERENCE_GATE](REFERENCE_GATE.md)。

长期运行需要继续观察连续操作、事件保留、未决事项、timer 积压、重启、多次升级、备份恢复和存储增长。缓存／表现仍需覆盖过期、驱逐、权限变化、乱序响应和换客户端。

临时消费者不承担“证明 Runtime 方向正确”的职责。只有当某个实验明确针对一条可被反驳的假设时，才值得继续投入；失去当前用途就删除。

## 暂不作为当前前提

完整游戏、强制 Party／好友／背包模型、统一资产平台、分布式托管和不可信插件沙箱都不是当前 Runtime 成立的默认门槛。若产品目标将来改变，再把相应问题提升到正确层级。
