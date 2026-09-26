# 分层基线与仓库入口

建立日期：2026-09-26；盘点起点：`f8a995467201ff26a51eb6dce644283de8e6d157`。
本文组织现有设计，不替代 [产品定义](../PRODUCT_POSITIONING.md)，不宣布所有目标已经实现。

## 四层，而不是六道关卡

| 层级 | 回答的问题 | 现行材料 | 不负责什么 |
| --- | --- | --- | --- |
| L1 目标与原则 | 做什么；不能牺牲什么；责任边界是什么？ | PRODUCT_POSITIONING | 不指定 JSON 字段、数据库、玩法或测试数量。 |
| L2 领域与信任模型 | 谁参与；什么是事实；谁持有什么权限？ | 本文的对象与责任表；产品定义的责任范围 | 不自行选定仲裁制度、身份验证方案或通用业务状态机。 |
| L3 行为与协议合同 | 什么输入可接受；状态如何改变；失败和恢复意味着什么？ | FOUNDATION 及各能力合同 | 不把当前实现缺陷写成允许行为，也不强制所有可选能力。 |
| L4 实现 | 当前代码、存储、传输、部署和界面如何兑现合同？ | agent_world、examples、独立世界仓库 | 不因库的限制、动画结束或测试便利改变上层含义。 |

测试、实验、运行观察和 UI 验收贯穿四层，不单列第 5、6 层。界面是实现的一部分，可以提出用户需求反馈，但不能创造服务端已提交的事实。
约束自上向下追溯，证据自下向上反馈。发现上层矛盾时允许明确修订；这不是禁止反馈的瀑布流程，也不是代码导入方向的机械镜像。
只维护能澄清责任的层次；同一份文件可以覆盖紧密相关的模型和合同，不要求每层各建目录、团队、服务或审批流程。

## L2：对象、事实与责任

| 概念 | 准确定义与边界 |
| --- | --- |
| 用户／底层身份与档案 | 用户持有身份是既定目标；跨世界统一验证尚未实现。当前 Role Core 与 universe 绑定令牌只是实现现状。 |
| 凭据／授权 | 凭据证明调用身份与模式；各世界另行判断行动资格。拥有同一身份不等于获得全部世界权限。 |
| 外部 Agent／客户端 | 在用户授权下推理、连接、调用和显示；不是 Runtime 内托管或自动唤醒的主体。 |
| WorldDefinition／universe | 前者声明一套世界规则；后者选择隔离的持久实例。规则包名、数据实例和角色身份不能混用。 |
| 调用／operation_id／业务事项 | 一次函数调用、传输重试身份、跨调用领域对象是不同概念。回执成功不自动代表整个业务事项完成。 |
| state／receipt／event／view | 分别是当前事实、提交结果、通知／保留记录、获准派生观察；读取游标与缓存不是业务完成标记。 |
| 自然语言字段 | 可存储消息或证据，但不构成授权或经过验证的结论。必须由已声明函数、鉴权和规则检查处理结构化操作。 |
| Activity／Timer／Stream／ViewSpec | 按需声明的能力，分别服务排他控制、定时规则、事件读取和投影；不是所有世界的必选组件。 |
| 第三方／群众公裁 | 需要外部参与及明确的结构化成立条件；具体机制未选定，见 OPEN_DESIGN，不默认为不存在或已完成。 |

## 现行文档的唯一职责

| 材料 | 职责与权威范围 |
| --- | --- |
| [PRODUCT_POSITIONING](../PRODUCT_POSITIONING.md) | L1 产品目标及固定前提；不复制另一份原则。 |
| [FOUNDATION](FOUNDATION.md) | 函数、SDK、身份接入、事务、回执、升级及当前偏差。 |
| [AGENT_INTERACTION](AGENT_INTERACTION.md) | 结构化交互、外部运行、回应与恢复的语义边界。 |
| [WORLD_DATA](WORLD_DATA.md) | 数据归属、授权视图、快照和缓存合同。 |
| [OBSERVATION_STREAMS](OBSERVATION_STREAMS.md) | 公开观察、声明频道、历史与现场游标。 |
| [DURABLE_TIME](DURABLE_TIME.md) | 可选定时事项的授权、执行、版本和恢复。 |
| [RETENTION_PRESENTATION](RETENTION_PRESENTATION.md) | 可选保留政策与表现信封，不规定领域玩法。 |
| [OPEN_DESIGN](OPEN_DESIGN.md) | 待决策、实现缺口、待验证假设；不自动转成强制路线图。 |
| [REFERENCE_GATE](REFERENCE_GATE.md) | 验收范围和证据要求；不用游戏完成度证明内核成立。 |
| [FOUNDATION_AUDIT](FOUNDATION_AUDIT.md)、Commons、World Zero | 带时间的历史证据／示例说明，不覆盖当前合同。 |
| [本次审计](REPOSITORY_AUDIT_2026-09-26.md) | 本次实际检查、修复和剩余限制；不是新的产品规格。 |

