# L3：领域模型

复核：2026-09-27。本文只回答 **系统中有哪些概念，以及这些概念彼此是什么关系**。它不指定 HTTP 路径、Python 类名、数据库表或具体公裁规则。

## 身份与参与者

| 概念 | 含义 |
| --- | --- |
| User Identity | 用户持有的底层身份概念，产品目标要求可跨世界使用；当前跨部署统一验证尚未实现。 |
| Credential | 向 Runtime 证明调用身份与访问模式的凭据。凭据不是世界内全部行动权限的总开关。 |
| Role | 当前单个 Runtime 数据库中的稳定参与身份／显示档案；进入各 universe 的 presence 与凭据另行记录。它比 universe-scoped token 更稳定，但仍不能冒充跨独立部署的最终 User Identity。 |
| External Agent / Client | 在 Runtime 外部、获得用户授权后发起结构化调用并呈现结果的程序或宿主。 |
| System Actor | Runtime 按已声明机制执行的系统身份，例如 timer；它不是用户 Agent，也不能借此冒充用户意愿。 |

## 世界与规则

| 概念 | 含义 |
| --- | --- |
| WorldDefinition | 一套可安装的世界规则、函数、状态声明及可选能力定义。 |
| Universe | 某个 WorldDefinition 的持久实例与隔离数据范围。多个 universe 可以使用同一规则定义。 |
| World Function | 世界声明的可调用结构化操作或读取入口。函数是否允许执行由身份、模式、参数与世界规则共同决定。 |
| State | 当前权威世界事实。状态键和值属于具体世界的领域设计，而不是 Runtime 的固定玩法。 |
| Business Object | 世界自己定义的跨调用对象，例如请求、交易、案件、任务；Runtime 不假定统一对象类型或完成状态机。 |

## 操作、事实与观察

| 概念 | 含义 |
| --- | --- |
| Operation | 一次结构化写意图及其重试身份。operation_id 用于同一意图的传输恢复，不等于业务对象 ID。 |
| Receipt | 某次写操作已提交结果的持久记录。它证明该操作结果，不证明更大的业务事项已经结束。 |
| Event | 已提交的定向通知／观察记录；被投递或读取不自动产生接收者的确认事实。 |
| Stream | 世界声明的共享事件频道；频道可见性和保留由世界规则定义，不等于通用群聊系统。 |
| View | 依据当前身份与规则计算出的授权投影。它是观察面，不是另一份权威状态。 |
| Cursor / Checkpoint | 读取或同步位置。它表示客户端观察进度，不是业务 ACK、确认或完成标记。 |

## 可选控制与时间

| 概念 | 含义 |
| --- | --- |
| Activity | 需要排他控制时使用的有限租约与 fencing 对象；不是全局“角色忙碌”状态。 |
| Timer | 已提交的持久定时事项，由 Runtime 按世界声明执行；不是后台 Agent 或模型会话。 |
| Retention Policy | 对特定历史／通知数据的清理规则；不应删除仍承担权威、去重或恢复责任的数据。 |
| Presentation Cue | 表达已提交逻辑动作的表现信封；用于客户端展示，不拥有创建业务事实的权限。 |

## 必须保持的非等价关系
User Identity ≠ Role ≠ Credential；Operation ≠ Business Object；Receipt ≠ Business Completion；Event Delivered ≠ Read ≠ Responded ≠ Confirmed ≠ Executed ≠ Completed；Cursor ≠ ACK；View / Cache / Presentation ≠ Authoritative State；Timer ≠ Agent。

第三方／群众公裁若落地，应在这一层增加必要的领域概念（例如 Case、Evidence、Decision、Reviewer 等），但只有在 [OPEN_DESIGN](OPEN_DESIGN.md) 的机制确定后才正式纳入，不能提前把候选设计伪装成既定模型。
