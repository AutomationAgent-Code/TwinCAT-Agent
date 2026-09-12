# 工具恢复与可见性契约

## 本次修复

- 临时 diagnostics_pending / source conflict 不再写入永久失败去重记录。未执行不等于执行结果不确定；真正已写入但回读失败仍不得重放。
- 前台和后台使用同一 FailurePolicy。宿主 PID / 解决方案变化时隔离旧状态；有精确 TIPC 项目证据才缩小范围，没有证据的 SolutionBuild 保持解决方案范围，不能猜测出错对象。其他解决方案/项目级 direct 诊断不能冒充整个解决方案恢复。
- 有前后完整实时 source_hashes 的外部编辑可以记录为实际源码进展；同一份代码的重复读取不算进展。它不证明编译通过，也不解除诊断缺失门禁。
- PLC 源码变更入口复用 source mutation 分类，不再漏掉导入和快照恢复；快照预览不执行写入，不受该写入门禁阻断。
- 自动附带只读恢复依赖：plc_read / plc_find / plc_diagnostics / plc_build_status / tc_project_info。依赖注入不添加写入权限；执行时的工具类别检查也承认这些只读依赖。
- Build 缺少令牌的 next_action 修正为先读取状态、再申请 Build 审批。

## 自动恢复边界

仅 plc_diagnostics 与 plc_build_status 的可识别临时读取故障可以自动重试，最多两次。Build / Verify 已实际执行但诊断不完整时，自动读取诊断，不重复 Build。原构建仍保持未验证，诊断回读不能宣称本次编译成功。

默认诊断路径为绑定 PID 的 VSIX 管道；服务不可用时使用 strictPid 的 native COM ErrorItems 只读枚举。不会新建 XAE、聚焦错误列表、复制剪贴板、保存源码、Logout、Stop 或切换目标。空集合与枚举失败分开；编译忙碌、集合变化或失败却无错误仍不完整。

恢复仍失败时返回 recovery_exhausted，前台/后台停止这次自动循环并合并报告服务故障与未验证范围，不把停机当成功。审批、明确拒绝、写入不确定立即暴露，不隐藏或自动重试。过期审批仍需重新申请，并非静默授权。

## 界面与日志

中间只读重试仅更新原工具卡片为“正在恢复”，不生成多张失败卡片。恢复成功显示“已恢复”；仅诊断恢复时标明编译未验证。失败显示一条简短原因/下一步，原始结果保留在折叠详情与执行记录。模型上下文省略重复的 recovery.history，保留诊断结果和恢复是否耗尽。

历史回放与实时事件共用同一前端处理器。后端恢复记录不改写旧会话、不改变用户模型配置。此批仅更新源码；运行安装版需另行同步。单元/模拟测试不能替代实际 XAE 故障注入验收。

## 回归记录

### VAR_TEMP 诊断分类补修

2026-09-12 当前 XAE PID 19584 的真实只读结果：VSIX 返回 `ok=true/diagnosticsAvailable=true`，但将 `FB_PID.TcPOU@CalcPidiOutput (Decl)` 第 22 行 `VAR_TEMP declaration not allowed in this place` 放入 Medium/warning。旧分类缺少此明确错误签名，导致 failedProjects=2/errorCount=0/pending=true 被误报服务不可用。

后端兼容归一化与扩展源码补入该精确签名；仍要求失败构建、真实文件及正整数行号，不泛化提升普通警告。保持原始 ErrorLevel 与推断标志，完整性仍遵循已知旧 VSIX 合同，截断/计数不一致不解除门禁。恢复耗尽时区分“通道返回数据但证据不完整”和“服务不可用”。源码侧在当前 XAE 实读已得到 errorCount=1/warningCount=2/diagnostics_complete=true/buildPerformed=false/compiler_verified=false；未编译或修改 PLC。安装版仅需后端兼容修复，无需替换当前加载的扩展 DLL。

补修全量回归：1622 passed、117 subtests passed。已同步安装版后端，备份 `.updates/backup-20260912-041039`；当前 XAE 不重启，PLC 不修改。

2026-09-12：全量 `py -3.14 -m pytest -q --tb=short`，1615 passed、117 subtests passed。
新增跨工具恢复链测试、native COM 只读回退模拟、源码变化/目标隔离测试，以及 Node 驱动的生产前端事件处理器实时/历史回放测试；全部内联 JavaScript 通过语法解析。未对正在运行的 PLC 注入故障，未同步安装目录。
