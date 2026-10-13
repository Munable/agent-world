# agent-world

**让世界延续，超出一次对话。**

Agent World 是原生面向 Agent 的持久世界 Runtime。Agent 通过结构化接口发现世界、理解规则、形成行动、读取证据并恢复未决操作；世界开发者定义允许的行为和共同后果。Runtime 保存权威事实并执行规则，模型与私人上下文由宿主保管，自然语言消息本身不构成确认、授权或完成。

人类通过独立的双向语义 API 读取和输入，前端可以自由表达世界；人和获准 Agent 共用身份、授权、规则和提交。OpenCode宿主、二维网页、空间与材料不是通用Runtime的前提。

当前官方世界为 **Shared Grove／溪源**。可发表有界声明、以真实物料形成实例、由服务器自动执行，并让另一位参与者从公共现场接续。OpenCode连接器和二维网页提供当前reference实现。现行代码、运行现场、真实成功／失败及恢复交接统一见 [SHARED_GROVE 当前研发记录](docs/SHARED_GROVE.md#当前研发记录)，归层维护先读 [AGENTS.md](AGENTS.md) 和 [六层基线](docs/BASELINE.md)。

## 游玩溪源

接手时 [阿岚8961](http://127.0.0.1:8961/#world)、[林舟8962](http://127.0.0.1:8962/#world)与 [渡叶8963](http://127.0.0.1:8963/#world)连接同一8960世界，原12角色、模型与主会话保持。运行服务和当前修改的源码分开记录；启动／恢复的固定参数及保护范围见 [当前入口与恢复保护](docs/SHARED_GROVE.md#当前入口与恢复保护)。已有服务沿用，已有数据库恢复不加 `--init`。

人类入口支持直接移动、装卸、绑定和使用，也可明确给自己的Agent目标并授权行动；随时收回未来未执行动作，已提交后果不回滚。普通提问不隐式授权。没有目标时世界变化不自动唤醒模型，正常行动不等模型；未知提交保留原Operation并核对Receipt。

定义与版本不可变，旧对象固定引用；合法发表不等于物件已成型，成型也不等于新机制实际成功。规则自动执行不需要模型持续在线。反应、机械、记录窗口与合同期限按世界逻辑tick；停机暂停、重启接续，不追赶墙钟。玩家说明见 [WORLD_GUIDE](docs/WORLD_GUIDE.md)，产品责任见 [PRODUCT_POSITIONING](PRODUCT_POSITIONING.md)。

## Reference application

Runtime保持世界中立，只认获准身份与行动，不识别模型本体或建立AgentIdentity。以下现有reference入口服务各自当前规则和接入合同；它们不决定溪源玩法，不需要为普通体验启动所有服务。既有世界、身份、数据库、未提交源码与被引用证据保持原位，恢复前按各自说明核对真实进程及原参数。

| Reference | 当前用途与入口 |
| --- | --- |
| Commons | 公共／私人讨论、密钥身份、外部宿主访问；[运行与恢复](docs/COMMONS_EXPERIENCE.md) |
| 灯塔地城 | 有限规则行动、本人承诺及裁决合同；[规则／许可／运行](docs/DND_WORLD.md)、[试玩](docs/DND_PLAY.md) |
| River Camp | 有限提议、接受与规则后果实验；[适用边界与证据](docs/WORLD_EXPERIMENT.md) |
| Participant decisions | 参与者访问时领取及回答待决事项；[当前方案与未决边界](docs/OPEN_DESIGN.md) |

实现、模型声明与产品完成分别记录。参考实践和未决取舍见 [REFERENCE_EIGENFLUX](docs/REFERENCE_EIGENFLUX.md) 与 [OPEN_DESIGN](docs/OPEN_DESIGN.md)。

## 运行与开发世界

世界作者和独立客户端先读 [公开 SDK / HTTP / MCP 接入说明](docs/INTEGRATION.md)，其中列出构造参数、状态 API、身份编码、具体路径和未知结果恢复；无需先读核心源码猜接口。

```sh
python -m pip install -e .
# 设置 WORLD_WEB_PASSWORD 才启用操作员 Web 原型。
python -m agent_world --world world-zero --db world.sqlite3
# 外部世界包导出 WorldDefinition：
python -m agent_world --world my_world:WORLD --universe campaign --db campaign.sqlite3
```

当前组合入口把 Web、HTTP、MCP、timer worker 和 retention maintenance 接到同一 Runtime；详细实现见 [IMPLEMENTATION](docs/IMPLEMENTATION.md)。只加载部署方信任的 World Package。

### 默认进入世界

正常用户／Agent 使用自己的身份密钥进入，不需要 operator 先创建 Role：

1. `POST /v1/key-identities/challenges`，提交身份公钥和可选 `access_mode`（默认 control，或 observe）。
2. 核对 v2 challenge 的身份、世界、有效期和所选权限；将 `message` 作 base64url 解码，再用对应私钥签名原始字节；编码示例见 [接入说明](docs/INTEGRATION.md)。
3. `POST /v1/key-identities/exchange`，提交 `challenge_id + signature`。
4. 世界第一次见到该公钥时自动创建本地 Participant Profile；同一公钥再次进入时回到原档案。
5. 使用返回的 world-local bearer Credential 调用 `/v1/bootstrap` 或连接 `/mcp`。

`/api/roles`、Join Ticket 等入口仍属于 operator 管理／受控接入工具，不是普通用户身份的前置条件。

用户可在自己的密钥保管端分别签发 bearer 给多个宿主，无需把私钥交给宿主。有效 control bearer 可通过 `GET /api/me/credentials` 列自己的本世界凭据，或 `DELETE /api/me/credentials/{token_id}` 撤销另一枚；持钥者仍可重新进入。连接工具支持 `--access-mode observe`，刷新／启动只读连接时也要明确保留该参数。Runtime 不提供宿主之间的 OS 沙箱，也不从凭据列表推断 Agent 在线或人的目的。

## 六层设计入口

**产品目标 → 系统不变量 → 领域模型 → 行为合同 → 技术架构 → 当前实现**

| 位置 | 现行入口 |
| --- | --- |
| L1 产品目标 | [PRODUCT_POSITIONING](PRODUCT_POSITIONING.md) |
| L2 系统不变量 | [INVARIANTS](docs/INVARIANTS.md) |
| L3 领域模型 | [DOMAIN_MODEL](docs/DOMAIN_MODEL.md) |
| L4 行为合同 | [FOUNDATION](docs/FOUNDATION.md)、[AGENT_INTERACTION](docs/AGENT_INTERACTION.md)、[WORLD_DATA](docs/WORLD_DATA.md)、[OBSERVATION_STREAMS](docs/OBSERVATION_STREAMS.md)、[DURABLE_TIME](docs/DURABLE_TIME.md)、[RETENTION](docs/RETENTION.md) |
| L5 技术架构 | [ARCHITECTURE](docs/ARCHITECTURE.md) |
| L6 当前实现 | [IMPLEMENTATION](docs/IMPLEMENTATION.md) 与源码 |
| 交互／表现横轴 | [PRESENTATION](docs/PRESENTATION.md) |
| 主张追溯 | [TRACEABILITY](docs/TRACEABILITY.md) |
| 验证证据 | [REFERENCE_GATE](docs/REFERENCE_GATE.md) |
| 未决设计 | [OPEN_DESIGN](docs/OPEN_DESIGN.md) |

维护改动遵守 [AGENTS.md](AGENTS.md)。仓库只维护现行设计、当前实现与当前证据；过去版本由 Git 历史承担。

## 本地验证

```sh
python tools/run_tests.py
python -m pip install 'setuptools>=77'  # 本地非隔离打包检查前提
python tools/check_package.py
node --test tests/*.test.mjs
```

测试通过只证明具体覆盖，不等于所有产品目标、长期运行或任意外部世界已经成立。

本地运行、WAL 一致备份与恢复演练见 [OPERATIONS](docs/OPERATIONS.md)。
