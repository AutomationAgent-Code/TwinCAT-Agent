# PLC 语法模板与编译修正闭环

## 工具与证据

`plc_syntax_templates(kind)` 提供 XAE 分离式编辑器的最小语法参考：FB、Program、Function、Struct、Enum、Union、GVL、Method、Interface、Property 与 Get/Set（含接口成员）。
模板不直接创建对象，不代表已通过当前版本编译；应用 FB 仍先选功能模板。

共享写前审查新增独立 `syntax-*` 硬错误：对象结束关键字误写、声明区块/控制区块配对、括号与数组方括号配对。
注释、字符串和编译属性不当作代码。含条件编译的区域不执行整体配对，避免把不同预处理分支误判。
这些是保守检查，不是完整 ST 解析器；类型、库、表达式语义仍需真实编译。

`plc_build` 保留原始编译结果并新增：

- `compiler_verified`：实际构建、完整诊断、失败项目数和错误数共同确认。调用成功不等于编译通过。
- `diagnostics_complete`：错误数量与诊断列表一致、来源可用、非 pending/截断/失败数兜底。
- `repair_diagnostics`：原始文件、项目、行列、诊断代码与内容，不把编译器文件路径冒充 COM 树路径。
- `repair_allowed`、`diagnostic_fingerprint`、`next_action`：驱动修正和无进展检测。

## 顺序

1. 查询适用对象语法模板，先准备声明、实现和依赖。
2. 写前检查通过后写入，精确回读。
3. 在既有平台前置检查通过后构建；没有实际目标平台证据不能猜选。
4. 诊断完整且确有编译错误：按原始位置查找精确 COM 对象、读取局部代码，每次修正一组根因。
5. 局部补丁回读成功后再次构建；最后一次修改后必须有新的完整成功编译证据。

当轮失败策略阻止无成功源代码变更的重复编译；首次失败后最多再编译三轮，仍失败则报告残留问题。
诊断不完整不允许猜改；老旧桥只返回缺少完成证据的结果时会显示 incomplete，而非假成功。
平台前置失败不属于源码修正轮次，也不会触发“无源码进展”的重复编译门禁。目标平台在线探测不可用时，
若活动平台与全部 PLC Build Context 完整且一致，允许进行本地编译并明确返回
`target_compatibility_verified=false`；这只能证明当前编译器/平台组合通过，不能证明产物适合下载到目标。
若本地 Build Context 缺失、禁用或不一致，仍在构建前阻止，并将错误数/失败项目数返回为 unknown/null，
不能用合成的 0 或 1 冒充编译器结果。平台选择或修复仍是独立配置修改，需要单独确认。
编译结果不确定或写后验证失败会使旧编译证据失效。修正由 Agent 在已有编辑授权内调用工具，不在工具内部执行不透明的代码重写。
本轮预算不跨任务持久化；不是源代码语义进展证明。

范围限定：这一结果契约首先接入 Agent 的 `plc_build`，不宣称改造所有 CLI/MCP 编译入口。
不改变已有质量门禁设置；语法错误与质量警告分开。没有自动登录、下载、启动、改库或改编码。

## 官方依据

- [Automation Interface 对象读写](https://infosys.beckhoff.com/content/1033/tc3_automationinterface/242732427.html)
- [Interface](https://infosys.beckhoff.com/content/1033/tc3_plc_intro/4256428299.html)
- [Interface property](https://infosys.beckhoff.com/content/1033/tc3_plc_intro/4256479627.html)
- [ST CASE](https://infosys.beckhoff.com/content/1033/tc3_plc_intro/2528286347.html)

测试：`python -m pytest tests/test_plc_syntax_cycle.py -q`。模拟测试不等于 4024/4026 实际编译矩阵验证。
