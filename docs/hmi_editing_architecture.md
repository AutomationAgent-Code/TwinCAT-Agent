# HMI 编辑与事件工具整理

## 分层

- 页面编辑：现有 .view/.content/.usercontrol 通过精确路径 DTE TextDocument 写入，备份、保存、全文回读，不卸载工程。DTE 不公开文档时返回失败，不自动重载。
- 项目结构：新增/删除页面、引用仍使用独立结构工具；当前重载回退尚未全部替换。
- 符号映射：标准映射以 TcHmiSrv.Config 的 SYMBOLS/MAPPING/SCHEMA 为准；ADS.Config 的 RUNTIMES.SYMBOLS 是另一类固定地址配置，不应由 PLC TMC 类型直接拼装。
- Server 配置：与页面编辑分开。文件回读不等于 Server 成功加载；之前的运行时恢复问题仍需要完善事务与健康检查。
- 控件事件：hmi_events.py 按 packages.config 精确版本读取 Description.json 与基类事件。支持读取、预览、upsert、remove；保持其他事件及原有 preventDefault 等字段。常规事件默认保存为 `.onName`，显示在 XAE 下方 Framework/Operator/Control 原生事件组；只有显式 `placement=custom` 才保存为 `ControlId.onName` 并显示在上方 Custom。

## 入口

`hmi events <file> <control_id>` 列出支持事件及现有 Trigger。

`hmi events <file> <control_id> --action upsert --event .onPressed --placement native --actions-file actions.json --apply`

MCP/Agent：`tc_hmi_control_events`。动作支持 JavaScript、WriteToSymbol、Function、ControlApiFunction；目前是有限结构检查，不宣称完整 Framework schema 校验。编辑不执行事件。工具先从当前工程精确安装的 `Description.json` 返回 `available_events/recommended_events`；`Press` 等猜测名称在 COM 写入前拒绝。

`placement=native` 是默认值。旧版工具写成 `StartButton.onPressed` 的 Trigger 会在读取时标记
`migration_recommended=true`；对同一事件执行 native upsert 会保留动作及 `preventDefault` 等元数据，
同时把事件键迁移为 `.onPressed`。删除操作严格按 placement 匹配，避免误删明确保留的 Custom 事件。

省略 HMI project 时，Python 桥先绑定当前工具调用的精确 XAE PID，读取该 PID 的解决方案，
再从 `.sln` 解析实际 `.hmiproj`（支持嵌套目录）。唯一工程自动补全为绝对路径；多 HMI 工程
必须显式指定 project，不能按 DTE 项目顺序猜测。

动态 ADS 符号低层接口允许规范化 camelCase/大写键和基础 PLC 类型，但必须完整提供
`DOMAIN=ADS`、`DYNAMIC=true`、`USEMAPPING=true`、`MAPPING` 与 `SCHEMA`。缺项时返回完整
示例并建议优先使用 `tc_hmi_bind_plc` 从实际 TMC 生成；不猜 IndexGroup/IndexOffset。

`tc_hmi_browser_validate(entry_page="Main.view")` 通过临时隐藏浏览器调用官方
`TcHmi.View.load` 加载明确的、已保存且已登记页面。结果返回 `loaded_view`、保存页面控件/绑定
数量及 `saved_target_page_verified`；只读采集，不点击、不写 PLC、不启停 Server。

当前 script 子节点存储的 Trigger 会明确拒绝修改，避免破坏用户内容。在线交互测试未完成前，不把配置写入成功称为事件执行成功。

## 2026-09-05 验证记录

8 项单元测试通过；本机 Framework native1.12-tchmi / Controls 14.4.1 描述解析成功（71 类）。以下事件在真实安装包描述下完成离线生成与删除检查：Button.onPressed、Checkbox.onToggleStateChanged、Textbox.onTextChanged、NumericInput.onValueChanged、Combobox.onSelectionChanged、ProgressBar.onAttached。

XAE PID 20656 在线验证完成：修正 TextView GUID 为 `7651A703-06E5-11D1-8EBD-00A0C90F26EA`（701 是 Code View），并等待异步 OpenFile 完成。六类控件及事件均通过 DTE TextDocument 保存并全文/事件回读，`project_reloaded=false`。HMI 构建 `failed_projects=0`。

`scripts/test_hmi_demo_events.ps1` 在独立隐藏浏览器中通过官方 `TcHmi.View.load('AgentTest.view')` 加载测试页，不改变工程启动页。Button 使用浏览器鼠标事件；Checkbox/Textbox/NumericInput/Combobox 使用公开 setter；ProgressBar 验证 onAttached。六项执行计数检查全部通过，未捕获 JavaScript 异常。未触发 PLC 启停，也不代表其它动作类型或所有控件已完成运行测试。

官方 View API：https://infosys.beckhoff.com/content/1033/te2000_tc3_hmi_engineering/3728975115.html

## 2026-09-05 全控件覆盖

- 本机安装包目录共解析 71 个控件定义，Framework 为 `native1.12-tchmi`，Controls 为 `14.4.1`。
- 63 个工具箱公开控件和 1 个 Beckhoff 隐藏兼容控件，共 64 个具体类型，全部通过 DTE 添加、ID 回读和 HMI 构建；隐藏浏览器中 64/64 完成注册并触发 `onAttached`，没有缺失类型。
- Button、Checkbox、Textbox、NumericInput、Combobox、ProgressBar 的专用交互事件仍为 6/6 通过；其它具体控件本轮验证的是通用生命周期事件，不代表其每个专用事件、每项业务属性都已覆盖。
- 7 个 `TcHmi.Controls.System` 隐藏类型是框架基类或根/宿主类型，不是工具箱可直接创建项。Control、ContainerControl、Partial、View、Content 已由具体派生控件和现有 View/Content 间接覆盖；UserControl/UserControlHost 的结构创建测试暴露 XAE 自定义 Project System 重载缺陷，事务已回滚，未记为运行通过。
- RecipeEdit 和 RecipeSelect 本体均成功创建、注册、附加；但当前项目没有 `TcHmiRecipeManagement` Server domain，浏览器记录 14 条相关错误。因此“控件生命周期覆盖”通过，“配方业务能力覆盖”阻塞，不能混报成全功能通过。
- 测试页为 `AgentTest.view`，启动页 `Desktop.view` 未改变；测试未写 PLC 符号。生成器为 `scripts/create_hmi_control_coverage.py`，运行验证为 `scripts/test_hmi_demo_events.ps1`。
