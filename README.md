# agent-world

**让世界延续，超出一次对话。**

Agent World 是持久世界 Runtime。外部 Agent 和其他客户端通过结构化接口，按世界开发者定义的规则操作同一持久世界。Runtime 不运行用户 Agent，也不把自然语言消息本身解释成确认、授权或完成。

当前正式产品主线是 **Shared Grove／溪源**：人类在二维地图亲自采集、建设、装水、养护和处理物件；每个用户连接自己的 OpenCode，作为个人解释者及有限公共内容贡献者。0.24.0 是本地当前最佳已验证切片，具有真实材料代价、跨玩家后果、普通解释复验、恢复与实际 Chrome 交互证据；0.23 保留为历史参考。服务器保存材料与共享后果，不运行用户模型。唯一当前记录见 [SHARED_GROVE](docs/SHARED_GROVE.md#当前研发记录)。

> **开发／维护前先读 [AGENTS.md](AGENTS.md)。** Runtime仍是世界中立的底层；本世界的空间、材质和入口要求不自动成为所有世界规则。

## 游玩溪源

当前可打开 [溪源 8891](http://127.0.0.1:8891/)，连接 8894 的 0.24 研究世界。旧 8890／0.23 世界保留为历史基线。需要重新启动已有研究世界时，先进入源码目录，重用当前库且不加 `--init`：

```sh
cd /Users/mubin/Developer/agent-world/shared-grove
/Users/mubin/Developer/agent-world/repo/work/latency-runtime/bin/python -m agent_world.grove_app \
  --db /Volumes/DevDisk/DeveloperData/agent-world/test-work/shared-grove/research/world.sqlite3 \
  --port 8894
```

在另一个终端、同一源码目录启动自己的连接器：

```sh
cd /Users/mubin/Developer/agent-world/shared-grove
/Users/mubin/Developer/agent-world/repo/work/latency-runtime/bin/python -m agent_world.grove_connector \
  --world-url http://127.0.0.1:8894 --port 8891 \
  --storage /Volumes/DevDisk/DeveloperData/agent-world/private/shared-grove/research/connector-a
```

新建世界时使用不同的私有父目录和尚不存在的数据库，再加 `--init`；不初始化当前库。OpenCode 配置和验收范围见 [SHARED_GROVE](docs/SHARED_GROVE.md)。每个人运行自己的连接器，连接同一个世界；模型凭据与私人会话留在本机，普通移动与操作由服务器直接处理，不等待模型。第一次进入须完成实际 MCP 世界读取与助手回应，下载配置不代表接入成功。

首次实际入场后，Agent 因新探索或附近真实材料行动自动有限参与，默认最多 4 个 OpenCode message 请求，失败与纠正计入，可以停止；无变化不推理，正常行动不等待模型。显式创作保留在高级入口，普通提问只解释。实物贡献消耗真实公共库存，符合规则才自动发表并读回，不要求用户审提案。

**0.24 本轮本地验证通过。** 第 12 次失实保留，第 13 次同模型复问正确；实际用授权 14 次中的 13 次，自动参与均关闭。最终源码 535 项通过（107.444 秒），4 份 Node 测试文件、语法／diff、实际安装的 0.24.0 wheel 通过；最新备份独立还原与正常桌面接续通过。Git 内首次 535 和修复前旧 533 的失败仍保留，不作为最终结果。冷启动仍偶发超时，真人乐趣、移动端、跨机器、负载及炉的实际玩家价值未验证；原型继续受保护，见 [FRONTIER_WORLD](docs/FRONTIER_WORLD.md)。

## Earlier examples and scoped evidence

以下入口保留已有世界与协议实验，不是当前官方世界的新玩家起点。已有数据库与运行中的服务继续保留，普通体验不需要启动所有示例。

### Participant-supplied decisions

此前隔离实验验证参与者的 Agent 能否在访问时领取并回答另一位参与者的待决事项。它不设置全局 GM，不运行服务器模型、后台轮询、超时或自动改派；只提供派发、有限报价、本人接受和规则结算。其短流程证据不等于自由世界完整验收。

```sh
python -m examples.participant_app --init-local \
  --db /安全的私密数据目录/participants.sqlite3 \
  --private-dir /Git之外的空私密身份目录 --port 8802
```

初始化只建立 A/B/C 三个合成密钥身份。通过正式 HTTP/MCP 接入，先读取 `participant.handbook` 和函数 schema；`participant.visit` 是会领取附加题的写操作，必须使用稳定 Operation。历史 `received_intents` 不等于本次领题；只有 `additional_task` 表示当前待答事项。等待先取 bootstrap 通知锚点、再 look；通知到达后仍读取当前事实，超时只结束读取。重启已有库时去掉初始化参数。当前方案、实际验证和未验证边界见 [OPEN_DESIGN](docs/OPEN_DESIGN.md)。

### Existing fifth-edition lighthouse

既有有限示例：**旧灯塔地城**。采用 SRD 5.1 的两种一级角色与原创六房间场景。网页可以直接探索、交谈、战斗，也可下载连接资料让自己的 CLI 使用同一身份行动。常规玩法不依赖外部 AI 裁决者在线；额外自由意图先展示有限裁决与风险，再由本人承诺。它保留已完成的规则／恢复证据，不作为当前官方世界的新玩家起点。

```sh
python -m pip install -e .
python -m examples.dnd_app --init-local \
  --db /安全的私密数据目录/lighthouse.sqlite3 \
  --private-dir /Git之外的空私密身份目录 --port 8796
```

数据父目录权限须为 `0700`。首次初始化仅创建四个本地合成测试身份，其中 GM 具备有限裁决权限；网页普通用户用自己的密钥进入。已有库重新启动时去掉 `--init-local` 和 `--private-dir`，不得重复启动进程写同一数据库。页面打开 `http://127.0.0.1:8796/`。第一次玩看 [试玩指南](docs/DND_PLAY.md)；维护者的完整规则、许可、启动与实际证据见 [DND_WORLD](docs/DND_WORLD.md)，Agent 按需阅读机制见 [WORLD_GUIDE](docs/WORLD_GUIDE.md)。

### Commons

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

### River Camp

既有独立实验入口为 `http://127.0.0.1:8770/`。页面观察库存、通行状态、提议与后果；参与者可以直接探索、提议、接受自己的获准方案或连接自己的外部 Agent。裁判须由可信初始化或创始者显式授权，世界不会自动运行裁判模型。该实验的历史证据与限制见 [WORLD_EXPERIMENT](docs/WORLD_EXPERIMENT.md)。

已有服务运行时直接打开页面。需要重新启动同一世界时，在源码目录重用原数据库：

```sh
/Volumes/DevDisk/DeveloperData/agent-world/venv/bin/python -m examples.river_camp_app \
  --db /Volumes/DevDisk/DeveloperData/agent-world/river-camp.sqlite3 --port 8770
```

不要并行启动两个进程写同一数据库。仅创建全新隔离世界时，显式选择尚不存在的新数据库，再传 `--init-local --private-dir /安全的私密身份目录`；该命令建立五个合成测试身份，并非五名真人或五个常驻自治宿主。恢复已有世界不传 `--init-local`，不重置身份、授权或状态。

### Camp Campaign

上一轮有限营地入口为 `http://127.0.0.1:8772/`。原交付库通过正规提议、裁判与本人接受接口预置一座安全货运桥，可继续已有补给、建仓、剧情涨水与修复状态。这些固定章节不作为当前官方世界的设计目标；原 8770 实验世界继续保留，8773 是此前隔离验收入口。

已有 8772 服务运行时直接打开页面。需要重新启动交付世界时，在源码目录重用同一数据库：

```sh
/Users/mubin/Developer/agent-world/repo/work/latency-runtime/bin/python -m examples.river_camp_app \
  --campaign --db /Volumes/DevDisk/DeveloperData/agent-world/camp-campaign.sqlite3 --port 8772
```

`--campaign` 默认使用 universe `river-camp-campaign` 和端口 8772；显式 `--universe`／`--port` 可覆盖默认值。交付库的可信初始化身份保存在 `/Volumes/DevDisk/DeveloperData/agent-world/private/camp-campaign`，普通用户在页面创建或导入自己的身份。重用已有库不传 `--init-local`；创建另一空白营地需显式使用新数据库与空私密目录，该初始化只建立合成身份，仍从需要裁判的初桥阶段开始，不会自动预置桥梁。世界类型不匹配的已有库会被拒绝。

这是有限的一章：两趟固定外部补给，第二趟触发声明的剧情涨水；建仓可以防止损失，洪水后可领取一次外部应急箱并选择修复方案。共同目标由补给、仓库和涨水后安全通行的真实状态决定，个人目标由自己的实际贡献决定；剧情不代表真实天气或跨日经过。离开后可用同一身份继续。用户可以直接操作，也可连接自己的外部 Agent 宿主；连接资料只授予访问权限，推理仍需在宿主中启动，网页与 Runtime 不托管 Agent 循环。当前证据与限制沿用 [WORLD_EXPERIMENT](docs/WORLD_EXPERIMENT.md)。

## 当前主线

**Shared Grove／溪源当前为 0.24 本地最佳已验证切片。** 玩家用键盘和鼠标亲自行动，自己的 OpenCode 解释现场与有限贡献内容，中心服务器统一结算材料和共享后果。没有预置章节、共同目标、固定 GM 或后台模型。0.23 保留为历史参考；本轮通过不等于整体理想产品或真人乐趣已完成，当前证据与边界见 [SHARED_GROVE](docs/SHARED_GROVE.md)。

Runtime 保持世界中立：只认密钥身份，不识别模型本体，也不建立 AgentIdentity。私人记忆由用户与宿主保管，世界保存已提交事实与接续记录；本产品的 OpenCode 入口、二维地图和材料规则不成为所有世界的前提。

体验按实际沟通／交互轮次、功能发现、探索路径和共享后果的深度推进，玩家决定下一步，Agent 不代玩。雨、湿度与燃烧保留一秒逻辑步的必要计时，批量扫描不能延长规则时长；运行天数、日活和每日任务不衡量产品完成。使用说明见 [交互与表现](docs/PRESENTATION.md)，产品边界见 [PRODUCT_POSITIONING](PRODUCT_POSITIONING.md)。

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
node tests/grove_client.test.mjs
node tests/river_camp_client.test.mjs
node tests/river_camp_presentation.test.mjs
```

测试通过只证明具体覆盖，不等于所有产品目标、长期运行或任意外部世界已经成立。

本地运行、WAL 一致备份与恢复演练见 [OPERATIONS](docs/OPERATIONS.md)。
