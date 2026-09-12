---
name: vsix-isolated-shell-lessons
description: "Cross-project lessons from G:\claude\visual studio extension (VS2022) on VSIX dev for isolated shells — VSCT unreliability, devenv /setup risk, correct F5 experimental-instance path"
metadata:
  type: reference
---

## VSIX 在隔离 Shell（VS Isolated Shell）里的踩坑经验（跨项目迁移自 447c656d 会话）

来源：`G:\claude\visual studio extension`（VS2022 版 TwinCAT Agent 扩展，同类问题但目标是 VS2022 本体而非 TcXaeShell）。与 [[coagent-plugin-project]] 的 P2 菜单渲染问题高度相关，务必在继续调试 tc_agent_vsix 前应用这些教训。

**根因 1 — VSCT 菜单在纯命令行 MSBuild（无 VSSDK 工作负载）下不可靠。** ActivityLog 报 `Resource not found: TwinCATAgentCommands.vsct` / `Error loading UI library for package`——VSCT 编译成 `.cto` 但没正确嵌入 DLL。命令行 `msbuild` 编译 VSIX 时如果机器没装 **VS Extension Development 工作负载**（`Microsoft.VisualStudio.Component.VSSDK`），VSSDK 的构建目标链条不完整，会导致：VSCT 资源嵌入失败、`ExcludeAssets="runtime"` 丢失运行时依赖 DLL、pkgdef 里的菜单引用悬空。**这与 twin-cat-agent 项目 tc_agent_vsix 的"菜单不渲染"问题是同一类根因**——tc_agent_vsix 也是纯命令行 MSBuild 编译的（`.claude/memory/coagent-plugin-project.md` P2 记录）。

**根因 2（最终真相）— `devenv /setup` / `/updateConfiguration` 可能损坏 `privateregistry.bin`，导致 IDE 卡死。** 那次调试中反复用 `/updateConfiguration` 强制刷新扩展注册表，其中一次 `/setup` 直接把 VS2022 搞到打开就卡死，靠 `Rename-Item privateregistry.bin privateregistry.bin.bak` 回滚+重建才修复。**教训：不要对着用户正在用的、生产用途的 IDE Shell（尤其是 TwinCAT XAE / TcXaeShell，用户每天靠它做 PLC 工程）反复跑 `/setup`、`/updateConfiguration`，或直接改 `privateregistry.bin`/配置 hive。** 这与仓库 CLAUDE.md 安全规则"禁止未经同意操作注册表"精神一致，且有真实翻车案例佐证。

**正确路径 —— 用 VS IDE 内置 VSIX 项目模板 + F5 Experimental Instance 调试，不要纯命令行编译+手动注册表操作。**
1. 确认机器装了 `Microsoft.VisualStudio.Component.VSSDK`（VS Installer → 单个组件 → 勾 "Visual Studio SDK"）。twin-cat-agent 的 P2 spike 已确认此机装了 VS2022 Community 的 VisualStudioExtension workload，比姊妹项目起点更好。
2. 在 VS 里用 **File → New → Project → VSIX Project** 模板创建（而不是手写 .csproj + 命令行 msbuild），这样 VSSDK targets、运行时依赖解析、VSCT 编译链都是官方配置好的。
3. 按 **F5** 调试会启动一个 **Experimental Instance**（`/rootsuffix Exp`），VS 自动把包/菜单/工具窗口正确部署注册进实验实例的独立 hive，不污染主配置，出问题也不会搞坏日常用的 IDE。
4. 对隔离壳（如 TcXaeShell）：项目属性 → Debug → **Start external program** 指向壳的 exe（如 `TcXaeShell.exe`），**Command line arguments** 传对应的 `/rootsuffix <hive>`（本项目诊断出的稳定 hive 是 `IsoShell`，即 `15.0_IsoShell_Config` 系列，而不是每次启动哈希都变的 `15.0_Config_<hash>`）。这样调试部署走的是 VS 自己的正规部署机制，而不是手动 VSIXInstaller / 文件拷贝 / pkgdef 注册表手术。

