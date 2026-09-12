# TwinCAT Agent v1.0.8.56

- 修复 Agent 通过 `thread_send` 工具发送任务时未广播到目标会话的问题。
- Agent 工具发送的 `task` 现在会在目标会话显示并自动执行。
- 界面发送与 Agent 工具发送统一使用同一套跨会话投递链路。
