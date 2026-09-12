# TwinCAT Agent 不可回退清单

本文件定义已经由测试机确认、后续版本不得无意修改的产品行为。需要有意改变时，必须先说明影响、更新对应测试和开发记忆，再递增版本发布。

## 1. 已授权界面

- 未授权时显示授权页、TwinCAT System ID、授权码输入框和当前软件版本。
- 已授权后隐藏授权输入页，但顶部必须继续显示：
  - `vX.Y.Z.W` 软件版本；
  - `永久授权`、`授权至 YYYY-MM-DD` 或明确的到期状态；
  - 鼠标悬停可查看客户名称、授权编号和到期日期。
- 后端每个 `license_status` 事件必须包含 `app_version`、`device_id`、`valid`、`customer`、`license_id`、`expires_at`。
- 客户包必须包含 `app/VERSION`；版本文件、安装包文件名和 EXE 版本资源必须一致。

## 2. 嵌入面板与连接

- WebView2 从 `http://127.0.0.1:8766/?xae_pid=<PID>` 加载，不恢复为 `file://`，也不允许后端放宽接受 `Origin: null`。
- 页面必须携带当前 TcXaeShell PID；所有 COM 工具严格绑定该 PID，多个 XAE 实例之间不能串项目。
- 拖动、停靠和重新挂载面板后必须恢复显示，不能白屏，也不能因此重新加载错误项目。
- 后端以普通用户身份运行，并与 XAE 保持相同完整性级别；不得改为长期管理员自启动。

## 3. 会话、停止、权限与模型切换

- 每个解决方案使用独立历史文件，切换项目不得显示其他项目历史。
- 停止按钮立即给出反馈；取消必须穿透模型流、权限等待和 Agent 循环。
- 取消或切换 Provider 时回滚本轮未完成上下文，并关闭旧权限卡；旧权限响应不得进入新轮次。
- 切换 Provider 前先停止旧轮次；每个模型请求使用不可变 Provider 快照，禁止旧上下文串到新模型。
- 删除 PLC 对象或成员前必须执行引用预检；默认阻断仍有引用的目标，只有显式 `force` 才可越过。
- 顶层对象删除/重命名必须接受 `plc_find` 返回的精确树路径，重名时禁止猜测目标。
- 快照恢复默认只预览；执行恢复前创建备份，恢复后编译，失败或异常必须自动写回操作前代码。
- `ready` 事件不得在轮次仍忙时错误启用发送按钮。
- 权限模式固定为：
  - `plan`：只读，拒绝改动；
  - `ask`：所有改动逐项确认；
  - `accept`：代码、快照和非破坏性项目编辑自动执行，其他操作确认；
  - `auto`：无 `danger` 的低风险改动自动执行，`project/target/runtime/file/system` 风险仍确认。
- 自然语言中的读/写/启动关键词不再作为独立硬拦截；它只移除了额外的文本分类限制，
  不改变上述权限模式、人工确认、目标/PID/工程校验、PLC/HMI 前置门禁、回读或 TwinSAFE 硬确认。

## 4. 工具与原生桥

- 工具真实清单只以 `tc_agent/agent_core.py::REGISTRY` 为准；当前基线为 139 个工具。
- Agent 使用的每个 `ps_com` 命令都必须由 32 位原生桥支持，缺失数必须为 0。
- SYSTEM 工具只允许读取 `TIRC/TIRS/TIRT`；通用 CreateChild/DeleteChild 写操作只允许
  `TIRC/TIRT`，并且默认返回 preview，必须显式 `apply=true` 才能变更。
- `tc_system_settings_set`/`tc_realtime_settings_set`/`tc_core_assign` 只允许核分配以及
  `RouterMemory`、`MaxStackSize`、逐核 `BaseTime/LoadLimit/LatencyWarning/CpuMemorySize`
  白名单字段；4024 禁止 `CpuMemorySize` 且 Router Memory 不得超过 1024 MB，4026 将
  Router Memory 解释为 Global RT Memory 并单独报告 ADS Memory 语义。`tc_core_assign` 未显式指定 `Affinity`
  时按 `cpu_ids` 自动生成；写入后必须通过 TIRS ProduceXml 回读，
  不匹配时返回失败，不能假报成功。
- `tc_task_settings_set` 必须拒绝重复优先级，并校验任务 Cycle Time 不小于 Base Time 且为其整数倍；
  无法确认任务所在核时，只能在所有候选核均兼容或明确返回警告的情况下预览/写入。
- CPU/任务核分配不自动切换 Config、激活或重启；需要生效时由用户明确执行部署/重启流程。
- 所有 XAE COM 工具（含 NC/Motion）必须按目标 XAE PID 的真实位数选桥：IDE 15 x86 走 `runtime/com32`，IDE 17 x64 走 64 位运行时；禁止跨位数访问 ROT。
- 安装版即使 Agent 与 XAE 同位数也必须通过内置 Helper 子进程执行 COM；不得因同位数
  回退到后端主进程直接 Dispatch。工具超时只能终止 Helper，不能拖死 HTTP/WS 服务。
