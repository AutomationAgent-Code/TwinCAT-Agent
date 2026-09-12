# TwinCAT Agent 产品开发记忆

本文记录容易在后续会话中丢失、但会直接影响产品稳定性和安全性的技术决策。
修改授权、面板、后端或打包流程前先读本文。

## 当前产品架构

- XAE 内是 32 位 VS Shell 扩展 + WebView2 面板。
- Agent 后端使用自研模型循环，支持 OpenAI-compatible 与 Anthropic Messages 协议。
- 后端监听 `127.0.0.1:8765`（WebSocket）和 `127.0.0.1:8766`（HTTP）。
- 后端必须以普通用户权限运行；提升权限后会因 Windows ROT 隔离而看不到普通权限启动的 XAE。
- 项目历史保存在解决方案目录，Provider/API Key 配置保存在本机，不进入便携包。
- v1.0.8.83 起，项目数据库除消息/摘要外还保存 `agent_runs`、`agent_steps`、
  `tool_executions`、`approvals` 执行账本。重启时必须用账本补齐孤儿工具结果；
  运行中断且结果未知的写操作标记为 `uncertain`，禁止自动重放。
- 内部工具执行使用版本化 `ActionRequest` / `ActionResult` 信封；104 工具注册项必须
  声明 `protocol_version`、`side_effect` 和 `idempotency` 元数据。

## 设备授权

- 客户设备码直接使用 **TwinCAT System ID**，不再自行组合主板、硬盘、网卡或
  Windows MachineGuid。
- 首选读取路径：使用 `TcAdsDll.dll` 连接本机 AMS 地址的 License Server
  （AMS Port `30`），读取：
  - Index Group：`0x01010004`（`IGRP_LIC_SystemInfo`）
  - Index Offset：`1`（`IOFFS_LIC_SystemId`）
  - 长度：16 字节 Windows GUID，使用 little-endian GUID 内存布局解析。
- 后备路径：读取 `%ProgramData%\Beckhoff\TwinCAT\3.1\License` 或旧版
  `C:\TwinCAT\3.1\Target\License` 中 `.tclrs` / `.tclrq` 的 `<SystemId>`。
- 本开发机校验值：`447DAA40-52F5-DB2C-D3F8-3B1804726B7E`；ADS 和许可证文件
  两条路径必须返回相同值。
- 客户端只含 RSA 公钥；私钥只允许保存在外部安全目录，绝不能进入 Git、客户包或
  授权工具包。详细流程见 `docs/licensing.md`。
- 更换设备标识算法会使旧授权码失效；发布前必须同时更新客户端、发码工具和测试授权。
- 供应商发码记录保存在
  `%LOCALAPPDATA%\TwinCAT Agent License Tool\license_records.db`。GUI 和 CLI
  签发成功后都必须自动写入；该数据库包含完整授权码，需要内部保护并定期备份。

## UI 与扩展

- 品牌主色固定为接近 Beckhoff 视觉体系的工业红 `#EF0000` 与白色，立体层次只用
  少量暗红 `#A30000`；Logo 主视觉不再使用蓝色、青色或黄色，也不复制 Beckhoff
  的文字或图形标识。
- UI 单一真源是 `tc_agent/static/index.html`。
- 修改后要同步到 `tc_agent_vsix/webview/index.html`；打包脚本会再次强制同步到
  扩展 staging，避免客户看到旧 Provider/授权界面。
- WebView2 初始化只能执行一次；窗口移动后白屏、首次点击不显示等问题要优先检查
  初始化/可见性恢复，不要重复创建环境。
- VS 停靠/浮动会重建或更换 WebView2 的祖先 HWND。恢复渲染时必须同时调用 WPF
  `UpdateWindowPos()` 和控制器 `NotifyParentWindowPositionChanged()`，并监听
  `PresentationSource`/屏幕坐标变化；必要时只切换控制器 `IsVisible`，不要重建
  WebView2 或重新加载页面，否则会丢失当前 UI 状态。
- 停止一轮时后端必须发 `stopped` 事件，并为已产生的每个 `tool_use` 补齐
  `tool_result`，否则 UI 会一直忙或下一模型请求报 400。

## 文档检索

- 写任何依赖 Beckhoff 库 API 的 PLC 代码前，先使用 `docs_search` / `docs_read`。
- 产品运行只需要 `data/ba-docs/index.db`；它已被 Git 忽略，但完整便携包要包含。
- 更新文档索引执行：

  ```powershell
  & "G:\claude\twin-cat-agent\scripts\update_docs.ps1"
  ```

- 完整更新说明见 `docs/updating_docs.md`。

## Agent 工具注册表

- 客户包实际向模型开放的工具以 `tc_agent/agent_core.py::REGISTRY` 为准，不以
  `AGENTS.md` 或 `tc-template` CLI 的全部命令为准。
- 当前共 48 个工具；项目管理批次包含：
  `plc_create_project`、`plc_delete_project`、`tc_projects`、`tc_project_info`、
  `tc_open`、`tc_close`、`plc_vars`、`plc_search`、`plc_import_plcopen`、
  `plc_export_plcopen`。
- 目标与 I/O 批次包含：
  `tc_target_show`、`tc_target_routes`、`tc_target_set`、`tc_io_structure`、
  `tc_io_manifest_check`、`tc_io_esi_check`、`tc_io_validate`、`tc_io_create`、
  `tc_io_remove`、`tc_io_export`、`tc_scan_devices`。
- `tc_target_set` 只通过 Automation Interface 切换当前系统项目的目标，目标可为
  已有静态路由的名称/IP，也可为六段 AMS NetId；它不新增或删除系统路由。
- I/O 组态采用 JSON 清单驱动的离线 Automation Interface 流程：先检查清单和
  ESI，再创建和校验。不得用硬件扫描代替清单创建，不得自动安装 ESI，不得在创建/
  删除后自动激活、重启或切换 Config/Run。删除只移除清单列出的从站并保留主站。
- `tc_scan_devices` 是用户明确要求时才使用的独立硬件扫描工具。目标必须事先处于
  Config 模式；扫描本身不得隐式切换模式、激活或重启。它只清理由本轮新建且确认
  无从站的适配器，不得清理已有 I/O 设备。