## 从目标到代码与证据

| 产品定义中的目标 | 合同与代码落点 | 验证入口／状态 |
| --- | --- | --- |
| 用户身份由用户持有并跨世界通用 | FOUNDATION 的身份边界；runtime_core、identity_auth、onboarding_app | 当前身份隔离／撤销测试只验证当前实现，不能证明跨部署统一身份。实现缺口仍在 OPEN_DESIGN。 |
| Agent 在外部、结构化操作 | AGENT_INTERACTION；transport_contracts → runtime_functions → world_context | tests/test_transport.py、tests/test_world_sdk.py；不把一次 HTTP 成功解释成业务确认。 |
| 接受的世界变化可持久接续 | FOUNDATION、WORLD_DATA；runtime_functions、runtime_journal、runtime_views | tests/test_foundation.py、tests/test_world_data.py、tests/test_managed_state_boundary.py；证据仅限执行过的场景。 |
| 内核不携带固定玩法 | WorldDefinition 和可选能力合同；world_sdk、加载器、独立 examples | 外部模块加载、非游戏 workflow、独立包验收；两个示例世界不是通用模板。 |

## 代码组织与依赖

`agent_world/runtime_*.py` 承担存储、执行、身份、事务、历史、活动、观察与时间；`world_sdk.py`、`world_context.py` 和相关声明类型是世界作者接口。它们不导入 HTTP/MCP/UI 或具体示例世界。
`transport_contracts.py` 统一结构化网关；`http_app.py`、`mcp_app.py` 是传输适配；`product_app.py`、`onboarding_app.py` 和 `web/` 是管理／客户端原型。
`application.py`、`universe_loader.py`、`builtin_worlds.py` 是组合与装载入口，可以连接上述模块。无需为了“单向依赖”删除正常的组合关系。
`*_universe.py` 与 `examples/` 是世界规则消费者；根目录的短同名模块保留兼容导入。`tests/`、根目录旧回归和 `tools/` 负责验证，不是产品能力。
本轮不搬动这些公共导入路径，不拆服务，不新建通用规则引擎，也不把内核约千行文件单凭长度判为必须重写。

独立世界 `Munable/agent-world-lantern-hollow` 和 `Munable/agent-world-ashen-vault` 各自拥有规则、前端、测试与固定核心依赖。升级它们需要验证实际组合；不能把其中的美术、战斗或社交设计提升为 Runtime 的通用要求。

## 变更与反馈的最小规则

一次发现先区分：实现缺陷、规格歧义、测试假设错误、运行环境问题，或真正的目标冲突。只有最后一种需要改变产品目标，不能为了测试变绿默认选择它。
需要改合同时，在同一次提交／PR 说明旧含义、新含义、依据和受影响消费者；普通实现修复只改相关代码、测试和受影响说明，不要求逐层走审批。
保留三个不同状态：已决定的目标、当前实现及已验证范围、尚未决定／尚未验证。不能通过改日期或“已确认”措辞把推测升级为决定。
重大已复现缺陷的回归不能在无关功能改动中删除；必须核对差异与测试集合，而不只看通过数量。
最小记录只需“变更层级、来源／原因、影响、实际证据与未决项”。直接写在提交／PR 或本次审计中，不再维护第二套决策登记系统。

## 分层复核结论

保留四种职责区分，是为解决本仓库目标、合同、实现和历史证据混用的问题，不声称这是适用于所有项目的唯一分层。
取消“测试是第五层、表现是第六层”和“发现问题只能向下修改”的限制；保留显式目标变更与证据追溯。UI 使用性问题可反馈到模型和合同，测试也可以发现原则不自洽。
判断是否值得增加抽象：能否明确责任、减少实际重复或隔离已存在的变化？只为了层级对称、场景数量或假想未来扩展，不增加约束。
