# 可选保留政策与表现事件

复核：2026-09-26。两项能力均按需使用，不强制世界有图形界面。数据归属与缓存见 [WORLD_DATA](WORLD_DATA.md)；观察凭据和公开观察分别见 [Foundation](FOUNDATION.md)、[OBSERVATION_STREAMS](OBSERVATION_STREAMS.md)。

## 数据保留

`StateRule(..., history="metadata")` 只为新提交历史保存版本／变化标识，不保存前后值；`full` 是兼容默认，多个规则匹配时任一 metadata 规则抑制历史值。当前状态不变。它不是秘密擦除保证，旧历史、回执、通知、WAL 和备份可能仍保存内容。

世界可以声明：

```python
RetentionPolicy(event_seconds=3600, event_rows=1000,
                history_seconds=86400, history_rows=5000)
```

组合入口在配置后每 30 秒执行有界 maintenance，与 timer worker 独立。其他部署可运行 `python -m agent_world.maintenance --world module:WORLD --universe example --db world.sqlite3`。无 policy 不新增破坏性自动清理；共享流使用自己的保留合同。

清理在同一事务删除实例内连续前缀并推进 floor，短期突发可超过行限额。当前状态、commit metadata、timer ID 和操作回执不被此 policy 删除，因此数据库总大小没有上界保证。不能为控制容量直接删去重依据；相关设计与验证见 [OPEN_DESIGN](OPEN_DESIGN.md)。逻辑删除不是安全擦除，也不保证数据库文件立即缩小。

## 表现信封不是公开权限

`PresentationCue(cue_id, subject_id, channel, phase, name, data).event(recipient_role_id)` 生成普通 EventSpec，信封类型为 world.presentation。它是**按角色投递**，不会因为名为 speech 或 intent 就公开给所有人。只有显式发布到获准共享频道时才按该频道策略可见。

当前 channel 为 action / speech / intent，phase 为 start / finish / cancel。cue_id 关联生命周期；subject_id 标识世界对象；name/data 由世界定义；时间与 actor 来源由服务器记录。intent 只能是明确提交的意图字段，不是服务器推测的私人思考。

Runtime 校验信封与大小并随状态和回执原子提交，包括 timer outcome。世界负责主体、受众、生命周期和 payload 的领域合法性。信封不实现移动、对话或审批状态机。文本不作为 HTML、代码或自然语言控制命令执行。

表现数据用于描述逻辑动作，不是持续写入渲染帧。当前仍在进行的业务状态必须能从获准 state/view 恢复；动画结束不触发第二次业务提交，缓存丢失不改变结果。

## 可选 timeline

`ViewSpec(..., timeline=True)` 返回 opaque timeline_cursor，与快照同事务锚定。`world.view_timeline` / `POST /v1/views/timeline` 接受 cursor 和可选 limit，返回有序 events、新 cursor、base_cursor、has_more。

每次读取复核当前视图权限；只返回原接收者的 cue，且 subject 当前在视图中可见。受众由世界规则决定，不由 renderer 推断；这不是匿名公开频道。

检查点绑定 role、credential、universe、selector、world/view version。过期、驱逐、保留缺口需 ViewResetRequired 后重取快照，取消过期表现队列，不补造历史。WorldClient.readTimeline 与净增量同步分开维护游标，丢弃过期并发回复。此端点不暴露全局私人序列号，普通 recipient events 保持各自合同。

美术、行走帧、相机、寻路和领域动画绑定由世界／客户端实现，不进入 Runtime 的必要验收范围。
