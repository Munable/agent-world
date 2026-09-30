# L3：领域模型

复核：2026-09-30。本文只定义 **系统中的语义概念与关系**。这里不使用数据库表、Python 类、HTTP 路径或某个测试世界来定义概念；当前代码映射见 [L6 当前实现](IMPLEMENTATION.md)。

## 主体与身份

| 概念 | 含义 |
| --- | --- |
| User Identity | 用户持有的一把身份密钥所定义的底层身份。公钥相同即同一身份；公钥不同即不同身份。没有私钥就不能证明控制该身份。 |
| Principal | 世界可以把行动归因到的主体，包括用户参与者或明确的系统主体。 |
| Participant Profile | 某个 World Instance 为一个 User Identity 建立的本地参与档案。它由该世界保存，不随身份密钥自动跨世界搬运；不同世界可以为同一公钥保存不同档案。 |
| Credential | 世界在完成身份密钥持有证明后签发的本地调用凭据，用来让后续调用代表该世界中的 Principal；它不是身份密钥，也不是跨世界凭据。 |
| External Agent / Client | 在 Runtime 外部运行、代表用户发起调用和呈现结果的程序。它不是独立权威来源，除非持有相应主体的授权凭据。 |
| System Actor | 由 Runtime 按已声明机制执行的非用户主体，例如持久定时事项。其行为必须明确标记为系统来源。 |

## 世界与规则

| 概念 | 含义 |
| --- | --- |
| World Definition | 一套可版本化的世界规则、结构化行动、状态约束及可选能力声明。 |
| World Instance | 某套 World Definition 的一个隔离、持久运行实例；同一规则可以有多个实例。 |
| Domain Object | 世界开发者定义的业务对象，例如请求、关系、物品、案件或任务；Runtime 不预设统一对象类型。 |
| Query | 只读取获准事实、不会产生持久业务效果的声明式读取。 |
| Command | 可能改变世界事实的声明式行动，必须经过结构、身份、授权和当前状态检查。 |
| State Fact | 当前由世界规则认可的权威事实。 |

## 提交与恢复

| 概念 | 含义 |
| --- | --- |
| Operation | 一次可重试的 Command 意图及其稳定重试身份。Operation 不是跨多次调用的业务对象。 |
| Commit | Runtime 接受一次世界变化并使同一提交边界内的效果一起成立的原子边界。 |
| Receipt | 某个 Operation 已提交结果的持久证据；它只证明该 Operation，不证明更大的业务事项完成。 |

## 观察与派生数据

| 概念 | 含义 |
| --- | --- |
| Notification Event | 因已提交事实产生的定向观察记录。被投递或读取不自动代表接收者确认。 |
| Shared Stream | 世界声明的有序共享观察通道；它不是默认群聊，也不自动产生业务 ACK。 |
| View | 根据当前主体、权限和世界事实计算出的授权投影；它是派生数据，不是第二份权威状态。 |
| Sync Position | 用来继续读取或同步派生数据的位置标识；它不是确认、处理完成或业务终态。 |

## 可选协调概念

| 概念 | 含义 |
| --- | --- |
| Control Lease | 某些需要排他控制的过程使用的有限租约与 fencing；不是所有对话或角色的全局 busy。 |
| Scheduled Effect | 已提交、将在将来由 System Actor 按世界规则执行的持久事项；它不是后台 Agent。 |

## 核心关系

- 一个 User Identity 可以在多个 World Instance 中分别映射到各自的 Participant Profile；同一公钥再次进入同一世界时回到原档案，新公钥则创建新的本地档案。每个世界自己签发本地 Credential，世界自己的角色属性、关系和资产仍属于该 World Instance 的 Domain Object / State Fact。
- Credential 的作用是把一次调用可靠归因给某个 Principal；External Agent / Client 只是使用用户授权代表 Principal 发起调用，不因此成为新的权威主体。
- 一个 World Definition 可以产生多个相互隔离的 World Instance；规则身份和持久实例身份不能混为一谈。
- Principal 在某个 World Instance 中执行 Query 或 Command。Command 由 Operation 表示可重试意图，并在成功时形成 Commit 与 Receipt。
- Commit 可以改变 State Fact，并产生 Notification Event、Shared Stream 发布或 Scheduled Effect；这些派生结果仍必须属于同一个已声明提交语义。
- View 从当前 State Fact 和当前权限派生；Sync Position 只帮助继续读取 View / Stream / Notification，不进入业务完成语义。
- Control Lease 只在特定 Command 需要排他控制时参与 fencing，不是 Principal 的永久状态。

## 必须保持的非等价关系

- World Definition ≠ World Instance。
- User Identity ≠ Participant Profile ≠ Credential。
- External Agent / Client ≠ Principal；客户端只是代表某个 Principal 调用。
- Operation ≠ Domain Object。
- 传输响应 ≠ Commit ≠ Receipt ≠ 业务事项完成。
- Delivered ≠ Read ≠ Responded ≠ Confirmed ≠ Executed ≠ Completed。具体世界可以明确省略某些阶段，但 Runtime 不能擅自把它们合并。
- Notification / Stream / View / Sync Position / Presentation ≠ Authoritative State。
- Scheduled Effect ≠ Agent，也不代表用户在执行时重新作出了决定。

第三方／群众公裁若最终成为通用能力，再根据实际机制决定 Case、Evidence、Reviewer、Decision 等是否进入正式领域模型；在 [OPEN_DESIGN](OPEN_DESIGN.md) 定稿前不预占概念。

## EigenFlux 参考建议

这些概念用于首个社交 Reference Application 的 L3 设计验证，**不成为 Runtime 内置领域对象**：公开内容／回应、私有会话／消息、关系请求／关系、联系偏好／屏蔽、待处理事项／通知。候选的价值在于逼出身份归因、对象引用、授权投影、未决事项持久化和恢复语义；若以后其他领域出现同样需求，再评估是否抽取更通用的 Request / Response / Pending Item 等模型。

参考 EigenFlux 时保留一个关键区分：通知只是发现入口，关系请求或会话本身是业务事实；清除通知不能结束 pending 事项。交叉申请、关闭、撤回、屏蔽后的关系变化等仍由具体 Reference Application 定义，不能直接变成 Runtime 规则。
