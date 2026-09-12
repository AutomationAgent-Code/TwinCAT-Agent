# TwinCAT Agent Runtime 架构

## 当前执行边界（v1.0.8.107）

TwinCAT Agent 将“模型消息历史”和“实际执行事实”分开保存：

1. WebView 只负责提交用户请求、显示事件和完成权限确认。
2. Provider 适配器负责把中立消息转换为 OpenAI Chat Completions、Responses 或 Anthropic 协议；
   Responses 的加密继续状态随消息持久化，仍由同一工具执行循环完成审批和结果回传。
3. Agent 循环负责规划、权限判断和 Action/Observation 编排。
4. XAE 工具通过目标 PID 串行进入 Automation Interface/ADS 边界。
5. 项目级 `.TwinCATAgent/agent.db` 使用 SQLite + WAL 保存消息与执行账本。

执行账本采用四层结构：

- `agent_runs`：一次用户请求的生命周期、模型、目标 XAE、token 和最终状态；
- `agent_steps`：每次模型步骤的输入摘要、输出、状态和错误；
- `tool_executions`：工具、参数、风险、目标 PID、幂等键、结果和执行状态；
- `approvals`：权限请求、用户决定和关联工具执行。

内部工具协议为版本化 `ActionRequest` / `ActionResult` 信封。现有 WebView 事件和
模型工具 schema 保持兼容，不要求 Provider 或 UI 同步改版。

前台对话与兼容 Worker 共享两个运行时原语：

- `DurableRun`：统一创建/结束 Run、模型 Step、token 统计和中断状态；
- `DurableAction`：统一工具预约、幂等重放、拒绝、失败和 `uncertain` 处理。

两条循环仍保留不同的调度和权限策略，但不再各自实现执行事实状态机。Run/Step/Tool
终态采用单向转换，迟到的取消或异常不能把已完成操作反向覆盖为未知状态。

## 中断与幂等规则

- 同一 Run 中相同 `tool_call_id + tool_name + arguments` 产生稳定幂等键。
- 已完成调用再次出现时直接读取账本结果，不重复进入 XAE。
- Agent 在工具运行中退出时，该调用恢复为 `uncertain`。对于 PLC/项目/运行时写操作，
  不允许自动重放，必须先读取实际工程状态再决定补偿或重试。
- 重启恢复会依据账本补齐消息历史中缺失的 `tool_result`，避免严格 Provider 因孤儿
  `tool_use` 拒绝上下文，也避免模型把未知写操作当作从未执行。
- 审批在重启时自动过期，旧面板的审批响应不能进入新 Run。

## 工具契约

`agent_core.REGISTRY` 是内置工具的单一真源，数量以运行时注册表为准。后端在此基线之上按需合并
外部 MCP 工具；外部工具不改变内置工具编号，也不写回内置注册表。每个工具除 JSON
Schema 外还包含：

- `protocol_version`：内部执行协议版本；
- `side_effect`：`none/project/target/runtime/file/system` 等副作用范围；
- `idempotency`：只读工具为 `safe_replay`，写工具为 `ledger_guarded`；
- `category`、`readonly`、`danger`：用于动态筛选和权限门。

SYSTEM/实时配置工具遵循额外的两阶段契约：`tc_system_structure`、
`tc_system_settings`、`tc_core_info` 和 `tc_task_info` 只读 XAE 当前对象；
`tc_system_settings_set`、`tc_core_assign`、`tc_task_core_assign`、
`tc_system_add`、`tc_system_remove` 默认只返回差异预览，只有 `apply=true` 才执行
`ConsumeXml/CreateChild/DeleteChild`。这些工具在 Agent 接口层标记为 `danger=system`，
因此在 `accept` 和 `auto` 模式下都必须经过用户硬确认。系统树写入限制在
`TIRC/TIRT`，TIRS 只能通过白名单设置接口修改；每次写入都必须回读校验，
不负责自动激活或重启。

## 后续分层

以下改造必须建立在执行账本稳定之后，不与本版本混成一次高风险重写：

1. 把前台与兼容 Worker 的调度外壳进一步收敛到可配置的单一 `AgentRuntime`；
2. 将现有按调用隔离的 32/64 位 COM Helper 演进为可监控、可重启的 XAE Broker；
3. 为 build→static analysis→online verify 等确定性流程增加工作流节点；
4. 基于 Run/Step/Tool 主键输出 OpenTelemetry trace，并提供脱敏 Replay；
5. 审批支持参数编辑、差异预览和策略化批量授权。

