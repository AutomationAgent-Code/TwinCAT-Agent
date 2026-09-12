# 工具前置条件契约

本文档定义 Agent 工具入口的前置条件边界。它不是把所有状态压缩成一个
`online` 开关，而是分别核对 XAE 宿主、PLC 编辑器登录状态、PLC Runtime
ADS 状态、源文件保存/版本、目标和权限。

## 状态语义

| 状态维度 | 含义 | 不能替代的维度 |
| --- | --- | --- |
| XAE identity | 当前 Automation Interface 宿主 PID、解决方案和任务绑定一致 | PLC Runtime Run、编辑器 Login |
| PLC editor online | PLC 工程是否已 Login/处于在线编辑语境 | PLC Runtime Run、源文件已保存 |
| PLC Runtime state | 目标 Runtime 的实际 ADS Config/Stop/Run 状态 | XAE 是否连接、编辑器是否 Login |
| source revision | 当前对象/区域的实时源版本、保存状态和冲突指纹 | 编译结果、Runtime 状态 |
| target/endpoint | 当前目标 AMS NetId、实际 PLC runtime 和 ADS 端口 | 端口顺序猜测 |
| approval/gate | 权限策略、代码质量门禁和工具自身确认项 | 任一实际状态证据 |

官方语义也将这些动作分开：Login 是 PLC 工程连接到控制器，且要求工程无错误、
目标处于 Run；Online Change 是对运行中的应用加载变更；它们都不等同于“源文档
已保存”。参考 [Login](https://infosys.beckhoff.com/content/1033/tc3_plc_intro/2531393419.html)、
[Online Change](https://infosys.beckhoff.com/content/1033/tc3_userinterface/2531444363.html)
和 [Automation Interface 的 PLC online 访问](https://infosys.beckhoff.com/content/1031/tc3_automationinterface/21905744267.html)。

## 工具矩阵

| 工具类别 | 进入实际 handler 前声明/检查 | handler 内最终检查 | 未满足时 |
| --- | --- | --- | --- |
| 离线源码读取、索引、缓存 | solution/object scope、`source`/`live_xae`/`dirty_unknown` freshness provenance | 索引或文件读取 | 可在明确的已保存工程上离线；不得冒充实时 XAE |
| PLC 写入、patch、创建、删除、重命名、导入、成员/属性修改 | XAE identity、精确对象范围、未保存/源 revision、代码 gate、权限 | COM/Automation Interface 最终解析、写后 readback 和现有审查 | `blocked`/`conflict`/`unknown`；不自动保存、丢弃、Logout 或切模式 |
| Build/diagnostics | 工程绑定、source freshness；不隐式 Login/Start | 现有 platform preflight、构建互斥和错误来源判定 | 未确认工程或平台时不构建；旧诊断不能冒充本次构建 |
| ADS 值读取 | target identity、实际 runtime/ADS endpoint、符号类型 | 动态符号解析和在线读取 | endpoint/type 未知则 `unknown`，不猜端口 |
| ADS 值写入 | target identity、实际 endpoint、Runtime=Run、权限、可选 `expected_before` | 现有 expected-before、写后 readback/unknown 结果 | 不为写值自动 Start/Stop/Config/Restart；失败不重试 |
| Login/Logout/Start/Stop/Online | XAE/target identity、明确 runtime/all_plcs、平台映射、转换前状态、权限 | 现有 runtime transition contract、逐阶段 unchanged 校验 | 只报告阻断；不隐藏切 Config、启动或重启 |
| HMI、I/O、System、NC、Safety | 复用各自已有的项目/Schema/目标/高审批契约 | 原有专用 guarded contract、ProduceXml 回读或 Safety 硬确认 | 本层不绕过专用门禁，也不宣称所有底层直调入口统一受此层覆盖 |

## 结构化失败

所有由共享层拒绝的调用返回稳定字段：

```json
{
  "status": "blocked|unknown|conflict|unavailable|unsupported",
  "error_type": "tool_precondition",
  "condition": "source_conflict",
  "expected": "...",
  "actual": "...",
  "scope": {"tool": "..."},
  "reason": "...",
  "next_action": "...",
  "not_executed": true
}
```

`not_executed=true` 是硬保证：共享层失败后不调用实际 handler，也不自动发出
Logout、Stop、Config、Start、Restart 或切换目标命令。`unknown` 表示证据不足，
不是“按安全默认值当作离线”；`conflict` 表示绑定 PID、源版本或保存状态发生
冲突；`unavailable` 表示必要的宿主/目标证据不可读。

## 执行边界与覆盖情况

检查在 `agent_core.run_tool` 的实际 dispatch 点执行，并由 backend 的 XAE 工具
锁保护。稳定的解决方案/PID 可来自绑定上下文；易变化的 Runtime 状态、源 revision
和保存状态必须在执行前重新读取。批量 ADS/HMI 工具仍由各自 handler 做最终的
批内校验和回读，不能用一次静态预检替代。

当前共享层覆盖本地 Agent 注册表的 PLC 源变更、PLC/ADS 值工具、Runtime transition、
源码/HMI 读取 freshness 以及 System/I/O/NC/HMI 的声明元数据。以下边界明确不作
过度承诺：

1. 当前 XAE Automation Interface/桥接没有可靠、统一的“PLC 编辑器当前在线模式”
   字段；不能把 `LoggedIn`、Runtime `Run` 或活动文档 `saved` 猜成该字段。实际
   source mutation 只阻断可证实的未保存目标文档，Runtime Run 本身不阻断源码编辑。
2. 直接绕过 `agent_core.run_tool` 的低层 `tc_template`/PowerShell/COM 调用，以及
   外部 MCP 工具，不自动获得本地共享层声明；它们必须遵守各自已有 contract，或
   由对应 connector 自行实现同等门禁。
3. 专用 HMI/I/O/System/Safety contract 是最终权威；共享层的 metadata 是可见的
   入口声明，不替代 Schema、ProduceXml 回读、Safety review 或现场验收。

## 参考实现

声明位于 `tc_agent/tool_preconditions.py`，dispatch 检查位于
`tc_agent/agent_core.py`。新工具必须先声明条件，再决定是否能安全地做轻量只读
预检；不能因为缺少 API 而伪造 offline/online 结论。任何会改变 PLC、XAE、HMI、
I/O、System 或 Safety 状态的工具，都应明确列出作用域、实际状态证据、权限和
写后验证。
