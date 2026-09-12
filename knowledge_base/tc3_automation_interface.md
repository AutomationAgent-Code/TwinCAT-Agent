# TC3 Automation Interface — Knowledge Summary

Source: TC3_Automation_Interface_EN.pdf (v1.6.2, 2026-04-01, 166 pages)

---

## 1. Architecture Overview

TwinCAT 3 Automation Interface is a **COM-based API** that allows programmatic control of TwinCAT XAE (the engineering environment integrated into Visual Studio).

**Core layers:**
- **Visual Studio DTE** (`EnvDTE.DTE`) — controls VS itself (windows, solutions, error list)
- **ITcSysManager** — the TwinCAT system manager (open/activate/save configurations)
- **ITcSmTreeItem** — every element in TwinCAT's tree (I/O, PLC, Tasks, Axes, etc.)

All content in TwinCAT is organized as a **tree**. Each element = `ITcSmTreeItem` with a unique path like `TIPC^ProjectName^ProjectName Project^POUs`.

---

## 2. Getting Started — Creating a Project

```csharp
Type t = Type.GetTypeFromProgID("VisualStudio.DTE.10.0");
EnvDTE.DTE dte = Activator.CreateInstance(t);
dte.SuppressUI = false;
dte.MainWindow.Visible = true;

solution.Create(@"C:\Temp", "MySolution");
string template = @"C:\TwinCAT\3.1\Components\Base\PrjTemplate\TwinCAT Project.tsproj";
dynamic project = solution.AddFromTemplate(template, @"C:\Temp\MySolution", "MyProject");

ITcSysManager sysManager = project.Object as ITcSysManager;
sysManager.ActivateConfiguration();
sysManager.StartRestartTwinCAT();
```

**Powershell:**
```powershell
$dte = new-object -com VisualStudio.DTE.10.0
$dte.SuppressUI = $false
$project = $dte.Solution.AddFromTemplate($template, $dir, $name)
$sysManager = $project.Object
$sysManager.ActivateConfiguration()
$sysManager.StartRestartTwinCAT()
```

**ProgID by VS version:** `VisualStudio.DTE.10.0` (VS2010), `VisualStudio.DTE.11.0` (VS2012), `VisualStudio.DTE.12.0` (VS2013), etc.

**TwinCAT project template path (4026+):**
`C:\Program Files (x86)\Beckhoff\TwinCAT\3.1\Components\Base\PrjTemplate\TwinCAT Project.tsproj`

---

## 3. Silent Mode (SUPPRESS ALL DIALOGS)

```csharp
var settings = dte.GetObject("TcAutomationSettings");
settings.SilentMode = true;
```
**Powershell:** `$settings = $dte.GetObject("TcAutomationSettings"); $settings.SilentMode = $true`

Prevents message boxes during Automation Interface usage. Available since TC3.1 Build 4020.

---

## 4. Opening an Existing Configuration

```csharp
dte.Solution.Open(@"C:\path\to\solution.sln");
// or
dte.Solution.AddFromTemplate(@"C:\template.tszip", destinationDir, projectName);
```

---

## 5. Accessing the Error List

> ⚠️ **实测警告 (2026-07-21, TcXaeShell 15.0 / VS2017 Shell / 4024):**
> 下面的 `dte.ToolWindows.ErrorList.ErrorItems` 方法在**外部进程**驱动这台
> TcXaeShell 时**不可用** —— 跨进程 COM 连接下 UI 线程对象无法编组:
> `ErrorItems.Count` 恒为 0, `Windows`/`OutputWindow` 集合的 Count/Caption
> 也返回空 (但自动化对象模型 `Solution`/`SolutionBuild.LastBuildInfo`/POU
> 读写完全正常)。`SolutionBuild.LastBuildInfo` 能拿到失败项目数, 但拿不到错误文本。
>
> **本仓库实际采用的可靠方法** (见 `tc_template/TcCom.ps1` 的 `Read-ErrorList`):
> 前置 IDE 窗口 (Win32 `SetForegroundWindow`) → `ExecuteCommand('View.ErrorList')`
> 聚焦错误列表 → Win32 键盘事件 Ctrl+A / Ctrl+C → 读剪贴板 → 按制表符解析。
> 纯 PowerShell + Win32, 无 pyautogui、无 python COM。剪贴板 TSV 列:
> `严重性 代码 说明 项目 文件 行 禁止显示状态` (中文 locale)。
> 注意: 中文 locale 下"代码"列为空, 错误码 (C0032) 前缀在"说明"里, 需正则补提取。
> `vsBuildState`: 1=NotStarted 2=InProgress **3=Done** (等 Done 要判 3, 不是 2)。