- 扫描前的 Config 判定优先通过 `TcAdsDll.dll` 读取目标 System Service
  （AMS Port 300）的 ADS 状态：`7` 和部分 CP 设备返回的 `15` 都视为 Config。
  TIRS XML 只返回 Started、导致模式为 Unknown 时不得直接报死；若 ADS 探测也暂时
  不可用，可做一次不切模式、不激活、不重启的扫描尝试，让 Automation Interface
  自身返回结果。
- `tc_config_mode` 不得在发送一次 TIRS `ConsumeXml` 后直接宣称成功。正确流程是：
  先请求 TIRS 切换并轮询 ADS Port 300；未进入 `7/15` 时再调用 XAE 官方
  `TwinCAT.RestartTwinCATConfigMode`，继续轮询。只有 ADS 确认后才返回成功，
  两种方法均失败必须返回 error。该工具不得顺带调用 `ActivateConfiguration`。
- 后端向工具执行器传递当前面板绑定的 XAE PID；所有 COM/I/O 工具必须沿用该 PID，
  避免多个 XAE 同时打开时误改其他项目。
- `plc_delete_project`、`tc_open`、`tc_close` 标记为 `danger="project"`；
  在“编辑放行”模式下仍必须逐次确认，只有“全自动”模式才自动执行。
- `tc_target_set`、`tc_io_create`、`tc_io_remove`、`tc_scan_devices` 同样标记为敏感操作；
  “编辑放行”模式仍需逐次确认。
- `tc_open` 只接受已存在的 `.sln`，先保存并关闭当前解决方案，再在同一 XAE
  实例中打开目标，避免另起 XAE 后绑定到错误实例。

## 打包与发布

- 客户一键安装包（首选交付）：

  ```powershell
  & "G:\claude\twin-cat-agent\scripts\build_installer.ps1"
  ```

  产物：`dist/TwinCAT-Agent-Setup-vX.Y.Z.exe`。版本统一读取仓库根目录 `VERSION`，
  文件名和 EXE 版本资源必须保持一致。安装主体放在
  `%LOCALAPPDATA%\Programs\TwinCAT Agent`，仅复制 XAE 扩展时提升权限；后端必须由
  普通用户启动。完整说明见 `docs/installer.md`。

- 便携 ZIP 作为诊断/离线备用：

  ```powershell
  & "G:\claude\twin-cat-agent\scripts\build_portable.ps1" -Zip
  ```

  产物：`dist/TwinCAT-Agent-Portable.zip`。

- 供应商发码工具：

  ```powershell
  & "G:\claude\twin-cat-agent\scripts\build_license_tool.ps1"
  ```

  产物：`dist/TwinCAT-Agent-License-Issuer.zip`，只能供应商内部使用。

- 每次交付前至少验证：
  1. 便携 Python 能读取期望的 TwinCAT System ID。
  2. 新授权码能够签发、验签并完成 WebSocket 授权握手。
  3. 授权工具 `--smoke-test` 能正常退出。
  4. Setup/ZIP 中不存在 `.pem`、私钥、`config.json`、聊天历史。
  5. Setup 的 `--test-install` 解包回归通过，且启动器 `--backend-only` 正常退出。

## 不能重犯的操作

- 不对 TcXaeShell 执行 `devenv /setup` 或 `/updateConfiguration`。
- 不直接修改 TcXaeShell 的私有注册表数据库。

### TwinCAT 4024 / 4026 路径规则

- 4024 的 TwinCAT 默认根目录是 `C:\TwinCAT\3.1`。
- 4026 的程序组件默认在 `C:\Program Files (x86)\Beckhoff\TwinCAT\3.1`，可变数据
  默认迁到 `C:\ProgramData\Beckhoff\TwinCAT\3.1`。
- 4024/4026 的 32 位独立 XAE Shell 通常都在
  `C:\Program Files (x86)\Beckhoff\TcXaeShell\Common7\IDE`。`C:\TwinCAT\3.1` 是
  4024 的 TwinCAT 组件根目录，不能据此断定 TcXaeShell.exe 也位于该目录。
- 把最终命中的准确 exe 路径保存到 `install_options.json`，供安装、启动、卸载全程复用。
- 所有新工具必须按 `TWINCAT3DIR → 4024 根 → 4026 Program Files 根` 探测二进制，
  按 `ProgramData → 安装根` 探测配置/许可/路由数据；禁止只写死其中一条路径。
- 当前扩展为 VS Shell 15/x86，只安装到 `TcXaeShell`，不能复制到
  `TcXaeShell64`。仅有 64 位 Shell 的机器使用浏览器模式，直到提供独立 x64 扩展。

### 模型空响应不能视为成功

- 典型表现：最后一个工具已经返回，界面随后只显示 `N turns`，没有最终文字，也没有错误；
  用户会感觉 Agent “突然不动了”。
- 根因：上游 SSE 连接可能正常结束但没有返回文字或工具调用，旧逻辑把
  `{text: "", tool_calls: []}` 当成正常完成。
- 处理规则：空响应不写入历史、不发成功 `result`；最多自动重试 2 次，仍为空则向 UI
  明确发送错误并解除忙碌状态，同时在 `_backend.log` 记录模型名、次数和最终异常。

### 文档结果采用“双通道”

- `docs_search/docs_read` 的完整结果只发给 UI，方便用户核对原文。
- 写入模型历史的搜索结果必须先生成确定性的提取摘要；工具实际返回多少条就总结多少条，
  不在上下文层额外限制结果数量或单条片段长度。正文摘要最多 1800 字符，并优先保留
  查询词、参数、输入输出和示例相关句子。
- 同一轮重复搜索同一关键词或读取同一路径时，只注入“沿用前文摘要”和来源路径，
  不再重复注入正文。
- 加载旧项目历史时自动把遗留的大段文档正文迁移成摘要，避免升级后继续携带旧包袱。
- 摘要必须保留标题和 InfoSys path，确保模型仍能追溯来源；PLC API 名称采用提取式摘要，
  不额外调用模型改写，避免参数名被“总结错”。

