# HMI 控件批量编辑

新建页面使用 `tc_hmi_create_view.controls` 批量提交；既有页面的多个目标使用
`tc_hmi_controls_batch`（Agent 和 MCP 均提供），单个目标仍用 `tc_hmi_control_edit`。
不要并行写同一页面。此功能不改变 XAE 嵌入、工程重载或在线运行策略。

参数为 `project`、`file`、`operations`、`apply`（默认 false）。每批 1..100 项，
每个目标 ID 只出现一次，按顺序执行，先添加父控件再添加子控件。
每项包含 `action: add/update/remove`、`control_id`，以及可选的
`control_type`（add 必填）、`parent_id`（仅 add）、`attributes`。
属性必须为完整 `data-tchmi-*` 名称；值是字符串或 null（删除），复杂值为 JSON 字符串。
事件仍使用专用事件工具，不通过猜测事件结构加速。

本地脚本可用 `tc_template.hmi_events.control_events_batch(file, edits, project, apply)`
批量编辑同页原生事件。每项只接受 `control_id/event/actions`，每批 1..100 项，
禁止重复控件/事件组合；逐项检查本机事件声明与动作结构后，合并同一控件的 Trigger，
通过受保护的批量编辑保存一次并完整回读。它不接受任意控件属性或自定义 placement，
也不会绕过 Schema/源文件哈希门禁。该入口是本地库函数，不代表新增 Agent/MCP 注册工具。

流程：参数检查 → 一次页面预览 → 一份当前安装包 Schema → 批量目标校验
→ 校验项目/源码/Schema 文件指纹 → 一次 DTE 备份、保存及整页精确回读。
只校验本批新增/修改控件的属性，仍检查全局控件 ID；不强迫修复无关旧控件。
删除父控件与本批其他目标冲突时拒绝，不能先写部分结果再报错。
预检失败不写入；编辑器未保存内容或预检后发生变动时拒绝覆盖。
写入失败沿用 DTE 写入函数的恢复原文尝试及备份，不保证 XAE 故障时回滚一定成功。

结果包含 `operation_count` 和逐目标 `operations`，应用成功时逐项 `verified=true`；
这基于整个候选文档的编辑器及 UTF-8 磁盘回读，不仅仅检查 ID 存在。
不自动重载工程、启动 PLC 或 HMI Server；浏览器和 ADS 值仍需另行验证。

提速来源是减少模型往返和重复读包、预览、保存。没有跨请求的长期 Schema 缓存，
每批重新建立版本与文件指纹，避免复用过期规范。实际整页生成耗时仍需现场验收。
