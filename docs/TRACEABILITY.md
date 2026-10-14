# 六层关键主张追溯

复核：2026-10-14。这里将关键L1/L2主张追到L3-L6与证据，不新增规格或质量评分。当前源码为0.38.0／world42／state25；本轮最终Python／Node及实际安装包检查已通过，四次Native创作尝试未形成新实例，GPT公开SDK创作与Native阿岚接续的有界真实链已有证据。旧0.37模块化证据不自动覆盖新接口或模型结果。当前源码、整轮验证和历史原生Agent结果分别见[当前研发记录](SHARED_GROVE.md#当前研发记录)，本机合同与恢复结果不替代真人体验、跨机及规模验收。

| 主张 | 下层落点 | 当前状态 | 核心证据／限制 |
| --- | --- | --- | --- |
| G1 世界持续存在 | State Fact、Operation、Commit、Receipt；L4 提交／恢复合同；Runtime journal/state | **已实现核心机制；长期证据有限** | `tests/test_foundation.py`、`tests/test_world_data.py`、`tests/test_world_sdk.py` 覆盖提交、恢复、重启与升级；没有真实长期 soak 证明。 |
| G2 用户带身份跨世界 | User Identity、Participant Profile、Credential；身份合同 | **已实现最小密钥身份合同，并接成默认组合入口** | `tests/test_key_identity.py` 验证默认 challenge → exchange → bootstrap；Capability Harness 进一步验证同钥多 Credential／双 MCP session、撤销后持钥重进、重启回档、并发首次进入与独立 Runtime 隔离。新公钥就是新身份，档案不跨世界搬运，密钥丢失不提供恢复。 |
| G3 Agent体验为首要责任 | 能力发现、Agent contract、固定观察、原意图与Receipt；独立客户端 | **当前官方世界已接通并本地验证** | `test_grove_agent_contract.py`、`test_grove_agent_client.py`、`test_grove_independent_consumers.py`覆盖正式发现、护栏、HTTP写／MCP恢复、错意图及停止后只读恢复；`test_grove_program_syntax.py`、`test_grove_mcp_output_schema.py`、`test_grove_native_tool_mapping.py`覆盖机器语法入口、成功／错误schema及reference宿主实际工具名。不以协议成功证明模型理解。 |
| G4 外部宿主保留私人运行与控制 | External Agent／Client、执行控制器、私人Journal；L5宿主边界 | **架构和当前宿主边界成立** | Runtime无模型推理器；`test_architecture_boundaries.py`、`test_grove_execution.py`、`test_grove_revoked_recovery.py`覆盖依赖、预算、停止、封段及原操作恢复。未知scope不重放，不证明所有未来宿主正确。 |
| G5 人类通过双向语义接口参与 | DTO、动作、参数、候选与原子Command；人类API／renderer | **当前官方契约与独立消费通过** | `test_grove_client_projection.py`、Node客户端与`test_grove_independent_consumers.py`覆盖版本／身份／水位、分页、9项来源输入及按正式动作描述提交；未验证所有renderer或新视觉体验。 |
| G6 世界开发者拥有领域规则 | World Definition、Domain Object、公开作者SDK | **核心机制与独立消费成立** | `test_world_sdk.py`与`test_independent_integration.py`覆盖陌生作者／客户端消费正式接口；有限样本不证明任意领域天然适配。 |
| G7 真实共同后果推动通用能力 | 官方世界规则、持久事实、共同内容与后续选择 | **本轮有界跨宿主链已核对；开放Native创作仍有失败** | 当前真实模型、自动推进及跨作者接续只见[SHARED_GROVE](SHARED_GROVE.md#当前研发记录)。四次Native创作尝试未形成新实例；GPT公开SDK实际创作并核验0→1，原Native阿岚接续且自行读取同实例1→2；途中有恢复和目标纠偏，按主体分别记录，不补判旧尝试成功。新增语法、诊断与效果读取不自行证明创作成功。 |
| I1 声明机制产生权威事实 | Command、State Fact、Commit；L4 结构／授权／规则链 | **已实现底座** | `tests/test_foundation.py`、`tests/test_managed_state_boundary.py`、transport tests 覆盖 schema、授权、回滚；Runtime 不解释文本语义。 |
| I2 不伪造他人意愿 | Principal、Credential、Command；世界业务状态机 | **Runtime 提供归因底座，具体确认仍由世界定义** | 调用主体来自鉴权而非业务参数；没有通用 Confirm 类型，因此 Runtime 也不能替所有世界证明业务确认正确。 |
| I3 用户身份只认密钥，且与领域授权分离 | Identity Key + world-local Credential + command/state authorization | **已实现当前机制** | challenge signature 证明持钥；`identity_keys` 保存公钥到本地 role 映射；Function/state authorization、observe/control 继续独立判断。不同公钥不能合并，世界 bearer 不能跨实例使用。 |
| I4 Runtime 不冒充用户／Agent | External Agent / Client、System Actor、Scheduled Effect | **已实现架构边界** | `tests/test_timers.py` 覆盖定时事项离线执行与不保存用户 token；系统行为与用户行为来源区分。 |
| I5 提交事实独立于会话 | Commit、Receipt、authoritative state | **已实现核心机制** | receipt 恢复、进程重启、状态持久化测试存在；网络外部副作用不在该保证内。 |
| I6 同一 Runtime 提交无半成品 | L4 原子提交；事务执行核心 | **有强回归覆盖** | `test_write_cannot_commit_partially`、output/schema failure、timer retry/rollback、history rollback；`tests/test_storage_recovery.py` 另在真实子进程暂存普通 Command 的状态／回执／通知／stream／timer 后、COMMIT 前退出，重启确认零残留再同 Operation 提交一次。 |
| I7 未知结果不靠猜测 | Operation + Receipt + structured recovery error + credential rotation receipt | **已实现核心写操作语义** | 普通 Command receipt、同 ID replay、结构化 recovery 均有回归；identity token rotation 现在也以 operation_id 持久化恢复映射，可在响应丢失或 Runtime 重启后恢复同一 replacement credential。 |
| I8 派生观察不创造事实 | View、Stream、Sync Position、Presentation 横轴 | **已实现主要边界** | view/stream/client tests 覆盖缓存、乱序、撤权、retention gap；具体 UI 仍需按世界正确命名业务状态。 |
| I9 world instance 默认隔离 | World Instance、instance-scoped state/action | **支持接口层已实现；不是敌对代码安全沙箱** | `test_world_instances_are_isolated`、cross-universe write 回归存在。FunctionContext 不再公开数据库连接；但受信任 Python World Package 仍与 Runtime 同进程，因此这不是恶意租户沙箱。 |
| I10 Runtime 领域中立 | Domain Object 由世界定义；核心不含固定玩法 | **当前代码结构基本符合** | architecture boundary test 防止 core 反向依赖示例／adapter；有限示例只能证明未立即耦合，不能证明未来不会退化。 |
| 官方世界可组合后果 | 世界声明、真实物料、数量、关系与持久规则执行 | **语法、诊断与效果读取已实现；真实范围独立** | 本轮机器schema只描述草稿语法，跨节点语义仍由编译器检查；依赖诊断明确static_only／world_facts_checked=false／execution_proven=false，provided与deferred不证明现场执行。effect_evidence绑定精确实例与定义，只读注册事实、显式导出与有限类型化历史；非法谓词提供有界合法目录或正式续读，unknown及cursor部分覆盖不等于效果不存在，observed不建立行动因果归属。实际检查见[当前研发记录](SHARED_GROVE.md#当前研发记录)。 |
| 宿主承接共同事实 | 当前观察、原Operation／Receipt、私人目标、有限授权及预算 | **同一世界参与与恢复成立；目标完成仍为Agent声明** | completed检查点保留completion_source=agent_claim、verification.status=unverified；原回执、新鲜读回及本任务已交付的效果读取引用不能裁定自然语言目标。当前GPT公开SDK与Native接续已有原回执和同实例变化证据，模型曾提交错误完成声明，旧湿润作品失败不由新接口补判。实际状态只在[当前研发记录](SHARED_GROVE.md#当前研发记录)接续。 |

## Runtime 能力矩阵

本表只描述当前能力和缺口，不把“测试覆盖较少”误写成“功能不存在”。

| 能力 | 当前状态 | 主要判断 |
| --- | --- | --- |
| Principal 归因与 world-scoped Credential | **已实现并有强回归** | Bearer credential 绑定 role + world instance；observe/control、撤权、过期与 session 绑定已有测试。 |
| 跨独立部署 User Identity | **已实现最小密钥身份** | 同一 Ed25519 公钥可在独立 Runtime 完成持钥证明；各世界保存本地档案并独立签发 credential。系统明确不提供密钥找回、换钥继承或档案跨世界同步。 |
| 世界本地授权 | **已实现并有强回归** | Function authorization、state authorization、credential access mode 分离；`tests/test_discovery_authorization.py` 通过 HTTP/MCP 验证目录可见／隐藏与执行允许／拒绝的四组合，拒绝无持久副作用。 |
| State / schema / CAS / journal | **已实现并有强回归；原库存在已知历史缺口** | 当前状态、版本、删除 tombstone、历史 journal、schema 与授权均有合同测试。本轮原库既有state_changes损坏经副本恢复，当前状态、其他27张表及221115笔原Operation逐值一致；22项日志缺口中11项由原WAL找回，另11项metadata journal不可恢复，history floor为6974378并保留history gap。原DB／WAL／SHM保留于 [原始归档](/Volumes/DevDisk/DeveloperData/Archives/agent-world/shared-grove/agent-effects-20261014-original/)，不把当前状态恢复写成历史无损。 |
| 原子 Command / Commit | **已实现并有强回归** | 状态、Receipt、Event、Stream、Timer 等受管效果在同一提交边界成立或回滚。 |
| Operation / Receipt / 未知结果恢复 | **已实现核心机制** | 同 operation_id 重放、响应丢失恢复、重启后 receipt 恢复成立。 |
| Credential rotation 未知结果恢复 | **已实现** | rotate 要求 operation_id，并持久化 rotation receipt；不保存 replacement token 明文，可重建同一结果。 |
| Notification Event / bounded wait | **已实现** | 定向事件、分页、cursor、bounded wait、取消与唤醒有测试。 |
| Shared Stream | **已实现可选能力** | 有序发布、授权、cursor、历史截断/reset 与客户端去重已有覆盖。 |
| View / Snapshot / Sync / Timeline | **已实现可选能力** | 授权投影、增量同步、checkpoint、retention gap、乱序客户端处理已有覆盖。 |
| Control Lease / fencing | **已实现可选能力** | claim/renew/finish、过期与 stale runtime fencing 有测试；不作为所有行为前提。 |
| Scheduled Effect / Timer | **已实现可选能力** | 持久 timer、离线 owner、retry/rollback、重启与 retention 有覆盖。 |
| 连续系统工作检查点 | **已实现；验证范围见当前研发记录** | 当前代次、完成水位与最新准确回执；独立任务提交、重启接续、旧代不重做及引用保护。玩家Receipt和一次性Timer不采用这项过期策略。 |
| Shared Grove 相互作用域 | **已实现；不代表生产规模** | 完整可变依赖闭包、帧前准备、域内预算、局部压力恢复和完成水位；正式路径合同见 `test_grove_domain_contracts.py`。统一帧屏障与SQLite单写者仍是明确边界。 |
| Shared Grove 能力依赖诊断 | **已实现静态诊断；不证明执行** | 类型引用、声明机制与构造初值给provided／missing／deferred／unknown建议；现场事实、材料、事件、可达性与效果未被证明，截断省略不等于无依赖。 |
| Shared Grove 精确实例效果读取 | **已实现有界证据接口；历史始终部分覆盖** | 同一entity_id／definition_ref的注册事实及显式导出、最后一次类型化变化和公开outcome；当前谓词成立不代替效果见证，after_step只限定见证判定，未知与缺口沿正式cursor继续读取，不授予权限或裁定目标。 |
| Retention | **已实现机制；长期策略仍需验证** | event/history/stream 保留与 gap recovery 已实现；长期压缩与真实增长仍需 soak。 |
| 结构化错误与恢复提示 | **已实现** | recovery / retry_after / details 经 HTTP/MCP 保真。 |
| HTTP / MCP 统一 Gateway | **已实现主要合同** | 同函数、同错误、receipt replay、identity/session 边界有跨 transport 测试。 |
| 多参与者拓扑验证 | **Harness 已覆盖密钥身份与主要本机拓扑** | 参与者通过正式 Key Identity 进入；1→N、N→1、N→N、并发 CAS、撤权后持钥重进、重启回档、同钥多 Credential／双 MCP session、独立 Runtime 身份连续但 profile/bearer 隔离，以及 32 身份并发进入已有回归；跨机器网络分区与长期大规模仍待验证。 |
| 外部系统副作用交付 | **明确缺口** | Runtime 事务不能给支付、第三方 API、文件等外部写入提供 exactly-once；尚无通用 outbox/delivery/compensation 合同。 |
| 通用 Request/Response/Confirmation 模型 | **未证明需要** | Commons v2 已用现有 State + Operation/Receipt + Event 承载 Post/Reply/Conversation/Message、私有授权与通知恢复；当前实验没有暴露必须提升为 Runtime 通用会话／确认原语的缺口。 |
| 真实多宿主 Agent / 长期运行 | **本机真实宿主已有短时证据；跨机器与 soak 仍缺** | OpenCode 与 Pi 已在独立进程中通过 MCP 自主发现并调用 Commons，OpenCode 新进程可恢复离线期间事实；实验为本机 loopback 且不承担身份鉴权证明。跨机器网络故障、长期运行和更广宿主矩阵仍是证据缺口，不是当前功能缺口。 |

## 证据使用规则

1. 核心单元／合同测试验证具体 Runtime 行为；它们是主要回归证据。
2. `tools/check_package.py` 中的仓库外 package integration probe 只验证打包、导入与若干公共能力组合边界；它不是参考世界。
3. 临时创建的外部项目只有在仍验证当前假设时才保留；失去当前用途就删除，不作为需求来源、架构来源或 Runtime 通用性的必要验收门槛。
4. 一条 L1/L2 主张没有直接测试并不等于它无效；应明确区分“设计要求”“实现状态”和“验证状态”。
5. 任何新测试消费者都必须先说明它想反驳哪条具体假设；不能先造一个大场景，再从它碰巧需要的功能反推核心必须增加抽象。
