# 六层关键主张追溯

复核：2026-09-27。这里不是新的规格层，而是把关键 L1/L2 主张追到 L3-L6 与现有证据。状态只描述当前仓库，不是质量评分。

| 主张 | 下层落点 | 当前状态 | 核心证据／限制 |
| --- | --- | --- | --- |
| G1 世界持续存在 | State Fact、Operation、Commit、Receipt；L4 提交／恢复合同；Runtime journal/state | **已实现核心机制；长期证据有限** | `tests/test_foundation.py`、`tests/test_world_data.py`、`tests/test_world_sdk.py` 覆盖提交、恢复、重启与升级；没有真实长期 soak 证明。 |
| G2 用户带身份跨世界 | User Identity、Participant Profile、Credential；身份合同 | **部分实现** | 当前 Role/Profile 可在同一 Runtime 数据库复用，但调用 token 仍 scope 到单个 world instance；用户持有的跨世界可验证凭证／等价证明及跨独立部署信任尚未实现。 |
| G3 Agent 在 Runtime 外部 | External Agent / Client、System Actor；L5 外部推理边界 | **架构上成立** | Runtime 没有模型推理器；timer 以系统来源执行。现有测试能证明 timer 不保存用户 token 等局部性质，但不能证明所有未来宿主都正确。 |
| G4 世界开发者拥有领域规则 | World Definition、Domain Object；L4 声明式行动／状态合同；SDK | **已形成核心机制** | `tests/test_world_sdk.py` 覆盖外部模块、非游戏规则、版本与 schema；这证明接口可承载这些夹具，不证明任意领域天然适配。 |
| G5 多类型世界共用 Runtime | 领域中立不变量、SDK／adapter 架构 | **设计与架构成立，通用性不可由有限样本证明** | 仓库内最小示例和打包夹具只验证接口边界；任何临时游戏／社交测试都不能升级为产品证明。 |
| I1 声明机制产生权威事实 | Command、State Fact、Commit；L4 结构／授权／规则链 | **已实现底座** | `tests/test_foundation.py`、`tests/test_managed_state_boundary.py`、transport tests 覆盖 schema、授权、回滚；Runtime 不解释文本语义。 |
| I2 不伪造他人意愿 | Principal、Credential、Command；世界业务状态机 | **Runtime 提供归因底座，具体确认仍由世界定义** | 调用主体来自鉴权而非业务参数；没有通用 Confirm 类型，因此 Runtime 也不能替所有世界证明业务确认正确。 |
| I3 身份与领域授权分离 | Credential + command/state authorization | **已实现当前机制** | Function authorization、state authorization、observe/control 等有回归；跨世界身份本身仍未完成。 |
| I4 Runtime 不冒充用户／Agent | External Agent / Client、System Actor、Scheduled Effect | **已实现架构边界** | `tests/test_timers.py` 覆盖定时事项离线执行与不保存用户 token；系统行为与用户行为来源区分。 |
| I5 提交事实独立于会话 | Commit、Receipt、authoritative state | **已实现核心机制** | receipt 恢复、进程重启、状态持久化测试存在；网络外部副作用不在该保证内。 |
| I6 同一 Runtime 提交无半成品 | L4 原子提交；事务执行核心 | **有强回归覆盖** | `test_write_cannot_commit_partially`、output/schema failure、timer retry/rollback、history rollback 等。 |
| I7 未知结果不靠猜测 | Operation + Receipt + structured recovery error | **已实现核心写操作语义** | receipt 恢复与同 ID replay 测试存在；`test_error_contract.py`、`test_structured_error_transport.py` 验证 HTTP/MCP 保留 recovery/retry/details。Capability Harness 还验证重启后 replay；identity token rotate 丢响应仍是已知缺口。 |
| I8 派生观察不创造事实 | View、Stream、Sync Position、Presentation 横轴 | **已实现主要边界** | view/stream/client tests 覆盖缓存、乱序、撤权、retention gap；具体 UI 仍需按世界正确命名业务状态。 |
| I9 world instance 默认隔离 | World Instance、instance-scoped state/action | **支持接口层已实现；不是敌对代码安全沙箱** | `test_world_instances_are_isolated`、cross-universe write 回归存在。受信任 Python world code 与当前 raw connection escape hatch 不提供恶意租户机密隔离。 |
| I10 Runtime 领域中立 | Domain Object 由世界定义；核心不含固定玩法 | **当前代码结构基本符合** | architecture boundary test 防止 core 反向依赖示例／adapter；有限示例只能证明未立即耦合，不能证明未来不会退化。 |

## 证据使用规则

1. 核心单元／合同测试验证具体 Runtime 行为；它们是主要回归证据。
2. `tools/check_package.py` 中的仓库外 package integration probe 只验证打包、导入与若干公共能力组合边界；它不是参考世界。
3. 临时创建的外部项目只有在仍验证当前假设时才保留；失去当前用途就删除，不作为需求来源、架构来源或 Runtime 通用性的必要验收门槛。
4. 一条 L1/L2 主张没有直接测试并不等于它无效；应明确区分“设计要求”“实现状态”和“验证状态”。
5. 任何新测试消费者都必须先说明它想反驳哪条具体假设；不能先造一个大场景，再从它碰巧需要的功能反推核心必须增加抽象。