### DeepSeek V4 思考模式兼容

- `deepseek-chat` 走非思考路径，响应通常较快；直接选择 `deepseek-v4-flash/pro` 时，
  官方接口默认开启高强度思考，长时间只有 `reasoning_content` 而没有正文，旧版面板会被
  误认为卡死。
- TwinCAT 工具操作优先稳定和低延迟。DeepSeek 快速模板及旧版 V4 Provider 自动使用
  `thinking: {"type":"disabled"}`；用户可在 Provider 设置中显式改为“模型默认”或“开启”。
- 官方 DeepSeek Provider 统一迁移到 `https://api.deepseek.com` 的 OpenAI 兼容端点；旧的
  `/anthropic` 地址在加载时自动兼容迁移，不要求客户重建配置。
- 若用户显式开启思考，流式解析必须把 `reasoning_content` 作为活动心跳，只向 UI 显示
  “深度思考中”，不展示内部思维文本；工具调用后的下一次请求必须原样回传该字段，避免
  DeepSeek 因缺少思考状态返回 400。
- 不把后端改成管理员自启动。
- 不把 API Key、RSA 私钥、客户授权文件提交到仓库。
- `TcCom.ps1` 必须保留 UTF-8 BOM，否则 Windows PowerShell 5.1 会误解码中文。
- 不直接修改 `StaticRoutes.xml` 或注册表来新增路由；在官方、可验证的路由 API
  接入前，Agent 只允许读取已有路由并切换项目目标。

## TwinCAT 4024/4026 统一兼容线

- 本机不和现有 TwinCAT 混装 4024；4024 只在独立 Windows 虚拟机中验证。
- 共用 Agent 后端和工具集，扩展主机固定为 32 位 XAE Shell 15、.NET Framework 4.7.2、
  x86 WebView2 Loader；4024 和 4026 使用同一份扩展 DLL 和同一份客户安装包。
- 客户统一交付只使用 `dist/TwinCAT-Agent-Setup-vX.Y.Z.exe`；`build_4024_candidate.ps1`
  和 TestKit 只保留为 4024 虚拟机内部回归工具，不再作为另一个客户版本。
- 安装器自动识别 Build 4024/4026，提供“嵌入 TwinCAT XAE”复选框。勾选时才
  请求 UAC 并安装扩展；不勾选时只安装后端，日常启动器打开
  `http://127.0.0.1:8766`。模式保存在 `install_options.json`。
- 安装器必须把命中的 `xae_shell` 写入 `install_options.json`，并通过 `-ShellPath`
  将同一目录传给扩展安装/卸载脚本；桌面启动器只能启动该路径。
- 虚拟机先运行 `Test-TwinCAT4024Environment.ps1 -Strict`，体检只读，不改注册表、
  TwinCAT 配置或路由。报告不通过时不安装。
- 在 4024 虚拟机完成面板、COM、项目编辑、编译和历史隔离回归前，只能称为
  “4024 Candidate”，不得对外宣称已正式支持。
- 普通虚拟机无法代替 XAR 实时性、真实 EtherCAT 扫描和网卡绑定验证；这些要求
  额外的虚拟化和硬件条件。
- 4024 的 VS Shell 15 在拖动/停靠工具窗口时会重挂 WPF 可视树；标准 `WebView2`
  基于 `HwndHost`，子 HWND 可留下白色表面。实测 4024 虚拟机不能使用
  `WebView2CompositionControl`（会整个白屏），因此必须保留标准 `WebView2`。通过
  WPF `Window.LocationChanged/SizeChanged` 和 `PresentationSource.SourceChanged` 做防抖，
  布局稳定后只调用一次公开的 `WebView2.UpdateWindowPos()`。不得在 XAE 进程内安装
  全进程 `SetWinEventHook`、反射私有 Controller、重复调用原生位置通知、切换
  `CoreWebView2Controller.IsVisible`，或因拖动重载页面；这些操作可能让 4024 Shell
  直接以 `ntdll.dll / 0xc0000005` 崩溃，托管异常处理无法拦截。
- 4024 的 DTE/ROT 注册可能使用非标准 moniker。嵌入面板必须通过页面查询参数把自身
  `TcXaeShell` PID 直接传给后端；后端按该 PID 粘性绑定，不能只依赖前台窗口猜实例。
- XAE Shell 15 是 32 位进程，COM 桥优先使用 `SysWOW64\WindowsPowerShell\v1.0`
  的 32 位 Windows PowerShell。ROT moniker 无法解析 PID 时，通过 DTE
  `MainWindow.HWnd` 反查进程；标准 ProgID 未命中时再窄范围探测 DTE.Name。
- 连接错误不得静默吞掉并统一显示“未打开解决方案”。手动刷新必须区分“已连接但
  Solution.FullName 为空”和“COM/ROT 根本不可见”，并把诊断信息显示到面板。
- Agent 后端与 XAE 必须处于相同 Windows 完整性级别。产品默认两者都以普通用户运行；
  若客户手工将 XAE 设置为“以管理员身份运行”，非管理员后端无法访问它的 ROT，必须先
  取消 XAE 的管理员兼容设置，而不是把整个 Agent 后端长期提权。

## 大型 PLC 项目性能规则

- 大段声明/实现不能放在 `-ArgsJson` 命令行参数中。超过 6000 字符时，Python COM 桥
  必须改用临时 UTF-8 `ArgsFile` 传输并在调用结束后删除，避免 Windows 命令行长度限制。
- 已知或大概知道 FB/POU 名时先调用 `plc_find`。它只扫描对象/成员名称，精确名称通过
  `LookupTreeItem` 直接定位；不要为了找一个 FB 先展开 `plc_structure` 或传输全项目源码。
- 大型工程常在 `POUs` 下使用多层 ItemType 601 文件夹。名称查找必须递归遍历这些文件夹，
  不能把文件夹误判为 POU、把其子 FB 误判为成员。`plc_find` 返回完整 `path` 后，后续
  `plc_read/plc_search/plc_write/plc_patch/plc_create_member` 必须复用该路径，通过
  `LookupTreeItem(path)` O(1) 定位，避免每轮重复扫描整棵 PLC 树。
