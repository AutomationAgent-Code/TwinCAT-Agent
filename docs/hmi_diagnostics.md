# HMI 诊断与验证边界

## 警告审查

构建成功不等于验收通过。`tc_hmi_build` 和 `tc_hmi_diagnostics` 保留错误、警告的
项目归属、文件、行号与完整明细，分别返回 `error_count` / `warning_count`；不可读时为 null。
存在警告即设置 `warning_review_required=true`，验收保持待审查，不能凭 0 error 宣告完成。
构建成功、诊断完整且仅有警告时返回 `review_required`，界面显示“构建成功，有警告待审查”；
只读诊断不证明构建成功，显示“有警告待审查”。真正构建失败和诊断缺失仍保持失败/未完成。
审查需逐条说明影响及处置，不能自动忽略或通过重复构建清除警告。当前没有持久化警告豁免功能；
仍存在的警告继续保持待审查。快照可能包含其他工程或旧记录，不据此自动修改项目。

`tc_hmi_diagnostics(project, check_page=true)` 是只读工具，不构建、不清空错误列表、不
启动或激活 PLC/HMI Server，不写 ADS。Agent 与外部 MCP 均提供此入口。

- `error_list`：当前 XAE 的 DTE ErrorItems 快照，最多 200 条，保留项目归属；可能包含
  其他项目或先前构建的记录。接口不支持、读取中断或超过上限时 `complete=false`。
  不可读时 `total=null`，绝不能解释为零错误。当前 TcXaeShell 15 的外部 COM 读取仍可能
  不可用；本修复明确报告限制，不通过抢焦点、复制剪贴板或变更扩展绕过。
- `server_log`：当前解决方案 `.engineering_servers/<HMI 项目文件名>/logger.db`，只读
  查询已验证的 SQLite schema，读取最近一小时至多 100 条事件。拒绝路径逃逸；外部链接
  工程、缺失文件、未知 schema、锁冲突均报告 unavailable。只返回已知端口状态错误的
  runtime/code/symbol/netid 参数，其他事件不输出参数内容。
- 日志保留 UTC 时间、距今秒数及原始 severity。某些 ADS 错误被服务器以 Info 记录，
  因此额外标记 ERROR/FAILED/FAILURE 事件。`fault_record` 仅表示过去发生过故障；
  不代表当前持续故障。空日志、截断日志也不证明系统健康。
- `page`：仅探测已存在 Server 的实际 HTTP 入口。HTTP 200 不等于控件/脚本、按钮动作
  或 PLC 通信通过；浏览器行为仍需独立只读验证。
- `plc_communication`：默认 `not_checked`，必须独立调用 `tc_hmi_ads_live_check` 才能
  提供在线读值证据；即使诊断发现离线也不隐式上线。

变量绑定故障优先使用 `tc_hmi_binding_diagnose`。它把原先需要人工拼接的静态绑定、TMC
导出、HMI Server 动态映射/Schema、XAE 实际 NetId/PLC ADS 端口和在线只读检查合并为
一个有界结果，并用 `blocking_stage` 与 `root_causes` 保留多个并发根因。ADS 错误文本即使
受控制台编码影响，也会稳定返回 `ads_error_code` 和已知符号名（例如 ADS 6 对应
`ERR_TARGETPORTNOTFOUND`）。该工具只诊断；不会切换目标、登录/启动 PLC、重载 HMI 或写配置。

推荐处理顺序：

1. 修正无效 SymbolExpression、内部符号或控件引用；
2. 确认变量已进入所选 PLC 的 TMC，缺失时先修声明/导出并重新编译；
3. 对“TMC 已有、HMI 映射缺失”的变量调用 `tc_hmi_bind_plc` 预览，再按审批应用；
4. 端点不一致时仍由 `tc_hmi_bind_plc` 使用 XAE 实际目标/端口生成配置，不猜 851；
5. 端点一致但 ADS 不可达时，只报告实际错误并等待用户决定本地/远程目标及是否上线；
6. 修复后重新诊断，最后对明确页面执行浏览器验证。

`tc_hmi_build` 保留 `build_succeeded`，表示 DTE 构建返回值；读取 LastBuildInfo 失败时
为 null，不再默认 0。错误明细不可用、有错误或不完整时整体返回 incomplete/failed，
`success=false`、`diagnostics_pending=true`。其结果包含服务器历史日志，但不自动探测
HTTP/ADS。诊断缺失时不得循环重编译，改用只读诊断并报告尚未验证的层级。

执行策略同时识别 camelCase 和 snake_case 的失败项目数、错误数及 diagnostics pending，
避免传输格式差异把不完整结果记成成功。当前内置注册表基线为 190 个工具。

回归测试：`tests/test_hmi_diagnostics.py` 包含真实 PowerShell 函数与假 DTE 的契约测试、
SQLite 只读/时间范围/截断/未知 schema/跨工程保护，以及 HTTP 成功不代表 ADS 验证的测试。
