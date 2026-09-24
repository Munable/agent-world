# Product positioning · 产品定位

Reviewed 2026-09-24 against source `247fcde003197c78a8a7d6184e3517e76b5276d9`.
This is the positioning reference for the README and company website. Runtime contracts remain authoritative for implementation scope.

## The center

**A shared world that outlasts the conversation.**

Agent World gives people and agents somewhere to act together: a world with its own identity, rules and lasting state. A conversation may end; an accepted action still has a recorded result, and the same role can return to the state it left behind.

The product's value is continuity. The runtime owns the facts; models choose actions, world authors define the rules, and clients present what actually happened. This creates a useful foundation for persistent games and shared workflows without coupling their existence to one chat session.

### Website copy

> **A shared world that outlasts the conversation.**
> A runtime for people and agents to act in the same persistent world. Shared rules, durable state and recoverable actions keep their consequences intact across sessions.

Status label: **Open-source runtime**.

## Why this claim belongs to this project

| Product decision | What it makes possible | Evidence in this source |
| --- | --- | --- |
| Browser and Agent actions enter the same rules and transaction path | Different clients operate on one authoritative world | [Foundation contract](docs/FOUNDATION.md), [reference transport test](tests/test_reference_transport.py) |
| Stable roles, world state and retained operation receipts | Return to an existing role and recover committed results; retries do not repeat an accepted mutation | [Runtime execution](agent_world/runtime_functions.py), [foundation contract](docs/FOUNDATION.md) |
| World-owned persistent timers | Accepted timed obligations can settle without a connected model or browser, while a world worker is running | [Durable time](docs/DURABLE_TIME.md), [timer runtime](agent_world/runtime_timers.py) |
| Portable world package separated from the runtime | Village, encounter and workflow rules can use the same foundation | [Reference gate](docs/REFERENCE_GATE.md), [workflow example](examples/workflow_world.py) |
| Authorized views and retained shared streams | People can observe accepted facts and public events without controlling a role | [Observation contract](docs/OBSERVATION_STREAMS.md) |

These are source and existing acceptance references reviewed for copy, not fresh execution results from this positioning pass.

## The deliberate tradeoff

Prioritize consistent, recoverable consequences over unrestricted generated behavior. Worlds are authored trusted Python packages; their rules determine what can happen. The current local SQLite runtime favors a compact, inspectable foundation. Do not describe it as a distributed hosting platform, universal game generator or sandbox for arbitrary world code.

Persistence also does not imply continuous model thought. The world worker can complete an accepted timer; it cannot wake a stopped Agent host or invent that Agent's next decision. Runtime guarantees cover committed world data, not arbitrary payments, messages or other external effects.

## Relationship to the worlds

Agent World provides continuity and authority. [Lantern Hollow](https://github.com/Munable/agent-world-lantern-hollow) makes those properties tangible through a place people and agents can inhabit and return to. [Ashen Vault](https://github.com/Munable/agent-world-ashen-vault) uses them for explicit rules, resource choices and persistent adventure outcomes. Their maps, quests and combat systems belong to those worlds.

The connection to Marine Mystique's belief in software changing reality is concrete: an intention becomes a durable, shared consequence. These projects currently demonstrate that in digital worlds; claims about physical-world execution need separate evidence.

## 中文定位

**让世界延续，超出一次对话。**

Agent World 为人与 Agent 提供一个共同采取行动的世界：身份可以延续，规则由世界执行，结果会留下。对话结束之后，已经发生的事情仍然成立；再次进入时，同一个角色可以接着行动。

它的核心是“持续性”。模型决定做什么，世界规则决定实际发生什么，客户端呈现已经发生的事实。游戏和协作流程因此可以拥有独立于聊天会话的状态。

### 官网文案

> **让世界延续，超出一次对话。**
> 让人与 Agent 在同一个持久世界中行动。共同的规则、持续的状态与可恢复的操作，让每次行动的结果跨越会话保留下来。

状态：**开源运行时**。

### 取舍与边界

优先让行动结果一致、可恢复，再扩展世界的自由度。世界作者负责规则和内容，底座负责身份、事务、回执、观察与时间。当前是基于 SQLite 的可检查底座，不能写成任意世界自动生成、分布式托管或持续自主思考平台。

灯溪镇呈现“一个可以返回的地方”；灰烬地城呈现“按明确规则留下后果的选择”。两者是不同的世界产品。公司“用软件改变现实”的理念在这里对应的是：把意图变成共同可见、确实保留的数字世界事实。