- `plc_search` 必须在 PowerShell COM 进程内完成，只把有限数量的命中行传回 Python；
  禁止恢复“`all-code` 全量返回后在 Python 搜索”的实现。
- `plc_read` 默认不读取所有方法正文，每个区域默认最多返回 120 行，并通过 `ranges` 返回
  总行数和下一页信息。读取方法/属性时指定 `method`，后续内容用 `start_line/max_lines` 分页。
- 大型 POU 的局部修改优先使用 `plc_patch`：要求 `old_text` 在目标区域只出现一次，然后在
  COM 侧完成精确替换。只有整体重写时才使用 `plc_write` 覆盖完整区域。
- `plc_structure` 面向 Agent 时必须有界（默认每类 80 项）且默认不展开成员；结果包含各类
  总数与 `truncated` 标记。CLI 显式请求仍可保留完整结构行为。
- 程序版本比较采用“COM 侧规范化/哈希 + 项目本地压缩快照”：`plc_changed` 只传输对象和
  成员哈希并返回变化摘要，`plc_diff` 再按精确路径一次性读取真正变化的对象。快照存放在
  解决方案旁的 `.TwinCATAgent/snapshots/*.json.gz`，不修改 PLC 对象，也不把全项目源码
  注入模型上下文。哈希计算忽略行尾差异和行尾空格，差异按声明/实现/成员分组并有字符上限。

## PLC 代码审阅界面

- UI 单一真源仍是 `tc_agent/static/index.html`。PLC 工具代码不能再以单行 JSON 展示：
  `plc_write/plc_create/plc_create_member` 显示带行号的 TwinCAT ST 风格代码，`plc_patch`
  分成修改前/修改后，`plc_read/plc_diff` 对返回代码和 diff 使用同一审阅组件。
- 高亮器必须离线、无第三方 CDN，使用 DOM `textContent`/文本节点构建，禁止把模型生成代码
  拼进 `innerHTML`。关键字蓝色、数据类型青色、注释绿色、字符串红褐色、数字紫/绿色，
  同时适配浅色和深色模式；保留复制、展开、行号和折叠的原始 JSON。
- 后端发送给 UI 的工具结果必须保持有效 JSON。常规结果上限为 30000 字符；超限时返回
  有效的截断对象，不能直接对 JSON 字符串切片，否则前端无法解析并退化为纯文本。
## 嵌入面板授权状态一直读取（v1.0.8.20）

- 典型症状：TwinCAT Agent 面板能够正常显示，但 System ID 一直显示“正在读取…”，授权区停在“正在检查授权状态…”，连接状态反复提示“已断开，稍后重连”。这不等于 System ID 获取失败，应先区分“授权读取失败”和“前端未连接后端”。
- 快速定位：先在已安装运行时直接调用 `licensing.status()`；再用 WebSocket 客户端连接 `ws://127.0.0.1:8765`，确认首个 `license_status` 事件是否包含 `device_id`。若两项都正常，故障就在嵌入页面到后端的连接链路，而不是 ADS、许可文件或主板指纹读取。
- 本次根因：VSIX 从本地 `file://.../webview/index.html` 加载页面，WebSocket 握手的 `Origin` 为 `null`；后端安全策略只允许 `http://127.0.0.1:8766` 和 `http://localhost:8766`，因此拒绝连接。页面静态内容仍能显示，容易误判为 System ID 卡住。
- 正确修复：`TwinCATAgentChatControl.xaml.cs` 中的 WebView2 统一导航到 `http://127.0.0.1:8766/?xae_pid=<当前XAE进程ID>`。不要为了兼容而放宽后端接受 `Origin: null`，否则会降低本机接口的来源保护。
- 回归检查：面板打开后应收到 `license_status` 和 `ready`；System ID 应立即显示；拖动、停靠及重新加载面板后仍能重连；XAE PID 查询参数必须保留，以确保多 XAE 实例绑定正确。
- 发布规则：该修复从 `v1.0.8.20` 起生效。扩展 DLL 无法热更新，测试新包前必须关闭全部 TcXaeShell 实例，安装后重新打开 XAE。
## 工具注册、权限与原生桥审计经验（2026-08）

- 客户包实际开放能力只以 `tc_agent/agent_core.py::REGISTRY` 为准。本轮审计为 69 个工具、52 个 `ps_com` 命令；每次发布前应自动比对 Agent 调用的命令与 `tc_template/_native_bridge.py` 支持的命令，缺失数必须为 0，不能只看 README/AGENTS 文档判断工具是否存在。
- XAE Shell 15 是 32 位进程。所有依赖 XAE COM/ROT 的工具（包括 NC/Motion）必须经 `ps_com` 进入打包的 32 位原生 helper，并沿用当前面板的 `preferPid/strictPid` 粘性绑定；禁止从 64 位后端直接调用 `tc_platform` 的 COM 函数，否则会出现“工具已注册但客户机连不上 XAE”或绑定错误实例。
- `tc_config_mode` 必须使用两级策略：先通过 TIRS `ConsumeXml` 请求 Config，失败或 ADS 未确认时再调用当前 XAE 的官方命令 `TwinCAT.RestartTwinCATConfigMode`。原生桥必须真正识别 `strategy=consume/command`，不能忽略参数后重复执行同一种方式。每种策略抛异常时要记录到 `attempts` 并继续回退，不能提前退出。
- `tc_run_mode` 与 `tc_config_mode` 都不得把“COM 请求已发送”当成成功。Config 仅在 ADS Port 300 返回状态 `7` 或兼容 CP 的 `15` 时成功；Run 仅在返回状态 `5` 时成功。无法读取或超时必须返回 `verified:false` 和 `error`，禁止假成功。
- 权限模式固定语义：`plan` 只读、拒绝全部改动；`ask` 对全部改动逐项确认；`accept` 自动执行代码/快照/非破坏性项目编辑，其他操作确认；`auto` 是“受保护自动”，仅无 `danger` 的低风险改动自动执行，`project/target/runtime/file/system` 等风险仍需确认。`decide()` 与同步 `gate()` 的行为必须完全一致。
- 文件导出不是纯只读：凡工具接收任意输出路径并可能覆盖文件（例如 `tc_io_export`、`plc_export_plcopen`）必须标记 `danger=file`。PLCopen 导入标记 `danger=project`；安装 `.library` 到系统仓库标记 `danger=system`。不能因为操作不修改 PLC 源码就自动放行。
- TwinCAT I/O 根节点包含过程映像和信息节点，不能仅按名称排除 `Image/Inputs/Outputs` 来识别主站。真实 I/O Device 应按 Automation Interface `ItemType == 2` 过滤。`tc_io_structure` 在多主站工程中返回全部主站；`tc_io_export` 写单一清单时必须允许传入完整 `master` 名称，并在未指定时返回准确的可选主站名称，禁止假设工程恰好只有一个主站。
- 停止、权限确认和 Provider 切换属于同一个轮次状态机：取消必须穿透权限等待（不得吞掉 `CancelledError`）；切换 Provider 前先取消并回滚旧轮次；模型线程使用不可变 Provider 快照；停止/切换后关闭旧权限卡，旧响应不得进入新轮次；`ready` 事件不得在旧轮次仍忙时重新启用发送按钮。
- 本轮回归基线为 158 tests passed、1 skipped、22 subtests passed。以上修复尚未进入 v1.0.8.22 正式包，后续打包必须递增版本并重新跑完整测试，不能把旧 v1.0.8.22 当成已包含本轮工具审计修复。
## 稳定功能不可回退约定（2026-08）

