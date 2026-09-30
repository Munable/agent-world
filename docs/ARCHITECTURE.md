# L5：技术架构

复核：2026-09-30。本文定义 **技术责任、依赖、事务边界和信任边界**，但不把当前 Python 文件或 SQLite 表本身当成架构。当前代码映射见 [L6 当前实现](IMPLEMENTATION.md)。

## 组件责任

| 组件 | 必须承担 | 不应承担 |
| --- | --- | --- |
| Identity Core | 保存／验证参与身份与调用凭据的运行时事实，向执行核心提供已验证 Principal。 | 决定具体世界的业务资格。 |
| Runtime Execution Core | 执行 Query / Command、授权链、状态提交、Receipt、通知及可选运行时效果。 | 解析自然语言意图、运行模型推理、硬编码某个世界玩法。 |
| World Authoring Boundary | 让世界声明动作、状态约束、授权、投影、共享观察、持久时间等规则。 | 启动 Web 服务、管理用户私人模型上下文。 |
| Observation / Projection | 计算授权 View、读取通知／Stream、维护可重建同步位置。 | 成为第二份权威业务状态。 |
| Scheduler / Maintenance | 驱动已经提交的 Scheduled Effect 和有界数据保留工作。 | 代替用户或 Agent 产生新意图。 |
| Gateway | 把不同传输映射到同一个 Runtime 行为合同，规范 schema 与错误语义。 | 复制一套 transport-specific 世界规则。 |
| Transport Adapters | 处理 HTTP、MCP 或未来其他传输的编码、鉴权入口和生命周期。 | 自己决定业务是否完成。 |
| Composition Root | 选择 World Definition / World Instance，组装 adapters、workers、Runtime 和部署配置。 | 重写 Runtime 提交语义。 |
| World Package | 通过公开作者边界提供具体领域规则和内容。 | 反向成为 Runtime 固定玩法。 |
| Client / Agent Host | 管理用户授权、外部推理、连接、缓存和表现。 | 通过本地 UI 或模型总结制造服务器事实。 |

## 依赖方向

关键边界不是要求整个仓库只有一条 import 链，而是防止低层权威逻辑依赖上层适配与具体消费者。

- Runtime Execution Core 可以依赖共享合同／声明类型和内部能力模块，但不能依赖 HTTP/MCP/Web、具体 World Package 或测试项目。
- 可移植 World Package 应依赖公开作者边界，而不是 Runtime 私有模块、adapter 或管理 UI。
- Gateway / Transport Adapters 可以依赖 Runtime 和身份解析；Runtime 不反向依赖它们。
- Client 通过公开传输合同工作，不应需要数据库或 Runtime 私有对象。
- Composition Root 可以依赖多个组件，因为它的职责就是组装，而不是被误当成“纯核心”。

当前静态 import guard 只防止明显反向依赖；动态导入和运行时猴子补丁不在该测试能力范围内。

## 事务与一致性边界

Runtime 管理的 Command 写入必须汇聚到一个权威事务边界。普通外部 Command 与 Scheduled Effect 最终应复用同一提交语义，使状态、Receipt、通知、Stream 发布和后续定时命令不会分别成功。

授权 Query / View 使用一致读取边界观察世界事实；可重建 cache 在读取之后维护，不能成为提交前提。

并发执行可以有多个进程或 worker，但最终写入必须通过存储／fencing 协调，使旧租约、重复 Operation 和并发 timer 不产生两套已提交权威结果。

## 写入路径

1. Adapter 接收结构化请求和凭据。
2. Identity Core / auth boundary 解析 Principal 与访问模式。
3. Gateway 把请求映射为同一个 Query / Command 合同。
4. Runtime 取得所需事务／fencing 边界，校验动作、版本、授权与参数。
5. World Package 的声明规则产生候选世界变化与 Runtime 管理效果。
6. Runtime 校验候选结果并在提交前重查会影响资格的身份／租约。
7. 同一提交边界内的权威效果一起提交或一起回滚。
8. Adapter 只编码已形成的结果；响应丢失后的恢复仍由 Operation / Receipt 语义处理。

## 读取与表现路径

Query、View、Notification、Stream 都从当前权威事实和当前权限出发。Client 可以缓存、排序、投影和动画，但这些派生层没有特殊旁路去修改权威状态。

表现信号可以随已提交事实产生；动画是否播放完不进入 Runtime 的业务提交链。

## 信任边界

### 外部输入

客户端、Agent 输出、自然语言 payload、选择器和资源引用都是不可信输入，必须经过 schema、身份、授权和大小边界。

### 身份密钥与世界本地凭据

用户身份私钥是用户自己的根控制材料，Runtime／世界只接收公钥和针对新鲜 challenge 的签名，不保存身份私钥。公钥是跨世界稳定身份；世界本地档案、权限和 bearer Credential 都不是身份密钥本身。

世界完成持钥证明后，只在自己的 Identity Core 中保存 `公钥 → 本地 Principal/Profile` 映射，并签发本世界 scope 的调用 Credential。Bearer Credential 是秘密调用材料，应由宿主管理并在鉴权层使用；世界规则只需要得到已经解析的 Principal／访问信息，不应依赖把 bearer secret 或身份私钥复制到世界状态。

### World Package

**当前架构把被加载的 World Package 当成部署方信任的代码。** 业务层的 World Instance 隔离不等于敌对租户安全沙箱。

如果未来产品要在同一宿主中执行互不信任的第三方世界代码，就必须引入独立的进程／权限／存储或真正的沙箱边界；不能靠现有 scope 字段或 SQLite authorizer 宣称完成安全隔离。

### 主机与权威存储

拥有进程、数据库文件、备份或主机管理权限的一方处在应用层授权之外。应用权限不能防止主机管理员直接读取／修改底层数据。

### 外部系统

付款、网络写入、文件和其他外部副作用处在 Runtime 内部事务之外。需要跨系统一致性时必须建立明确的幂等、outbox、交付或补偿架构。

## 未来架构变化的判定

文件拆分、函数重命名或换数据库不一定改变 L5。只有组件责任、信任域、提交边界、依赖方向或进程／隔离模型改变时，才算技术架构变化。

## EigenFlux 参考建议

宿主安装／连接检查、稳定本地配置、取消与重连属于外部宿主／adapter 责任；身份验证、世界授权、提交、Receipt、Snapshot 与同步恢复继续走现有 Runtime 路径。参考 Agent Home 的价值在于稳定隔离身份／配置，而不是引入 EigenFlux CLI、Redis、Elasticsearch 或另一套 Agent 框架。

通知／推送只作为低延迟发现手段，权威恢复仍依赖当前状态、Receipt 和可验证的同步合同。以后若确有容量或跨主机需求，再依据观测到的瓶颈引入消息总线或新服务；不能因为参考项目采用微服务就提前复制其部署形态。
