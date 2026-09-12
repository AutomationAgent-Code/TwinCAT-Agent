# TwinCAT Platform Control

TwinCAT runtime operations — build, activate, login, run/config mode switching, deploy pipeline.

## Trigger
```
/tc <operation>
```

## Commands

只执行用户要求的运行时操作。创建/改代码/编译不自动授权扫描、切目标、登录、启动、激活、重启或部署。
上线前核对 XAE PID、解决方案、实际 Target NetId 和 PLC endpoint；已有明确授权沿用，目标或范围不明时先确认。
编译失败或结果不可确认时不得继续上线。任务边界和代码工作流见 [主技能](twincat-agent.md)。

### Build & Check

| Command | Description | API |
|---------|-------------|-----|
| `tc build` | Build PLC project, show errors | `SolutionBuild.Build()` + `BuildState` polling |
| `tc check` | Check all PLC objects (compile check) | `ITcPlcIECProject2.CheckAllObjects()` |

**Build completion detection**: All compile functions poll `dte.Solution.SolutionBuild.BuildState` until it reaches `2` (dsBuildStateDone). No blind `time.sleep()`.

**Error reading**: 使用现有 build/diagnostics 封装汇总 Error List、Build Output 与构建状态，保留结果来源和不可用标记。
空 ErrorItems 不证明 0 错误；不可用、超时或未观察到当前构建完成时报告未验证，不用旧 LastBuildInfo 冒充本次结果。

### Diagnostics

| Command | Description |
|---------|-------------|
| `tc diag health` | Full system health check (Runtime / XAE / Solution / Target / Routes / Platform / Version / Errors) |
| `tc diag ping [target]` | Test ADS connectivity |
| `tc diag route [target]` | Route diagnostics (existence + ADS ping + runtime state) |
| `tc diag target [target]` | Comprehensive target info (device name, CPU arch, OS version, TcVersion) |
| `tc diag errors` | Error list with enhanced diagnosis and fix suggestions |
| `tc diag license` | License status query |

### Activate & Restart

| Command | Description | API |
|---------|-------------|-----|
| `tc activate` | Activate config + restart TwinCAT | `ActivateConfiguration()` + `StartRestartTwinCAT()` |
| `tc restart` | Standalone runtime restart (no re-activate) | `StartRestartTwinCAT()` |
| `tc boot` | Set PLC as boot project (autostart) | `ITcPlcProject.GenerateBootProject()` |

### Projects & Device Settings

| Command | Description | API |
|---------|-------------|-----|
| `tc projects` | List PLC projects (TIPC) + solution projects | tree iterate + `Solution.Projects` |
| `tc project-info` | Solution path, target NetId, PLC projects | `GetTargetNetId()` |
| `tc io-structure [--depth N]` | Show I/O (TIID) device tree | recursive tree iterate |
| `tc device-read <path>` | Read a tree item's settings XML | `ProduceXml(False)` |
| `tc device-write <path> <xml_file>` | Write a tree item's settings | `ConsumeXml(xml)` |
| `tc ethercat-adapter` | Show each I/O device's bound NIC | parse device `ProduceXml` |
| `tc ethercat-adapter-set <dev> <desc>` | Rebind device NIC (best-effort) | edit `<DeviceDesc>` + `ConsumeXml` |

### NC Axis Configuration

| Command | Description | API |
|---------|-------------|-----|
| `tc ncConfigurate` | Create NC task + axes for servo drives → link to I/O | `CreateChild` + `ConsumeXml` |

**NC config workflow:**
1. Scan for servo drives (EL72xx, AX5xxx, etc.) in the I/O tree
2. Create NC task (`TINC`) if not already present
3. Create one axis per drive and link via `ConsumeXml` `<IoItem>`

### I/O Scanning & Configuration

| Command | Description | API |
|---------|-------------|-----|
| `tc scan` | Scan EtherCAT devices on current target → add to config | `ProduceXml` + `CreateChild` + `ScanBoxes` |

**Scan workflow:**
1. Only for an authorized hardware scan, switch to Config mode if required; explain the runtime interruption
2. `ProduceXml(False)` on `TIID` → hardware discovery
3. Parse `<FoundDevices>/<Device>` for each physical device
4. `CreateChild(name, subType, "", None)` on TIID for each device
5. `ConsumeXml(<DeviceDef>AddressInfo</DeviceDef>)` — configure device address
6. `ConsumeXml(<ScanBoxes>1</ScanBoxes>)` — discover sub-terminals / slices

### Variable Linking (I/O ↔ PLC)