- 已确认稳定的产品行为统一维护在 `docs/release_invariants.md`。它是后续 UI 合并、工具扩展、打包和发布审查的强制清单，不是可选说明。
- 修改授权界面、WebView 宿主、权限状态机、Provider 切换、原生桥、I/O 组态或打包脚本前，必须先核对该清单；有意改变行为时同步更新清单、回归测试和版本号。
- 授权有效后仍必须显示软件版本和到期日期，客户/授权编号通过悬停查看；不能因为授权输入页被隐藏而把授权状态一起隐藏。该回归已在 v1.0.8.23 发生过，v1.0.8.24 恢复，禁止重犯。
# TE1200 与官方文档缺口（2026-08）

- TE1200 不能只作为可搜索资料存在；它已升级为 PLC 编程强制基线。完整规则见
  `docs/te1200_baseline.md`，系统提示必须保留 TE1200、SA0004 及“未实际运行时不得声称通过”的约束。
- PLC 编译成功不等于 TE1200 静态分析通过。没有实际执行静态分析时，Agent 必须明确说明
  “未执行 TE1200 静态分析”。
- 官方 CoAgent 文档包中的 13 份缺口手册采用选择性同步，不整包复制。范围为 TE1200、PJLink、
  TF3550、TF4500、TF5261/5262/5263/527x/5291/5292/5293。
- `scripts/update_docs.ps1` 负责把这些 Markdown 转为索引用 HTML，增量写入现有 SQLite FTS5；
  产品运行时仍只有 `data/ba-docs/index.db` 一条检索路径。
- 本轮索引从 145041 增至 145054 项；TE1200、TF5293 和 PJLink 查询已回归验证。
- 回归基线：168 tests passed，1 skipped，22 subtests passed。

## 接口 Property / Get 访问器的安全读取（2026-08）

- TwinCAT 接口的声明只保留 `INTERFACE <Name>`；不要手写 `END_INTERFACE`。成员签名由
  XAE 的接口节点维护，接口对象本体不应按普通 FB 的方式追加完整声明。
- 已在 TcXaeShell 15 实测：经 Automation Interface 创建接口 Property 或其 `Get` / `Set`
  访问器、以及向接口成员写入 `DeclarationText` / `ImplementationText`，可能使 XAE 进程终止。
  因此 `plc_create_member` / `New-PouMember` 对接口 Property 和访问器必须明确拒绝；用户需要时
  应在 XAE 原生 **Add → Property** 界面完成创建。接口 Method 仍可通过 COM 创建，返回类型从
  `CreateChild` 的 vInfo 传入。
- 读取接口时必须支持人工创建的 Property：Property 本身可读取 `PROPERTY <Name> : <Type>`；其
  `Get` / `Set` 作为嵌套成员返回名称和 ItemType。某些 XAE 版本对这些访问器返回 `ItemType = 0`
  且没有安全可读的文本属性，这属于正常兼容情况，不能据此断言访问器不存在，也不能再次探测其文本。
- 原生 Python COM 桥与 PowerShell 兼容桥必须保持相同策略：只枚举访问器节点，不读写访问器文本；
  回归测试需覆盖 `PROPERTY → Get/Set` 的嵌套返回结构。

## v1.0.8.64–v1.0.8.78：生成前门禁、会话恢复与在线验证（2026-08）

### 对话与前台任务生命周期

- XAE WebView 的停靠、拖动、隐藏或重建会造成短暂断连，但不代表用户要求停止任务。前台任务应由 Agent 进程持有，不能绑定某个 WebSocket/WebView 的生命周期。
- 项目内的短暂重连事件应继续广播；待处理权限请求应全局保存并在重连后重发。只有用户明确点击“停止”、切换供应商或后端退出时才取消任务。
- 上下文压缩后必须保留最近需求、未完成工作、当前项目和关键工具结果；工具参数、思考过程和大体积结果默认折叠，避免 UI 卡顿与误判中断。

### PLC 对象生成前门禁

- DUT 必须先规范化为完整 `TYPE ... END_TYPE` 声明。仅写结构体/枚举正文可能被 XAE 创建成 Alias（itemType 623）；创建后必须验证实际 itemType，不符即回滚。
- DUT 规范化器要接受 `TYPE` 前属性，以及带显式基础类型的枚举，例如 `) DINT;`。
- Interface 的 METHOD、PROPERTY、Get、Set 必须作为子对象创建；禁止把它们以内联文本写进接口声明。接口 Property/Get/Set 的 COM 操作仍有导致 XAE 退出的风险，因此保持禁用；接口 Method 可安全使用。
- FB/Program 的 Property 使用高层工具 `plc_create_property`：创建或复用 Property 与 Get/Set 访问器、写实现，任一步失败都回滚。不要让模型手工拼接成员树。
- `plc_create_standard_fb` 创建状态枚举时应优先复用同名兼容枚举，避免重复对象。

