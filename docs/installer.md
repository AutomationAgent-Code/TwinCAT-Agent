# TwinCAT Agent 4024/4026 统一安装包

## 客户使用

交付客户的主文件只有：

```text
dist\TwinCAT-Agent-Setup-v1.0.1.exe
```

使用步骤：

1. 完全关闭 TwinCAT XAE。
2. 双击 `TwinCAT-Agent-Setup-v1.0.1.exe`（版本号以后会递增）。
3. 安装器会自动识别 TwinCAT Build 4024/4026。
4. 如需 XAE 内嵌面板，保持勾选“嵌入 TwinCAT XAE”。
5. 阅读《TwinCAT Agent 软件使用协议与数据说明》并勾选同意。
6. 点击“立即安装”。只有勾选嵌入时才会请求一次 UAC。
7. 安装完成后双击桌面的 TwinCAT Agent 图标。

安装器会自动完成：

- 安装便携 Python、包含 183 tools 的 Agent 后端和倍福文档索引。
- 根据选项安装/更新 TwinCAT XAE 扩展。
- 创建带产品图标的桌面、开始菜单和开机启动快捷方式。
- 安装图形卸载程序。
- 嵌入模式：以普通用户启动后端，再打开 TwinCAT XAE。
- 仅后端模式：启动后端并打开 `http://127.0.0.1:8766`。

## XAE 嵌入的固定方式

“嵌入 TwinCAT XAE”使用本项目唯一验证过的原生安装链路：扩展文件复制到
`<TcXaeShell>\Common7\IDE\Extensions\TwinCAT Agent`，然后在同一个管理员流程中执行
`TcXaeShell.exe /setup` 重建 XAE 包和菜单缓存。安装完成后以普通用户启动后端，重新打开
TcXaeShell，再从 View/Other Windows 打开 TwinCAT Agent。

不要使用 VSIXInstaller、`devenv /setup` 或 Beckhoff 官方 `TwinCAT-CoAgent` 目录。前者把
TcXaeShell 当成普通 Visual Studio SKU，可能产生 1011/2003/NoApplicableSKUs；后者是不同产品。
完整边界、升级顺序和故障恢复规则见 [XAE 嵌入固定契约](xae_embedding_contract.md)。

安装模式保存在 `install_options.json`。不勾选嵌入表示本次不修改 XAE；
如果旧版嵌入扩展已经存在，安装器不会未经确认删除它。

默认安装目录：

```text
%LOCALAPPDATA%\Programs\TwinCAT Agent
```

## 4024 / 4026 路径兼容

安装器不能把 TwinCAT 根目录写死为单一路径。当前统一包按下面的矩阵探测：

| 内容 | Build 4024 默认位置 | Build 4026 默认位置 |
|---|---|---|
| TwinCAT 二进制/组件 | `C:\TwinCAT\3.1` | `C:\Program Files (x86)\Beckhoff\TwinCAT\3.1` |
| 可变配置、许可和运行数据 | `C:\TwinCAT\3.1` | `C:\ProgramData\Beckhoff\TwinCAT\3.1` |
| 32 位 XAE Shell | `C:\Program Files (x86)\Beckhoff\TcXaeShell` | 同左（安装了 x86 Shell 时） |

`C:\TwinCAT\3.1` 是 4024 的 TwinCAT 组件根目录，不等于独立 XAE Shell 的程序目录。
安装器仍读取 `TWINCAT3DIR` 并保留经典目录作为自定义安装的后备候选。最终命中的
`TcXaeShell.exe` 会写入 `install_options.json`；安装扩展、日常启动和卸载始终复用
这个准确路径。

当前嵌入扩展面向 VS Shell 15 / x86。`TcXaeShell64` 是不同架构，不能把 x86
扩展 DLL 直接复制进去；目标机只有 64 位 Shell 时，应取消“嵌入 TwinCAT XAE”使用
浏览器模式，或另外安装 32 位 XAE Shell。

升级时直接运行新版 Setup。安装器会保留现有 `config.json`（Provider/API Key）和
无解决方案时使用的兼容数据库 `app/tc_agent/agent.db`。正常项目对话存放在解决方案目录的
`.TwinCATAgent/agent.db`（SQLite + WAL），本来就在安装目录之外，不会被覆盖。
`chat_history.json` 与项目目录中的 `.tc_agent_history.json` 仅作为旧版本的一次性迁移来源；
`%LOCALAPPDATA%\TwinCAT Agent\license.json` 同样位于安装目录之外。

## 开发机重新打包

普通 PowerShell 运行：

```powershell
& "G:\claude\twin-cat-agent\scripts\build_installer.ps1"
```

默认版本读取仓库根目录的 `VERSION`。发布新版时只需修改这个文件；也可临时覆盖：

```powershell
& "G:\claude\twin-cat-agent\scripts\build_installer.ps1" -Version 1.0.2
```

文件名和 EXE 属性中的 FileVersion/ProductVersion 会同步生成，例如
`TwinCAT-Agent-Setup-v1.0.2.exe`。构建时会自动清理旧的正式 Setup，只保留最新版本。

脚本会依次：

1. 重建完整便携包。
2. 构建带图标的日常启动器和卸载器。
3. 将完整便携包、启动器、卸载器和 Logo 嵌入单文件 Setup。
4. 扫描私钥、API 配置和聊天历史，发现泄漏就停止交付。
5. 自动删除便携 ZIP、解压目录和安装器临时目录，`dist` 只保留最终 Setup。

如只做不带倍福文档库的小包测试：

```powershell
& "G:\claude\twin-cat-agent\scripts\build_installer.ps1" -NoDocs
```

## 回归检查

安装器支持开发测试参数，不修改 XAE、启动项或系统安装目录：

```powershell
$setup = Get-ChildItem ".\dist\TwinCAT-Agent-Setup-v*.exe" |
  Sort-Object LastWriteTime -Descending | Select-Object -First 1
& $setup.FullName --test-install `
  "G:\claude\twin-cat-agent\dist\_installer-test"
```

仅后端选项回归可在命令末尾加 `--no-embed`。

至少检查：

- `app\tc_agent\backend.py`
- `app\data\ba-docs\index.db`
- `extension\TwinCAT Agent\TwinCATAgent.Xae.dll`
- `TwinCAT-Agent.exe`
- `TwinCAT-Agent-Uninstall.exe`
- `twincat-agent.ico`
- `install_options.json`

## 当前限制

- 测试版尚未代码签名，Windows SmartScreen 可能显示“未知发布者”。
- 正式销售前需要购买 EV/OV 代码签名证书，对 Setup、启动器、卸载器和扩展 DLL 签名。
- 当前后台自启动使用普通用户的 Startup 快捷方式，尚未升级为带崩溃恢复的托盘程序。
