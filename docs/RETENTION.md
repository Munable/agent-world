# L4：数据保留合同

复核：2026-09-27。本文只定义历史／通知数据的保留与清理行为。表现语义已独立到 [PRESENTATION](PRESENTATION.md)；数据归属与缓存见 [WORLD_DATA](WORLD_DATA.md)。

## 状态历史与通知保留

StateRule 的 history="metadata" 只为新提交历史保存版本／变化标识，不保存前后值；full 是兼容默认。多个规则匹配时任一 metadata 规则抑制历史值，当前状态本身不因此改变。

这不是秘密擦除保证：旧历史、回执、通知、存储日志、数据库副本和备份可能仍保存内容。逻辑删除也不保证物理存储立即缩小。

世界可以声明 RetentionPolicy，包括 event_seconds、event_rows、history_seconds 和 history_rows。没有 policy 时，Runtime 不新增破坏性自动清理。

保留清理必须是有界、显式配置的维护行为，并与 timer 语义独立。当前周期和命令入口见 [IMPLEMENTATION](IMPLEMENTATION.md)。

## 清理边界

清理在同一事务删除实例内连续前缀并推进 floor，短期突发可以暂时超过行数限制。当前状态、commit metadata、timer ID 和 operation receipt 不由此 policy 删除，因此该 policy **不提供数据库总大小上界**。

去重、未知结果恢复或仍在进行的业务事项所需数据不能为了省空间被直接清除。跨保留周期仍有效的事项必须在当前权威状态中保存足够信息，或由明确业务规则进入终态。

共享 StreamSpec 有自己的 retention_seconds / max_events 合同；共享流清理与 recipient events、state、receipts 分离。不同数据用途不能因为都叫“历史”就共用一个删除语义。

## 未决问题

回执、commit metadata、终态 timer ID 等长期增长后的压缩／归档策略尚未统一确定。应先验证增长、恢复和旧 operation 重放，再定义安全过期规则，见 [OPEN_DESIGN](OPEN_DESIGN.md)。

保留策略属于行为合同，不是存储技术本身；未来更换 SQLite 也必须维持已经承诺的恢复和去重语义。
