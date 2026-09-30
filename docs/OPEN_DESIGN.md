# 未决设计、实现差距与待验证

复核：2026-09-30。这里不是第七层。每个未决项都标明真正归属；候选方案在决定前不具有现行合同地位。

## 当前处理顺序

1. **不继续为已经成立的本机能力重复造抽象。** Key Identity、Capability Harness、Commons v2、恢复矩阵和真实 OpenCode/Pi 宿主短时实验都已经有当前证据；没有新的反例时不增加 Runtime 原语。
2. **下一优先级是证据，而不是功能。** 重点补跨机器／跨主机 Agent、网络分区、长期 soak、生产数据库迁移和真实用户使用。它们失败时再判断是实现缺陷、合同问题还是环境问题。
3. **Reference Application 只按真实产品流程继续长。** Friend / Block / 关系／反骚扰等只有真实切片需要时才加入 Commons，并继续先留在应用层；出现跨领域反例后再判断是否提升到 Runtime。
4. **外部副作用交付是唯一已确认的 Runtime 实现缺口，但不抢跑。** 只有真实消费者需要支付、第三方 API、文件或其他外部写入时，再设计 outbox / delivery / compensation 合同。第三方／群众公裁和长期 retention 语义继续按各自层级推进。

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


## 1. 共同事项的接续模型

**归属：L3 + L4。**

现有 State、Operation、Receipt、Event 和 Control Lease 足以让具体世界构建流程，但不等于已经有通用会话／审批状态机。

需要明确：是否存在值得通用化的 Request / Response / Confirmation / Completion 对象；引用关系；允许提交者；Delivered / Read / Responded / Confirmed / Executed / Completed 的可选阶段；并发、晚到、撤回、超时、重发和换客户端恢复。

Commons v2 已经完成一轮最小非游戏交互验证：Post / Reply / Conversation / Message、私有授权、通知与离线恢复都能由现有 State + Operation/Receipt + Event 表达。当前证据**不支持**增加全局 conversation turn、统一审批状态机或“所有动作必领租约”；只有新的跨领域反例出现时才重开这个问题。

## 2. 第三方／群众公裁

**归属：L3 + L4；定稿后才进入 L5/L6。**

这是明确待做方向。第三方主体及其 Agent 在 Runtime 外部作判断；Runtime 只验证结构化提交和既定规则，不阅读证据文本后自行推理判案。

需要明确 Case / Evidence / Reviewer / Decision 哪些概念通用；参与资格、任务分配、利益冲突、证据可见性、意见格式、有效裁决成立条件、重复提交、超时／退出、复核和最终执行。

尚未选择票数、权重、随机抽样、仲裁者资格或激励机制，也不要求所有世界经过公裁。

## 3. 长期数据保留后的语义

**归属：L4。**

Receipt、commit metadata、未决事项、终态 Scheduled Effect identity 等长期增长后的压缩／归档还没有统一合同。必须先确定旧 Operation 重放、未知结果恢复和业务终态语义，再决定何时可以明确过期。

## 当前缺口状态

| 项目 | 归属 | 当前状态 |
| --- | --- | --- |
| 外部副作用交付 | L4/L5 | **已确认边界缺口。** 需要外部写入时尚无通用 outbox / delivery / compensation contract。 |
| 通用共同事项辅助 | L3/L4 | **仍未证明需要进入 Runtime。** Commons v2 的 Post/Reply/Conversation/Message 已能由现有 State + Operation/Receipt + Event 表达，当前没有新证据要求增加通用会话／请求原语。 |
| 第三方／群众公裁 | L3/L4 | **产品方向未定稿。** 先设计成立规则，不算 Runtime 当前缺原语。 |

## 待验证

**归属：证据横轴。**

长期运行需要继续观察连续操作、事件保留、未决事项、timer 积压、重启、多次升级、备份恢复和存储增长。缓存／表现仍需覆盖过期、驱逐、权限变化、乱序响应和换客户端。

临时消费者不承担“证明 Runtime 方向正确”的职责。只有当某个实验明确针对一条可被反驳的假设时，才值得继续投入；失去当前用途就删除。

## 暂不作为当前前提

完整游戏、强制 Party／好友／背包模型、统一资产平台、分布式托管和不可信插件沙箱都不是当前 Runtime 成立的默认门槛。若产品目标将来改变，再把相应问题提升到正确层级。
