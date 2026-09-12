# HMI 工具参数与失败恢复（2026-09-06）

本次针对 Project10 对话记录暴露的 Agent 问题，不自动继续修改用户 PLC/HMI 工程。

- `tc_hmi_control_events.actions` 向模型公开 `objectType`、`symbolExpression`、
  `value.objectType` 等字段；read 返回本机实际事件列表和完整 `action_contract` 示例。
  参数无效时先在 Python 层拒绝，并返回所需字段/可用事件；不会靠猜字段反复调用 COM。
- HMI 工具 `file` 参数在分派前检查控制字符、非法 Windows 路径字符和协议片段；
  `control_events` 直接调用也检查。拒绝后要求重新提供路径，不擅自修剪成另一个文件。
- `blocked`、`written=false`、`review.approved=false` 不再被记为成功。
  显式 `status=preview` 的未写入仍属于成功预览。当前模型批次遇到门禁拒绝后，
  跳过后续写操作并补全 tool_result，保留只读检查，让下一模型步先修复拒绝原因。
  前台、后台派发和 CLI 使用相同判定；不修改已有历史执行记录。
- 标准 FB 枚举创建先 `find-pou` 精确筛选名称。空结果才创建；同名歧义、对象类型错误、
  查询失败不会进入创建分支。不再依赖 COM 包装后已丢失的 FileNotFoundError 类型。
- 策略明确区分 PLC 编译、HMI 构建、绑定配置、浏览器运行和在线动作验证，
  未完成的验证必须报告；截图数值不构成实时绑定的证据。

现场只读与预览：`LineHMI/Desktop.view` 的 `BtnStart` 可读取当前 1.12 控件事件；
`objectType=WriteToSymbol`、`value={objectType:StaticValue,value:true}` 与实际映射
符号组合后 `apply=false` 返回 preview。没有写入触发器，没有点击按钮或写 PLC 值。

验证入口：`tests/test_hmi_tool_recovery.py`、`tests/test_hmi_events.py`、
`tests/test_agent_tools.py`、全量 pytest。包含原错误文件名、错误动作键、门禁执行记录、
批次跳过和枚举发现失败场景。
