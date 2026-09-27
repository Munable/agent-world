# 未决设计、实现差距与待验证

更新：2026-09-27。这里不是第七层。每个未决项都标明真正归属；候选方案在决定前不具有现行合同地位。

## 当前处理顺序

1. **先验证 Runtime 能力面，而不是固定两人故事。** 建立可编排 Capability Harness，覆盖单主体、多主体、1→N 广播、N→1 汇聚、N→N、并发竞争、离线／后加入、撤权、重连和恢复。
2. **并行维护一个社交 Reference Application。** 它用于真实消费 Runtime、暴露 API／恢复／权限缺口，不定义产品本体，也不把 Conversation / Friend / Block 强塞进 Runtime。
3. **缺口先分类。** Reference Application 自己的领域问题留在应用；已有 Runtime 能力可解决的就正确使用；只有跨领域通用且当前无法可靠表达的问题才进入 Runtime。
4. 跨世界身份、第三方／群众公裁、外部副作用交付等长期问题继续按各自层级推进，不被首个 Reference Application 的范围覆盖或取消。

## EigenFlux 参考采纳清单

| 编号 | 借鉴点 | 当前取舍 |
| --- | --- | --- |
| EF-01 | 分段接入、稳定本地配置、初次接入与恢复分流 | **采纳方向。** 用于 Agent/adapter 合同；不复制其 CLI 或每 Agent 一套用户身份。 |
| EF-02 | 来源关联的会话、历史与关系请求 | **Reference Application 候选。** 用于暴露对象引用、参与者授权和未决事项恢复，不成为 Runtime 固有社交模型。 |
| EF-03 | 拒收／屏蔽／未回应联系预算 | **Reference Application 候选。** 采纳问题，不照抄次数、silent-success 或好友规则。 |
| EF-04 | 通知 + 查询恢复、cursor、重连与有界退避 | **采纳通用模式。** 先复用现有 wait / stream / snapshot / reset，不承诺单账号单流或读取即已读。 |
| EF-05 | 结构化错误、retry/reset/recovery 提示并由客户端完整保留 | **通用 Runtime 合同已落地并有回归证据。** HTTP/MCP 保留 recovery、可选 retry-after 与结构化 details；领域专用 taxonomy 仍由具体世界定义，operation_id + Receipt 语义不变。 |
| EF-06 | 公开资料、私有历史、来源标识与授权分离 | **采纳边界。** 不默认上传私人上下文，不从名称推断认证。 |

来源与代码／测试核对范围见 [REFERENCE_EIGENFLUX](REFERENCE_EIGENFLUX.md)。

## 1. 跨世界身份架构

**归属：L2 身份不变量 + L3 身份模型 + L4 凭据／撤销合同 + L5 信任架构。优先级高。**

L1/G2 已确定：用户控制底层身份，并应能向彼此独立的世界证明“这是同一个底层身份”，而各世界仍分别决定本地授权。当前 Role 只在同一 Runtime 数据库稳定，world-scoped bearer token 只证明某个世界内的调用资格，因此不能靠删除 universe 检查解决。

### 当前优先候选：用户持有根身份 + 世界本地调用凭据

建议下一轮按以下结构做最小原型，而不是发一枚跨世界万能 bearer：

1. **User-held Root Identity**：用户持有非对称根密钥或等价的可验证身份证明；Runtime／世界不持有用户根私钥。
2. **Device / Agent Delegation**：具体设备或 Agent Host 使用由根身份明确委托、可限期／撤销的证明代表用户发起接入，避免把根私钥直接交给每个 Agent。
3. **World Verification**：世界通过 challenge / proof-of-possession 验证根身份或有效委托，并把它映射到本世界的 Participant Profile。
4. **World-local Credential**：验证身份后仍由该世界发放自己 scope 的调用凭据；身份成立不自动授予任何世界动作权限。

这个结构能同时满足“同一用户跨世界可验证”和“各世界权限独立”，也不要求不同世界共享数据库或互相信任对方发出的 bearer token。

仍需定稿：

- 根身份采用什么标准／编码以及标识是否直接由公钥派生。
- 是否默认允许不同世界关联同一全局标识，还是支持 pairwise / selective-disclosure 标识以降低跨世界跟踪。
- 根密钥丢失、轮换、恢复与设备撤销怎样建立连续身份，而不是创建一个新用户。
- 委托凭证的 scope、有效期、撤销发现与宿主 UX。
- Participant Profile 哪些字段属于用户可携带资料，哪些永远是各世界本地事实。
- 是否需要第三方 issuer / recovery authority；若需要，其信任范围必须显式。

上述候选 **尚未实现，也不是最终协议选择**。下一步应先做最小 proof + 两个独立 world deployment 的验证实验，再决定正式 L3/L4 schema。

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

## 当前缺口状态

| 项目 | 归属 | 当前状态 |
| --- | --- | --- |
| 跨世界身份 | L2-L6 | **已确认 Runtime/产品根缺口。** 当前只有 world-scoped credential；候选架构见本文件第 1 节。 |
| 外部副作用交付 | L4/L5 | **已确认边界缺口。** 需要外部写入时尚无通用 outbox / delivery / compensation contract。 |
| 通用共同事项辅助 | L3/L4 | **未证明需要进入 Runtime。** 继续由 Reference Application 验证。 |
| 第三方／群众公裁 | L3/L4 | **产品方向未定稿。** 先设计成立规则，不算 Runtime 当前缺原语。 |

## 待验证

**归属：证据横轴。**

长期运行需要继续观察连续操作、事件保留、未决事项、timer 积压、重启、多次升级、备份恢复和存储增长。缓存／表现仍需覆盖过期、驱逐、权限变化、乱序响应和换客户端。

临时消费者不承担“证明 Runtime 方向正确”的职责。只有当某个实验明确针对一条可被反驳的假设时，才值得继续投入；失去当前用途就删除。

## 暂不作为当前前提

完整游戏、强制 Party／好友／背包模型、统一资产平台、分布式托管和不可信插件沙箱都不是当前 Runtime 成立的默认门槛。若产品目标将来改变，再把相应问题提升到正确层级。
