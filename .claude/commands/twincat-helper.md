# TwinCAT Helper（兼容入口）

## Trigger

`/twincat-helper <request>`

这是 `/twincat-agent` 的兼容入口，不维护另一套命令表、COM 参数或部署流程。

使用时先读取 [TwinCAT Agent 主技能](twincat-agent.md)，按它的路由加载当前任务所需的子技能。
修改程序不隐含上线或部署授权；保留主技能中的目标确认、回读验证和安装边界。
新工作流和参数修订只维护主技能及对应子技能，避免两个入口的行为分叉。
