# agent-world

**让世界延续，超出一次对话。**

Agent World 是世界 Runtime。外部 Agent 和其他客户端通过结构化接口，按开发者定义的规则操作同一持久世界。服务器不运行 Agent，不将自然语言消息解释为控制指令。

[产品定义](PRODUCT_POSITIONING.md)规定用户持有、跨世界通用的身份令牌与统一档案。**当前 0.14.0 的令牌仍绑定单个 universe；跨世界统一验证尚未实现。** 文档目标不等于代码已经支持。

## 运行与开发世界

```sh
python -m pip install -e .
# 设置 WORLD_WEB_PASSWORD 才启用操作员 Web 原型。
python -m agent_world --world world-zero --db world.sqlite3
# 外部世界包导出 WorldDefinition：
python -m agent_world --world my_world:WORLD --universe campaign --db campaign.sqlite3
```

Web、HTTP、MCP 使用同一数据库和 origin，默认 `http://127.0.0.1:8000`。Web 默认用户名为 `operator`；世界调用使用 Bearer 身份凭据。操作员入口只是当前管理工具，不定义用户身份的所有权。只加载受信任世界代码。

## 先读这三处

[产品定义](PRODUCT_POSITIONING.md) → [分层基线与仓库入口](docs/BASELINE.md) → [本次盘点与验证](docs/REPOSITORY_AUDIT_2026-09-26.md)。维护改动遵守 [AGENTS.md](AGENTS.md)。

原则、模型、合同、实现是四种职责，测试跨层使用；目录不按四层机械拆分。

## 现行文档

| 文档 | 唯一职责 |
| --- | --- |
| [Foundation](docs/FOUNDATION.md) | 函数、身份接入、事务、回执、版本与 SDK 合同。 |
| [Agent 交互](docs/AGENT_INTERACTION.md) | 结构化接入、消息／回应边界、外部执行、等待与恢复。 |
| [世界数据](docs/WORLD_DATA.md) | 服务端／Agent 数据归属、授权视图与缓存。 |
| [观察与事件流](docs/OBSERVATION_STREAMS.md) | 公开观察、频道、游标和事件读取。 |
| [持久时间](docs/DURABLE_TIME.md) | 可选定时事项的授权、执行、失败与恢复。 |
| [保留与表现](docs/RETENTION_PRESENTATION.md) | 可选保留政策和表现信封，不定义玩法。 |
| [待设计与待验证](docs/OPEN_DESIGN.md) | 尚未定稿的交互细节、公裁、实现缺口和实验问题。 |
| [验收范围](docs/REFERENCE_GATE.md) | 按能力选择的检查与证据边界，不是游戏产品路线图。 |

[Commons](docs/COMMONS_WORLD_V01.md)、[World Zero](docs/WORLD_ZERO_V01.md) 是带日期的示例说明；[Foundation audit](docs/FOUNDATION_AUDIT.md) 是历史测试记录，不是现行合同。独立规则示例：[encounter](examples/encounter_world.py)、[workflow](examples/workflow_world.py)。

## 本地验证

```sh
python tools/run_tests.py
python tools/check_package.py
node tests/view_client.test.mjs
node tests/stream_client.test.mjs
```

日常开发优先本地或可控机器。纯文档变更只做内容、链接和差异检查；不为它反复运行计费 CI。功能测试通过不等于长期运行或外部 Agent 协作逻辑已经全部验证。
