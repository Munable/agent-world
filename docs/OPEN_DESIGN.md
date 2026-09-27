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

上述候选 **尚未进入 Runtime 正式合同，也不是最终协议选择**。

### 当前实验结果

仓库当前用 `experiments/cross_world_identity.py` 对这个候选做了可删除的垂直实验，验证代码不属于稳定 `agent_world` package API。

已经得到的证据：

- 用户根身份使用 Ed25519 非对称密钥；根私钥只留在用户侧实验对象，世界只收到公钥、签名、delegation 和 proof。
- 根身份可签发面向具体 device / Agent Host 公钥的 delegation，并限制 audience、能力和有效期。
- 世界签发一次性 challenge；device 对 challenge + delegation identity 做 proof-of-possession，challenge 绑定 deployment + universe audience，并防重放、过期和签名篡改。
- 两个不同 Python 进程、不同 Runtime DB、不同 identity DB 能验证同一个 root identity；两边创建各自本地 Participant Profile，role_id 可以不同。
- 每个世界仍签发自己的 world-local bearer credential；即使 universe 名字相同，A 的 bearer 不能在 B 使用。
- verifier 重启或更换 device / Agent Host 后，只要 root delegation 仍有效，就能回到该世界原有本地 Profile，而不是创建新的底层身份映射。
- root 签名的 delegation revocation 到达某个世界后，会阻止该 delegation 再签发本地 credential，并撤销该世界已从它签发的本地 credential。
- root rotation 当前采用 old root + new root 双签名连续性证明；两个世界可独立接受 rotation、保持各自原本 role/profile，并拒绝旧 root 的后续 delegation。实验可选择在 rotation 时撤销旧 root 已签出的本地 credential。

实验也明确暴露了尚未解决的问题：

1. **跨世界可关联性。** 当前实验的 `root_id` 是根公钥哈希，因此不同世界可以直接关联同一用户。这证明“同一身份”很简单，但隐私并不好；pairwise / selective-disclosure identity 仍需设计。
2. **root 丢失恢复。** 当前 rotation 需要 old root 与 new root 双签名，只解决主动轮换，不解决旧根私钥已经丢失或被盗后的身份恢复。是否需要 recovery authority / social recovery / 多设备阈值仍未定。
3. **撤销传播。** revocation 是可验证的，但不会凭空同步到所有独立部署。实验明确证明：只通知 A 时，B 仍继续承认旧 delegation，直到 B 也收到撤销证明。传播／新鲜度合同仍需设计。
4. **部署内跨存储原子性。** 实验故意把 identity verifier store 与 Runtime DB 分开，因此“身份映射／delegation provenance”与“本地 bearer 发放”不是同一数据库事务；当前仅做失败补偿。正式实现要么并入 Identity Core 的同一提交边界，要么设计明确的 delivery / compensation 机制。
5. **协议与密钥托管。** 当前 JSON canonicalization、标识格式和 Ed25519 只是候选实验；没有确定标准化 credential 格式、硬件密钥／系统 keychain、用户 consent UX 或第三方 issuer。
6. **本地授权仍然独立。** 实验只证明身份连续性和本地 credential 发放，不证明某用户在新世界有任何业务权限；这一点反而符合 I3。

下一步不是继续加更多身份功能，而是根据这些实验结果决定：正式 Root Identity / Delegation / Challenge Proof 是否进入 L3/L4，以及撤销传播、pairwise identity 和 root recovery 哪个先成为下一条可证伪假设。

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