- 两种桥都必须包含其运行路径所需的 `pywin32` 与 `pyads`，并分别定位同位数的 `Common32/TcAdsDll.dll` 或 `Common64/TcAdsDll.dll`；不能依赖系统 PATH。打包阶段必须分别执行导入冒烟测试。
- 写操作不能吞异常或返回假成功；错误结果必须包含 `error` 或明确的失败状态。
- `tc_config_mode`：先 TIRS ConsumeXml，失败后真正调用 `TwinCAT.RestartTwinCATConfigMode`；仅 ADS 状态 `7/15` 算成功。
- `tc_run_mode`：仅 ADS 状态 `5` 算成功。发送 COM 请求不等于切换成功。
- 普通读取、代码编辑和编译工具不得修改 XAE 全局 `SilentMode` 或 `DTE.SuppressUI`。确需抑制确认框的运行时、部署或扫描流程只能在调用期间临时开启，并必须在成功、失败和提前返回后恢复用户原值。

## 5. I/O 组态

- I/O 主站按 Automation Interface `ItemType == 2` 识别，不能把 `Image/Inputs/Outputs/InfoData/Term` 当作主站。
- 空 I/O 树（真实 Device 数为 0）必须允许创建第一个主站。
- 空 I/O 下 `tc_io_structure` 返回 `master_count: 0`、`empty: true`，不得报错。
- 空 I/O 下 `tc_io_export` 不生成无效 manifest，也不覆盖目标文件。
- 多主站下 `tc_io_structure` 返回全部主站；写清单时通过完整 `master` 名称选择，错误信息必须返回未截断的可选名称。
- 离线清单创建、删除、扫描都不得隐式激活配置、重启 Runtime 或切换 Config/Run。

## 6. 文档、上下文与大项目

- `docs_search/docs_read` 完整结果用于 UI，注入模型历史前必须摘要；不得把大段原文反复塞入上下文。
- 写依赖 Beckhoff 库 API 的 PLC 代码前先搜索官方文档。
- 大项目优先 `plc_find` 精确定位，使用分页 `plc_read` 和局部 `plc_patch`；不得默认展开并传输全项目源码。
- 同时读取多个相关对象时优先使用 `plc_read_fast`，保持单 Helper/DTE 往返、每项分页和
  整批字符预算；快速读取必须来自 XAE 实时对象，禁止用磁盘 XML 缓存替代。
- 单对象日常上下文读取优先使用 `plc_read_smart`：磁盘模式必须以 `.plcproj` 的
  `Compile Include` 精确定位并返回来源、哈希、路径和耗时；验证、写后反读和未保存编辑
  必须使用实时 COM。磁盘结果必须标记 `dirty_unknown`，不能冒充 XAE 权威内容。
- PLC 写入与 patch 必须独立执行实时 COM 反读并校验目标区域；`plc_verify` 必须先取得
  实时 XAE 全项目代码，再执行编译和静态分析。
- `plc_read_current`/`plc_dirty_current` 必须仅访问 XAE 当前活动 PLC 文档：优先由
  活动文件的 `POUs/DUTs/GVLs/...` 相对路径直接定位 TreeItem，不得为此调用全项目
  `list/all-code`；目录树无法对齐时才允许按名称回退。`plc_dirty_current` 必须返回
  文档 `saved` 状态和实时/磁盘的正文差异，且忽略仅末尾空白的格式化差异。
- 每个模型 `tool_use` 必须紧跟对应 `tool_result`；空 assistant 消息不得写入历史。
- 每个解决方案的多对话和共享记忆只存入 `.TwinCATAgent/agent.db`；不得跨项目复用数据库。
- 每个前台请求必须产生持久化 Run；模型步骤、工具执行和审批必须关联到该 Run。
- 已完成工具调用允许从账本重放结果；`running/uncertain` 写操作不得自动再次进入 XAE。
- Run、Step 和 Tool Execution 一旦进入终态不得被迟到的取消、超时或异常反向覆盖。
- 前台与兼容 Worker 必须共用 `DurableRun`/`DurableAction`；不得重新复制另一套
  工具执行、幂等或终态转换逻辑。
- Agent 重启必须把未完成 Run/Step 标记为中断、使待审批过期，并根据工具账本补齐
  孤儿 `tool_use`，不得仅依赖“输入继续”的文本恢复。
- 产品不提供后台 Worker/子任务入口；保留旧数据库记录仅用于兼容和审计，禁止自动恢复并产生 token。
- 不恢复固定 turns 上限。
- XAE/COM 调用按目标 XAE PID 串行；不同 XAE 实例不得共用一把全局锁。

## 7. 发布门禁

- 每次发布必须先运行完整 `pytest`；测试数量和通过基线以对应提交的受控 CI 结果为准，不在文档中维护容易过时的固定通过数。发布证据必须保存 CI 链接或测试报告，并确认没有收集错误、失败或非预期跳过。
- UI 单一真源为 `tc_agent/static/index.html`，打包时同步到扩展副本，禁止从旧副本反向覆盖。
- 每次修复递增 `VERSION`，重新生成安装包并核对文件版本与 SHA256。
- 不执行 TcXaeShell `/setup`、`/updateConfiguration`，不直接修改私有注册表数据库。
- 不将 API Key、RSA 私钥、客户授权文件、聊天历史或项目私密数据打入安装包。
- Windows 启动项必须启动常驻的单实例系统托盘控制器，不能退化为“启动后端后立即退出”的普通启动器。托盘菜单至少包含服务状态、启动、停止、重启、打开浏览器界面、打开 XAE 和退出；升级/卸载必须先通过 `--shutdown` 退出托盘，避免安装目录文件锁。
