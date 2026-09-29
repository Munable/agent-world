# 验证证据横轴

复核：2026-09-27。验证只回答 **某条 L1-L6 声明在什么源码、环境和场景下得到什么证据**。测试不是 L7，也不能因为一个测试消费者需要某功能，就反向创建产品要求。

关键主张当前状态见 [TRACEABILITY](TRACEABILITY.md)。

## 核心必需验证

| 范围 | 检查什么 |
| --- | --- |
| 当前 unittest | Query/Command、原子提交、Receipt、身份／授权、state、迁移、view、stream、timer、retention、结构化错误等 Runtime 合同。 |
| 当前回归保护 | 仍属于现行合同的 schema / authorization / version / recovery 等缺陷不能被无声删除测试。 |
| 架构静态边界 | Runtime/SDK 不反向 import adapter、UI、示例世界或测试代码。 |
| 客户端合同测试 | snapshot/delta、乱序、stream 去重、表现队列等客户端行为。 |
| package check | wheel 能构建、独立安装、导入资源，并由 checkout 外的 package probe 组合使用公开 SDK。 |

这些检查也只证明自己的覆盖范围。单次全绿不是长期运行、任意世界通用性或生产安全性的证明。

## 不同证据各自能说明什么

| 证据 | 可以说明 | 不能说明 |
| --- | --- | --- |
| 单元／合同回归 | 某个明确语义在当前代码下成立。 | 整个产品已经完成。 |
| 当前 transport / integration 测试 | 当前 HTTP/MCP、身份和进程边界在被测路径中成立。 | 真实外部 Agent 长期行为或全部平台。 |
| checkout 外 package probe | 安装包、公开 import 与若干可选能力可以脱离源码目录组合使用。 | 它不是 Reference Application，也不能证明复杂领域天然适配。 |
| Capability Harness | 多参与者拓扑、并发、撤权、重放、恢复等明确能力组合成立。 | 某个具体产品体验已经合理。 |
| Reference Application | 一组真实领域规则能够消费 Runtime，并可暴露通用缺口。 | 该领域对象应该进入 Runtime。 |
| 浏览器视觉／轨迹 | 特定 UI 组合的呈现和最终事实一致。 | 服务器合同本身正确。 |
| soak / 长期运行 | 积累、恢复、保留、timer、升级组合是否出现时间相关问题。 | 未覆盖的业务语义。 |

## Runtime Capability Harness

验证不固定为“两名 Agent 对话”。底层能力按拓扑与故障维度组合：

| 维度 | 至少覆盖 |
| --- | --- |
| 参与者拓扑 | 1 个主体、1→1、1→N、N→1、N→N、多个权限组。 |
| 写入／竞争 | 单写、不同 scope 并发、同对象竞争、事务回滚、同 ID 重放、未知结果恢复。 |
| 观察 | 多消费者、快慢消费者、后加入、离线后恢复、重复／乱序、cursor 过期。 |
| 身份／权限 | observe/control、撤权、凭据失效、错误身份、跨 world instance 拒绝。 |
| 时间／保留 | timeout、timer、通知清理、状态／历史保留边界、进程重启。 |
| transport | HTTP/MCP 语义一致、多个 session/client、取消、唤醒与恢复。 |

确定性协议客户端负责精确复现与断言；真实外部 Agent 只用于验证发现、理解、宿主接续和实际使用体验，不能替代底层确定性测试。

## Reference Application 验证

社交是第一个现实消费者之一，可以验证公开发布、关联回应、私有会话、关系／拒收等具体流程，但它的对象和规则不自动上升为 Runtime 要求。

任何从 Reference Application 提升的 Runtime 能力都必须回答：

1. 它在什么非本应用场景中同样成立？
2. 现有 Runtime 能力为什么不足？
3. 最小可复现反例是什么？
4. 引入新原语后如何验证不会把领域规则塞进核心？

EigenFlux 参考机制的当前取舍见 [REFERENCE_EIGENFLUX](REFERENCE_EIGENFLUX.md)。

## 当前必须保留的证据

