# L5：技术架构

复核：2026-09-27。本文只回答 **哪些技术组件承担哪些责任，以及这些组件之间允许怎样依赖**。具体文件和当前数据库实现见 [IMPLEMENTATION](IMPLEMENTATION.md)；行为语义见 L4 合同。

## 责任分区

| 分区 | 责任 | 不应承担 |
| --- | --- | --- |
| Runtime Core | 事务执行、状态提交、回执、事件、活动、视图、流、timer、保留及身份运行时数据。 | HTTP/MCP 协议细节、具体世界玩法、模型推理。 |
| World SDK / Declarations | 让世界声明函数、状态、视图、流、timer、保留及表现事件。 | 启动服务器、保存 Agent 私人上下文。 |
| Gateway Contract | 将结构化请求映射到同一 Runtime 行为路径，并统一错误与 schema。 | 复制世界规则或自行判断业务完成。 |
| Transport Adapters | HTTP、MCP 等外部协议适配。 | 形成第二套业务逻辑。 |
| Identity / Onboarding Adapters | 当前身份发放、交换、鉴权和管理入口。 | 把当前 universe 范围实现定义成最终身份模型。 |
| Application / Workers | 组合 Web、HTTP、MCP、timer worker、保留维护和世界加载。 | 重写 Runtime 事务语义。 |
| World Packages | 消费 SDK，定义具体领域规则和内容。 | 反向成为 Runtime 的固定玩法。 |
| Client / Web | 读取获准数据、提交结构化操作、管理本地缓存和表现。 | 通过显示状态制造服务端事实。 |

## 依赖方向

核心原则不是“全仓库只能有一条单向 import 链”，而是 **内核与 SDK 不能反向依赖适配器、客户端或具体世界**。

允许的典型方向：

- World packages / clients → SDK or transport contract → Runtime Core
- HTTP / MCP adapters → identity + gateway → Runtime Core
- Application composition → adapters + workers + loader
- Runtime Core → runtime contracts / world declarations / storage helpers

组合入口可以依赖多个下游模块，这是它的职责；为了追求漂亮箭头而删除正常组合关系没有意义。

## 写入路径

1. Transport adapter 接收结构化请求。
2. 身份层解析当前调用主体与访问模式。
3. Gateway 将请求映射到 Runtime 函数调用，不解释自然语言业务含义。
4. Runtime 打开事务并执行 WorldDefinition 中的同步规则代码。
5. Runtime 校验状态变化、授权、schema、事件与可选能力命令。
6. 状态、回执、事件、流／timer 命令等按合同原子提交；失败则回滚。
7. adapter 只把已形成的结果编码回对应传输协议。

响应丢失发生在第 6 步之后时，客户端通过原 operation_id 和 receipt 恢复，不把“没收到 HTTP 响应”推断成“服务器一定没提交”。

## 读取与表现路径

读取入口在当前身份／公开策略下取得状态、函数结果、view、event 或 stream。客户端可以缓存、投影、排序和动画，但缓存与表现层没有直接写入权威业务状态的特殊通道。

Presentation Cue 是 Runtime/SDK 中的表现数据类型，不是另一套业务控制面。表现事件随业务操作提交，但动画结束不会再触发隐式业务提交。

## 信任边界

- 外部客户端、Agent 输入和自然语言 payload 都是不可信输入，必须通过声明 schema 与权限检查。
- 世界 Python 包当前是受信任代码，Runtime 不是恶意插件沙箱；这属于当前架构边界，不可包装成安全隔离保证。
- operator／部署管理入口与普通世界参与者入口分开；管理能力不能被前端参数冒充。
- SQLite 文件、备份和主机权限位于部署信任边界之外，应用层授权不能防止拥有主机文件权限的人直接修改数据库。
- 外部付款、网络、文件等副作用不属于 SQLite 事务原子性；需要独立交付／补偿合同时另行设计。

静态边界测试只检查部分 import 方向，它是架构回归提示，不是安全证明。当前具体模块映射见 [IMPLEMENTATION](IMPLEMENTATION.md)。

## EigenFlux 参考建议

**EF-01 / EF-04 / EF-06，L5 候选。** 参考其 [宿主接入分工](REFERENCE_EIGENFLUX_2026-09-27.md#ef-01-接入分段与身份接续)，沿用现有责任分区；状态见 [OPEN_DESIGN](OPEN_DESIGN.md#eigenflux-参考采纳清单)。

安装／连接检查、凭据存放、原生调度、取消和本地重连属于外部接入与宿主适配；身份验证、世界授权、提交与回执属于现有服务端路径；会话、关系和联系规则先由首个社交世界承担。客户端说明或 Skill 不能取代服务器权限检查。

优先验证一个真实可用宿主加现有 HTTP/MCP 的接入闭环。其他宿主按能力声明支持或缺失项；不能因某框架在机器上已安装就配置它，也不强制安装另一个框架来补调度能力。稳定本地连接配置可以参考 Agent Home，但不要求引入 EigenFlux CLI 或改变现有用户身份目标。

推送将来可作为通知适配，不成为唯一事实存储；先验证有界 wait 是否足够。身份／可见性等安全依赖失败不能静默视为允许，限流的失败行为需显式定合同。来源为第三方或官方的文本都不能绕过结构化授权。

这批参考不引入新服务、数据库、消息总线、搜索平台、后台 Agent 或 Runtime 内模型推理。未来确有发现／推荐需求时再评估外部可选组件；不得凭推荐结果直接写入关系、接受或完成事实。
