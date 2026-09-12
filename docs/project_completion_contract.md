# 工程生成与验收证据契约

2026-09-07：Project11 对话审查回归。

- HMI 生成上下文保留完整 `data-tchmi-*` 属性名；复杂值使用本机 schema 的结构字段，不靠尝试失败猜字段。Schema 裁剪只去描述/工程 UI 元数据，不去校验约束。
- PLC 准备阶段附带已保存任务入口、空入口以及没有直接程序调用证据的 PROGRAM。此检查不是完整编译器调用图，也不包含未保存内容，不能把间接调用或未提供内容武断判为不存在。
- 前台执行按修改/验证顺序记录证据。再次修改会使之前的编译和验证证据失效。最终回复附上未闭环项；静态结构通过、编译通过、符号映射和在线行为必须分开陈述。只读对话、快照和预览不触发源代码修改验收。
- Agent 的 `tc_hmi_ads_symbol_set` 保留预览和删除，禁止直接应用模型填写的静态数字地址；新增 PLC 绑定使用 `tc_hmi_bind_plc`。CLI 底层静态地址工具仍供明确知道地址的人工工作流使用，不代表地址已验证。
- TMC 找不到预期符号时检查任务调用和编译导出，不能填 `16448/0` 等地址绕过。数组绑定使用真实下界与符号路径，例如 `Root[0]::member`；保存的根映射匹配不证明元素类型/在线值已经验证。[Beckhoff SymbolExpression 路径](https://infosys.beckhoff.com/content/1033/te2000_tc3_hmi_engineering/4707239947.html)。
- PLC 回读只允许规范化 CRLF 和末尾行结束符；代码内部内容、空格差异仍须报告。`written=true, verified=false` 不得自动重放，应先回读。
- HMI DTE 写入同时检查编辑器内容和严格 UTF-8 磁盘内容。旧 ASCII 页继承 ANSI 编码时，仅在刚保存、回读一致且没有未保存编辑后关闭该文档，规范为 UTF-8 BOM 并重新打开验证。保留写前备份，不修改全局编辑器配置。[Microsoft 文件编码](https://learn.microsoft.com/en-us/visualstudio/ide/how-to-save-and-open-files-with-encoding?view=visualstudio)。
- VSIX 的 `errorsRead` 表示成功枚举诊断，包括空列表，不再表示“错误条目非零”。不可用计数为 null，`diagnosticsAvailable=false`。旧 UI-thread 成功枚举协议由 Python 客户端兼容规范化；不把未知来源的空结果当成功枚举。
- COM `GetTypeInfo` 的忙 HRESULT 必须传播到有界重试，避免 pywin32 吞掉 RPC_E_CALL_REJECTED 后误报 `.Solution` 属性不存在。XAE 长期忙时报告原因，不自动关闭弹窗、重启 XAE 或启动 PLC。
- SQLite 和历史适配器共享结构化恢复逻辑：保留原始任务、实际错误分类、有限执行摘要，不递归包裹恢复请求，不复制 provider 私有推理字段。余额/模型能力/配置问题不是网络重试问题。

范围边界：源代码修改、构建、静态验证不会自动授权下载、登录、启动、激活或真实设备操作。在线验收应绑定实际目标和项目版本，另经用户授权。
