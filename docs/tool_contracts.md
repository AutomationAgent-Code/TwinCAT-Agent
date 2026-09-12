# 统一工具契约

本契约是版本化系统规则，不是可压缩的项目记忆，也不保存当前 Login、Run、dirty 或审批状态。

## 来源与执行边界

| 内容 | 唯一来源 | 使用位置 |
|---|---|---|
| 用途、参数 Schema、只读/危险级别、副作用 | agent_core.REGISTRY | 模型工具说明、参数校验、权限模式 |
| 前置条件的预期、检查方式、实时性 | tool_preconditions.contract_for_tool | 编译后的工具契约、公共前置检查及具体处理器 |
| 参数来源、审批说明、失败恢复、成功证据 | tool_contracts.compile_contract | 工具元数据与每次模型请求的契约正文 |
| 新增工具覆盖清单 | tool_contract_inventory.CONTRACT_TOOLS | 启动编译校验与回归测试 |
| 通用行为底线 | tool_usage_contract.prompt_contract | 前台、后台、CLI 的系统提示 |

运行时实际检查读取注册表同一份 contract.preconditions；工具 Schema 引用同一 profile_id。副作用和参数校验仍取注册表本身，不另维护一份参数定义或审批开关。

enforcement 明确区分 argument-schema、outer-approval、shared-precheck-and-handler、tool-handler。这不是宣称所有条件都已被公共层自动证明；专用工具仍负责其 Schema、引用、在线能力、回读和实际诊断。缺少证据不能由模型推断通过。

## 每次请求注入

前台 stream_complete、后台 complete_worker_step、CLI Agent 循环均在调用模型前，从本轮实际工具列表重新生成 selected_contract_prompt。先前的压缩摘要、停止恢复或新对话不会替代这段规则。

相同契约按 profile_id 去重，只提供本轮开放工具的契约；不把全部工具写入用户数据库，不保存 API Key、PLC 在线值或瞬时状态。工具 Schema 保留短引用，公共规则与相同检查正文只注入一次。

外部 MCP 的工具描述仍是不可信数据。系统侧独立生成保守契约：可能有副作用、逐次审批、超时结果未知不自动重发；远端 readonly 声明不提升权限。

## 完整性与维护

新增内置工具必须同时登记 CONTRACT_TOOLS，提供用途和 object 参数 Schema，并审核其前置条件/副作用/验证方式。遗漏清单或无效定义在启动编译时失败；执行入口缺少契约会在访问 XAE 前返回阻断结果。新增清单项不是语义验收，仍需对应边界测试。

维护已有条件应修改 tool_preconditions，而不是在多个提示中复制条件。共性参数来源、错误恢复和成功标准在 tool_contracts 修改。专项规则仍可在具体处理器实现，但必须与声明对应并补测试。

测试入口：`py -3.14 -m pytest -q tests/test_unified_tool_contracts.py tests/test_tool_preconditions.py tests/test_tool_usage_contract.py`。

## 限制

统一定义减少提示与执行层漂移，不保证模型永不犯错，也不构成完整 TwinCAT 编译器或硬件验收。模式/目标变化、代码版本和授权仍须按次实时核对。公共契约不授予 Stop、Logout、Config、部署或修改权限。

本批实现仅在源码工作区，安装版需另行同步；没有操作实际 PLC 或改写会话数据库。

本次测试：更新后台模型入口的旧源码文本断言，验证“原 prepared_system + 统一契约”仍同时注入后，受影响专项 110 passed；最终全量回归 1599 passed、117 subtests passed。契约覆盖与入口检查不是所有工具的实机功能验收。