**⚠️ 更正（2026-07-20，twin-cat-agent 项目实测）：上面第 3-4 点对"跨到另一个 shell"不成立，只对同一个 VS 产品的 Experimental Instance 有效。** 实测把 tc_agent_vsix 的 `DeployExtension` 设 `true`、`.csproj.user` 里 `StartProgram` 指向 TcXaeShell.exe + `StartArguments=/rootsuffix IsoShell`，在 VS2022 里按 F5 后：**pkgdef 被部署进了 VS2022 自己的扩展目录**（`%LOCALAPPDATA%\Microsoft\VisualStudio\<VS2022 hive>\Extensions\`），完全没碰到 TcXaeShell。看了 `Microsoft.VsSDK.targets` 源码才明白根因：`DeployVsixExtensionFiles` target 通过 `GetInstallationDirectoryForInstance InstanceId="$(DeployTargetInstanceId)"` 找部署目标，这个查找绑定的是**正在构建它的那个 VS SxS Setup Instance**（即 VS2022 自己），TcXaeShell 不是可被发现的 VS Setup Instance，天然到不了。真正"只做文件、不依赖 ExtensionManager"的是 `CopyVsixExtensionFiles` target（`CopyVsixExtensionFiles=true` + `CopyVsixExtensionLocation=<路径>`），注释写着"Typically only used for Isolated Shell solutions"——但这条路径**不做 pkgdef 合并注册**，跟手动拷贝到 Extensions 目录是同一个坑（回到"文件都在但菜单不渲染"的老问题）。**结论：VS2022 的 F5/Deploy 机制本质上到不了 TcXaeShell，"用 IDE 官方流程" 这条经验只对同产品内的 Experimental Instance 成立，隔离壳场景仍需另想办法**（候选：直接用 TcXaeShell 自己的 MSBuild/devenv 去构建+部署这个项目，如果它内置了一份；或者继续啃 `CopyVsixExtensionFiles` + 弄清楚 isolated shell 自己在启动时到底怎么把新扩展合并进 `_IsoShell_Config`）。

**次要教训:**
- `InitializeAsync` 里直接 `frame.Show()` 自动弹出工具窗口容易死锁——用命令触发的懒加载，不要包初始化时强制显示。
- 免管理员权限的用户级安装路径：`%LOCALAPPDATA%\<Vendor>\<ShellName>\<ver>_IsoShell\Extensions\<name>\`（对 TcXaeShell 是 `%LOCALAPPDATA%\Beckhoff\TcXaeShell\<ver>_IsoShell\Extensions\`）——但即使文件都拷对，**不注册 pkgdef 菜单/包依然不会合并进运行时配置**，纯拷贝不够。
- 出问题时优先看 ActivityLog（`%APPDATA%\<Vendor>\<Shell>\<ver>[_IsoShell]\ActivityLog.xml`），比瞎猜快得多。
- 最小可行版本优先：先做零依赖、无 VSCT、纯代码注册工具窗口的最简扩展验证壳能加载包，再逐步加回 UI/依赖，比一次性上完整版更容易定位卡死/加载失败的层次。

**追加事故记录（2026-07-20）：姊妹项目的旧扩展残留导致这台机器的 VS2022 每次启动必死锁，已修复。** 排查 tc_agent_vsix 时先在 VS2022 里测 F5（见上面的更正），结果 VS2022 本身在**任何**项目下都会卡死——打开后连菜单栏都画不出来。查 ActivityLog（`devenv ... /log <path>`，日志路径可以随便指定，不需要放到 shell 自己的 APPDATA 目录）发现卡在 `Begin package load [TwinCATAgentPackage]` 之后再无 `End package load`，且再没有任何后续日志——UI 线程死锁。`TwinCATAgentPackage` 正是姊妹项目 [[vs-extension-sibling-project]] 那次调试留下的扩展，那次会话结尾自称"已清理干净"但其实没清干净，一直潜伏到这次才暴露。**修复方式且已验证安全**：`VSIXInstaller.exe /uninstall:<VsixID> /q`（先关掉所有 devenv 进程再跑）——这是官方支持的卸载路径，不碰注册表/`privateregistry.bin`，跑完 VS2022 立刻恢复正常启动。VsixID 从 `%LOCALAPPDATA%\Microsoft\VisualStudio\<hive>\Extensions\<随机名>\*.vsixmanifest` 里的 `<Identity Id="...">` 读。**教训：`VSIXInstaller /uninstall` 是安全的官方操作，不同于 `devenv /setup`/`/updateConfiguration` 那类会连带损坏 `privateregistry.bin` 的骚操作——遇到扩展导致的 IDE 问题，先试这个，不要跳过它直接上注册表手术。**

**How to apply:** 调试 tc_agent_vsix 菜单不渲染问题时，鉴于 VS2022→TcXaeShell 部署路径已被证伪，改从 `CopyVsixExtensionFiles` 或 TcXaeShell 自身构建工具链的角度继续查，不要再假设 VS2022 里按 F5 能解决；任何要碰 TcXaeShell 注册表/`/setup`/`/updateConfiguration` 的操作，先跟用户确认（该 shell 是生产用 IDE，翻车代价高），但 `VSIXInstaller /uninstall:<id> /q` 已验证安全可以直接用。相关：[[coagent-plugin-project]]、[[com-dynamic-dispatch-py314]]、[[vs-extension-sibling-project]]。
