# HMI 原生项目项创建

`tc_hmi_item_create` 默认预览，apply=true 才创建。使用本机倍福
`DTE.GetObject("Beckhoff.TcHmi.1.12")`，不依赖为空的 DTE ProjectItems。
通过安装组件公开的 ITcHmiItem.AddFolder / AddItem(name, HmiTemplates) 创建，页面则使用
ITcHmiProject.AddView/AddContent 后再用 ITcHmiFile.AddControl/ChangeAttributes 做语义构建，
官方向导处理模板、参数、工程登记和从属关系。不得回退到旧版手拼 XML。

支持 folder、view、content、usercontrol、codebehind_js、javascript、javascript_classic、css、function_js。
`javascript_classic` 必须走 ITcHmiItem.AddExistingItem；不能把 AddItem 生成的 JavascriptModule
当作经典脚本。页面创建的预检候选只存在内存中，不是复制页面源码。
name 不带扩展名；folder 必须已存在，缺失时先显式创建。
parent_file 不支持：不再人为拼接 DependentUpon。TypeScript 尚未验证。

写入前检查精确工程、就绪状态、名称、文件冲突和重解析点，备份工程与配置。
原生创建后仅保存目标 DteProject，并回读项目树、生成文件、工程 Include 和适用的 Framework 登记。
页面类额外执行安装版本 Schema 校验。不重载工程、不发布、不操作 PLC。
异常可能已有原生向导输出，返回 incomplete / retry_safe=false，禁止盲目重试或删除。
调用期间请求官方 SuppressUi(true)，结束后恢复原始 DTE.SuppressUI 状态。
本机官方 GetHmiProjects()/GetProjects() 内部主动 ShowToolWindow(SymbolsManagement)，
普通 SuppressUi 无法抑制。必须调用 GetHmiProject(EnvDTE.Project) 重载，并核对返回的
ProjectDirectory；不得以字符串重载或枚举获取 HMI 项目。创建链路不关闭用户已有窗口。

2026-09-08：XAE PID 36232 / ComplexLineHMI / TE2000 14.3.379.1，
在 AgentNativeTest 实测八类创建及精确保存。原生 Function 和 UserControl 的
DependentUpon 包含工程相对目录，旧生成器只有文件名；CodeBehind 的类型引用路径
也由官方向导生成。旧 AgentItemTest 结果不能作为原生格式认证。

构建验证受现有 build_platform_mismatch 门禁阻止，没有修改平台绕过。
通过界面检查复现旧枚举路径弹出 Configuration 浮动窗口。改为 Project 重载后，
NoEnumerationPopup.content 创建、保存、Schema 校验通过，后续界面检查未见再次弹出。
ui_trace 仅是即时 DTE 采样，不能证明异步窗口不存在。浏览器业务行为未验证；
源代码更新尚未打包安装。

创建结果现补充 `saved_state_evidence`：按种类核对 Framework 配置、依赖类型、
配套文件登记/所有权，以及 UserControl 生成 Schema。缺少任一适用项则返回
incomplete / verified=false，即使原生节点和文件已创建也不报完成。
详细双向测试清单见 [免重载验收](hmi_no_reload_acceptance.md)。

## 精确路径契约

所有现有 HMI 文件读写/删除先按工程根目录规范化相对路径，再在 `.hmiproj` 中要求
恰好一个完全相同的 `Include`。不会用文件名、后缀或 `EndsWith` 猜测目标；例如请求
`Desktop.view` 不会命中 `00 Content/Desktop.view`，重复 Include 直接失败。
标准目录名 `00 Content`、`100 UserControl` 等允许空格，但创建项名称仍按标识符规则校验。

## 原生删除：早期重载实现记录（已被下文免重载实现替代）

新增 Agent/MCP `tc_hmi_item_delete(file, project, apply=false, repair_orphan=false)`。
已有 Agent、MCP、CLI 的页面删除/UserControl 删除经 Python 桥接统一路由到新流程。
直接调用 TcCom.ps1 的旧 hmi-delete-view/hmi-user-control-delete 分支已禁止写入，
自动化应使用新工具或 CLI/Python 包装，不应绕过引用审查和快照门禁。

- 原生 DeleteChild 会删除磁盘文件，但返回 true 不证明文件存在或配置已清理。
- 支持 View/Content、UserControl+参数、Function+描述、JS/CSS、空文件夹。
  非空目录拒绝递归删除；启动页、登录页、未知从属项、路径逃逸和引用冲突阻止写入。
- 执行前检查未保存文档/工程和审查快照，备份目标文件、工程、配置及生成 Schema。
- 原生删除并精确保存后，如有配置登记，先卸载目标 HMI，再只清除对应列表条目；
  空文件夹也执行一次卸载/重载，清除原生接口可能残留的精确 Folder Include 登记。
  UserControl 只清除 frameworkUserControlConfig 精确指向被删参数文件的 Schema 定义。
  之后重载一次，结果显式返回 reload_required。不发布、不登录/启动 PLC。
- 文件、节点、hmiproj、tchmiconfig 和适用的生成 Schema 全部回读成功才 verified=true。
  失败返回 incomplete，尝试从备份恢复；rollback_reloaded 不等于完整回滚已验证。