| Command | Description | API |
|---------|-------------|-----|
| `tc link list` | List process-data variables from scanned terminals | `VarCount`/`Var` on TIID |
| `tc link show` | Show current variable mappings | `ProduceMappingInfo()` |
| `tc link add <io> <plc>` | Link an I/O variable to a PLC symbol | `LinkVariables()` |
| `tc link remove <io>` | Remove all links from an I/O variable | `UnlinkVariables()` |

**Link workflow:**
1. Read existing I/O configuration; use `tc scan` only if hardware scanning is explicitly requested, not as a prerequisite for linking existing nodes
2. `tc link list` — enumerate available I/O variables grouped by terminal
3. Copy the full path of the I/O variable to link
4. `tc link add <io_path> <plc_path>` — create the link
5. `tc link show` — verify the mapping

### PLC Online

> ⚠️ 需要项目中存在 PLC 项目。纯 I/O 项目（无 PLC）会跳过 online 步骤。

| Command | Description | API |
|---------|-------------|-----|
| `tc login` | Login to PLC runtime | ConsumeXml `<LoginCmd>true</LoginCmd>` |
| `tc logout` | Logout from PLC runtime | ConsumeXml `<LogoutCmd>true</LogoutCmd>` |
| `tc start` | Start PLC program | ConsumeXml `<StartCmd>true</StartCmd>` |
| `tc stop` | Stop PLC program | ConsumeXml `<StopCmd>true</StopCmd>` |
| `tc online` | Login + Start in one step | Both above |

Online commands use ConsumeXml on the PLC project node:
```xml
<TreeItem>
  <IECProjectDef>
    <OnlineSettings>
      <Commands>
        <LoginCmd>true</LoginCmd>
        <StartCmd>true</StartCmd>
      </Commands>
    </OnlineSettings>
  </IECProjectDef>
</TreeItem>
```

### Run / Config Mode

| Command | Description |
|---------|-------------|
| `tc state` | Show current TwinCAT runtime state + IsTwinCATStarted |
| `tc config` | Switch to Config mode (stop runtime) |
| `tc run` | Switch to Run mode (start runtime) |

### Target & Settings

| Command | Description | API |
|---------|-------------|-----|
| `tc target` | Show current target NetId (default: show) | `GetTargetNetId()` |
| `tc target show` | Explicitly show current target NetId | `GetTargetNetId()` |
| `tc target list` | List all configured routes from StaticRoutes.xml | `StaticRoutes.xml` |
| `tc target set <target>` | Set target by name, address, or NetId | `resolve_target()` + `SetTargetNetId()` |
| `tc target add --auto-auth` | Interactive: pick adapter → scan → select device → add route | `list_local_adapters()` + GUI |
| `tc target add -n <n> -a <ip> --auto-auth` | Direct: add route with default credentials (skip adapter picker) | `add_static_route()` |
| `tc target search` | Scan local subnets for Beckhoff devices (port 48898) | TCP multi-thread scan |
| `tc silent --on/--off` | Toggle silent mode (no dialogs) | `TcAutomationSettings.SilentMode` |

### Version Management

| Command | Description | API |
|---------|-------------|-----|
| `tc version show` | Show target / local / pinned TcVersion | `get_pinned_tc_version()` |
| `tc version pin [ver]` | Pin project to TcVersion (writes TcVersionFixed) | `PinnedTcVersion` + `TcVersionFixed` |
| `tc version unpin` | Remove pin → TwinCAT auto-selects | Clear `TcVersionFixed` |

**tc-template create 自动处理版本：**
创建项目时自动写入 `TcVersionFixed="true"` + `TargetNetId` 到 `.tsproj`，
XAE 打开时自动使用匹配的版本，无需手动操作。

**Example: user explicitly requested discovery, route registration, target selection and deployment:**
```
tc target search                                    → scan local subnets for Beckhoff devices
tc target add -n CX-A1 -a 192.168.1.229 --auto-auth    → add route with default credentials
tc target set CX-A1                                 → switch target
tc deploy                                           → deploy to target
```
> `--auto-auth` 使用默认凭据 Administrator / 1 自动认证，路由立即激活。
> 自定义认证使用 `--user` / `--password`；未提供凭据的直接调用会被封装拒绝，不会退回写 StaticRoutes.xml。
> 添加路由本身不授权后续切目标或部署。名称/IP 是选项，不是位置参数。

### Full Deploy Pipeline

| Command | Description |
|---------|-------------|
| `tc deploy` | build → boot → activate → restart → login → start |
| `tc deploy --no-silent` | Same as above but trial-license dialogs appear |

