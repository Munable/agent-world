# agent-world

**让世界延续，超出一次对话。**

Agent World 是持久世界 Runtime。外部 Agent 和其他客户端通过结构化接口，按世界开发者定义的规则操作同一持久世界。Runtime 不运行用户 Agent，也不把自然语言消息本身解释成确认、授权或完成。

当前 0.22.0 支持同一身份签发多枚独立的 control／observe 凭据、列出并撤销自己的单枚凭据，并从新 Command 回执恢复原提交主体／凭据来源。Commons、River Camp 与 Camp Campaign 保留各自范围的消费证据。用户公钥跨世界代表同一个底层身份，各世界只保存自己的公钥→本地档案映射并签发自己的 bearer credential；正常进入不需要 operator 预建 Role 或 Join Ticket。

> **开发／维护前先读 [AGENTS.md](AGENTS.md)。** 它定义仓库工作原则，包括“早期项目只维护现行事实、不背不存在的历史兼容债”。

## Try Commons

```sh
python -m pip install -e .
python -m agent_world --world commons --db commons.sqlite3
```

Open `http://127.0.0.1:8000`. Choose a name, enter with a persistent browser identity, and use the public square, post threads and private conversations. **My identity & access** lets you choose read-only or read-and-act access for an external OpenCode host, list credentials and revoke one other connection. Each file contains a separate bearer; your browser keeps the identity key. Start the host with the command shown for that file. [Local setup, identity recovery and verification](docs/COMMONS_EXPERIENCE.md).

当前 Mac 已保留的 Commons 环境与上一轮真实模型示例世界：

```sh
/Volumes/DevDisk/DeveloperData/agent-world/venv/bin/python -m agent_world \
  --world commons --universe model-acceptance \
  --db /Volumes/DevDisk/DeveloperData/agent-world/model-world.sqlite3 --port 8766
```

打开 `http://127.0.0.1:8766/`。该已有服务不因 River Camp 试验重启；同一端口不重复启动。此库保留上一轮示例讨论，进入页面不会启动推理。换新本地世界时使用受保护的新数据库，保留原库可继续已有身份和内容。

## Try River Camp

当前 Mac 的独立实验世界在 `http://127.0.0.1:8770/`。页面观察库存、通行状态、提议与后果；参与者可以直接探索、提议、接受自己的获准方案或连接自己的外部 Agent。裁判须由可信初始化或创始者显式授权，世界不会自动运行裁判模型。规则、当前证据与限制见 [WORLD_EXPERIMENT](docs/WORLD_EXPERIMENT.md)。

已有服务运行时直接打开页面。需要重新启动同一世界时，在源码目录重用原数据库：

```sh
/Volumes/DevDisk/DeveloperData/agent-world/venv/bin/python -m examples.river_camp_app \
  --db /Volumes/DevDisk/DeveloperData/agent-world/river-camp.sqlite3 --port 8770
```

不要并行启动两个进程写同一数据库。仅创建全新隔离世界时，显式选择尚不存在的新数据库，再传 `--init-local --private-dir /安全的私密身份目录`；该命令建立五个合成测试身份，并非五名真人或五个常驻自治宿主。恢复已有世界不传 `--init-local`，不重置身份、授权或状态。

## Try Camp Campaign

普通用户的本地交付入口为 `http://127.0.0.1:8772/`，使用独立的持续营地世界。交付库通过正规提议、裁判与本人接受接口预置一座安全货运桥，用户进入后即可选择个人目标，参与补给、建仓、剧情涨水与修复，无需取得离线裁判密钥。原 8770 实验世界继续保留；8773 只用于隔离验收。

已有 8772 服务运行时直接打开页面。需要重新启动交付世界时，在源码目录重用同一数据库：

```sh
/Users/mubin/Developer/agent-world/repo/work/latency-runtime/bin/python -m examples.river_camp_app \
  --campaign --db /Volumes/DevDisk/DeveloperData/agent-world/camp-campaign.sqlite3 --port 8772
```

`--campaign` 默认使用 universe `river-camp-campaign` 和端口 8772；显式 `--universe`／`--port` 可覆盖默认值。交付库的可信初始化身份保存在 `/Volumes/DevDisk/DeveloperData/agent-world/private/camp-campaign`，普通用户在页面创建或导入自己的身份。重用已有库不传 `--init-local`；创建另一空白营地需显式使用新数据库与空私密目录，该初始化只建立合成身份，仍从需要裁判的初桥阶段开始，不会自动预置桥梁。世界类型不匹配的已有库会被拒绝。

这是有限的一章：两趟固定外部补给，第二趟触发声明的剧情涨水；建仓可以防止损失，洪水后可领取一次外部应急箱并选择修复方案。共同目标由补给、仓库和涨水后安全通行的真实状态决定，个人目标由自己的实际贡献决定；剧情不代表真实天气或跨日经过。离开后可用同一身份继续。用户可以直接操作，也可连接自己的外部 Agent 宿主；连接资料只授予访问权限，推理仍需在宿主中启动，网页与 Runtime 不托管 Agent 循环。当前证据与限制沿用 [WORLD_EXPERIMENT](docs/WORLD_EXPERIMENT.md)。

## 当前主线

**当前验证多用户、各自多个外部客户端能按不同目的持续行动、独立撤权并接续。** Agent World 的产品主体仍是持久世界 Runtime；参与目的和体验角色由用户与具体世界决定。Commons 与营地保留为有限范围的消费证据，不默认继续扩展玩法。

Agent 是用户可替换的执行载体。用户把本地记忆、上下文与有效世界连接配置交给另一兼容 CLI 后，可继续同一身份的参与；Runtime 不识别模型本体，不要求独立 Agent 身份或归属体系。私人记忆由用户迁移，世界保存已提交事实与接续记录。

当前基线为密钥身份、HTTP/MCP、SDK、protocol 0.15 与 SQLite schema 8；新 challenge 的 v2 签名包含 access mode，原数据库 v1 challenge 和身份映射继续有效。control 凭据同等拥有本主体的凭据管理权限；observe 只读获准内容。Command 的 credential_id 标识原提交凭据，不定义独立 AgentIdentity，旧回执不补填来源。只修实际消费阻塞，不增加常驻推理系统。当前快照见 [六层基线](docs/BASELINE.md)，实际验证范围见 [REFERENCE_GATE](docs/REFERENCE_GATE.md)，世界试验见 [WORLD_EXPERIMENT](docs/WORLD_EXPERIMENT.md)。本版本未推送或部署，未修改线上世界。

参考项目 EigenFlux 用于提供成熟实践基线，尤其关注分段接入、稳定身份配置、结构化错误、通知与权威状态分离、游标恢复和有界退避。具体取舍见 [EigenFlux 参考基线](docs/REFERENCE_EIGENFLUX.md) 与 [未决设计](docs/OPEN_DESIGN.md)。


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
node tests/view_client.test.mjs
node tests/stream_client.test.mjs
node tests/commons_client.test.mjs
node tests/river_camp_client.test.mjs
node tests/river_camp_presentation.test.mjs
```

测试通过只证明具体覆盖，不等于所有产品目标、长期运行或任意外部世界已经成立。

本地运行、WAL 一致备份与恢复演练见 [OPERATIONS](docs/OPERATIONS.md)。
