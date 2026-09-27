# agent-world

**让世界延续，超出一次对话。**

Agent World 是持久世界 Runtime。外部 Agent 和其他客户端通过结构化接口，按世界开发者定义的规则操作同一持久世界。Runtime 不运行用户 Agent，也不把自然语言消息本身解释成确认、授权或完成。

当前 0.14.0 的 credential 仍绑定单个 universe；跨独立部署的用户身份验证尚未实现。产品目标与当前实现必须分开阅读。

## 运行与开发世界

```sh
python -m pip install -e .
# 设置 WORLD_WEB_PASSWORD 才启用操作员 Web 原型。
python -m agent_world --world world-zero --db world.sqlite3
# 外部世界包导出 WorldDefinition：
python -m agent_world --world my_world:WORLD --universe campaign --db campaign.sqlite3
```

当前组合入口把 Web、HTTP、MCP、timer worker 和 retention maintenance 接到同一 Runtime；详细实现见 [IMPLEMENTATION](docs/IMPLEMENTATION.md)。只加载部署方信任的 World Package。

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

维护改动遵守 [AGENTS.md](AGENTS.md)。历史审计与示例不覆盖上述现行入口。

## 本地验证

```sh
python tools/run_tests.py
python tools/check_package.py
node tests/view_client.test.mjs
node tests/stream_client.test.mjs
```

测试通过只证明具体覆盖，不等于所有产品目标、长期运行或任意外部世界已经成立。
