# TwinCAT Agent 1.0.8.109

- 重构运行时、工具契约、PLC 源码交付和安装包流程，统一版本并发布新的 Windows 安装包。

- XAE 扩展完成内部命名收敛：源码工程和程序集输出统一为 `TwinCATAgent.Xae`；保留原有 VSIX
  Identifier、包/命令/工具窗口 GUID，并在本产品扩展目录内事务清理旧 `TcCoAgent`/旧重命名残留。

- 新增“设置 → 第三方 MCP 服务”界面，支持本地 stdio 与远程 Streamable HTTP MCP。
- 支持添加、编辑、测试、启停和删除用户自己的 PLC 模板库、代码知识库及自动化服务。
- 第三方 MCP 工具采用独立命名空间，密钥不返回界面或模型，且所有调用均需用户逐次确认。

- 新增 SYSTEM/实时配置工具：读取 `TIRC/TIRS/TIRT` 树、Real-Time Settings、CPU 核/Affinity 和任务参数。
- 新增 SYSTEM 节点预览/添加/删除、TwinCAT 核分配和实时任务核分配；所有写操作默认 preview，`apply=true` 才执行并回读验证。
- 同步 Native COM、PowerShell、MCP 和 `tc` CLI 工具层，系统写操作不会自动切换 Config、激活或重启。

- 修复升级安装 `WinError 5 拒绝访问`：退出后端后会等待 TwinCAT Agent 托盘释放自身 EXE 文件锁，超时只终止已验证属于本产品的托盘进程。

- 修复 XAE 视图菜单缺少 TwinCAT Agent：TcXaeShell 15.0 无法解析部署用 `.pkgdef` 中的 UTF-8 中文值，导致整个包被跳过；部署注册现在保持 ASCII。
- 明确 `TcCoAgent` 仅是本项目的内部技术程序集/包标识，与 Beckhoff 自带的 `TwinCAT-CoAgent` 扩展无关。

- 仅恢复 XAE 前端嵌入扩展的已知成功兼容清单：使用 TcXaeShell 15.0 的 Community/x86 扩展宿主标识。
- 后端 Agent、端口、工具和项目数据未改变。

- 修复 XAE 扩展安装后刷新缓存仍以普通权限运行，导致 `extension.configurationchanged` 报“Elevation required”。
- 扩展复制和 TcXaeShell `/setup` 缓存刷新现在在同一次管理员流程中完成。
- 修正 XAE 扩展清单版本同步为 1.0.8.102。

- 修复“后端正常、XAE 的视图菜单找不到 TwinCAT Agent”：安装器现在使用检测到的 **TcXaeShell 自带 VSIXInstaller** 注册 Package/Menu，不再只复制文件。
- 修复 XAE VSIXInstaller 弹出帮助页：调用时显式指定 TcXaeShell 路径、VS 15 Community SKU 和版本，而不是依赖隔离 Shell 自动推断。
- 修复 VSIX 注册失败 `1011`：不再把普通 ZIP 当作 VSIX，改用 VSSDK 构建的合法 VSIX/OPC 安装包。
- 修复 VSIX 注册失败 `0x80070490`：目标从 Visual Studio Community 改为 TwinCAT XAE 实际使用的独立 `TcXaeShell`。
- 安装器窗口标题栏使用 TwinCAT Agent 正式图标，不再显示 Tk 默认羽毛图标。
- 移除不兼容的 XAE VSIXInstaller 注册步骤，改由 TcXaeShell `/setup` 原生重建扩展包与菜单缓存，避免 `2003` 等错误。
- XAE 扩展 VSIX 的版本随 Agent 发行版本递增，确保升级会被正确识别。
- 保持扩展文件级兜底安装与旧 `TcCoAgent` 残留清理。