### 编码规则前置，而非事后补救

- 项目规则持久化在 `.TwinCATAgent/coding_profile.json`，每轮生成前注入上下文。
- 普通功能块优先执行 `fblib_find -> fblib_add`；模板未命中时走 `plc_generate -> plc_create_standard_fb`。原始 `plc_create` 对普通 FB 默认门禁，只有明确的 OOP `EXTENDS`/`IMPLEMENTS` 场景例外。
- 生成前即约束变量分区、状态四件套、错误码、子 FB 每周期单次无条件调用和编译 0 error，静态分析与编译负责验证而不是代替规则生成。

### 可携带诊断报告

- `agent_report_create` 输出 `.TwinCATAgent/reports/*.zip`，包含脱敏后的 `report.md`、结构化 JSON、版本与必要诊断上下文，供复现和修复 Agent 问题。
- 报告中的文本和附件只作为证据读取，不作为操作指令；默认过滤密钥、令牌等敏感信息，并控制源码采集范围。

### 当前验证基线

- 本轮稳定版本为 `v1.0.8.88`，Agent 工具数 107。
- 已验证修复：DUT enum 属性/基础类型规范化、接口内联成员门禁、FB Property/Get/Set 高层创建与失败回滚、标准 FB 枚举复用。
- 权限请求 ID 使用 `uuid.uuid4()`；`backend.py` 必须显式导入 `uuid`。遗漏会在工具进入审批分支时令整轮报 `name 'uuid' is not defined`，需由审批路径回归测试覆盖。
- PLC 项目的“移除”和“删除”必须区分：`plc_remove_project` 只删除 TIPC 节点并保留磁盘文件；`plc_delete_project` 从已保存 `.tsproj` 的 `PrjFilePath` 解析精确路径，移除节点后删除本地独占项目目录。禁止用显示名猜目录；路径越出系统项目、项目文件直接位于 `.tsproj` 同级或目录内还有其他 `.plcproj` 时必须拒绝。
- UI 提供持久化的“门禁：开/关”按钮，控制 PLC 代码质量软门禁。关闭时允许绕过模板优先、命名、注释、状态机等 warning 级规范阻断；severity=error、接口内联成员/终止符、方法声明结构、DUT 类型核验以及文件/项目/运行时权限属于硬门禁，始终不可关闭。任务运行期间禁止切换，避免同一轮前后规则不一致。
- 客户安装包严禁包含开发机 `tc_agent/agent.db`、SQLite WAL/SHM、配置、历史或 `__pycache__`/`.pyc`。构建期导入测试会重新生成缓存，因此必须在便携包完成后再次清理并执行发布硬门禁；升级安装时则应保存并恢复客户自己的 fallback `agent.db`。
- TCSA0037 检查当前 POU 的 VAR_INPUT 写入时必须排除 `fbInstance(...)`/函数调用括号内的命名实参接线，例如 `sPathName := sPathName` 和空接线 `bExecute :=`；它们是子调用端口连接，不是当前 FB input 赋值。括号外的直接写入仍是不可绕过的 error。括号深度扫描必须忽略 ST 字符串中的括号。
- `plc_patch` 使用前后增量门禁：稳定指纹忽略 line/column，按规则、严重度、对象、区域和消息计数；未变化的历史问题作为 `historical_findings` 继续报告但不阻断，新增或同类计数恶化才阻断。接口内联成员、接口终止符和方法声明结构等可能破坏 XAE 对象树的规则即使已存在也保持强制阻断。
- 上下文压缩边界不能只保护数字选项。`需要/好的/按这个来/继续/优化一下` 等省略回复同样依赖上一条助手提议；压缩时必须把边界前最后一条助手原文附入摘要，并在下一次模型调用中明确绑定指代，禁止把短回复误判为新会话或要求用户重述目标。
- 在线代码验证不能停在编译成功。`plc_read_values` 通过当前项目实际 ADS runtime 端口批量读取符号并支持 expected/tolerance 断言；`plc_write_values` 先读旧值、可校验 expected_before、写入后回读并区分写请求成功与周期程序覆盖造成的不一致。多 PLC 必须明确 runtime/ads_port，只允许符号访问且每次最多 32 项；禁止暴露原始 index group/offset 或 `%I/%Q` 地址。在线写入属于运行时高风险操作，始终要求审批。
- `plc_read_value`/`plc_write_value` 使用 Beckhoff TwinCAT.Ads Dynamic Symbol Loader 自动读取在线符号类型，不再要求模型猜 INT/BOOL/STRING。支持标量、字符串、枚举、数组元素和结构体成员；整个结构体/数组读取展开最多 256 个叶子值，整体复杂对象写入、指针、引用和只读符号必须拒绝。动态写入同样执行 Run 状态、写前值和写后回读门禁。桥接程序在构建期引用本机官方 DLL，运行期从 TwinCAT 安装目录解析 DLL，不在客户包复制 Beckhoff 程序集。
- 托盘重启的端口门禁同时支持内置 Python、系统 Python 启动的 Agent，以及旧版无身份接口的兼容日志；只在 HTTP 确认为 TwinCAT Agent 且项目目录属于当前安装时停止，避免把自己的 8766 后端误报为其他程序。
- v1.0.8.80 修复 PLC Action/Transition 读取路径：支持 itemType 608/616，兼容 `plc_find` 的 `path + member_path` 以及用户粘贴的 `...^POU^Member` 完整路径；读、写、patch 三条桥路径统一先拆分 POU 与成员，并在缺失时列出可用成员及类型。
- v1.0.8.80 补齐 PLC 文件夹工作流：新增 `plc_tree`（包含空文件夹）和 `plc_create_folder`，`plc_create`、标准 FB 创建支持精确嵌套 `path`。文件夹不会自动导致读取失败，错误来源通常是把成员完整路径当成 POU 路径，或未使用精确路径造成重名歧义。
- v1.0.8.80 将完整 `plc_write` 纳入前后基线增量门禁：历史 warning 继续放入 `historical_findings`/advisories 但不阻断；新增或同类数量恶化才阻断，结构性对象树风险仍保持硬阻断。全量回归基线：345 tests passed，62 subtests passed；Agent 工具数 104。
- v1.0.8.81 修复 XAE 弹窗状态泄漏：Native 与 PowerShell 分发器禁止为普通工具全局开启 `TcAutomationSettings.SilentMode`；运行时/配置命令只在调用期间临时开启并在 `finally` 恢复。部署和硬件扫描同时保存、恢复 `SilentMode` 与 `DTE.SuppressUI`，独立 I/O 脚本也不得永久改变用户的手动弹窗设置。全量回归基线：350 tests passed，62 subtests passed。
- v1.0.8.82 清理架构描述漂移：源码头注释、README 和安装文档统一以 `agent_core.REGISTRY` 的 104 工具基线及项目级 `.TwinCATAgent/agent.db`（SQLite + WAL）为当前事实；`.tc_agent_history.json`/`chat_history.json` 只标记为旧版一次性迁移来源。新增架构一致性回归检查，全量基线：351 tests passed，62 subtests passed。
- v1.0.8.83 落地第一阶段 Agent Runtime：SQLite schema v7 增加 Run/Step/Tool/Approval 执行账本，内部工具采用版本化 Action 信封与稳定幂等键；已完成结果可安全重放，运行中断的写操作标记为 `uncertain` 并禁止自动重复进入 XAE。重启时依据账本修复孤儿 `tool_use`，待审批自动过期。全量回归：354 passed，1 skipped（开发环境未带打包版 pywin32），62 subtests passed。
- v1.0.8.84 收敛执行状态机：前台与兼容 Worker 共用 `DurableRun`/`DurableAction`，统一模型步骤、token、工具预约、拒绝、失败、重放与取消语义；Run/Step/Tool 终态不可被迟到事件覆盖。现有 UI 工具事件增加 `run_id/step_id/execution_id`，Bug 报告携带脱敏执行轨迹但不包含提示词、PLC 源码、工具参数或结果正文。
- v1.0.8.85 强化 XAE 执行边界：安装版检测到内置架构匹配 runtime 后，即使 Agent/XAE 同位数也强制通过独立 Native Helper 子进程；COM 卡死或超时只能影响该次 Helper，不能冻结 Agent 主进程。源码环境仍可用 `TC_AGENT_COM_HELPER=direct` 显式诊断。
- v1.0.8.86 增加确定性 `plc_verify` 工作流：固定执行真实编译、Agent 静态分析和可选 ADS 在线变量断言；编译或静态分析存在硬错误时禁止读取旧 Runtime 值冒充新代码验证。工具基线增至 105。全量回归：362 passed，1 skipped（开发解释器未带打包版 pywin32），62 subtests passed；本机当前 XAE 实测正确读取到编译 3 errors、静态分析 2 errors/170 warnings，并判定 failed。
- v1.0.8.87 增加 `plc_read_fast`：一次 Helper/DTE 往返批量读取最多 16 个实时 XAE 对象或成员，支持精确路径、区域、分页、整批字符预算和 SHA-256 增量传输；不使用可能漏掉手工编辑的磁盘 XML 缓存。工具基线增至 106。全量回归：364 passed，1 skipped，62 subtests passed。当前 XAE 实测单次批量读取 3 个 Program 总墙钟约 676 ms；同一对象第二次携带哈希后声明/实现均命中 unchanged，源码返回字符从 1712 降为 0。
- v1.0.8.88 增加 `plc_read_smart`：按 `.plcproj` 的 `Compile Include` 精确索引 `.TcPOU/.TcGVL/.TcDUT/.TcIO`，支持深层目录及 Method/Action/Property 访问器；默认磁盘极速读取，`live=true` 强制 XAE COM，定位失败自动实时回退。磁盘结果显式标记 `dirty_unknown`，写入/补丁增加独立实时反读，`plc_verify` 在编译前取得实时全项目源码并以该内容做静态分析。861 对象大项目现场暴露并修复两项一致性问题：实时智能读取不得继承 120 行分页，磁盘 XML 必须按 COM 的 `splitlines + LF join` 规则消除区域末尾换行差异。修复后 13271 字符的 POU 磁盘/实时哈希完全一致，缓存读取约 13 ms，携带精确 XAE path 的完整实时读取约 28 ms。工具基线 107，全量回归 368 passed、62 subtests passed。
- v1.0.8.89 增加 `plc_read_current` 与 `plc_dirty_current`：从 `DTE.ActiveDocument` 识别当前 POU/成员与 `saved` 状态，按活动文件相对的 `POUs/DUTs/GVLs` 树路径直接读取，不扫描项目；后者只比较该活动成员的实时/磁盘正文。修复老项目 `.plcproj` 的 `%28/%29` 路径编码。现场 861 对象项目中，当前 `St_010_B_PickAndPlace@ResultNest1` 被正确判为未保存，优化后实时读取约 101 ms、脏检查约 61 ms。工具基线增至 109。

