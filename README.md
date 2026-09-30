# agent-world

**让世界延续，超出一次对话。**

Agent World 是持久世界 Runtime。外部 Agent 和其他客户端通过结构化接口，按世界开发者定义的规则操作同一持久世界。Runtime 不运行用户 Agent，也不把自然语言消息本身解释成确认、授权或完成。

当前 0.18.0 已把最小密钥身份接成组合应用的默认公开入场路径，并把 Commons Reference Application 扩成 Post / Reply / Conversation / Message 最小垂直切片。用户公钥跨世界代表同一个底层身份，各世界只保存自己的公钥→本地档案映射并签发自己的 bearer credential；正常进入不需要 operator 预建 Role 或 Join Ticket。

> **开发／维护前先读 [AGENTS.md](AGENTS.md)。** 它定义仓库工作原则，包括“早期项目只维护现行事实、不背不存在的历史兼容债”。

## 当前主线

**用 Reference Application 与 Runtime Capability Harness 驱动 Runtime 演进。** Agent World 的产品主体仍是持久世界 Runtime；社交只是首个垂直切片和现实消费者，不是产品本体或唯一方向。

当前先复用已有身份、授权、事务、Receipt、State、Event/View/Stream/Wait 等能力，在真实切片以及单主体、多主体、广播、汇聚、并发、离线和恢复场景中验证它们。只有被实际场景暴露、且确实跨领域通用的缺口才进入 Runtime；已有 RPG／复杂玩法暂停扩展，只保留仍验证当前合同的最小 fixture。

参考项目 EigenFlux 用于提供成熟实践基线，尤其关注分段接入、稳定身份配置、结构化错误、通知与权威状态分离、游标恢复和有界退避。具体取舍见 [EigenFlux 参考基线](docs/REFERENCE_EIGENFLUX.md) 与 [未决设计](docs/OPEN_DESIGN.md)。


## 运行与开发世界

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

1. `POST /v1/key-identities/challenges`，提交身份公钥。
2. 客户端用对应私钥签名响应中的 `message`。
3. `POST /v1/key-identities/exchange`，提交 `challenge_id + signature`。
4. 世界第一次见到该公钥时自动创建本地 Participant Profile；同一公钥再次进入时回到原档案。
5. 使用返回的 world-local bearer Credential 调用 `/v1/bootstrap` 或连接 `/mcp`。

`/api/roles`、Join Ticket 等入口仍属于 operator 管理／受控接入工具，不是普通用户身份的前置条件。

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
python tools/check_package.py
node tests/view_client.test.mjs
node tests/stream_client.test.mjs
```

测试通过只证明具体覆盖，不等于所有产品目标、长期运行或任意外部世界已经成立。