**理论上的 COM 方法 (在 4026/VS2022 集成版上可能可用, 在本机 4024 上实测为空):**

```powershell
$errors = $dte.ToolWindows.ErrorList.ErrorItems
for ($i = 1; $i -le $errors.Count; $i++) {
    $item = $errors.Item($i)
    Write-Host "$($item.Description) ($($item.FileName):$($item.Line))"
}
```

Each `ErrorItem` has: `Description`, `FileName`, `Line`, `Column`, `Project`.

---

## 6. Tree Navigation (ITcSmTreeItem)

### Path format
`TIID^Device1^Term1` — elements joined by `^` (`circumflex accent`)

### Top-level path abbreviations
| Abbrev | Meaning |
|--------|---------|
| `TIID` | I/O devices |
| `TIPC` | PLC projects |
| `TINC` | NC (motion) |
| `TIRT` | Real-time tasks |
| `TIRS` | Real-time settings |
| `TIRC` | Root configuration |

### Key methods

**Lookup:**
```csharp
ITcSmTreeItem item = sysManager.LookupTreeItem("TIPC^ProjectName^ProjectName Project^POUs");
```

**Create child:**
```csharp
ITcSmTreeItem plc = sysManager.LookupTreeItem("TIPC");
ITcSmTreeItem newProject = plc.CreateChild("Name", subType, "", pathToTemplate);
// subType=0: copy to solution, subType=1: move, subType=2: use original location
```

**Navigate children:**
```csharp
foreach (ITcSmTreeItem child in parentItem)
    Console.WriteLine(child.Name);
```

**XML import/export:**
```csharp
string xml = item.ProduceXml();   // export
item.ConsumeXml(xml);             // import modified XML
```

**Import/Export child (XTI files):**
```csharp
ITcSmTreeItem io = sysManager.LookupTreeItem("TIID");
io.ImportChild(@"C:\IoTemplate.xti", "", true, "ImportedDevice");
io.ExportChild("DeviceName", @"C:\export.xti");
```

---

## 7. PLC Projects

### Create new PLC project
```csharp
ITcSmTreeItem plc = sysManager.LookupTreeItem("TIPC");
ITcSmTreeItem proj = plc.CreateChild("Name", 0, "", "Standard PLC Template");
// or use full path to .plcproj or .tpzip file
```

### Open existing PLC project
```csharp
ITcSmTreeItem proj = plc.CreateChild("Name", 1, "", @"C:\path\to\project.plcproj");
// subType: 0=copy, 1=move, 2=use original location
```

### Create POU
```csharp
ITcSmTreeItem pous = sysManager.LookupTreeItem("TIPC^PlcName^PlcName Project^POUs");
ITcSmTreeItem pou = pous.CreateChild("FB_Test", 604, "", IECLANGUAGETYPES.IECLANGUAGE_ST);
```

### Cast to ITcPlcPOU for code access
```csharp
ITcPlcPou fbPou = (ITcPlcPou)pou;
ITcPlcDeclaration decl = (ITcPlcDeclaration)fbPou;
ITcPlcImplementation impl = (ITcPlcImplementation)fbPou;
string declaration = decl.DeclarationText;
string implementation = impl.ImplementationText;
decl.DeclarationText = "PROGRAM MAIN\nVAR\n...";
impl.ImplementationText = "...";
```

---

## 8. Import POU from template file

```csharp
ITcSmTreeItem plcProject = sysManager.LookupTreeItem("TIPC^Name^Name Project");
// Single file
plcProject.CreateChild("NameOfPou", 58, null, @"C:\path\FB_Test.TcPOU");
// Multiple files
string[] paths = { "FB_Test.TcPOU", "ST_Struct.TcDUT" };
plcProject.CreateChild(null, 58, null, paths);
```

---

## 9. PLC Library Management

```csharp
ITcSmTreeItem refs = sysManager.LookupTreeItem("TIPC^Proj^Proj Project^References");
ITcPlcLibraryManager libMan = (ITcPlcLibraryManager)refs;

// Add library
libMan.AddLibrary("Tc2_MDP", "*", "Beckhoff Automation GmbH");
// or by display name
libMan.AddLibrary("Tc2_Math, * (Beckhoff Automation GmbH)");

// Add placeholder
libMan.AddPlaceholder("Placeholder_NC", "Tc2_NC", "*", "Beckhoff Automation GmbH");

// Remove
libMan.RemoveReference("Tc2_Math");

// Scan all installed libraries
ITcPlcReferences libs = libMan.ScanLibraries();

// Freeze placeholder version
libMan.FreezePlaceholder("Placeholder_NC");

// Repository management
libMan.InsertRepository("MyRepo", @"C:\MyLibs", 0);
libMan.InstallLibrary("MyRepo", @"C:\lib.library", false);
libMan.RemoveRepository("MyRepo");
```