多 Agent 不是当前优先项。TwinCAT 工程保持单写者原则；并行能力优先用于只读文档、
代码审查和诊断，写入统一回到同一个 XAE 执行边界。

## XAE 进程隔离

安装版只要存在架构匹配的 `runtime/com32` 或 `runtime/python`，所有 Automation
Interface 调用（包括与 Agent 主进程同位数的 XAE）都通过一次性 Helper 子进程。
Helper 绑定精确 XAE PID，并由调用超时控制；COM 卡死时终止 Helper，不阻塞 8765/8766
主服务。`TC_AGENT_COM_HELPER=direct` 只保留给源码环境的显式诊断，不是产品默认路径。

本地更新必须同时更新 `app/tc_template` 和 `runtime/com32/app/tc_template`；后者
在 32 位解释器中优先导入，遗漏会造成主后台已更新、COM 工具仍执行旧代码。
`Update-LocalInstallation.ps1` 将辅助代码纳入同步和备份，不要求替换解释器本身。
源码更新可供两种解释器加载；仅字节码安装包必须提供匹配的 32 位辅助包，禁止将
主后台 Python 3.14 的字节码复制给 Python 3.12。验收需从安装版发起精确 PID 的
只读调用，不能只核对主后台文件或版本号。

## 确定性验证工作流

`plc_verify` 不让模型自由拼接验证步骤，而是固定执行：

1. `com_build(always_read_errors=True)` 获取真实编译诊断；
2. 对当前全部 PLC 对象执行 Agent 静态分析；
3. 用户提供断言时，通过 ADS 读取在线符号并检查 `expected/tolerance`。

编译或静态分析出现硬错误时，第 3 步明确标记 `skipped`。v2 拒绝空 PLC 对象列表、
缺失的编译计数及静态诊断字段，并区分 `source_verified`、`runtime_values_verified`
与 `runtime_matches_build`。目前没有在线构建身份比对证据，后者返回 `null`；即使
源码检查和在线值断言都通过，带在线断言的请求仍返回 `status=incomplete`、
`verified=false`，不得声称本次修改已在线验证。没有提供在线断言时，`verified`
只覆盖源码编译和 Agent 静态分析。工具不会为验证自动下载或启动 PLC。

## 审计修复契约（2026-09-06）

### HMI 创建恢复与错误保留

`tc_hmi_create_project` 按创建模板、等待文件/加载、保存解决方案、回读验证分阶段运行。
默认通过原生 Python 动态 DTE 执行，避免 PowerShell 在 TcXaeShell HMI Project 上的
`Specified cast is not valid` 属性读取问题；没有原生桥时保留保守的 PowerShell 回退，
无法确认已有项目精确身份时拒绝修改，不以同名或项目顺序猜测。
非幂等的 `AddFromTemplate` 每次新建仅调用一次；报错后检查副作用，不盲目重试向导。
仅对明确的 COM busy 错误重试保存；最终同时验证有效 `.hmiproj`、XAE 精确路径与 `.sln` 登记。
对新建且匿名的 HMI Project System，允许保存解决方案后再解析身份，但不会提前报告成功。
重复请求若发现同路径的有效项目已经加载，会接续保存并返回 `recovered`；磁盘孤立目录、
损坏文件或不同路径的同名项目保持原样，返回 `incomplete` 和 `phase/state/next_action`。
保存只针对解决方案登记，不调用 `File.SaveAll` 强制保存其他修改中的编辑器。
本机 Framework 模板清单允许在没有 HMI 项目时读取；没有活动工程的版本信息留空，不猜版本。
HMI 结果摘要和历史压缩保留失败标志及错误信息；大结果降级时也不得伪装为空控件列表。

### 执行、缓存与发布

- 工具线程在进入执行器和获得 XAE 锁后检查取消令牌，已取消的排队操作不得进入 COM。
  已进入 COM 的调用无法安全强杀，仍按 `uncertain` 保留执行记录，取消不等于回滚。
- 编辑器缓存以 PID、解决方案、文件和成员隔离，声明/实现分别存储；未知区域文本不返回。
  显式工程路径必须精确匹配。缓存事件只触发绑定 PID 的回读，并核对解决方案、文件及成员。