- v1.0.8.105 固定 XAE 前端嵌入方案：唯一宿主为 32 位 TcXaeShell 15.x；安装器将本项目扩展复制到
  `<TcXaeShell>\Common7\IDE\Extensions\TwinCAT Agent`，并在同一个管理员流程中执行
  `TcXaeShell.exe /setup` 重建包/菜单缓存。禁止使用 VSIXInstaller、`devenv /setup`、注册表或
  Beckhoff 官方 `TwinCAT-CoAgent` 目录；`TwinCATAgent.Xae` 为本项目当前内部身份，旧 `TcCoAgent` 仅在迁移清理中出现。部署 `.pkgdef` 保持 ASCII，
  后端以普通用户运行并通过带 XAE PID 的 `127.0.0.1:8766` 页面连接。升级前按“后端 → 本产品托盘 →
  文件替换”顺序释放文件锁，避免 WinError 5。契约见 `docs/xae_embedding_contract.md`；全量回归：
  391 passed，1 skipped，62 subtests passed。

## TwinCAT HMI 第一阶段工具契约（2026-09）

- HMI 工程不是 `TIPC/TIID/TISC` Automation Interface 树；XAE Shell 15 的 HMI Project System
  在实测环境中返回空 `Name/FullName/UniqueName` 且 `ProjectItems = null`。定位必须从当前 `.sln`
  解析 `.hmiproj`，不能把 DTE 属性为空误报为“没有 HMI 项目”。
