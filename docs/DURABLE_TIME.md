# 可选的持久定时事项

复核：2026-09-26。Timer 是服务器按世界规则执行的已提交定时事项，不是服务器里的 Agent、模型会话或代用户保存的凭据。不需要定时行为的世界可以不声明 timer。

## 声明与提交

`TimerSpec` 声明同步 handler、对象 input_schema、可选 output_schema / authorize、版本、重试次数和基础延迟。它不是 FunctionSpec，不可通过猜测工具名从 HTTP/MCP 直接执行。

`ctx.schedule_timer(id, handler, arguments, due_at=...)` 和 `ctx.cancel_timer(id)` 在 managed write 中缓冲命令，与操作一并提交；读函数和投影不能调度。初始化和迁移可调度，未知 handler 或无效命令回滚操作。`ctx.get_timer(id)` 是世界规则的有界元数据读取，不自动公开底层队列。

世界函数自行声明谁能调度、取消和查看。调度被接受后，事项不因创建者离线或凭据撤销自动消失。执行身份为 `system:timer`，ctx.timer 记录 ID、到期时间、原发起者／操作、创建时间和尝试次数。TimerSpec.authorize 在写锁下重查执行条件，state_authorizer 仍适用。是否随业务所有者状态失效由世界规则决定。

## 原子性与继续执行

调度、状态、来源和原操作回执同事务提交。每次触发的状态、历史、通知、回执、后续 timer 命令及 terminal status 原子提交。多个 worker 在 SQLite 写锁下串行；提交前崩溃后回调可能重跑，但不能产生两套已提交数据库效果。外部网络、付款或文件副作用不享有该保证，不得直接混入回调。

timer_id 在 universe 内唯一。同 ID 同意图再次调度为 no-op，完成／取消后也不重新激活；不同意图冲突，新一次发生需新 ID。取消只有在其事务先于触发提交时生效；取消已完成 timer 不撤销效果，取消未知 ID 报错，正在触发的 timer 不能取消自身。

timer 固定 world ID/version 和 handler version。升级不自动重解释旧事项，版本不匹配变为 blocked；迁移可明确取消旧 ID 并创建替代项。旧进程不得执行新版世界。此合同不定义通用业务轮次或外部 Agent 调度。

## 失败、晚到和容量

RetryTimer 请求回滚后重试，指数延迟有上限，尝试预算为 1–10 次，耗尽为 failed。授权／规则拒绝为 rejected；未预期 handler 错误为 failed，原始异常正文不向客户端泄露。存储故障回滚并留待后续 sweep。

一次失败不阻止其他到期事项。每次 sweep 的候选列表固定且有界，后续立即到期项不形成同一 sweep 内的无限循环。一次事务最多改变 32 个 timer，一个 universe 最多 10,000 个 pending timer。终态记录保留以防 ID 复用，未实现自动终态归档。

due_at 为有限 UTC Unix 时间戳，不是虚构游戏时间或对话轮次。worker 取得写锁后复查时钟；时钟回退可能延后执行，错误前跳也会影响到期判断。过期的一次性事项在服务恢复后处理，不捏造错过的周期。ctx.now 是执行时间，ctx.timer.due_at 是原截止时间；晚到业务结果由规则定义，不承诺硬实时。

## 运行边界

组合入口 `python -m agent_world` 在 lifespan 内默认运行 worker，即使无客户端；`--no-timers` 可禁用，`--timer-interval` 默认一秒。也可运行独立 worker：

```sh
python -m agent_world.timer_worker --world my_world:WORLD --universe campaign --db world.sqlite3
python -m agent_world.timer_worker --world my_world:WORLD --universe campaign --db world.sqlite3 --once
```

worker 与服务需使用匹配的包和数据库。旧独立 HTTP/MCP factory 不默认启 worker。关闭时等待正在执行的短规则完成；受信任回调不得挂起或在事务中等待外部参与者。导入包或运行测试不会安装常驻服务。

`examples/timed_worlds.py` 与 timer 测试覆盖规则期限、进程中断、并发 worker、权限复核、重试、取消和版本变化；这些是合同测试，不证明外部 Agent 持续在线。跨调用用户决定保存在世界状态中，不能统一改写成 timer。未决问题见 [OPEN_DESIGN](OPEN_DESIGN.md)。
