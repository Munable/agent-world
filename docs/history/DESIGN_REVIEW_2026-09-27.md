# 六层深层复核记录：2026-09-27

本文件是一次带日期的审计记录，不是新的规格层。现行规则仍以 L1-L6 与两个横轴文档为准。

## 为什么继续重做上一轮整理

上一轮已经把四层改成六层，但继续逐句检查后发现层级名称正确、内容仍有交叉污染：L1 写实现版本和 SQLite，L2 用 HTTP/MCP 名称举不变量，L3 用数据库和 SDK 类定义概念，L4 又混入大量当前端点／CLI／容量参数。

本轮因此按“这句话换掉底层技术后还成立吗？”重新归层，而不是保护昨天刚写的文档。

## 主要校正

1. **L1 纯化。** 删除当前版本、SQLite/Python 和测试消费者，只保留 G1-G5 产品目标。
2. **L2 纯化。** 把系统不变量改成 I1-I10；仓库治理规则移回 BASELINE/AGENTS。
3. **L3 纯化。** 领域模型改用语义概念，Role/WorldDefinition/Table 等当前映射统一去 L6。
4. **L4 纯化。** Foundation、Agent Interaction、Data、Streams、Time、Retention 改成行为语义；具体 tool、endpoint、CLI 和容量移到 L6。
5. **L5 补足信任边界。** 明确 World Package 当前是受信任部署代码，业务 universe 隔离不是敌对插件沙箱。
6. **L6 做实。** 增加语义→代码映射、identity 现状、主要模块、完整主要表、公开 SDK/core tools、关键容量和 legacy raw connection 边界。
7. **证据降级。** 灯溪镇、灰烬地城明确降为历史 ad-hoc 兼容性探针，不再作为验收门槛或设计依据。
8. **增加 TRACEABILITY。** 把关键 G/I 主张分别标成已实现、部分实现、架构成立但证据有限等状态，避免“测试全绿=产品完成”。

## 深层发现

### 跨世界身份不是单纯 L6 TODO

G2 的当前缺口需要 L3/L4/L5 共同设计：跨独立部署如何验证同一用户、信任根、credential scope、撤销／轮换／恢复和世界间最小披露都尚未决定。简单删除当前 universe scope 会破坏隔离而不是完成产品目标。

### universe 隔离不是恶意代码安全隔离

支持的 Runtime 接口会按 universe 限制状态与操作，且 managed raw state write 会在提交前重校验。但 World Package 是同进程受信任 Python；legacy `ctx.conn` 可以读取内部数据库结构，Python 代码也拥有进程／OS 能力。

因此当前可以声称“支持接口的业务作用域隔离”，不能声称“互不信任世界包的机密安全沙箱”。如果未来产品要多租户运行敌对 world code，必须改变 L5 架构。

### 临时大场景没有设计权威

灯溪镇、灰烬地城是在已有基础后临时创建的测试消费者。它们可能暴露 bug，但它们碰巧需要的内容不能反向证明 Runtime 必须拥有某种玩法或表现抽象。历史结果保留，设计权威取消。

## 本轮仍未决定

- 跨世界 identity trust architecture。
- 通用共同事项／确认模型是否值得进入 Runtime。
- 第三方／群众公裁的正式领域模型和成立条件。
- 长期 Receipt/commit/timer identity 的归档语义。
- 外部副作用 delivery contract。
- 是否以及何时正式废弃 legacy raw connection。

## 验证纪律

本轮文档和静态架构测试之后，仍必须重新运行核心 unittest、package check、核心 Node client tests 与文档／链接检查。临时外部测试世界不是必跑项；除非有具体假设需要它们反证。

## 实际验证结果

深层复核后的核心验证已经执行，而不是只写了验证计划：

- `python tools/run_tests.py`：189 项核心 unittest 通过；12 份保留旧回归脚本全部通过。
- `tests/test_architecture_boundaries.py` 与 `tests/test_regression_guards.py` 单独运行：3 项通过。
- `node tests/view_client.test.mjs`、`node tests/stream_client.test.mjs`：通过。
- `python tools/check_package.py`：wheel 构建、独立安装、静态资源与最小仓库外 WorldDefinition fixture 通过。
- 静态结构：91 个 Python 文件解析通过；24 份 Markdown、83 个本地链接、4 个 JSON 检查通过；`git diff --check` 通过。
- 最后一轮 G2 加固、L3 关系补充和历史归档之后，`agent_world/`、`tests/`、`tools/` 相对完整测试快照 `efe72db` 的 diff 为 0 字节。

本轮**没有**重新运行灯溪镇／灰烬地城；这是刻意的证据边界调整，不是漏测。它们没有具体待反驳假设，因此继续运行只会制造“测试很多所以架构靠谱”的错觉。

本轮仍没有新的长期 soak、真实多宿主 Agent、Linux/Python 版本矩阵、跨独立部署身份或生产数据库迁移证据。详细机器可读记录见 [深层复核证据](REPOSITORY_EVIDENCE_DEEP_2026-09-27.json)。