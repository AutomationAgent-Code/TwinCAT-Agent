# TwinCAT Agent XAE 嵌入方案（固定契约）

本文档是 TwinCAT Agent 前端嵌入 TwinCAT XAE 的唯一受支持方案。安装器、便携版脚本、升级流程
和故障排查都必须遵循这里的边界；如果实现与本文档冲突，以本文档和回归测试为准。

## 1. 真实宿主与身份

- 宿主是 TwinCAT 自带的 **32 位 TcXaeShell 15.x**，不是普通 Visual Studio 2022。
- 扩展目标目录固定为：

  ```text
  <TcXaeShell 根目录>\Common7\IDE\Extensions\TwinCAT Agent\
  ```

- `TwinCATAgent.Xae.dll`、`TwinCATAgent.Xae.pkgdef` 和命名空间 `TwinCATAgent.Xae` 是本项目
  的内部程序集/包标识；它们与 Beckhoff 官方目录或扩展 `TwinCAT-CoAgent` 没有任何关系。
  `TcCoAgent.dll`/`TcCoAgent.pkgdef`、`TwinCATAgent.dll`/`TwinCATAgent.pkgdef` 仅作为本产品
  旧版本升级时的精确残留清单保留，禁止把它们与官方组件混装、替换或当成同一个组件。
- 扩展面板中的可见产品名是 `TwinCAT Agent`。
- 当前扩展只支持 x86 TcXaeShell。只有 `TcXaeShell64` 的机器使用浏览器模式，不能把这份 DLL
  复制到 64 位 Shell。

## 2. 唯一安装链路

```text
关闭全部 TcXaeShell
        ↓
检测并记住确切 TcXaeShell.exe 路径
        ↓
UAC 管理员流程：将扩展文件复制到 Extensions\TwinCAT Agent
        ↓
同一个管理员流程执行 TcXaeShell.exe /setup
        ↓
普通用户启动 TwinCAT Agent 后端
        ↓
重新打开 TcXaeShell
        ↓
从 XAE 的 View/Other Windows 菜单打开 TwinCAT Agent
```

关键规则：

1. 复制扩展和 `/setup` 必须在同一次提升权限的流程中完成。`/setup` 会写入
   `extension.configurationchanged`，普通权限刷新会失败并提示 `Elevation required`。
2. `/setup` 是 TcXaeShell 自己的扩展扫描/菜单缓存重建入口。它完成后必须重启 XAE，
   因为托管扩展 DLL 不能在运行中的 Shell 内热替换。
3. 后端仍以普通用户运行；后端和 XAE 必须处于相同 Windows 完整性级别，否则 COM/ROT
   连接可能不可见。
4. 安装器保存命中的 `xae_shell` 到 `install_options.json`，升级和卸载必须复用这个路径，
   不得重新猜测另一份 XAE。

## 3. 明确禁止的替代方式

- **禁止使用 VSIXInstaller 注册客户版扩展。** TcXaeShell 是隔离 Shell，不是当前安装的
  Visual Studio SKU；VSIXInstaller 会出现 `NoApplicableSKUs`、1011、2003 等错误，或把扩展
  注册到 VS2022 而不是 XAE。
- **禁止使用 `devenv /setup`、`/updateConfiguration`** 刷新 TcXaeShell。
- **禁止直接修改注册表、TcXaeShell 私有注册表数据库或 Beckhoff 官方扩展目录。**
- **禁止把后端网页 iframe/浏览器窗口冒充 XAE 嵌入。** XAE 内的面板必须由本项目的 VS Package
  创建，WebView2 页面再连接本机 `http://127.0.0.1:8766/?xae_pid=<当前 XAE PID>`。
- **禁止删除整个 XAE Extensions 目录。** 升级只覆盖本项目目录中的文件，并清理本项目已知
  的旧重命名残留；不得影响其他扩展。

## 4. 扩展包不可变约束

部署目录至少必须包含：

```text
TwinCATAgent.Xae.dll
TwinCATAgent.Xae.pkgdef
extension.vsixmanifest
```

`TwinCATAgent.Xae.pkgdef` 是 TcXaeShell 15.0 的部署注册文件，必须保持 ASCII。尤其是 `Class` 和
`CodeBase` 必须指向当前文件：

```text
"Class"="TwinCATAgent.Xae.TwinCATAgentPackage"
"CodeBase"="$PackageFolder$\\TwinCATAgent.Xae.dll"
```

不要把 UTF-8 中文写入部署用 `.pkgdef` 值；XAE 15 的旧版解析器可能因此跳过整个包，表现为
“后端正常但 View 菜单没有 TwinCAT Agent”。本地化文本放在 VSIX manifest 或程序集资源中。

## 5. 升级、卸载和失败恢复

- 升级顺序固定为：停止后端 → 停止本产品托盘并等待自身 EXE 文件锁释放 → 原子替换本地安装目录；
  不要用 `taskkill` 按端口或进程名误杀其他程序。
- XAE 扩展升级使用 `install_extension_native(..., refresh_cache=True)`；该过程先 staging/校验，
  并在复制或 `/setup` 失败时恢复原目录；卸载使用 `remove_extension_native()`，两者都必须
  通过精确的 `TcXaeShell` 根目录校验。
- 安装器只复制 `TwinCAT Agent` 子目录，不改 Beckhoff 的 `TwinCAT-CoAgent` 子目录；升级只在
  该目录内删除本产品旧文件名，不触碰 `ChatVs.dll` 或其它官方文件。
- 如果出现 `WinError 5`，先确认 XAE 和 Agent 托盘已退出，再重新运行最新版安装器；安装器会
  自动发送托盘退出事件并等待文件锁，只有路径确认属于本产品时才允许兜底终止。
- 如果出现 `Elevation required`，说明 `/setup` 没有在管理员子流程中执行；不能通过给后端提权
  来绕过，应重新运行安装器。
- 如果出现 VSIXInstaller 帮助页、`NoApplicableSKUs`、1011 或 2003，说明走回了旧 VSIX
  注册链路，应停止使用该命令，改用本文档的原生复制 + `/setup` 流程。

## 6. 变更门禁

任何改动以下内容的 PR/版本发布都必须更新本文档并通过 `tests/test_unified_installer.py`：

- XAE 宿主架构、扩展目标目录或包身份；
- 扩展复制方式、权限边界或 `/setup` 缓存刷新方式；
- VSIX manifest、pkgdef 的 `Class`/`CodeBase`/编码；
- 安装、升级、卸载时的路径保护和旧版本残留清理。

在真实 TcXaeShell 中验证时，必须记录：XAE 版本、Shell 位数、ActivityLog 是否加载
`TwinCAT Agent`、View 菜单是否出现面板、后端是否以普通用户启动，以及关闭/重启 XAE 后
面板是否仍可重新连接。