> 无 PLC 项目时跳过 PLC 专属阶段；纯 I/O 部署仍需 build → ActivateConfiguration → restart。
> 注意 CLI `tc activate` 本身已经包含重启，与底层仅激活的 ActivateConfiguration 不同，不要再重复重启。
> 部署不得隐式切 Config；扫描需要 Config 时也不能将激活配置当作失败后的自动兜底。
>
> 默认启用 SilentMode 抑制授权弹窗。若需查看试用授权提醒，使用 `tc deploy --no-silent`。

---

## Typical Workflows

### Edit code only (no runtime authorization)
```
plc read MAIN                      # confirm actual code first
# Apply the requested change via PLC tools, then read back.
tc build                           # report compilation; online behavior remains unverified
```

### Code-only change + explicitly authorized online run
```
# Read, edit and read back with PLC tools first.
tc build
tc online                          # only after a confirmed successful build; login + start
# Read online values at the actual PLC endpoint and compare against expectations.
```

### Explicitly authorized full deployment to remote controller
```
tc target list                     # list available routes
tc target set CX-A1A9A0            # set by name (or address / NetId)
tc target                          # verify current target
tc deploy                          # full pipeline
```

### Explicitly authorized pure I/O deployment (no PLC)
```
tc target set CP-8DEE9E            # switch to remote I/O device
tc deploy                          # build → activate → restart (login/start skipped)
```

---

## API Reference

All operations use the TwinCAT Automation Interface (COM) documented in
`knowledge_base/tc3_automation_interface.md`.

### Key interfaces used

| Interface | Methods |
|-----------|---------|
| `ITcSysManager` | `ActivateConfiguration()`, `StartRestartTwinCAT()`, `IsTwinCATStarted()`, `LinkVariables()` |
| `ITcSysManager2` | `GetTargetNetId()`, `SetTargetNetId()` |
| `ITcPlcIECProject2` | `CheckAllObjects()` |
| `ITcPlcProject` | `GenerateBootProject()`, `BootProjectAutostart` |
| `ITcSmTreeItem` | `CreateChild()`, `ProduceXml()`, `ConsumeXml()`, `DeleteChild()` |
| `EnvDTE.SolutionBuild2` | `Build()`, `BuildProject()`, `BuildState` |
| `EnvDTE80.ToolWindows` | `ErrorList.ErrorItems` — **pure COM error reading (no pyautogui)** |
| `ITcPlcDeclaration` | `DeclarationText` — read/write POU declarations |
| `ITcPlcImplementation` | `ImplementationText` — read/write POU implementations |

### Error List — Pure COM
```python
# dte.ToolWindows.ErrorList.ErrorItems.Item(i)
# → .Description / .FileName / .Line / .Column / .Project / .ErrorLevel
```
Replaced the old pyautogui+clipboard hack. Zero mouse, zero screen coordinates.

> ⚠️ `ToolWindows.ErrorList.ErrorItems` 在 Python 3.14 强制动态绑定下不可用（`ToolWindows` 在
> DTE2 接口上）。回退：读 Output 窗口"生成/Build"面板文本 + `SolutionBuild.LastBuildInfo`（0=成功）。
> **清列表勿用** `ExecuteCommand("TwinCAT.ClearErrorList")` —— 装了 Analytics 扩展时会弹遗留调试
> MessageBox（原生 Win32，SilentMode 拦不住）。清列表纯属视觉，直接跳过。详见 `/plc` skill。

### Build State — Polling
```python
# dte.Solution.SolutionBuild.BuildState
# 0 = NotStarted, 1 = InProgress, 2 = Done
```
All four build functions poll BuildState instead of blind sleep.

### Online commands
Sent via `ITcSmTreeItem.ConsumeXml()` on the PLC project node.
Path format: `TIPC^<ProjectName>^<NestedName> Project`

### Runtime state
Read via `ITcSmTreeItem.ProduceXml()` on `TIRS` (Real-Time Settings) node.

### I/O device scanning
Scans via `ITcSmTreeItem.ProduceXml(False)` on `TIID`. Devices added via
`CreateChild` + `ConsumeXml(<DeviceDef>)`. Sub-terminals scanned via
`ConsumeXml(<ScanBoxes>1</ScanBoxes>)` on each device node.

### Variable linking
I/O variables enumerated via `ITcSmTreeItem.VarCount` / `Var`. Links
created via `ITcSysManager.LinkVariables(plcPath, ioPath)`. Mappings
exported/imported via `ITcSysManager3.ProduceMappingInfo` /
`ConsumeMappingInfo`.