- Python 外层追加对照事务备份的只读保存状态检查。即使 COM 报成功，残留配置、
  缺失 Schema 或其他登记/配置被改动也会使结果降为 incomplete。这个追加检查不会
  自行回滚或重载，备份保留；它也不证明 live hierarchy 或免重载。
- repair_orphan=true 只允许目标文件/节点都已缺失、仍有配置登记的场景。
  正常删除找不到目标时拒绝，不把重复删除当成功。
- 引用审查采用保守文本检查，注释/同名文本也可能阻止删除，需要人工审查，不提供 force 绕过。

当前 ComplexLineHMI 已验证：NoEnumerationPopup.content 孤立登记修复；
UiPhaseProbe.content、NativePanel.usercontrol（含参数与 Schema）、NativeFunction.js
（含描述）及空 Nested 目录删除。全部已创建项目内备份。测试未构建/发布，未修改业务页面。

## 不重载删除：进程内能力探测（尚未启用写入）

2026-09-09 本机 TE2000 14.3.379.1 实现检查发现，HMI 工程的
`IVsHierarchyDeleteHandler3.DeleteItems` 会执行删除前/后处理，而单项
`ITcHmiItem.DeleteChild` 没有走完整流程。此为本机版本实现证据，不是跨版本保证。
外部 Automation 的 `DteProject.Object` 在 COM 边界抛 InvalidCastException；
已移除这条不可用的实验路径，避免给正常创建增加 Shell Interop 依赖。

新增 VSIX 独立只读管道 `TwinCAT-Agent-HmiHierarchy-<PID>`：

- 仅接受 `probe-delete`；没有 apply 或执行删除的命令，不改变现有公开工具路由。
- 当前用户 ACL、请求大小上限及超时；与 build/diagnostics 管道隔离。
- XAE UI 线程经 `SVsSolution` 枚举已加载工程，用 `IVsProject.GetMkDocument`
  核对完整 `.hmiproj` 路径；再用 CanonicalName 双向核对文件/空目录。
- 拒绝工程根节点、选择项、路径逃逸、重解析点、受保护目录及非空目录。
- 仅调用 `QueryDeleteItems`，返回 `storage_delete_allowed`；这不代表引用审查通过、
  删除授权或成功。固定 `written=false / verified=false / execution_enabled=false`。
- 不操作鼠标、不调用 HMI 项目枚举、不保存、不重载，也不修改 PLC/运行时。

诊断脚本 `scripts/Test-HmiDeleteHandler.ps1` 要求显式传入
`-XaePid -SolutionFile -ProjectFile -ItemFile`；未加载新版扩展时返回
`bridge_unavailable`，不降级到鼠标或旧删除方法。

源码 Release 编译通过。2026-09-09 用户关闭 XAE 后，已通过管理员安装流程更新 DLL
并执行 `/setup`，两处安装 DLL 哈希一致，模型配置哈希不变，后端未更新。
重新打开 Test.sln（PID 18788）并通过已登记命令打开 Agent 面板后，进程内探测成功：
`AgentNativeTest/QuietContent.content` 的 canonical path 一致、item_id=11，
`IVsHierarchyDeleteHandler3.QueryDeleteItems` 返回 storage_delete_allowed=true。
未删除、未保存或重载 HMI。接下来需要接入带备份、引用/未保存门禁和完整回读的删除事务。
当前不能宣称公开删除工具已支持免重载，也不能把 Query 的 allowed 当成 verified。

工程定位使用微软公开 [IVsSolution](https://learn.microsoft.com/en-us/dotnet/api/microsoft.visualstudio.shell.interop.ivssolution)
服务与安装的 Visual Studio SDK，不反射调用 Beckhoff 私有删除函数。

## 当前源码删除路径与实测状态（2026-09-09 后续）

外部 PowerShell 通过 OLE IServiceProvider.QueryService(SVsSolution) 可取得精确工程
IVsHierarchy。已在源码用 `IVsHierarchyDeleteHandler3.DeleteItems` 替代 DeleteChild，
保留快照、引用、未保存门禁和备份，不再手改配置文件或卸载/加载工程。
文件夹 CanonicalName 需要尾部分隔符；仅对已确认目录允许这一等价形式并双向核对。
失败保留现场及备份，返回 incomplete/recovery_required，不自动重载回滚。
`repair_orphan` 暂拒绝，不允许通过孤立登记修复绕过免重载约束。

PID 18788 的 View、Content、UserControl 各两轮添加/删除/同名创建通过，
UserControl 的参数/DependentUpon/生成 Schema 同步验证通过。空文件夹仍残留
Folder Include，CodeBehind 仍残留 dependencyFiles；两者均正确报 incomplete。
矩阵与备份记录见 [免重载验收](hmi_no_reload_acceptance.md)。不宣称全覆盖。

此次本地安装只更新了 VSIX DLL 并刷新缓存，模型配置哈希不变，后端未更新；
新删除工具仍是源码开发状态，不应把 XAE 插件更新当作工具层已全部安装。