当前仓库不保存阶段编号测试或旧审计快照。某个过去出现过的缺陷如果仍对应现行合同，就把最小断言保留在当前命名的测试文件中。

当前关键证据入口包括：

- `tests/test_foundation.py`：事务、Operation/Receipt、本地 Credential、Join Ticket、credential rotation receipt、撤权、并发与基础恢复。
- `tests/test_key_identity.py`：身份公钥持钥证明、首次建档、同钥回档、新钥新身份、跨世界本地档案／本地 bearer、错误签名、过期 challenge 与禁用档案。
- `tests/test_onboarding.py`：operator credential rotation 的 operation_id、丢响应重放与 receipt 查询。
- `tests/test_transport.py`：HTTP/MCP 同源语义、会话身份绑定、严格主体归因、wait/cancel/wakeup、跨 transport replay。
- `tests/test_error_contract.py`、`tests/test_structured_error_transport.py`：结构化 recovery / retry / details 及 HTTP/MCP 保真。
- `tests/test_runtime_capability_matrix.py`：正式 Key Identity 参与者下的 1→N、N→1、N→N、撤权、重放、重启回档、并发首次进入、批量进入、跨独立 Runtime 隔离与同对象 CAS 竞争；`tests/test_transport.py` 另覆盖同一密钥的双 MCP session。
- view / stream / timer / world-data / SDK 测试：各自现行能力合同。
- `tools/check_package.py`：当前 package 构建与 checkout 外 probe。
- 两份 Node 客户端测试：当前 View / Stream 客户端合同。

当前 0.17.0 代码树已重新执行：`python tools/run_tests.py` 共 **221 个 unittest 全部通过**。`tests/test_key_identity.py` 的 **10 个密钥身份合同测试**覆盖默认 challenge → exchange → bootstrap；`tests/test_runtime_capability_matrix.py` 的 **9 个矩阵测试**现在全部使用正式 Key Identity 参与者，并覆盖同钥多 Credential、撤销后持钥重进、重启回档、独立 Runtime 隔离、并发首次进入、32 身份并发进入和同对象 CAS 竞争；`tests/test_transport.py` 另验证同一密钥的两枚本地 Credential 可同时建立两个 MCP session。0.17.0 package check 与两份 Node 客户端合同在默认入场变更时已通过；本轮只增强测试／证据，没有修改 package 或客户端实现。这个数字只描述当前树，不作为未来提交的永久成绩单。

## 新实验最低要求

任何新实验先写明：

1. 它要反驳哪一条具体 Goal / Invariant / Contract / Architecture claim。
2. 为什么现有最小测试不足。
3. 最小场景是什么。
4. 什么结果算失败，而不只是“跑起来了”。
5. 源码版本、环境、实际输出和未覆盖项。

优先补最小回归；只有交互性质无法被小夹具表达时，才增加更大消费者。失去当前验证用途的实验材料直接删除，过去内容由 Git 历史保存。

## 密钥身份验证证据

当前正式合同只验证“密钥就是身份”：

1. Ed25519 公钥作为 User Identity；世界通过一次性 challenge + signature 验证私钥持有。
2. 同一公钥第一次进入某 World Instance 时创建本地 Participant Profile；以后同钥回到同一档案。
3. 新公钥创建新身份／新档案，不允许通过名字、档案内容或声明认领旧公钥档案。
4. 同一公钥进入两个独立 Runtime 时，两边可以保存不同本地 profile / role_id；不会搬运 display name、avatar、资产或权限。
5. 每个世界独立签发 bearer Credential；A 的 Credential 在 B 无效。
6. challenge 过期、错误私钥签名和禁用本地档案都会拒绝进入。
7. 同一 challenge 的网络重试恢复同一个本地 Credential，不重复创建档案。
8. 身份密钥没有恢复／换钥继承接口；这不是待补功能，而是当前 L2 明确边界。


## 当前仍缺的验证

长期 soak、当前提交的完整远端 CI 矩阵、真实多宿主 Agent、跨机器网络分区、生产数据库迁移和真实用户使用仍需要独立证据。
