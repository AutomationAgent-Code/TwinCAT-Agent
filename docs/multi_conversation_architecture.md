# TwinCAT Agent 多对话架构

## 当前实现

每个 TwinCAT 解决方案使用自己的数据库：

```text
<解决方案目录>/.TwinCATAgent/agent.db
```

数据库使用 SQLite + WAL，包含：

- `threads`：对话元数据、父子关系、类型、状态和归档状态。
- `thread_state`：每个对话独立的模型消息、UI 事件、压缩摘要和中断恢复快照。
- `mailbox`：对话之间的结构化任务、上下文和结果消息。
- `worker_runs` / `change_proposals`：旧版后台子任务的兼容审计表；当前版本不再创建后台任务。
- `project_memories`：项目级结构化事实、决策、偏好与约束。
- `metadata`：数据库版本和旧历史迁移状态。

旧的 `.tc_agent_history.json` 第一次打开时会迁移为“主对话”，迁移只执行一次。

## 对话隔离与上下文

每个对话拥有独立的：

- 原始近期消息；
- 较早历史的语义压缩摘要；
- UI 回放事件；
- 网络中断恢复快照；
- 未读项目内消息。

模型上下文不会在对话间隐式混用。需要共享的信息通过 `thread_send` 发送，接收方下一轮会自动获得未读结构化消息；任务成功完成后才标记已读。

## Agent 可用的协作工具

- `thread_list`：列出当前项目的对话与未读状态。
- `thread_send`：向另一对话发送任务、上下文、通知或任务结果。
- `thread_inbox`：读取当前或指定对话的收件箱。
- `thread_rename` / `thread_fork`：管理普通对话生命周期。
- `memory_list` / `memory_remember` / `memory_forget`：维护项目共享记忆。
- `project_data_health` / `project_data_backup` / `project_data_export`：检查、快照和导出项目对话数据。

## UI 协议

WebSocket 支持 `list_threads`、`switch_thread`、`new_session`、`archive_thread` 和 `thread_send`。聊天页顶部可创建、切换和归档对话；有未读消息的对话显示圆点。

## 普通多对话

当前产品只保留普通多对话，不再提供后台 Worker/子任务。用户可以新建、切换、重命名、分叉和归档对话；分叉复制模型消息与语义摘要，但不复制 UI 回放和中断恢复状态。对话之间可通过 `thread_send` 明确传递上下文，不会隐式混用历史。

旧数据库中的 `worker/review/research` 记录和审计表不会被删除，便于读取历史与回滚，但界面不再提供创建、运行、停止、汇总或恢复入口，模型工具也不能创建后台任务。

## 项目共享记忆

`project_memories` 保存跨对话共享的结构化 `fact`、`decision`、`preference` 和 `constraint`。每条记录包含稳定键、内容、来源对话、置信度和启停状态。对话原始历史不会因此混用；模型只接收最多 8000 字符的活动记忆摘要。

同一个活动键出现不同内容时返回冲突，不静默覆盖。疑似 API Key、Token、密码、TwinCAT Agent 授权码的内容会被拒绝。`memory_forget` 仅停用记录并保留审计历史。若记忆与 XAE 工具读取的实时状态冲突，以实时状态为准。

## 数据维护与迁移

UI 的“数据维护”与同名工具提供三项能力：`health` 执行 SQLite 完整性检查并统计记录；`backup` 使用 SQLite Backup API 生成一致性快照；`export` 输出可审计 UTF-8 JSON。文件分别位于项目的 `.TwinCATAgent/backups/` 与 `.TwinCATAgent/exports/`。导出包含完整对话正文，不包含 Provider 配置或 API Key，但仍应作为项目敏感资料保管。

不得跨解决方案共享数据库。