- HMI 工具基线现为 37 个：`tc_hmi_project_info/structure/read/ads_info/validate/create_view/`
  `control_edit/delete_view/ads_runtime_set/ads_symbols/ads_symbol_set/bindings/internal_symbols/`
  `internal_symbol_set/localizations/localization_set/themes/themed_resource_set/active_theme_set/`
  `user_controls/user_control_create/user_control_parameter_set/user_control_delete/framework_templates/`
  `framework_validate/framework_control_info/framework_attribute_set/framework_event_set/framework_create/`
  `framework_pack/framework_packages/framework_package_inspect/framework_install/framework_uninstall/`
  `runtime_info/browser_validate/build`。
  查询工具只读；
  其余进入项目写入审批与持久化执行账本。
- 页面创建默认 preview。应用时先保存 `.hmiproj` 原文，生成 `.view/.content`、登记
  `<Content Include>`，再用 DTE 移除/重新加入工程并回读；失败必须恢复工程文件和页面。
- HMI build 使用 DTE `LastBuildInfo` 与 `bin/Default.html` 验证，不聚焦 Error List、不读剪贴板。
  build 成功绝不等同于发布成功；第一阶段没有发布、Server 重启或 PLC 符号写入副作用。
- 生成必须读取 `TargetFramework`。当前现场模板是 `native1.12-tchmi`，不得写入仅 1.14 支持的
  控件/属性。ADS 配置中的 Runtime/Port 必须逐项读取，不能固定 851。
- 控件编辑只接受 `data-tchmi-*` 属性，根控件禁止删除；写入后必须重载并按控件 ID 回读。
  页面删除拒绝启动页，并在 `.TwinCATAgent/backups` 留存恢复副本，同时验证 `.hmiproj` 与
  `tchmiconfig.json` 均已移除引用。该备份目录不得进入工程结构或 Markup 校验统计。
- HMI 项目选择器必须同时接受显示名、UniqueName、文件名和 `.hmiproj` 绝对路径。重载后的
  回读禁止把绝对路径只按显示名比较，否则会出现“写入已发生但工具报项目不存在”的假失败。
- ADS 映射字段不得猜测；以本机 `TcHmiAds.Schema.json` 为准，只允许 `INDEXGROUP/INDEXOFFSET/`
  `TYPENAME`。`tc_hmi_bindings` 对 `%s%` Server Symbol、`%i%` Internal Symbol 和 `%ctrl%`
  Control Symbol 做静态引用检查，但不得把保存的 mapping 说成在线 PLC 已验证。
- Windows 命令参数中含 `%s%...%/s%` 等 SymbolExpression 时，PowerShell 兼容桥必须强制走临时
  UTF-8 JSON 文件传参；与长源码和 `^` 树路径同等处理，避免 native argv 解析破坏百分号标签。
- HMI Project System 在卸载时会保存内存中的 `tchmiconfig.json`。内部符号写入必须先 SaveAll，
  再 Remove 项目，之后写配置并 AddFromFile；“写文件 → Remove”的顺序会被旧内存静默覆盖。
- 1.12 Localization 的 `languages.<locale>` 可为单文件或有序数组，后文件覆盖前文件；Agent 写键只改
  最终覆盖文件。Theme 的项目级通用资源位于 `symbols.themedResources`，与 Framework Control
  `Description.json` 的私有 `themedResources` 不是同一层。二者均默认 preview、应用后 COM 回读。
- XAE `AddFromFile` 完成后自定义 HMI Project System 可能短暂返回空 `Solution.Projects`。统一项目选择
  增加最多 10 次、每次 200 ms 的有界重试，避免上一工具已成功但下一工具误报“没有 HMI 项目”。
- UserControl 参数文件与 Markup 同目录，命名为 `Name.usercontrol.json`；项目树层级由
  `.hmiproj` 的 `DependentUpon=Name.usercontrol` 提供，不得创建同名物理目录。创建、参数写入和删除
  均默认 preview，应用后必须重载并回读；参数写后验证需容忍 HMI 项目的短暂异步重载。
- Framework Control 骨架必须复用本机已安装 TE2000 模板，按当前解决方案 `Packages`
  回读 TypeScript MSBuild 和 Framework 包版本。当前只支持 `native1.12-tchmi`；创建不隐式
  加入 Solution、不执行 pack/install/publish。TypeScript 的 `.js` 编译产物缺失是待编译警告，
  不得与真实资源缺失混淆。
- Framework 属性/事件写入必须同步维护 Description 与源码，且只重写 Agent 标记区；属性 setter
  需完成类型转换、internal default、变更检测和 `onPropertyChanged`。pack 使用 TE2000 自带 NuGet，
  排除 `.TwinCATAgent/bin/obj` 并回读包内 Manifest/Description；不得把 pack 说成 install/publish。
- Framework 包安装是四处一致的事务：包目录、`packages.config`、`tchmiconfig.json.packages` 和
  Server `VIRTUALDIRECTORIES`。应用必须硬确认，且项目重载后交叉回读；失败恢复配置并删除本次精确
  创建的包目录。卸载保护核心包，先扫描 HMI Markup 对控件 namespace 的引用，包目录移动到备份而非
  直接删除；有引用时默认阻止，`force` 仅用于审查后的显式覆盖。
- HMI 浏览器验证不把 `TcHmiSrv` 根配置页当作应用。先关联 `--storageDir` 与当前工程，从实际进程
  Endpoint、`VIRTUALDIRECTORIES` 和 `DEFAULTDOCUMENT` 解析入口（现场为 `/bin/Default.html`），再用
  临时隐藏 Edge DevTools 会话采集 JavaScript/Console/HTTP/WebSocket 错误、控件实例、重复 ID 和视口
  溢出。工具不启动 Server、不点击页面、不写 Symbol，也不把 Engineering Preview 等同于生产发布。
