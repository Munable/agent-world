# 未决设计、实现差距与待验证

更新：2026-09-27。这里不是第七层。每个未决项都标明真正归属；候选方案在决定前不具有现行合同地位。

## 1. 跨世界身份架构

**归属：L2 身份不变量 + L3 身份模型 + L4 凭据／撤销合同 + L5 信任架构。优先级高。**

L1/G2 已确定：用户控制底层身份，并应持有可跨不同世界验证该身份的凭证或等价证明；各世界仍分别授权。当前代码只有同一数据库内稳定 Role profile，bearer token 仍绑定单 universe，因此这不只是“删一个 universe 检查”的实现任务。

需要明确：

- 跨独立开发者／部署时，什么标识表示“同一个底层用户身份”。
- 用户实际持有什么可跨世界证明身份的凭证或密钥；世界验证什么，而不是要求每个世界重新创建账号。
- 信任根如何建立：共同 issuer、公钥／用户签名、联邦信任或其他机制尚未选择。
- credential scope、最小披露、世界间可关联性和隐私边界。
- revoke、rotate、丢响应恢复、设备更换和被盗凭据处理。
- 世界自己的权限如何继续保持本地，避免“全局身份”变成“全局万能权限”。

在这些问题定稿前，当前 universe-scoped token 只能算过渡实现。

## 2. 共同事项的接续模型

**归属：L3 + L4。**

现有 State、Operation、Receipt、Event 和 Control Lease 足以让具体世界构建流程，但不等于已经有通用会话／审批状态机。

需要明确：是否存在值得通用化的 Request / Response / Confirmation / Completion 对象；引用关系；允许提交者；Delivered / Read / Responded / Confirmed / Executed / Completed 的可选阶段；并发、晚到、撤回、超时、重发和换客户端恢复。

先用最小非游戏交互验证语义，再决定哪些真的应该进入 Runtime。不要先造全局 conversation turn、统一审批状态机或“所有动作必领租约”。

## 3. 第三方／群众公裁

**归属：L3 + L4；定稿后才进入 L5/L6。**

这是明确待做方向。第三方主体及其 Agent 在 Runtime 外部作判断；Runtime 只验证结构化提交和既定规则，不阅读证据文本后自行推理判案。

需要明确 Case / Evidence / Reviewer / Decision 哪些概念通用；参与资格、任务分配、利益冲突、证据可见性、意见格式、有效裁决成立条件、重复提交、超时／退出、复核和最终执行。

尚未选择票数、权重、随机抽样、仲裁者资格或激励机制，也不要求所有世界经过公裁。

## 4. 长期数据保留后的语义

**归属：L4。**

Receipt、commit metadata、未决事项、终态 Scheduled Effect identity 等长期增长后的压缩／归档还没有统一合同。必须先确定旧 Operation 重放、未知结果恢复和业务终态语义，再决定何时可以明确过期。

## 5. World Package 信任与 legacy raw connection

**归属：L5 + L6。**

当前 World Package 是部署方信任的 Python 代码，`FunctionContext.conn` 是 legacy escape hatch。它不提供敌对租户隔离，也让世界代码知道 SQLite 内部结构。

短期目标是继续保证 managed state write 不绕过 schema／authorization，并减少新世界对 raw connection 的依赖。若未来产品要托管互不信任的第三方世界代码，需要重新设计进程、权限、资源限制和存储隔离，而不是把现有业务 scope 描述成沙箱。

## 明确实现缺口

| 项目 | 归属 | 当前状态 |
| --- | --- | --- |
| 跨世界身份 | L5/L6 相对 G2/I3 | 未定最终信任架构；当前 token universe-scoped。 |
| credential rotate 丢响应恢复 | L4/L6 | 目前仍可能需要 operator 介入。 |
| 通用共同事项辅助 | L3/L4 | 尚未决定是否值得提取为 Runtime 模型。 |
| 第三方／群众公裁 | L3/L4 | 概念和成立规则未定稿。 |
| 外部副作用交付 | L4/L5 | 没有通用 outbox / delivery / compensation contract。 |
| legacy raw connection | L6 | 兼容入口仍存在，新世界不应依赖其内部 SQL 形状。 |

managed raw-state write 的 schema／authorization 回退已在本分支复现并修复，不再列为未完成项；范围见 [IMPLEMENTATION](IMPLEMENTATION.md) 和历史审计。

## 待验证

**归属：证据横轴。**

长期运行需要继续观察连续操作、事件保留、未决事项、timer 积压、重启、多次升级、备份恢复和存储增长。缓存／表现仍需覆盖过期、驱逐、权限变化、乱序响应和换客户端。

灯溪镇、灰烬地城等临时消费者不再承担“证明 Runtime 方向正确”的职责。只有当某个新实验明确针对一条可被反驳的假设时，才值得继续投入。

## 暂不作为当前前提

完整游戏、强制 Party／好友／背包模型、统一资产平台、分布式托管和不可信插件沙箱都不是当前 Runtime 成立的默认门槛。若产品目标将来改变，再把相应问题提升到正确层级。