---

## 10. PLC Online Commands

Via ConsumeXml on the PLC project node:

```xml
<TreeItem>
  <IECProjectDef>
    <OnlineSettings>
      <Commands>
        <LoginCmd>true</LoginCmd>
        <LogoutCmd>false</LogoutCmd>
        <StartCmd>false</StartCmd>
        <StopCmd>false</StopCmd>
      </Commands>
    </OnlineSettings>
  </IECProjectDef>
</TreeItem>
```

Additional commands: `ResetColdCmd`, `ResetOriginCmd`.

---

## 11. Template System (Three Levels)

### Level 1: Configuration templates (*.sln, *.tszip)
```csharp
project = solution.AddFromTemplate(@"C:\template.tszip", destination, name);
// or
project = solution.Open(@"C:\project.sln");
```

### Level 2: PLC project templates (*.plcproj, *.tpzip)
```csharp
ITcSmTreeItem plc = sysManager.LookupTreeItem("TIPC");
plc.CreateChild("Name", 0, null, @"C:\project.plcproj");
// or archive
plc.CreateChild("Name", 0, null, @"C:\template.tpzip");
```

### Level 3: Structural element templates (*.xti)
Used for I/O devices, motion axes:
```csharp
ITcSmTreeItem io = sysManager.LookupTreeItem("TIID");
io.ImportChild(@"C:\device.xti", "", true, "Name");
```

---

## 12. Save As Archive

```csharp
// Full solution → .tszip
ITcSysManager9 sysMan9 = (ITcSysManager9)sysManager;
sysMan9.SaveAsArchive(@"C:\backup.tszip");

// Single PLC project → .tpzip
ITcSmTreeItem plc = sysManager.LookupTreeItem("TIPC");
plc.ExportChild("ProjectName", @"C:\project.tpzip");
```

---

## 13. Useful Patterns

### CheckAllObjects (PLC compilation check)
```csharp
ITcSmTreeItem proj = sysManager.LookupTreeItem("TIPC^Name^Name Project");
ITcPlcIECProject2 iec = (ITcPlcIECProject2)proj;
iec.CheckAllObjects();
```

### Boot project
```csharp
ITcPlcProject plcRoot = ...;
plcRoot.BootProjectAutostart = true;
plcRoot.GenerateBootProject(true);
```

### Set target platform
```csharp
ITcConfigManager cfg = ((ITcSysManager7)sysManager).ConfigurationManager;
cfg.ActiveTargetPlatform = "TwinCAT RT (x64)";
```

### Build specific project
```csharp
sln.SolutionBuild.BuildProject("Release|TwinCAT RT (x64)", projectFullPath, true);
```

### Access Target NetId
```csharp
string netId = sysManager.GetTargetNetId();
```

---

## 14. COM MessageFilter (REQUIRED)

Every Automation Interface application MUST implement a COM MessageFilter to handle rejected COM calls from Visual Studio. Without it, `RPC_E_CALL_REJECTED` errors occur.

Key: `CoRegisterMessageFilter()` with `RetryRejectedCall()` returning 99 (retry immediately).

See section 4.2.5 of the PDF for the full C# implementation (~60 lines).

---

## 15. Key COM Interfaces

| Interface | Purpose |
|-----------|---------|
| `ITcSysManager` | Top-level TwinCAT config (open/activate/save/LookupTreeItem) |
| `ITcSysManager3` | Adds LookupTreeItemById |
| `ITcSysManager7` | Adds ConfigurationManager access |
| `ITcSysManager9` | Adds SaveAsArchive |
| `ITcSmTreeItem` | Any tree node (Name, PathName, Children, ProduceXml, ConsumeXml, CreateChild, ImportChild, ExportChild) |
| `ITcPlcIECProject` | PLC project — SaveAsLibrary, code access |
| `ITcPlcIECProject2` | Adds CheckAllObjects |
| `ITcPlcPOU` | POU-specific access |
| `ITcPlcDeclaration` | POU declaration area read/write |
| `ITcPlcImplementation` | POU implementation area read/write |
| `ITcPlcLibraryManager` | Library/placeholder/repository CRUD |
| `ITcConfigManager` | Target platform selection |
| `ITcRemoteManager` | Switch between TwinCAT versions |