- PLC 工具不复用整轮结果；保留 SQLite 增量索引与批内去重，确保用户手动保存后重新校验。
  磁盘读取也使用任务绑定的解决方案，不共享进程级的解决方案路径缓存。
- `.sln` 读取只沿其 `.tsproj/.xti/.plcproj` 引用发现 PLC；传目录才启用离线扫描。
  离线扫描排除 `.TwinCATAgent/.updates/.git/.codex/_Boot/bin/obj`。
- 发布脚本、后端清单地址及前端下载白名单固定一致：`AutomationAgent-Code/TwinCAT-Agent`。
  不修改宿主注册、pkgdef、扩展安装位置或后端启动方式。

## 执行策略与故障恢复（2026-09-06）

`tc_agent/execution_policy.py` 集中保存读取去重与结果成功判定，前台和 Worker 使用
相同的结果判定。`error/denied`、`ok/success/verified=false`、失败状态、非零构建错误计数、
待补齐诊断和 MCP 的 `isError=true` 均不能视为成功；
MCP 大结果限长时仍保留失败标记，不会被记成成功执行。

- 自动工具路由按当前意图加入文档、版本与外部 MCP；用户在界面指定的类别仍优先。
- “继续”“1”等简短回复从最近会话选择工具，必要时参考压缩摘要；明确新任务按新请求选择。
- 只对已成功的已保存源码读取去重；失败读取可重试，精确筛选和翻页视为新请求。
- 在线变量、运行状态、编译与 `live/refresh` 请求允许复测。重复的静态读取被拒绝后
  计入连续失败阈值，防止模型把跳过结果当成进展而无限循环。
- 获准写入进入执行前就清除读取缓存和去重记录；即使写入部分失败，也不会拿旧结果验证。
- Worker 不复用前台会话读取缓存，避免跨对话、跨目标缓存混用。
- MCP 连接失败后按 30/60/120/240/300 秒退避；配置变更或显式强制刷新可提前重试。
  连接与进程等待在目录锁之外执行，工具目录与设置状态仍可读取。

这些策略保证执行事实一致，不替代真实 XAE/PLC 在线验收。实时轮询的停止条件仍由
任务策略约束；没有把所有相同在线读取视为死循环，也没有自动重试写操作。

### 工具选择与大结果裁剪（2026-09-07）

自动路由现在采用两级选择：先按 PLC/HMI/I/O/NC/Safety/System 等领域选类别，再对
工具数量最多的 HMI 类按当前意图选子集。页面编辑、事件、ADS 绑定、浏览器验收、主题、
本地化、UserControl 与 Framework 包不再默认同时暴露。用户在界面手动指定类别时仍保留
完整类别，不被自动子集覆盖；“HMI build”也不会因通用 `build` 一词误加载整套 PLC 工具。
文档、版本和外部 MCP 只在请求明确涉及相应能力时加入，减少无关 Schema 对模型选择的干扰。

结果裁剪只影响模型上下文和超大 UI 预览，完整执行结果仍保存于执行账本。通用摘要优先保留
`status/error/phase/verified/build_succeeded/diagnostics_*/next_action`，列表不再只截取开头，
而是优先保留位于任意位置的 error、warning 和失败项。PLC 源码及 HMI 页面继续走专用的
连续分页器，不做首尾拼接；游标按模型实际收到的原子记录或 UTF-16 内容长度生成。

## 快速读取程序

`plc_read_fast` 默认读取已保存源码索引；`live=true` 时采用一次 Helper、一次
DTE/ROT 连接的批量实时读取策略：

- 每批最多 16 个 POU/GVL/DUT/接口或 FB 成员；
- 每项独立指定精确树路径、成员、声明/实现区和行分页；
- 返回各区域 SHA-256；客户端下次传 `known_hashes` 时，未变化区域不再返回源码；
- 整批有统一字符预算，防止大型项目源码挤爆模型上下文；
- 实时模式读取 XAE 当前内存对象，能看到用户尚未落盘的手工编辑。

推荐流程是一次 `plc_find` 定位相关对象，再用一次 `plc_read_fast` 批量读取，而不是连续
调用多次 `plc_read`。哈希机制减少跨进程传输和模型 token；需要未保存内容或写后验证时
必须明确选择实时模式，不把已保存源码当作实时读回。
