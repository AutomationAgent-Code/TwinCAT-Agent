param([string]$Command = '', [string]$ArgsJson = '', [string]$ArgsFile = '')
# =====================================================================
#  TcCom.ps1 — TwinCAT Automation Interface via PowerShell (no Python COM)
#  与 tc_template/plc.py 的 COM 操作等价；纯 IDispatch 晚绑定，无需 interop 程序集。
#
#  两种用法:
#    (1) dot-source 载入函数库 (文件须为 UTF-8 BOM):
#          . .\TcCom.ps1
#          $dte = Connect-Tc
#          Read-Pou  $dte 'MAIN'
#          Write-Pou $dte 'MAIN' implementation "nCounter := nCounter + 1;"
#          New-Pou   $dte 'FB_Test' fb
#          Invoke-TcBuild $dte
#
#    (2) 作为 JSON 桥 (被 Python _ps_bridge.py 调用, 输出单行 JSON 到 stdout):
#          powershell -NoProfile -File TcCom.ps1 -Command read-pou -ArgsJson '{"name":"MAIN"}'
#          -> {"ok":true,"data":{...}}   或   {"ok":false,"error":"..."}
#
#  关键设计 / 踩过的坑:
#    1. .DeclarationText / .ImplementationText 可直接晚绑定读写, 无需 CastTo
#       (Python 3.14 强制动态绑定才需要 CastTo("ITcPlcDeclaration"))。
#    2. COM 树项实现 IEnumVARIANT(可 foreach)。若把它跨 return 输出, PowerShell
#       管道会"展开"成其子节点 → .DeclarationText 变成对 N 个子节点的成员枚举。
#       对策: COM 项绝不跨函数边界; 遍历+读写在同一 foreach 作用域内完成,
#       函数只返回纯数据(pscustomobject/string)。
#    3. 不要 ExecuteCommand("TwinCAT.ClearErrorList"): 装了 Analytics 扩展时命中
#       遗留调试 MessageBox(原生 Win32 弹窗), SilentMode / SuppressUI 都拦不住。
#    4. 中文路径名(如 'xxx项目')只用运行时 COM 名比较; 不要在 .ps1 里写中文字面量
#       做匹配 —— PS 5.1 读无 BOM 脚本会误解码。本文件已存为 UTF-8 BOM。
#    5. dispatcher 里 Connect-Tc 的返回值必须赋给变量($dte = ...), 否则 COM 对象
#       会泄漏到 stdout, 破坏 JSON 输出。
# =====================================================================

$tcHmiBindingModule = Join-Path $PSScriptRoot 'TcHmiBinding.ps1'
if (Test-Path -LiteralPath $tcHmiBindingModule) { . $tcHmiBindingModule }
$tcHmiServerModule = Join-Path $PSScriptRoot 'TcHmiServer.ps1'
$tcHmiItemsModule = Join-Path $PSScriptRoot 'TcHmiItems.ps1'
. $tcHmiItemsModule
$tcHmiProjectModule = Join-Path $PSScriptRoot 'TcHmiProject.ps1'
if (Test-Path -LiteralPath $tcHmiProjectModule) { . $tcHmiProjectModule }
if (Test-Path -LiteralPath $tcHmiServerModule) { . $tcHmiServerModule }

# ---- ROT 枚举 + 前台窗口判定 (支持同时打开多个 XAE 实例) ----
#  GetActiveObject 只会返回"最先注册到 ROT"的那个实例, 所以同时开两个方案时
#  永远抓到旧的那个, 必须关掉一个才行。这里改为枚举 ROT 里所有 DTE, 再按
#  "当前前台窗口所属进程"挑选, 使助手跟随用户正在操作的那个 XAE。
$script:TcRotLoadError = ''
if (-not ('TcRot' -as [type])) {
  try {
    Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
public static class TcRot {
    [DllImport("ole32.dll")] static extern int GetRunningObjectTable(int r, out IRunningObjectTable prot);
    [DllImport("ole32.dll")] static extern int CreateBindCtx(int r, out IBindCtx ppbc);
    [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] static extern int GetWindowThreadProcessId(IntPtr hWnd, out int pid);

    public static int ForegroundPid() {
        int p = 0; GetWindowThreadProcessId(GetForegroundWindow(), out p); return p;
    }
    public static int WindowPid(IntPtr hwnd) {
        int p = 0; GetWindowThreadProcessId(hwnd, out p); return p;
    }
    // 返回 [displayName, comObject] 列表
    public static List<object[]> Running() {
        var res = new List<object[]>();
        IRunningObjectTable rot; IBindCtx ctx;
        if (GetRunningObjectTable(0, out rot) != 0 || CreateBindCtx(0, out ctx) != 0) return res;
        IEnumMoniker en; rot.EnumRunning(out en); en.Reset();
        IMoniker[] mon = new IMoniker[1];
        while (en.Next(1, mon, IntPtr.Zero) == 0) {
            string name = null; object obj = null;
            try { mon[0].GetDisplayName(ctx, null, out name); } catch { }
            if (name == null) continue;
            try { rot.GetObject(mon[0], out obj); } catch { }
            if (obj != null) res.Add(new object[] { name, obj });
        }
        return res;
    }
}
'@ -ErrorAction Stop
  } catch {
    # 某些客户机被 WDAC/AppLocker 限制为 ConstrainedLanguage，Add-Type 会被禁止。
    # 不让这里的可选 ROT 枚举器拖垮全部 COM 功能；Connect-Tc 还有纯 .NET 回退。
    $script:TcRotLoadError = [string]$_.Exception.Message
  }
}
$script:TcRotAvailable = $null -ne ('TcRot' -as [type])

$script:TcChosenPid = 0

function Get-TcWindowPid {
    param([IntPtr]$Hwnd)
    if ($Hwnd -eq [IntPtr]::Zero) { return 0 }
    if ($script:TcRotAvailable) {
        try { return [int][TcRot]::WindowPid($Hwnd) } catch { }
    }
    # 无 Add-Type 回退：DTE.MainWindow.HWnd 是 XAE 的顶层窗口句柄，直接从
    # Get-Process 匹配即可，不需要 user32 P/Invoke。
    try {
        $handle = [int64]$Hwnd
        $proc = Get-Process -ErrorAction SilentlyContinue |
            Where-Object { [int64]$_.MainWindowHandle -eq $handle } |
            Select-Object -First 1
        if ($proc) { return [int]$proc.Id }
    } catch { }
    return 0
}

function Connect-Tc {
    # 4026 = TcXaeShell.DTE.17.0; 4024/4022 = 15.0/14.0; VS2022 集成 = VisualStudio.DTE.17.0
    #  $PreferPid: 调用方(后端)上次绑定的实例。前台不是 XAE 时优先沿用它, 避免
    #  用户切到别的程序时后台轮询把面板拽到另一个实例上(抖动)。
    param([string[]]$ProgIds = @(
        'TcXaeShell.DTE.17.0','TcXaeShell.DTE.15.0','TcXaeShell.DTE.14.0',
        'VisualStudio.DTE.17.0','VisualStudio.DTE.15.0'),
        [int]$PreferPid = 0,
        [bool]$Sticky = $true,
        [bool]$StrictPid = $false)

    # 优先: 枚举 ROT, 按前台进程挑实例 (moniker 形如 !TcXaeShell.DTE.15.0:12345)
    if ($script:TcRotAvailable) { try {
        $fg = [TcRot]::ForegroundPid()
        $cands = @()
        $running = @([TcRot]::Running())
        foreach ($e in $running) {
            $name = [string]$e[0]
            foreach ($pid_ in $ProgIds) {
                if ($name -like "*$pid_*") {
                    $procId = 0
                    if ($name -match ':(\d+)\s*$') { $procId = [int]$Matches[1] }
                    if ($procId -le 0) {
                        try {
                            $hwnd = [IntPtr][int64]$e[1].MainWindow.HWnd
                            $procId = Get-TcWindowPid $hwnd
                        } catch { }
                    }
                    $solution = ''
                    try { $solution = [string]$e[1].Solution.FullName } catch { }
                    $cands += [pscustomobject]@{
                        Name = $name; Pid = $procId; Solution = $solution; Obj = $e[1] }
                    break
                }
            }
        }
        # 4024 的部分隔离 Shell 使用非标准 ROT moniker。已知 ProgID 没命中时，
        # 再通过 DTE.Name/MainWindow/Solution 做窄范围识别，避免只能靠 ProgID。
        if ($cands.Count -eq 0) {
            foreach ($e in $running) {
                try {
                    $obj = $e[1]
                    $dteName = [string]$obj.Name
                    if ($dteName -notmatch '(?i)(TcXaeShell|Visual Studio)') { continue }
                    $hwnd = [IntPtr][int64]$obj.MainWindow.HWnd
                    $procId = Get-TcWindowPid $hwnd
                    $solution = ''
                    try { $solution = [string]$obj.Solution.FullName } catch { }
                    $cands += [pscustomobject]@{
                        Name = [string]$e[0]; Pid = $procId; Solution = $solution; Obj = $obj }
                } catch { }
            }
        }
        if ($cands.Count -gt 0) {
            $hit = $null
            # 粘性(后台轮询): 只要已绑定的实例还活着就继续用它, 不被新开/前台的
            # XAE 抢走 —— 否则每开一个 XAE 面板都会被拽过去。
            if ($Sticky -and $PreferPid -gt 0) {
                $sameProcess = @($cands | Where-Object { $_.Pid -eq $PreferPid })
                $hit = $sameProcess | Where-Object { $_.Solution } | Select-Object -First 1
                if (-not $hit) { $hit = $sameProcess | Select-Object -First 1 }
                if (-not $hit -and $StrictPid) {
                    throw ("XAE PID $PreferPid is running, but its DTE is not visible in this " +
                           "COM/ROT session. Ensure TwinCAT Agent and XAE run at the same " +
                           "Windows privilege level; do not run XAE as administrator.")
                }
            }
            # 首次绑定 或 手动刷新(非粘性): 用当前前台的那个 XAE
            if (-not $hit) {
                $foreground = @($cands | Where-Object { $_.Pid -eq $fg })
                $hit = $foreground | Where-Object { $_.Solution } | Select-Object -First 1
                if (-not $hit) { $hit = $foreground | Select-Object -First 1 }
            }
            # 绑定实例已关闭时的兜底
            if (-not $hit -and $PreferPid -gt 0) {
                $hit = $cands | Where-Object { $_.Pid -eq $PreferPid } | Select-Object -First 1
            }
            if (-not $hit) { $hit = $cands | Where-Object { $_.Solution } | Select-Object -First 1 }
            if (-not $hit) { $hit = $cands | Select-Object -First 1 }
            $script:TcChosenPid = $hit.Pid
            return $hit.Obj
        }
        if ($StrictPid -and $PreferPid -gt 0) {
            throw ("XAE PID $PreferPid is running, but no matching DTE was found in ROT. " +
                   "Ensure TwinCAT Agent and XAE run at the same Windows privilege level; " +
                   "do not run XAE as administrator.")
        }
    } catch {
        if ($StrictPid) { throw }
    } }

    # 回退: 单实例场景的老路径
    foreach ($progId in $ProgIds) {
        try {
            $obj = [System.Runtime.InteropServices.Marshal]::GetActiveObject($progId)
            $actualPid = 0
            try {
                $hwnd = [IntPtr][int64]$obj.MainWindow.HWnd
                $actualPid = Get-TcWindowPid $hwnd
            } catch { }
            if ($StrictPid -and $PreferPid -gt 0 -and
                $actualPid -gt 0 -and $actualPid -ne $PreferPid) { continue }
            $script:TcChosenPid = if ($actualPid -gt 0) { $actualPid } else { $PreferPid }
            return $obj
        } catch { }
    }
    if ($StrictPid -and $PreferPid -gt 0) {
        $detail = if ($script:TcRotLoadError) {
            " TcRot unavailable: $($script:TcRotLoadError)"
        } else { '' }
        throw ("XAE PID $PreferPid is running, but its DTE could not be bound." +
               " Ensure TwinCAT Agent and XAE run at the same Windows privilege level." + $detail)
    }
    throw 'No running TwinCAT/VS DTE found. Open TcXaeShell (or VS+XAE) first.'
}

function Set-TcSilentMode {
    # Automation Interface Silent Mode (>= 3.1.4020): 抑制走 AI 的对话框。
    # 注意: 拦不住第三方扩展的原生 MessageBox。
    param([Parameter(Mandatory)]$Dte, [bool]$On = $true)
    try { $s = $Dte.GetObject('TcAutomationSettings'); $s.SilentMode = $On } catch { }
}

# ---- 定位当前解决方案中的 TwinCAT System Manager 项目 ----
function Get-TcSystemProject {
    param([Parameter(Mandatory)]$Dte)
    $count = 0
    try { $count = [int]$Dte.Solution.Projects.Count } catch { }
    for ($i = 1; $i -le $count; $i++) {
        try {
            $project = $Dte.Solution.Projects.Item($i)
            $sys = $project.Object
            $sys.LookupTreeItem('TIID') | Out-Null
            return ,$project
        } catch { }
    }
    throw 'TwinCAT System Manager project not found in the current solution.'
}

function Get-TcSystemManager {
    param([Parameter(Mandatory)]$Dte)
    $project = Get-TcSystemProject $Dte
    return ,$project.Object
}

function Get-TcTarget {
    param([Parameter(Mandatory)]$Dte)
    $sys = Get-TcSystemManager $Dte
    [pscustomobject]@{
        target_netid = [string]$sys.GetTargetNetId()
        solution     = [string]$Dte.Solution.FullName
    }
}

function Set-TcTarget {
    param([Parameter(Mandatory)]$Dte, [Parameter(Mandatory)][string]$NetId)
    $value = $NetId.Trim()
    $parts = @($value -split '\.')
    if ($parts.Count -ne 6) { throw "Invalid AMS NetId '$value': expected six numeric parts." }
    foreach ($part in $parts) {
        $n = 0
        if (-not [int]::TryParse($part, [ref]$n) -or $n -lt 0 -or $n -gt 255) {
            throw "Invalid AMS NetId '$value': every part must be between 0 and 255."
        }
    }
    $sys = Get-TcSystemManager $Dte
    $previous = [string]$sys.GetTargetNetId()
    $sys.SetTargetNetId($value)
    try { $Dte.ExecuteCommand('File.SaveAll') } catch { }
    [pscustomobject]@{
        previous_netid = $previous
        target_netid   = [string]$sys.GetTargetNetId()
        solution       = [string]$Dte.Solution.FullName
    }
}

# ---- SYSTEM / Real-Time tree and CPU settings ----
# Generic SYSTEM CRUD is intentionally restricted to TIRC/TIRT.  TIRS is a
# settings document and must be changed through Set-TcSystemSettings so the
# allow-list and readback checks cannot be bypassed.
function Get-TcSystemPath {
    param([Parameter(Mandatory)][string]$Path, [bool]$Mutation = $false,
          [bool]$AllowRoot = $true)
    $value = $Path.Trim().Trim('^').Replace('/', '^')
    $parts = @($value -split '\^' | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
        ForEach-Object { ([string]$_).Trim() })
    if ($parts.Count -eq 0) { throw 'SYSTEM tree path must not be empty.' }
    $readRoots = @('TIRC','TIRS','TIRT')
    $writeRoots = @('TIRC','TIRT')
    $roots = if ($Mutation) { $writeRoots } else { $readRoots }
    $root = ([string]$parts[0]).ToUpperInvariant()
    if ($root -notin $roots) {
        throw ('SYSTEM tree path must start with one of: ' + ($roots -join ', '))
    }
    if (-not $AllowRoot -and $parts.Count -eq 1) {
        throw "Refusing to operate on SYSTEM root '$root'."
    }
    foreach ($part in $parts) {
        if ($part -in @('.', '..') -or $part.Contains('^')) {
            throw 'SYSTEM tree path contains an invalid segment.'
        }
    }
    return ($parts -join '^')
}

function Get-TcSystemTreeNode {
    param([Parameter(Mandatory)]$Item, [Parameter(Mandatory)][string]$Path,
          [int]$Depth = 0, [int]$MaxDepth = 6)
    $name = [string]$Item.Name
    $actualPath = [string]$Item.PathName
    if ([string]::IsNullOrWhiteSpace($actualPath)) { $actualPath = $Path }
    $children = @()
    if ($Depth -lt $MaxDepth) {
        foreach ($child in $Item) {
            $childName = [string]$child.Name
            if ([string]::IsNullOrWhiteSpace($childName)) { continue }
            $childPath = [string]$child.PathName
            if ([string]::IsNullOrWhiteSpace($childPath)) { $childPath = "$actualPath^$childName" }
            $children += Get-TcSystemTreeNode $child $childPath ($Depth + 1) $MaxDepth
        }
    }
    [pscustomobject]@{
        name = $name; path = $actualPath
        item_type = [int]$Item.ItemType
        item_subtype = [int]$Item.ItemSubType
        children = @($children)
    }
}

function Get-TcSystemStructure {
    param([Parameter(Mandatory)]$Dte, [int]$MaxDepth = 6, [object[]]$Roots = $null)
    $sys = Get-TcSystemManager $Dte
    $requested = if ($Roots -and $Roots.Count) {
        @($Roots | ForEach-Object { ([string]$_).Trim().ToUpperInvariant() })
    } else { @('TIRC','TIRS','TIRT') }
    foreach ($root in $requested) { [void](Get-TcSystemPath $root) }
    $found = @(); $missing = @()
    foreach ($root in $requested) {
        try {
            $item = $sys.LookupTreeItem($root)
            $found += Get-TcSystemTreeNode $item $root 0 ([Math]::Max(0, [Math]::Min($MaxDepth, 12)))
        } catch {
            $missing += [pscustomobject]@{ root = $root; error = [string]$_.Exception.Message }
        }
    }
    [pscustomobject]@{
        status = 'ok'; target_netid = [string]$sys.GetTargetNetId()
        max_depth = [Math]::Max(0, [Math]::Min($MaxDepth, 12))
        roots = @($found); missing_roots = @($missing)
    }
}

function Convert-TcSystemInteger {
    param([Parameter(Mandatory)]$Value, [Parameter(Mandatory)][string]$Field,
          [long]$Maximum = 9223372036854775807)
    $text = ([string]$Value).Trim(); [long]$number = 0
    try {
        if ($text -match '^(0x|#x)[0-9a-fA-F]+$') {
            $number = [Convert]::ToInt64($text.Substring(2), 16)
        } else { $number = [Convert]::ToInt64($text, 10) }
    } catch { throw "$Field must be an integer." }
    if ($number -lt 0 -or $number -gt $Maximum) {
        throw "$Field must be between 0 and $Maximum."
    }
    return $number
}

function Get-TcXmlLocalName {
    param([Parameter(Mandatory)]$Node)
    $name = [string]$Node.LocalName
    if ([string]::IsNullOrWhiteSpace($name)) { $name = [string]$Node.Name }
    return $name
}

function Get-TcRealtimeVersionInfo {
    param([Parameter(Mandatory)]$Dte, [Parameter(Mandatory)][xml]$Xml)
    $version = ''; $build = 0; $revision = 0; $source = 'unknown'
    try {
        foreach ($project in @($Dte.Solution.Projects)) {
            $file = [string]$project.FullName
            if (-not $file.EndsWith('.tsproj', [System.StringComparison]::OrdinalIgnoreCase) -or
                -not (Test-Path -LiteralPath $file)) { continue }
            [xml]$projectXml = Get-Content -LiteralPath $file -Raw -ErrorAction Stop
            $version = [string]$projectXml.DocumentElement.GetAttribute('TcVersion')
            if ($version -match '^\d+\.\d+\.(\d+)(?:\.(\d+))?') {
                $build = [int]$matches[1]
                if ($matches[2]) { $revision = [int]$matches[2] }
                $source = 'project'
                break
            }
        }
    } catch { }
    $inferred = $false
    if ($build -ge 4026) { $family = '4026' }
    elseif ($build -gt 0 -and $build -le 4024) { $family = '4024' }
    elseif ($Xml.SelectSingleNode("//*[local-name()='CoreBoostActive' or local-name()='CpuMemorySize']")) {
        $family = '4026'; $source = 'xml_feature_inference'; $inferred = $true
    } else { $family = 'unknown' }
    $semantics = if ($family -eq '4024') { 'combined_global_rt_and_ads_memory' }
                 elseif ($family -eq '4026') { 'global_rt_memory_ads_separate' }
                 else { 'unknown_version_do_not_assume' }
    $applyMode = if ($family -eq '4024') { 'target_reboot' }
                 elseif ($family -eq '4026') { 'activate_configuration' }
                 else { 'version_dependent' }
    [pscustomobject]@{
        version=$version; build=if ($build) { $build } else { $null }; revision=$revision
        family=$family; source=$source; inferred=$inferred
        router_memory_semantics=$semantics; router_memory_apply=$applyMode
        core_memory_supported=($family -eq '4026'); ads_memory_separate=($family -eq '4026')
    }
}

function Get-TcSystemSettingsSummary {
    param([Parameter(Mandatory)][xml]$Xml, $VersionInfo = $null)
    # TIRS versions differ: some expose Settings attributes, while the
    # 4024/4026 XML uses RTimeSetDef with child elements.
    $settings = $Xml.SelectSingleNode("//*[local-name()='Settings' or local-name()='RTimeSetDef']")
    if ($null -eq $settings) {
        return [pscustomobject]@{ attributes = [ordered]@{}; cpu_ids = @(); tasks = @(); settings_found = $false }
    }
    $attributes = [ordered]@{}
    foreach ($attribute in @($settings.Attributes)) {
        $attributes[[string]$attribute.Name] = [string]$attribute.Value
    }
    foreach ($child in @($settings.ChildNodes)) {
        if ($child.NodeType -eq [System.Xml.XmlNodeType]::Element -and
            $child.ChildNodes.Count -eq 1 -and
            $child.ChildNodes[0].NodeType -eq [System.Xml.XmlNodeType]::Text -and
            $null -ne $child.InnerText) {
            if (-not $attributes.Contains([string]$child.LocalName)) {
                $attributes[[string]$child.LocalName] = [string]$child.InnerText
            }
        }
    }
    $cpuIds = @()
    $cpuContainer = $settings.SelectSingleNode("./*[local-name()='CPUs']")
    $cpuNodes = if ($cpuContainer) {
        @($cpuContainer.SelectNodes("./*[local-name()='CPU' or local-name()='Cpu']"))
    } else {
        @($settings.SelectNodes("./*[local-name()='CPU' or local-name()='Cpu']"))
    }
    $cpuById = @{}
    foreach ($cpu in $cpuNodes) {
        $attr = $cpu.Attributes | Where-Object { [string]$_.Name -ieq 'CpuId' -or [string]$_.Name -ieq 'id' } | Select-Object -First 1
        if ($null -eq $attr) { continue }
        try {
            $id = Convert-TcSystemInteger $attr.Value 'cpu_id' 4095
            $cpuIds += $id; $cpuById[[int]$id] = $cpu
        } catch { }
    }
    $tasks = New-Object System.Collections.ArrayList
    $taskNodes = @($Xml.SelectNodes("//*[local-name()='Task' or local-name()='TaskDef']"))
    foreach ($task in $taskNodes) {
        $entry = [ordered]@{}
        foreach ($attribute in @($task.Attributes)) { $entry[[string]$attribute.Name] = [string]$attribute.Value }
        foreach ($child in @($task.ChildNodes)) {
            if ($child.NodeType -eq [System.Xml.XmlNodeType]::Element -and
                $child.ChildNodes.Count -eq 1 -and $null -ne $child.InnerText) {
                $entry[[string]$child.LocalName] = [string]$child.InnerText
            }
        }
        [void]$tasks.Add([pscustomobject]$entry)
    }
    $getRaw = {
        param([string]$Name)
        $attr = $settings.Attributes | Where-Object { [string]$_.Name -ieq $Name } | Select-Object -First 1
        if ($attr) { return [string]$attr.Value }
        $child = $settings.ChildNodes | Where-Object {
            $_.NodeType -eq [System.Xml.XmlNodeType]::Element -and [string]$_.LocalName -ieq $Name
        } | Select-Object -First 1
        if ($child) { return [string]$child.InnerText }
        return $null
    }
    $getAttr = {
        param([string]$Name)
        $raw = & $getRaw $Name
        if ($null -eq $raw) { return $null }
        try { return (Convert-TcSystemInteger $raw $Name) } catch { return [string]$raw }
    }
    $targetCpuInfo = $settings.SelectSingleNode("./*[local-name()='TargetCPUInfo']")
    $getTargetAttr = {
        param([string[]]$Names)
        if ($null -eq $targetCpuInfo) { return $null }
        foreach ($name in $Names) {
            $node = $targetCpuInfo.ChildNodes | Where-Object {
                $_.NodeType -eq [System.Xml.XmlNodeType]::Element -and
                [string]$_.LocalName -ieq $name
            } | Select-Object -First 1
            if ($node) {
                try { return (Convert-TcSystemInteger $node.InnerText $name) }
                catch { return $null }
            }
        }
        return $null
    }
    $availableCpus = & $getTargetAttr @('AvailabeCPUs', 'AvailableCPUs')
    $realTimeCpus = & $getTargetAttr @('RealTimeCPUs')
    $targetPCoreAffinity = & $getTargetAttr @('PCoreAffinity')
    $targetECoreAffinity = & $getTargetAttr @('ECoreAffinity')
    $currentAffinity = & $getAttr 'Affinity'
    $maskLengths = @($targetPCoreAffinity, $targetECoreAffinity, $currentAffinity) |
        Where-Object { $_ -is [int64] -or $_ -is [int32] } |
        ForEach-Object { ([long]$_).ToString('X').Length * 4 }
    $highestConfigured = if (@($cpuIds).Count) { ([int](@($cpuIds) | Measure-Object -Maximum).Maximum) + 1 } else { 0 }
    $coreCount = [Math]::Max([int]($availableCpus -as [int]), [Math]::Max([int]($highestConfigured), [int](($maskLengths | Measure-Object -Maximum).Maximum)))
    $coreCount = [Math]::Min([Math]::Max($coreCount, 0), 64)
    $cores = New-Object System.Collections.ArrayList
    for ($coreId = 0; $coreId -lt $coreCount; $coreId++) {
        $bit = [long]1 -shl $coreId
        $coreType = if ($targetPCoreAffinity -is [int64] -and (($targetPCoreAffinity -band $bit) -ne 0)) { 'P' }
                    elseif ($targetECoreAffinity -is [int64] -and (($targetECoreAffinity -band $bit) -ne 0)) { 'E' }
                    else { 'Unknown' }
        $cpuNode = if ($cpuById.ContainsKey($coreId)) { $cpuById[$coreId] } else { $null }
        $getCpuValue = {
            param([string]$Name)
            if ($null -eq $cpuNode) { return $null }
            $attr = $cpuNode.Attributes | Where-Object { [string]$_.Name -ieq $Name } | Select-Object -First 1
            $child = $cpuNode.ChildNodes | Where-Object {
                $_.NodeType -eq [System.Xml.XmlNodeType]::Element -and [string]$_.LocalName -ieq $Name
            } | Select-Object -First 1
            $raw = if ($attr) { $attr.Value } elseif ($child) { $child.InnerText } else { $null }
            if ($null -eq $raw) { return $null }
            try { return (Convert-TcSystemInteger $raw $Name) } catch { return $null }
        }
        $baseTime = & $getCpuValue 'BaseTime'
        $latencyWarning = & $getCpuValue 'LatencyWarning'
        $coreMemory = & $getCpuValue 'CpuMemorySize'
        [void]$cores.Add([pscustomobject]@{
            id = $coreId; label = "$coreId ($coreType)"; core_type = $coreType
            selected = ($currentAffinity -is [int64] -and (($currentAffinity -band $bit) -ne 0))
            configured = @($cpuIds) -contains $coreId
            load_limit_percent = & $getCpuValue 'LoadLimit'
            base_time_100ns = $baseTime
            base_time_us = if ($null -ne $baseTime) { [double]$baseTime / 10.0 } else { $null }
            latency_warning_100ns = $latencyWarning
            latency_warning_us = if ($null -ne $latencyWarning) { [double]$latencyWarning / 10.0 } else { $null }
            core_memory_bytes = $coreMemory
            core_memory_kb = if ($null -ne $coreMemory) { [double]$coreMemory / 1024.0 } else { $null }
            core_frequency = & $getCpuValue 'CoreFrequency'
            core_memory_allocation_limit = & $getCpuValue 'CpuMemoryAllocLimit'
        })
    }
    if ($null -eq $VersionInfo) {
        $VersionInfo = [pscustomobject]@{ family='unknown'; version=''; build=$null; revision=$null
            source='unknown'; inferred=$false; router_memory_semantics='unknown_version_do_not_assume'
            router_memory_apply='version_dependent'; core_memory_supported=$false; ads_memory_separate=$false }
    }
    $routerMemoryKb = & $getAttr 'RouterMemory'
    $routerMemoryMb = if ($routerMemoryKb -is [int64] -or $routerMemoryKb -is [int32]) {
        [double]$routerMemoryKb / 1024.0
    } else { $null }
    $adsEstimate = if ($VersionInfo.family -eq '4026' -and $null -ne $routerMemoryMb) {
        [Math]::Max(4.0, [Math]::Min(32.0, $routerMemoryMb * 0.25))
    } else { $null }
    [pscustomobject]@{
        attributes = $attributes
        max_cpus = & $getAttr 'MaxCpus'
        p_core_affinity = & $getAttr 'PCoreAffinity'
        e_core_affinity = & $getAttr 'ECoreAffinity'
        cpu_ids = @($cpuIds); tasks = @($tasks.ToArray()); settings_found = $true
        available_cpus = $availableCpus; real_time_cpus = $realTimeCpus
        affinity = $currentAffinity
        twincat = $VersionInfo
        router_memory_raw_kb = $routerMemoryKb
        router_memory_mb = $routerMemoryMb
        max_task_stack_kb = & $getAttr 'MaxStackSize'
        max_task_dumps = & $getAttr 'MaxTaskDumps'
        global_ads_memory_estimated_mb = $adsEstimate
        target_p_core_affinity = $targetPCoreAffinity
        target_e_core_affinity = $targetECoreAffinity
        cores = @($cores.ToArray())
    }
}

function Get-TcSystemSettings {
    param([Parameter(Mandatory)]$Dte)
    $sys = Get-TcSystemManager $Dte
    $item = $sys.LookupTreeItem('TIRS')
    [xml]$xml = [string]$item.ProduceXml($false)
    $versionInfo = Get-TcRealtimeVersionInfo $Dte $xml
    $summary = Get-TcSystemSettingsSummary $xml $versionInfo
    [pscustomobject]@{ status = 'ok'; path = 'TIRS'; xml = $xml.OuterXml } |
        Add-Member -NotePropertyMembers @{
            attributes = $summary.attributes; max_cpus = $summary.max_cpus
            p_core_affinity = $summary.p_core_affinity; e_core_affinity = $summary.e_core_affinity
            cpu_ids = $summary.cpu_ids; tasks = $summary.tasks; settings_found = $summary.settings_found
            available_cpus = $summary.available_cpus; real_time_cpus = $summary.real_time_cpus
            affinity = $summary.affinity
            target_p_core_affinity = $summary.target_p_core_affinity
            target_e_core_affinity = $summary.target_e_core_affinity
            cores = $summary.cores
            twincat = $summary.twincat
            router_memory_raw_kb = $summary.router_memory_raw_kb
            router_memory_mb = $summary.router_memory_mb
            max_task_stack_kb = $summary.max_task_stack_kb
            max_task_dumps = $summary.max_task_dumps
            global_ads_memory_estimated_mb = $summary.global_ads_memory_estimated_mb
        } -PassThru
}

function Invoke-TcRealtimeReadButton {
    <# Invoke the XAE Settings page's "Read from Target" button via UIA.

       The control is exposed as a Pane with a native Button handle, so use
       BM_CLICK rather than an InvokePattern or mouse/SendKeys operation.
       It invokes a visible/occluded XAE control without bringing the XAE
       window to the foreground.
    #>
    param([Parameter(Mandatory)]$Dte)
    try {
        Add-Type -AssemblyName UIAutomationClient -ErrorAction Stop
        Add-Type -AssemblyName UIAutomationTypes -ErrorAction Stop
        $hwnd = [IntPtr]::Zero
        try { $hwnd = [IntPtr][int64]$Dte.MainWindow.HWnd } catch { }
        if ($hwnd -eq [IntPtr]::Zero -and $script:TcChosenPid -gt 0) {
            try {
                # Some TcXaeShell DTE proxies hide MainWindow.HWnd even though
                # Connect-Tc already identified the process from the ROT.
                # Resolve the same process through its public main-window
                # handle; this does not activate or foreground the window.
                $process = Get-Process -Id ([int]$script:TcChosenPid) -ErrorAction Stop
                $hwnd = [IntPtr][int64]$process.MainWindowHandle
            } catch { }
        }
        if ($hwnd -eq [IntPtr]::Zero) { return $null }
        $root = [System.Windows.Automation.AutomationElement]::FromHandle($hwnd)
        if ($null -eq $root) { return $null }
        # The XAE designer reports this control as ControlType.Pane, even
        # though its NativeWindowHandle belongs to a real Win32 Button.
        # Search the complete UIA subtree instead of filtering on ControlType.
        $condition = [System.Windows.Automation.Condition]::TrueCondition
        $buttons = $root.FindAll(
            [System.Windows.Automation.TreeScope]::Descendants, $condition)
        if (-not ('TcUiNative' -as [type])) {
            Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class TcUiNative {
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern IntPtr SendMessage(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam);
}
'@ -ErrorAction Stop
        }
        foreach ($button in $buttons) {
            try {
                $automationId = [string]$button.Current.AutomationId
                $name = [string]$button.Current.Name
                $isReadButton = ($automationId -eq '1508') -or
                    ($name -match '^(从目标读取|Read from Target)$')
                if (-not $isReadButton) { continue }
                if (-not [bool]$button.Current.IsEnabled) { continue }
                $nativeHandle = [IntPtr]$button.Current.NativeWindowHandle
                if ($nativeHandle -eq [IntPtr]::Zero) { continue }
                # BM_CLICK (0x00F5) invokes a standard Button control without
                # activating its parent window or moving keyboard focus.
                [void][TcUiNative]::SendMessage(
                    $nativeHandle, [uint32]0x00F5,
                    [IntPtr]::Zero, [IntPtr]::Zero)
                return [pscustomobject]@{
                    status = 'refreshed'
                    method = 'UIAutomation.NativeButton.BM_CLICK'
                    control = if ($name) { $name } else { '1508' }
                    automation_id = $automationId
                    native_window_handle = [int64]$nativeHandle
                    focus_changed = $false
                    configuration_changed = $false
                    note = '已调用 XAE Real-Time 的“从目标读取”；未写配置、未激活、未重启。'
                }
            } catch { }
        }
    } catch { }
    return $null
}

function Invoke-TcRealtimeRefresh {
    <#
      Refresh only the currently active TwinCAT Real-Time designer.

      ProduceXml/ConsumeXml operate on the Automation Interface model, but
      the open XAE designer is a separate view and does not repaint merely
      because that model was read.  The primary path invokes the actual
      Settings page button (AutomationId 1508) with UI Automation.  DTE
      command aliases remain a compatibility fallback for XAE versions that
      do not expose that control through UI Automation.
    #>
    param([Parameter(Mandatory)]$Dte)
    $buttonResult = Invoke-TcRealtimeReadButton $Dte
    if ($null -ne $buttonResult) { return $buttonResult }
    $names = New-Object System.Collections.ArrayList
    foreach ($name in @('TwinCAT.刷新', 'TwinCAT.Refresh')) {
        if (-not $names.Contains($name)) { [void]$names.Add($name) }
    }
    try {
        foreach ($command in @($Dte.Commands)) {
            try {
                $name = [string]$command.Name
                if ($name -match '(?i)^TwinCAT\.(刷新|Refresh)$' -and
                    -not $names.Contains($name)) {
                    [void]$names.Add($name)
                }
            } catch { }
        }
    } catch { }
    $errors = New-Object System.Collections.ArrayList
    foreach ($name in @($names)) {
        try {
            $Dte.ExecuteCommand([string]$name)
            return [pscustomobject]@{
                status = 'refreshed'
                command = [string]$name
                focus_changed = $false
                configuration_changed = $false
                note = '仅刷新当前已打开的 XAE Real-Time 页面；未写配置、未激活、未重启。'
            }
        } catch {
            [void]$errors.Add(('{0}: {1}' -f $name, [string]$_.Exception.Message))
        }
    }
    $detail = if ($errors.Count) { $errors -join '; ' } else { '没有可用的 TwinCAT 刷新命令' }
    throw ('Realtime 页面未刷新。请先在 XAE 中打开并选中 SYSTEM > Real-Time > Settings；' +
        '本工具不会自动切换页面或抢焦点。' + " $detail")
}

function Get-TcSystemSettingsPatch {
    param([Parameter(Mandatory)]$Settings, [Parameter(Mandatory)]$Current)
    if ($null -eq $Settings) { throw 'settings must be a non-empty object.' }
    $patch = [ordered]@{}
    foreach ($property in @($Settings.PSObject.Properties)) {
        $key = ([string]$property.Name).Trim().ToLowerInvariant().Replace('-', '_')
        switch ($key) {
            'max_cpus' { $patch['MaxCpus'] = Convert-TcSystemInteger $property.Value 'max_cpus' 4095; if ($patch['MaxCpus'] -le 0) { throw 'max_cpus must be greater than zero.' } }
            'cpu_ids' {
                $values = @($property.Value)
                if ($values.Count -eq 0 -or ($values.Count -eq 1 -and $values[0] -is [string])) { throw 'cpu_ids must be a non-empty array.' }
                $ids = @($values | ForEach-Object { Convert-TcSystemInteger $_ 'cpu_ids' 4095 })
                if (@($ids | Select-Object -Unique).Count -ne $ids.Count) { throw 'cpu_ids must not contain duplicates.' }
                $patch['CpuIds'] = @($ids | Sort-Object)
            }
            'p_core_affinity' { $patch['PCoreAffinity'] = Convert-TcSystemInteger $property.Value 'p_core_affinity' }
            'affinity' { $patch['Affinity'] = Convert-TcSystemInteger $property.Value 'affinity' }
            'e_core_affinity' { $patch['ECoreAffinity'] = Convert-TcSystemInteger $property.Value 'e_core_affinity' }
            'router_memory_mb' {
                $value = Convert-TcSystemInteger $property.Value 'router_memory_mb' 65535
                if ($value -le 0) { throw 'router_memory_mb must be greater than zero.' }
                if ($Current.twincat.family -eq '4024' -and $value -gt 1024) {
                    throw 'TwinCAT 4024 router_memory_mb must not exceed 1024.'
                }
                if ($Current.twincat.family -eq 'unknown' -and $value -gt 1024) {
                    throw 'router_memory_mb above 1024 requires a confirmed TwinCAT 4026 target.'
                }
                $patch['RouterMemory'] = [long]$value * 1024
            }
            'max_stack_size_kb' {
                $value = Convert-TcSystemInteger $property.Value 'max_stack_size_kb' 1048576
                if ($value -le 0) { throw 'max_stack_size_kb must be greater than zero.' }
                $patch['MaxStackSize'] = $value
            }
            'core_settings' {
                $entries = @($property.Value)
                if (-not $entries.Count) { throw 'core_settings must be a non-empty array.' }
                $normalized = New-Object System.Collections.ArrayList
                $seen = @{}
                foreach ($entry in $entries) {
                    if ($null -eq $entry -or -not ($entry.PSObject.Properties.Name -contains 'cpu_id')) {
                        throw 'each core_settings entry must contain cpu_id.'
                    }
                    $cpuId = Convert-TcSystemInteger $entry.cpu_id 'core_settings.cpu_id' 4095
                    if ($seen.ContainsKey([int]$cpuId)) { throw 'core_settings must not contain duplicate cpu_id values.' }
                    $seen[[int]$cpuId] = $true
                    $item = [ordered]@{ cpu_id = $cpuId }
                    foreach ($coreProperty in @($entry.PSObject.Properties)) {
                        $coreKey = ([string]$coreProperty.Name).Trim().ToLowerInvariant().Replace('-', '_')
                        if ($coreKey -eq 'cpu_id') { continue }
                        switch ($coreKey) {
                            'base_time_100ns' { $item['BaseTime'] = Convert-TcSystemInteger $coreProperty.Value $coreKey 2147483647 }
                            'load_limit_percent' {
                                $limit = Convert-TcSystemInteger $coreProperty.Value $coreKey 100
                                if ($limit -le 0) { throw 'load_limit_percent must be between 1 and 100.' }
                                $item['LoadLimit'] = $limit
                            }
                            'latency_warning_100ns' { $item['LatencyWarning'] = Convert-TcSystemInteger $coreProperty.Value $coreKey 2147483647 }
                            'core_memory_kb' {
                                if ($Current.twincat.family -ne '4026') {
                                    throw "core_memory_kb is not supported on TwinCAT $($Current.twincat.family); it requires a confirmed TwinCAT 4026 target."
                                }
                                $memoryKb = Convert-TcSystemInteger $coreProperty.Value $coreKey 67108864
                                $item['CpuMemorySize'] = [long]$memoryKb * 1024
                            }
                            default { throw "unsupported core setting '$($coreProperty.Name)'." }
                        }
                    }
                    if ($item.Count -le 1) { throw "core_settings entry for CPU $cpuId does not contain a setting." }
                    [void]$normalized.Add([pscustomobject]$item)
                }
                $patch['CoreSettings'] = @($normalized.ToArray())
            }
            default { throw "unsupported SYSTEM setting '$($property.Name)'; allowed: max_cpus, cpu_ids, affinity, p_core_affinity, e_core_affinity, router_memory_mb, max_stack_size_kb, core_settings" }
        }
    }
    $max = if ($patch.Contains('MaxCpus')) { $patch['MaxCpus'] } else { $Current.max_cpus }
    if ($patch.Contains('CpuIds') -and $null -ne $max) {
        $bad = @($patch['CpuIds'] | Where-Object { $_ -ge [int]$max })
        if ($bad.Count) { throw "cpu_ids contains $($bad[0]), but max_cpus is $max." }
    }
    if ($patch.Contains('Affinity') -and $null -ne $max -and
        (([long]$patch['Affinity']) -shr [int]$max) -ne 0) {
        throw 'affinity contains a core outside max_cpus.'
    }
    foreach ($field in @('Affinity', 'PCoreAffinity', 'ECoreAffinity')) {
        if ($patch.Contains($field)) {
            $attributeNames = @()
            try { $attributeNames += @($Current.attributes.Keys) } catch { }
            $attributeNames += @($Current.attributes.PSObject.Properties.Name)
            $present = @($attributeNames | Where-Object { $_ -ieq $field }).Count -gt 0
            if (-not $present) { throw "current TIRS XML does not expose writable $field; read tc_system_settings first." }
        }
    }
    foreach ($field in @('RouterMemory', 'MaxStackSize')) {
        if ($patch.Contains($field)) {
            $attributeNames = @()
            try { $attributeNames += @($Current.attributes.Keys) } catch { }
            $attributeNames += @($Current.attributes.PSObject.Properties.Name)
            if (-not (@($attributeNames | Where-Object { $_ -ieq $field }).Count)) {
                throw "current TIRS XML does not expose writable $field; read tc_system_settings first."
            }
        }
    }
    if ($patch.Contains('CoreSettings')) {
        foreach ($entry in @($patch['CoreSettings'])) {
            if (-not (@($Current.cpu_ids) -contains [int]$entry.cpu_id)) {
                throw "core_settings references CPU $($entry.cpu_id), which is not configured as an RT core."
            }
        }
    }
    return [pscustomobject]$patch
}

function Set-TcSystemSettings {
    param([Parameter(Mandatory)]$Dte, [Parameter(Mandatory)]$Settings,
          [bool]$Apply = $false)
    $sys = Get-TcSystemManager $Dte; $item = $sys.LookupTreeItem('TIRS')
    [xml]$beforeXml = [string]$item.ProduceXml($false)
    $versionInfo = Get-TcRealtimeVersionInfo $Dte $beforeXml
    $before = Get-TcSystemSettingsSummary $beforeXml $versionInfo
    $patch = Get-TcSystemSettingsPatch $Settings $before
    $settingsNode = $beforeXml.SelectSingleNode("//*[local-name()='Settings' or local-name()='RTimeSetDef']")
    if ($null -eq $settingsNode) { throw 'TIRS XML does not contain a writable Settings/RTimeSetDef element.' }
    foreach ($field in @('MaxCpus','Affinity','PCoreAffinity','ECoreAffinity','RouterMemory','MaxStackSize')) {
        if ($patch.PSObject.Properties.Name -contains $field) {
            $attribute = $settingsNode.Attributes | Where-Object { [string]$_.Name -ieq $field } | Select-Object -First 1
            if ($attribute) {
                $attribute.Value = [string]$patch.$field
            } else {
                $element = $settingsNode.ChildNodes | Where-Object {
                    $_.NodeType -eq [System.Xml.XmlNodeType]::Element -and [string]$_.LocalName -ieq $field
                } | Select-Object -First 1
                if ($null -eq $element) { throw "TIRS XML does not contain writable $field." }
                $element.InnerText = [string]$patch.$field
            }
        }
    }
    if ($patch.PSObject.Properties.Name -contains 'CpuIds') {
        $cpuContainer = $settingsNode.SelectSingleNode("./*[local-name()='CPUs']")
        if ($null -eq $cpuContainer) {
            $cpuContainer = $beforeXml.CreateElement('CPUs')
            [void]$settingsNode.AppendChild($cpuContainer)
        }
        $cpuNodes = @($cpuContainer.SelectNodes("./*[local-name()='CPU' or local-name()='Cpu']"))
        $sample = $cpuNodes | Select-Object -First 1
        $cpuElementName = if ($sample) { [string]$sample.LocalName } else { 'CPU' }
        $cpuAttributeName = if ($sample) {
            $candidate = $sample.Attributes | Where-Object { [string]$_.Name -ieq 'CpuId' -or [string]$_.Name -ieq 'id' } | Select-Object -First 1
            if ($candidate) { [string]$candidate.Name } else { 'id' }
        } else { 'id' }
        $existing = @{}
        foreach ($cpu in $cpuNodes) {
            $candidate = $cpu.Attributes | Where-Object {
                [string]$_.Name -ieq 'CpuId' -or [string]$_.Name -ieq 'id'
            } | Select-Object -First 1
            if ($candidate) {
                try { $existing[[int](Convert-TcSystemInteger $candidate.Value 'cpu_id' 4095)] = $cpu } catch { }
            }
            [void]$cpuContainer.RemoveChild($cpu)
        }
        foreach ($id in @($patch.CpuIds)) {
            # Reuse an existing CPU node so per-core settings such as
            # LoadLimit/BaseTime are not lost when adding another core.
            $cpu = if ($existing.ContainsKey([int]$id)) { $existing[[int]$id] } else {
                $newCpu = $beforeXml.CreateElement($cpuElementName)
                $newCpu.SetAttribute($cpuAttributeName, [string]$id)
                $newCpu
            }
            [void]$cpuContainer.AppendChild($cpu)
        }
    }
    if ($patch.PSObject.Properties.Name -contains 'CoreSettings') {
        $cpuContainer = $settingsNode.SelectSingleNode("./*[local-name()='CPUs']")
        $cpuNodes = if ($cpuContainer) {
            @($cpuContainer.SelectNodes("./*[local-name()='CPU' or local-name()='Cpu']"))
        } else { @($settingsNode.SelectNodes("./*[local-name()='CPU' or local-name()='Cpu']")) }
        $byId = @{}
        foreach ($cpu in $cpuNodes) {
            $candidate = $cpu.Attributes | Where-Object {
                [string]$_.Name -ieq 'CpuId' -or [string]$_.Name -ieq 'id'
            } | Select-Object -First 1
            if ($candidate) {
                try { $byId[[int](Convert-TcSystemInteger $candidate.Value 'cpu_id' 4095)] = $cpu } catch { }
            }
        }
        foreach ($entry in @($patch.CoreSettings)) {
            $cpuId = [int]$entry.cpu_id
            if (-not $byId.ContainsKey($cpuId)) { throw "TIRS XML does not contain configured CPU $cpuId." }
            $cpu = $byId[$cpuId]
            foreach ($field in @($entry.PSObject.Properties.Name | Where-Object { $_ -ne 'cpu_id' })) {
                $attribute = $cpu.Attributes | Where-Object { [string]$_.Name -ieq $field } | Select-Object -First 1
                if ($attribute) { $attribute.Value = [string]$entry.$field; continue }
                $element = $cpu.ChildNodes | Where-Object {
                    $_.NodeType -eq [System.Xml.XmlNodeType]::Element -and [string]$_.LocalName -ieq $field
                } | Select-Object -First 1
                if ($null -eq $element) { throw "TIRS XML does not expose writable $field for CPU $cpuId." }
                $element.InnerText = [string]$entry.$field
            }
        }
    }
    $afterXml = $beforeXml.OuterXml
    [xml]$previewXml = $afterXml; $preview = Get-TcSystemSettingsSummary $previewXml $versionInfo
    $requiresTargetReboot = (($patch.PSObject.Properties.Name -contains 'RouterMemory') -and $versionInfo.family -eq '4024')
    $requiresAdsRestart = (($patch.PSObject.Properties.Name -contains 'RouterMemory') -and $versionInfo.family -eq '4026')
    if (-not $Apply) {
        return [pscustomobject]@{ status='preview'; path='TIRS'; requested=$patch; before=$before; after=$preview
            requires_activation=$true; requires_target_reboot=$requiresTargetReboot
            requires_restart_for_ads_memory=$requiresAdsRestart; applied=$false }
    }
    [void]$item.ConsumeXml($afterXml)
    $readback = Get-TcSystemSettings $Dte
    $verified = $true
    foreach ($field in @($patch.PSObject.Properties.Name)) {
        $actual = switch ($field) {
            'MaxCpus' { $readback.max_cpus }; 'CpuIds' { $readback.cpu_ids }
            'Affinity' { $readback.affinity }
            'PCoreAffinity' { $readback.p_core_affinity }; 'ECoreAffinity' { $readback.e_core_affinity }
            'RouterMemory' { $readback.router_memory_raw_kb }
            'MaxStackSize' { $readback.max_task_stack_kb }
            'CoreSettings' { $readback.cores }
        }
        $expected = $patch.$field
        if ($field -eq 'CoreSettings') {
            foreach ($entry in @($expected)) {
                $core = @($actual | Where-Object { [int]$_.id -eq [int]$entry.cpu_id }) | Select-Object -First 1
                if ($null -eq $core) { $verified = $false; continue }
                foreach ($name in @($entry.PSObject.Properties.Name | Where-Object { $_ -ne 'cpu_id' })) {
                    $readName = switch ($name) {
                        'BaseTime' { 'base_time_100ns' }; 'LoadLimit' { 'load_limit_percent' }
                        'LatencyWarning' { 'latency_warning_100ns' }; 'CpuMemorySize' { 'core_memory_bytes' }
                    }
                    if ([long]$core.$readName -ne [long]$entry.$name) { $verified = $false }
                }
            }
        }
        elseif ($field -eq 'CpuIds') { if ((@($actual) -join ',') -ne (@($expected) -join ',')) { $verified = $false } }
        elseif ($actual -ne $expected) { $verified = $false }
    }
    if (-not $verified) { throw 'TIRS settings were written but readback did not match the requested values.' }
    [pscustomobject]@{ status='written'; path='TIRS'; requested=$patch; before=$before; after=$readback
        readback_verified=$true; requires_activation=$true; requires_target_reboot=$requiresTargetReboot
        requires_restart_for_ads_memory=$requiresAdsRestart; applied=$true }
}

function Get-TcCoreInfo {
    param([Parameter(Mandatory)]$Dte)
    $settings = Get-TcSystemSettings $Dte
    [pscustomobject]@{ status='ok'; path='TIRS'; max_cpus=$settings.max_cpus; cpu_ids=$settings.cpu_ids
        p_core_affinity=$settings.p_core_affinity; e_core_affinity=$settings.e_core_affinity
        available_cpus=$settings.available_cpus; real_time_cpus=$settings.real_time_cpus
        affinity=$settings.affinity
        target_p_core_affinity=$settings.target_p_core_affinity
        target_e_core_affinity=$settings.target_e_core_affinity
        cores=$settings.cores
        twincat=$settings.twincat; router_memory_mb=$settings.router_memory_mb
        max_task_stack_kb=$settings.max_task_stack_kb
        settings_found=$settings.settings_found; tasks=$settings.tasks }
}

function Get-TcRealtimeInfo {
    param([Parameter(Mandatory)]$Dte)
    $settings = Get-TcSystemSettings $Dte
    $tasks = Get-TcTaskInfo $Dte 12
    [pscustomobject]@{
        status='ok'; path='TIRS'; twincat=$settings.twincat
        memory=[pscustomobject]@{
            router_memory_mb=$settings.router_memory_mb
            router_memory_raw_kb=$settings.router_memory_raw_kb
            max_task_stack_kb=$settings.max_task_stack_kb
            global_ads_memory_estimated_mb=$settings.global_ads_memory_estimated_mb
        }
        cores=$settings.cores; tasks=$tasks.tasks
    }
}

function Set-TcCoreAssignment {
    param([Parameter(Mandatory)]$Dte, [Parameter(Mandatory)][object[]]$CpuIds,
          [Nullable[int]]$MaxCpus = $null, [Nullable[long]]$PCoreAffinity = $null,
          [Nullable[long]]$ECoreAffinity = $null, [Nullable[long]]$Affinity = $null,
          [bool]$Apply = $false)
    $patch = [ordered]@{ cpu_ids = @($CpuIds) }
    if ($null -ne $MaxCpus) { $patch.max_cpus = $MaxCpus }
    if ($null -eq $Affinity) {
        [long]$mask = 0
        foreach ($id in @($CpuIds)) {
            if ([int]$id -gt 62) { throw 'cpu_id must be between 0 and 62 when selecting cores by Affinity.' }
            $mask = $mask -bor ([long]1 -shl [int]$id)
        }
        $patch.affinity = $mask
    } else { $patch.affinity = $Affinity }
    if ($null -ne $PCoreAffinity) { $patch.p_core_affinity = $PCoreAffinity }
    if ($null -ne $ECoreAffinity) { $patch.e_core_affinity = $ECoreAffinity }
    Set-TcSystemSettings $Dte ([pscustomobject]$patch) $Apply
}

function Get-TcTaskInfo {
    param([Parameter(Mandatory)]$Dte, [int]$MaxDepth = 6)
    $sys = Get-TcSystemManager $Dte; $root = $sys.LookupTreeItem('TIRT')
    $tree = Get-TcSystemTreeNode $root 'TIRT' 0 ([Math]::Max(0, [Math]::Min($MaxDepth, 12)))
    # Pass a mutable accumulator through the recursive scriptblock.  A plain
    # PowerShell array is fixed-size from the scriptblock child scope, so +=
    # or Add() can fail while walking a real TIRT tree.
    $tasks = New-Object System.Collections.ArrayList
    $walk = {
        param($Parent, [string]$ParentPath, $Accumulator)
        foreach ($child in $Parent) {
            $name = [string]$child.Name; if ([string]::IsNullOrWhiteSpace($name)) { continue }
            $path = [string]$child.PathName; if ([string]::IsNullOrWhiteSpace($path)) { $path = "$ParentPath^$name" }
            $parameters = [ordered]@{}
            try {
                [xml]$taskXml = [string]$child.ProduceXml($false)
                $taskNode = $taskXml.SelectSingleNode("//*[local-name()='Task' or local-name()='TaskDef']")
                if ($taskNode) {
                    foreach ($attribute in @($taskNode.Attributes)) {
                        $parameters[[string]$attribute.Name] = [string]$attribute.Value
                    }
                    foreach ($parameter in @($taskNode.ChildNodes)) {
                        if ($parameter.NodeType -eq [System.Xml.XmlNodeType]::Element -and
                            $parameter.ChildNodes.Count -eq 1 -and
                            $parameter.ChildNodes[0].NodeType -eq [System.Xml.XmlNodeType]::Text) {
                            $parameters[[string]$parameter.LocalName] = [string]$parameter.InnerText
                        }
                    }
                }
            } catch { }
            $priority = $null; $cycleTime = $null; $cpuAffinity = $null
            try { if ($parameters.Contains('Priority')) { $priority = Convert-TcSystemInteger $parameters['Priority'] 'Priority' } } catch { }
            try { if ($parameters.Contains('CycleTime')) { $cycleTime = Convert-TcSystemInteger $parameters['CycleTime'] 'CycleTime' } } catch { }
            try { if ($parameters.Contains('CpuAffinity')) { $cpuAffinity = Convert-TcSystemInteger $parameters['CpuAffinity'] 'CpuAffinity' } } catch { }
            [void]$Accumulator.Add([pscustomobject]@{
                name=$name; path=$path; item_type=[int]$child.ItemType; parameters=[pscustomobject]$parameters
                priority=$priority; cycle_time_100ns=$cycleTime
                cycle_time_us=if ($null -ne $cycleTime) { [double]$cycleTime / 10.0 } else { $null }
                auto_start=([string]$parameters['AutoStart'] -ieq 'true'); cpu_affinity=$cpuAffinity
            })
            & $walk $child $path $Accumulator
        }
    }
    & $walk $root 'TIRT' $tasks
    [pscustomobject]@{ status='ok'; path='TIRT'; tree=$tree; tasks=@($tasks.ToArray()) }
}

function Set-TcTaskSettings {
    param([Parameter(Mandatory)]$Dte, [Parameter(Mandatory)][string]$TaskPath,
          [Parameter(Mandatory)]$Settings, [bool]$Apply = $false)
    $path = Get-TcSystemPath $TaskPath $true $false
    if ($null -eq $Settings -or @($Settings.PSObject.Properties).Count -eq 0) {
        throw 'settings must be a non-empty object.'
    }
    $sys = Get-TcSystemManager $Dte; $item = $sys.LookupTreeItem($path)
    [xml]$xml = [string]$item.ProduceXml($false)
    $task = $xml.SelectSingleNode("//*[local-name()='Task' or local-name()='TaskDef']")
    if ($null -eq $task) { throw 'task XML does not contain a Task/TaskDef element.' }
    $patch = [ordered]@{}
    foreach ($property in @($Settings.PSObject.Properties)) {
        $key = ([string]$property.Name).Trim().ToLowerInvariant().Replace('-', '_')
        switch ($key) {
            'priority' { $patch['Priority'] = Convert-TcSystemInteger $property.Value $key 31 }
            'cycle_time_100ns' { $value = Convert-TcSystemInteger $property.Value $key 2147483647; if ($value -le 0) { throw 'cycle_time_100ns must be positive.' }; $patch['CycleTime'] = $value }
            'cycle_time_us' {
                try { [decimal]$ticks = [decimal]$property.Value * 10 }
                catch { throw 'cycle_time_us must be numeric.' }
                if ($ticks -le 0 -or $ticks -ne [decimal]::Truncate($ticks)) {
                    throw 'cycle_time_us must be positive with 0.1 us precision.'
                }
                $patch['CycleTime'] = [long]$ticks
            }
            'auto_start' {
                if ($property.Value -isnot [bool]) { throw 'auto_start must be boolean.' }
                $patch['AutoStart'] = [bool]$property.Value
            }
            'tick_modulo' { $patch['TickModulo'] = Convert-TcSystemInteger $property.Value $key 2147483647 }
            'input_update_pre_ticks' { $patch['InputUpdatePreTicks'] = Convert-TcSystemInteger $property.Value $key 2147483647 }
            'exceed_warning' { $patch['ExceedWarning'] = Convert-TcSystemInteger $property.Value $key 2147483647 }
            'watchdog_stack_capacity' { $patch['WatchdogStackCapacity'] = Convert-TcSystemInteger $property.Value $key 2147483647 }
            default { throw "unsupported task setting '$($property.Name)'." }
        }
    }
    if ($patch.Contains('Priority')) {
        $allTasks = Get-TcTaskInfo $Dte 12
        $conflict = @($allTasks.tasks | Where-Object {
            $_.path -ne $path -and $null -ne $_.priority -and [int]$_.priority -eq [int]$patch['Priority']
        }) | Select-Object -First 1
        if ($conflict) { throw "priority $($patch['Priority']) is already used by $($conflict.path)." }
    }
    $warnings = New-Object System.Collections.ArrayList
    if ($patch.Contains('CycleTime')) {
        $rt = Get-TcSystemSettings $Dte
        $bases = @($rt.cores | Where-Object { $_.configured -and $null -ne $_.base_time_100ns -and $_.base_time_100ns -gt 0 } |
            ForEach-Object { [long]$_.base_time_100ns })
        $bad = @($bases | Where-Object { [long]$patch['CycleTime'] -lt $_ -or ([long]$patch['CycleTime'] % $_) -ne 0 })
        $unique = @($bases | Sort-Object -Unique)
        if ($bases.Count -and $bad.Count) {
            if ($unique.Count -eq 1 -or $bad.Count -eq $bases.Count) {
                throw "cycle time $($patch['CycleTime']) x100ns is not compatible with RT core base time(s) $($unique -join ',')."
            }
            [void]$warnings.Add('RT cores use different BaseTime values; cycle is not compatible with every core. Assign the task to a compatible core before activation.')
        } elseif (-not $bases.Count) {
            [void]$warnings.Add('No configured RT-core BaseTime was available; cycle/BaseTime compatibility was not verified.')
        }
    }
    $before = [ordered]@{}
    foreach ($field in @($patch.Keys)) {
        $attribute = $task.Attributes | Where-Object { [string]$_.Name -ieq $field } | Select-Object -First 1
        $element = $task.ChildNodes | Where-Object {
            $_.NodeType -eq [System.Xml.XmlNodeType]::Element -and [string]$_.LocalName -ieq $field
        } | Select-Object -First 1
        $text = if ($patch[$field] -is [bool]) { ([string]$patch[$field]).ToLowerInvariant() } else { [string]$patch[$field] }
        if ($attribute) { $before[$field] = [string]$attribute.Value; $attribute.Value = $text }
        elseif ($element) { $before[$field] = [string]$element.InnerText; $element.InnerText = $text }
        else { throw "task XML does not expose writable $field." }
    }
    $result = [ordered]@{ status='preview'; path=$path; requested=[pscustomobject]$patch
        before=[pscustomobject]$before; warnings=@($warnings.ToArray()); requires_activation=$true; applied=$false }
    if (-not $Apply) { return [pscustomobject]$result }
    [void]$item.ConsumeXml($xml.OuterXml)
    [xml]$readbackXml = [string]$item.ProduceXml($false)
    $readbackTask = $readbackXml.SelectSingleNode("//*[local-name()='Task' or local-name()='TaskDef']")
    $readback = [ordered]@{}
    foreach ($field in @($patch.Keys)) {
        $attribute = $readbackTask.Attributes | Where-Object { [string]$_.Name -ieq $field } | Select-Object -First 1
        $element = $readbackTask.ChildNodes | Where-Object {
            $_.NodeType -eq [System.Xml.XmlNodeType]::Element -and [string]$_.LocalName -ieq $field
        } | Select-Object -First 1
        $value = if ($attribute) { [string]$attribute.Value } elseif ($element) { [string]$element.InnerText } else { $null }
        $readback[$field] = $value
        $matches = if ($patch[$field] -is [bool]) { ([string]$value -ieq [string]$patch[$field]) }
                   else { (Convert-TcSystemInteger $value $field) -eq [long]$patch[$field] }
        if (-not $matches) { throw "task setting $field was written but readback did not match." }
    }
    $result.status='written'; $result.readback=[pscustomobject]$readback
    $result.readback_verified=$true; $result.applied=$true
    return [pscustomobject]$result
}

function Get-TcTaskCoreTarget {
    param([Parameter(Mandatory)][xml]$Xml)
    foreach ($element in @($Xml.SelectNodes('//*'))) {
        foreach ($name in @('CpuAffinity','CpuId','CoreId','AssignedCore')) {
            $attribute = $element.Attributes | Where-Object { [string]$_.Name -ieq $name } | Select-Object -First 1
            if ($attribute) { return [pscustomobject]@{ kind='attribute'; element=$element; name=[string]$attribute.Name; value=[string]$attribute.Value } }
        }
        foreach ($child in @($element.ChildNodes)) {
            if ($child.NodeType -eq [System.Xml.XmlNodeType]::Element -and [string]$child.LocalName -in @('CpuAffinity','CpuId','CoreId','AssignedCore')) {
                return [pscustomobject]@{ kind='element'; element=$child; name=[string]$child.LocalName; value=[string]$child.InnerText }
            }
        }
    }
    return $null
}

function Set-TcTaskCoreAssignment {
    param([Parameter(Mandatory)]$Dte, [Parameter(Mandatory)][string]$TaskPath,
          [Parameter(Mandatory)][int]$CpuId, [bool]$Apply = $false)
    $path = Get-TcSystemPath $TaskPath $true $false
    $sys = Get-TcSystemManager $Dte; $item = $sys.LookupTreeItem($path)
    [xml]$xml = [string]$item.ProduceXml($false)
    $cpu = Convert-TcSystemInteger $CpuId 'cpu_id' 4095
    $target = Get-TcTaskCoreTarget $xml
    if ($null -eq $target) { throw 'Task XML does not expose CpuAffinity/CpuId/CoreId/AssignedCore; read tc_task_info first.' }
    $before = $target.value
    $isAffinity = [string]$target.name -ieq 'CpuAffinity'
    if ($isAffinity -and $cpu -gt 63) { throw 'cpu_id must be between 0 and 63 when the task exposes CpuAffinity.' }
    $targetValue = if ($isAffinity) { '#x{0:X}' -f ([long]1 -shl $cpu) } else { [string]$cpu }
    if ($target.kind -eq 'attribute') { $target.element.SetAttribute($target.name, $targetValue) }
    else { $target.element.InnerText = $targetValue }
    $afterXml = $xml.OuterXml
    if (-not $Apply) {
        return [pscustomobject]@{ status='preview'; path=$path; field=$target.name
            requested_cpu_id=$cpu; before_value=$before; after_value=$targetValue
            before_cpu_id=if ($isAffinity) { $null } else { $before }
            after_cpu_id=if ($isAffinity) { $null } else { $cpu }
            requires_activation=$true; applied=$false }
    }
    [void]$item.ConsumeXml($afterXml)
    [xml]$readbackXml = [string]$item.ProduceXml($false)
    $readback = Get-TcTaskCoreTarget $readbackXml
    $expectedValue = if ($isAffinity) { [long]1 -shl $cpu } else { [long]$cpu }
    if ($null -eq $readback -or (Convert-TcSystemInteger $readback.value 'readback task core' 9223372036854775807) -ne $expectedValue) { throw 'Task core was written but readback did not match.' }
    [pscustomobject]@{ status='written'; path=$path; field=$target.name
        requested_cpu_id=$cpu; before_value=$before; readback_value=$readback.value
        readback_cpu_id=if ($isAffinity) { $null } else { $cpu }
        readback_verified=$true; requires_activation=$true; applied=$true }
}

function New-TcSystemItem {
    param([Parameter(Mandatory)]$Dte, [Parameter(Mandatory)][string]$ParentPath,
          [Parameter(Mandatory)][string]$Name, [Parameter(Mandatory)][int]$ItemType,
          [string]$Info = '', [bool]$Apply = $false)
    $parentPath = Get-TcSystemPath $ParentPath $true $true
    if ([string]::IsNullOrWhiteSpace($Name) -or $Name.Contains('^') -or $Name -in @('.', '..')) { throw 'SYSTEM child name must be one non-empty path segment.' }
    if ($ItemType -lt 0 -or $ItemType -gt 65535) { throw 'item_type must be between 0 and 65535.' }
    $sys = Get-TcSystemManager $Dte; $parent = $sys.LookupTreeItem($parentPath)
    foreach ($child in @($parent)) { if ([string]$child.Name -ieq $Name) { throw "SYSTEM child already exists: $parentPath^$Name" } }
    $path = "$parentPath^$Name"
    if (-not $Apply) { return [pscustomobject]@{ status='preview'; parent=$parentPath; path=$path; name=$Name; item_type=$ItemType; applied=$false } }
    [void]$parent.CreateChild($Name, $ItemType, '', $Info)
    $created = $sys.LookupTreeItem($path)
    [pscustomobject]@{ status='created'; parent=$parentPath; path=([string]$created.PathName); name=$Name; item_type=[int]$created.ItemType; applied=$true }
}

function Remove-TcSystemItem {
    param([Parameter(Mandatory)]$Dte, [Parameter(Mandatory)][string]$Path,
          [bool]$Apply = $false, [bool]$AllowWithChildren = $false)
    $path = Get-TcSystemPath $Path $true $false
    $sys = Get-TcSystemManager $Dte; $item = $sys.LookupTreeItem($path)
    $children = @()
    foreach ($child in $item) { $children += $child }
    if ($children.Count -and -not $AllowWithChildren) { throw "SYSTEM node '$path' has $($children.Count) children; set allow_with_children=true to remove it." }
    $parts = @($path -split '\^'); $parentPath = ($parts[0..($parts.Count - 2)] -join '^'); $name = $parts[-1]
    $payload = [ordered]@{ status='preview'; path=$path; parent=$parentPath; name=$name; item_type=[int]$item.ItemType; child_count=$children.Count; applied=$false }
    if (-not $Apply) { return [pscustomobject]$payload }
    $parent = $sys.LookupTreeItem($parentPath); [void]$parent.DeleteChild($name)
    $stillExists = $false
    try { $sys.LookupTreeItem($path) | Out-Null; $stillExists = $true } catch { }
    if ($stillExists) { throw "SYSTEM node still exists after deletion: $path" }
    try {
        $Dte.ExecuteCommand('File.SaveAll')
    } catch {
        throw "SYSTEM node '$path' was removed from the live XAE tree, but File.SaveAll failed: $([string]$_.Exception.Message)"
    }
    $payload.status = 'deleted'; $payload.applied = $true
    $payload.saved = $true
    [pscustomobject]$payload
}

function Get-TcIoRuntimeMode {
    param([Parameter(Mandatory)]$Sys)
    $started = $false
    try { $started = [bool]$Sys.IsTwinCATStarted() } catch { }
    if (-not $started) { return 'NotStarted' }
    try {
        $tirs = $Sys.LookupTreeItem('TIRS')
        [xml]$xml = $tirs.ProduceXml($false)
        $parts = @()
        foreach ($path in @('//TwinCATState', '//RunMode', '//CurrentState', '//State')) {
            $node = $xml.SelectSingleNode($path)
            if ($node) { $parts += [string]$node.InnerText }
        }
        $probe = ($parts -join ' ').ToLowerInvariant()
        if ($probe -match 'config') { return 'Config' }
        if ($probe -match 'run') { return 'Run' }
    } catch { }
    return 'Unknown'
}

function Invoke-TcIoScan {
    param(
        [Parameter(Mandatory)]$Dte,
        [bool]$ConfigConfirmed = $false,
        [bool]$AllowUnknown = $false
    )
    $sys = Get-TcSystemManager $Dte
    $mode = Get-TcIoRuntimeMode $sys
    if ($ConfigConfirmed) { $mode = 'Config (ADS)' }
    if ($mode -notlike 'Config*' -and -not ($mode -eq 'Unknown' -and $AllowUnknown)) {
        throw ("Device scan requires TwinCAT Config mode; current mode is '$mode'. " +
               "Switch to Config mode separately, then approve the scan again.")
    }

    $io = $sys.LookupTreeItem('TIID')
    [xml]$scanXml = $io.ProduceXml($false)
    $foundNodes = @($scanXml.SelectNodes('//FoundDevices/Device'))
    $metaNames = @(
        'Image', 'Image-Info', 'Process Image', 'Process Image-Info',
        'Inputs', 'Outputs', 'InfoData', 'SyncUnits', '<default>'
    )
    $existingNames = @{}
    foreach ($child in $io) { $existingNames[[string]$child.Name] = $true }

    $found = @()
    $added = @()
    $skipped = @()
    $removedEmpty = @()
    $failed = @()

    for ($index = 0; $index -lt $foundNodes.Count; $index++) {
        $device = $foundNodes[$index]
        $subtype = 0
        [void][int]::TryParse([string]$device.ItemSubType, [ref]$subtype)
        $subtypeName = [string]$device.ItemSubTypeName
        $name = ''
        $pnp = $device.SelectSingleNode('.//Pnp/DeviceDesc')
        if ($pnp -and [string]$pnp.InnerText) { $name = [string]$pnp.InnerText }
        if ([string]::IsNullOrWhiteSpace($name)) { $name = $subtypeName }
        if ([string]::IsNullOrWhiteSpace($name)) { $name = "Device $($index + 1)" }
        $name = $name.Trim().TrimEnd('#').Trim()

        $found += [pscustomobject]@{
            name = $name
            subtype = $subtype
            subtype_name = $subtypeName
        }
        if ($existingNames.ContainsKey($name)) {
            $skipped += [pscustomobject]@{ name = $name; reason = 'already configured' }
            continue
        }
        if ($subtype -le 0) {
            $failed += [pscustomobject]@{ name = $name; error = 'invalid ItemSubType' }
            continue
        }

        $created = $null
        try {
            $created = $io.CreateChild($name, $subtype, '', $null)
            $existingNames[$name] = $true
            $address = $device.SelectSingleNode('AddressInfo')
            if ($address) {
                [void]$created.ConsumeXml(
                    "<TreeItem><DeviceDef>$($address.OuterXml)</DeviceDef></TreeItem>")
            }
            [void]$created.ConsumeXml(
                '<TreeItem><DeviceDef><ScanBoxes>1</ScanBoxes></DeviceDef></TreeItem>')

            $boxCount = 0
            foreach ($child in $created) {
                if ([string]$child.Name -notin $metaNames) { $boxCount++ }
            }
            if ($boxCount -eq 0) {
                # 只清理由本轮刚创建且确认无从站的适配器，绝不碰既有设备。
                try {
                    [void]$io.DeleteChild($name)
                    $removedEmpty += $name
                    [void]$existingNames.Remove($name)
                } catch {
                    $failed += [pscustomobject]@{
                        name = $name
                        error = "No slaves found and cleanup failed: $($_.Exception.Message)"
                    }
                }
                continue
            }
            $added += [pscustomobject]@{ name = $name; slave_count = $boxCount }
        } catch {
            if ($null -ne $created) {
                try {
                    [void]$io.DeleteChild($name)
                    [void]$existingNames.Remove($name)
                } catch { }
            }
            $failed += [pscustomobject]@{ name = $name; error = [string]$_.Exception.Message }
        }
    }

    try { $Dte.ExecuteCommand('File.SaveAll') } catch { }
    [pscustomobject]@{
        target = [string]$sys.GetTargetNetId()
        mode = $mode
        found = @($found)
        added = @($added)
        skipped = @($skipped)
        removed_empty = @($removedEmpty)
        failed = @($failed)
        activated = $false
        restarted = $false
        mode_changed = $false
    }
}

# ---- 内部: 按 ItemType 分类 PLC 定义，不依赖 POUs/DUTs/GVLs 文件夹名称 ----
function Get-PlcObjectCategory {
    param([int]$ItemType)
    switch ($ItemType) {
        { $_ -in @(602,603,604) } { return 'POUs' }
        { $_ -in @(605,606,607,623) } { return 'DUTs' }
        615 { return 'GVLs' }
        618 { return 'Interfaces' }
        619 { return 'VISUs' }
        default { return '' }
    }
}

function Get-PlcObjectFrames {
    param([Parameter(Mandatory)]$Dte,
          [string[]]$Folders = @('POUs','DUTs','GVLs','Interfaces','VISUs'))
    $sys = Get-TcSystemManager $Dte
    $wanted = [Collections.Generic.HashSet[string]]::new(
        [StringComparer]::OrdinalIgnoreCase)
    foreach ($folderName in $Folders) { [void]$wanted.Add([string]$folderName) }
    $objects = [Collections.Generic.List[object]]::new()
    $plc = $sys.LookupTreeItem('TIPC')
    foreach ($controller in $plc) {
        $root = $null; try { $root = $controller.NestedProject } catch { }
        if ($null -eq $root) { continue }
        $base = 'TIPC^{0}^{1}' -f [string]$controller.Name, [string]$root.Name
        $stack = [Collections.Stack]::new()
        $stack.Push([pscustomobject]@{ node=$root; path=$base; depth=0 })
        while ($stack.Count -gt 0) {
            $frame = $stack.Pop()
            foreach ($item in $frame.node) {
            $name = [string]$item.Name; if (-not $name) { continue }
            $itemType = 0; try { $itemType = [int]$item.ItemType } catch { }
            $itemPath = "$($frame.path)^$name"
            if ($itemType -eq 601) {
                $nextDepth = if ($name -in @('POUs','DUTs','GVLs','Interfaces','VISUs')) {
                    [int]$frame.depth
                } else { [int]$frame.depth + 1 }
                $stack.Push([pscustomobject]@{
                    node=$item; path=$itemPath; depth=$nextDepth })
                continue
            }
            $category = Get-PlcObjectCategory $itemType
            if (-not $category -or -not $wanted.Contains($category)) { continue }
                [void]$objects.Add([pscustomobject]@{
                    node=$item; name=$name; folder=$category; itemType=$itemType
                    path=$itemPath; parent=[string]$frame.path; depth=[int]$frame.depth })
            }
        }
    }
    @($objects.ToArray())
}

# ---- 内部: 单遍新鲜遍历定位 POU, 并在同一作用域内执行 $Action(委托) ----
#  $Action 接收 COM 项, 返回纯数据; COM 项不离开本函数, 故不被管道展开。
function Invoke-OnPou {
    param([Parameter(Mandatory)]$Dte, [Parameter(Mandatory)][string]$Name,
          [Parameter(Mandatory)][scriptblock]$Action,
          [string[]]$Folders = @('POUs','DUTs','GVLs','Interfaces'),
          [string]$TreePath = '')
    $sys = Get-TcSystemManager $Dte
    $bases = @()
    $unavailableProjects = @()
    foreach ($controller in $sys.LookupTreeItem('TIPC')) {
        $nested = $null; try { $nested = $controller.NestedProject } catch { }
        if ($null -ne $nested) {
            $bases += ('TIPC^{0}^{1}' -f [string]$controller.Name, [string]$nested.Name)
        } else {
            $unavailableProjects += [string]$controller.Name
        }
    }

    if (-not $bases.Count) {
        throw '[plc_project_unavailable] No accessible PLC NestedProject. Check XAE project loading/disabled state; this does not prove Disabled. No reload performed.'
    }
    foreach ($unavailableName in $unavailableProjects) {
        if ($TreePath.StartsWith("TIPC^$unavailableName^", [StringComparison]::OrdinalIgnoreCase)) {
            throw "[plc_project_unavailable] PLC '$unavailableName' NestedProject is unavailable. Check XAE loading state; no reload performed."
        }
    }

    # Fastest path: plc_find returns the exact tree path.  Validate that the
    # caller cannot escape the current PLC project before resolving it.
    if ($TreePath) {
        $insideKnownProject = $false
        foreach ($base in $bases) {
            if ($TreePath.StartsWith("$base^", [StringComparison]::OrdinalIgnoreCase)) {
                $insideKnownProject = $true; break
            }
        }
        if (-not $insideKnownProject) {
            throw "Tree path is outside the current PLC project: '$TreePath'"
        }
        try {
            $exact = $sys.LookupTreeItem($TreePath)
            if ($null -ne $exact -and [string]$exact.Name -eq $Name) {
                return (& $Action $exact)
            }
        } catch {
            throw "Object '$Name' was not found at tree path '$TreePath'."
        }
        throw "Tree path '$TreePath' does not resolve to object '$Name'."
    }

    # Fast path: LookupTreeItem resolves an exact object path directly instead
    # of enumerating every POU in a large project for each read/write call.
    # A name-only lookup may span multiple PLC projects.  Do not return the
    # first direct hit: the generic search below detects ambiguity and asks the
    # caller to reuse plc_find's exact path.

    # Generic fallback: scan all custom/localized folders below NestedProject
    # and classify definitions by ItemType.
    $matches = @(@(Get-PlcObjectFrames $Dte -Folders $Folders) | Where-Object {
        [string]$_.name -eq $Name
    })
    if ($matches.Count -eq 1) { return (& $Action $matches[0].node) }
    if ($matches.Count -gt 1) {
        $paths = (($matches | ForEach-Object { [string]$_.path }) -join ', ')
        throw "Object '$Name' exists in multiple PLC projects; use plc_find and pass its exact path. Matches: $paths"
    }
    throw "Object '$Name' not found in any PLC folder."
}

# ---- 列举所有 PLC 对象 (name/folder/itemType, 纯数据) ----
function Get-PouList {
    param([Parameter(Mandatory)]$Dte,
          [string[]]$Folders = @('POUs','DUTs','GVLs','Interfaces'))
    $out = @(Get-PlcObjectFrames $Dte -Folders $Folders | ForEach-Object {
        [pscustomobject]@{
            name = [string]$_.name; folder = [string]$_.folder
            itemType = [int]$_.itemType; path = [string]$_.path
            parent = [string]$_.parent; depth = [int]$_.depth }
    })
    ,$out   # 前置逗号: 强制单元素也保持数组
}

# ---- 创建/重命名后的权威对象身份确认 ----
# CreateChild 的返回值在部分 XAE Build 中并不可靠；重新遍历当前树并返回
# 精确 path/itemType，后续写入必须使用该 path，不能退回到同名对象的模糊查找。
function Get-PlcObjectIdentity {
    param([Parameter(Mandatory)]$Dte, [Parameter(Mandatory)][string]$Name,
          [string]$TreePath = '')
    $matches = @(@(Get-PlcObjectFrames $Dte) | Where-Object {
        [string]$_.name -ieq $Name -and
        (-not $TreePath -or [string]$_.path -ieq $TreePath)
    })
    if ($matches.Count -ne 1) {
        $paths = ($matches | ForEach-Object { [string]$_.path }) -join ', '
        if ($matches.Count -eq 0) { throw "Created object '$Name' was not found after tree refresh." }
        throw "Created object '$Name' is ambiguous after tree refresh: $paths"
    }
    $match = $matches[0]
    [pscustomobject]@{ name=[string]$match.name; path=[string]$match.path
        parent=[string]$match.parent; itemType=[int]$match.itemType; depth=[int]$match.depth }
}

# ---- 代码分页：大型 POU 只返回指定行段，避免 COM/JSON/模型上下文过大 ----
function Get-CodeSlice {
    param([string]$Text = '', [int]$StartLine = 1, [int]$MaxLines = 0)
    if ($StartLine -lt 1) { $StartLine = 1 }
    if ([string]::IsNullOrEmpty($Text)) {
        return [pscustomobject]@{ text = ''; total_lines = 0; start_line = $StartLine
                                 end_line = 0; has_more = $false }
    }
    $lines = @([string]$Text -split "`r?`n")
    $total = $lines.Count
    $startIndex = $StartLine - 1
    if ($startIndex -ge $total) {
        return [pscustomobject]@{ text = ''; total_lines = $total; start_line = $StartLine
                                 end_line = $total; has_more = $false }
    }
    $endIndex = $total - 1
    if ($MaxLines -gt 0) { $endIndex = [Math]::Min($endIndex, $startIndex + $MaxLines - 1) }
    $slice = if ($startIndex -eq $endIndex) { [string]$lines[$startIndex] } else {
        [string]($lines[$startIndex..$endIndex] -join "`r`n") }
    [pscustomobject]@{ text = $slice; total_lines = $total; start_line = $StartLine
                       end_line = $endIndex + 1; has_more = ($endIndex -lt $total - 1) }
}

# ---- 读代码: 默认只读 POU 本体;成员正文按需读取，避免大型 FB 输出爆炸 ----
function Read-Pou {
    param([Parameter(Mandatory)]$Dte, [Parameter(Mandatory)][string]$Name,
          [ValidateSet('all','declaration','implementation','members')]
          [string]$Area = 'all', [string]$Method = '',
          [ValidateSet('','method','action','transition','property','propget','propset')]
          [string]$MemberType = '',
          [bool]$IncludeMemberCode = $false,
          [int]$StartLine = 1, [int]$MaxLines = 0,
          [string]$TreePath = '')
    Invoke-OnPou $Dte $Name {
        param($o)
        $target = $o
        $displayName = [string]$o.Name
        $memberTypeResolved = $null
        if ($Method) {
            foreach ($seg in ($Method -split '\.')) {
                $next = $null
                foreach ($child in $target) {
                    if ([string]$child.Name -eq $seg) { $next = $child; break }
                }
                if ($null -eq $next) {
                    $available = @($target | ForEach-Object {
                        $type = 0; try { $type = [int]$_.ItemType } catch { }
                        if ([string]$_.Name) { "{0} (itemType {1})" -f [string]$_.Name, $type }
                    })
                    $hint = if ($available.Count) { "; available members: " + ($available -join ', ') } else { '' }
                    throw "Member '$seg' not found under '$Name'$hint"
                }
                $target = $next
            }
            if ($MemberType) {
                $typeMap = @{
                    method=@(609,610); action=@(608); transition=@(616)
                    property=@(611,612); propget=@(613,654); propset=@(614,655)
                }
                $targetType = 0; try { $targetType = [int]$target.ItemType } catch { }
                if ($targetType -notin $typeMap[$MemberType]) {
                    # A read has already resolved an exact member name.  Treat
                    # the caller's type as a hint so a model/UI mistaking an
                    # Action for a Method does not waste another COM round-trip.
                    $actualMap = @{ 608='action'; 609='method'; 610='method'; 611='property'; 612='property'
                                    613='propget'; 614='propset'; 616='transition'; 654='propget'; 655='propset' }
                    $actual = if ($actualMap.ContainsKey($targetType)) { $actualMap[$targetType] } else { "itemType $targetType" }
                    $memberTypeResolved = [pscustomobject]@{
                        requested=[string]$MemberType; actual=[string]$actual
                        message='Resolved from the actual XAE member type.' }
                }
            }
            $displayName = "$Name.$Method"
        }

        $decl = ''; $impl = ''; $ranges = @{}
        if ($Area -in @('all','declaration')) {
            $raw = ''; try { $raw = [string]$target.DeclarationText } catch { }
            $slice = Get-CodeSlice $raw $StartLine $MaxLines
            $decl = $slice.text
            $ranges['declaration'] = [pscustomobject]@{
                total_lines=$slice.total_lines; start_line=$slice.start_line
                end_line=$slice.end_line; has_more=$slice.has_more }
        }
        if ($Area -in @('all','implementation')) {
            $raw = ''; try { $raw = [string]$target.ImplementationText } catch { }
            $slice = Get-CodeSlice $raw $StartLine $MaxLines
            $impl = $slice.text
            $ranges['implementation'] = [pscustomobject]@{
                total_lines=$slice.total_lines; start_line=$slice.start_line
                end_line=$slice.end_line; has_more=$slice.has_more }
        }
        # 成员(方法/属性/动作/转换/访问器)。不按"有无代码"过滤 —— 新建的空成员
        # 也要列出, 否则 action/transition 刚建完看不见。
        $kindMap = @{ 608='action'; 609='method'; 610='method(itf)'; 611='property'
                      612='property(itf)'; 613='propget'; 614='propset'; 616='transition'
                      654='propget(itf)'; 655='propset(itf)' }
        $methods = @()
        # Interface property accessors are represented as children of the
        # property.  Some XAE builds expose them with ItemType 0 and no safe
        # text properties, so return their names as a nested tree without
        # probing DeclarationText/ImplementationText on those nodes.
        function Get-MemberPayload {
            param($Member, [bool]$WithCode = $false)
            $memberType = 0; try { $memberType = [int]$Member.ItemType } catch { }
            $payload = [ordered]@{ name=[string]$Member.Name; itemType=$memberType }
            if ($WithCode) {
                $memberDecl = ''; $memberImpl = ''
                try { $memberDecl = [string]$Member.DeclarationText } catch { }
                try { $memberImpl = [string]$Member.ImplementationText } catch { }
                $payload['declaration'] = $memberDecl
                $payload['implementation'] = $memberImpl
            }
            $nested = @()
            foreach ($nestedChild in $Member) {
                $nestedName = [string]$nestedChild.Name; if (-not $nestedName) { continue }
                $nestedType = 0; try { $nestedType = [int]$nestedChild.ItemType } catch { }
                if ($nestedType -in @(608,609,610,611,612,613,614,616,654,655) -or
                    ($memberType -eq 612 -and $nestedName -in @('Get','Set'))) {
                    $nested += (Get-MemberPayload $nestedChild $false)
                }
            }
            if ($nested.Count -gt 0) { $payload['members'] = @($nested) }
            [pscustomobject]$payload
        }
        if (-not $Method) { foreach ($child in $o) {
            $cn = [string]$child.Name; if (-not $cn) { continue }
            try { $it = [int]$child.ItemType } catch { }
            $kind = $kindMap[$it]; if (-not $kind) { $kind = "type$it" }
            $member = Get-MemberPayload $child $IncludeMemberCode
            $member | Add-Member -NotePropertyName kind -NotePropertyValue $kind
            $methods += $member
        } }
        $targetType = 0; try { $targetType = [int]$target.ItemType } catch { }
        $actualMemberType = ''
        if ($Method) { $actualMemberType = [string]$kindMap[$targetType] }
        [pscustomobject]@{ name = $displayName; itemType = $targetType; area = $Area
                            declaration = $decl; implementation = $impl
                            methods = @($methods); ranges = $ranges
                            memberCodeIncluded = $IncludeMemberCode
                            member_type = $actualMemberType
                            member_type_resolved = $memberTypeResolved }
    } -TreePath $TreePath
}

# ---- 写代码: IDE 内部操作, 自动重解析, 无"外部修改"弹窗 ----
#  Area: declaration | implementation;  -Method 写 POU 下方法/属性/动作的对应区
function Write-Pou {
    param([Parameter(Mandatory)]$Dte, [Parameter(Mandatory)][string]$Name,
          [ValidateSet('declaration','implementation')][string]$Area,
          [Parameter(Mandatory)][string]$Code, [string]$Method = '',
          [string]$TreePath = '')
    Invoke-OnPou $Dte $Name {
        param($o)
        $target = $o
        if ($Method) {
            # 支持点号路径: 'Value.Get' → 属性 Value 下的 Get 访问器(嵌套两层)
            foreach ($seg in ($Method -split '\.')) {
                $next = $null
                foreach ($child in $target) { if ([string]$child.Name -eq $seg) { $next = $child; break } }
                if ($null -eq $next) { throw "Member '$seg' (of '$Method') not found under '$Name'" }
                $target = $next
            }
        }
        # Interface members receive their signature through CreateChild vInfo.
        # TcXaeShell 15 can crash when a child interface item's text properties
        # are set through IDispatch; keep methods, properties and accessors
        # read-only rather than risking the IDE process.
        $targetType = 0; try { $targetType = [int]$target.ItemType } catch { }
        if ($targetType -in @(610,612,654,655)) {
            throw 'Interface member text is read-only on this XAE COM bridge; specify return_type when creating the member.'
        }
        if ($targetType -eq 618 -and $Area -eq 'declaration' -and
            $Code -match '(?im)^\s*(METHOD|PROPERTY|END_INTERFACE)\b') {
            throw "Interface '$Name' declaration may only contain its INTERFACE header; create methods as child objects."
        }
        if ($Area -eq 'declaration') { $target.DeclarationText = $Code }
        else { $target.ImplementationText = $Code }
        if ($Method) { "[OK] $Name.$Method $Area written" } else { "[OK] $Name $Area written" }
    }.GetNewClosure() -TreePath $TreePath
}

# ---- 局部替换代码：大型 POU 无需把完整正文经模型往返后再覆盖 ----
function Patch-Pou {
    param([Parameter(Mandatory)]$Dte, [Parameter(Mandatory)][string]$Name,
          [ValidateSet('declaration','implementation')][string]$Area,
          [Parameter(Mandatory)][string]$OldText,
          [Parameter(Mandatory)][AllowEmptyString()][string]$NewText,
          [string]$Method = '', [string]$TreePath = '')
    if ([string]::IsNullOrEmpty($OldText)) { throw 'old_text must not be empty.' }
    Invoke-OnPou $Dte $Name {
        param($o)
        $target = $o
        if ($Method) {
            foreach ($seg in ($Method -split '\.')) {
                $next = $null
                foreach ($child in $target) {
                    if ([string]$child.Name -eq $seg) { $next = $child; break }
                }
                if ($null -eq $next) { throw "Member '$seg' (of '$Method') not found under '$Name'" }
                $target = $next
            }
        }
        $current = if ($Area -eq 'declaration') { [string]$target.DeclarationText } else {
            [string]$target.ImplementationText }
        $first = $current.IndexOf($OldText, [StringComparison]::Ordinal)
        if ($first -lt 0) { throw "old_text was not found in '$Name$(if($Method){'.'+$Method})' $Area" }
        $second = $current.IndexOf($OldText, $first + $OldText.Length, [StringComparison]::Ordinal)
        if ($second -ge 0) { throw 'old_text occurs more than once; provide a larger unique block.' }
        $updated = $current.Substring(0, $first) + $NewText +
            $current.Substring($first + $OldText.Length)
        if ($Area -eq 'declaration') { $target.DeclarationText = $updated }
        else { $target.ImplementationText = $updated }
        [pscustomobject]@{
            name = $Name; method = $Method; area = $Area; status = 'patched'
            chars_before = $current.Length; chars_after = $updated.Length
        }
    }.GetNewClosure() -TreePath $TreePath
}

# ---- 新建 POU/DUT/GVL (CreateChild) ----
#  subType 权威表见 InfoSys "ITcSmTreeItem Item Types" (doc 242781195):
#    601 folder | 602 POU Program | 603 POU Function | 604 POU FB
#    605 DUT enum | 606 DUT struct | 607 DUT union | 608 action | 609 method
#    611 property | 613/614 property get/set | 615 GVL | 618 interface | 619 visu
# DUT 的初始声明决定 XAE 实际创建的 ItemType。仅传 STRUCT/枚举正文会被
# XAE 15 静默创建成 Alias(623)，因此备用 PowerShell 桥也必须先补齐 TYPE 包装。
function Normalize-DutDeclaration {
    param([Parameter(Mandatory)][string]$Type,
          [Parameter(Mandatory)][string]$Name,
          [Parameter(Mandatory)][string]$Declaration)
    if ($Type -notin @('struct','enum','union','alias')) { return $Declaration }
    $source = $Declaration.Trim()
    if (-not $source) { throw "$Type DUT '$Name' requires a declaration" }
    $prefix = ''
    $typeStart = [regex]::Match(
        $source, '^\s*TYPE\b',
        [System.Text.RegularExpressions.RegexOptions]::IgnoreCase -bor
        [System.Text.RegularExpressions.RegexOptions]::Multiline)
    $typeSource = if ($typeStart.Success) { $source.Substring($typeStart.Index) } else { $source }
    $match = [regex]::Match(
        $typeSource,
        '^\s*TYPE\s+([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*?)\s*END_TYPE\s*;?\s*$',
        [System.Text.RegularExpressions.RegexOptions]::IgnoreCase -bor
        [System.Text.RegularExpressions.RegexOptions]::Singleline)
    if ($match.Success) {
        if ($match.Groups[1].Value -ine $Name) {
            throw "DUT declaration name '$($match.Groups[1].Value)' does not match object name '$Name'"
        }
        if ($typeStart.Success) { $prefix = $source.Substring(0, $typeStart.Index).Trim() }
        $source = $match.Groups[2].Value.Trim()
    } elseif ($typeStart.Success) {
        throw "DUT '$Name' declaration must be a complete TYPE ... END_TYPE block"
    }
    if ($Type -eq 'struct' -and
        ($source -notmatch '^\s*STRUCT\b' -or $source -notmatch '\bEND_STRUCT\s*;?\s*$')) {
        throw "Struct DUT '$Name' must contain STRUCT ... END_STRUCT"
    }
    if ($Type -eq 'union' -and
        ($source -notmatch '^\s*UNION\b' -or $source -notmatch '\bEND_UNION\s*;?\s*$')) {
        throw "Union DUT '$Name' must contain UNION ... END_UNION"
    }
    if ($Type -eq 'enum') {
        $enumTail = [regex]::Match($source, '\)\s*([A-Za-z_][A-Za-z0-9_]*)?\s*;?\s*$')
        if ($source -notmatch '^\s*\(' -or -not $enumTail.Success) {
            throw "Enum DUT '$Name' must contain an '(...)' member list ending with );. Place {attribute 'strict'} before TYPE; never append <strict> after the list."
        }
        $baseType = $enumTail.Groups[1].Value
        $source = $source.Substring(0, $enumTail.Index) + ')' + $(if ($baseType) { " $baseType" } else { '' }) + ';'
    }
    if ($Type -eq 'alias') { $source = $source.TrimEnd().TrimEnd(';').TrimEnd() + ';' }
    $canonical = "TYPE $Name :`n$source`nEND_TYPE"
    if ($prefix) { "$prefix`n$canonical" } else { $canonical }
}

#  (曾把 program 误写成 603 → 报 'TREEITEMTYPE_PLCPOUFUNC 不支持 String vInfo')
#  vInfo: Program/FB = IEC 语言; Function = [IEC 语言, 返回类型] 数组;
#         DUT = ''; GVL = $null.
function New-Pou {
    param([Parameter(Mandatory)]$Dte, [Parameter(Mandatory)][string]$Name,
          [ValidateSet('fb','program','function','struct','enum','union','alias','gvl',
                       'interface','visu')][string]$Type = 'fb',
          [string]$Declaration = '', [string]$Implementation = '', [string]$Language = 'ST',
          [string]$ReturnType = '', [string]$ParentPath = '')
    if ($Type -in @('struct','enum','union','alias')) {
        $Declaration = Normalize-DutDeclaration $Type $Name $Declaration
    }
    if ($Type -eq 'interface' -and
        $Declaration -match '(?im)^\s*(METHOD|PROPERTY|END_INTERFACE)\b') {
        throw "Interface '$Name' declaration may only contain its INTERFACE header; create methods as child objects."
    }
    # Interface 的 vInfo 是扩展接口类型；无扩展时必须传空字符串。
    # PowerShell 的 $null 经 COM 会成为 DBNull，TwinCAT 会拒绝创建 PLCITF。
    $map = @{ fb=@(604,'POUs','ST'); program=@(602,'POUs','ST'); function=@(603,'POUs','ST')
              struct=@(606,'DUTs',''); enum=@(605,'DUTs',''); union=@(607,'DUTs','')
              alias=@(623,'DUTs','')
              gvl=@(615,'GVLs',$null)
              interface=@(618,'Interfaces',''); visu=@(619,'VISUs',$null) }
    $spec = $map[$Type]; $subType = $spec[0]; $folderName = $spec[1]; $vInfo = $spec[2]
    if ($Type -in @('fb','program')) { $vInfo = $Language }
    elseif ($Type -eq 'function') {
        # Function (603) requires a two-element SAFEARRAY: [language,
        # return type].  A scalar return type is a BSTR VARIANT and XAE
        # rejects it with "vInfo (Type: String) is not supported".
        $vInfo = [string[]]@($Language, $(if ($ReturnType) { $ReturnType } else { 'BOOL' }))
    }
    elseif ($Type -in @('struct','enum','union','alias','gvl') -and $Declaration) {
        # DUT/GVL subtype is chosen from the initial declaration.  Creating
        # an empty DUT and assigning DeclarationText afterwards makes XAE 15
        # materialize an Alias (623) regardless of the requested subtype.
        $vInfo = $Declaration
    }

    $sys = Get-TcSystemManager $Dte
    $plc = $sys.LookupTreeItem('TIPC')
    $created = $false
    foreach ($c in $plc) {
        $nested = $null; try { $nested = $c.NestedProject } catch { }
        if ($null -eq $nested) { continue }
        $base = 'TIPC^{0}^{1}' -f [string]$c.Name, [string]$nested.Name
        # Resolve an explicitly selected nested folder when supplied.  The
        # old path always wrote to the canonical POUs/DUTs/GVLs folder and
        # silently ignored the user's folder selection.
        $folder = $null
        $targetPath = ''
        if ($ParentPath) {
            if (-not ($ParentPath.Equals($base, [StringComparison]::OrdinalIgnoreCase) -or
                      $ParentPath.StartsWith("$base^", [StringComparison]::OrdinalIgnoreCase))) {
                throw "Parent tree path is outside the current PLC project: $ParentPath"
            }
            try { $folder = $sys.LookupTreeItem($ParentPath) } catch { }
            $targetPath = $ParentPath
        } else {
            try { $folder = $sys.LookupTreeItem("$base^$folderName") } catch { }
            if ($null -eq $folder) {
                try {
                    $nested.CreateChild($folderName, 601, '', $null) | Out-Null
                    Start-Sleep -Milliseconds 250
                    $folder = $sys.LookupTreeItem("$base^$folderName")
                } catch {
                    continue
                }
            }
            $targetPath = "$base^$folderName"
        }
        $folderType = 0; try { $folderType = [int]$folder.ItemType } catch { }
        if ($null -ne $folder -and $folderType -ne 601) {
            throw "Parent tree path is not a PLC folder: $targetPath"
        }
        if ($null -eq $folder) { continue }
        $folder.CreateChild($Name, $subType, '', $vInfo) | Out-Null
        Start-Sleep -Milliseconds 500
        $createdItem = $null
        try { $createdItem = $sys.LookupTreeItem("$targetPath^$Name") } catch { }
        if ($Type -in @('struct','enum','union','alias')) {
            $actualType = 0; try { $actualType = [int]$createdItem.ItemType } catch { }
            if ($actualType -ne $subType) {
                $rolledBack = $false
                try { $folder.DeleteChild($Name); $rolledBack = $true } catch { }
                $note = if ($rolledBack) { 'invalid object was rolled back' } else { 'manual cleanup required' }
                throw "TwinCAT created DUT '$Name' as itemType $actualType, expected $subType; $note."
            }
        }
        # Never trust CreateChild's COM return as identity.  The refresh also
        # catches creations that landed in an unexpected nested folder.
        $created = Get-PlcObjectIdentity $Dte $Name "$targetPath^$Name"
        break
    }
    if (-not $created) { throw "Folder '$folderName' not found; cannot create '$Name'." }

    Start-Sleep -Milliseconds 500
    # DUT/GVL declaration was already consumed at CreateChild time so that
    # its requested item subtype is retained.  Do not make a redundant write.
    if ($Declaration -and $Type -notin @('struct','enum','union','alias','gvl')) {
        Write-Pou $Dte $Name declaration $Declaration -TreePath $created.path | Out-Null
    }
    if ($Implementation) { Write-Pou $Dte $Name implementation $Implementation -TreePath $created.path | Out-Null }
    [pscustomobject]@{ status='created'; type=$Type; name=$Name; path=[string]$created.path
        parent=[string]$created.parent; itemType=[int]$created.itemType; verified=$true }
}

# ---- 新建 PLC 文件夹：父路径必须是当前 PLC 树中的精确节点 ----
function New-PlcFolder {
    param([Parameter(Mandatory)]$Dte, [Parameter(Mandatory)][string]$Name,
          [string]$ParentPath = '')
    if ([string]::IsNullOrWhiteSpace($Name) -or $Name.Contains('^')) {
        throw 'Folder name must be a non-empty single tree segment.'
    }
    $sys = Get-TcSystemManager $Dte
    $bases = @()
    foreach ($controller in $sys.LookupTreeItem('TIPC')) {
        $nested = $null; try { $nested = $controller.NestedProject } catch { }
        if ($null -ne $nested) {
            $bases += ('TIPC^{0}^{1}' -f [string]$controller.Name, [string]$nested.Name)
        }
    }
    if (-not $ParentPath) {
        if ($bases.Count -eq 0) { throw 'No PLC project found under TIPC.' }
        $ParentPath = "$($bases[0])^POUs"
    }
    $valid = $false
    foreach ($base in $bases) {
        if ($ParentPath.Equals($base, [StringComparison]::OrdinalIgnoreCase) -or
            $ParentPath.StartsWith("$base^", [StringComparison]::OrdinalIgnoreCase)) {
            $valid = $true; break
        }
    }
    if (-not $valid) { throw "Parent tree path is outside the current PLC project: $ParentPath" }
    $parent = $null; try { $parent = $sys.LookupTreeItem($ParentPath) } catch { }
    if ($null -eq $parent) { throw "Parent tree path was not found: $ParentPath" }
    foreach ($child in $parent) {
        if ([string]$child.Name -ieq $Name) { throw "'$Name' already exists under '$ParentPath'" }
    }
    $parent.CreateChild($Name, 601, '', $null) | Out-Null
    Start-Sleep -Milliseconds 250
    [pscustomobject]@{ name=$Name; parent_path=$ParentPath
        path="$ParentPath^$Name"; itemType=601; status='created' }
}

# ---- 新建 POU/接口 成员 (CreateChild 在 POU 节点上, 非文件夹) ----
#  与 New-Pou 的区别: 父节点是 POU/接口自身。subType 权威表 doc 242781195:
#    POU 下:  action=608  method=609  property=611  propget=613  propset=614  transition=616
#    接口下:  method=610  property=612  propget=654  propset=655   (接口用独立一套!)
#  父节点是不是接口在运行时按 ItemType(618) 判定, 调用方无需关心。
#  vInfo: POU method/property = [语言, 返回类型]; action/transition = 语言;
#         Interface method/property = 返回类型; getter/setter = 无参数。
#  注: 建 property 时 TwinCAT 通常自动带 Get/Set; 单独补建才需要 propget/propset。
function New-PouMember {
    param([Parameter(Mandatory)]$Dte, [Parameter(Mandatory)][string]$Pou,
          [Parameter(Mandatory)][string]$Name,
          [ValidateSet('method','property','action','transition','propget','propset')]
          [string]$Type = 'method',
          [string]$ReturnType = 'BOOL', [string]$Language = 'ST',
          [string]$Declaration = '', [string]$Implementation = '',
          [string]$TreePath = '')

    $pouMap = @{ action=608; method=609; property=611; propget=613; propset=614; transition=616 }
    $itfMap = @{ method=610; property=612; propget=654; propset=655 }
    $memberName = $Name
    $isAccessor = $Type -in @('propget','propset')
    $accName = if ($Type -eq 'propget') { 'Get' } else { 'Set' }
    $memberPath = if ($isAccessor) { "$Name.$accName" } else { $Name }
    $resolved = ''
    $sys = Get-TcSystemManager $Dte
    # 访问器必须在属性节点下创建。把两个分支拆开，避免 Invoke-OnPou 的参数作用域
    # 覆盖外层 $Type/$isAccessor，导致 654/655 被错误地建在接口节点上。
    if ($isAccessor) {
        $base = Get-PlcProjectPath $Dte
        $o = $null
        if ($TreePath) { try { $o = $sys.LookupTreeItem($TreePath) } catch { } }
        if ($null -eq $o) {
            foreach ($folderName in @('Interfaces','POUs')) {
                try { $o = $sys.LookupTreeItem("$base^$folderName^$Pou") } catch { $o = $null }
                if ($null -ne $o) { break }
            }
        }
        if ($null -eq $o) { throw "POU/接口 '$Pou' 不存在" }
        $isItfLocal = ([int]$o.ItemType -eq 618)
        if ($isItfLocal) {
            throw 'Interface property accessors are disabled on this XAE COM bridge because TwinCAT can terminate during their creation.'
        }
        $stLocal = if ($isItfLocal) {
            if ($accName -eq 'Get') { 654 } else { 655 }
        } else {
            if ($accName -eq 'Get') { 613 } else { 614 }
        }
        $prop = $null
        foreach ($c in $o) {
            if ([string]$c.Name -eq $memberName) { $prop = $c; break }
        }
        if ($null -eq $prop) { throw "属性 '$memberName' 不存在于 '$Pou', 请先建 property" }
        # Accessors accept no vInfo for interfaces and IEC language for POUs.
        try {
        $accessorVInfo = if ($isItfLocal) { $null } else { 'ST' }
        $prop.CreateChild($accName, $stLocal, '', $accessorVInfo) | Out-Null
        } catch {
            throw "Create accessor failed: acc=$accName subtype=$stLocal memberType=$Type return=$ReturnType language=$Language; $($_.Exception.Message)"
        }
        $resolved = if ($isItfLocal) { 'interface' } else { 'pou' }
    } else {
        $resolved = Invoke-OnPou $Dte $Pou {
            param($o)
            $isItfLocal = ([int]$o.ItemType -eq 618)
            if ($isItfLocal -and $Type -eq 'property') {
                throw 'Interface properties are disabled on this XAE COM bridge because TwinCAT can terminate while creating their accessors; use an interface method or create the complete property manually in XAE.'
            }
            $stLocal = if ($isItfLocal) { $itfMap[$Type] } else { $pouMap[$Type] }
            if ($null -eq $stLocal) {
                throw "成员类型 '$Type' 不支持于$(if($isItfLocal){'接口'}else{'POU'}) '$Pou'"
            }
            # POU methods/properties require [language, return type].
            # Interface methods/properties take the return type only.
            # Actions/transitions take the IEC language only.
            $memberVInfo = if ($isItfLocal) {
                $ReturnType
            } elseif ($Type -in @('method','property')) {
                [string[]]@($Language, $ReturnType)
            } else {
                $Language
            }
            $o.CreateChild($memberName, $stLocal, '', $memberVInfo) | Out-Null
            if ($isItfLocal) { 'interface' } else { 'pou' }
        }.GetNewClosure() -TreePath $TreePath
    }

    Start-Sleep -Milliseconds 400
    $parentIdentity = Get-PlcObjectIdentity $Dte $Pou $TreePath
    $verifiedParent = $sys.LookupTreeItem($parentIdentity.path)
    $memberTarget = $verifiedParent
    foreach ($segment in ($memberPath -split '\.')) {
        $memberTarget = @($memberTarget | Where-Object { [string]$_.Name -ieq $segment }) | Select-Object -First 1
        if ($null -eq $memberTarget) { throw "Created member '$memberPath' was not found after tree refresh under '$($parentIdentity.path)'." }
    }
    $actualMemberType = 0; try { $actualMemberType = [int]$memberTarget.ItemType } catch { }
    if ($Declaration)    { Write-Pou $Dte $Pou declaration    $Declaration -Method $memberPath -TreePath $parentIdentity.path | Out-Null }
    if ($Implementation) { Write-Pou $Dte $Pou implementation $Implementation -Method $memberPath -TreePath $parentIdentity.path | Out-Null }
    [pscustomobject]@{ pou = $Pou; member = $memberPath; type = $Type
                       parentKind = [string]$resolved; status = 'created'
                       path=[string]$parentIdentity.path; member_path=("{0}^{1}" -f $parentIdentity.path, $memberPath)
                       itemType=$actualMemberType; verified=$true }
}

# ---- 删除 POU/接口内部成员 ----
function Remove-PouMember {
    param([Parameter(Mandatory)]$Dte, [Parameter(Mandatory)][string]$Pou,
          [Parameter(Mandatory)][string]$Name,
          [ValidateSet('method','property','action','transition','propget','propset')]
          [string]$Type = 'method', [string]$TreePath = '',
          [bool]$DryRun = $false, [bool]$Force = $false)

    $search = Search-PlcCode $Dte ([regex]::Escape($Name)) $true $false '' 100
    $exclude = "$Pou.$Name"
    $refs = @($search.matches | Where-Object {
        $candidate = [string]$_.pou
        -not ($candidate.Equals($exclude, [StringComparison]::OrdinalIgnoreCase) -or
              $candidate.StartsWith("$exclude.", [StringComparison]::OrdinalIgnoreCase)) })
    if ($DryRun) {
        return [pscustomobject]@{ pou=$Pou; member=$Name; type=$Type; path=$TreePath
            status='preview'; would_block=($refs.Count -gt 0 -and -not $Force)
            reference_count=$refs.Count; references=$refs }
    }
    if ($refs.Count -gt 0 -and -not $Force) {
        return [pscustomobject]@{ pou=$Pou; member=$Name; type=$Type; path=$TreePath
            status='blocked'; error="Member has $($refs.Count) reference(s); inspect references or pass force=true."
            reference_count=$refs.Count; references=$refs }
    }

    $deleted = Invoke-OnPou $Dte $Pou {
        param($o)
        if ($Type -in @('propget','propset')) {
            $prop = $null
            foreach ($child in $o) {
                if ([string]$child.Name -eq $Name) { $prop = $child; break }
            }
            if ($null -eq $prop) { throw "属性 '$Name' 不存在于 '$Pou'" }
            $accessorName = if ($Type -eq 'propget') { 'Get' } else { 'Set' }
            $accessor = $null
            foreach ($child in $prop) {
                if ([string]$child.Name -eq $accessorName) { $accessor = $child; break }
            }
            if ($null -eq $accessor) { throw "访问器 '$Name.$accessorName' 不存在于 '$Pou'" }
            $prop.DeleteChild([string]$accessor.Name)
            return "$Name.$accessorName"
        }
        $expected = @{
            method=@(609,610); property=@(611,612); action=@(608); transition=@(616)
        }[$Type]
        $member = $null
        foreach ($child in $o) {
            if ([string]$child.Name -eq $Name) { $member = $child; break }
        }
        if ($null -eq $member) { throw "成员 '$Name' 不存在于 '$Pou'" }
        if ($expected -notcontains [int]$member.ItemType) {
            throw "成员 '$Name' 存在，但类型不是 '$Type'"
        }
        $o.DeleteChild([string]$member.Name)
        return [string]$member.Name
    }.GetNewClosure() -TreePath $TreePath
    [pscustomobject]@{ pou=$Pou; member=[string]$deleted; type=$Type
                       path=$TreePath; status='deleted'; reference_count=$refs.Count
                       references=$refs }
}

function Rename-PouMember {
    param([Parameter(Mandatory)]$Dte, [Parameter(Mandatory)][string]$Pou,
          [Parameter(Mandatory)][string]$OldName,
          [Parameter(Mandatory)][string]$NewName, [string]$TreePath = '')
    $renamed = Invoke-OnPou $Dte $Pou {
        param($o)
        $target = $null; $collision = $false
        foreach ($child in $o) {
            if ([string]$child.Name -eq $OldName) { $target = $child }
            if ([string]$child.Name -eq $NewName) { $collision = $true }
        }
        if ($null -eq $target) { throw "成员 '$OldName' 不存在于 '$Pou'" }
        if ($collision) { throw "成员 '$NewName' 已存在于 '$Pou'" }
        $target.Name = $NewName
        return $true
    }.GetNewClosure() -TreePath $TreePath
    [pscustomobject]@{ pou=$Pou; old=$OldName; new=$NewName
                       path=$TreePath; status='renamed' }
}

# ---- Win32: 前置窗口 + 键盘事件 (读 Error List 用, 纯 PowerShell 无 pyautogui) ----
#  为什么不用 COM 读错误: TcXaeShell 15 (VS2017/Express shell) 下, 跨进程自动化连接
#  的 UI 线程对象无法编组 —— ToolWindows.ErrorList.ErrorItems 恒返回 0, Windows/
#  OutputWindow 集合的 Caption/Count 也是空 (自动化对象模型 Solution/SolutionBuild/
#  POU 读写正常, 只有 UI 对象模型失效)。v0.1 的原始注释早已记录 "DTE2/ErrorItems 不
#  可用", 当时用 pyautogui 复制。这里用纯 Win32 键盘事件复刻同一物理复制, 不引入依赖。
if (-not ('TcW32' -as [type])) {
    Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public class TcW32 {
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern void keybd_event(byte vk, byte scan, uint flags, IntPtr extra);
}
"@
}

function Send-CtrlKey {
    param([Parameter(Mandatory)][char]$Key)
    $VK_CTRL = 0x11; $KEYUP = 0x2
    $vk = [byte][char]([string]$Key).ToUpper()
    [TcW32]::keybd_event($VK_CTRL, 0, 0, [IntPtr]::Zero)
    [TcW32]::keybd_event($vk, 0, 0, [IntPtr]::Zero)
    Start-Sleep -Milliseconds 60
    [TcW32]::keybd_event($vk, 0, $KEYUP, [IntPtr]::Zero)
    [TcW32]::keybd_event($VK_CTRL, 0, $KEYUP, [IntPtr]::Zero)
}

# ---- 读 Error List: 前置 IDE → 聚焦错误列表 → Ctrl+A/Ctrl+C → 解析 TSV ----
#  返回结构化条目数组。列(制表符分隔): 严重性 代码 说明 项目 文件 行 禁止显示状态。
function Read-ErrorList {
    param([Parameter(Mandatory)]$Dte)
    Add-Type -AssemblyName System.Windows.Forms
    # 读前备份: 原前台窗口 + 剪贴板内容 —— 读完原样还原, 尽量少打扰用户。
    #  (TcXaeShell 15 下 COM ErrorItems 与 UIA 都读不到内容, 只能物理复制;
    #   无法完全不抢焦点, 但可以做到"用完还回去"。)
    $prevFg = [IntPtr]::Zero
    try { $prevFg = [TcW32]::GetForegroundWindow() } catch { }
    $prevClip = $null
    try { if ([System.Windows.Forms.Clipboard]::ContainsText()) {
              $prevClip = [System.Windows.Forms.Clipboard]::GetText() } } catch { }

    $proc = Get-Process TcXaeShell -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $proc) { $proc = Get-Process devenv -ErrorAction SilentlyContinue | Select-Object -First 1 }
    if ($proc) {
        # 仅当窗口最小化时才 SW_RESTORE(9)。对最大化窗口调 SW_RESTORE 会把它
        # 还原成普通大小 —— 每次编译都让 XAE 退出最大化, 不能无条件调。
        if ([TcW32]::IsIconic($proc.MainWindowHandle)) {
            [TcW32]::ShowWindow($proc.MainWindowHandle, 9) | Out-Null   # SW_RESTORE
        }
        [TcW32]::SetForegroundWindow($proc.MainWindowHandle) | Out-Null
        Start-Sleep -Milliseconds 400
    }
    try { $Dte.ExecuteCommand('View.ErrorList') } catch { }
    # ExecuteCommand opens the tool window but does not always move keyboard
    # focus to its grid. Activate the Error List explicitly before Ctrl+A/C.
    try { $Dte.ToolWindows.ErrorList.Activate() } catch { }
    Start-Sleep -Milliseconds 600
    [System.Windows.Forms.Clipboard]::Clear()
    Send-CtrlKey 'a'; Start-Sleep -Milliseconds 200
    Send-CtrlKey 'c'; Start-Sleep -Milliseconds 500
    $clip = ''
    try { $clip = [System.Windows.Forms.Clipboard]::GetText() } catch { }

    # 还原剪贴板与前台窗口
    try {
        if ($null -ne $prevClip) { [System.Windows.Forms.Clipboard]::SetText($prevClip) }
        else { [System.Windows.Forms.Clipboard]::Clear() }
    } catch { }
    if ($prevFg -ne [IntPtr]::Zero -and $prevFg -ne $proc.MainWindowHandle) {
        try { [TcW32]::SetForegroundWindow($prevFg) | Out-Null } catch { }
    }

    $items = @()
    foreach ($ln in ($clip -split "`r?`n")) {
        if (-not $ln.Trim()) { continue }
        $col = $ln -split "`t"
        if ($col.Count -lt 3) { continue }
        $sevRaw = $col[0].Trim()
        # 跳过表头 (严重性/Severity)
        if ($sevRaw -in @('严重性','Severity')) { continue }
        $sev = switch -Regex ($sevRaw) {
            '错误|Error'   { 'error';   break }
            '警告|Warning' { 'warning'; break }
            default        { 'message' }
        }
        $lineNo = 0; if ($col.Count -ge 6) { [void][int]::TryParse(($col[5].Trim()), [ref]$lineNo) }
        $desc = if ($col.Count -ge 3) { $col[2].Trim() } else { '' }
        $code = if ($col.Count -ge 2) { $col[1].Trim() } else { '' }
        # 中文 locale 下 code 列为空, 错误码(C0032)前缀在说明里 —— 补提取
        if (-not $code -and $desc -match '^\s*(C\d+)\s*:') { $code = $matches[1] }
        $items += [pscustomobject]@{
            severity    = $sev
            code        = $code
            description = $desc
            project     = if ($col.Count -ge 4) { $col[3].Trim() } else { '' }
            file        = if ($col.Count -ge 5) { $col[4].Trim() } else { '' }
            line        = $lineNo
        }
    }
    # Do not prepend the PowerShell comma operator here.  `return ,$items`
    # wraps an empty array as one object, which made an empty clipboard look
    # like `errorsRead=true` while still producing no diagnostics.
    return $items
}

# ---- 解决方案构建平台（纯 DTE/PowerShell COM，兼容混合 HMI + TwinCAT 工程）----
function Get-TcSolutionBuildPlatforms {
    param([Parameter(Mandatory)]$Dte)
    $slnPath = [string]$Dte.Solution.FullName
    if (-not $slnPath -or -not [IO.File]::Exists($slnPath)) {
        throw 'No saved solution is open.'
    }
    $text = [IO.File]::ReadAllText($slnPath)
    $section = [regex]::Match(
        $text,
        '(?ms)^\s*GlobalSection\(SolutionConfigurationPlatforms\)\s*=\s*preSolution\s*$\r?\n(?<body>.*?)^\s*EndGlobalSection\s*$')
    if (-not $section.Success) { throw "SolutionConfigurationPlatforms is missing in '$slnPath'." }

    $platforms = @()
    $index = 0
    foreach ($line in ($section.Groups['body'].Value -split "`r?`n")) {
        if ($line -notmatch '^\s*(?<full>[^=]+?)\s*=\s*(?<mapped>.+?)\s*$') { continue }
        $full = [string]$matches['full'].Trim()
        $parts = @($full -split '\|', 2)
        if ($parts.Count -ne 2) { continue }
        $index++
        $platforms += [pscustomobject]@{
            index = $index
            config = [string]$parts[0]
            platform = [string]$parts[1]
            full = $full
        }
    }

    $active = ''
    try {
        $activeObject = $Dte.Solution.SolutionBuild.ActiveConfiguration
        $active = [string]$activeObject.Name
        if ($active -notmatch '\|' -and
            $activeObject.PSObject.Properties.Name -contains 'PlatformName') {
            $platformName = [string]$activeObject.PlatformName
            if ($platformName) { $active = "$active|$platformName" }
        }
    } catch { }
    [pscustomobject]@{
        solution = $slnPath
        active = $active
        platforms = $platforms
    }
}

function Get-TcActiveBuildContexts {
    param([Parameter(Mandatory)]$Dte)
    $items = @()
    try {
        $configuration = $Dte.Solution.SolutionBuild.ActiveConfiguration
        for ($i = 1; $i -le $configuration.SolutionContexts.Count; $i++) {
            $context = $configuration.SolutionContexts.Item($i)
            $items += [pscustomobject]@{
                project = [string]$context.ProjectName
                configuration = [string]$context.ConfigurationName
                platform = [string]$context.PlatformName
                should_build = [bool]$context.ShouldBuild
            }
        }
    } catch { }
    return $items
}

function Get-TcBuildCommandState {
    param([Parameter(Mandatory)]$Dte, [Parameter(Mandatory)][string]$Name)
    try {
        $command = $Dte.Commands.Item($Name)
        try {
            return [pscustomobject]@{ name=$Name; available=[bool]$command.IsAvailable;
                source='dte-command-isavailable' }
        } catch {
            return [pscustomobject]@{ name=$Name; available=$null;
                source='dte-command-lookup'; error=$_.Exception.Message }
        }
    } catch {
        return [pscustomobject]@{ name=$Name; available=$false;
            source='dte-command-lookup'; error=$_.Exception.Message }
    }
}

function Get-TcBuildState {
    param([Parameter(Mandatory)]$Dte)
    $build = $Dte.Solution.SolutionBuild
    $rawState = $null; $last = $null; $active = ''
    try { $rawState = [int]$build.BuildState } catch { }
    try { $last = [int]$build.LastBuildInfo } catch { }
    try { $active = [string]$build.ActiveConfiguration.Name } catch { }
    # Current TcXaeShell EnvDTE: 1=NotStarted, 2=InProgress, 3=Done.
    # Preserve unknown values as unknown; never treat them as idle.
    $busy = $null
    if ($rawState -in @(1,2,3)) { $busy = ($rawState -eq 2) }
    [pscustomobject]@{
        status='read'; solution=[string]$Dte.Solution.FullName
        build_state=$rawState; last_build_info=$last
        active_configuration=$active; busy=$busy
        commands=[pscustomobject]@{
            build=(Get-TcBuildCommandState $Dte 'Build.BuildSolution')
            rebuild=(Get-TcBuildCommandState $Dte 'Build.RebuildSolution')
        }
        source='dte-solution-build-and-commands'
    }
}

function Set-TcSolutionBuildPlatform {
    param([Parameter(Mandatory)]$Dte, [Parameter(Mandatory)][string]$FullName)
    $info = Get-TcSolutionBuildPlatforms $Dte
    $match = @($info.platforms | Where-Object {
        [string]$_.full -ieq $FullName
    })
    if ($match.Count -ne 1) {
        throw "Build platform '$FullName' was not found. Available: $(@($info.platforms.full) -join ', ')"
    }
    $configuration = $Dte.Solution.SolutionBuild.SolutionConfigurations.Item([int]$match[0].index)
    $configuration.Activate()
    $readback = Get-TcSolutionBuildPlatforms $Dte
    if (-not [string]$readback.active -or [string]$readback.active -ine [string]$match[0].full) {
        throw "Build platform readback mismatch: requested '$($match[0].full)', active '$($readback.active)'."
    }
    [pscustomobject]@{
        status = 'ok'
        config = [string]$match[0].config
        platform = [string]$match[0].platform
        full = [string]$match[0].full
        index = [int]$match[0].index
        contexts = @(Get-TcActiveBuildContexts $Dte)
    }
}

# ---- 编译状态（成功时不碰 UI，失败时才启用 Error List 兼容回退）----
# 首选嵌入式 VSIX 在 XAE UI 线程读取结构化诊断。只有当前环境退回 PowerShell
# 桥且 LastBuildInfo 报告失败时，才激活 Error List 并 Ctrl+A/Ctrl+C，以避免
# “构建失败但无错误详情”的假成功；读取结束后会恢复剪贴板和原前台窗口。
function Invoke-TcBuild {
    param([Parameter(Mandatory)]$Dte, [int]$TimeoutSec = 120,
          [bool]$AlwaysReadErrors = $false,
          [ValidateSet('build','rebuild')][string]$Action = 'build')
    $platformInfo = Get-TcSolutionBuildPlatforms $Dte
    $buildContexts = @(Get-TcActiveBuildContexts $Dte)
    # Build($true) = WaitForBuildToFinish, 同步阻塞至编译结束; 避免 BuildState 竞态
    # (vsBuildState: 1=NotStarted 2=InProgress 3=Done —— 旧代码等 2 是错的)。
    try {
        if ($Action -eq 'rebuild') {
            $Dte.ExecuteCommand('Build.RebuildSolution')
            $deadline = (Get-Date).AddSeconds($TimeoutSec)
            while ((Get-Date) -lt $deadline) {
                try { if ([int]$Dte.Solution.SolutionBuild.BuildState -eq 3) { break } } catch { }
                Start-Sleep -Milliseconds 500
            }
        } else {
            $Dte.Solution.SolutionBuild.Build($true)
        }
    } catch {
        if ($Action -eq 'rebuild') { throw }
        try { $Dte.ExecuteCommand('Build.BuildSolution') } catch { }
        $deadline = (Get-Date).AddSeconds($TimeoutSec)
        while ((Get-Date) -lt $deadline) {
            try { if ($Dte.Solution.SolutionBuild.BuildState -eq 3) { break } } catch { }
            Start-Sleep -Milliseconds 500
        }
    }
    $failed = 0; try { $failed = [int]$Dte.Solution.SolutionBuild.LastBuildInfo } catch { }

    # The normal successful-build path remains non-invasive.  On the legacy
    # PowerShell transport, a failed build has no UI-thread/native diagnostic
    # channel, so read the Error List once as a failure-only fallback. This is
    # the last-resort path for machines without pythoncom or the updated VSIX.
    $items = @()
    $readOnFailure = ($failed -gt 0)
    if ($AlwaysReadErrors -or $readOnFailure) {
        try { $items = @(Read-ErrorList $Dte) } catch { $items = @() }
    }
    $errors = @($items | Where-Object {
        $_.severity -eq 'error' -or
        ($failed -gt 0 -and $_.project -and $_.file -and
         ($_.code -match '^(C|T)\d{4}$' -or $_.description -match '^\s*(C|T)\d{4}\s*:'))
    })
    $warnings = @($items | Where-Object { $errors -notcontains $_ -and $_.severity -eq 'warning' })
    $pending = ($failed -gt 0 -and $errors.Count -eq 0)

    [pscustomobject]@{
        buildPerformed = $true
        build_action    = $Action
        failedProjects = $failed
        errorCount     = $errors.Count
        errors         = $errors
        warnings       = $warnings
        errorsRead     = [bool]$items.Count
        errorSource    = if ($AlwaysReadErrors) { 'clipboard-error-list-explicit' }
                          elseif ($readOnFailure) { 'clipboard-error-list-failure-fallback' }
                          else { 'not-read-no-focus' }
        diagnosticsPending = $pending
        activeConfiguration = [string]$platformInfo.active
        buildContexts = $buildContexts
        message        = if ($failed -eq 0) { 'Build succeeded' } elseif ($pending) {
            'Build failed; diagnosticsPending=true. Retry or use the embedded XAE Agent panel.'
        } else {
            'Build failed'
        }
    }
}

# ---- 内部: 取 NestedProject 节点 (含 POUs/DUTs/GVLs 文件夹) ----
#  注意: NestedProject 是可枚举 COM 项, 绝不 return 出函数 (会被管道展开成子节点)。
#  所有用到它的函数都在自身作用域内 foreach, 只返回纯数据。

# ---- 删除对象 (DeleteChild) ----
function Remove-Pou {
    param([Parameter(Mandatory)]$Dte, [Parameter(Mandatory)][string]$Name,
          [string[]]$Folders = @('POUs','DUTs','GVLs','Interfaces','VISUs'),
          [string]$TreePath = '', [bool]$DryRun = $false, [bool]$Force = $false)
    $sys = Get-TcSystemManager $Dte
    $matches = @(Get-PlcObjectFrames $Dte -Folders $Folders | Where-Object {
        ([string]$_.name -ieq $Name) -and (-not $TreePath -or [string]$_.path -ieq $TreePath) })
    if ($matches.Count -eq 0) { throw "Object '$Name' not found in any PLC folder." }
    if ($matches.Count -gt 1) { throw "Object '$Name' is ambiguous; pass its exact path." }
    $frame = $matches[0]
    $search = Search-PlcCode $Dte ([regex]::Escape($Name)) $true $false '' 100
    $refs = @($search.matches | Where-Object {
        $candidate = [string]$_.pou
        -not ($candidate.Equals($Name, [StringComparison]::OrdinalIgnoreCase) -or
              $candidate.StartsWith("$Name.", [StringComparison]::OrdinalIgnoreCase)) })
    if ($DryRun) {
        return [pscustomobject]@{ name=$Name; path=[string]$frame.path; status='preview'
            would_block=($refs.Count -gt 0 -and -not $Force); reference_count=$refs.Count; references=$refs }
    }
    if ($refs.Count -gt 0 -and -not $Force) {
        return [pscustomobject]@{ name=$Name; path=[string]$frame.path; status='blocked'
            error="Object has $($refs.Count) reference(s); inspect references or pass force=true."
            reference_count=$refs.Count; references=$refs }
    }
    $parent = $sys.LookupTreeItem([string]$frame.parent)
    $parent.DeleteChild([string]$frame.node.Name)
    [pscustomobject]@{ name=$Name; folder=[string]$frame.folder; path=[string]$frame.path
        status='deleted'; reference_count=$refs.Count; references=$refs }
}

# ---- 重命名对象 (设 ITcSmTreeItem.Name), 带同名碰撞检查 ----
function Rename-Pou {
    param([Parameter(Mandatory)]$Dte, [Parameter(Mandatory)][string]$OldName,
          [Parameter(Mandatory)][string]$NewName,
          [string[]]$Folders = @('POUs','DUTs','GVLs','Interfaces','VISUs'),
          [string]$TreePath = '')
    $matches = @(Get-PlcObjectFrames $Dte -Folders $Folders | Where-Object {
        ([string]$_.name -ieq $OldName) -and (-not $TreePath -or [string]$_.path -ieq $TreePath) })
    if ($matches.Count -eq 0) { throw "Object '$OldName' not found in any PLC folder." }
    if ($matches.Count -gt 1) { throw "Object '$OldName' is ambiguous; pass its exact path." }
    $frame = $matches[0]
    $sys = Get-TcSystemManager $Dte
    $parent = $sys.LookupTreeItem([string]$frame.parent)
    foreach ($sibling in $parent) {
        if ([string]$sibling.Name -ieq $NewName) { throw "'$NewName' already exists beside '$OldName'" }
    }
    $frame.node.Name = $NewName
    [pscustomobject]@{ old=$OldName; new=$NewName; folder=[string]$frame.folder
        path=[string]$frame.path; status='renamed' }
}

# ---- 内部: PLC 项目树路径 'TIPC^<实例>^<项目>' (纯字符串, 可安全返回) ----
#  本地化健壮: 不匹配节点名里的 'Project'/'项目' 字样, 只认 NestedProject 属性;
#  回退到 '*Instance*' 子节点 (老工程模式)。
function Get-PlcProjectPath {
    param([Parameter(Mandatory)]$Dte)
    $sys = Get-TcSystemManager $Dte
    $plc = $sys.LookupTreeItem('TIPC')
    foreach ($c in $plc) {
        $nested = $null; try { $nested = $c.NestedProject } catch { }
        if ($null -ne $nested) { return ('TIPC^{0}^{1}' -f [string]$c.Name, [string]$nested.Name) }
        foreach ($sub in $c) {
            if ([string]$sub.Name -like '*Instance*') {
                return ('TIPC^{0}^{1}' -f [string]$c.Name, [string]$sub.Name)
            }
        }
    }
    throw 'No PLC project found under TIPC. Create one first (create-plc-project).'
}

# ---- PLC 项目 CRUD (TIPC 下 CreateChild / DeleteChild) ----
function New-PlcProject {
    param([Parameter(Mandatory)]$Dte, [string]$Name = 'PLC1',
          [string]$Template = 'Standard PLC Template')
    $sys = Get-TcSystemManager $Dte
    $plc = $sys.LookupTreeItem('TIPC')
    foreach ($c in $plc) { if ([string]$c.Name -eq $Name) { throw "PLC project '$Name' already exists" } }
    $plc.CreateChild($Name, 0, '', $Template) | Out-Null
    try { $Dte.ExecuteCommand('File.SaveAll') }
    catch { throw "PLC project '$Name' was created in XAE but File.SaveAll failed: $($_.Exception.Message)" }
    Start-Sleep -Milliseconds 500
    $created = $null
    foreach ($candidate in $plc) {
        if ([string]$candidate.Name -ieq $Name) { $created = $candidate; break }
    }
    if ($null -eq $created) { throw "PLC project '$Name' creation could not be verified under TIPC." }
    $nestedName = ''; try { $nestedName = [string]$created.NestedProject.Name } catch { }
    [pscustomobject]@{
        name = $Name; template = $Template; status = 'created'; saved = $true
        path = 'TIPC^{0}' -f [string]$created.Name
        nested_project = $nestedName; verified = [bool]$nestedName
    }
}

function Remove-PlcProject {
    param([Parameter(Mandatory)]$Dte, [Parameter(Mandatory)][string]$Name)
    $sys = Get-TcSystemManager $Dte
    $plc = $sys.LookupTreeItem('TIPC')
    $found = $false
    foreach ($c in $plc) { if ([string]$c.Name -eq $Name) { $found = $true } }
    if (-not $found) { throw "PLC project '$Name' not found" }
    $plc.DeleteChild($Name)
    [pscustomobject]@{ name = $Name; status = 'removed'; files_deleted = $false }
}

function Remove-PlcProjectFiles {
    param([Parameter(Mandatory)]$Dte, [Parameter(Mandatory)][string]$Name)
    try { $Dte.ExecuteCommand('File.SaveAll') } catch { }
    $systemProject = Get-TcSystemProject $Dte
    $tsproj = [IO.Path]::GetFullPath([string]$systemProject.FullName)
    if ([IO.Path]::GetExtension($tsproj).ToLowerInvariant() -ne '.tsproj') {
        throw "TwinCAT system project path is unavailable: $tsproj"
    }
    [xml]$document = Get-Content -LiteralPath $tsproj -Raw -Encoding UTF8
    $projectNode = @($document.SelectNodes("//*[local-name()='Plc']/*[local-name()='Project']")) |
        Where-Object { [string]$_.Name -eq $Name } | Select-Object -First 1
    if ($null -eq $projectNode -or -not [string]$projectNode.PrjFilePath) {
        throw "PLC project '$Name' has no PrjFilePath in $tsproj"
    }
    $root = [IO.Path]::GetDirectoryName($tsproj).TrimEnd([IO.Path]::DirectorySeparatorChar)
    $projectFile = [IO.Path]::GetFullPath((Join-Path $root ([string]$projectNode.PrjFilePath)))
    $projectDir = [IO.Path]::GetDirectoryName($projectFile).TrimEnd([IO.Path]::DirectorySeparatorChar)
    $prefix = $root + [IO.Path]::DirectorySeparatorChar
    if (-not $projectFile.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "PLC project path escapes the system project: $projectFile"
    }
    if ([IO.Path]::GetExtension($projectFile).ToLowerInvariant() -ne '.plcproj') {
        throw "Unsafe PLC project file type: $projectFile"
    }
    if ($projectDir -eq $root) {
        throw 'PLC project is stored beside the .tsproj; automatic directory deletion is unsafe'
    }
    if (-not [IO.File]::Exists($projectFile)) { throw "PLC project file not found: $projectFile" }
    $otherProjects = @(Get-ChildItem -LiteralPath $projectDir -Filter '*.plcproj' -File |
        Where-Object { [IO.Path]::GetFullPath($_.FullName) -ne $projectFile })
    if ($otherProjects.Count -gt 0) {
        throw "PLC directory contains other projects and will not be deleted: $projectDir"
    }
    $plc = $systemProject.Object.LookupTreeItem('TIPC')
    $target = @($plc) | Where-Object { [string]$_.Name -eq $Name } | Select-Object -First 1
    if ($null -eq $target) { throw "PLC project '$Name' not found" }
    $plc.DeleteChild([string]$target.Name)
    try { $Dte.ExecuteCommand('File.SaveAll') } catch { }
    try { [IO.Directory]::Delete($projectDir, $true) }
    catch {
        throw "PLC project was removed from XAE, but its files could not be deleted: $projectDir : $($_.Exception.Message)"
    }
    [pscustomobject]@{
        name = $Name; status = 'deleted'; files_deleted = $true
        project_file = $projectFile; deleted_directory = $projectDir
    }
}

# ---- 解决方案 / 项目信息 (只返回纯数据，COM 项不跨函数边界) ----
function Get-TcProjects {
    param([Parameter(Mandatory)]$Dte)
    $solutionProjects = @()
    $plcProjects = @()
    $projectCount = 0
    try { $projectCount = [int]$Dte.Solution.Projects.Count } catch { }

    for ($i = 1; $i -le $projectCount; $i++) {
        try {
            $project = $Dte.Solution.Projects.Item($i)
            $solutionProjects += [string]$project.Name
            try {
                $tipc = $project.Object.LookupTreeItem('TIPC')
                foreach ($child in $tipc) {
                    $plcProjects += [string]$child.Name
                }
            } catch { }
        } catch { }
    }

    [pscustomobject]@{
        solution_projects = @($solutionProjects)
        plc_projects      = @($plcProjects)
    }
}

function Get-TcAdsPortFromItem {
    param($Item)
    if ($null -eq $Item) {
        return [pscustomobject]@{ ads_port = $null; source = 'unresolved' }
    }
    foreach ($propertyName in @('AdsPort', 'AmsPort', 'Port')) {
        try {
            $value = Convert-TcSystemInteger $Item.$propertyName $propertyName 65535
            if ($value -ge 1) {
                return [pscustomobject]@{
                    ads_port = [int]$value
                    source = "property:$propertyName"
                }
            }
        } catch { }
    }
    $raw = ''
    try { $raw = [string]$Item.ProduceXml($false) }
    catch { try { $raw = [string]$Item.ProduceXml() } catch { } }
    if (-not [string]::IsNullOrWhiteSpace($raw)) {
        try {
            [xml]$xml = $raw
            foreach ($tag in @('AdsPort', 'AmsPort', 'Port')) {
                $node = $xml.SelectSingleNode("//*[local-name()='$tag']")
                if ($null -ne $node) {
                    try {
                        $value = Convert-TcSystemInteger $node.InnerText $tag 65535
                        if ($value -ge 1) {
                            return [pscustomobject]@{
                                ads_port = [int]$value
                                source = "xml:$tag"
                            }
                        }
                    } catch { }
                }
                $attribute = $xml.SelectSingleNode("//@*[local-name()='$tag']")
                if ($null -ne $attribute) {
                    try {
                        $value = Convert-TcSystemInteger $attribute.Value $tag 65535
                        if ($value -ge 1) {
                            return [pscustomobject]@{
                                ads_port = [int]$value
                                source = "xml-attribute:$tag"
                            }
                        }
                    } catch { }
                }
            }
        } catch { }
    }
    return [pscustomobject]@{ ads_port = $null; source = 'unresolved' }
}

function Get-TcPlcRuntimes {
    param([Parameter(Mandatory)]$Dte)
    $sys = Get-TcSystemManager $Dte
    $tipc = $sys.LookupTreeItem('TIPC')
    $runtimes = New-Object System.Collections.ArrayList
    foreach ($root in @($tipc)) {
        $nested = $null
        try { $nested = $root.NestedProject } catch { }
        $children = @()
        try { $children = @($root) } catch { }
        $instance = $children | Where-Object {
            try { ([string]$_.Name).IndexOf('instance', [StringComparison]::OrdinalIgnoreCase) -ge 0 }
            catch { $false }
        } | Select-Object -First 1
        if ($null -eq $instance) {
            foreach ($child in $children) {
                $candidate = Get-TcAdsPortFromItem $child
                if ($null -ne $candidate.ads_port) { $instance = $child; break }
            }
        }
        $portInfo = [pscustomobject]@{ ads_port = $null; source = 'unresolved' }
        foreach ($candidate in @($instance, $root, $nested)) {
            if ($null -eq $candidate) { continue }
            $portInfo = Get-TcAdsPortFromItem $candidate
            if ($null -ne $portInfo.ads_port) { break }
        }
        $rootPath = ''
        $instanceName = ''
        $instancePath = ''
        try { $rootPath = [string]$root.PathName } catch { }
        try { $instanceName = [string]$instance.Name } catch { }
        try { $instancePath = [string]$instance.PathName } catch { }
        [void]$runtimes.Add([pscustomobject]@{
            name = [string]$root.Name
            project_name = if ($null -ne $nested) { [string]$nested.Name } else { '' }
            instance = $instanceName
            path = if ($instancePath) { $instancePath } else { $rootPath }
            ads_port = $portInfo.ads_port
            port_source = [string]$portInfo.source
        })
    }
    [pscustomobject]@{
        target_netid = [string]$sys.GetTargetNetId()
        plcs = @($runtimes.ToArray())
    }
}

function Get-TcProjectInfo {
    param([Parameter(Mandatory)]$Dte)
    $projects = Get-TcProjects $Dte
    $solution = ''
    $projectCount = 0
    $targetNetId = ''
    try { $solution = [string]$Dte.Solution.FullName } catch { }
    try { $projectCount = [int]$Dte.Solution.Projects.Count } catch { }

    for ($i = 1; $i -le $projectCount; $i++) {
        try {
            $sys = $Dte.Solution.Projects.Item($i).Object
            $sys.LookupTreeItem('TIPC') | Out-Null
            try { $targetNetId = [string]$sys.GetTargetNetId() } catch { }
            break
        } catch { }
    }

    [pscustomobject]@{
        solution      = $solution
        project_count = $projectCount
        target_netid  = $targetNetId
        plc_projects  = @($projects.plc_projects)
    }
}

# ---- TwinCAT HMI project tools (standard DTE + file contract) ----
# TE2000 HMI projects are regular Visual Studio projects.  The page/control
# model lives in .view/.content markup while server configuration is JSON.
# There is no TwinCAT System Manager tree for these objects, so use DTE only
# to locate/save/include project items and use validated file generation for
# the actual HMI artifacts.
function Test-TcHmiComBusy {
    param([Exception]$Exception)
    for ($current = $Exception; $null -ne $current; $current = $current.InnerException) {
        if ($current.HResult -in @(-2147418111, -2147417846) -or
            $current.Message -match '0x80010001|0x8001010A|RPC_E_CALL_REJECTED|RPC_E_SERVERCALL_RETRYLATER') { return $true }
    }
    return $false
}

function Get-TcHmiCreationState {
    param($Dte, [string]$SolutionFile, [string]$ProjectFile)
    try { $sameSolution = ([string]$Dte.Solution.FullName).Equals($SolutionFile, [StringComparison]::OrdinalIgnoreCase) }
    catch {
        if (-not (Test-TcHmiComBusy $_.Exception)) { throw }
        return [pscustomobject]@{ same_solution=$null; file_exists=[IO.File]::Exists($ProjectFile);
            file_valid=$false; loaded_in_xae=$false; persisted_in_solution=$false; busy=$true }
    }
    $exists = [IO.File]::Exists($ProjectFile); $valid = $false; $loaded = $false; $persisted = $false
    if ($exists) {
        try {
            $xml = New-Object System.Xml.XmlDocument
            $xml.XmlResolver = $null
            $xml.Load($ProjectFile)
            $valid = $xml.DocumentElement.LocalName -eq 'Project'
        } catch { }
    }
    if ($sameSolution) {
        foreach ($project in @(Get-TcHmiProjectObjects $Dte)) {
            $candidate = [string]$project.TcHmiResolvedFullName
            if ($candidate -and [IO.Path]::GetFullPath($candidate).Equals($ProjectFile, [StringComparison]::OrdinalIgnoreCase)) {
                $loaded = $true; break
            }
        }
        $text = [IO.File]::ReadAllText($SolutionFile)
        foreach ($match in [regex]::Matches($text, '(?im)^Project\("[^"]+"\)\s*=\s*"[^"]+",\s*"([^"]+)",')) {
            $candidate = [IO.Path]::GetFullPath((Join-Path ([IO.Path]::GetDirectoryName($SolutionFile)) $match.Groups[1].Value))
            if ($candidate.Equals($ProjectFile, [StringComparison]::OrdinalIgnoreCase)) { $persisted = $true; break }
        }
    }
    [pscustomobject]@{ same_solution=$sameSolution; file_exists=$exists; file_valid=$valid;
        loaded_in_xae=$loaded; persisted_in_solution=$persisted }
}

function New-TcHmiProject {
    param(
        [Parameter(Mandatory)]$Dte,
        [Parameter(Mandatory)][string]$Name,
        [string]$OutputDirectory = '',
        [string]$Template = '',
        [bool]$Apply = $false,
        [int]$WaitSeconds = 45,
        [int]$SaveAttempts = 20
    )
    if ($Name -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') { throw "Invalid HMI project name: $Name" }
    $solutionFile = ''; try { $solutionFile = [string]$Dte.Solution.FullName } catch { }
    if (-not $solutionFile -or -not [IO.File]::Exists($solutionFile)) { throw 'Save/open a solution before creating an HMI project.' }
    if (-not $Template) {
        $Template = 'C:\Program Files (x86)\Beckhoff\TwinCAT\Functions\TE2000-HMI-Engineering\Templates\ProjectTemplates\TcXaeShell\StarterPrj_PackagesConfig\StarterPrj.vstemplate'
    }
    $templatePath = [IO.Path]::GetFullPath($Template)
    if (-not [IO.File]::Exists($templatePath) -or -not [IO.Path]::GetExtension($templatePath).Equals('.vstemplate', [StringComparison]::OrdinalIgnoreCase)) {
        throw "TwinCAT HMI project template not found: $templatePath"
    }
    $destination = if ($OutputDirectory) { [IO.Path]::GetFullPath($OutputDirectory) } else {
        [IO.Path]::GetFullPath((Join-Path ([IO.Path]::GetDirectoryName($solutionFile)) $Name))
    }
    $projectFile = Join-Path $destination ($Name + '.hmiproj')
    $existing = [IO.Directory]::Exists($destination) -or [IO.File]::Exists($projectFile)
    $state = Get-TcHmiCreationState $Dte $solutionFile $projectFile
    if (-not $state.same_solution) { throw 'XAE solution changed before HMI creation.' }
    $existing = $existing -or $state.loaded_in_xae -or $state.persisted_in_solution
    if ($existing -and -not ($state.file_valid -and $state.loaded_in_xae)) {
        return [pscustomobject]@{ status='incomplete'; ok=$false; phase='inspect_existing';
            error="Destination exists but is not a valid, exactly matched loaded HMI project: $destination";
            project_file=$projectFile; state=$state; files_preserved=$true; retry_safe=$false;
            next_action='Inspect the existing files and XAE load state; do not delete or recreate blindly.' }
    }
    if (-not $Apply) {
        return [pscustomobject]@{ status='preview'; name=$Name; template=$templatePath; destination=$destination;
            project_file=$projectFile; apply_required=$true; reuse_existing=$existing; state=$state }
    }
    $diagnostics = New-Object System.Collections.ArrayList
    $creationFailed = $false; $creationBusy = $false
    if (-not $existing) {
        try {
            # Non-idempotent: issue once only. An exception may follow side effects.
            [void]$Dte.Solution.AddFromTemplate($templatePath, $destination, $Name, $false)
        } catch {
            $creationFailed = $true
            $creationBusy = Test-TcHmiComBusy $_.Exception
            [void]$diagnostics.Add(@{ phase='add_from_template'; message=$_.Exception.Message; busy=$creationBusy })
        }
    }
    $deadline = [DateTime]::UtcNow.AddSeconds([Math]::Max(0, $WaitSeconds))
    do {
        $state = Get-TcHmiCreationState $Dte $solutionFile $projectFile
        if ($state.same_solution -eq $false) { throw 'XAE solution changed while creating HMI; files were preserved.' }
        # Fresh custom HMI projects may expose identity only after .sln saving.
        # Existing directories still require an exact live match before saving.
        if ($state.file_valid -and ($state.loaded_in_xae -or -not $existing)) { break }
        if ($creationFailed -and -not $creationBusy) { break }
        if ([DateTime]::UtcNow -ge $deadline) { break }
        Start-Sleep -Milliseconds 250
    } while ($true)
    if (-not ($state.file_valid -and ($state.loaded_in_xae -or -not $existing))) {
        return [pscustomobject]@{ status='incomplete'; ok=$false; phase='wait_for_project';
            error='HMI creation has not reached a valid, loaded project; output was preserved.';
            project_file=$projectFile; state=$state; diagnostics=@($diagnostics.ToArray());
            files_preserved=$true; retry_safe=$false; next_action='Inspect XAE wizard/load state before retrying; do not recreate or delete output.' }
    }
    for ($attempt = 1; $attempt -le [Math]::Max(1, $SaveAttempts); $attempt++) {
        if ($state.persisted_in_solution) { break }
        try {
            if (-not ([string]$Dte.Solution.FullName).Equals($solutionFile, [StringComparison]::OrdinalIgnoreCase)) {
                throw 'XAE solution changed before saving HMI registration.'
            }
            # Save the solution registration, not unrelated modified editors.
            [void]$Dte.Solution.SaveAs($solutionFile)
        } catch {
            $busy = Test-TcHmiComBusy $_.Exception
            [void]$diagnostics.Add(@{ phase='save_solution'; attempt=$attempt; message=$_.Exception.Message; busy=$busy })
            if (-not $busy) { break }
        }
        $state = Get-TcHmiCreationState $Dte $solutionFile $projectFile
        if ($state.persisted_in_solution) { break }
        if ($attempt -lt $SaveAttempts) { Start-Sleep -Milliseconds 500 }
    }
    $state = Get-TcHmiCreationState $Dte $solutionFile $projectFile
    if (-not ($state.same_solution -and $state.file_valid -and $state.loaded_in_xae -and $state.persisted_in_solution)) {
        return [pscustomobject]@{ status='incomplete'; ok=$false; phase='save_solution';
            error='HMI output exists, but loaded identity and solution registration are not both verified.';
            project_file=$projectFile; state=$state; diagnostics=@($diagnostics.ToArray());
            files_preserved=$true; retry_safe=($state.same_solution -and $state.file_valid -and $state.loaded_in_xae);
            next_action='If the exact project is loaded, resume this request after XAE is ready. Otherwise inspect XAE loading first; never delete output or rerun the wizard blindly.' }
    }
    [pscustomobject]@{ status= $(if ($existing) { 'recovered' } else { 'created' }); ok=$true;
        name=$Name; template=$templatePath; destination=$destination; project_file=$projectFile;
        verified_in_xae=$true; persisted_in_solution=$true; reused_existing=$existing;
        diagnostics=@($diagnostics.ToArray()) }
}

function Get-TcHmiProjectObjects {
    param([Parameter(Mandatory)]$Dte)
    $result = New-Object System.Collections.ArrayList
    $allProjects = New-Object System.Collections.ArrayList
    function Visit-TcHmiProject {
        param($Project)
        if ($null -eq $Project) { return }
        [void]$allProjects.Add($Project)
        $fullName = ''
        try { $fullName = [string]$Project.FullName } catch { }
        if ($fullName -and [IO.Path]::GetExtension($fullName).Equals('.hmiproj', [StringComparison]::OrdinalIgnoreCase)) {
            $Project | Add-Member -NotePropertyName TcHmiResolvedFullName -NotePropertyValue ([IO.Path]::GetFullPath($fullName)) -Force
            $Project | Add-Member -NotePropertyName TcHmiResolvedName -NotePropertyValue ([IO.Path]::GetFileNameWithoutExtension($fullName)) -Force
            $Project | Add-Member -NotePropertyName TcHmiResolvedUniqueName -NotePropertyValue ([string]$Project.UniqueName) -Force
            [void]$result.Add($Project)
        }
        $count = 0
        try { $count = [int]$Project.ProjectItems.Count } catch { }
        for ($i = 1; $i -le $count; $i++) {
            try {
                $sub = $Project.ProjectItems.Item($i).SubProject
                if ($null -ne $sub) { Visit-TcHmiProject $sub }
            } catch { }
        }
    }
    $count = 0
    try { $count = [int]$Dte.Solution.Projects.Count } catch { }
    for ($i = 1; $i -le $count; $i++) {
        try { Visit-TcHmiProject ($Dte.Solution.Projects.Item($i)) } catch { }
    }
    # The TwinCAT HMI custom project system on TcXaeShell 15 can expose an
    # EnvDTE.Project whose Name/FullName/UniqueName are all empty.  Resolve
    # those fields from the already-open .sln and pair entries by project
    # order; this still leaves creation/inclusion on the real COM Project.
    if ($result.Count -eq 0) {
        $solutionFile = ''; try { $solutionFile = [string]$Dte.Solution.FullName } catch { }
        if ($solutionFile -and [IO.File]::Exists($solutionFile)) {
            $solutionText = [IO.File]::ReadAllText($solutionFile, [Text.Encoding]::UTF8)
            # Parse every solution entry so its index stays aligned with the
            # DTE Projects collection.  Comparing HMI count with total project
            # count incorrectly rejects normal mixed HMI + TwinCAT solutions.
            $matches = [regex]::Matches($solutionText,
                '(?im)^Project\("[^"]+"\)\s*=\s*"([^"]+)",\s*"([^"]+)",\s*"[^"]+"')
            for ($i = 0; $i -lt $matches.Count; $i++) {
                $relative = $matches[$i].Groups[2].Value.Replace('/', '\')
                if (-not [IO.Path]::GetExtension($relative).Equals('.hmiproj', [StringComparison]::OrdinalIgnoreCase)) {
                    continue
                }
                $resolved = [IO.Path]::GetFullPath((Join-Path ([IO.Path]::GetDirectoryName($solutionFile)) $relative))
                if (-not [IO.File]::Exists($resolved)) { continue }
                $project = $null
                foreach ($candidate in @($allProjects.ToArray())) {
                    $candidateFull = ''; $candidateUnique = ''
                    try { $candidateFull = [string]$candidate.FullName } catch { }
                    try { $candidateUnique = [string]$candidate.UniqueName } catch { }
                    if (($candidateFull -and [IO.Path]::GetFullPath($candidateFull).Equals($resolved, [StringComparison]::OrdinalIgnoreCase)) -or
                        ($candidateUnique -and $candidateUnique.Replace('/', '\').Equals($relative, [StringComparison]::OrdinalIgnoreCase))) {
                        $project = $candidate; break
                    }
                }
                if ($null -eq $project -and $i -lt $allProjects.Count) {
                    # TcXaeShell's HMI project has empty identity fields.  The
                    # corresponding solution-order item is safe only when it
                    # is not a TwinCAT System Manager project.
                    $candidate = $allProjects[$i]; $isSystemProject = $false
                    try {
                        $candidate.Object.LookupTreeItem('TIID') | Out-Null
                        $isSystemProject = $true
                    } catch { }
                    if (-not $isSystemProject) { $project = $candidate }
                }
                if ($null -eq $project) {
                    # Once a custom HMI project has been unloaded, TcXaeShell
                    # can reorder the top-level collection and expose that
                    # project as the only anonymous, non-System entry.  Do not
                    # rely on solution-file index alignment in that state.
                    $anonymous = @($allProjects.ToArray() | Where-Object {
                        $candidate = $_; $candidateName = ''; $candidateFull = ''; $candidateUnique = ''
                        try { $candidateName = [string]$candidate.Name } catch { }
                        try { $candidateFull = [string]$candidate.FullName } catch { }
                        try { $candidateUnique = [string]$candidate.UniqueName } catch { }
                        $isSystem = $false
                        try { $candidate.Object.LookupTreeItem('TIID') | Out-Null; $isSystem = $true } catch { }
                        (-not $isSystem -and -not $candidateName -and -not $candidateFull -and -not $candidateUnique)
                    })
                    if ($anonymous.Count -eq 1) { $project = $anonymous[0] }
                }
                if ($null -eq $project) { continue }
                $project | Add-Member -NotePropertyName TcHmiResolvedFullName -NotePropertyValue $resolved -Force
                $project | Add-Member -NotePropertyName TcHmiResolvedName -NotePropertyValue $matches[$i].Groups[1].Value -Force
                $project | Add-Member -NotePropertyName TcHmiResolvedUniqueName -NotePropertyValue $relative -Force
                [void]$result.Add($project)
            }
        }
    }
    return $result.ToArray()
}

function Get-TcHmiResolvedFullName {
    param([Parameter(Mandatory)]$Project)
    $value = ''; try { $value = [string]$Project.TcHmiResolvedFullName } catch { }
    if (-not $value) { try { $value = [string]$Project.FullName } catch { } }
    return $value
}

function Get-TcHmiResolvedName {
    param([Parameter(Mandatory)]$Project)
    $value = ''; try { $value = [string]$Project.TcHmiResolvedName } catch { }
    if (-not $value) { try { $value = [string]$Project.Name } catch { } }
    if (-not $value) { $value = [IO.Path]::GetFileNameWithoutExtension((Get-TcHmiResolvedFullName $Project)) }
    return $value
}

function Get-TcHmiResolvedUniqueName {
    param([Parameter(Mandatory)]$Project)
    $value = ''; try { $value = [string]$Project.TcHmiResolvedUniqueName } catch { }
    if (-not $value) { try { $value = [string]$Project.UniqueName } catch { } }
    return $value
}

function Get-TcHmiProjectObject {
    param([Parameter(Mandatory)]$Dte, [string]$ProjectName = '')
    $projects = @(Get-TcHmiProjectObjects $Dte)
    # The custom HMI project system briefly exposes an empty Projects
    # collection while AddFromFile finishes asynchronously.  A short bounded
    # retry prevents the next independent Agent call from reporting a false
    # "no HMI project" immediately after a verified mutation/reload.
    for ($attempt = 0; $projects.Count -eq 0 -and $attempt -lt 10; $attempt++) {
        Start-Sleep -Milliseconds 200
        $projects = @(Get-TcHmiProjectObjects $Dte)
    }
    if ($ProjectName) {
        $selector = $ProjectName.Trim()
        $selectorFull = ''
        try { if ([IO.Path]::IsPathRooted($selector)) { $selectorFull = [IO.Path]::GetFullPath($selector) } } catch { }
        $projects = @($projects | Where-Object {
            $full = [IO.Path]::GetFullPath((Get-TcHmiResolvedFullName $_))
            $unique = Get-TcHmiResolvedUniqueName $_
            (Get-TcHmiResolvedName $_).Equals($selector, [StringComparison]::OrdinalIgnoreCase) -or
            ([IO.Path]::GetFileNameWithoutExtension($full)).Equals($selector, [StringComparison]::OrdinalIgnoreCase) -or
            ($unique -and $unique.Equals($selector, [StringComparison]::OrdinalIgnoreCase)) -or
            ($selectorFull -and $full.Equals($selectorFull, [StringComparison]::OrdinalIgnoreCase))
        })
    }
    if ($projects.Count -eq 0) {
        throw $(if ($ProjectName) { "TwinCAT HMI project '$ProjectName' was not found in the open solution." }
                else { 'No TwinCAT HMI .hmiproj project was found in the open solution.' })
    }
    if ($projects.Count -gt 1) {
        $names = @($projects | ForEach-Object { Get-TcHmiResolvedName $_ }) -join ', '
        throw "Multiple TwinCAT HMI projects are open ($names); specify project."
    }
    return $projects[0]
}

function Get-TcHmiProjectPath {
    param([Parameter(Mandatory)]$Project)
    $projectFile = [IO.Path]::GetFullPath((Get-TcHmiResolvedFullName $Project))
    if (-not [IO.File]::Exists($projectFile) -or
        -not [IO.Path]::GetExtension($projectFile).Equals('.hmiproj', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Invalid TwinCAT HMI project path: $projectFile"
    }
    return [IO.Path]::GetDirectoryName($projectFile)
}

function Resolve-TcHmiFilePath {
    param([Parameter(Mandatory)][string]$ProjectDirectory,
          [Parameter(Mandatory)][string]$RelativePath,
          [string[]]$AllowedExtensions = @(),
          [switch]$RequireRegistered)
    if ([string]::IsNullOrWhiteSpace($RelativePath)) { throw 'HMI relative file path is required.' }
    if ([IO.Path]::IsPathRooted($RelativePath)) { throw 'HMI file path must be relative to the project.' }
    $controlCharacters = @($RelativePath.ToCharArray() | Where-Object { [int]$_ -lt 32 })
    if ($RelativePath.IndexOfAny(@([char]'<',[char]'>',[char]'"',[char]'|',[char]'?',[char]':')) -ge 0 -or
        $controlCharacters.Count -gt 0) {
        throw 'HMI file path contains illegal characters.'
    }
    $relative = $RelativePath.Replace('\','/')
    $segments = @($relative.Split('/'))
    if (@($segments | Where-Object { [string]::IsNullOrWhiteSpace($_) -or $_ -in @('.','..') }).Count -gt 0) {
        throw 'HMI file path cannot contain empty or traversal segments.'
    }
    if (@($segments | Where-Object { $_.Trim() -cne $_ -or $_.EndsWith('.') }).Count -gt 0) {
        throw 'HMI file path contains an unsafe segment.'
    }
    $root = [IO.Path]::GetFullPath($ProjectDirectory).TrimEnd([IO.Path]::DirectorySeparatorChar)
    $full = [IO.Path]::GetFullPath((Join-Path $root $relative))
    if (-not $full.StartsWith($root + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "HMI file path escapes the project directory: $RelativePath"
    }
    if ($AllowedExtensions.Count -gt 0) {
        $ext = [IO.Path]::GetExtension($full).ToLowerInvariant()
        if ($AllowedExtensions -notcontains $ext) { throw "Unsupported HMI file type '$ext'." }
    }
    if ($RequireRegistered) {
        $projects = @(Get-ChildItem -LiteralPath $root -Filter '*.hmiproj' -File -ErrorAction SilentlyContinue)
        if ($projects.Count -ne 1) { throw "Cannot resolve an exact HMI project registration under '$root'." }
        [xml]$projectXml = [IO.File]::ReadAllText($projects[0].FullName, [Text.Encoding]::UTF8)
        $wanted = $relative.TrimEnd('/').ToLowerInvariant()
        $matches = @($projectXml.SelectNodes('//*[@Include]') | Where-Object {
            ([string]$_.GetAttribute('Include')).Replace('\','/').TrimEnd('/').ToLowerInvariant() -ceq $wanted
        })
        if ($matches.Count -eq 0) { throw "HMI item is not registered at exact path: $relative" }
        if ($matches.Count -gt 1) { throw "HMI item has duplicate exact registrations: $relative" }
    }
    return $full
}

function Get-TcHmiProjectInfo {
    param([Parameter(Mandatory)]$Dte, [string]$ProjectName = '')
    $project = Get-TcHmiProjectObject $Dte $ProjectName
    $projectFile = [IO.Path]::GetFullPath((Get-TcHmiResolvedFullName $project))
    $root = Get-TcHmiProjectPath $project
    [xml]$xml = Get-Content -LiteralPath $projectFile -Raw -Encoding UTF8
    function Read-HmiProperty([string]$Name) {
        $node = $xml.SelectSingleNode("//*[local-name()='$Name']")
        if ($null -ne $node) { return [string]$node.InnerText }
        return ''
    }
    $configPath = Join-Path $root 'Properties\tchmiconfig.json'
    $config = $null
    if ([IO.File]::Exists($configPath)) {
        try { $config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json }
        catch { throw "Invalid Properties/tchmiconfig.json: $($_.Exception.Message)" }
    }
    $items = @($xml.SelectNodes("//*[local-name()='Content']/@Include") | ForEach-Object { [string]$_.Value })
    [pscustomobject]@{
        status = 'ok'; name = Get-TcHmiResolvedName $project; project_file = $projectFile
        project_directory = $root; solution = [string]$Dte.Solution.FullName
        project_guid = Read-HmiProperty 'ProjectGuid'
        hmi_title = Read-HmiProperty 'HmiTitle'
        hmi_version = Read-HmiProperty 'HmiVersion'
        framework = Read-HmiProperty 'TargetFrameworkMoniker'
        framework_target = Read-HmiProperty 'TargetFramework'
        engineering_initial = Read-HmiProperty 'HmiInitial'
        engineering_recent = Read-HmiProperty 'HmiRecent'
        use_x64 = Read-HmiProperty 'HmiUseX64'
        communication_server_port = Read-HmiProperty 'HmiCommunicationServerPort'
        communication_router_port = Read-HmiProperty 'HmiCommunicationRouterPort'
        startup_view = if ($null -ne $config) { [string]$config.startupView } else { '' }
        active_theme = if ($null -ne $config) { [string]$config.activeTheme } else { '' }
        scale_mode = if ($null -ne $config) { [string]$config.scaleMode } else { '' }
        content_item_count = $items.Count
        views = @($items | Where-Object { [IO.Path]::GetExtension($_) -ieq '.view' })
        contents = @($items | Where-Object { [IO.Path]::GetExtension($_) -ieq '.content' })
        readonly = $true
    }
}

function Get-TcHmiStructure {
    param([Parameter(Mandatory)]$Dte, [string]$ProjectName = '')
    $project = Get-TcHmiProjectObject $Dte $ProjectName
    $root = Get-TcHmiProjectPath $project
    $files = New-Object System.Collections.ArrayList
    foreach ($file in Get-ChildItem -LiteralPath $root -Recurse -File) {
        $relative = $file.FullName.Substring($root.Length).TrimStart('\').Replace('\','/')
        if ($relative -match '^(\.TwinCATAgent|bin|obj|Packages|node_modules)/') { continue }
        $ext = $file.Extension.ToLowerInvariant()
        $role = switch ($ext) {
            '.view' { 'view' }; '.content' { 'content' }; '.usercontrol' { 'usercontrol' }
            '.function' { 'function' }; '.partial' { 'partial' }; '.localization' { 'localization' }
            '.theme' { 'theme' }; '.css' { 'style' }; '.js' { 'javascript' }; '.ts' { 'typescript' }
            '.json' { if ($relative -like 'Server/*') { 'server-config' } else { 'json' } }
            '.hmiproj' { 'project' }; default { 'asset' }
        }
        [void]$files.Add([pscustomobject]@{ path=$relative; role=$role; size=[long]$file.Length })
    }
    [pscustomobject]@{
        status='ok'; project=Get-TcHmiResolvedName $project; project_directory=$root
        file_count=$files.Count; files=@($files.ToArray()); readonly=$true
    }
}

function Read-TcHmiFile {
    param([Parameter(Mandatory)]$Dte, [string]$ProjectName = '',
          [Parameter(Mandatory)][string]$File, [int]$MaxChars = 200000,
          [string]$ControlId = '', [bool]$IncludeContent = $true,
          [int]$MaxControls = 200, [int]$ControlOffset = 0, [int]$ContentOffset = 0)
    $project = Get-TcHmiProjectObject $Dte $ProjectName
    $root = Get-TcHmiProjectPath $project
    $path = Resolve-TcHmiFilePath $root $File @('.view','.content','.usercontrol','.partial','.function','.js','.ts','.css','.json','.localization','.theme') -RequireRegistered
    if (-not [IO.File]::Exists($path)) { throw "HMI file not found: $File" }
    $sourceBefore = Get-Item -LiteralPath $path
    $sourceSize = $sourceBefore.Length; $sourceTicks = $sourceBefore.LastWriteTimeUtc.Ticks
    $fullText = [IO.File]::ReadAllText($path, [Text.Encoding]::UTF8)
    $sourceAfter = Get-Item -LiteralPath $path
    $sourceConsistent = ($sourceSize -eq $sourceAfter.Length -and $sourceTicks -eq $sourceAfter.LastWriteTimeUtc.Ticks)
    $ControlOffset = [Math]::Max(0, $ControlOffset)
    $ContentOffset = [Math]::Max(0, [Math]::Min($ContentOffset, $fullText.Length))
    # Offsets are UTF-16 code units, matching the XAE/.NET string contract.
    if ($ContentOffset -gt 0 -and $ContentOffset -lt $fullText.Length -and [char]::IsLowSurrogate($fullText[$ContentOffset])) { $ContentOffset-- }
    $text = ''
    $truncated = $false
    if ($IncludeContent) {
        $text = $fullText.Substring($ContentOffset)
        if ($MaxChars -gt 0 -and $text.Length -gt $MaxChars) {
            $take = $MaxChars
            if ($take -gt 0 -and [char]::IsHighSurrogate($text[$take - 1])) { $take = [Math]::Max(2, $take - 1) }
            $text = $text.Substring(0, $take)
            $truncated = $true
        }
    }
    $MaxControls = [Math]::Max(1, [Math]::Min($MaxControls, 5000))
    $maxBindings = [Math]::Max(4, [Math]::Min($MaxControls * 4, 10000))
    $controls = New-Object System.Collections.ArrayList
    $bindings = New-Object System.Collections.ArrayList
    $controlCount = 0
    $bindingCount = 0
    if ([IO.Path]::GetExtension($path) -in @('.view','.content','.usercontrol')) {
        try {
            [xml]$markup = $fullText
            $allNodes = @($markup.SelectNodes("//*[@data-tchmi-type]"))
            $controlCount = $allNodes.Count
            if ($ControlId) {
                $selectedNodes = @($allNodes | Where-Object { [string]$_.id -ceq $ControlId })
                if ($selectedNodes.Count -eq 0) {
                    $candidates = @($allNodes | Select-Object -First 20 | ForEach-Object { [string]$_.id })
                    throw "HMI control '$ControlId' not found in '$File'. First control IDs: $($candidates -join ', ')"
                }
            } else {
                $selectedNodes = @($allNodes | Select-Object -Skip $ControlOffset -First $MaxControls)
            }
            $selectedIds = @{}
            foreach ($node in $selectedNodes) { $selectedIds[[string]$node.id] = $true }
            foreach ($node in $allNodes) {
                $attrs = [ordered]@{}
                foreach ($attribute in @($node.Attributes)) {
                    if ($attribute.Name.StartsWith('data-tchmi-', [StringComparison]::OrdinalIgnoreCase)) {
                        if ($selectedIds.ContainsKey([string]$node.id)) {
                            $attrs[$attribute.Name] = [string]$attribute.Value
                        }
                        if ([string]$attribute.Value -match '%[a-zA-Z]%.*%/[a-zA-Z]%') {
                            if (-not $ControlId -or $selectedIds.ContainsKey([string]$node.id)) {
                                $bindingCount++
                                if ($selectedIds.ContainsKey([string]$node.id) -and $bindings.Count -lt $maxBindings) {
                                    [void]$bindings.Add([pscustomobject]@{ control=[string]$node.id; attribute=$attribute.Name; expression=[string]$attribute.Value })
                                }
                            }
                        }
                    }
                }
                if ($selectedIds.ContainsKey([string]$node.id)) {
                    [void]$controls.Add([pscustomobject]@{ id=[string]$node.id; type=[string]$node.'data-tchmi-type'; attributes=[pscustomobject]$attrs })
                }
            }
        } catch { throw "Invalid HMI markup '$File': $($_.Exception.Message)" }
    }
    $result = [ordered]@{
        status='read'; project=Get-TcHmiResolvedName $project; file=$File.Replace('\','/'); full_path=$path
        control_id=$ControlId; content_included=$IncludeContent; content_chars=$(if ($IncludeContent) { $text.Length } else { 0 })
        source='saved_file'; live_xae=$false; dirty_unknown=$true; source_consistent=$sourceConsistent
        source_size=$sourceSize; source_mtime_ticks=$sourceTicks
        content_offset=$ContentOffset; total_content_chars=$fullText.Length
        next_content_offset=$(if ($IncludeContent -and $truncated) { $ContentOffset + $text.Length } else { $null })
        control_offset=$(if ($ControlId) { 0 } else { $ControlOffset })
        next_control_offset=$(if (-not $ControlId -and ($ControlOffset + $controls.Count) -lt $controlCount) { $ControlOffset + $controls.Count } else { $null })
        truncated=$truncated; controls=@($controls.ToArray()); total_control_count=$controlCount
        control_count=$(if ($ControlId) { $selectedNodes.Count } else { $controlCount })
        returned_control_count=$controls.Count; controls_truncated=(-not $ControlId -and $controlCount -gt ($ControlOffset + $controls.Count))
        bindings=@($bindings.ToArray()); binding_count=$bindingCount
        returned_binding_count=$bindings.Count; bindings_truncated=($bindingCount -gt $bindings.Count)
        readonly=$true
    }
    if ($IncludeContent) { $result['content'] = $text }
    [pscustomobject]$result
}

function Get-TcHmiAdsInfo {
    param([Parameter(Mandatory)]$Dte, [string]$ProjectName = '')
    $project = Get-TcHmiProjectObject $Dte $ProjectName
    $root = Get-TcHmiProjectPath $project
    $result = New-Object System.Collections.ArrayList
    foreach ($scope in @('default','remote')) {
        $relative = "Server\ADS\ADS.Config.$scope.json"
        $path = Join-Path $root $relative
        if (-not [IO.File]::Exists($path)) { continue }
        try { $config = [IO.File]::ReadAllText($path, [Text.Encoding]::UTF8) | ConvertFrom-Json }
        catch { throw "Invalid $relative`: $($_.Exception.Message)" }
        foreach ($property in @($config.RUNTIMES.PSObject.Properties)) {
            $runtime = $property.Value
            [void]$result.Add([pscustomobject]@{
                scope=$scope; name=[string]$property.Name; enabled=[bool]$runtime.ENABLED
                netid=[string]$runtime.NETID; port=[int]$runtime.PORT
                read_only=[bool]$runtime.READ_ONLY; use_whitelisting=[bool]$runtime.USE_WHITELISTING
                mapped_symbol_count=@($runtime.SYMBOLS.PSObject.Properties).Count
            })
        }
    }
    [pscustomobject]@{
        status='ok'; project=Get-TcHmiResolvedName $project; runtimes=@($result.ToArray())
        note='Configuration readback only; no ADS connection or PLC symbol availability was tested.'
        readonly=$true
    }
}

function Test-TcHmiProject {
    param([Parameter(Mandatory)]$Dte, [string]$ProjectName = '')
    $project = Get-TcHmiProjectObject $Dte $ProjectName
    $root = Get-TcHmiProjectPath $project
    $projectFile = [IO.Path]::GetFullPath((Get-TcHmiResolvedFullName $project))
    $findings = New-Object System.Collections.ArrayList
    function Add-HmiFinding([string]$Severity,[string]$Code,[string]$File,[string]$Message) {
        [void]$findings.Add([pscustomobject]@{ severity=$Severity; code=$Code; file=$File; message=$Message })
    }
    $projectXml = $null
    try { [xml]$projectXml = [IO.File]::ReadAllText($projectFile, [Text.Encoding]::UTF8) }
    catch { Add-HmiFinding 'error' 'invalid-hmiproj' ([IO.Path]::GetFileName($projectFile)) $_.Exception.Message }
    $config = $null; $configPath = Join-Path $root 'Properties\tchmiconfig.json'
    if (-not [IO.File]::Exists($configPath)) { Add-HmiFinding 'error' 'missing-config' 'Properties/tchmiconfig.json' 'Required HMI framework configuration is missing.' }
    else { try { $config = [IO.File]::ReadAllText($configPath, [Text.Encoding]::UTF8) | ConvertFrom-Json }
           catch { Add-HmiFinding 'error' 'invalid-config-json' 'Properties/tchmiconfig.json' $_.Exception.Message } }
    if ($null -ne $config) {
        if (-not [string]$config.startupView) { Add-HmiFinding 'error' 'missing-startup-view' 'Properties/tchmiconfig.json' 'startupView is empty.' }
        else {
            $startup = Resolve-TcHmiFilePath $root ([string]$config.startupView) @('.view') -RequireRegistered
            if (-not [IO.File]::Exists($startup)) { Add-HmiFinding 'error' 'startup-view-not-found' ([string]$config.startupView) 'Configured startup view does not exist.' }
        }
        $themeNames = @($config.themes.PSObject.Properties.Name)
        if ($themeNames -cnotcontains [string]$config.activeTheme) {
            Add-HmiFinding 'error' 'active-theme-not-found' 'Properties/tchmiconfig.json' "Active theme '$([string]$config.activeTheme)' is not registered."
        }
        foreach ($theme in @($config.themes.PSObject.Properties)) {
            foreach ($resource in @($theme.Value.resources)) {
                try { $themePath = Resolve-TcHmiFilePath $root ([string]$resource.name) -RequireRegistered }
                catch { Add-HmiFinding 'error' 'invalid-theme-resource-path' 'Properties/tchmiconfig.json' $_.Exception.Message; continue }
                if (-not [IO.File]::Exists($themePath)) {
                    Add-HmiFinding 'error' 'theme-resource-not-found' ([string]$resource.name) "Theme '$($theme.Name)' resource does not exist."
                }
            }
        }
        foreach ($language in @($config.languages.PSObject.Properties)) {
            foreach ($relativeValue in @($language.Value)) {
                $relative = [string]$relativeValue
                try { $languagePath = Resolve-TcHmiFilePath $root $relative @('.localization') -RequireRegistered }
                catch { Add-HmiFinding 'error' 'invalid-localization-path' 'Properties/tchmiconfig.json' $_.Exception.Message; continue }
                if (-not [IO.File]::Exists($languagePath)) {
                    Add-HmiFinding 'error' 'localization-file-not-found' $relative "Registered locale '$($language.Name)' file does not exist."
                    continue
                }
                try {
                    $languageDocument = [IO.File]::ReadAllText($languagePath, [Text.Encoding]::UTF8) | ConvertFrom-Json
                    if ([string]$languageDocument.locale -cne [string]$language.Name) {
                        Add-HmiFinding 'warning' 'localization-locale-mismatch' $relative "File locale '$([string]$languageDocument.locale)' differs from registered locale '$($language.Name)'."
                    }
                } catch { Add-HmiFinding 'error' 'invalid-localization-json' $relative $_.Exception.Message }
            }
        }
        foreach ($resource in @($config.symbols.themedResources.PSObject.Properties)) {
            foreach ($themeName in $themeNames) {
                if ($resource.Value.values.PSObject.Properties.Name -cnotcontains $themeName) {
                    Add-HmiFinding 'warning' 'themed-resource-value-missing' 'Properties/tchmiconfig.json' "Themed resource '$($resource.Name)' has no value for theme '$themeName'."
                }
            }
        }
        $userControlUrls = @($config.userControls | ForEach-Object { [string]$_.url })
        foreach ($duplicate in @($userControlUrls | Group-Object | Where-Object { $_.Count -gt 1 })) {
            Add-HmiFinding 'error' 'duplicate-usercontrol-registration' 'Properties/tchmiconfig.json' "UserControl '$($duplicate.Name)' is registered $($duplicate.Count) times."
        }
        $projectContentNodes = if ($null -ne $projectXml) { @($projectXml.SelectNodes("//*[local-name()='Content']")) } else { @() }
        foreach ($relativeValue in $userControlUrls) {
            $relative = $relativeValue.Replace('\','/')
            try { $userControlPath = Resolve-TcHmiFilePath $root $relative @('.usercontrol') -RequireRegistered }
            catch { Add-HmiFinding 'error' 'invalid-usercontrol-path' 'Properties/tchmiconfig.json' $_.Exception.Message; continue }
            if (-not [IO.File]::Exists($userControlPath)) {
                Add-HmiFinding 'error' 'usercontrol-file-not-found' $relative 'Registered UserControl markup does not exist.'
            }
            $parameterRelative = "$relative.json"
            $parameterPath = Resolve-TcHmiFilePath $root $parameterRelative @('.json') -RequireRegistered
            if (-not [IO.File]::Exists($parameterPath)) {
                Add-HmiFinding 'error' 'usercontrol-parameter-file-not-found' $parameterRelative 'The dependent UserControl parameter document does not exist.'
            } else {
                try {
                    $parameterDocument = [IO.File]::ReadAllText($parameterPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
                    if (-not ([string]$parameterDocument.'$schema').EndsWith('UserControlConfig.Schema.json', [StringComparison]::OrdinalIgnoreCase)) {
                        Add-HmiFinding 'warning' 'usercontrol-schema-unexpected' $parameterRelative 'The parameter document does not reference UserControlConfig.Schema.json.'
                    }
                    $parameterNames = @($parameterDocument.parameters | ForEach-Object { [string]$_.name })
                    foreach ($duplicateParameter in @($parameterNames | Group-Object | Where-Object { $_.Count -gt 1 })) {
                        Add-HmiFinding 'error' 'duplicate-usercontrol-parameter' $parameterRelative "Parameter '$($duplicateParameter.Name)' occurs $($duplicateParameter.Count) times."
                    }
                    foreach ($parameter in @($parameterDocument.parameters)) {
                        try { Test-TcHmiUserControlParameter $parameter }
                        catch { Add-HmiFinding 'error' 'invalid-usercontrol-parameter' $parameterRelative $_.Exception.Message }
                    }
                } catch { Add-HmiFinding 'error' 'invalid-usercontrol-parameter-json' $parameterRelative $_.Exception.Message }
            }
            if ($null -ne $projectXml) {
                $mainNodes = @($projectContentNodes | Where-Object { ([string]$_.Include).Replace('\','/') -ieq $relative })
                $parameterNodes = @($projectContentNodes | Where-Object { ([string]$_.Include).Replace('\','/') -ieq $parameterRelative })
                if ($mainNodes.Count -ne 1) {
                    Add-HmiFinding 'error' 'usercontrol-project-registration' ([IO.Path]::GetFileName($projectFile)) "UserControl '$relative' has $($mainNodes.Count) project registrations; expected one."
                }
                if ($parameterNodes.Count -ne 1) {
                    Add-HmiFinding 'error' 'usercontrol-parameter-registration' ([IO.Path]::GetFileName($projectFile)) "Parameter file '$parameterRelative' has $($parameterNodes.Count) project registrations; expected one."
                } elseif (([string]$parameterNodes[0].DependentUpon).Replace('\','/') -ine $relative) {
                    Add-HmiFinding 'error' 'usercontrol-dependent-upon' ([IO.Path]::GetFileName($projectFile)) "Parameter file '$parameterRelative' must depend on '$relative'."
                }
            }
        }
    }
    $markupFiles = @(Get-ChildItem -LiteralPath $root -Recurse -File | Where-Object {
        if ($_.Extension -notin @('.view','.content','.usercontrol')) { return $false }
        $relative = $_.FullName.Substring($root.Length).TrimStart('\').Replace('\','/')
        return ($relative -notmatch '^(\.TwinCATAgent|bin|obj|Packages|node_modules)/')
    })
    foreach ($item in $markupFiles) {
        $relative = $item.FullName.Substring($root.Length).TrimStart('\').Replace('\','/')
        try {
            [xml]$markup = [IO.File]::ReadAllText($item.FullName, [Text.Encoding]::UTF8)
            $nodes = @($markup.SelectNodes("//*[@data-tchmi-type]"))
            if ($nodes.Count -eq 0) { Add-HmiFinding 'error' 'missing-root-control' $relative 'No data-tchmi-type control was found.'; continue }
            $ids = @($nodes | ForEach-Object { [string]$_.id } | Where-Object { $_ })
            foreach ($duplicate in @($ids | Group-Object | Where-Object { $_.Count -gt 1 })) {
                Add-HmiFinding 'error' 'duplicate-control-id' $relative "Control id '$($duplicate.Name)' occurs $($duplicate.Count) times."
            }
            foreach ($node in $nodes) {
                if (-not [string]$node.id) { Add-HmiFinding 'error' 'missing-control-id' $relative "Control '$([string]$node.'data-tchmi-type')' has no id." }
                if (-not ([string]$node.'data-tchmi-type').StartsWith('TcHmi.Controls.', [StringComparison]::Ordinal)) {
                    Add-HmiFinding 'warning' 'unusual-control-type' $relative "Control '$([string]$node.id)' has type '$([string]$node.'data-tchmi-type')'."
                }
            }
        } catch { Add-HmiFinding 'error' 'invalid-markup' $relative $_.Exception.Message }
    }
    foreach ($scope in @('default','remote')) {
        $relative = "Server/ADS/ADS.Config.$scope.json"; $path = Join-Path $root $relative.Replace('/','\')
        if (-not [IO.File]::Exists($path)) { Add-HmiFinding 'warning' 'missing-ads-config' $relative 'ADS configuration file is missing.'; continue }
        try {
            $ads = [IO.File]::ReadAllText($path, [Text.Encoding]::UTF8) | ConvertFrom-Json
            foreach ($property in @($ads.RUNTIMES.PSObject.Properties)) {
                $runtime = $property.Value
                if ([int]$runtime.PORT -lt 1 -or [int]$runtime.PORT -gt 65535) { Add-HmiFinding 'error' 'invalid-ads-port' $relative "Runtime '$($property.Name)' has invalid port." }
                if (-not ([string]$runtime.NETID -match '^\d{1,3}(\.\d{1,3}){5}$')) { Add-HmiFinding 'error' 'invalid-ams-netid' $relative "Runtime '$($property.Name)' has invalid NETID." }
            }
        } catch { Add-HmiFinding 'error' 'invalid-ads-json' $relative $_.Exception.Message }
    }
    $errors = @($findings | Where-Object { $_.severity -eq 'error' })
    $warnings = @($findings | Where-Object { $_.severity -eq 'warning' })
    [pscustomobject]@{
        status='validated'; project=Get-TcHmiResolvedName $project; valid=($errors.Count -eq 0)
        error_count=$errors.Count; warning_count=$warnings.Count; findings=@($findings.ToArray())
        checked_markup_files=$markupFiles.Count; readonly=$true
        note='Structural validation only; browser runtime behavior and live ADS symbol access are not proven.'
    }
}

function Convert-TcHmiHtmlCompatibleMarkup {
    param([Parameter(Mandatory)][string]$Markup)
    # HMI markup is parsed as HTML in the browser.  XML's compact <div />
    # serialization is therefore unsafe: HTML treats the following sibling
    # controls as children of that div and only the first control is created.
    return [regex]::Replace(
        $Markup,
        '<div(?<attributes>\s[^<>]*?)\s*/>',
        '<div${attributes}></div>',
        [System.Text.RegularExpressions.RegexOptions]::IgnoreCase
    )
}

function Get-TcHmiContractHash {
    param([AllowEmptyString()][string]$Text)
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Text.Replace("`r`n","`n"))))).Replace('-','').ToLowerInvariant() }
    finally { $sha.Dispose() }
}

function Assert-TcHmiWriteContract {
    param([string]$ProjectFile, [string]$Markup, [bool]$Apply)
    if (-not $Apply) { return }
    $gate = $script:HmiWriteGate
    if ($null -eq $gate -or -not $gate.candidate_hash -or -not @($gate.files).Count) {
        throw 'HMI write contract missing. Use the updated Agent/CLI/MCP write entry; raw writes are blocked.'
    }
    if ([IO.Path]::GetFullPath($ProjectFile) -ine [string]$gate.project_file) {
        throw 'HMI project changed after contract validation; retry.'
    }
    $hash = Get-TcHmiContractHash $Markup
    if ($hash -cne [string]$gate.candidate_hash) { throw 'HMI candidate changed after contract validation; retry.' }
    if ($gate.source_file) {
        $source = [IO.File]::ReadAllText([string]$gate.source_file, [Text.Encoding]::UTF8)
        if ((Get-TcHmiContractHash $source) -cne [string]$gate.source_hash) {
            throw 'HMI source changed after preview; retry without overwriting the new content.'
        }
    }
    foreach ($entry in @($gate.files)) {
        $file = Get-Item -LiteralPath ([string]$entry.path) -ErrorAction Stop
        if ($file.Length -ne [long]$entry.size -or $file.LastWriteTimeUtc.Ticks -ne [long]$entry.ticks) {
            throw "HMI installed contract changed after validation: $($entry.path)"
        }
    }
}

function New-TcHmiView {
    param([Parameter(Mandatory)]$Dte, [string]$ProjectName = '',
          [Parameter(Mandatory)][string]$Name,
          [ValidateSet('view','content')][string]$Kind = 'view',
          [object[]]$Controls = @(), [bool]$Apply = $false)
    $project = Get-TcHmiProjectObject $Dte $ProjectName
    $root = Get-TcHmiProjectPath $project
    $baseName = [IO.Path]::GetFileNameWithoutExtension($Name)
    if (-not ($baseName -match '^[A-Za-z_][A-Za-z0-9_]*$') -or $Name.IndexOfAny(@([char]'\',[char]'/')) -ge 0) {
        throw 'HMI view name must be a single identifier using letters, digits and underscore.'
    }
    $extension = if ($Kind -eq 'view') { '.view' } else { '.content' }
    $relative = $baseName + $extension
    $path = Resolve-TcHmiFilePath $root $relative @($extension)
    if ([IO.File]::Exists($path)) { throw "HMI file already exists: $relative" }
    $rootType = if ($Kind -eq 'view') { 'TcHmi.Controls.System.TcHmiView' } else { 'TcHmi.Controls.System.TcHmiContent' }
    $document = New-Object System.Xml.XmlDocument
    $rootNode = $document.CreateElement('div')
    $rootNode.SetAttribute('id', $baseName)
    $rootNode.SetAttribute('data-tchmi-type', $rootType)
    foreach ($pair in @{'data-tchmi-top'='0';'data-tchmi-left'='0';'data-tchmi-width-mode'='Content';'data-tchmi-min-width'='100';'data-tchmi-min-width-unit'='%';'data-tchmi-height-mode'='Content';'data-tchmi-min-height'='100';'data-tchmi-min-height-unit'='%'}.GetEnumerator()) {
        $rootNode.SetAttribute([string]$pair.Key, [string]$pair.Value)
    }
    [void]$document.AppendChild($rootNode)
    $ids = @($baseName)
    foreach ($control in @($Controls)) {
        $id = [string]$control.id; $type = [string]$control.type
        if (-not ($id -match '^[A-Za-z_][A-Za-z0-9_]*$')) { throw "Invalid HMI control id '$id'." }
        if ($ids -contains $id) { throw "Duplicate HMI control id '$id'." }
        if (-not $type.StartsWith('TcHmi.Controls.', [StringComparison]::Ordinal)) { throw "Invalid HMI control type '$type'." }
        $ids += $id
        $node = $document.CreateElement('div'); $node.SetAttribute('id',$id); $node.SetAttribute('data-tchmi-type',$type)
        if ($null -ne $control.attributes) {
            foreach ($attribute in @($control.attributes.PSObject.Properties)) {
                $attributeName = [string]$attribute.Name
                if (-not $attributeName.StartsWith('data-tchmi-', [StringComparison]::OrdinalIgnoreCase)) {
                    throw "Only data-tchmi-* control attributes are allowed; got '$attributeName'."
                }
                $node.SetAttribute($attributeName, [string]$attribute.Value)
            }
        }
        [void]$rootNode.AppendChild($node)
    }
    $settings = New-Object System.Xml.XmlWriterSettings
    $settings.Indent = $true; $settings.OmitXmlDeclaration = $true
    $settings.Encoding = New-Object System.Text.UTF8Encoding($false)
    $builder = New-Object System.Text.StringBuilder
    $writer = [System.Xml.XmlWriter]::Create($builder, $settings)
    $document.Save($writer); $writer.Dispose()
    $markup = Convert-TcHmiHtmlCompatibleMarkup $builder.ToString()
    Assert-TcHmiWriteContract (Get-TcHmiResolvedFullName $project) $markup $Apply
    if (-not $Apply) {
        return [pscustomobject]@{ status='preview'; project=Get-TcHmiResolvedName $project; file=$relative
            project_file=Get-TcHmiResolvedFullName $project
            full_path=$path; kind=$Kind; control_count=@($Controls).Count; markup=$markup
            would_write_file=$true; would_add_to_project=$true; apply_required=$true }
    }
    try { $Dte.ExecuteCommand('File.SaveAll') } catch { }
    $projectFile = Get-TcHmiResolvedFullName $project
    $originalProjectText = [IO.File]::ReadAllText($projectFile, [Text.Encoding]::UTF8)
    [IO.File]::WriteAllText($path, $markup, (New-Object System.Text.UTF8Encoding($false)))
    $added = $null; $reloadMode = 'project-items'
    try {
        $projectItems = $null; try { $projectItems = $project.ProjectItems } catch { }
        if ($null -ne $projectItems) {
            $added = $projectItems.AddFromFile($path)
            $project.Save()
        } else {
            # TcXaeShell 15's HMI custom project system often exposes no
            # ProjectItems collection.  Persist the generated artifact in
            # the MSBuild item list, then make XAE consume it by reloading the
            # project through the standard DTE Solution API.
            $closing = $originalProjectText.LastIndexOf('</ItemGroup>', [StringComparison]::OrdinalIgnoreCase)
            if ($closing -lt 0) { throw 'The HMI project has no ItemGroup insertion point.' }
            $entry = "    <Content Include=`"$relative`">`r`n      <SubType>Content</SubType>`r`n      <Visible>true</Visible>`r`n    </Content>`r`n  "
            $updatedProjectText = $originalProjectText.Insert($closing, $entry)
            [IO.File]::WriteAllText($projectFile, $updatedProjectText, (New-Object System.Text.UTF8Encoding($true)))
            $Dte.Solution.Remove($project)
            $added = $Dte.Solution.AddFromFile($projectFile, $false)
            $reloadMode = 'hmiproj-update-and-com-reload'
        }
        try { $Dte.ExecuteCommand('File.SaveAll') } catch { }
    } catch {
        try { [IO.File]::WriteAllText($projectFile, $originalProjectText, (New-Object System.Text.UTF8Encoding($true))) } catch { }
        try { if ([IO.File]::Exists($path)) { [IO.File]::Delete($path) } } catch { }
        try {
            $stillOpen = @(Get-TcHmiProjectObjects $Dte).Count -gt 0
            if (-not $stillOpen) { $Dte.Solution.AddFromFile($projectFile, $false) | Out-Null }
        } catch { }
        throw "HMI file generation/load failed and files were rolled back: $($_.Exception.Message)"
    }
    Start-Sleep -Milliseconds 300
    [xml]$readback = [IO.File]::ReadAllText((Get-TcHmiResolvedFullName $project), [Text.Encoding]::UTF8)
    $included = @($readback.SelectNodes("//*[local-name()='Content']/@Include") | ForEach-Object { [string]$_.Value }) -contains $relative
    if (-not $included) { throw "HMI view '$relative' exists but .hmiproj readback did not contain it." }
    $itemName=''; try { $itemName=[string]$added.Name } catch { }
    [pscustomobject]@{
        status='created'; project=Get-TcHmiResolvedName $project; file=$relative; full_path=$path
        kind=$Kind; control_count=@($Controls).Count; project_item=$itemName
        file_exists=[IO.File]::Exists($path); project_includes_file=$included
        com_item_created=[bool]($null -ne $added); load_mode=$reloadMode
        verified=([IO.File]::Exists($path) -and $included -and $null -ne $added)
    }
}

function Convert-TcHmiXmlText {
    param([Parameter(Mandatory)][xml]$Document)
    $settings = New-Object System.Xml.XmlWriterSettings
    $settings.Indent = $true; $settings.OmitXmlDeclaration = $true
    $settings.Encoding = New-Object System.Text.UTF8Encoding($false)
    $builder = New-Object System.Text.StringBuilder
    $writer = [System.Xml.XmlWriter]::Create($builder, $settings)
    try { $Document.Save($writer) } finally { $writer.Dispose() }
    return (Convert-TcHmiHtmlCompatibleMarkup $builder.ToString())
}

function Reload-TcHmiProject {
    param([Parameter(Mandatory)]$Dte, [Parameter(Mandatory)]$Project,
          [Parameter(Mandatory)][string]$ProjectFile, [bool]$SaveBeforeRemove = $false)
    if ($SaveBeforeRemove) { try { $Dte.ExecuteCommand('File.SaveAll') } catch { } }
    $Dte.Solution.Remove($Project)
    # Persist removal before re-adding.  Without this step the custom TE2000
    # project system may leave an anonymous unloaded placeholder in mixed
    # solutions; AddFromFile then returns null because the .sln still contains
    # the old entry.
    try { $Dte.ExecuteCommand('File.SaveAll') } catch { }
    Start-Sleep -Milliseconds 200
    $loaded = $null
    for ($attempt = 0; $null -eq $loaded -and $attempt -lt 3; $attempt++) {
        try { $loaded = $Dte.Solution.AddFromFile($ProjectFile, $false) } catch { }
        if ($null -eq $loaded) { Start-Sleep -Milliseconds 300 }
    }
    if ($null -eq $loaded) { throw "XAE did not reload HMI project '$ProjectFile'." }
    try { $Dte.ExecuteCommand('File.SaveAll') } catch { }
    return ,$loaded
}

function Get-TcHmiEditorTextReadback {
    param([Parameter(Mandatory)]$Document)
    # Saving/encoding conversion may briefly invalidate the editor buffer.
    # Retry only reads; never repeat ReplaceText or Save here.
    for ($readAttempt=0; $readAttempt -lt 25; $readAttempt++) {
        try {
            $textDocument = $Document.Object('TextDocument')
            if ($null -ne $textDocument -and $null -ne $textDocument.StartPoint -and $null -ne $textDocument.EndPoint) {
                $point = $textDocument.StartPoint.CreateEditPoint()
                if ($null -ne $point) { return [string]$point.GetText($textDocument.EndPoint) }
            }
        } catch { }
        Start-Sleep -Milliseconds 200
    }
    throw 'HMI editor readback remained unavailable after save; write outcome requires inspection.'
}

function Set-TcHmiDocumentText {
    param([Parameter(Mandatory)]$Dte, [Parameter(Mandatory)][string]$Path,
          [Parameter(Mandatory)][AllowEmptyString()][string]$Text)
    $full = [IO.Path]::GetFullPath($Path)
    $Dte.ItemOperations.OpenFile($full, '{7651A703-06E5-11D1-8EBD-00A0C90F26EA}') | Out-Null
    $doc = $null
    for ($attempt=0; $null -eq $doc -and $attempt -lt 20; $attempt++) {
        try {
            for ($i=1; $i -le $Dte.Documents.Count; $i++) {
                $candidate = $Dte.Documents.Item($i)
                if ([string]$candidate.FullName -ieq $full) { $doc=$candidate; break }
            }
        } catch { $doc = $null } # Read-only lookup may be rejected while XAE opens its editor.
        if ($null -eq $doc) { Start-Sleep -Milliseconds 200 }
    }
    if ($null -eq $doc) { throw "Exact HMI text document not available: $full" }
    # OpenFile may register the document before its text buffer is ready.
    # Retry read-only acquisition only; never replay a write on uncertainty.
    $td = $null; $ep = $null; $bufferReady = $false
    for ($attempt=0; -not $bufferReady -and $attempt -lt 20; $attempt++) {
        try {
            $td = $doc.Object('TextDocument')
            if ($null -ne $td -and $null -ne $td.StartPoint -and $null -ne $td.EndPoint) {
                $ep = $td.StartPoint.CreateEditPoint()
                if ($null -ne $ep) {
                    $original = [string]$ep.GetText($td.EndPoint)
                    $bufferReady = $true
                }
            }
        } catch { $bufferReady = $false }
        if (-not $bufferReady) { Start-Sleep -Milliseconds 200 }
    }
    if (-not $bufferReady) { throw "HMI editor text buffer is not ready; no write performed: $full" }
    $saved = [IO.File]::ReadAllText($full, [Text.Encoding]::UTF8)
    if ($original.Replace("`r`n","`n") -cne $saved.Replace("`r`n","`n")) {
        throw 'HMI editor has unsaved changes. Save or discard them explicitly before Agent editing; nothing was overwritten.'
    }
    if ($script:HmiWriteGate.source_file -and
        (Get-TcHmiContractHash $original) -cne [string]$script:HmiWriteGate.source_hash) {
        throw 'HMI editor content changed after preview; retry.'
    }
    $backup = $full + '.agent-' + (Get-Date -Format 'yyyyMMddHHmmssfff') + '.bak'
    [IO.File]::WriteAllText($backup, $original, [Text.UTF8Encoding]::new($false))
    try {
        $ep.ReplaceText($td.EndPoint, $Text, 1)
        $doc.Save()
        $actual = Get-TcHmiEditorTextReadback $doc
        if ($actual.Replace("`r`n","`n") -cne $Text.Replace("`r`n","`n")) { throw 'HMI editor readback mismatch.' }
        # ASCII template files may inherit the Windows ANSI code page. The
        # Unicode editor readback alone does not prove the saved bytes are UTF-8.
        $strictUtf8 = New-Object Text.UTF8Encoding($false, $true)
        $diskMatches = $false
        try {
            $diskText = [IO.File]::ReadAllText($full, $strictUtf8)
            $diskMatches = $diskText.Replace("`r`n","`n") -ceq $Text.Replace("`r`n","`n")
        } catch { }
        if (-not $diskMatches) {
            if (-not $doc.Saved) { throw 'HMI document changed before encoding normalization.' }
            $doc.Close(2) # vsSaveChangesNo: just saved and exactly verified above.
            [IO.File]::WriteAllText($full, $Text, (New-Object Text.UTF8Encoding($true)))
            $Dte.ItemOperations.OpenFile($full, '{7651A703-06E5-11D1-8EBD-00A0C90F26EA}') | Out-Null
            $doc = $null
            for ($attempt=0; $null -eq $doc -and $attempt -lt 20; $attempt++) {
                try {
                    for ($i=1; $i -le $Dte.Documents.Count; $i++) {
                        $candidate = $Dte.Documents.Item($i)
                        if ([string]$candidate.FullName -ieq $full) { $doc=$candidate; break }
                    }
                } catch { $doc = $null }
                if ($null -eq $doc) { Start-Sleep -Milliseconds 100 }
            }
            if ($null -eq $doc) { throw 'UTF-8 document reload unavailable.' }
            $td = $doc.Object('TextDocument')
            $actual = Get-TcHmiEditorTextReadback $doc
            if ($actual.Replace("`r`n","`n") -cne $Text.Replace("`r`n","`n")) { throw 'UTF-8 editor reload mismatch.' }
        }
        $diskText = [IO.File]::ReadAllText($full, $strictUtf8)
        if ($diskText.Replace("`r`n","`n") -cne $Text.Replace("`r`n","`n")) { throw 'UTF-8 disk readback mismatch.' }
    } catch {
        try { $td.StartPoint.CreateEditPoint().ReplaceText($td.EndPoint,$original,1); $doc.Save() } catch { }
        throw
    }
    [pscustomobject]@{verified=$true; load_mode='dte-text-document'; project_reloaded=$false; backup=$backup}
}

function Set-TcHmiMarkup {
    param([Parameter(Mandatory)]$Dte, [string]$ProjectName = '',
          [Parameter(Mandatory)][string]$File,
          [Parameter(Mandatory)][AllowEmptyString()][string]$Markup,
          [bool]$Apply = $false)
    $project = Get-TcHmiProjectObject $Dte $ProjectName
    $root = Get-TcHmiProjectPath $project
    $path = Resolve-TcHmiFilePath $root $File @('.view','.content','.usercontrol') -RequireRegistered
    if (-not [IO.File]::Exists($path)) { throw "HMI markup file not found: $File" }
    $original = [IO.File]::ReadAllText($path, [Text.Encoding]::UTF8)
    try { [xml]$document = $Markup } catch { throw "Invalid HMI markup '$File': $($_.Exception.Message)" }
    $controls = @($document.SelectNodes('//*[@data-tchmi-type]'))
    if ($controls.Count -eq 0) { throw 'HMI markup must contain at least one TcHmi control.' }
    $ids = @($controls | ForEach-Object { [string]$_.id })
    if (@($ids | Where-Object { -not $_ }).Count) { throw 'Every HMI control must have an id.' }
    $duplicates = @($ids | Group-Object | Where-Object { $_.Count -gt 1 })
    if ($duplicates.Count) { throw "Duplicate HMI control id: $($duplicates[0].Name)" }
    foreach ($control in $controls) {
        if (-not ([string]$control.'data-tchmi-type').StartsWith('TcHmi.Controls.', [StringComparison]::Ordinal)) {
            throw "Invalid HMI control type on '$([string]$control.id)'."
        }
    }
    $updated = Convert-TcHmiXmlText $document
    Assert-TcHmiWriteContract (Get-TcHmiResolvedFullName $project) $updated $Apply
    if (-not $Apply) {
        return [pscustomobject]@{ status='preview'; project=Get-TcHmiResolvedName $project
            project_file=Get-TcHmiResolvedFullName $project
            source_file=$path; source_hash=Get-TcHmiContractHash $original
            file=$File.Replace('\','/'); control_count=$controls.Count; markup=$updated; apply_required=$true }
    }
    $write = Set-TcHmiDocumentText $Dte $path $updated
    [xml]$readback = [IO.File]::ReadAllText($path, [Text.Encoding]::UTF8)
    $readbackControls = @($readback.SelectNodes('//*[@data-tchmi-type]'))
    if ($readbackControls.Count -ne $controls.Count) { throw 'HMI markup control-count readback mismatch.' }
    [pscustomobject]@{ status='applied'; project=Get-TcHmiResolvedName $project
        file=$File.Replace('\','/'); control_count=$readbackControls.Count; verified=$true
        load_mode=$write.load_mode; project_reloaded=$false; backup=$write.backup }
}

function Update-TcHmiControlXml {
    # Pure in-memory edit shared by single and batch transactions; never saves.
    param([Parameter(Mandatory)][xml]$Document,
          [ValidateSet('add','update','remove')][string]$Action,
          [Parameter(Mandatory)][string]$ControlId,
          [string]$ControlType = '', [string]$ParentId = '',
          [object]$Attributes = $null)
    if (-not ($ControlId -match '^[A-Za-z_][A-Za-z0-9_]*$')) { throw "Invalid HMI control id '$ControlId'." }
    if ($ParentId -and -not ($ParentId -match '^[A-Za-z_][A-Za-z0-9_]*$')) { throw "Invalid parent control id '$ParentId'." }
    $nodes = @($document.SelectNodes('//*[@id]'))
    $matches = @($nodes | Where-Object { [string]$_.id -ceq $ControlId })
    $changes = New-Object System.Collections.ArrayList
    if ($Action -eq 'add') {
        if ($matches.Count -gt 0) { throw "HMI control id '$ControlId' already exists in markup." }
        if (-not $ControlType.StartsWith('TcHmi.Controls.', [StringComparison]::Ordinal)) { throw "A valid TcHmi.Controls.* type is required when adding '$ControlId'." }
        $parent = $document.DocumentElement
        if ($ParentId) {
            $parents = @($nodes | Where-Object { [string]$_.id -ceq $ParentId })
            if ($parents.Count -ne 1) { throw "Parent control '$ParentId' was not found uniquely in markup." }
            $parent = $parents[0]
        }
        $target = $document.CreateElement('div'); $target.SetAttribute('id', $ControlId)
        $target.SetAttribute('data-tchmi-type', $ControlType)
        [void]$parent.AppendChild($target)
        [void]$changes.Add([pscustomobject]@{ field='control'; before=$null; after=$ControlId })
    } else {
        if ($matches.Count -ne 1) { throw "HMI control '$ControlId' was not found uniquely in markup." }
        $target = $matches[0]
        if ($target -eq $document.DocumentElement -and $Action -eq 'remove') { throw 'The root View/Content control cannot be removed.' }
        if ($Action -eq 'remove') {
            [void]$changes.Add([pscustomobject]@{ field='control'; before=$ControlId; after=$null })
            [void]$target.ParentNode.RemoveChild($target)
        } elseif ($ControlType) {
            if (-not $ControlType.StartsWith('TcHmi.Controls.', [StringComparison]::Ordinal)) { throw "Invalid HMI control type '$ControlType'." }
            $beforeType = [string]$target.GetAttribute('data-tchmi-type')
            $target.SetAttribute('data-tchmi-type', $ControlType)
            [void]$changes.Add([pscustomobject]@{ field='data-tchmi-type'; before=$beforeType; after=$ControlType })
        }
    }
    if ($Action -ne 'remove' -and $null -ne $Attributes) {
        foreach ($attribute in @($Attributes.PSObject.Properties)) {
            $attributeName = [string]$attribute.Name
            if (-not $attributeName.StartsWith('data-tchmi-', [StringComparison]::OrdinalIgnoreCase) -or
                $attributeName -ieq 'data-tchmi-type') {
                throw "Only data-tchmi-* attributes except data-tchmi-type are allowed; got '$attributeName'."
            }
            $before = if ($target.HasAttribute($attributeName)) { [string]$target.GetAttribute($attributeName) } else { $null }
            if ($null -eq $attribute.Value) { $target.RemoveAttribute($attributeName) }
            else { $target.SetAttribute($attributeName, [string]$attribute.Value) }
            $after = if ($target.HasAttribute($attributeName)) { [string]$target.GetAttribute($attributeName) } else { $null }
            [void]$changes.Add([pscustomobject]@{ field=$attributeName; before=$before; after=$after })
        }
    }
    return [pscustomobject]@{ action=$Action; control_id=$ControlId; changes=@($changes.ToArray()) }
}

function Edit-TcHmiControl {
    param([Parameter(Mandatory)]$Dte, [string]$ProjectName = '',
          [Parameter(Mandatory)][string]$File,
          [ValidateSet('add','update','remove')][string]$Action,
          [Parameter(Mandatory)][string]$ControlId,
          [string]$ControlType = '', [string]$ParentId = '',
          [object]$Attributes = $null, [bool]$Apply = $false)
    $project = Get-TcHmiProjectObject $Dte $ProjectName
    $projectDisplayName = Get-TcHmiResolvedName $project
    $projectFile = Get-TcHmiResolvedFullName $project
    $root = Get-TcHmiProjectPath $project
    $path = Resolve-TcHmiFilePath $root $File @('.view','.content','.usercontrol') -RequireRegistered
    $original = [IO.File]::ReadAllText($path, [Text.Encoding]::UTF8)
    [xml]$document = $original
    $edit = Update-TcHmiControlXml $document $Action $ControlId $ControlType $ParentId $Attributes
    $changes = New-Object System.Collections.ArrayList
    foreach ($change in $edit.changes) { [void]$changes.Add($change) }
    $updated = Convert-TcHmiXmlText $document
    Assert-TcHmiWriteContract $projectFile $updated $Apply
    if (-not $Apply) {
        return [pscustomobject]@{ status='preview'; project=$projectDisplayName; file=$File.Replace('\','/')
            project_file=$projectFile
            source_file=$path; source_hash=Get-TcHmiContractHash $original
            action=$Action; control_id=$ControlId; changes=@($changes.ToArray()); markup=$updated
            would_reload_project=$false; apply_required=$true }
    }
    $write = Set-TcHmiDocumentText $Dte $path $updated
    [xml]$readback = [IO.File]::ReadAllText($path, [Text.Encoding]::UTF8)
    $exists = @($readback.SelectNodes('//*[@id]') | Where-Object { [string]$_.id -ceq $ControlId }).Count -eq 1
    $verified = if ($Action -eq 'remove') { -not $exists } else { $exists }
    if (-not $verified) { throw "HMI control '$ControlId' edit could not be verified after DTE save." }
    [pscustomobject]@{ status='applied'; project=$projectDisplayName; file=$File.Replace('\','/')
        action=$Action; control_id=$ControlId; changes=@($changes.ToArray()); verified=$true
        load_mode=$write.load_mode; project_reloaded=$false; backup=$write.backup }
}

function Edit-TcHmiControlsBatch {
    param([Parameter(Mandatory)]$Dte, [string]$ProjectName = '',
          [Parameter(Mandatory)][string]$File,
          [object[]]$Operations, [bool]$Apply = $false)
    if ($Operations.Count -lt 1 -or $Operations.Count -gt 100) { throw 'Provide 1..100 control operations per batch.' }
    $project = Get-TcHmiProjectObject $Dte $ProjectName
    $projectFile = Get-TcHmiResolvedFullName $project
    $root = Get-TcHmiProjectPath $project
    $path = Resolve-TcHmiFilePath $root $File @('.view','.content','.usercontrol') -RequireRegistered
    $original = [IO.File]::ReadAllText($path, [Text.Encoding]::UTF8)
    [xml]$document = $original
    $results = New-Object System.Collections.ArrayList
    $ids = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
    foreach ($op in $Operations) {
        foreach ($key in $op.PSObject.Properties.Name) {
            if ($key -notin @('action','control_id','control_type','parent_id','attributes')) { throw "Unknown batch operation field '$key'." }
        }
        if ($op.action -notin @('add','update','remove')) { throw 'Each operation requires add/update/remove action.' }
        if (-not $ids.Add([string]$op.control_id)) { throw "Duplicate batch target '$($op.control_id)'; combine its changes into one operation." }
        $edit = Update-TcHmiControlXml $document ([string]$op.action) ([string]$op.control_id) `
            ([string]$op.control_type) ([string]$op.parent_id) $op.attributes
        [void]$results.Add($edit)
    }
    # Reject deleting a parent of another target: every operation has an unambiguous final result.
    foreach ($edit in $results) {
        $count = @($document.SelectNodes('//*[@id]') | Where-Object { [string]$_.id -ceq $edit.control_id }).Count
        if (($edit.action -ne 'remove' -and $count -ne 1) -or ($edit.action -eq 'remove' -and $count -ne 0)) {
            throw "Conflicting final state for '$($edit.control_id)'."
        }
    }
    $updated = Convert-TcHmiXmlText $document
    Assert-TcHmiWriteContract $projectFile $updated $Apply
    if (-not $Apply) {
        return [pscustomobject]@{ status='preview'; project_file=$projectFile; file=$File
            source_file=$path; source_hash=Get-TcHmiContractHash $original; markup=$updated
            operations=@($results.ToArray()); operation_count=$results.Count
            would_reload_project=$false; apply_required=$true }
    }
    # One backup, one DTE save and exact whole-document readback (including attributes).
    $write = Set-TcHmiDocumentText $Dte $path $updated
    foreach ($edit in $results) { $edit | Add-Member -NotePropertyName verified -NotePropertyValue $true }
    return [pscustomobject]@{ status='applied'; project_file=$projectFile; file=$File
        operations=@($results.ToArray()); operation_count=$results.Count; verified=$true
        load_mode=$write.load_mode; project_reloaded=$false; backup=$write.backup }
}

function Remove-TcHmiView {
    param([Parameter(Mandatory)]$Dte, [string]$ProjectName = '',
          [Parameter(Mandatory)][string]$File, [bool]$Apply = $false)
    $project = Get-TcHmiProjectObject $Dte $ProjectName
    try { $Dte.ExecuteCommand('File.SaveAll') } catch { }
    $projectDisplayName = Get-TcHmiResolvedName $project
    $projectFile = Get-TcHmiResolvedFullName $project
    $root = Get-TcHmiProjectPath $project
    $path = Resolve-TcHmiFilePath $root $File @('.view','.content') -RequireRegistered
    $relative = $path.Substring($root.Length).TrimStart('\').Replace('\','/')
    if (-not [IO.File]::Exists($path)) { throw "HMI page not found: $relative" }
    $configPath = Join-Path $root 'Properties\tchmiconfig.json'
    $configText = [IO.File]::ReadAllText($configPath, [Text.Encoding]::UTF8)
    $config = $configText | ConvertFrom-Json
    if ([string]$config.startupView -ieq $relative) { throw "Refusing to delete startup view '$relative'; select another startup view first." }
    $projectText = [IO.File]::ReadAllText($projectFile, [Text.Encoding]::UTF8)
    [xml]$projectXml = $projectText
    $includes = @($projectXml.SelectNodes("//*[local-name()='Content']") | Where-Object { [string]$_.Include -ieq $relative })
    if ($includes.Count -ne 1) { throw "HMI page '$relative' is not registered uniquely in .hmiproj." }
    $backupDir = Join-Path $root '.TwinCATAgent\backups'
    $backupFile = Join-Path $backupDir ((Get-Date -Format 'yyyyMMdd-HHmmssfff') + '-' + [IO.Path]::GetFileName($path))
    if (-not $Apply) {
        return [pscustomobject]@{ status='preview'; project=$projectDisplayName; file=$relative
            would_remove_project_item=$true; would_remove_framework_entry=$true
            would_delete_file=$true; planned_backup=$backupFile; apply_required=$true }
    }
    [IO.Directory]::CreateDirectory($backupDir) | Out-Null
    [IO.File]::Copy($path, $backupFile, $false)
    $includes[0].ParentNode.RemoveChild($includes[0]) | Out-Null
    $projectXml.Save($projectFile)
    if ([IO.Path]::GetExtension($path) -ieq '.view') {
        $config.views = @($config.views | Where-Object { [string]$_.url -ine $relative })
    } else {
        $config.content = @($config.content | Where-Object { [string]$_.url -ine $relative })
    }
    [IO.File]::WriteAllText($configPath, ($config | ConvertTo-Json -Depth 100), (New-Object Text.UTF8Encoding($false)))
    [IO.File]::Delete($path)
    try { $project = Reload-TcHmiProject $Dte $project $projectFile }
    catch {
        [IO.File]::WriteAllText($projectFile, $projectText, (New-Object Text.UTF8Encoding($true)))
        [IO.File]::WriteAllText($configPath, $configText, (New-Object Text.UTF8Encoding($false)))
        [IO.File]::Copy($backupFile, $path, $true)
        try { $Dte.Solution.AddFromFile($projectFile, $false) | Out-Null } catch { }
        throw "HMI page deletion failed and project files were rolled back: $($_.Exception.Message)"
    }
    [xml]$readback = [IO.File]::ReadAllText($projectFile, [Text.Encoding]::UTF8)
    $stillIncluded = @($readback.SelectNodes("//*[local-name()='Content']") | Where-Object { [string]$_.Include -ieq $relative }).Count -gt 0
    $configReadback = [IO.File]::ReadAllText($configPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
    $frameworkEntries = if ([IO.Path]::GetExtension($path) -ieq '.view') { @($configReadback.views) } else { @($configReadback.content) }
    $frameworkRemoved = @($frameworkEntries | Where-Object { [string]$_.url -ieq $relative }).Count -eq 0
    $verified = (-not [IO.File]::Exists($path)) -and (-not $stillIncluded) -and $frameworkRemoved -and [IO.File]::Exists($backupFile)
    [pscustomobject]@{ status='deleted'; project=$projectDisplayName; file=$relative; backup_file=$backupFile
        file_deleted=(-not [IO.File]::Exists($path)); project_item_removed=(-not $stillIncluded)
        framework_entry_removed=$frameworkRemoved; verified=$verified; load_mode='hmiproj-update-and-com-reload' }
}

function Set-TcHmiAdsRuntime {
    param([Parameter(Mandatory)]$Dte, [string]$ProjectName = '',
          [ValidateSet('default','remote','both')][string]$Scope = 'both',
          [ValidateSet('upsert','remove')][string]$Action = 'upsert',
          [Parameter(Mandatory)][string]$Name, [object]$Settings = $null,
          [bool]$Apply = $false)
    if (-not ($Name -match '^[A-Za-z_][A-Za-z0-9_.-]*$')) { throw "Invalid ADS Runtime name '$Name'." }
    $project = Get-TcHmiProjectObject $Dte $ProjectName
    try { $Dte.ExecuteCommand('File.SaveAll') } catch { }
    $projectDisplayName = Get-TcHmiResolvedName $project
    $projectFile = Get-TcHmiResolvedFullName $project
    $root = Get-TcHmiProjectPath $project
    $scopes = if ($Scope -eq 'both') { @('default','remote') } else { @($Scope) }
    $plans = New-Object System.Collections.ArrayList
    $originals = @{}
    foreach ($currentScope in $scopes) {
        $relative = "Server/ADS/ADS.Config.$currentScope.json"
        $path = Join-Path $root $relative.Replace('/','\')
        if (-not [IO.File]::Exists($path)) { throw "ADS configuration file not found: $relative" }
        $original = [IO.File]::ReadAllText($path, [Text.Encoding]::UTF8); $originals[$path] = $original
        $config = $original | ConvertFrom-Json
        $runtimes = [ordered]@{}
        foreach ($property in @($config.RUNTIMES.PSObject.Properties)) { $runtimes[[string]$property.Name] = $property.Value }
        $before = if ($runtimes.Contains($Name)) { $runtimes[$Name] } else { $null }
        if ($Action -eq 'remove') {
            if ($null -eq $before) { throw "ADS Runtime '$Name' does not exist in $currentScope configuration." }
            $runtimes.Remove($Name)
        } else {
            $enabled = if ($null -ne $Settings -and $Settings.PSObject.Properties.Name -contains 'enabled') { [bool]$Settings.enabled } elseif ($null -ne $before) { [bool]$before.ENABLED } else { $true }
            $netId = if ($null -ne $Settings -and $Settings.PSObject.Properties.Name -contains 'netid') { [string]$Settings.netid } elseif ($null -ne $before) { [string]$before.NETID } else { '' }
            $port = if ($null -ne $Settings -and $Settings.PSObject.Properties.Name -contains 'port') { [int]$Settings.port } elseif ($null -ne $before) { [int]$before.PORT } else { 0 }
            if (-not ($netId -match '^\d{1,3}(\.\d{1,3}){5}$') -or @($netId.Split('.') | Where-Object { [int]$_ -gt 255 }).Count -gt 0) { throw "Invalid AMS NetId '$netId'." }
            if ($port -lt 1 -or $port -gt 65535) { throw "Invalid ADS port '$port'." }
            $readOnly = if ($null -ne $Settings -and $Settings.PSObject.Properties.Name -contains 'read_only') { [bool]$Settings.read_only } elseif ($null -ne $before) { [bool]$before.READ_ONLY } else { $false }
            $whitelist = if ($null -ne $Settings -and $Settings.PSObject.Properties.Name -contains 'use_whitelisting') { [bool]$Settings.use_whitelisting } elseif ($null -ne $before) { [bool]$before.USE_WHITELISTING } else { $false }
            $symbols = if ($null -ne $before -and $null -ne $before.SYMBOLS) { $before.SYMBOLS } else { [pscustomobject]@{} }
            $runtimes[$Name] = [pscustomobject][ordered]@{ ENABLED=$enabled; NETID=$netId; PORT=$port
                READ_ONLY=$readOnly; SYMBOLS=$symbols; USE_WHITELISTING=$whitelist }
        }
        $config.RUNTIMES = [pscustomobject]$runtimes
        $updated = $config | ConvertTo-Json -Depth 100
        [void]$plans.Add([pscustomobject]@{ scope=$currentScope; path=$path; before=$before
            after=if($Action -eq 'remove'){$null}else{$runtimes[$Name]}; text=$updated })
    }
    if (-not $Apply) {
        return [pscustomobject]@{ status='preview'; project=$projectDisplayName; scope=$Scope; action=$Action
            runtime=$Name; changes=@($plans | Select-Object scope,before,after); apply_required=$true }
    }
    try {
        foreach ($plan in $plans) { [IO.File]::WriteAllText([string]$plan.path, [string]$plan.text, (New-Object Text.UTF8Encoding($false))) }
        $project = Reload-TcHmiProject $Dte $project $projectFile
    } catch {
        foreach ($path in $originals.Keys) { [IO.File]::WriteAllText($path, [string]$originals[$path], (New-Object Text.UTF8Encoding($false))) }
        try { $Dte.Solution.AddFromFile($projectFile, $false) | Out-Null } catch { }
        throw "ADS Runtime update failed and configuration was rolled back: $($_.Exception.Message) [$($_.ScriptStackTrace)]"
    }
    $readback = Get-TcHmiAdsInfo $Dte $projectFile
    $matches = @($readback.runtimes | Where-Object { $_.name -ceq $Name -and $scopes -contains $_.scope })
    $verified = if ($Action -eq 'remove') { $matches.Count -eq 0 } else { $matches.Count -eq $scopes.Count }
    [pscustomobject]@{ status='applied'; project=$projectDisplayName; scope=$Scope; action=$Action
        runtime=$Name; verified=$verified; runtimes=@($matches); load_mode='config-update-and-com-reload' }
}

function Get-TcHmiAdsSymbols {
    param([Parameter(Mandatory)]$Dte, [string]$ProjectName = '',
          [string]$Runtime = '', [ValidateSet('','default','remote')][string]$Scope = '')
    $project = Get-TcHmiProjectObject $Dte $ProjectName
    $root = Get-TcHmiProjectPath $project
    $symbols = New-Object System.Collections.ArrayList
    $scopes = if ($Scope) { @($Scope) } else { @('default','remote') }
    foreach ($currentScope in $scopes) {
        $relative = "Server/ADS/ADS.Config.$currentScope.json"
        $path = Join-Path $root $relative.Replace('/','\')
        if (-not [IO.File]::Exists($path)) { continue }
        $config = [IO.File]::ReadAllText($path, [Text.Encoding]::UTF8) | ConvertFrom-Json
        foreach ($runtimeProperty in @($config.RUNTIMES.PSObject.Properties)) {
            if ($Runtime -and -not ([string]$runtimeProperty.Name).Equals($Runtime, [StringComparison]::OrdinalIgnoreCase)) { continue }
            foreach ($symbolProperty in @($runtimeProperty.Value.SYMBOLS.PSObject.Properties)) {
                $mapping = $symbolProperty.Value
                [void]$symbols.Add([pscustomobject]@{
                    scope=$currentScope; runtime=[string]$runtimeProperty.Name; name=[string]$symbolProperty.Name
                    index_group=[uint32]$mapping.INDEXGROUP; index_offset=[uint32]$mapping.INDEXOFFSET
                    type_name=[string]$mapping.TYPENAME
                })
            }
        }
    }
    [pscustomobject]@{ status='ok'; project=Get-TcHmiResolvedName $project; runtime=$Runtime; scope=$Scope
        symbol_count=$symbols.Count; symbols=@($symbols.ToArray()); readonly=$true
        note='Saved ADS symbol mappings only; PLC reachability and address/type correctness were not tested.' }
}

function Set-TcHmiAdsSymbol {
    param([Parameter(Mandatory)]$Dte, [string]$ProjectName = '',
          [ValidateSet('default','remote','both')][string]$Scope = 'both',
          [Parameter(Mandatory)][string]$Runtime,
          [Parameter(Mandatory)][string]$Name,
          [ValidateSet('upsert','remove')][string]$Action = 'upsert',
          [uint32]$IndexGroup = 0, [uint32]$IndexOffset = 0,
          [string]$TypeName = '', [bool]$Apply = $false)
    if ([string]::IsNullOrWhiteSpace($Runtime)) { throw 'ADS Runtime name is required.' }
    if ([string]::IsNullOrWhiteSpace($Name) -or $Name.Contains('%')) { throw 'A non-empty ADS mapped symbol name is required.' }
    if ($Action -eq 'upsert' -and [string]::IsNullOrWhiteSpace($TypeName)) { throw 'type_name is required when adding or updating an ADS symbol mapping.' }
    $project = Get-TcHmiProjectObject $Dte $ProjectName
    try { $Dte.ExecuteCommand('File.SaveAll') } catch { }
    $projectDisplayName = Get-TcHmiResolvedName $project
    $projectFile = Get-TcHmiResolvedFullName $project
    $root = Get-TcHmiProjectPath $project
    $scopes = if ($Scope -eq 'both') { @('default','remote') } else { @($Scope) }
    $plans = New-Object System.Collections.ArrayList; $originals = @{}
    foreach ($currentScope in $scopes) {
        $relative = "Server/ADS/ADS.Config.$currentScope.json"
        $path = Join-Path $root $relative.Replace('/','\')
        if (-not [IO.File]::Exists($path)) { throw "ADS configuration file not found: $relative" }
        $original = [IO.File]::ReadAllText($path, [Text.Encoding]::UTF8); $originals[$path] = $original
        $config = $original | ConvertFrom-Json
        $runtimeProperty = @($config.RUNTIMES.PSObject.Properties | Where-Object { ([string]$_.Name).Equals($Runtime, [StringComparison]::OrdinalIgnoreCase) })
        if ($runtimeProperty.Count -ne 1) { throw "ADS Runtime '$Runtime' was not found uniquely in $currentScope configuration." }
        $runtimeName = [string]$runtimeProperty[0].Name; $runtimeObject = $runtimeProperty[0].Value
        $mapped = [ordered]@{}
        foreach ($property in @($runtimeObject.SYMBOLS.PSObject.Properties)) { $mapped[[string]$property.Name] = $property.Value }
        $before = if ($mapped.Contains($Name)) { $mapped[$Name] } else { $null }
        if ($Action -eq 'remove') {
            if ($null -eq $before) { throw "ADS symbol mapping '$Name' does not exist in Runtime '$runtimeName' ($currentScope)." }
            $mapped.Remove($Name)
        } else {
            $mapped[$Name] = [pscustomobject][ordered]@{ INDEXGROUP=[uint32]$IndexGroup; INDEXOFFSET=[uint32]$IndexOffset; TYPENAME=$TypeName }
        }
        $runtimeObject.SYMBOLS = [pscustomobject]$mapped
        $updated = $config | ConvertTo-Json -Depth 100
        [void]$plans.Add([pscustomobject]@{ scope=$currentScope; path=$path; runtime=$runtimeName
            before=$before; after=if($Action -eq 'remove'){$null}else{$mapped[$Name]}; text=$updated })
    }
    if (-not $Apply) {
        return [pscustomobject]@{ status='preview'; project=$projectDisplayName; scope=$Scope; runtime=$Runtime
            action=$Action; symbol=$Name; changes=@($plans | Select-Object scope,before,after); apply_required=$true }
    }
    try {
        foreach ($plan in $plans) { [IO.File]::WriteAllText([string]$plan.path, [string]$plan.text, (New-Object Text.UTF8Encoding($false))) }
        $project = Reload-TcHmiProject $Dte $project $projectFile
    } catch {
        foreach ($path in $originals.Keys) { [IO.File]::WriteAllText($path, [string]$originals[$path], (New-Object Text.UTF8Encoding($false))) }
        try { $Dte.Solution.AddFromFile($projectFile, $false) | Out-Null } catch { }
        throw "ADS symbol mapping update failed and configuration was rolled back: $($_.Exception.Message) [$($_.ScriptStackTrace)]"
    }
    $readback = Get-TcHmiAdsSymbols $Dte $projectFile $Runtime ''
    $matches = @($readback.symbols | Where-Object { $_.name -ceq $Name -and $scopes -contains $_.scope })
    $verified = if ($Action -eq 'remove') { $matches.Count -eq 0 } else {
        $matches.Count -eq $scopes.Count -and @($matches | Where-Object {
            [uint32]$_.index_group -ne $IndexGroup -or [uint32]$_.index_offset -ne $IndexOffset -or [string]$_.type_name -cne $TypeName
        }).Count -eq 0
    }
    if (-not $verified) { throw "ADS symbol mapping '$Name' write could not be verified." }
    [pscustomobject]@{ status='applied'; project=$projectDisplayName; scope=$Scope; runtime=$Runtime
        action=$Action; symbol=$Name; verified=$true; mappings=@($matches); load_mode='config-update-and-com-reload' }
}

function Set-TcHmiDynamicSymbols {
    param([Parameter(Mandatory)]$Dte, [string]$ProjectName = '',
          [Parameter(Mandatory)][object]$Symbols,
          [object]$Definitions = $null, [bool]$Apply = $false)
    $project = Get-TcHmiProjectObject $Dte $ProjectName
    try { $Dte.ExecuteCommand('File.SaveAll') } catch { }
    $projectDisplayName = Get-TcHmiResolvedName $project
    $projectFile = Get-TcHmiResolvedFullName $project
    $root = Get-TcHmiProjectPath $project
    $path = Join-Path $root 'Server\TcHmiSrv\TcHmiSrv.Config.default.json'
    if (-not [IO.File]::Exists($path)) { throw 'TcHmiSrv default configuration was not found.' }
    $original = [IO.File]::ReadAllText($path, [Text.Encoding]::UTF8)
    $config = $original | ConvertFrom-Json
    $symbolMap = [ordered]@{}
    foreach ($property in @($config.SYMBOLS.PSObject.Properties)) { $symbolMap[[string]$property.Name] = $property.Value }
    $changedNames = New-Object System.Collections.ArrayList
    foreach ($property in @($Symbols.PSObject.Properties)) {
        $name = [string]$property.Name; $item = $property.Value
        if (-not $name.StartsWith('ADS.', [StringComparison]::Ordinal)) { throw "Dynamic ADS symbol must start with 'ADS.': $name" }
        if ([string]$item.DOMAIN -cne 'ADS' -or -not [bool]$item.DYNAMIC -or -not [bool]$item.USEMAPPING) {
            throw "Dynamic symbol '$name' must use DOMAIN=ADS, DYNAMIC=true and USEMAPPING=true."
        }
        if (-not ([string]$item.MAPPING -match '^[A-Za-z_][A-Za-z0-9_.-]*::')) { throw "Invalid ADS mapping for '$name'." }
        if ($null -eq $item.SCHEMA) { throw "Dynamic symbol '$name' requires a schema." }
        $symbolMap[$name] = $item; [void]$changedNames.Add($name)
    }
    $config.SYMBOLS = [pscustomobject]$symbolMap
    $definitionNames = New-Object System.Collections.ArrayList
    if ($null -ne $Definitions) {
        if ($null -eq $config.DEFINITIONS.ADS) { $config.DEFINITIONS | Add-Member -NotePropertyName ADS -NotePropertyValue ([pscustomobject]@{}) -Force }
        $definitionMap = [ordered]@{}
        foreach ($property in @($config.DEFINITIONS.ADS.PSObject.Properties)) { $definitionMap[[string]$property.Name] = $property.Value }
        foreach ($property in @($Definitions.PSObject.Properties)) {
            $name = [string]$property.Name
            if (-not $name.StartsWith('ADS-', [StringComparison]::Ordinal)) { throw "ADS definition must start with 'ADS-': $name" }
            $definitionMap[$name] = $property.Value; [void]$definitionNames.Add($name)
        }
        $config.DEFINITIONS.ADS = [pscustomobject]$definitionMap
    }
    $updated = $config | ConvertTo-Json -Depth 100
    if (-not $Apply) {
        return [pscustomobject]@{ status='preview'; project=$projectDisplayName
            symbols=@($changedNames.ToArray()); definitions=@($definitionNames.ToArray()); apply_required=$true }
    }
    $backup = $path + '.agent-' + (Get-Date -Format 'yyyyMMddHHmmssfff') + '.bak'
    [IO.File]::Copy($path, $backup, $false)
    try {
        $Dte.Solution.Remove($project)
        [IO.File]::WriteAllText($path, $updated, (New-Object Text.UTF8Encoding($false)))
        $project = $Dte.Solution.AddFromFile($projectFile, $false)
        if ($null -eq $project) { throw 'XAE did not reload the HMI project.' }
        try { $Dte.ExecuteCommand('File.SaveAll') } catch { }
    } catch {
        [IO.File]::Copy($backup, $path, $true)
        try { $Dte.Solution.AddFromFile($projectFile, $false) | Out-Null } catch { }
        throw "Dynamic HMI symbol update failed and was rolled back: $($_.Exception.Message)"
    }
    $readback = [IO.File]::ReadAllText($path, [Text.Encoding]::UTF8) | ConvertFrom-Json
    foreach ($name in @($changedNames.ToArray())) {
        if ($readback.SYMBOLS.PSObject.Properties.Name -cnotcontains $name) { throw "Dynamic symbol readback failed: $name" }
    }
    foreach ($name in @($definitionNames.ToArray())) {
        if ($readback.DEFINITIONS.ADS.PSObject.Properties.Name -cnotcontains $name) { throw "ADS definition readback failed: $name" }
    }
    [pscustomobject]@{ status='applied'; project=$projectDisplayName
        symbol_count=$changedNames.Count; definition_count=$definitionNames.Count; verified=$true
        backup=$backup; load_mode='config-update-and-com-reload' }
}

function Get-TcHmiBindings {
    param([Parameter(Mandatory)]$Dte, [string]$ProjectName = '')
    $project = Get-TcHmiProjectObject $Dte $ProjectName
    $projectDisplayName = Get-TcHmiResolvedName $project
    $root = Get-TcHmiProjectPath $project
    $ads = Get-TcHmiAdsInfo $Dte $projectDisplayName
    $runtimeNames = @($ads.runtimes | Select-Object -ExpandProperty name -Unique)
    $mapped = Get-TcHmiAdsSymbols $Dte $projectDisplayName '' ''
    $dynamicNames = @()
    $serverConfigPath = Join-Path $root 'Server\TcHmiSrv\TcHmiSrv.Config.default.json'
    if ([IO.File]::Exists($serverConfigPath)) {
        try {
            $serverConfig = [IO.File]::ReadAllText($serverConfigPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
            $dynamicNames = @($serverConfig.SYMBOLS.PSObject.Properties | Where-Object {
                [string]$_.Value.DOMAIN -ceq 'ADS' -and [bool]$_.Value.DYNAMIC -and [bool]$_.Value.USEMAPPING
            } | ForEach-Object { [string]$_.Name })
        } catch { }
    }
    $configPath = Join-Path $root 'Properties\tchmiconfig.json'
    $config = [IO.File]::ReadAllText($configPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
    $internalNames = @($config.symbols.internal.PSObject.Properties | ForEach-Object { [string]$_.Name })
    $localization = Get-TcHmiLocalizations $Dte $projectDisplayName
    $localizationNames = @($localization.keys | ForEach-Object { [string]$_.key })
    $themedResourceNames = @($config.symbols.themedResources.PSObject.Properties | ForEach-Object { [string]$_.Name })
    $allControlIds = New-Object System.Collections.ArrayList
    $files = @(Get-ChildItem -LiteralPath $root -Recurse -File | Where-Object {
        if ($_.Extension -notin @('.view','.content','.usercontrol')) { return $false }
        $relative = $_.FullName.Substring($root.Length).TrimStart('\').Replace('\','/')
        return ($relative -notmatch '^(\.TwinCATAgent|bin|obj|Packages|node_modules)/')
    })
    foreach ($file in $files) {
        try { [xml]$doc = [IO.File]::ReadAllText($file.FullName, [Text.Encoding]::UTF8)
            foreach ($node in @($doc.SelectNodes('//*[@id]'))) { if ([string]$node.id) { [void]$allControlIds.Add([string]$node.id) } }
        } catch { }
    }
    $bindings = New-Object System.Collections.ArrayList; $findings = New-Object System.Collections.ArrayList
    $expressionPattern = [regex]'%(?<tag>[A-Za-z][A-Za-z0-9]*)%(?<path>.*?)%/\k<tag>%'
    foreach ($file in $files) {
        $relative = $file.FullName.Substring($root.Length).TrimStart('\').Replace('\','/')
        try { [xml]$doc = [IO.File]::ReadAllText($file.FullName, [Text.Encoding]::UTF8) } catch { continue }
        foreach ($node in @($doc.SelectNodes('//*'))) {
            foreach ($attribute in @($node.Attributes)) {
                $value = [string]$attribute.Value
                foreach ($match in $expressionPattern.Matches($value)) {
                    $tag = $match.Groups['tag'].Value.ToLowerInvariant(); $pathValue = $match.Groups['path'].Value
                    $kind = switch ($tag) { 's' {'server'}; 'i' {'internal'}; 'ctrl' {'control'}; 'l' {'localization'}; 'tr' {'themed-resource'}; default {'other'} }
                    $state='unverified'; $detail='Expression syntax recognized; this symbol type is not statically resolved.'
                    if ($kind -eq 'server') {
                        if ($pathValue -match '^[^.]+::') {
                            $state='mapping-path-invalid'
                            $detail='Raw ADS MAPPING path was used as a page symbol. Use the TcHmiSrv SYMBOLS key, normally ADS.<Runtime>.<PLC symbol>.'
                        } else {
                            $parts = @($pathValue -split '\.')
                            $runtime=''; $symbolName=$pathValue
                            if ($parts.Count -ge 3 -and $parts[0] -ieq 'ADS') { $runtime=$parts[1]; $symbolName=($parts[2..($parts.Count-1)] -join '.') }
                            elseif ($parts.Count -ge 2 -and $runtimeNames -contains $parts[0]) { $runtime=$parts[0]; $symbolName=($parts[1..($parts.Count-1)] -join '.') }
                            if (-not $runtime) { $state='runtime-unresolved'; $detail='Server expression does not identify a configured ADS Runtime.' }
                            elseif ($runtimeNames -notcontains $runtime) { $state='runtime-missing'; $detail="ADS Runtime '$runtime' is not configured." }
                            else {
                                $isMapped = @($mapped.symbols | Where-Object { $_.runtime -ieq $runtime -and ($_.name -ieq $symbolName -or $_.name -ieq $pathValue) }).Count -gt 0
                                if (-not $isMapped) {
                                    $isMapped = @($dynamicNames | Where-Object {
                                        $pathValue -ieq $_ -or $pathValue.StartsWith(($_ + '::'), [StringComparison]::OrdinalIgnoreCase) -or
                                        ($pathValue -match ('^' + [regex]::Escape($_) + '\[\d+\](?:::|\[|$)'))
                                    }).Count -gt 0
                                }
                                if ($isMapped) { $state='mapped'; $detail='A saved ADS mapping exists.' }
                                else { $state='unmapped'; $detail='Runtime exists but no saved ADS mapping matches this symbol.' }
                            }
                        }
                    } elseif ($kind -eq 'internal') {
                        if ($internalNames -contains $pathValue) { $state='resolved'; $detail='Internal symbol exists in tchmiconfig.json.' }
                        else { $state='missing'; $detail='Internal symbol is not declared in tchmiconfig.json.' }
                    } elseif ($kind -eq 'control') {
                        $controlId = ($pathValue -split '::')[0]
                        if ($allControlIds -contains $controlId) { $state='resolved'; $detail='Referenced control ID exists in the project.' }
                        else { $state='missing'; $detail="Referenced control '$controlId' was not found." }
                    } elseif ($kind -eq 'localization') {
                        if ($localizationNames -ccontains $pathValue) { $state='resolved'; $detail='Localization key exists in a registered project language file.' }
                        else { $state='missing'; $detail="Localization key '$pathValue' was not found." }
                    } elseif ($kind -eq 'themed-resource') {
                        if ($themedResourceNames -ccontains $pathValue) { $state='resolved'; $detail='Project themed resource exists in tchmiconfig.json.' }
                        elseif ($pathValue -match '^(control|package)::') { $state='unverified'; $detail='Control/package themed resource syntax recognized; package resources are not statically expanded.' }
                        else { $state='missing'; $detail="Project themed resource '$pathValue' was not found." }
                    }
                    $entry = [pscustomobject]@{ file=$relative; control=[string]$node.id; attribute=[string]$attribute.Name
                        expression=$match.Value; tag=$tag; kind=$kind; path=$pathValue; state=$state; detail=$detail }
                    [void]$bindings.Add($entry)
                    if ($state -in @('mapping-path-invalid','runtime-unresolved','runtime-missing','unmapped','missing')) {
                        [void]$findings.Add([pscustomobject]@{ severity=if($state -eq 'unmapped'){'warning'}else{'error'}
                            code="binding-$state"; file=$relative; control=[string]$node.id; attribute=[string]$attribute.Name
                            expression=$match.Value; message=$detail })
                    }
                }
            }
        }
    }
    $errors = @($findings | Where-Object { $_.severity -eq 'error' }); $warnings = @($findings | Where-Object { $_.severity -eq 'warning' })
    [pscustomobject]@{ status='checked'; project=$projectDisplayName; binding_count=$bindings.Count
        bindings=@($bindings.ToArray()); error_count=$errors.Count; warning_count=$warnings.Count
        findings=@($findings.ToArray()); valid=($errors.Count -eq 0); readonly=$true
        note='Static expression/reference check only; live HMI Server symbol resolution was not executed.' }
}

function Get-TcHmiInternalSymbols {
    param([Parameter(Mandatory)]$Dte, [string]$ProjectName = '')
    $project = Get-TcHmiProjectObject $Dte $ProjectName
    $root = Get-TcHmiProjectPath $project
    $configPath = Join-Path $root 'Properties\tchmiconfig.json'
    $config = [IO.File]::ReadAllText($configPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
    $symbols = New-Object System.Collections.ArrayList
    foreach ($property in @($config.symbols.internal.PSObject.Properties)) {
        $item = $property.Value
        [void]$symbols.Add([pscustomobject]@{
            name=[string]$property.Name; type=[string]$item.type; value=$item.value
            persist=if($item.PSObject.Properties.Name -contains 'persist'){[bool]$item.persist}else{$false}
            readonly=if($item.PSObject.Properties.Name -contains 'readonly'){[bool]$item.readonly}else{$false}
        })
    }
    [pscustomobject]@{ status='ok'; project=Get-TcHmiResolvedName $project
        symbol_count=$symbols.Count; symbols=@($symbols.ToArray()); readonly=$true }
}

function Set-TcHmiInternalSymbol {
    param([Parameter(Mandatory)]$Dte, [string]$ProjectName = '',
          [Parameter(Mandatory)][string]$Name,
          [ValidateSet('upsert','remove')][string]$Action = 'upsert',
          [object]$Settings = $null, [bool]$Apply = $false)
    if (-not ($Name -match '^[A-Za-z_][A-Za-z0-9_.-]*$')) { throw "Invalid internal symbol name '$Name'." }
    $project = Get-TcHmiProjectObject $Dte $ProjectName
    try { $Dte.ExecuteCommand('File.SaveAll') } catch { }
    $projectDisplayName = Get-TcHmiResolvedName $project
    $projectFile = Get-TcHmiResolvedFullName $project
    $root = Get-TcHmiProjectPath $project
    $configPath = Join-Path $root 'Properties\tchmiconfig.json'
    $original = [IO.File]::ReadAllText($configPath, [Text.Encoding]::UTF8)
    $config = $original | ConvertFrom-Json
    $items = [ordered]@{}
    foreach ($property in @($config.symbols.internal.PSObject.Properties)) { $items[[string]$property.Name] = $property.Value }
    $before = if ($items.Contains($Name)) { $items[$Name] } else { $null }
    if ($Action -eq 'remove') {
        if ($null -eq $before) { throw "Internal HMI symbol '$Name' does not exist." }
        $items.Remove($Name)
        $after = $null
    } else {
        $type = if ($null -ne $Settings -and $Settings.PSObject.Properties.Name -contains 'type') { [string]$Settings.type } elseif ($null -ne $before) { [string]$before.type } else { '' }
        if (-not $type.StartsWith('tchmi:', [StringComparison]::Ordinal)) { throw "Internal symbol type must be a tchmi: schema reference; got '$type'." }
        $persist = if ($null -ne $Settings -and $Settings.PSObject.Properties.Name -contains 'persist') { [bool]$Settings.persist } elseif ($null -ne $before -and $before.PSObject.Properties.Name -contains 'persist') { [bool]$before.persist } else { $false }
        $readOnly = if ($null -ne $Settings -and $Settings.PSObject.Properties.Name -contains 'readonly') { [bool]$Settings.readonly } elseif ($null -ne $before -and $before.PSObject.Properties.Name -contains 'readonly') { [bool]$before.readonly } else { $false }
        $value = if ($null -ne $Settings -and $Settings.PSObject.Properties.Name -contains 'value') { $Settings.value } elseif ($null -ne $before) { $before.value } else { $null }
        $after = [pscustomobject][ordered]@{ value=$value; type=$type; persist=$persist; readonly=$readOnly }
        $items[$Name] = $after
    }
    $config.symbols.internal = [pscustomobject]$items
    $updated = $config | ConvertTo-Json -Depth 100
    if (-not $Apply) {
        return [pscustomobject]@{ status='preview'; project=$projectDisplayName; action=$Action
            symbol=$Name; before=$before; after=$after; apply_required=$true }
    }
    try {
        # The HMI custom project system persists its in-memory configuration
        # while being unloaded.  Write tchmiconfig only after Remove(),
        # otherwise that unload save can silently restore the old symbols.
        $Dte.Solution.Remove($project)
        [IO.File]::WriteAllText($configPath, $updated, (New-Object Text.UTF8Encoding($false)))
        $project = $Dte.Solution.AddFromFile($projectFile, $false)
        if ($null -eq $project) { throw 'XAE did not reload the HMI project.' }
        try { $Dte.ExecuteCommand('File.SaveAll') } catch { }
    }
    catch {
        try {
            $open = @(Get-TcHmiProjectObjects $Dte)
            foreach ($candidate in $open) {
                if ((Get-TcHmiResolvedFullName $candidate) -ieq $projectFile) { $Dte.Solution.Remove($candidate); break }
            }
        } catch { }
        [IO.File]::WriteAllText($configPath, $original, (New-Object Text.UTF8Encoding($false)))
        try { $Dte.Solution.AddFromFile($projectFile, $false) | Out-Null } catch { }
        throw "Internal HMI symbol update failed and configuration was rolled back: $($_.Exception.Message)"
    }
    $readback = Get-TcHmiInternalSymbols $Dte $projectFile
    $matches = @($readback.symbols | Where-Object { $_.name -ceq $Name })
    $verified = if ($Action -eq 'remove') { $matches.Count -eq 0 } else {
        $matches.Count -eq 1 -and [string]$matches[0].type -ceq [string]$after.type -and
        [bool]$matches[0].persist -eq [bool]$after.persist -and [bool]$matches[0].readonly -eq [bool]$after.readonly
    }
    if (-not $verified) { throw "Internal HMI symbol '$Name' write could not be verified." }
    [pscustomobject]@{ status='applied'; project=$projectDisplayName; action=$Action; symbol=$Name
        verified=$true; readback=if($matches.Count){$matches[0]}else{$null}; load_mode='config-update-and-com-reload' }
}

function Get-TcHmiLocalizations {
    param([Parameter(Mandatory)]$Dte, [string]$ProjectName = '')
    $project = Get-TcHmiProjectObject $Dte $ProjectName
    $root = Get-TcHmiProjectPath $project
    $configPath = Join-Path $root 'Properties\tchmiconfig.json'
    $config = [IO.File]::ReadAllText($configPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
    $languages = New-Object System.Collections.ArrayList
    $allKeys = [ordered]@{}
    foreach ($language in @($config.languages.PSObject.Properties)) {
        $locale = [string]$language.Name
        $paths = @($language.Value)
        $files = New-Object System.Collections.ArrayList
        $merged = [ordered]@{}
        foreach ($relativeValue in $paths) {
            $relative = [string]$relativeValue
            $path = Resolve-TcHmiFilePath $root $relative @('.localization') -RequireRegistered
            if (-not [IO.File]::Exists($path)) {
                [void]$files.Add([pscustomobject]@{ path=$relative; exists=$false; locale=''; key_count=0 })
                continue
            }
            try { $document = [IO.File]::ReadAllText($path, [Text.Encoding]::UTF8) | ConvertFrom-Json }
            catch { throw "Invalid HMI localization file '$relative': $($_.Exception.Message)" }
            foreach ($property in @($document.localizedText.PSObject.Properties)) {
                $merged[[string]$property.Name] = $property.Value
                $allKeys[[string]$property.Name] = $true
            }
            [void]$files.Add([pscustomobject]@{
                path=$relative; exists=$true; locale=[string]$document.locale
                key_count=@($document.localizedText.PSObject.Properties).Count
            })
        }
        [void]$languages.Add([pscustomobject]@{
            locale=$locale; files=@($files.ToArray()); key_count=$merged.Count
            texts=[pscustomobject]$merged
        })
    }
    $keyRows = New-Object System.Collections.ArrayList
    foreach ($key in @($allKeys.Keys | Sort-Object)) {
        $missing = New-Object System.Collections.ArrayList
        foreach ($language in @($languages.ToArray())) {
            if ($language.texts.PSObject.Properties.Name -cnotcontains $key) { [void]$missing.Add($language.locale) }
        }
        [void]$keyRows.Add([pscustomobject]@{ key=$key; missing_locales=@($missing.ToArray()) })
    }
    [pscustomobject]@{
        status='ok'; project=Get-TcHmiResolvedName $project
        fallback=[string]$config.languageFallback; language_count=$languages.Count
        key_count=$allKeys.Count; languages=@($languages.ToArray()); keys=@($keyRows.ToArray())
        readonly=$true
    }
}

function Set-TcHmiLocalization {
    param([Parameter(Mandatory)]$Dte, [string]$ProjectName = '',
          [Parameter(Mandatory)][string]$Key,
          [ValidateSet('upsert','remove')][string]$Action = 'upsert',
          [object]$Values = $null, [bool]$Apply = $false)
    if (-not ($Key -match '^[A-Za-z_][A-Za-z0-9_.-]*$')) { throw "Invalid localization key '$Key'." }
    $project = Get-TcHmiProjectObject $Dte $ProjectName
    try { $Dte.ExecuteCommand('File.SaveAll') } catch { }
    $projectDisplayName = Get-TcHmiResolvedName $project
    $projectFile = Get-TcHmiResolvedFullName $project
    $root = Get-TcHmiProjectPath $project
    $configPath = Join-Path $root 'Properties\tchmiconfig.json'
    $config = [IO.File]::ReadAllText($configPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
    $available = @($config.languages.PSObject.Properties.Name)
    $requested = if ($null -ne $Values) { @($Values.PSObject.Properties.Name) } else { @() }
    if ($Action -eq 'upsert' -and $requested.Count -eq 0) { throw 'Localization upsert requires a values object keyed by locale.' }
    if ($Action -eq 'remove' -and $requested.Count -eq 0) { $requested = $available }
    $plans = New-Object System.Collections.ArrayList
    $originals = [ordered]@{}
    foreach ($locale in $requested) {
        if ($available -cnotcontains $locale) { throw "Localization locale '$locale' is not registered in tchmiconfig.json." }
        $languageProperty = $config.languages.PSObject.Properties[$locale]
        $paths = @($languageProperty.Value)
        if ($paths.Count -eq 0) { throw "Localization locale '$locale' has no registered file." }
        # The last registered file wins according to the 1.12 Framework schema.
        $relative = [string]$paths[$paths.Count - 1]
        $path = Resolve-TcHmiFilePath $root $relative @('.localization') -RequireRegistered
        if (-not [IO.File]::Exists($path)) { throw "Localization file '$relative' does not exist." }
        $original = [IO.File]::ReadAllText($path, [Text.Encoding]::UTF8)
        $originals[$path] = $original
        try { $document = $original | ConvertFrom-Json } catch { throw "Invalid HMI localization file '$relative': $($_.Exception.Message)" }
        $texts = [ordered]@{}
        foreach ($property in @($document.localizedText.PSObject.Properties)) { $texts[[string]$property.Name] = $property.Value }
        $beforeExists = $texts.Contains($Key)
        $before = if ($beforeExists) { $texts[$Key] } else { $null }
        if ($Action -eq 'remove') { [void]$texts.Remove($Key); $after = $null }
        else { $after = $Values.PSObject.Properties[$locale].Value; $texts[$Key] = $after }
        $document.localizedText = [pscustomobject]$texts
        $updated = $document | ConvertTo-Json -Depth 100
        [void]$plans.Add([pscustomobject]@{
            locale=$locale; file=$relative; path=$path; before_exists=$beforeExists
            before=$before; after=$after; text=$updated
        })
    }
    if (-not $Apply) {
        return [pscustomobject]@{ status='preview'; project=$projectDisplayName; action=$Action
            key=$Key; changes=@($plans | Select-Object locale,file,before_exists,before,after); apply_required=$true }
    }
    try {
        $Dte.Solution.Remove($project)
        foreach ($plan in $plans) {
            [IO.File]::WriteAllText([string]$plan.path, [string]$plan.text, (New-Object Text.UTF8Encoding($false)))
        }
        $project = $Dte.Solution.AddFromFile($projectFile, $false)
        if ($null -eq $project) { throw 'XAE did not reload the HMI project.' }
        try { $Dte.ExecuteCommand('File.SaveAll') } catch { }
    } catch {
        try {
            foreach ($candidate in @(Get-TcHmiProjectObjects $Dte)) {
                if ((Get-TcHmiResolvedFullName $candidate) -ieq $projectFile) { $Dte.Solution.Remove($candidate); break }
            }
        } catch { }
        foreach ($path in $originals.Keys) {
            [IO.File]::WriteAllText($path, [string]$originals[$path], (New-Object Text.UTF8Encoding($false)))
        }
        try { $Dte.Solution.AddFromFile($projectFile, $false) | Out-Null } catch { }
        throw "HMI localization update failed and files were rolled back: $($_.Exception.Message)"
    }
    $readback = Get-TcHmiLocalizations $Dte $projectFile
    $verified = $true
    foreach ($plan in $plans) {
        $language = @($readback.languages | Where-Object { $_.locale -ceq $plan.locale })
        $property = if ($language.Count -eq 1) { $language[0].texts.PSObject.Properties[$Key] } else { $null }
        if ($Action -eq 'remove') { if ($null -ne $property) { $verified = $false } }
        elseif ($null -eq $property -or (($property.Value | ConvertTo-Json -Compress) -cne ($plan.after | ConvertTo-Json -Compress))) { $verified = $false }
    }
    if (-not $verified) { throw "HMI localization key '$Key' write could not be verified." }
    [pscustomobject]@{ status='applied'; project=$projectDisplayName; action=$Action; key=$Key
        verified=$true; locales=@($requested); load_mode='file-update-and-com-reload' }
}

function Get-TcHmiThemes {
    param([Parameter(Mandatory)]$Dte, [string]$ProjectName = '')
    $project = Get-TcHmiProjectObject $Dte $ProjectName
    $root = Get-TcHmiProjectPath $project
    $configPath = Join-Path $root 'Properties\tchmiconfig.json'
    $config = [IO.File]::ReadAllText($configPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
    $themes = New-Object System.Collections.ArrayList
    foreach ($property in @($config.themes.PSObject.Properties)) {
        $resources = New-Object System.Collections.ArrayList
        foreach ($resource in @($property.Value.resources)) {
            $path = Resolve-TcHmiFilePath $root ([string]$resource.name) -RequireRegistered
            [void]$resources.Add([pscustomobject]@{
                name=[string]$resource.name; type=[string]$resource.type
                description=[string]$resource.description; exists=[IO.File]::Exists($path)
            })
        }
        [void]$themes.Add([pscustomobject]@{
            name=[string]$property.Name; active=([string]$property.Name -ceq [string]$config.activeTheme)
            resources=@($resources.ToArray())
        })
    }
    $symbols = New-Object System.Collections.ArrayList
    foreach ($property in @($config.symbols.themedResources.PSObject.Properties)) {
        $item = $property.Value
        $missing = New-Object System.Collections.ArrayList
        foreach ($theme in @($themes.ToArray())) {
            if ($item.values.PSObject.Properties.Name -cnotcontains $theme.name) { [void]$missing.Add($theme.name) }
        }
        [void]$symbols.Add([pscustomobject]@{
            name=[string]$property.Name; type=[string]$item.type; description=[string]$item.description
            values=$item.values; missing_themes=@($missing.ToArray())
        })
    }
    [pscustomobject]@{ status='ok'; project=Get-TcHmiResolvedName $project
        active_theme=[string]$config.activeTheme; theme_count=$themes.Count; themes=@($themes.ToArray())
        resource_count=$symbols.Count; themed_resources=@($symbols.ToArray()); readonly=$true }
}

function Set-TcHmiThemedResource {
    param([Parameter(Mandatory)]$Dte, [string]$ProjectName = '',
          [Parameter(Mandatory)][string]$Name,
          [ValidateSet('upsert','remove')][string]$Action = 'upsert',
          [object]$Settings = $null, [bool]$Apply = $false)
    if (-not ($Name -match '^[A-Za-z_][A-Za-z0-9_.-]*$')) { throw "Invalid themed resource name '$Name'." }
    $project = Get-TcHmiProjectObject $Dte $ProjectName
    try { $Dte.ExecuteCommand('File.SaveAll') } catch { }
    $projectDisplayName = Get-TcHmiResolvedName $project
    $projectFile = Get-TcHmiResolvedFullName $project
    $root = Get-TcHmiProjectPath $project
    $configPath = Join-Path $root 'Properties\tchmiconfig.json'
    $original = [IO.File]::ReadAllText($configPath, [Text.Encoding]::UTF8)
    $config = $original | ConvertFrom-Json
    $items = [ordered]@{}
    foreach ($property in @($config.symbols.themedResources.PSObject.Properties)) { $items[[string]$property.Name] = $property.Value }
    $before = if ($items.Contains($Name)) { $items[$Name] } else { $null }
    if ($Action -eq 'remove') {
        if ($null -eq $before) { throw "Themed resource '$Name' does not exist." }
        [void]$items.Remove($Name); $after = $null
    } else {
        $type = if ($null -ne $Settings -and $Settings.PSObject.Properties.Name -contains 'type') { [string]$Settings.type } elseif ($null -ne $before) { [string]$before.type } else { '' }
        if (-not $type.StartsWith('tchmi:', [StringComparison]::Ordinal)) { throw "Themed resource type must be a tchmi: schema reference; got '$type'." }
        $description = if ($null -ne $Settings -and $Settings.PSObject.Properties.Name -contains 'description') { [string]$Settings.description } elseif ($null -ne $before) { [string]$before.description } else { '' }
        $values = [ordered]@{}
        if ($null -ne $before -and $null -ne $before.values) {
            foreach ($property in @($before.values.PSObject.Properties)) { $values[[string]$property.Name] = $property.Value }
        }
        if ($null -ne $Settings -and $Settings.PSObject.Properties.Name -contains 'values') {
            foreach ($property in @($Settings.values.PSObject.Properties)) {
                if ($config.themes.PSObject.Properties.Name -cnotcontains [string]$property.Name) {
                    throw "Theme '$($property.Name)' is not registered in tchmiconfig.json."
                }
                $values[[string]$property.Name] = $property.Value
            }
        }
        $after = [pscustomobject][ordered]@{ type=$type; description=$description; values=[pscustomobject]$values }
        $items[$Name] = $after
    }
    $config.symbols.themedResources = [pscustomobject]$items
    $updated = $config | ConvertTo-Json -Depth 100
    if (-not $Apply) {
        return [pscustomobject]@{ status='preview'; project=$projectDisplayName; action=$Action
            resource=$Name; before=$before; after=$after; apply_required=$true }
    }
    try {
        $Dte.Solution.Remove($project)
        [IO.File]::WriteAllText($configPath, $updated, (New-Object Text.UTF8Encoding($false)))
        $project = $Dte.Solution.AddFromFile($projectFile, $false)
        if ($null -eq $project) { throw 'XAE did not reload the HMI project.' }
        try { $Dte.ExecuteCommand('File.SaveAll') } catch { }
    } catch {
        try {
            foreach ($candidate in @(Get-TcHmiProjectObjects $Dte)) {
                if ((Get-TcHmiResolvedFullName $candidate) -ieq $projectFile) { $Dte.Solution.Remove($candidate); break }
            }
        } catch { }
        [IO.File]::WriteAllText($configPath, $original, (New-Object Text.UTF8Encoding($false)))
        try { $Dte.Solution.AddFromFile($projectFile, $false) | Out-Null } catch { }
        throw "HMI themed resource update failed and configuration was rolled back: $($_.Exception.Message)"
    }
    $readback = Get-TcHmiThemes $Dte $projectFile
    $matches = @($readback.themed_resources | Where-Object { $_.name -ceq $Name })
    $verified = if ($Action -eq 'remove') { $matches.Count -eq 0 } else { $matches.Count -eq 1 -and [string]$matches[0].type -ceq [string]$after.type }
    if (-not $verified) { throw "HMI themed resource '$Name' write could not be verified." }
    [pscustomobject]@{ status='applied'; project=$projectDisplayName; action=$Action; resource=$Name
        verified=$true; readback=if($matches.Count){$matches[0]}else{$null}; load_mode='config-update-and-com-reload' }
}

function Set-TcHmiActiveTheme {
    param([Parameter(Mandatory)]$Dte, [string]$ProjectName = '',
          [Parameter(Mandatory)][string]$Theme, [bool]$Apply = $false)
    $project = Get-TcHmiProjectObject $Dte $ProjectName
    try { $Dte.ExecuteCommand('File.SaveAll') } catch { }
    $projectDisplayName = Get-TcHmiResolvedName $project
    $projectFile = Get-TcHmiResolvedFullName $project
    $root = Get-TcHmiProjectPath $project
    $configPath = Join-Path $root 'Properties\tchmiconfig.json'
    $original = [IO.File]::ReadAllText($configPath, [Text.Encoding]::UTF8)
    $config = $original | ConvertFrom-Json
    if ($config.themes.PSObject.Properties.Name -cnotcontains $Theme) { throw "Theme '$Theme' is not registered in tchmiconfig.json." }
    $before = [string]$config.activeTheme; $config.activeTheme = $Theme
    $updated = $config | ConvertTo-Json -Depth 100
    if (-not $Apply) {
        return [pscustomobject]@{ status='preview'; project=$projectDisplayName; before=$before
            after=$Theme; apply_required=$true }
    }
    try {
        $Dte.Solution.Remove($project)
        [IO.File]::WriteAllText($configPath, $updated, (New-Object Text.UTF8Encoding($false)))
        $project = $Dte.Solution.AddFromFile($projectFile, $false)
        if ($null -eq $project) { throw 'XAE did not reload the HMI project.' }
        try { $Dte.ExecuteCommand('File.SaveAll') } catch { }
    } catch {
        [IO.File]::WriteAllText($configPath, $original, (New-Object Text.UTF8Encoding($false)))
        try { $Dte.Solution.AddFromFile($projectFile, $false) | Out-Null } catch { }
        throw "HMI active theme update failed and configuration was rolled back: $($_.Exception.Message)"
    }
    $readback = Get-TcHmiThemes $Dte $projectFile
    if ([string]$readback.active_theme -cne $Theme) { throw "HMI active theme '$Theme' write could not be verified." }
    [pscustomobject]@{ status='applied'; project=$projectDisplayName; before=$before; after=$Theme
        verified=$true; load_mode='config-update-and-com-reload' }
}

function Get-TcHmiUserControls {
    param([Parameter(Mandatory)]$Dte, [string]$ProjectName = '')
    $project = Get-TcHmiProjectObject $Dte $ProjectName
    $root = Get-TcHmiProjectPath $project
    $configPath = Join-Path $root 'Properties\tchmiconfig.json'
    $config = [IO.File]::ReadAllText($configPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
    $items = New-Object System.Collections.ArrayList
    foreach ($entry in @($config.userControls)) {
        $relative = [string]$entry.url
        $path = Resolve-TcHmiFilePath $root $relative @('.usercontrol') -RequireRegistered
        $baseName = [IO.Path]::GetFileNameWithoutExtension($path)
        # TE2000 stores the dependent parameter document beside the markup
        # (for example Foo.usercontrol + Foo.usercontrol.json).  The project
        # system only displays it as a child node; there is no physical
        # Foo.usercontrol directory.
        $parameterRelative = "$relative.json"
        $parameterPath = Resolve-TcHmiFilePath $root $parameterRelative @('.json') -RequireRegistered
        $controls = New-Object System.Collections.ArrayList
        $markupValid = $false
        if ([IO.File]::Exists($path)) {
            try {
                [xml]$markup = [IO.File]::ReadAllText($path, [Text.Encoding]::UTF8)
                foreach ($node in @($markup.SelectNodes('//*[@data-tchmi-type]'))) {
                    [void]$controls.Add([pscustomobject]@{ id=[string]$node.id; type=[string]$node.'data-tchmi-type' })
                }
                $markupValid = $true
            } catch { }
        }
        $parameterDocument = $null
        if ([IO.File]::Exists($parameterPath)) {
            try { $parameterDocument = [IO.File]::ReadAllText($parameterPath, [Text.Encoding]::UTF8) | ConvertFrom-Json } catch { }
        }
        $parameterItems = [object[]]@()
        if ($null -ne $parameterDocument) { $parameterItems = [object[]]@($parameterDocument.parameters) }
        [void]$items.Add([pscustomobject]@{
            name=$baseName; url=$relative; exists=[IO.File]::Exists($path); markup_valid=$markupValid
            parameter_file=$parameterRelative; parameter_file_exists=[IO.File]::Exists($parameterPath)
            parameter_count=if($null -ne $parameterDocument){@($parameterDocument.parameters).Count}else{0}
            parameters=$parameterItems
            control_count=$controls.Count; controls=[object[]]$controls.ToArray()
        })
    }
    [pscustomobject]@{ status='ok'; project=Get-TcHmiResolvedName $project
        user_control_count=$items.Count; user_controls=@($items.ToArray()); readonly=$true }
}

function Test-TcHmiUserControlParameter {
    param([Parameter(Mandatory)]$Parameter)
    $required = @('name','displayName','visible','type','category','readOnly','bindable','heritable')
    foreach ($field in $required) {
        if ($Parameter.PSObject.Properties.Name -cnotcontains $field) { throw "UserControl parameter is missing required field '$field'." }
    }
    if (-not ([string]$Parameter.name).StartsWith('data-tchmi-', [StringComparison]::Ordinal)) {
        throw "UserControl parameter name must start with data-tchmi-; got '$([string]$Parameter.name)'."
    }
    if (-not ([string]$Parameter.type).StartsWith('tchmi:', [StringComparison]::Ordinal)) {
        throw "UserControl parameter type must be a tchmi: schema reference; got '$([string]$Parameter.type)'."
    }
}

function New-TcHmiUserControl {
    param([Parameter(Mandatory)]$Dte, [string]$ProjectName = '',
          [Parameter(Mandatory)][string]$Name, [object[]]$Parameters = @(),
          [object[]]$Controls = @(), [bool]$Apply = $false)
    $baseName = [IO.Path]::GetFileNameWithoutExtension($Name)
    if (-not ($baseName -match '^[A-Za-z_][A-Za-z0-9_]*$') -or $Name.IndexOfAny(@([char]'\',[char]'/')) -ge 0) {
        throw 'HMI UserControl name must be a single identifier using letters, digits and underscore.'
    }
    $parameterItems = New-Object System.Collections.ArrayList
    foreach ($parameter in @($Parameters)) {
        Test-TcHmiUserControlParameter $parameter
        [void]$parameterItems.Add($parameter)
    }
    $project = Get-TcHmiProjectObject $Dte $ProjectName
    $projectDisplayName = Get-TcHmiResolvedName $project
    $projectFile = Get-TcHmiResolvedFullName $project
    $root = Get-TcHmiProjectPath $project
    $relative = "$baseName.usercontrol"
    $path = Resolve-TcHmiFilePath $root $relative @('.usercontrol')
    $parameterRelative = "$relative.json"
    $parameterPath = Resolve-TcHmiFilePath $root $parameterRelative @('.json')
    if ([IO.File]::Exists($path) -or [IO.File]::Exists($parameterPath)) { throw "HMI UserControl already exists: $relative" }

    $document = New-Object System.Xml.XmlDocument
    $rootNode = $document.CreateElement('div')
    $rootNode.SetAttribute('id', "${baseName}_1")
    $rootNode.SetAttribute('data-tchmi-type', 'TcHmi.Controls.System.TcHmiUserControl')
    foreach ($pair in @{'data-tchmi-top'='0';'data-tchmi-left'='0';'data-tchmi-width'='100';'data-tchmi-height'='100';'data-tchmi-width-unit'='%';'data-tchmi-height-unit'='%';'data-tchmi-creator-viewport-width'='500';'data-tchmi-creator-viewport-height'='500'}.GetEnumerator()) {
        $rootNode.SetAttribute([string]$pair.Key, [string]$pair.Value)
    }
    [void]$document.AppendChild($rootNode)
    $ids = @("${baseName}_1")
    foreach ($control in @($Controls)) {
        $id = [string]$control.id; $type = [string]$control.type
        if (-not ($id -match '^[A-Za-z_][A-Za-z0-9_]*$') -or $ids -contains $id) { throw "Invalid or duplicate HMI control id '$id'." }
        if (-not $type.StartsWith('TcHmi.Controls.', [StringComparison]::Ordinal)) { throw "Invalid HMI control type '$type'." }
        $ids += $id
        $node = $document.CreateElement('div'); $node.SetAttribute('id',$id); $node.SetAttribute('data-tchmi-type',$type)
        foreach ($attribute in @($control.attributes.PSObject.Properties)) {
            if (-not ([string]$attribute.Name).StartsWith('data-tchmi-', [StringComparison]::OrdinalIgnoreCase)) { throw "Only data-tchmi-* control attributes are allowed; got '$($attribute.Name)'." }
            $node.SetAttribute([string]$attribute.Name, [string]$attribute.Value)
        }
        [void]$rootNode.AppendChild($node)
    }
    $markup = Convert-TcHmiXmlText $document
    $configPath = Join-Path $root 'Properties\tchmiconfig.json'
    $configText = [IO.File]::ReadAllText($configPath, [Text.Encoding]::UTF8)
    $config = $configText | ConvertFrom-Json
    $schema = ([string]$config.'$schema').Replace('TchmiConfig.Schema.json','UserControlConfig.Schema.json')
    $parameterDocument = [pscustomobject][ordered]@{
        '$schema'=$schema
        parameters=[object[]]$parameterItems.ToArray()
    }
    $parameterText = $parameterDocument | ConvertTo-Json -Depth 100
    $config.userControls = @($config.userControls) + @([pscustomobject]@{ url=$relative })
    $updatedConfig = $config | ConvertTo-Json -Depth 100
    $projectText = [IO.File]::ReadAllText($projectFile, [Text.Encoding]::UTF8)
    $closing = $projectText.LastIndexOf('</ItemGroup>', [StringComparison]::OrdinalIgnoreCase)
    if ($closing -lt 0) { throw 'The HMI project has no ItemGroup insertion point.' }
    $entries = "    <Content Include=`"$relative`">`r`n      <SubType>Content</SubType>`r`n      <Visible>true</Visible>`r`n    </Content>`r`n" +
               "    <Content Include=`"$parameterRelative`">`r`n      <SubType>Content</SubType>`r`n      <DependentUpon>$relative</DependentUpon>`r`n      <Visible>true</Visible>`r`n    </Content>`r`n  "
    $updatedProject = $projectText.Insert($closing, $entries)
    Assert-TcHmiWriteContract $projectFile $markup $Apply
    if (-not $Apply) {
        return [pscustomobject]@{ status='preview'; project=$projectDisplayName; name=$baseName; file=$relative
            project_file=$projectFile
            parameter_file=$parameterRelative; parameter_count=$parameterItems.Count; control_count=@($Controls).Count
            markup=$markup; parameter_config=$parameterDocument; apply_required=$true }
    }
    try {
        $Dte.Solution.Remove($project)
        [IO.File]::WriteAllText($path, $markup, (New-Object Text.UTF8Encoding($false)))
        [IO.File]::WriteAllText($parameterPath, $parameterText, (New-Object Text.UTF8Encoding($false)))
        [IO.File]::WriteAllText($configPath, $updatedConfig, (New-Object Text.UTF8Encoding($false)))
        [IO.File]::WriteAllText($projectFile, $updatedProject, (New-Object Text.UTF8Encoding($true)))
        $project = $Dte.Solution.AddFromFile($projectFile, $false)
        if ($null -eq $project) { throw 'XAE did not reload the HMI project.' }
        try { $Dte.ExecuteCommand('File.SaveAll') } catch { }
    } catch {
        try { if ([IO.File]::Exists($path)) { [IO.File]::Delete($path) } } catch { }
        try { if ([IO.File]::Exists($parameterPath)) { [IO.File]::Delete($parameterPath) } } catch { }
        [IO.File]::WriteAllText($configPath, $configText, (New-Object Text.UTF8Encoding($false)))
        [IO.File]::WriteAllText($projectFile, $projectText, (New-Object Text.UTF8Encoding($true)))
        try { $Dte.Solution.AddFromFile($projectFile, $false) | Out-Null } catch { }
        throw "HMI UserControl creation failed and project files were rolled back: $($_.Exception.Message)"
    }
    $readback = Get-TcHmiUserControls $Dte $projectFile
    $matches = @($readback.user_controls | Where-Object { $_.url -ceq $relative })
    $verified = $matches.Count -eq 1 -and $matches[0].exists -and $matches[0].parameter_file_exists
    if (-not $verified) { throw "HMI UserControl '$relative' creation could not be verified." }
    [pscustomobject]@{ status='created'; project=$projectDisplayName; name=$baseName; file=$relative
        parameter_file=$parameterRelative; verified=$true; readback=$matches[0]
        load_mode='hmiproj-config-update-and-com-reload' }
}

function Set-TcHmiUserControlParameter {
    param([Parameter(Mandatory)]$Dte, [string]$ProjectName = '',
          [Parameter(Mandatory)][string]$UserControl, [Parameter(Mandatory)][string]$Name,
          [ValidateSet('upsert','remove')][string]$Action = 'upsert',
          [object]$Settings = $null, [bool]$Apply = $false)
    $project = Get-TcHmiProjectObject $Dte $ProjectName
    try { $Dte.ExecuteCommand('File.SaveAll') } catch { }
    $projectDisplayName = Get-TcHmiResolvedName $project
    $projectFile = Get-TcHmiResolvedFullName $project
    $root = Get-TcHmiProjectPath $project
    $baseName = [IO.Path]::GetFileNameWithoutExtension($UserControl)
    $relative = "$baseName.usercontrol"
    $parameterRelative = "$relative.json"
    $parameterPath = Resolve-TcHmiFilePath $root $parameterRelative @('.json') -RequireRegistered
    if (-not [IO.File]::Exists($parameterPath)) { throw "UserControl parameter file not found: $parameterRelative" }
    $original = [IO.File]::ReadAllText($parameterPath, [Text.Encoding]::UTF8)
    $document = $original | ConvertFrom-Json
    $items = New-Object System.Collections.ArrayList
    foreach ($item in @($document.parameters)) { [void]$items.Add($item) }
    $matches = @($items | Where-Object { [string]$_.name -ceq $Name })
    $before = if ($matches.Count -eq 1) { $matches[0] } else { $null }
    if ($matches.Count -gt 1) { throw "UserControl parameter '$Name' is duplicated." }
    if ($Action -eq 'remove') {
        if ($null -eq $before) { throw "UserControl parameter '$Name' does not exist." }
        $items = New-Object System.Collections.ArrayList
        foreach ($item in @($document.parameters | Where-Object { [string]$_.name -cne $Name })) { [void]$items.Add($item) }
        $after = $null
    } else {
        if ($null -eq $Settings) { throw 'UserControl parameter upsert requires settings.' }
        if ($Settings.PSObject.Properties.Name -cnotcontains 'name') { $Settings | Add-Member -NotePropertyName name -NotePropertyValue $Name }
        if ([string]$Settings.name -cne $Name) { throw 'UserControl parameter settings.name must match name.' }
        Test-TcHmiUserControlParameter $Settings
        $after = $Settings
        if ($null -ne $before) {
            for ($i=0; $i -lt $items.Count; $i++) { if ([string]$items[$i].name -ceq $Name) { $items[$i]=$after; break } }
        } else { [void]$items.Add($after) }
    }
    $document.parameters = @($items.ToArray())
    $updated = $document | ConvertTo-Json -Depth 100
    if (-not $Apply) {
        return [pscustomobject]@{ status='preview'; project=$projectDisplayName; user_control=$relative
            action=$Action; parameter=$Name; before=$before; after=$after; apply_required=$true }
    }
    try {
        $Dte.Solution.Remove($project)
        [IO.File]::WriteAllText($parameterPath, $updated, (New-Object Text.UTF8Encoding($false)))
        $project = $Dte.Solution.AddFromFile($projectFile, $false)
        if ($null -eq $project) { throw 'XAE did not reload the HMI project.' }
        try { $Dte.ExecuteCommand('File.SaveAll') } catch { }
    } catch {
        [IO.File]::WriteAllText($parameterPath, $original, (New-Object Text.UTF8Encoding($false)))
        try { $Dte.Solution.AddFromFile($projectFile, $false) | Out-Null } catch { }
        throw "HMI UserControl parameter update failed and file was rolled back: $($_.Exception.Message)"
    }
    $readback = $null; $found = [object[]]@(); $verified = $false; $verifyAttempts = 0
    for ($attempt = 1; $attempt -le 10; $attempt++) {
        $verifyAttempts = $attempt
        $readback = Get-TcHmiUserControls $Dte $projectFile
        $control = [object[]]@($readback.user_controls | Where-Object { $_.url -ceq $relative })
        if ($control.Count -eq 1) {
            $found = [object[]]@($control[0].parameters | Where-Object { [string]$_.name -ceq $Name })
        } else { $found = [object[]]@() }
        $verified = if($Action -eq 'remove'){$found.Count -eq 0}else{$found.Count -eq 1}
        if ($verified) { break }
        Start-Sleep -Milliseconds 200
    }
    if (-not $verified) { throw "HMI UserControl parameter '$Name' write could not be verified." }
    [pscustomobject]@{ status='applied'; project=$projectDisplayName; user_control=$relative
        action=$Action; parameter=$Name; verified=$true; verify_attempts=$verifyAttempts
        readback=if($found.Count){$found[0]}else{$null} }
}

function Remove-TcHmiUserControl {
    param([Parameter(Mandatory)]$Dte, [string]$ProjectName = '',
          [Parameter(Mandatory)][string]$UserControl, [bool]$Apply = $false)
    $project = Get-TcHmiProjectObject $Dte $ProjectName
    try { $Dte.ExecuteCommand('File.SaveAll') } catch { }
    $projectDisplayName = Get-TcHmiResolvedName $project
    $projectFile = Get-TcHmiResolvedFullName $project
    $root = Get-TcHmiProjectPath $project
    $baseName = [IO.Path]::GetFileNameWithoutExtension($UserControl)
    $relative = "$baseName.usercontrol"
    $path = Resolve-TcHmiFilePath $root $relative @('.usercontrol') -RequireRegistered
    $parameterRelative = "$relative.json"
    $parameterPath = Resolve-TcHmiFilePath $root $parameterRelative @('.json') -RequireRegistered
    if (-not [IO.File]::Exists($path)) { throw "HMI UserControl not found: $relative" }
    $configPath = Join-Path $root 'Properties\tchmiconfig.json'
    $configText = [IO.File]::ReadAllText($configPath, [Text.Encoding]::UTF8); $config = $configText | ConvertFrom-Json
    if (@($config.userControls | Where-Object { [string]$_.url -ieq $relative }).Count -ne 1) { throw "UserControl '$relative' is not registered uniquely." }
    $config.userControls = @($config.userControls | Where-Object { [string]$_.url -ine $relative })
    $updatedConfig = $config | ConvertTo-Json -Depth 100
    $projectText = [IO.File]::ReadAllText($projectFile, [Text.Encoding]::UTF8); [xml]$projectXml = $projectText
    foreach ($includeName in @($relative,$parameterRelative)) {
        $nodes = @($projectXml.SelectNodes("//*[local-name()='Content']") | Where-Object { [string]$_.Include -ieq $includeName })
        if ($nodes.Count -ne 1) { throw "UserControl file '$includeName' is not registered uniquely in .hmiproj." }
        $nodes[0].ParentNode.RemoveChild($nodes[0]) | Out-Null
    }
    $updatedProject = Convert-TcHmiXmlText $projectXml
    $backupDirectory = Join-Path $root ('.TwinCATAgent\backups\' + (Get-Date -Format 'yyyyMMdd-HHmmssfff') + '-' + $baseName + '.usercontrol')
    if (-not $Apply) {
        return [pscustomobject]@{ status='preview'; project=$projectDisplayName; user_control=$relative
            parameter_file=$parameterRelative; planned_backup=$backupDirectory; apply_required=$true }
    }
    [IO.Directory]::CreateDirectory($backupDirectory) | Out-Null
    [IO.File]::Copy($path, (Join-Path $backupDirectory ([IO.Path]::GetFileName($path))), $false)
    if ([IO.File]::Exists($parameterPath)) { [IO.File]::Copy($parameterPath, (Join-Path $backupDirectory ([IO.Path]::GetFileName($parameterPath))), $false) }
    try {
        $Dte.Solution.Remove($project)
        [IO.File]::WriteAllText($configPath, $updatedConfig, (New-Object Text.UTF8Encoding($false)))
        [IO.File]::WriteAllText($projectFile, $updatedProject, (New-Object Text.UTF8Encoding($true)))
        [IO.File]::Delete($path)
        if ([IO.File]::Exists($parameterPath)) { [IO.File]::Delete($parameterPath) }
        $project = $Dte.Solution.AddFromFile($projectFile, $false)
        if ($null -eq $project) { throw 'XAE did not reload the HMI project.' }
        try { $Dte.ExecuteCommand('File.SaveAll') } catch { }
    } catch {
        [IO.File]::WriteAllText($configPath, $configText, (New-Object Text.UTF8Encoding($false)))
        [IO.File]::WriteAllText($projectFile, $projectText, (New-Object Text.UTF8Encoding($true)))
        [IO.File]::Copy((Join-Path $backupDirectory ([IO.Path]::GetFileName($path))), $path, $true)
        $parameterBackup = Join-Path $backupDirectory ([IO.Path]::GetFileName($parameterPath))
        if ([IO.File]::Exists($parameterBackup)) { [IO.File]::Copy($parameterBackup, $parameterPath, $true) }
        try { $Dte.Solution.AddFromFile($projectFile, $false) | Out-Null } catch { }
        throw "HMI UserControl deletion failed and project files were rolled back: $($_.Exception.Message)"
    }
    $readback = Get-TcHmiUserControls $Dte $projectFile
    $verified = @($readback.user_controls | Where-Object { $_.url -ceq $relative }).Count -eq 0 -and -not [IO.File]::Exists($path)
    if (-not $verified) { throw "HMI UserControl '$relative' deletion could not be verified." }
    [pscustomobject]@{ status='deleted'; project=$projectDisplayName; user_control=$relative
        backup_directory=$backupDirectory; verified=$true; load_mode='hmiproj-config-update-and-com-reload' }
}

function Get-TcHmiFrameworkTemplateRoot {
    $candidates = @()
    if (${env:ProgramFiles(x86)}) {
        $candidates += (Join-Path ${env:ProgramFiles(x86)} 'Beckhoff\TwinCAT\Functions\TE2000-HMI-Engineering\Templates')
    }
    if ($env:ProgramFiles) {
        $candidates += (Join-Path $env:ProgramFiles 'Beckhoff\TwinCAT\Functions\TE2000-HMI-Engineering\Templates')
    }
    foreach ($candidate in $candidates) {
        if ([IO.Directory]::Exists($candidate)) { return [IO.Path]::GetFullPath($candidate) }
    }
    throw 'TwinCAT HMI Engineering templates were not found. Confirm that TE2000 Engineering is installed.'
}

function Get-TcHmiFrameworkTemplates {
    param([Parameter(Mandatory)]$Dte, [string]$ProjectName = '')
    $templateRoot = Get-TcHmiFrameworkTemplateRoot
    $project = $null
    $projectInfo = @{ framework_target=''; engineering_recent='' }
    # Template discovery must also work before the first HMI project exists.
    if ($ProjectName -or @(Get-TcHmiProjectObjects $Dte).Count -gt 0) {
        $project = Get-TcHmiProjectObject $Dte $ProjectName
        $projectInfo = Get-TcHmiProjectInfo $Dte (Get-TcHmiResolvedFullName $project)
    }
    $frameworkRoot = Join-Path $templateRoot 'ProjectTemplates\TcXaeShell\Framework'
    $definitions = @(
        [pscustomobject]@{ language='typescript'; folder='FrameworkPrj_PackagesConfig'; source='Source.ts' },
        [pscustomobject]@{ language='javascript'; folder='FrameworkPrjJs_PackagesConfig'; source='Source.js' },
        [pscustomobject]@{ language='empty'; folder='FrameworkPrjEmpty_PackagesConfig'; source='' }
    )
    $items = New-Object System.Collections.ArrayList
    foreach ($definition in $definitions) {
        $path = Join-Path $frameworkRoot $definition.folder
        [void]$items.Add([pscustomobject]@{
            language=$definition.language; path=$path; installed=[IO.Directory]::Exists($path)
            project_template=Join-Path $path 'ProjectTemplate.hmiextproj'
            manifest=Join-Path $path 'Manifest.json'; source_file=$definition.source
        })
    }
    [pscustomobject]@{
        status='ok'; project=$(if ($null -ne $project) { Get-TcHmiResolvedName $project } else { '' })
        target_framework=[string]$projectInfo.framework_target
        engineering_recent=[string]$projectInfo.engineering_recent
        template_root=$templateRoot; templates=[object[]]$items.ToArray(); readonly=$true
    }
}

function Resolve-TcHmiFrameworkProject {
    param([Parameter(Mandatory)][string]$Source)
    $full = [IO.Path]::GetFullPath($Source)
    if ([IO.File]::Exists($full)) {
        if (-not [IO.Path]::GetExtension($full).Equals('.hmiextproj', [StringComparison]::OrdinalIgnoreCase)) {
            throw "Framework source file must be .hmiextproj: $full"
        }
        return $full
    }
    if ([IO.Directory]::Exists($full)) {
        $projects = @(Get-ChildItem -LiteralPath $full -File -Filter '*.hmiextproj')
        if ($projects.Count -ne 1) { throw "Framework directory must contain exactly one .hmiextproj; found $($projects.Count)." }
        return $projects[0].FullName
    }
    throw "Framework project source does not exist: $full"
}

function Resolve-TcHmiFrameworkControl {
    param([Parameter(Mandatory)][string]$Source, [string]$Control = '')
    $projectFile = Resolve-TcHmiFrameworkProject $Source
    $root = [IO.Path]::GetDirectoryName($projectFile)
    $manifestPath = Join-Path $root 'Manifest.json'
    if (-not [IO.File]::Exists($manifestPath)) { throw "Framework Manifest.json is missing: $manifestPath" }
    try { $manifest = [IO.File]::ReadAllText($manifestPath, [Text.Encoding]::UTF8) | ConvertFrom-Json }
    catch { throw "Framework Manifest.json is invalid: $($_.Exception.Message)" }
    $matches = New-Object System.Collections.ArrayList
    foreach ($module in @($manifest.modules | Where-Object { [string]$_.type -ceq 'Control' })) {
        $baseRelative = ([string]$module.basePath).Trim('/','\').Replace('\','/')
        $descriptionRelative = ($baseRelative + '/' + [string]$module.descriptionFile).TrimStart('/')
        $descriptionPath = Resolve-TcHmiFilePath $root $descriptionRelative @('.json') -RequireRegistered
        if (-not [IO.File]::Exists($descriptionPath)) { continue }
        try { $description = [IO.File]::ReadAllText($descriptionPath, [Text.Encoding]::UTF8) | ConvertFrom-Json }
        catch { throw "Control description '$descriptionRelative' is invalid: $($_.Exception.Message)" }
        $candidate = [pscustomobject]@{
            project_file=$projectFile; project_directory=$root; manifest_path=$manifestPath
            module=$module; base_path=$baseRelative; description_relative=$descriptionRelative
            description_path=$descriptionPath; description=$description
        }
        if (-not $Control -or [string]$description.name -ieq $Control -or
            $baseRelative -ieq $Control -or [IO.Path]::GetFileName($baseRelative) -ieq $Control) {
            [void]$matches.Add($candidate)
        }
    }
    if ($matches.Count -eq 0) { throw "Framework control '$Control' was not found in Manifest.json." }
    if ($matches.Count -gt 1) { throw "Framework project contains multiple controls; specify the control name or basePath." }
    return $matches[0]
}

function Get-TcHmiFrameworkControlSource {
    param([Parameter(Mandatory)]$ResolvedControl)
    $controlRoot = [IO.Path]::GetDirectoryName([string]$ResolvedControl.description_path)
    $preferred = New-Object System.Collections.ArrayList
    foreach ($dependency in @($ResolvedControl.description.dependencyFiles)) {
        if ([IO.Path]::GetExtension([string]$dependency.name) -ieq '.js') {
            $jsPath = Resolve-TcHmiFilePath $controlRoot ([string]$dependency.name) -RequireRegistered
            $tsPath = [IO.Path]::ChangeExtension($jsPath, '.ts')
            if ([IO.File]::Exists($tsPath)) { [void]$preferred.Add($tsPath) }
            elseif ([IO.File]::Exists($jsPath)) { [void]$preferred.Add($jsPath) }
        }
    }
    if ($preferred.Count -eq 0) {
        foreach ($file in @(Get-ChildItem -LiteralPath $controlRoot -File | Where-Object { $_.Extension -in @('.ts','.js') })) {
            [void]$preferred.Add($file.FullName)
        }
    }
    $unique = @($preferred | Select-Object -Unique)
    if ($unique.Count -eq 0) { throw "No TypeScript or JavaScript source was found for control '$([string]$ResolvedControl.description.name)'." }
    if ($unique.Count -gt 1) {
        $sameName = @($unique | Where-Object { [IO.Path]::GetFileNameWithoutExtension($_) -ieq [string]$ResolvedControl.description.name })
        if ($sameName.Count -eq 1) { return $sameName[0] }
        throw "Multiple control source files were found; cannot select one safely: $($unique -join ', ')"
    }
    return $unique[0]
}

function Get-TcHmiFrameworkControlInfo {
    param([Parameter(Mandatory)][string]$Source, [string]$Control = '')
    $resolved = Resolve-TcHmiFrameworkControl $Source $Control
    $sourcePath = ''; $language = 'unknown'
    try {
        $sourcePath = Get-TcHmiFrameworkControlSource $resolved
        $language = if ([IO.Path]::GetExtension($sourcePath) -ieq '.ts') { 'typescript' } else { 'javascript' }
    } catch { }
    [pscustomobject]@{
        status='ok'; project_file=$resolved.project_file; control=[string]$resolved.description.name
        namespace=[string]$resolved.description.namespace; base=[string]$resolved.description.base
        description_file=$resolved.description_relative; source_file=$sourcePath; language=$language
        attributes=@($resolved.description.attributes); functions=@($resolved.description.functions)
        events=@($resolved.description.events); readonly=$true
    }
}

function Get-TcHmiFrameworkSetting {
    param($Settings, [string]$Name, $Default = $null)
    if ($null -ne $Settings -and $Settings.PSObject.Properties.Name -contains $Name) { return $Settings.$Name }
    return $Default
}

function ConvertTo-TcHmiFrameworkPascalName {
    param([Parameter(Mandatory)][string]$Name)
    $parts = @($Name -split '[^A-Za-z0-9]+' | Where-Object { $_ })
    if ($parts.Count -eq 0) { throw 'Framework member name is empty.' }
    $value = ($parts | ForEach-Object { $_.Substring(0,1).ToUpperInvariant() + $_.Substring(1) }) -join ''
    if (-not ($value -match '^[A-Za-z_][A-Za-z0-9_]*$')) { throw "Invalid Framework member name '$Name'." }
    return $value
}

function ConvertTo-TcHmiFrameworkHtmlName {
    param([Parameter(Mandatory)][string]$PropertyName)
    $kebab = [regex]::Replace($PropertyName, '(?<!^)([A-Z])', '-$1').ToLowerInvariant()
    return 'data-tchmi-' + $kebab
}

function Set-TcHmiFrameworkGeneratedRegion {
    param([Parameter(Mandatory)][string]$SourcePath, [Parameter(Mandatory)][string]$Kind,
          [Parameter(Mandatory)][string]$Name, [string]$Body = '', [bool]$Remove = $false)
    $text = [IO.File]::ReadAllText($SourcePath, [Text.Encoding]::UTF8)
    $begin = "// <TwinCATAgent:${Kind}:${Name}>"
    $end = "// </TwinCATAgent:${Kind}:${Name}>"
    $pattern = '(?ms)^\s*' + [regex]::Escape($begin) + '.*?^\s*' + [regex]::Escape($end) + '\r?\n?'
    $hadRegion = [regex]::IsMatch($text, $pattern)
    if ($Remove) {
        if ($hadRegion) { $text = [regex]::Replace($text, $pattern, '') }
        return [pscustomobject]@{ text=$text; had_region=$hadRegion; changed=$hadRegion }
    }
    if ($hadRegion) {
        $text = [regex]::Replace($text, $pattern, [Text.RegularExpressions.MatchEvaluator]{ param($m) $Body })
        return [pscustomobject]@{ text=$text; had_region=$true; changed=$true }
    }
    $anchor = [regex]::Match($text, '(?m)^\s*protected __elementTemplateRoot')
    if (-not $anchor.Success) {
        $anchor = [regex]::Match($text, '(?m)^(\s*)/\*\*\r?\n\s*\* (?:Raised after the control|If raised, the control object exists)')
    }
    if (-not $anchor.Success) { throw "Could not find a safe generated-code insertion point in $SourcePath." }
    $text = $text.Insert($anchor.Index, $Body)
    return [pscustomobject]@{ text=$text; had_region=$false; changed=$true }
}

function New-TcHmiFrameworkAttributeCode {
    param([Parameter(Mandatory)][string]$PropertyName, [Parameter(Mandatory)][string]$ValueKind,
          [Parameter(Mandatory)][string]$SourcePath, [bool]$ReadOnly = $false)
    $field = '__' + $PropertyName.Substring(0,1).ToLowerInvariant() + $PropertyName.Substring(1)
    $extension = [IO.Path]::GetExtension($SourcePath).ToLowerInvariant()
    $valueType = switch ($ValueKind) {
        'boolean' { 'boolean | null' }
        'number' { 'number | null' }
        'string' { 'string | null' }
        'object' { 'object | null' }
        default { 'any' }
    }
    $fieldType = if ($valueType -eq 'any') { 'any' } else { $valueType + ' | undefined' }
    $converter = switch ($ValueKind) {
        'boolean' { 'TcHmi.ValueConverter.toBoolean(valueNew)' }
        'number' { 'TcHmi.ValueConverter.toNumber(valueNew)' }
        'string' { 'TcHmi.ValueConverter.toString(valueNew)' }
        'object' { 'TcHmi.ValueConverter.toObject(valueNew)' }
        default { 'valueNew' }
    }
    if ($extension -eq '.ts') {
        $lines = New-Object System.Collections.ArrayList
        [void]$lines.Add("            // <TwinCATAgent:Attribute:$PropertyName>")
        [void]$lines.Add("            protected $field`: $fieldType;")
        if (-not $ReadOnly) {
            [void]$lines.Add('')
            [void]$lines.Add("            public set$PropertyName(valueNew: $valueType): void {")
            [void]$lines.Add("                let convertedValue = $converter;")
            [void]$lines.Add('                if (convertedValue === null) {')
            [void]$lines.Add("                    convertedValue = this.getAttributeDefaultValueInternal('$PropertyName') as $valueType;")
            [void]$lines.Add('                }')
            [void]$lines.Add("                if (convertedValue === this.$field) return;")
            [void]$lines.Add("                this.$field = convertedValue;")
            [void]$lines.Add("                TcHmi.EventProvider.raise(this.__id + '.onPropertyChanged', { propertyName: '$PropertyName' });")
            [void]$lines.Add("                this.__process$PropertyName();")
            [void]$lines.Add('            }')
        }
        [void]$lines.Add('')
        [void]$lines.Add("            public get$PropertyName(): $fieldType {")
        [void]$lines.Add("                return this.$field;")
        [void]$lines.Add('            }')
        [void]$lines.Add('')
        [void]$lines.Add("            protected __process$PropertyName(): void {")
        [void]$lines.Add('                // Apply the value to the control DOM here.')
        [void]$lines.Add('            }')
        [void]$lines.Add("            // </TwinCATAgent:Attribute:$PropertyName>")
        [void]$lines.Add('')
        return ($lines -join "`r`n") + "`r`n"
    }
    $lines = New-Object System.Collections.ArrayList
    [void]$lines.Add("                // <TwinCATAgent:Attribute:$PropertyName>")
    if (-not $ReadOnly) {
        [void]$lines.Add("                set$PropertyName(valueNew) {")
        [void]$lines.Add("                    let convertedValue = $converter;")
        [void]$lines.Add('                    if (convertedValue === null) {')
        [void]$lines.Add("                        convertedValue = this.getAttributeDefaultValueInternal('$PropertyName');")
        [void]$lines.Add('                    }')
        [void]$lines.Add("                    if (convertedValue === this.$field) return;")
        [void]$lines.Add("                    this.$field = convertedValue;")
        [void]$lines.Add("                    TcHmi.EventProvider.raise(this.__id + '.onPropertyChanged', { propertyName: '$PropertyName' });")
        [void]$lines.Add("                    this.__process$PropertyName();")
        [void]$lines.Add('                }')
    }
    [void]$lines.Add("                get$PropertyName() {")
    [void]$lines.Add("                    return this.$field;")
    [void]$lines.Add('                }')
    [void]$lines.Add("                __process$PropertyName() {")
    [void]$lines.Add('                    // Apply the value to the control DOM here.')
    [void]$lines.Add('                }')
    [void]$lines.Add("                // </TwinCATAgent:Attribute:$PropertyName>")
    [void]$lines.Add('')
    return ($lines -join "`r`n") + "`r`n"
}

function New-TcHmiFrameworkEventCode {
    param([Parameter(Mandatory)][string]$EventName, [Parameter(Mandatory)][string]$SourcePath)
    $shortName = $EventName.TrimStart('.')
    $member = if ($shortName.StartsWith('on') -and $shortName.Length -gt 2) { 'raise' + $shortName.Substring(2) } else { 'raise' + $shortName }
    if ([IO.Path]::GetExtension($SourcePath) -ieq '.ts') {
        return "            // <TwinCATAgent:Event:$shortName>`r`n" +
            "            public $member(data?: object): void {`r`n" +
            "                TcHmi.EventProvider.raise(this.__id + '$EventName', data);`r`n" +
            "            }`r`n" +
            "            // </TwinCATAgent:Event:$shortName>`r`n`r`n"
    }
    return "                // <TwinCATAgent:Event:$shortName>`r`n" +
        "                $member(data) {`r`n" +
        "                    TcHmi.EventProvider.raise(this.__id + '$EventName', data);`r`n" +
        "                }`r`n" +
        "                // </TwinCATAgent:Event:$shortName>`r`n`r`n"
}

function Set-TcHmiFrameworkAttribute {
    param([Parameter(Mandatory)][string]$Source, [string]$Control = '',
          [Parameter(Mandatory)][string]$Name,
          [ValidateSet('upsert','remove')][string]$Action = 'upsert',
          $Settings = $null, [bool]$Apply = $false)
    $resolved = Resolve-TcHmiFrameworkControl $Source $Control
    $description = $resolved.description
    $propertyName = ConvertTo-TcHmiFrameworkPascalName $Name
    $sourcePath = Get-TcHmiFrameworkControlSource $resolved
    $existing = @($description.attributes | Where-Object { [string]$_.propertyName -ieq $propertyName })
    if ($existing.Count -gt 1) { throw "Attribute property '$propertyName' is duplicated in Description.json." }
    if ($Action -eq 'remove' -and $existing.Count -eq 0) { throw "Attribute property '$propertyName' was not found." }
    $next = @($description.attributes | Where-Object { [string]$_.propertyName -ine $propertyName })
    $readOnly = $false; $valueKind = 'string'; $attribute = $null
    if ($Action -eq 'upsert') {
        $valueKind = [string](Get-TcHmiFrameworkSetting $Settings 'value_kind' 'string')
        if ($valueKind -notin @('string','boolean','number','object','any')) {
            throw 'Framework attribute value_kind must be string, boolean, number, object or any.'
        }
        $htmlName = [string](Get-TcHmiFrameworkSetting $Settings 'html_name' (ConvertTo-TcHmiFrameworkHtmlName $propertyName))
        if (-not ($htmlName -match '^data-tchmi-[a-z0-9][a-z0-9-]*$')) { throw "Invalid Framework HTML attribute name '$htmlName'." }
        $type = [string](Get-TcHmiFrameworkSetting $Settings 'type' '')
        if (-not $type) {
            $definition = switch ($valueKind) {
                'boolean'{'Boolean'} 'number'{'Number'} 'object'{'Any'} 'any'{'Any'} default{'String'}
            }
            $type = "tchmi:general#/definitions/$definition"
        }
        if (-not $type.StartsWith('tchmi:', [StringComparison]::Ordinal)) { throw "Framework attribute type must start with 'tchmi:'." }
        $readOnly = [bool](Get-TcHmiFrameworkSetting $Settings 'read_only' $false)
        $attribute = [ordered]@{
            name=$htmlName; propertyName=$propertyName
            propertyGetterName=('get' + $propertyName)
            displayName=[string](Get-TcHmiFrameworkSetting $Settings 'display_name' $propertyName)
            visible=[bool](Get-TcHmiFrameworkSetting $Settings 'visible' $true)
            themeable=[string](Get-TcHmiFrameworkSetting $Settings 'themeable' 'Standard')
            displayPriority=[int](Get-TcHmiFrameworkSetting $Settings 'display_priority' 10)
            type=$type; category=[string](Get-TcHmiFrameworkSetting $Settings 'category' 'General')
            description=[string](Get-TcHmiFrameworkSetting $Settings 'description' '')
            readOnly=$readOnly; bindable=[bool](Get-TcHmiFrameworkSetting $Settings 'bindable' $true)
            defaultBindingMode=[string](Get-TcHmiFrameworkSetting $Settings 'default_binding_mode' 'OneWay')
            heritable=[bool](Get-TcHmiFrameworkSetting $Settings 'heritable' $true)
            defaultValue=(Get-TcHmiFrameworkSetting $Settings 'default_value' $null)
            defaultValueInternal=(Get-TcHmiFrameworkSetting $Settings 'default_value_internal' $null)
        }
        if (-not $readOnly) { $attribute.propertySetterName = 'set' + $propertyName }
        if ($attribute.themeable -notin @('None','Standard','Advanced')) { throw 'themeable must be None, Standard or Advanced.' }
        if ($attribute.defaultBindingMode -notin @('OneWay','TwoWay')) { throw 'default_binding_mode must be OneWay or TwoWay.' }
        $next += [pscustomobject]$attribute
    }
    $code = if ($Action -eq 'upsert') { New-TcHmiFrameworkAttributeCode $propertyName $valueKind $sourcePath $readOnly } else { '' }
    $region = Set-TcHmiFrameworkGeneratedRegion $sourcePath 'Attribute' $propertyName $code ($Action -eq 'remove')
    $unmanagedExisting = $existing.Count -eq 1 -and -not [bool]$region.had_region
    $preview = [pscustomobject]@{
        status='preview'; project_file=$resolved.project_file; control=[string]$description.name
        action=$Action; property_name=$propertyName; html_name=if($attribute){$attribute.name}else{[string]$existing[0].name}
        description_file=$resolved.description_path; source_file=$sourcePath
        source_region_present=[bool]$region.had_region; unmanaged_existing=$unmanagedExisting
        apply_blocked_reason=if($unmanagedExisting){'Existing attribute is not owned by a TwinCATAgent generated region.'}else{''}
        apply_required=$true
    }
    if (-not $Apply) { return $preview }
    if ($unmanagedExisting) { throw "Attribute '$propertyName' already exists outside a TwinCATAgent generated region; refusing to adopt or remove user code." }
    $backupRoot = Join-Path $resolved.project_directory ('.TwinCATAgent\backups\' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff') + '-FrameworkAttribute')
    [IO.Directory]::CreateDirectory($backupRoot) | Out-Null
    $descriptionBackup = Join-Path $backupRoot ([IO.Path]::GetFileName($resolved.description_path))
    $sourceBackup = Join-Path $backupRoot ([IO.Path]::GetFileName($sourcePath))
    [IO.File]::Copy($resolved.description_path, $descriptionBackup, $false)
    [IO.File]::Copy($sourcePath, $sourceBackup, $false)
    try {
        $description.attributes = @($next)
        $json = $description | ConvertTo-Json -Depth 40
        [IO.File]::WriteAllText($resolved.description_path, $json + "`r`n", (New-Object Text.UTF8Encoding($false)))
        [IO.File]::WriteAllText($sourcePath, [string]$region.text, (New-Object Text.UTF8Encoding($false)))
        $validation = Test-TcHmiFrameworkProject $resolved.project_file
        if (-not $validation.valid) { throw "Framework validation failed with $($validation.error_count) error(s)." }
    } catch {
        [IO.File]::Copy($descriptionBackup, $resolved.description_path, $true)
        [IO.File]::Copy($sourceBackup, $sourcePath, $true)
        throw "Framework attribute update failed and files were restored: $($_.Exception.Message)"
    }
    $readback = Get-TcHmiFrameworkControlInfo $resolved.project_file ([string]$description.name)
    $present = @($readback.attributes | Where-Object { [string]$_.propertyName -ieq $propertyName }).Count -eq 1
    $verified = if ($Action -eq 'upsert') { $present } else { -not $present }
    if (-not $verified) { throw "Framework attribute '$propertyName' readback verification failed." }
    [pscustomobject]@{
        status=if($Action -eq 'upsert'){'updated'}else{'removed'}; control=[string]$description.name
        property_name=$propertyName; description_file=$resolved.description_path; source_file=$sourcePath
        backup_directory=$backupRoot; verified=$true; validation=$validation
    }
}

function Set-TcHmiFrameworkEvent {
    param([Parameter(Mandatory)][string]$Source, [string]$Control = '',
          [Parameter(Mandatory)][string]$Name,
          [ValidateSet('upsert','remove')][string]$Action = 'upsert',
          $Settings = $null, [bool]$Apply = $false)
    $resolved = Resolve-TcHmiFrameworkControl $Source $Control
    $description = $resolved.description
    $pascal = ConvertTo-TcHmiFrameworkPascalName ($Name -replace '^\.?on','')
    $eventName = '.on' + $pascal
    $sourcePath = Get-TcHmiFrameworkControlSource $resolved
    $existing = @($description.events | Where-Object { [string]$_.name -ieq $eventName })
    if ($existing.Count -gt 1) { throw "Event '$eventName' is duplicated in Description.json." }
    if ($Action -eq 'remove' -and $existing.Count -eq 0) { throw "Event '$eventName' was not found." }
    $next = @($description.events | Where-Object { [string]$_.name -ine $eventName })
    $event = $null
    if ($Action -eq 'upsert') {
        $arguments = @(Get-TcHmiFrameworkSetting $Settings 'arguments' @())
        foreach ($argument in $arguments) {
            if (-not ([string]$argument.type).StartsWith('tchmi:', [StringComparison]::Ordinal)) {
                throw "Framework event argument type must start with 'tchmi:'."
            }
        }
        $event = [pscustomobject][ordered]@{
            name=$eventName; displayName=[string](Get-TcHmiFrameworkSetting $Settings 'display_name' $eventName)
            visible=[bool](Get-TcHmiFrameworkSetting $Settings 'visible' $true)
            displayPriority=[int](Get-TcHmiFrameworkSetting $Settings 'display_priority' 5)
            category=[string](Get-TcHmiFrameworkSetting $Settings 'category' 'Control')
            description=[string](Get-TcHmiFrameworkSetting $Settings 'description' '')
            heritable=[bool](Get-TcHmiFrameworkSetting $Settings 'heritable' $true)
            allowsPreventDefault=[bool](Get-TcHmiFrameworkSetting $Settings 'allows_prevent_default' $false)
            arguments=$arguments
        }
        $next += $event
    }
    $shortName = $eventName.TrimStart('.')
    $code = if ($Action -eq 'upsert') { New-TcHmiFrameworkEventCode $eventName $sourcePath } else { '' }
    $region = Set-TcHmiFrameworkGeneratedRegion $sourcePath 'Event' $shortName $code ($Action -eq 'remove')
    $unmanagedExisting = $existing.Count -eq 1 -and -not [bool]$region.had_region
    $preview = [pscustomobject]@{
        status='preview'; project_file=$resolved.project_file; control=[string]$description.name
        action=$Action; event_name=$eventName; raiser=('raise' + $pascal)
        description_file=$resolved.description_path; source_file=$sourcePath
        source_region_present=[bool]$region.had_region; unmanaged_existing=$unmanagedExisting
        apply_blocked_reason=if($unmanagedExisting){'Existing event is not owned by a TwinCATAgent generated region.'}else{''}
        apply_required=$true
    }
    if (-not $Apply) { return $preview }
    if ($unmanagedExisting) { throw "Event '$eventName' already exists outside a TwinCATAgent generated region; refusing to adopt or remove user code." }
    $backupRoot = Join-Path $resolved.project_directory ('.TwinCATAgent\backups\' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff') + '-FrameworkEvent')
    [IO.Directory]::CreateDirectory($backupRoot) | Out-Null
    $descriptionBackup = Join-Path $backupRoot ([IO.Path]::GetFileName($resolved.description_path))
    $sourceBackup = Join-Path $backupRoot ([IO.Path]::GetFileName($sourcePath))
    [IO.File]::Copy($resolved.description_path, $descriptionBackup, $false)
    [IO.File]::Copy($sourcePath, $sourceBackup, $false)
    try {
        $description.events = @($next)
        $json = $description | ConvertTo-Json -Depth 40
        [IO.File]::WriteAllText($resolved.description_path, $json + "`r`n", (New-Object Text.UTF8Encoding($false)))
        [IO.File]::WriteAllText($sourcePath, [string]$region.text, (New-Object Text.UTF8Encoding($false)))
        $validation = Test-TcHmiFrameworkProject $resolved.project_file
        if (-not $validation.valid) { throw "Framework validation failed with $($validation.error_count) error(s)." }
    } catch {
        [IO.File]::Copy($descriptionBackup, $resolved.description_path, $true)
        [IO.File]::Copy($sourceBackup, $sourcePath, $true)
        throw "Framework event update failed and files were restored: $($_.Exception.Message)"
    }
    $readback = Get-TcHmiFrameworkControlInfo $resolved.project_file ([string]$description.name)
    $present = @($readback.events | Where-Object { [string]$_.name -ieq $eventName }).Count -eq 1
    $verified = if ($Action -eq 'upsert') { $present } else { -not $present }
    if (-not $verified) { throw "Framework event '$eventName' readback verification failed." }
    [pscustomobject]@{
        status=if($Action -eq 'upsert'){'updated'}else{'removed'}; control=[string]$description.name
        event_name=$eventName; raiser=('raise' + $pascal); description_file=$resolved.description_path
        source_file=$sourcePath; backup_directory=$backupRoot; verified=$true; validation=$validation
    }
}

function Test-TcHmiFrameworkProject {
    param([Parameter(Mandatory)][string]$Source)
    $projectFile = Resolve-TcHmiFrameworkProject $Source
    $root = [IO.Path]::GetDirectoryName($projectFile)
    $findings = New-Object System.Collections.ArrayList
    function Add-FrameworkFinding([string]$Severity,[string]$Code,[string]$File,[string]$Message) {
        [void]$findings.Add([pscustomobject]@{ severity=$Severity; code=$Code; file=$File; message=$Message })
    }
    $projectXml = $null
    try { [xml]$projectXml = [IO.File]::ReadAllText($projectFile, [Text.Encoding]::UTF8) }
    catch { Add-FrameworkFinding 'error' 'invalid-hmiextproj' ([IO.Path]::GetFileName($projectFile)) $_.Exception.Message }
    $targetFramework = ''
    if ($null -ne $projectXml) {
        $node = $projectXml.SelectSingleNode("//*[local-name()='TargetFramework']")
        if ($null -ne $node) { $targetFramework = [string]$node.InnerText }
        if (-not $targetFramework) { Add-FrameworkFinding 'error' 'missing-target-framework' ([IO.Path]::GetFileName($projectFile)) 'TargetFramework is missing.' }
    }
    $manifestPath = Join-Path $root 'Manifest.json'; $manifest = $null
    if (-not [IO.File]::Exists($manifestPath)) { Add-FrameworkFinding 'error' 'missing-manifest' 'Manifest.json' 'Framework Manifest.json is missing.' }
    else { try { $manifest = [IO.File]::ReadAllText($manifestPath, [Text.Encoding]::UTF8) | ConvertFrom-Json }
           catch { Add-FrameworkFinding 'error' 'invalid-manifest-json' 'Manifest.json' $_.Exception.Message } }
    $controls = New-Object System.Collections.ArrayList
    if ($null -ne $manifest) {
        if ([int]$manifest.apiVersion -lt 1) { Add-FrameworkFinding 'error' 'invalid-api-version' 'Manifest.json' 'apiVersion must be at least 1.' }
        $controlModules = @($manifest.modules | Where-Object { [string]$_.type -ceq 'Control' })
        if ($controlModules.Count -eq 0) { Add-FrameworkFinding 'warning' 'no-control-module' 'Manifest.json' 'No Control module is declared.' }
        foreach ($module in $controlModules) {
            $baseRelative = ([string]$module.basePath).TrimEnd('/','\').Replace('\','/')
            $descriptionRelative = ($baseRelative + '/' + [string]$module.descriptionFile).TrimStart('/')
            try { $descriptionPath = Resolve-TcHmiFilePath $root $descriptionRelative @('.json') -RequireRegistered }
            catch { Add-FrameworkFinding 'error' 'invalid-control-path' 'Manifest.json' $_.Exception.Message; continue }
            if (-not [IO.File]::Exists($descriptionPath)) {
                Add-FrameworkFinding 'error' 'missing-control-description' $descriptionRelative 'Control Description.json is missing.'
                continue
            }
            try { $description = [IO.File]::ReadAllText($descriptionPath, [Text.Encoding]::UTF8) | ConvertFrom-Json }
            catch { Add-FrameworkFinding 'error' 'invalid-control-description-json' $descriptionRelative $_.Exception.Message; continue }
            foreach ($field in @('name','namespace','base','template')) {
                if (-not [string]$description.$field) { Add-FrameworkFinding 'error' 'missing-control-field' $descriptionRelative "Required field '$field' is missing." }
            }
            if (-not ([string]$description.namespace).StartsWith('TcHmi.Controls.', [StringComparison]::Ordinal)) {
                Add-FrameworkFinding 'error' 'invalid-control-namespace' $descriptionRelative 'Control namespace must start with TcHmi.Controls.'
            }
            $resolvedControl = [pscustomobject]@{
                project_file=$projectFile; project_directory=$root; manifest_path=$manifestPath
                module=$module; base_path=$baseRelative; description_relative=$descriptionRelative
                description_path=$descriptionPath; description=$description
            }
            $sourcePath = ''; $sourceText = ''
            try {
                $sourcePath = Get-TcHmiFrameworkControlSource $resolvedControl
                $sourceText = [IO.File]::ReadAllText($sourcePath, [Text.Encoding]::UTF8)
            } catch {
                Add-FrameworkFinding 'warning' 'control-source-unavailable' $descriptionRelative $_.Exception.Message
            }
            $attributeNames = @{}; $propertyNames = @{}
            foreach ($attribute in @($description.attributes)) {
                $htmlName = [string]$attribute.name; $propertyName = [string]$attribute.propertyName
                foreach ($field in @('name','displayName','propertyName','propertyGetterName','type')) {
                    if (-not [string]$attribute.$field) { Add-FrameworkFinding 'error' 'missing-attribute-field' $descriptionRelative "Attribute '$propertyName' is missing '$field'." }
                }
                if ($htmlName -ne 'id' -and -not $htmlName.StartsWith('data-tchmi-', [StringComparison]::Ordinal)) {
                    Add-FrameworkFinding 'error' 'invalid-attribute-name' $descriptionRelative "Attribute '$htmlName' must be id or start with data-tchmi-."
                }
                if (-not ([string]$attribute.type).StartsWith('tchmi:', [StringComparison]::Ordinal)) {
                    Add-FrameworkFinding 'error' 'invalid-attribute-type' $descriptionRelative "Attribute '$propertyName' type must start with tchmi:."
                }
                if ($attributeNames.ContainsKey($htmlName.ToLowerInvariant())) { Add-FrameworkFinding 'error' 'duplicate-attribute-name' $descriptionRelative "Duplicate attribute name '$htmlName'." }
                else { $attributeNames[$htmlName.ToLowerInvariant()] = $true }
                if ($propertyNames.ContainsKey($propertyName.ToLowerInvariant())) { Add-FrameworkFinding 'error' 'duplicate-property-name' $descriptionRelative "Duplicate propertyName '$propertyName'." }
                else { $propertyNames[$propertyName.ToLowerInvariant()] = $true }
                if ($sourceText -and [string]$attribute.propertyGetterName -and
                    $sourceText -notmatch ('\b' + [regex]::Escape([string]$attribute.propertyGetterName) + '\s*\(')) {
                    Add-FrameworkFinding 'error' 'missing-property-getter' $descriptionRelative "Source does not implement '$([string]$attribute.propertyGetterName)'."
                }
                $isReadOnly = $false; if ($attribute.PSObject.Properties.Name -contains 'readOnly') { $isReadOnly = [bool]$attribute.readOnly }
                if (-not $isReadOnly -and -not [string]$attribute.propertySetterName) {
                    Add-FrameworkFinding 'error' 'missing-property-setter-name' $descriptionRelative "Writable attribute '$propertyName' has no propertySetterName."
                }
                elseif (-not $isReadOnly -and $sourceText -and
                    $sourceText -notmatch ('\b' + [regex]::Escape([string]$attribute.propertySetterName) + '\s*\(')) {
                    Add-FrameworkFinding 'error' 'missing-property-setter' $descriptionRelative "Source does not implement '$([string]$attribute.propertySetterName)'."
                }
            }
            $eventNames = @{}
            foreach ($event in @($description.events)) {
                foreach ($field in @('name','displayName','category')) {
                    if (-not [string]$event.$field) { Add-FrameworkFinding 'error' 'missing-event-field' $descriptionRelative "Event '$([string]$event.name)' is missing '$field'." }
                }
                $eventName = [string]$event.name
                if (-not ($eventName -match '^\.on[A-Za-z_][A-Za-z0-9_]*$')) {
                    Add-FrameworkFinding 'error' 'invalid-event-name' $descriptionRelative "Custom event '$eventName' must use .onName format."
                }
                if ($eventNames.ContainsKey($eventName.ToLowerInvariant())) { Add-FrameworkFinding 'error' 'duplicate-event-name' $descriptionRelative "Duplicate event '$eventName'." }
                else { $eventNames[$eventName.ToLowerInvariant()] = $true }
                foreach ($argument in @($event.arguments)) {
                    if (-not ([string]$argument.type).StartsWith('tchmi:', [StringComparison]::Ordinal)) {
                        Add-FrameworkFinding 'error' 'invalid-event-argument-type' $descriptionRelative "Event '$eventName' argument type must start with tchmi:."
                    }
                }
                if ($sourceText -and $sourceText -notmatch [regex]::Escape($eventName)) {
                    Add-FrameworkFinding 'warning' 'event-not-raised' $descriptionRelative "Source does not contain a raise/register reference for '$eventName'."
                }
            }
            $requiredFiles = New-Object System.Collections.ArrayList
            [void]$requiredFiles.Add([string]$description.template)
            foreach ($entry in @($description.icons)) { [void]$requiredFiles.Add([string]$entry.name) }
            foreach ($entry in @($description.dependencyFiles)) { [void]$requiredFiles.Add([string]$entry.name) }
            foreach ($theme in @($description.themes.PSObject.Properties)) {
                foreach ($entry in @($theme.Value.resources)) { [void]$requiredFiles.Add([string]$entry.name) }
            }
            foreach ($entry in @($description.dataTypes)) { [void]$requiredFiles.Add([string]$entry.schema) }
            foreach ($fileName in @($requiredFiles | Where-Object { $_ } | Select-Object -Unique)) {
                $relative = ($baseRelative + '/' + $fileName).TrimStart('/')
                try { $path = Resolve-TcHmiFilePath $root $relative -RequireRegistered }
                catch { Add-FrameworkFinding 'error' 'invalid-control-resource-path' $descriptionRelative $_.Exception.Message; continue }
                if (-not [IO.File]::Exists($path)) {
                    $typescriptSource = if ([IO.Path]::GetExtension($path) -ieq '.js') { [IO.Path]::ChangeExtension($path,'.ts') } else { '' }
                    if ($typescriptSource -and [IO.File]::Exists($typescriptSource)) {
                        Add-FrameworkFinding 'warning' 'generated-javascript-pending' $relative 'JavaScript output is not present yet; the matching TypeScript source exists and must be compiled.'
                    } else {
                        Add-FrameworkFinding 'error' 'missing-control-resource' $relative "Resource referenced by '$descriptionRelative' does not exist."
                    }
                }
                elseif ([IO.Path]::GetExtension($path) -ieq '.json') {
                    try { [IO.File]::ReadAllText($path, [Text.Encoding]::UTF8) | ConvertFrom-Json | Out-Null }
                    catch { Add-FrameworkFinding 'error' 'invalid-control-resource-json' $relative $_.Exception.Message }
                }
            }
            [void]$controls.Add([pscustomobject]@{
                name=[string]$description.name; namespace=[string]$description.namespace
                base=[string]$description.base; base_path=$baseRelative; description_file=$descriptionRelative
                attribute_count=@($description.attributes).Count; event_count=@($description.events).Count
                function_count=@($description.functions).Count; source_file=$sourcePath
                attributes=@($description.attributes); events=@($description.events)
            })
        }
    }
    foreach ($file in Get-ChildItem -LiteralPath $root -Recurse -File -Include *.json,*.xml,*.hmiextproj,*.nuspec,*.ts,*.js,*.css,*.html) {
        try {
            $text = [IO.File]::ReadAllText($file.FullName, [Text.Encoding]::UTF8)
            if ($text -match '\$[A-Za-z_][A-Za-z0-9_.]*\$') {
                $relative = $file.FullName.Substring($root.Length).TrimStart('\').Replace('\','/')
                Add-FrameworkFinding 'error' 'unresolved-template-token' $relative "Unresolved template token '$($Matches[0])'."
            }
        } catch { }
    }
    $errors = @($findings | Where-Object severity -eq 'error'); $warnings = @($findings | Where-Object severity -eq 'warning')
    [pscustomobject]@{
        status='validated'; project_file=$projectFile; project_directory=$root; target_framework=$targetFramework
        valid=($errors.Count -eq 0); error_count=$errors.Count; warning_count=$warnings.Count
        control_count=$controls.Count; controls=[object[]]$controls.ToArray()
        findings=[object[]]$findings.ToArray(); readonly=$true
        note='Structural validation only; TypeScript compilation, NuGet packing and browser runtime behavior are not proven.'
    }
}

function New-TcHmiFrameworkProject {
    param([Parameter(Mandatory)]$Dte, [string]$ProjectName = '',
          [Parameter(Mandatory)][string]$Name, [Parameter(Mandatory)][string]$OutputDirectory,
          [ValidateSet('typescript','javascript')][string]$Language = 'typescript',
          [string]$Description = '', [bool]$Apply = $false)
    if (-not ($Name -match '^[A-Za-z_][A-Za-z0-9_]*$')) { throw 'Framework project name must use letters, digits and underscore.' }
    $hmiProject = Get-TcHmiProjectObject $Dte $ProjectName
    $hmiInfo = Get-TcHmiProjectInfo $Dte (Get-TcHmiResolvedFullName $hmiProject)
    if ([string]$hmiInfo.framework_target -cne 'native1.12-tchmi') {
        throw "Framework scaffold currently supports native1.12-tchmi; current project is '$([string]$hmiInfo.framework_target)'."
    }
    $parent = [IO.Path]::GetFullPath($OutputDirectory)
    if (-not [IO.Directory]::Exists($parent)) { throw "Framework output parent does not exist: $parent" }
    $destination = [IO.Path]::GetFullPath((Join-Path $parent $Name))
    if (-not $destination.StartsWith($parent.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Framework destination escapes output parent.' }
    if ([IO.File]::Exists($destination) -or [IO.Directory]::Exists($destination)) { throw "Framework destination already exists: $destination" }
    $templateRoot = Get-TcHmiFrameworkTemplateRoot
    $templateFolder = if ($Language -eq 'typescript') { 'FrameworkPrj_PackagesConfig' } else { 'FrameworkPrjJs_PackagesConfig' }
    $template = Join-Path $templateRoot "ProjectTemplates\TcXaeShell\Framework\$templateFolder"
    if (-not [IO.Directory]::Exists($template)) { throw "Installed Framework template is missing: $template" }
    $solutionRoot = [IO.Path]::GetDirectoryName([string]$Dte.Solution.FullName)
    $packagesRoot = Join-Path $solutionRoot 'Packages'
    $typescriptPackages = if ([IO.Directory]::Exists($packagesRoot)) { @(Get-ChildItem -LiteralPath $packagesRoot -Directory -Filter 'Microsoft.TypeScript.MSBuild.*') } else { @() }
    if ($typescriptPackages.Count -eq 0) { throw "Microsoft.TypeScript.MSBuild package was not found under $packagesRoot." }
    $typescriptPackage = $typescriptPackages | Sort-Object Name -Descending | Select-Object -First 1
    $typescriptVersion = $typescriptPackage.Name.Substring('Microsoft.TypeScript.MSBuild.'.Length)
    $frameworkPackages = @(Get-ChildItem -LiteralPath $packagesRoot -Directory -Filter 'Beckhoff.TwinCAT.HMI.Framework.*')
    if ($frameworkPackages.Count -eq 0) { throw "Beckhoff.TwinCAT.HMI.Framework package was not found under $packagesRoot." }
    $frameworkPackage = $frameworkPackages | Sort-Object Name -Descending | Select-Object -First 1
    $frameworkVersion = $frameworkPackage.Name.Substring('Beckhoff.TwinCAT.HMI.Framework.'.Length)
    $frameworkRuntime = Join-Path $frameworkPackage.FullName 'runtimes\native1.12-tchmi'
    $relativePackages = '..\'
    try {
        $from = New-Object Uri(($destination.TrimEnd('\') + '\'))
        # The official template appends the literal "packages\" segment,
        # therefore this placeholder points to the solution directory that
        # contains Packages, not to the Packages directory itself.
        $to = New-Object Uri(($solutionRoot.TrimEnd('\') + '\'))
        $relativePackages = [Uri]::UnescapeDataString($from.MakeRelativeUri($to).ToString()).Replace('/','\')
    } catch { }
    if (-not $relativePackages.EndsWith('\')) { $relativePackages += '\' }
    $controlName = $Name + 'Control'; $projectFile = Join-Path $destination ($Name + '.hmiextproj')
    $relativeFrameworkRuntime = ''
    try {
        $fromControl = New-Object Uri(((Join-Path $destination $controlName).TrimEnd('\') + '\'))
        $toRuntime = New-Object Uri(($frameworkRuntime.TrimEnd('\') + '\'))
        $relativeFrameworkRuntime = [Uri]::UnescapeDataString($fromControl.MakeRelativeUri($toRuntime).ToString()).TrimEnd('/')
    } catch { }
    $preview = [pscustomobject]@{
        status='preview'; project=$Name; language=$Language; destination=$destination
        project_file=$projectFile; control=$controlName; target_framework='native1.12-tchmi'
        typescript_msbuild_version=$typescriptVersion; framework_package_version=$frameworkVersion
        packages_relative_path=$relativePackages
        template=$template; apply_required=$true
    }
    if (-not $Apply) { return $preview }
    $replacements = [ordered]@{
        '$safeprojectname$'=$Name; '$safeprojectname_cleaned$'=$Name
        '$projectname_tchmi_physical$'=$Name; '$projectname_tchmi$'=$Name
        '$default_namespace$'=$Name; '$projectdescription$'=if($Description){$Description}else{"TwinCAT HMI Framework controls for $Name."}
        '$guid1$'=[Guid]::NewGuid().ToString(); '$year$'=(Get-Date -Format 'yyyy')
        '$time$'=(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
        '$Configuration_RelativePath_To_Packages$'=$relativePackages
        '$Microsoft.TypeScript.MSBuild_VERSION$'=$typescriptVersion
        '$Beckhoff.TwinCAT.HMI.Framework_VERSION$'=$frameworkVersion
        '$relativedestinationdirectory$'=$relativeFrameworkRuntime
    }
    function Write-FrameworkTemplateFile([string]$Source,[string]$Target,[bool]$Replace=$true) {
        [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($Target)) | Out-Null
        if (-not $Replace) { [IO.File]::Copy($Source,$Target,$false); return }
        $text = [IO.File]::ReadAllText($Source,[Text.Encoding]::UTF8)
        foreach ($pair in $replacements.GetEnumerator()) { $text = $text.Replace([string]$pair.Key,[string]$pair.Value) }
        [IO.File]::WriteAllText($Target,$text,(New-Object Text.UTF8Encoding($false)))
    }
    try {
        [IO.Directory]::CreateDirectory($destination) | Out-Null
        $rootFiles = @{
            'ProjectTemplate.hmiextproj'=($Name+'.hmiextproj'); 'Manifest.json'='Manifest.json'
            '_eslintrc.json'='.eslintrc.json'; '_tsconfig.tpl.json'='tsconfig.tpl.json'; '_tsconfig.json'='tsconfig.json'
            'FrameworkPrj.nuspec'=($Name+'.nuspec'); 'LICENSE.txt'='LICENSE.txt'; '0packages.config'='packages.config'
            'packages.xsd'='packages.xsd'; 'tchmi.gitignore'='.gitignore'; 'tchmi.tfignore'='.tfignore'
        }
        foreach ($pair in $rootFiles.GetEnumerator()) { Write-FrameworkTemplateFile (Join-Path $template $pair.Key) (Join-Path $destination $pair.Value) $true }
        Write-FrameworkTemplateFile (Join-Path $template 'logo.png') (Join-Path $destination 'Images\logo.png') $false
        Write-FrameworkTemplateFile (Join-Path $template '16x16.png') (Join-Path $destination "$controlName\Icons\16x16.png") $false
        foreach ($fileName in @('Description.json','Style.css','Template.html')) {
            Write-FrameworkTemplateFile (Join-Path $template $fileName) (Join-Path $destination "$controlName\$fileName") $true
        }
        Write-FrameworkTemplateFile (Join-Path $template 'Types.Schema.json') (Join-Path $destination "$controlName\Schema\Types.Schema.json") $true
        $sourceExtension = if($Language -eq 'typescript'){'ts'}else{'js'}
        Write-FrameworkTemplateFile (Join-Path $template ("Source.$sourceExtension")) (Join-Path $destination "$controlName\$controlName.$sourceExtension") $true
        Write-FrameworkTemplateFile (Join-Path $template 'StyleThemesBase.css') (Join-Path $destination "$controlName\Themes\Base\Style.css") $true
        Write-FrameworkTemplateFile (Join-Path $template 'StyleThemesBaseDark.css') (Join-Path $destination "$controlName\Themes\Base-Dark\Style.css") $true
        $validation = Test-TcHmiFrameworkProject $projectFile
        if (-not $validation.valid) { throw "Generated Framework project failed structural validation with $($validation.error_count) errors." }
    } catch {
        if ([IO.Directory]::Exists($destination)) { [IO.Directory]::Delete($destination,$true) }
        throw "HMI Framework project creation failed and generated files were removed: $($_.Exception.Message)"
    }
    [pscustomobject]@{
        status='created'; project=$Name; language=$Language; destination=$destination; project_file=$projectFile
        control=$controlName; target_framework='native1.12-tchmi'; verified=$true; validation=$validation
        added_to_solution=$false; publish_performed=$false
    }
}

function Invoke-TcHmiFrameworkPack {
    param([Parameter(Mandatory)][string]$Source, [string]$OutputDirectory = '',
          [string]$Version = '', [bool]$Apply = $false)
    $projectFile = Resolve-TcHmiFrameworkProject $Source
    $root = [IO.Path]::GetDirectoryName($projectFile)
    $validation = Test-TcHmiFrameworkProject $projectFile
    if (-not $validation.valid) { throw "Framework project has $($validation.error_count) structural error(s); pack is blocked." }
    $pendingJavaScript = @($validation.findings | Where-Object { [string]$_.code -eq 'generated-javascript-pending' })
    if ($pendingJavaScript.Count -gt 0) { throw 'Framework TypeScript output is pending; compile the generated JavaScript before packing.' }
    $nuspecFiles = @(Get-ChildItem -LiteralPath $root -File -Filter '*.nuspec')
    if ($nuspecFiles.Count -ne 1) { throw "Framework project must contain exactly one .nuspec; found $($nuspecFiles.Count)." }
    $nuspecPath = $nuspecFiles[0].FullName
    try { [xml]$nuspec = [IO.File]::ReadAllText($nuspecPath, [Text.Encoding]::UTF8) }
    catch { throw "Framework nuspec is invalid: $($_.Exception.Message)" }
    $namespace = New-Object Xml.XmlNamespaceManager($nuspec.NameTable)
    $namespace.AddNamespace('n','http://schemas.microsoft.com/packaging/2013/05/nuspec.xsd')
    $idNode = $nuspec.SelectSingleNode('//n:metadata/n:id',$namespace)
    $versionNode = $nuspec.SelectSingleNode('//n:metadata/n:version',$namespace)
    if ($null -eq $idNode -or -not [string]$idNode.InnerText) { throw 'Framework nuspec metadata id is missing.' }
    $packageId = [string]$idNode.InnerText
    $packageVersion = if ($Version) { $Version } elseif ($null -ne $versionNode) { [string]$versionNode.InnerText } else { '' }
    if (-not ($packageVersion -match '^\d+\.\d+\.\d+(?:\.\d+)?(?:-[0-9A-Za-z.-]+)?$')) {
        throw "Framework package version '$packageVersion' is not a supported semantic version."
    }
    $output = if ($OutputDirectory) { [IO.Path]::GetFullPath($OutputDirectory) } else { Join-Path $root 'bin\Packages' }
    $templateRoot = Get-TcHmiFrameworkTemplateRoot
    $nugetExe = Join-Path ([IO.Path]::GetDirectoryName($templateRoot)) 'bin\nuget\nuget.exe'
    if (-not [IO.File]::Exists($nugetExe)) { throw "TE2000 NuGet executable was not found: $nugetExe" }
    $expectedPackage = Join-Path $output ($packageId + '.' + $packageVersion + '.nupkg')
    $preview = [pscustomobject]@{
        status='preview'; project_file=$projectFile; nuspec=$nuspecPath; package_id=$packageId
        version=$packageVersion; output_directory=$output; expected_package=$expectedPackage
        nuget_executable=$nugetExe; validation=$validation; apply_required=$true
        install_performed=$false; publish_performed=$false
    }
    if (-not $Apply) { return $preview }
    [IO.Directory]::CreateDirectory($output) | Out-Null
    $arguments = @(
        'pack',$nuspecPath,'-BasePath',$root,'-OutputDirectory',$output,
        '-Version',$packageVersion,'-Exclude','.TwinCATAgent\**;bin\**;obj\**',
        '-NonInteractive','-NoPackageAnalysis'
    )
    $packOutput = & $nugetExe @arguments 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) { throw "NuGet pack failed with exit code $LASTEXITCODE`: $packOutput" }
    if (-not [IO.File]::Exists($expectedPackage)) {
        $candidates = @(Get-ChildItem -LiteralPath $output -File -Filter ($packageId + '.*.nupkg') | Sort-Object LastWriteTimeUtc -Descending)
        if ($candidates.Count -eq 0) { throw 'NuGet reported success, but no package was found in the output directory.' }
        $expectedPackage = $candidates[0].FullName
    }
    Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction SilentlyContinue
    $archive = [IO.Compression.ZipFile]::OpenRead($expectedPackage)
    try { $entries = @($archive.Entries | ForEach-Object { $_.FullName.Replace('\','/') }) }
    finally { $archive.Dispose() }
    $manifestEntry = 'runtimes/native1.12-tchmi/Manifest.json'
    $descriptionEntries = @($entries | Where-Object { $_ -match '^runtimes/native1\.12-tchmi/.+/Description\.json$' })
    $expectedDescriptions = @($validation.controls | ForEach-Object {
        'runtimes/native1.12-tchmi/' + ([string]$_.description_file).Replace('\','/')
    })
    $missingDescriptions = @($expectedDescriptions | Where-Object { $entries -notcontains $_ })
    $contaminatedEntries = @($entries | Where-Object { $_ -match '(^|/)\.TwinCATAgent/' -or $_ -match '^runtimes/native1\.12-tchmi/(bin|obj)/' })
    $verified = ($entries -contains $manifestEntry) -and $missingDescriptions.Count -eq 0 -and $contaminatedEntries.Count -eq 0
    if (-not $verified) {
        throw "NuGet package verification failed. Missing descriptions: $($missingDescriptions -join ', '); contaminated entries: $($contaminatedEntries -join ', ')."
    }
    [pscustomobject]@{
        status='packed'; project_file=$projectFile; package=$expectedPackage; package_id=$packageId
        version=$packageVersion; size_bytes=(Get-Item -LiteralPath $expectedPackage).Length
        manifest_entry=$manifestEntry; control_description_entries=$descriptionEntries
        excluded_agent_metadata=($contaminatedEntries.Count -eq 0)
        verified=$true; nuget_exit_code=0; install_performed=$false; publish_performed=$false
    }
}

function Read-TcHmiFrameworkPackage {
    param([Parameter(Mandatory)][string]$Package)
    $packagePath = [IO.Path]::GetFullPath($Package)
    if (-not [IO.File]::Exists($packagePath)) { throw "TwinCAT HMI package does not exist: $packagePath. Use tc_hmi_framework_packages and copy its package_path; do not guess package IDs, versions or NuGet cache paths." }
    if (-not [IO.Path]::GetExtension($packagePath).Equals('.nupkg', [StringComparison]::OrdinalIgnoreCase)) {
        throw "TwinCAT HMI package must be a .nupkg file: $packagePath"
    }
    Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction SilentlyContinue
    $archive = [IO.Compression.ZipFile]::OpenRead($packagePath)
    try {
        $nuspecEntries = @($archive.Entries | Where-Object { $_.FullName -match '^[^/\\]+\.nuspec$' })
        if ($nuspecEntries.Count -ne 1) { throw "Package must contain exactly one root .nuspec; found $($nuspecEntries.Count)." }
        $reader = New-Object IO.StreamReader($nuspecEntries[0].Open(), [Text.Encoding]::UTF8, $true)
        try { [xml]$nuspec = $reader.ReadToEnd() } finally { $reader.Dispose() }
        # TE2000 templates in the field use both the 2012/06 and 2013/05
        # NuSpec namespaces. local-name keeps package inspection version-safe.
        $idNode = $nuspec.SelectSingleNode("/*[local-name()='package']/*[local-name()='metadata']/*[local-name()='id']")
        $versionNode = $nuspec.SelectSingleNode("/*[local-name()='package']/*[local-name()='metadata']/*[local-name()='version']")
        $id = if($null -ne $idNode){[string]$idNode.InnerText}else{''}
        $version = if($null -ne $versionNode){[string]$versionNode.InnerText}else{''}
        if (-not ($id -match '^[A-Za-z0-9][A-Za-z0-9_.-]*$')) { throw "Package id '$id' is invalid." }
        if (-not ($version -match '^\d+\.\d+\.\d+(?:\.\d+)?(?:-[0-9A-Za-z.-]+)?$')) { throw "Package version '$version' is invalid." }
        $manifestEntries = @($archive.Entries | Where-Object { $_.FullName.Replace('\','/') -match '^runtimes/([^/]+)/Manifest\.json$' })
        if ($manifestEntries.Count -ne 1) { throw "Package must contain exactly one runtimes/<framework>/Manifest.json; found $($manifestEntries.Count)." }
        $manifestEntryName = $manifestEntries[0].FullName.Replace('\','/')
        $targetFramework = ($manifestEntryName -split '/')[1]
        $reader = New-Object IO.StreamReader($manifestEntries[0].Open(), [Text.Encoding]::UTF8, $true)
        try { $manifestText = $reader.ReadToEnd(); $manifest = $manifestText | ConvertFrom-Json }
        finally { $reader.Dispose() }
        if ([int]$manifest.apiVersion -lt 1) { throw 'Package Manifest apiVersion must be at least 1.' }
        $modules = @($manifest.modules)
        if ($modules.Count -eq 0) { throw 'Package Manifest does not contain any modules.' }
        $controls = New-Object System.Collections.ArrayList
        $functions = New-Object System.Collections.ArrayList
        $moduleTypes = @($modules | ForEach-Object { [string]$_.type } | Sort-Object -Unique)
        foreach ($module in @($modules | Where-Object { [string]$_.type -cin @('Control','Function') })) {
            $base = ([string]$module.basePath).Trim('/','\').Replace('\','/')
            $descriptionEntry = "runtimes/$targetFramework/$base/$([string]$module.descriptionFile)".Replace('//','/')
            if (-not [string]$module.descriptionFile -or $descriptionEntry -match '(^|/)\.\.(/|$)|:') {
                throw "Invalid package module description path: $descriptionEntry"
            }
            $entry = @($archive.Entries | Where-Object { $_.FullName.Replace('\','/') -ceq $descriptionEntry })
            if ($entry.Count -ne 1) { throw "Package $([string]$module.type) description is missing or duplicated: $descriptionEntry" }
            $reader = New-Object IO.StreamReader($entry[0].Open(), [Text.Encoding]::UTF8, $true)
            try { $controlDescription = $reader.ReadToEnd() | ConvertFrom-Json } finally { $reader.Dispose() }
            $description = [pscustomobject]@{
                name=[string]$controlDescription.name; namespace=[string]$controlDescription.namespace
                base_path=$base; description_entry=$descriptionEntry
            }
            if ([string]$module.type -ceq 'Control') { [void]$controls.Add($description) }
            else { [void]$functions.Add($description) }
        }
        $packageKind = if ($id -ieq 'Beckhoff.TwinCAT.HMI.Framework') { 'framework' }
            elseif ($controls.Count -gt 0 -and $functions.Count -gt 0) { 'mixed' }
            elseif ($controls.Count -gt 0) { 'controls' }
            elseif ($functions.Count -gt 0) { 'functions' } else { 'resources' }
        $dependencies = New-Object System.Collections.ArrayList
        foreach ($node in @($nuspec.SelectNodes("//*[local-name()='dependencies']//*[local-name()='dependency']"))) {
            [void]$dependencies.Add([pscustomobject]@{ id=[string]$node.id; version=[string]$node.version })
        }
        [pscustomobject]@{
            status='validated'; package=$packagePath; id=$id; version=$version
            target_framework=$targetFramework; manifest_entry=$manifestEntryName
            package_kind=$packageKind; module_types=$moduleTypes; module_count=$modules.Count
            control_count=$controls.Count; function_count=$functions.Count
            controls=[object[]]$controls.ToArray(); dependencies=[object[]]$dependencies.ToArray()
            functions=[object[]]$functions.ToArray()
            validation_scope='nuspec_manifest_and_control_function_descriptions'
            runtime_verified=$false; install_performed=$false
            size_bytes=(Get-Item -LiteralPath $packagePath).Length; readonly=$true
        }
    } finally { $archive.Dispose() }
}

function Get-TcHmiPackageArchives {
    param([string]$Folder, [string]$Id, [string]$Version)
    $files = if ([IO.Directory]::Exists($Folder)) {
        @(Get-ChildItem -LiteralPath $Folder -File -Filter '*.nupkg' | Sort-Object Name)
    } else { @() }
    $matches = @($files | Where-Object { $_.Name -ieq ($Id + '.' + $Version + '.nupkg') })
    $path = if ($matches.Count -eq 1) { [string]$matches[0].FullName } else { $null }
    [pscustomobject]@{
        package_path=$path; package_exists=[bool]$path
        archive_status=$(if($path){'found'}elseif($files.Count){'unresolved_filename'}else{'missing'})
        package_candidates=@($files | Select-Object -First 10 | ForEach-Object { [string]$_.FullName })
        candidates_truncated=($files.Count -gt 10); archive_identity_verified=$false
    }
}

function Get-TcHmiFrameworkPackages {
    param([Parameter(Mandatory)]$Dte, [string]$ProjectName = '', [string]$PackageId = '')
    $project = Get-TcHmiProjectObject $Dte $ProjectName
    $projectFile = Get-TcHmiResolvedFullName $project
    $root = Get-TcHmiProjectPath $project
    $projectInfo = Get-TcHmiProjectInfo $Dte $projectFile
    $solutionRoot = [IO.Path]::GetDirectoryName([string]$Dte.Solution.FullName)
    $packagesRoot = Join-Path $solutionRoot 'Packages'
    $packagesConfigPath = Join-Path $root 'packages.config'
    $configPath = Join-Path $root 'Properties\tchmiconfig.json'
    $serverPath = Join-Path $root 'Server\TcHmiSrv\TcHmiSrv.Config.default.json'
    if (-not [IO.File]::Exists($packagesConfigPath)) { throw "HMI packages.config is missing: $packagesConfigPath" }
    try { [xml]$packagesConfig = [IO.File]::ReadAllText($packagesConfigPath, [Text.Encoding]::UTF8) }
    catch { throw "HMI packages.config is invalid: $($_.Exception.Message)" }
    $config = if([IO.File]::Exists($configPath)){[IO.File]::ReadAllText($configPath,[Text.Encoding]::UTF8)|ConvertFrom-Json}else{$null}
    $server = if([IO.File]::Exists($serverPath)){[IO.File]::ReadAllText($serverPath,[Text.Encoding]::UTF8)|ConvertFrom-Json}else{$null}
    $items = New-Object System.Collections.ArrayList
    foreach ($node in @($packagesConfig.SelectNodes('/packages/package'))) {
        $id=[string]$node.id; $version=[string]$node.version
        if ($PackageId -and $id -ine $PackageId) { continue }
        $folder = Join-Path $packagesRoot ($id + '.' + $version)
        $archive = Get-TcHmiPackageArchives $folder $id $version
        $runtime = Join-Path $folder ('runtimes\' + [string]$projectInfo.framework_target)
        $manifestPath = Join-Path $runtime 'Manifest.json'
        $registered = $false
        if ($null -ne $config) { $registered = @($config.packages | Where-Object { [string]$_.name -ceq $id }).Count -eq 1 }
        $virtualKey = '/' + $id; $virtualValue = ''
        if ($null -ne $server -and $null -ne $server.VIRTUALDIRECTORIES) {
            $property = $server.VIRTUALDIRECTORIES.PSObject.Properties[$virtualKey]
            if ($null -ne $property) { $virtualValue = [string]$property.Value }
        }
        [void]$items.Add([pscustomobject]@{
            id=$id; version=$version; target_framework=[string]$node.targetFramework
            folder=$folder; folder_exists=[IO.Directory]::Exists($folder)
            package_path=$archive.package_path; package_exists=$archive.package_exists
            archive_status=$archive.archive_status; package_candidates=$archive.package_candidates
            candidates_truncated=$archive.candidates_truncated; archive_identity_verified=$false
            runtime_path=$runtime; manifest=$manifestPath; manifest_exists=[IO.File]::Exists($manifestPath)
            framework_inspection_applicable=[IO.File]::Exists($manifestPath)
            hmi_registered=$registered; virtual_directory=$virtualValue; virtual_directory_registered=[bool]$virtualValue
            consistent=([IO.Directory]::Exists($folder) -and ((-not [IO.File]::Exists($manifestPath)) -or ($registered -and [bool]$virtualValue)))
        })
    }
    [pscustomobject]@{
        status='ok'; project=Get-TcHmiResolvedName $project; project_file=$projectFile
        target_framework=[string]$projectInfo.framework_target; packages_root=$packagesRoot
        packages=[object[]]$items.ToArray(); readonly=$true
    }
}

function Install-TcHmiFrameworkPackage {
    param([Parameter(Mandatory)]$Dte, [string]$ProjectName = '',
          [Parameter(Mandatory)][string]$Package, [bool]$Apply = $false,
          [bool]$AcknowledgePackageChange = $false)
    $packageInfo = Read-TcHmiFrameworkPackage $Package
    # Broader read-only inspection must not silently broaden the existing
    # Control-package installation workflow or its validation coverage.
    if (@($packageInfo.controls).Count -eq 0) {
        throw 'This package can be inspected, but the current installation workflow only supports packages containing Control modules.'
    }
    $project = Get-TcHmiProjectObject $Dte $ProjectName
    try { $Dte.ExecuteCommand('File.SaveAll') } catch { }
    $projectFile = Get-TcHmiResolvedFullName $project
    $projectDisplayName = Get-TcHmiResolvedName $project
    $root = Get-TcHmiProjectPath $project
    $projectInfo = Get-TcHmiProjectInfo $Dte $projectFile
    if ([string]$packageInfo.target_framework -cne [string]$projectInfo.framework_target) {
        throw "Package target '$($packageInfo.target_framework)' does not match project target '$($projectInfo.framework_target)'."
    }
    $solutionRoot = [IO.Path]::GetDirectoryName([string]$Dte.Solution.FullName)
    $packagesRoot = Join-Path $solutionRoot 'Packages'
    $destination = [IO.Path]::GetFullPath((Join-Path $packagesRoot ($packageInfo.id + '.' + $packageInfo.version)))
    if (-not $destination.StartsWith([IO.Path]::GetFullPath($packagesRoot).TrimEnd('\') + '\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Package destination escapes the solution Packages directory.' }
    $packagesConfigPath = Join-Path $root 'packages.config'
    $configPath = Join-Path $root 'Properties\tchmiconfig.json'
    $serverPath = Join-Path $root 'Server\TcHmiSrv\TcHmiSrv.Config.default.json'
    foreach ($required in @($packagesConfigPath,$configPath,$serverPath)) { if(-not [IO.File]::Exists($required)){throw "Required HMI configuration file is missing: $required"} }
    [xml]$packagesConfig = [IO.File]::ReadAllText($packagesConfigPath,[Text.Encoding]::UTF8)
    $existing = @($packagesConfig.SelectNodes('/packages/package') | Where-Object { [string]$_.id -ieq [string]$packageInfo.id })
    if ($existing.Count -gt 0) {
        $versions = @($existing | ForEach-Object { [string]$_.version }) -join ', '
        throw "Package '$($packageInfo.id)' is already registered at version(s): $versions. Uninstall it before installing another copy."
    }
    $installedIds = @($packagesConfig.SelectNodes('/packages/package') | ForEach-Object { [string]$_.id })
    $missingDependencies = @($packageInfo.dependencies | Where-Object { $installedIds -notcontains [string]$_.id })
    if ($missingDependencies.Count -gt 0) { throw "Package dependencies are not installed: $(@($missingDependencies.id) -join ', ')." }
    $preview = [pscustomobject]@{
        status='preview'; project=$projectDisplayName; project_file=$projectFile; package=$packageInfo
        destination=$destination; packages_config=$packagesConfigPath; hmi_config=$configPath; server_config=$serverPath
        would_extract_package=$true; would_register_package=$true; would_reload_xae_project=$true
        apply_required=$true; acknowledge_package_change_required=$true; publish_performed=$false
    }
    if (-not $Apply) { return $preview }
    if (-not $AcknowledgePackageChange) { throw 'apply=true requires acknowledge_package_change=true.' }
    if ([IO.Directory]::Exists($destination)) { throw "Package destination already exists but is not registered: $destination" }
    $backupRoot = Join-Path $root ('.TwinCATAgent\backups\' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff') + '-FrameworkPackageInstall')
    [IO.Directory]::CreateDirectory($backupRoot) | Out-Null
    foreach ($file in @($packagesConfigPath,$configPath,$serverPath)) { [IO.File]::Copy($file,(Join-Path $backupRoot ([IO.Path]::GetFileName($file))),$false) }
    $projectRemoved = $false; $packageCreated = $false
    try {
        $templateRoot = Get-TcHmiFrameworkTemplateRoot
        $nugetExe = Join-Path ([IO.Path]::GetDirectoryName($templateRoot)) 'bin\nuget\nuget.exe'
        $sourceDirectory = [IO.Path]::GetDirectoryName([string]$packageInfo.package)
        $arguments = @('install',[string]$packageInfo.id,'-Version',[string]$packageInfo.version,'-Source',$sourceDirectory,
            '-OutputDirectory',$packagesRoot,'-NonInteractive','-NoCache','-PackageSaveMode','nupkg')
        $nugetOutput = & $nugetExe @arguments 2>&1 | Out-String
        if ($LASTEXITCODE -ne 0 -or -not [IO.Directory]::Exists($destination)) { throw "NuGet install failed: $nugetOutput" }
        $packageCreated = $true
        [void]$packagesConfig.DocumentElement.AppendChild($packagesConfig.CreateElement('package'))
        $packageNode = $packagesConfig.DocumentElement.LastChild
        $packageNode.SetAttribute('id',[string]$packageInfo.id); $packageNode.SetAttribute('version',[string]$packageInfo.version)
        $packageNode.SetAttribute('targetFramework',[string]$packageInfo.target_framework)
        $config = [IO.File]::ReadAllText($configPath,[Text.Encoding]::UTF8) | ConvertFrom-Json
        $config.packages = @($config.packages) + @([pscustomobject]@{name=[string]$packageInfo.id;basePath=('/'+[string]$packageInfo.id)})
        $server = [IO.File]::ReadAllText($serverPath,[Text.Encoding]::UTF8) | ConvertFrom-Json
        $virtualKey = '/' + [string]$packageInfo.id
        if ($server.VIRTUALDIRECTORIES.PSObject.Properties.Name -contains $virtualKey) { throw "Server virtual directory '$virtualKey' already exists." }
        $virtualValue = '..\..\Packages\' + [IO.Path]::GetFileName($destination) + '\runtimes\' + [string]$packageInfo.target_framework
        $server.VIRTUALDIRECTORIES | Add-Member -NotePropertyName $virtualKey -NotePropertyValue $virtualValue
        $Dte.Solution.Remove($project); $projectRemoved = $true
        $packagesText = '<?xml version="1.0" encoding="utf-8"?>' + "`r`n" + (Convert-TcHmiXmlText $packagesConfig)
        [IO.File]::WriteAllText($packagesConfigPath,$packagesText,(New-Object Text.UTF8Encoding($false)))
        [IO.File]::WriteAllText($configPath,($config|ConvertTo-Json -Depth 100)+"`r`n",(New-Object Text.UTF8Encoding($false)))
        [IO.File]::WriteAllText($serverPath,($server|ConvertTo-Json -Depth 100)+"`r`n",(New-Object Text.UTF8Encoding($false)))
        $project = $Dte.Solution.AddFromFile($projectFile,$false); $projectRemoved = $false
        if ($null -eq $project) { throw 'XAE did not reload the HMI project after package installation.' }
        try { $Dte.ExecuteCommand('File.SaveAll') } catch { }
        Start-Sleep -Milliseconds 500
        $inventory = Get-TcHmiFrameworkPackages $Dte $projectFile
        $installed = @($inventory.packages | Where-Object { $_.id -ceq [string]$packageInfo.id -and $_.version -ceq [string]$packageInfo.version })
        if ($installed.Count -ne 1 -or -not [bool]$installed[0].consistent) { throw 'Installed package did not pass configuration readback.' }
    } catch {
        $failure = $_.Exception.Message
        try {
            if (-not $projectRemoved) {
                foreach ($candidate in @(Get-TcHmiProjectObjects $Dte)) { if((Get-TcHmiResolvedFullName $candidate) -ieq $projectFile){$Dte.Solution.Remove($candidate);break} }
            }
        } catch { }
        foreach ($file in @($packagesConfigPath,$configPath,$serverPath)) {
            $backup = Join-Path $backupRoot ([IO.Path]::GetFileName($file)); if([IO.File]::Exists($backup)){[IO.File]::Copy($backup,$file,$true)}
        }
        if ($packageCreated -and [IO.Directory]::Exists($destination)) { [IO.Directory]::Delete($destination,$true) }
        try { $Dte.Solution.AddFromFile($projectFile,$false) | Out-Null; $Dte.ExecuteCommand('File.SaveAll') } catch { }
        throw "HMI Framework package installation failed and was rolled back: $failure"
    }
    [pscustomobject]@{
        status='installed'; project=$projectDisplayName; package_id=[string]$packageInfo.id; version=[string]$packageInfo.version
        destination=$destination; controls=@($packageInfo.controls); backup_directory=$backupRoot
        verified=$true; load_mode='config-update-and-com-reload'; publish_performed=$false
    }
}

function Get-TcHmiFrameworkPackageReferences {
    param([Parameter(Mandatory)][string]$ProjectRoot,
          [Parameter(Mandatory)][string[]]$Namespaces)
    $references = New-Object System.Collections.ArrayList
    $extensions = @('.view','.content','.usercontrol')
    foreach ($file in @(Get-ChildItem -LiteralPath $ProjectRoot -Recurse -File -ErrorAction SilentlyContinue |
            Where-Object {
                $extensions -contains $_.Extension.ToLowerInvariant() -and
                $_.FullName -notmatch '[\\/]\.TwinCATAgent[\\/]' -and
                $_.FullName -notmatch '[\\/](?:bin|obj)[\\/]'
            })) {
        $lines = [IO.File]::ReadAllLines($file.FullName, [Text.Encoding]::UTF8)
        for ($index = 0; $index -lt $lines.Count; $index++) {
            foreach ($namespace in @($Namespaces | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })) {
                if ($lines[$index] -match ([regex]::Escape($namespace) + '(?:\.|\b)')) {
                    [void]$references.Add([pscustomobject]@{
                        file=$file.FullName; relative_path=$file.FullName.Substring($ProjectRoot.TrimEnd('\').Length + 1)
                        line=($index + 1); namespace=$namespace; text=$lines[$index].Trim()
                    })
                    break
                }
            }
        }
    }
    return [object[]]$references.ToArray()
}

function Get-TcHmiInstalledFrameworkPackageInfo {
    param([Parameter(Mandatory)][string]$PackageFolder,
          [Parameter(Mandatory)][string]$TargetFramework,
          [Parameter(Mandatory)][string]$PackageId,
          [Parameter(Mandatory)][string]$Version)
    $runtime = Join-Path $PackageFolder ('runtimes\' + $TargetFramework)
    $manifestPath = Join-Path $runtime 'Manifest.json'
    if (-not [IO.File]::Exists($manifestPath)) {
        throw "Installed HMI package Manifest is missing: $manifestPath"
    }
    try { $manifest = [IO.File]::ReadAllText($manifestPath,[Text.Encoding]::UTF8) | ConvertFrom-Json }
    catch { throw "Installed HMI package Manifest is invalid: $($_.Exception.Message)" }
    $controls = New-Object System.Collections.ArrayList
    foreach ($module in @($manifest.modules | Where-Object { [string]$_.type -ceq 'Control' })) {
        $base = ([string]$module.basePath).Trim('/','\')
        $descriptionPath = Join-Path (Join-Path $runtime $base) ([string]$module.descriptionFile)
        if (-not [IO.File]::Exists($descriptionPath)) {
            throw "Installed HMI control description is missing: $descriptionPath"
        }
        try { $description = [IO.File]::ReadAllText($descriptionPath,[Text.Encoding]::UTF8) | ConvertFrom-Json }
        catch { throw "Installed HMI control description is invalid: $($_.Exception.Message)" }
        [void]$controls.Add([pscustomobject]@{
            name=[string]$description.name; namespace=[string]$description.namespace
            description_file=$descriptionPath
        })
    }
    if ($controls.Count -eq 0) { throw "Installed package '$PackageId' does not contain a Control module." }
    [pscustomobject]@{
        id=$PackageId; version=$Version; target_framework=$TargetFramework
        folder=$PackageFolder; manifest=$manifestPath; controls=[object[]]$controls.ToArray()
    }
}

function Uninstall-TcHmiFrameworkPackage {
    param([Parameter(Mandatory)]$Dte, [string]$ProjectName = '',
          [Parameter(Mandatory)][string]$PackageId, [bool]$Force = $false,
          [bool]$Apply = $false, [bool]$AcknowledgePackageChange = $false)
    if (-not ($PackageId -match '^[A-Za-z0-9][A-Za-z0-9_.-]*$')) { throw "Invalid package id '$PackageId'." }
    $protectedPackages = @(
        'Beckhoff.TwinCAT.HMI.Framework','Beckhoff.TwinCAT.HMI.Controls',
        'Beckhoff.TwinCAT.HMI.Functions','Beckhoff.TwinCAT.HMI.Server.Engineering',
        'Microsoft.TypeScript.MSBuild'
    )
    if ($protectedPackages -contains $PackageId) {
        throw "Core HMI package '$PackageId' is protected and cannot be removed by this tool."
    }
    $project = Get-TcHmiProjectObject $Dte $ProjectName
    try { $Dte.ExecuteCommand('File.SaveAll') } catch { }
    $projectFile = Get-TcHmiResolvedFullName $project
    $projectDisplayName = Get-TcHmiResolvedName $project
    $root = Get-TcHmiProjectPath $project
    $projectInfo = Get-TcHmiProjectInfo $Dte $projectFile
    $solutionRoot = [IO.Path]::GetDirectoryName([string]$Dte.Solution.FullName)
    $packagesRoot = [IO.Path]::GetFullPath((Join-Path $solutionRoot 'Packages'))
    $packagesConfigPath = Join-Path $root 'packages.config'
    $configPath = Join-Path $root 'Properties\tchmiconfig.json'
    $serverPath = Join-Path $root 'Server\TcHmiSrv\TcHmiSrv.Config.default.json'
    foreach ($required in @($packagesConfigPath,$configPath,$serverPath)) {
        if (-not [IO.File]::Exists($required)) { throw "Required HMI configuration file is missing: $required" }
    }
    [xml]$packagesConfig = [IO.File]::ReadAllText($packagesConfigPath,[Text.Encoding]::UTF8)
    $matches = @($packagesConfig.SelectNodes('/packages/package') |
        Where-Object { [string]$_.id -ieq $PackageId })
    if ($matches.Count -ne 1) { throw "Package '$PackageId' was not found uniquely in packages.config." }
    $node = $matches[0]
    $version = [string]$node.version
    $targetFramework = [string]$node.targetFramework
    if ([string]::IsNullOrWhiteSpace($targetFramework)) { $targetFramework = [string]$projectInfo.framework_target }
    $packageFolder = [IO.Path]::GetFullPath((Join-Path $packagesRoot ($PackageId + '.' + $version)))
    if (-not $packageFolder.StartsWith($packagesRoot.TrimEnd('\') + '\',[StringComparison]::OrdinalIgnoreCase)) {
        throw 'Package folder escapes the solution Packages directory.'
    }
    if (-not [IO.Directory]::Exists($packageFolder)) { throw "Installed package folder is missing: $packageFolder" }
    $installedInfo = Get-TcHmiInstalledFrameworkPackageInfo $packageFolder $targetFramework $PackageId $version
    $namespaces = @($installedInfo.controls | ForEach-Object { [string]$_.namespace } | Sort-Object -Unique)
    $references = @(Get-TcHmiFrameworkPackageReferences $root $namespaces)
    $config = [IO.File]::ReadAllText($configPath,[Text.Encoding]::UTF8) | ConvertFrom-Json
    $registeredCount = @($config.packages | Where-Object { [string]$_.name -ieq $PackageId }).Count
    $server = [IO.File]::ReadAllText($serverPath,[Text.Encoding]::UTF8) | ConvertFrom-Json
    $virtualKey = '/' + $PackageId
    $virtualProperty = $server.VIRTUALDIRECTORIES.PSObject.Properties[$virtualKey]
    $preview = [pscustomobject]@{
        status='preview'; project=$projectDisplayName; project_file=$projectFile
        package=$installedInfo; namespaces=$namespaces; references=$references
        reference_count=$references.Count; force_required=($references.Count -gt 0)
        hmi_registration_count=$registeredCount; virtual_directory_registered=($null -ne $virtualProperty)
        would_unregister_package=$true; would_move_package_to_backup=$true; would_reload_xae_project=$true
        apply_required=$true; acknowledge_package_change_required=$true; publish_performed=$false
    }
    if (-not $Apply) { return $preview }
    if (-not $AcknowledgePackageChange) { throw 'apply=true requires acknowledge_package_change=true.' }
    if ($references.Count -gt 0 -and -not $Force) {
        throw "Package '$PackageId' is referenced $($references.Count) time(s) by HMI markup. Remove those controls first or retry with force=true after review."
    }
    $backupRoot = Join-Path $root ('.TwinCATAgent\backups\' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff') + '-FrameworkPackageUninstall')
    $backupPackageRoot = Join-Path $backupRoot 'package'
    $backupPackageFolder = Join-Path $backupPackageRoot ([IO.Path]::GetFileName($packageFolder))
    [IO.Directory]::CreateDirectory($backupPackageRoot) | Out-Null
    foreach ($file in @($packagesConfigPath,$configPath,$serverPath)) {
        [IO.File]::Copy($file,(Join-Path $backupRoot ([IO.Path]::GetFileName($file))),$false)
    }
    $projectRemoved = $false; $packageMoved = $false
    try {
        [void]$node.ParentNode.RemoveChild($node)
        $config.packages = @($config.packages | Where-Object { [string]$_.name -ine $PackageId })
        if ($null -ne $virtualProperty) { $server.VIRTUALDIRECTORIES.PSObject.Properties.Remove($virtualKey) }
        $Dte.Solution.Remove($project); $projectRemoved = $true
        $packagesText = '<?xml version="1.0" encoding="utf-8"?>' + "`r`n" + (Convert-TcHmiXmlText $packagesConfig)
        [IO.File]::WriteAllText($packagesConfigPath,$packagesText,(New-Object Text.UTF8Encoding($false)))
        [IO.File]::WriteAllText($configPath,($config|ConvertTo-Json -Depth 100)+"`r`n",(New-Object Text.UTF8Encoding($false)))
        [IO.File]::WriteAllText($serverPath,($server|ConvertTo-Json -Depth 100)+"`r`n",(New-Object Text.UTF8Encoding($false)))
        [IO.Directory]::Move($packageFolder,$backupPackageFolder); $packageMoved = $true
        $project = $Dte.Solution.AddFromFile($projectFile,$false); $projectRemoved = $false
        if ($null -eq $project) { throw 'XAE did not reload the HMI project after package uninstallation.' }
        try { $Dte.ExecuteCommand('File.SaveAll') } catch { }
        Start-Sleep -Milliseconds 500
        $inventory = Get-TcHmiFrameworkPackages $Dte $projectFile
        if (@($inventory.packages | Where-Object { $_.id -ieq $PackageId }).Count -ne 0 -or
            [IO.Directory]::Exists($packageFolder)) {
            throw 'Uninstalled package remained visible during configuration readback.'
        }
    } catch {
        $failure = $_.Exception.Message
        try {
            if (-not $projectRemoved) {
                foreach ($candidate in @(Get-TcHmiProjectObjects $Dte)) {
                    if ((Get-TcHmiResolvedFullName $candidate) -ieq $projectFile) { $Dte.Solution.Remove($candidate); break }
                }
            }
        } catch { }
        foreach ($file in @($packagesConfigPath,$configPath,$serverPath)) {
            $backup = Join-Path $backupRoot ([IO.Path]::GetFileName($file))
            if ([IO.File]::Exists($backup)) { [IO.File]::Copy($backup,$file,$true) }
        }
        if ($packageMoved -and [IO.Directory]::Exists($backupPackageFolder) -and -not [IO.Directory]::Exists($packageFolder)) {
            [IO.Directory]::Move($backupPackageFolder,$packageFolder)
        }
        try { $Dte.Solution.AddFromFile($projectFile,$false) | Out-Null; $Dte.ExecuteCommand('File.SaveAll') } catch { }
        throw "HMI Framework package uninstallation failed and was rolled back: $failure"
    }
    [pscustomobject]@{
        status='uninstalled'; project=$projectDisplayName; package_id=$PackageId; version=$version
        removed_references_present=$references.Count; forced=$Force; backup_directory=$backupRoot
        package_backup=$backupPackageFolder; verified=$true; publish_performed=$false
    }
}

function Get-TcHmiRuntimeInfo {
    param([Parameter(Mandatory)]$Dte, [string]$ProjectName = '')
    $project = Get-TcHmiProjectObject $Dte $ProjectName
    $projectFile = Get-TcHmiResolvedFullName $project
    $root = Get-TcHmiProjectPath $project
    $serverConfigPath = Join-Path $root 'Server\TcHmiSrv\TcHmiSrv.Config.default.json'
    if (-not [IO.File]::Exists($serverConfigPath)) {
        throw "HMI Server default configuration is missing: $serverConfigPath"
    }
    try { $serverConfig = [IO.File]::ReadAllText($serverConfigPath,[Text.Encoding]::UTF8) | ConvertFrom-Json }
    catch { throw "HMI Server default configuration is invalid: $($_.Exception.Message)" }
    $processItems = New-Object System.Collections.ArrayList
    $processEndpoints = New-Object System.Collections.ArrayList
    try {
        foreach ($process in @(Get-CimInstance Win32_Process -Filter "Name='TcHmiSrv.exe'" -ErrorAction Stop)) {
            $commandLine = [string]$process.CommandLine
            if ([string]::IsNullOrWhiteSpace($commandLine) -or
                $commandLine.IndexOf($root,[StringComparison]::OrdinalIgnoreCase) -lt 0) { continue }
            $endpoint = ''
            $match = [regex]::Match($commandLine,'(?:^|\s)--endpoint=(?:"([^"]+)"|(\S+))',[Text.RegularExpressions.RegexOptions]::IgnoreCase)
            if ($match.Success) {
                $endpoint = if($match.Groups[1].Success){$match.Groups[1].Value}else{$match.Groups[2].Value}
                if ($endpoint) { [void]$processEndpoints.Add($endpoint.TrimEnd('/')) }
            }
            [void]$processItems.Add([pscustomobject]@{
                process_id=[int]$process.ProcessId; executable=[string]$process.ExecutablePath
                endpoint=$endpoint; storage_directory=$root
            })
        }
    } catch { }
    $configuredEndpoints = @($serverConfig.ENDPOINTS | ForEach-Object { ([string]$_).TrimEnd('/') } |
        Where-Object { $_ } | Sort-Object -Unique)
    # Probe only the public endpoint reported by an exact-project server
    # process.  TcHmiSrv also persists its force-auth endpoint in ENDPOINTS on
    # some TE2000/Server combinations.  That authentication listener is not an
    # HMI application endpoint and must never be used for /bin probes.  Saved
    # endpoints remain informational; they are never contacted without a
    # matching live process command line.
    $probeEndpoints = @($processEndpoints.ToArray() | Where-Object { $_ } | Sort-Object -Unique)
    $defaultDocument = @($serverConfig.DEFAULTDOCUMENT | ForEach-Object { [string]$_ } |
        Where-Object { $_ } | Select-Object -First 1)
    if ($defaultDocument.Count -eq 0) { $defaultDocument = @('Default.html') }
    $routes = New-Object System.Collections.ArrayList
    if ($null -ne $serverConfig.VIRTUALDIRECTORIES) {
        foreach ($property in @($serverConfig.VIRTUALDIRECTORIES.PSObject.Properties)) {
            [void]$routes.Add([pscustomobject]@{ route=[string]$property.Name; target=[string]$property.Value })
        }
    }
    $candidatePaths = New-Object System.Collections.ArrayList
    foreach ($route in @($routes.ToArray())) {
        if ([string]$route.route -eq '/bin' -or [string]$route.target -match '(?i)(?:^|[\\/])bin[\\/]?$') {
            [void]$candidatePaths.Add(([string]$route.route).TrimEnd('/') + '/' + $defaultDocument[0])
        }
    }
    [void]$candidatePaths.Add('/' + $defaultDocument[0])
    $probes = New-Object System.Collections.ArrayList
    foreach ($endpoint in $probeEndpoints) {
        foreach ($candidatePath in @($candidatePaths.ToArray() | Select-Object -Unique)) {
            $url = $endpoint + $candidatePath
            $statusCode = 0; $title = ''; $runtimeMarker = $false; $errorText = ''
            try {
                $response = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 4
                $statusCode = [int]$response.StatusCode
                $titleMatch = [regex]::Match([string]$response.Content,'<title[^>]*>(.*?)</title>',
                    [Text.RegularExpressions.RegexOptions]::IgnoreCase)
                if ($titleMatch.Success) { $title = [Net.WebUtility]::HtmlDecode($titleMatch.Groups[1].Value.Trim()) }
                $runtimeMarker = ([string]$response.Content).Contains('TCHMI_RUNTIME')
            } catch {
                try { $statusCode = [int]$_.Exception.Response.StatusCode } catch { }
                $errorText = if($statusCode -eq 460){'TwinCAT HMI Server returned HTTP 460 License Expired.'}
                    elseif($statusCode -gt 0){"HTTP probe returned status $statusCode."}
                    else{'HTTP probe failed or timed out.'}
            }
            [void]$probes.Add([pscustomobject]@{
                url=$url; status_code=$statusCode; title=$title
                runtime_marker=$runtimeMarker; reachable=($statusCode -ge 200 -and $statusCode -lt 400)
                error=$errorText
            })
        }
    }
    $selected = @($probes.ToArray() | Where-Object { $_.reachable -and $_.runtime_marker } | Select-Object -First 1)
    $applicationUrl = if($selected.Count -eq 1){[string]$selected[0].url}else{''}
    $licenseExpired = @($probes.ToArray() | Where-Object { [int]$_.status_code -eq 460 }).Count -gt 0
    [pscustomobject]@{
        status=if($applicationUrl){'ready'}elseif($licenseExpired){'license-expired'}elseif($processItems.Count -gt 0){'server-running-app-unavailable'}else{'server-not-running'}
        project=Get-TcHmiResolvedName $project; project_file=$projectFile; project_root=$root
        server_config=$serverConfigPath; processes=[object[]]$processItems.ToArray()
        configured_endpoints=$configuredEndpoints; probe_endpoints=$probeEndpoints
        routes=[object[]]$routes.ToArray()
        probes=[object[]]$probes.ToArray(); application_url=$applicationUrl
        server_running=($processItems.Count -gt 0); application_ready=[bool]$applicationUrl
        license_expired=$licenseExpired; license_state=if($licenseExpired){'expired'}else{'not_observed'}
        readonly=$true; start_server_performed=$false; publish_performed=$false
        note='Only the exact live process --endpoint is probed; saved and force-auth endpoints are not contacted. HTTP 460 is reported as License Expired. The tool does not start or publish the server.'
    }
}

function Invoke-TcHmiCdpCommand {
    param([Parameter(Mandatory)]$Socket, [Parameter(Mandatory)][hashtable]$State,
          [Parameter(Mandatory)][string]$Method, [hashtable]$Parameters = @{},
          [int]$TimeoutMs = 15000)
    $State.next_id = [int]$State.next_id + 1
    $commandId = [int]$State.next_id
    $json = @{id=$commandId;method=$Method;params=$Parameters} | ConvertTo-Json -Depth 30 -Compress
    $bytes = [Text.Encoding]::UTF8.GetBytes($json)
    $segment = New-Object 'System.ArraySegment[byte]' -ArgumentList (,$bytes)
    $sendToken = New-Object Threading.CancellationTokenSource($TimeoutMs)
    try {
        [void]$Socket.SendAsync($segment,[Net.WebSockets.WebSocketMessageType]::Text,$true,$sendToken.Token).GetAwaiter().GetResult()
    } finally { $sendToken.Dispose() }
    while ($true) {
        $builder = New-Object Text.StringBuilder
        do {
            $buffer = New-Object byte[] 65536
            $receiveSegment = New-Object 'System.ArraySegment[byte]' -ArgumentList (,$buffer)
            $receiveToken = New-Object Threading.CancellationTokenSource($TimeoutMs)
            try { $received = $Socket.ReceiveAsync($receiveSegment,$receiveToken.Token).GetAwaiter().GetResult() }
            finally { $receiveToken.Dispose() }
            if ($received.MessageType -eq [Net.WebSockets.WebSocketMessageType]::Close) {
                throw "Browser DevTools socket closed while waiting for '$Method'."
            }
            [void]$builder.Append([Text.Encoding]::UTF8.GetString($buffer,0,$received.Count))
        } while (-not $received.EndOfMessage)
        $message = $builder.ToString() | ConvertFrom-Json
        if ($null -ne $message.id -and [int]$message.id -eq $commandId) {
            if ($null -ne $message.error) { throw "Browser DevTools '$Method' failed: $($message.error.message)" }
            return $message
        }
        if ($message.method) { [void]$State.events.Add($message) }
    }
}

function Get-TcHmiBrowserExecutable {
    $candidates = @(
        "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
        "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
        "$env:LOCALAPPDATA\Microsoft\Edge\Application\msedge.exe",
        "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
        "$env:ProgramFiles\Google\Chrome\Application\chrome.exe"
    )
    foreach ($candidate in $candidates) { if ($candidate -and [IO.File]::Exists($candidate)) { return $candidate } }
    throw 'Microsoft Edge or Google Chrome was not found; browser runtime validation is unavailable.'
}

function Convert-TcHmiBrowserEvents {
    param([Parameter(Mandatory)][object[]]$Events, [Parameter(Mandatory)][int]$ViewportWidth)
    $diagnostics = New-Object System.Collections.ArrayList
    foreach ($event in $Events) {
        $method = [string]$event.method; $p = $event.params
        if ($method -eq 'Runtime.exceptionThrown') {
            $details = $p.exceptionDetails
            $message = [string]$details.text
            try { if($details.exception.description){$message=[string]$details.exception.description} } catch { }
            [void]$diagnostics.Add([pscustomobject]@{
                severity='error'; kind='javascript-exception'; message=$message
                url=[string]$details.url; line=([int]$details.lineNumber + 1); viewport_width=$ViewportWidth
            })
        } elseif ($method -eq 'Runtime.consoleAPICalled' -and [string]$p.type -in @('error','warning')) {
            $parts = @($p.args | ForEach-Object {
                if ($null -ne $_.value) {[string]$_.value} elseif($_.description){[string]$_.description}else{[string]$_.type}
            })
            [void]$diagnostics.Add([pscustomobject]@{
                severity=if([string]$p.type -eq 'error'){'error'}else{'warning'}
                kind='console'; message=($parts -join ' '); url=''; line=0; viewport_width=$ViewportWidth
            })
        } elseif ($method -eq 'Log.entryAdded' -and [string]$p.entry.level -in @('error','warning')) {
            [void]$diagnostics.Add([pscustomobject]@{
                severity=[string]$p.entry.level; kind='browser-log'; message=[string]$p.entry.text
                url=[string]$p.entry.url; line=[int]$p.entry.lineNumber; viewport_width=$ViewportWidth
            })
        } elseif ($method -eq 'Network.loadingFailed' -and -not [bool]$p.canceled -and
                  [string]$p.errorText -notmatch '(?i)ERR_ABORTED') {
            [void]$diagnostics.Add([pscustomobject]@{
                severity='error'; kind='resource-load-failed'; message=[string]$p.errorText
                url=''; line=0; viewport_width=$ViewportWidth
            })
        } elseif ($method -eq 'Network.responseReceived' -and [int]$p.response.status -ge 400) {
            [void]$diagnostics.Add([pscustomobject]@{
                severity='error'; kind='http-error'; message=('HTTP ' + [int]$p.response.status)
                url=[string]$p.response.url; line=0; viewport_width=$ViewportWidth
            })
        } elseif ($method -eq 'Network.webSocketFrameError') {
            [void]$diagnostics.Add([pscustomobject]@{
                severity='error'; kind='websocket-frame-error'; message=[string]$p.errorMessage
                url=''; line=0; viewport_width=$ViewportWidth
            })
        }
    }
    return [object[]]$diagnostics.ToArray()
}

function Test-TcHmiBrowserRuntime {
    param([Parameter(Mandatory)]$Dte, [string]$ProjectName = '',
          [int[]]$Widths = @(1280), [int]$Height = 720, [int]$SettleMs = 5000)
    $widthValues = @($Widths | ForEach-Object { [int]$_ } | Select-Object -Unique)
    if ($widthValues.Count -eq 0) { $widthValues = @(1280) }
    if ($widthValues.Count -gt 4) { throw 'At most four viewport widths can be validated per run.' }
    foreach ($width in $widthValues) { if ($width -lt 320 -or $width -gt 3840) { throw 'Viewport width must be between 320 and 3840 pixels.' } }
    if ($Height -lt 240 -or $Height -gt 2160) { throw 'Viewport height must be between 240 and 2160 pixels.' }
    if ($SettleMs -lt 500 -or $SettleMs -gt 30000) { throw 'settle_ms must be between 500 and 30000.' }
    $runtime = Get-TcHmiRuntimeInfo $Dte $ProjectName
    if (-not $runtime.application_ready) {
        throw "HMI browser runtime is unavailable ($($runtime.status)). Start Live View/HMI Engineering Server in XAE and build the project first."
    }
    $browser = Get-TcHmiBrowserExecutable
    $listener = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback,0)
    $listener.Start(); $debugPort = ([Net.IPEndPoint]$listener.LocalEndpoint).Port; $listener.Stop()
    $profileRoot = Join-Path ([IO.Path]::GetTempPath()) ('TwinCATAgent-HmiBrowser-' + [guid]::NewGuid().ToString('N'))
    [IO.Directory]::CreateDirectory($profileRoot) | Out-Null
    $browserProcess = $null; $socket = $null
    try {
        $arguments = @('--headless=new','--disable-gpu','--no-first-run','--no-default-browser-check',
            '--disable-background-networking',('--user-data-dir="' + $profileRoot + '"'),
            ('--remote-debugging-port=' + $debugPort),'about:blank')
        $browserProcess = Start-Process -FilePath $browser -ArgumentList $arguments -PassThru -WindowStyle Hidden
        $versionInfo = $null
        for ($attempt=0; $attempt -lt 60; $attempt++) {
            try { $versionInfo = Invoke-RestMethod -Uri ("http://127.0.0.1:$debugPort/json/version") -TimeoutSec 2; break }
            catch { Start-Sleep -Milliseconds 100 }
        }
        if ($null -eq $versionInfo) { throw 'Headless browser DevTools endpoint did not become ready.' }
        $target = $null
        for ($attempt=0; $attempt -lt 20; $attempt++) {
            $targets = @(Invoke-RestMethod -Uri ("http://127.0.0.1:$debugPort/json/list") -TimeoutSec 3)
            $target = $targets | Where-Object { [string]$_.type -eq 'page' } | Select-Object -First 1
            if ($null -ne $target -and $target.webSocketDebuggerUrl) { break }
            Start-Sleep -Milliseconds 100
        }
        if ($null -eq $target -or -not $target.webSocketDebuggerUrl) {
            try {
                $target = Invoke-RestMethod -Method Put `
                    -Uri ("http://127.0.0.1:$debugPort/json/new?about%3Ablank") -TimeoutSec 3
            } catch { }
        }
        if ($null -eq $target -or -not $target.webSocketDebuggerUrl) { throw 'Headless browser page target was not found.' }
        $socket = New-Object Net.WebSockets.ClientWebSocket
        $connectToken = New-Object Threading.CancellationTokenSource(10000)
        try { [void]$socket.ConnectAsync([Uri][string]$target.webSocketDebuggerUrl,$connectToken.Token).GetAwaiter().GetResult() }
        finally { $connectToken.Dispose() }
        $state = @{next_id=0;events=(New-Object System.Collections.ArrayList)}
        foreach ($domain in @('Runtime','Page','Network','Log')) {
            [void](Invoke-TcHmiCdpCommand $socket $state ($domain + '.enable'))
        }
        $expression = @'
(() => {
  const controls = Array.from(document.querySelectorAll('[data-tchmi-type]'));
  const ids = controls.map(x => x.id).filter(Boolean);
  const duplicates = Array.from(new Set(ids.filter((x, i) => ids.indexOf(x) !== i)));
  const visible = controls.filter(x => {
    const s = getComputedStyle(x), r = x.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 0 && r.height > 0;
  });
  let symbols = 0;
  for (const element of controls) for (const attr of element.attributes)
    if (/%(?:s|i|ctrl)%/.test(attr.value)) symbols++;
  return {
    ready_state: document.readyState,
    title: document.title,
    url: location.href,
    hmi_framework_loaded: typeof window.TcHmi === 'object',
    hmi_server_api_loaded: !!(window.TcHmi && TcHmi.Server),
    main_container_count: document.querySelectorAll('.tchmi-main-hmi-container').length,
    view_count: controls.filter(x => /TcHmiView$/.test(x.getAttribute('data-tchmi-type') || '')).length,
    control_count: controls.length,
    visible_control_count: visible.length,
    control_ids: ids,
    duplicate_ids: duplicates,
    control_types: Array.from(new Set(controls.map(x => x.getAttribute('data-tchmi-type')).filter(Boolean))),
    symbol_expression_count: symbols,
    viewport_width: innerWidth,
    viewport_height: innerHeight,
    document_width: document.documentElement.scrollWidth,
    document_height: document.documentElement.scrollHeight,
    horizontal_overflow: document.documentElement.scrollWidth > innerWidth + 1,
    vertical_overflow: document.documentElement.scrollHeight > innerHeight + 1
  };
})()
'@
        $viewportResults = New-Object System.Collections.ArrayList
        $allDiagnostics = New-Object System.Collections.ArrayList
        foreach ($width in $widthValues) {
            $state.events = New-Object System.Collections.ArrayList
            [void](Invoke-TcHmiCdpCommand $socket $state 'Emulation.setDeviceMetricsOverride' @{
                width=$width;height=$Height;deviceScaleFactor=1;mobile=$false
            })
            [void](Invoke-TcHmiCdpCommand $socket $state 'Page.navigate' @{url=[string]$runtime.application_url})
            Start-Sleep -Milliseconds $SettleMs
            $evaluation = Invoke-TcHmiCdpCommand $socket $state 'Runtime.evaluate' @{
                expression=$expression;returnByValue=$true;awaitPromise=$true
            }
            [void](Invoke-TcHmiCdpCommand $socket $state 'Runtime.evaluate' @{expression='true';returnByValue=$true})
            $metrics = $evaluation.result.result.value
            $diagnostics = @(Convert-TcHmiBrowserEvents ([object[]]$state.events.ToArray()) $width)
            foreach ($item in $diagnostics) { [void]$allDiagnostics.Add($item) }
            $hardErrors = @($diagnostics | Where-Object { $_.severity -eq 'error' })
            $loaded = ($metrics.ready_state -eq 'complete' -and [bool]$metrics.hmi_framework_loaded -and
                [int]$metrics.main_container_count -gt 0 -and [int]$metrics.view_count -gt 0)
            [void]$viewportResults.Add([pscustomobject]@{
                requested_width=$width; requested_height=$Height; loaded=$loaded
                metrics=$metrics; diagnostics=$diagnostics; error_count=$hardErrors.Count
                valid=($loaded -and $hardErrors.Count -eq 0 -and @($metrics.duplicate_ids).Count -eq 0)
            })
        }
        try { [void](Invoke-TcHmiCdpCommand $socket $state 'Browser.close' @{} 5000) } catch { }
        $errors = @($allDiagnostics.ToArray() | Where-Object { $_.severity -eq 'error' })
        $warnings = @($allDiagnostics.ToArray() | Where-Object { $_.severity -eq 'warning' })
        $invalidViewports = @($viewportResults.ToArray() | Where-Object { -not $_.valid })
        [pscustomobject]@{
            status=if($invalidViewports.Count -eq 0){'passed'}else{'failed'}
            success=($invalidViewports.Count -eq 0); project=$runtime.project
            application_url=[string]$runtime.application_url; browser_executable=$browser
            browser_version=[string]$versionInfo.Browser; viewport_results=[object[]]$viewportResults.ToArray()
            diagnostics=[object[]]$allDiagnostics.ToArray(); error_count=$errors.Count; warning_count=$warnings.Count
            readonly=$true; interaction_performed=$false; publish_performed=$false
            note='Validates the already-running engineering preview in a temporary headless browser; it does not prove production publish, authentication roles, user interaction flows or live PLC value correctness.'
        }
    } finally {
        if ($null -ne $socket) { try { $socket.Dispose() } catch { } }
        if ($null -ne $browserProcess) { try { if(-not $browserProcess.HasExited){$browserProcess.Kill()} } catch { } }
        Start-Sleep -Milliseconds 200
        if ([IO.Directory]::Exists($profileRoot)) { try { [IO.Directory]::Delete($profileRoot,$true) } catch { } }
    }
}

function Get-TcHmiDiagnostics {
    param([Parameter(Mandatory)]$Dte, [string]$ProjectName = '')
    $project = Get-TcHmiProjectObject $Dte $ProjectName
    $name = Get-TcHmiResolvedName $project
    $entries = New-Object System.Collections.ArrayList
    $readError = ''; $available = $false; $total = $null
    try {
        $items = $Dte.ToolWindows.ErrorList.ErrorItems
        if ($null -eq $items -or $null -eq $items.Count) { throw 'DTE ErrorItems is unavailable.' }
        $total = [int]$items.Count
        for ($i = 1; $i -le [Math]::Min($total, 200); $i++) {
            $item = $items.Item($i)
            $level = [int]$item.ErrorLevel
            $severity = if ($level -ge 4) {'error'} elseif ($level -eq 2) {'warning'} else {'info'}
            [void]$entries.Add([pscustomobject]@{
                severity=$severity; severity_raw=$level; description=[string]$item.Description
                project=[string]$item.Project; file=[string]$item.FileName; line=[int]$item.Line
            })
        }
        $available = $true
    } catch { $readError = $_.Exception.Message }
    [pscustomobject]@{
        status='read'; project=$name; project_file=Get-TcHmiResolvedFullName $project
        solution=[string]$Dte.Solution.FullName; readonly=$true
        error_list=[pscustomobject]@{
            available=$available; complete=($available -and $total -le 200)
            source='DTE ErrorItems'; scope='entire_solution'; total=$total
            entries=@($entries.ToArray()); read_error=$readError
            note='Current Error List snapshot; entries may predate this build or belong to another project.'
        }
    }
}

function Invoke-TcHmiBuild {
    param([Parameter(Mandatory)]$Dte, [string]$ProjectName = '')
    $project = Get-TcHmiProjectObject $Dte $ProjectName
    $root = Get-TcHmiProjectPath $project
    $configuration = ''
    try { $configuration = [string]$Dte.Solution.SolutionBuild.ActiveConfiguration.Name } catch { }
    if (-not $configuration) { $configuration = 'Release|TwinCAT HMI' }
    $uniqueName = Get-TcHmiResolvedUniqueName $project
    $Dte.Solution.SolutionBuild.BuildProject($configuration, $uniqueName, $true)
    $failed = $null; $buildReadError = ''
    try {
        $value = $Dte.Solution.SolutionBuild.LastBuildInfo
        if ($null -eq $value) { throw 'LastBuildInfo is unavailable.' }
        $failed = [int]$value
    } catch { $buildReadError = $_.Exception.Message }
    $diagnostics = Get-TcHmiDiagnostics $Dte $ProjectName
    $observedErrors = @($diagnostics.error_list.entries | Where-Object { $_.severity -eq 'error' })
    $buildSucceeded = ($null -ne $failed -and $failed -eq 0)
    $complete = ($buildSucceeded -and $diagnostics.error_list.complete -and $observedErrors.Count -eq 0)
    $outputPath = Join-Path $root 'bin'
    $entry = Join-Path $outputPath 'Default.html'
    [pscustomobject]@{
        status=if($null -ne $failed -and $failed -gt 0){'failed'}elseif($complete){'succeeded'}else{'incomplete'}
        project=Get-TcHmiResolvedName $project
        project_file=$diagnostics.project_file; solution=$diagnostics.solution
        configuration=$configuration; failed_projects=$failed; success=$complete
        build_succeeded=if($null -eq $failed){$null}else{$buildSucceeded}
        build_status_error=$buildReadError; error_list=$diagnostics.error_list
        output_path=$outputPath; entry_page=$entry; entry_page_exists=[IO.File]::Exists($entry)
        diagnostics_pending=(-not $complete)
        diagnostics_source='DTE LastBuildInfo + read-only ErrorItems attempt'
        page_verified=$false; plc_communication_verified=$false
        next_action='Do not rebuild only to recover diagnostics. Use tc_hmi_diagnostics; report unavailable diagnostics separately. No runtime was started or activated.'
    }
}

# ---- TwinSAFE project management (TISC; template/import first) ----
function Get-TcSafetyRoot {
    param([Parameter(Mandatory)]$Dte)
    $sys = Get-TcSystemManager $Dte
    try { return ,$sys.LookupTreeItem('TISC') }
    catch {
        throw ('TwinSAFE SAFETY root TISC is unavailable. Confirm that TwinCAT Safety Engineering ' +
               'is installed and that the current TwinCAT project supports Safety.')
    }
}

function Get-TcSafetyChildren {
    param([Parameter(Mandatory)]$Root)
    $items = New-Object System.Collections.ArrayList
    foreach ($child in $Root) { [void]$items.Add($child) }
    return $items.ToArray()
}

function Get-TcSafetyPath {
    param([Parameter(Mandatory)][string]$Path, [bool]$AllowRoot = $true)
    $value = $Path.Trim().Trim('^').Replace('/', '^')
    $parts = @($value -split '\^' | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
        ForEach-Object { ([string]$_).Trim() })
    if ($parts.Count -eq 0 -or [string]$parts[0] -ine 'TISC') {
        throw 'Safety tree path must start with TISC.'
    }
    if (-not $AllowRoot -and $parts.Count -eq 1) { throw 'Refusing to operate on the TISC root.' }
    foreach ($part in $parts) {
        if ($part -in @('.', '..') -or $part.Contains('^')) {
            throw 'Safety tree path contains an invalid segment.'
        }
    }
    return ($parts -join '^')
}

function Resolve-TcSafetyProject {
    param([Parameter(Mandatory)]$Dte, [string]$Project = '')
    $root = Get-TcSafetyRoot $Dte
    $projects = @(Get-TcSafetyChildren $root)
    if ([string]::IsNullOrWhiteSpace($Project)) {
        if ($projects.Count -eq 1) { return ,$projects[0] }
        if ($projects.Count -eq 0) { throw 'No Safety project exists under TISC.' }
        throw ('Multiple Safety projects exist; specify an exact project name or TISC path: ' +
               ((@($projects | ForEach-Object { [string]$_.Name })) -join ', '))
    }
    $selector = $Project.Trim()
    if ($selector -match '^(?i)TISC(?:\^|/)' -or $selector -ieq 'TISC') {
        $path = Get-TcSafetyPath $selector $false
        $sys = Get-TcSystemManager $Dte
        return ,$sys.LookupTreeItem($path)
    }
    $matches = @($projects | Where-Object { [string]$_.Name -ieq $selector })
    if ($matches.Count -ne 1) { throw "Safety project '$selector' was not found uniquely under TISC." }
    return ,$matches[0]
}

function Get-TcSafetyMetadata {
    param([Parameter(Mandatory)]$Item, $Dte = $null)
    $metadata = [ordered]@{}
    $readError = ''
    $projectFile = ''
    $xmlDocuments = New-Object System.Collections.ArrayList
    $candidates = New-Object System.Collections.ArrayList
    [void]$candidates.Add($Item)
    try {
        $nested = $Item.NestedProject
        if ($null -ne $nested) {
            [void]$candidates.Add($nested)
            try { $projectFile = [string]$nested.FullName } catch { }
        }
    } catch { }
    foreach ($child in $Item) {
        [void]$candidates.Add($child)
        if ([string]::IsNullOrWhiteSpace($projectFile)) {
            try {
                $nested = $child.NestedProject
                if ($null -ne $nested) {
                    [void]$candidates.Add($nested)
                    $projectFile = [string]$nested.FullName
                }
            } catch { }
            foreach ($propertyName in @('FullName','FileName','ProjectFile')) {
                try {
                    $candidateFile = [string]$child.$propertyName
                    if ($candidateFile.EndsWith('.splcproj', [StringComparison]::OrdinalIgnoreCase)) {
                        $projectFile = $candidateFile; break
                    }
                } catch { }
            }
        }
    }
    # Some TcXaeShell/TwinSAFE COM proxies return a mojibake NestedProject.FullName
    # for non-ASCII solution paths. Treat an unusable COM path as unresolved and
    # recover the authoritative relative PrjFilePath from the UTF-8 .tsproj.
    if (-not [string]::IsNullOrWhiteSpace($projectFile) -and
        -not (Test-Path -LiteralPath $projectFile -PathType Leaf)) {
        $projectFile = ''
    }
    if ([string]::IsNullOrWhiteSpace($projectFile) -and $null -ne $Dte) {
        try {
            foreach ($solutionProject in @($Dte.Solution.Projects)) {
                $systemFile = [string]$solutionProject.FullName
                if (-not $systemFile.EndsWith('.tsproj', [StringComparison]::OrdinalIgnoreCase) -or
                    -not (Test-Path -LiteralPath $systemFile -PathType Leaf)) { continue }
                [xml]$systemXml = Get-Content -LiteralPath $systemFile -Raw -Encoding UTF8
                $safetyProject = @($systemXml.SelectNodes(
                    "//*[local-name()='Safety']/*[local-name()='Project']")) |
                    Where-Object { [string]$_.Name -ieq [string]$Item.Name } |
                    Select-Object -First 1
                if ($null -ne $safetyProject -and -not [string]::IsNullOrWhiteSpace([string]$safetyProject.PrjFilePath)) {
                    $systemDirectory = [IO.Path]::GetDirectoryName($systemFile)
                    $relativeProjectFile = [string]$safetyProject.PrjFilePath
                    $joinedProjectFile = Join-Path $systemDirectory $relativeProjectFile
                    $projectFile = [IO.Path]::GetFullPath($joinedProjectFile)
                    break
                }
            }
        } catch {
            if ([string]::IsNullOrWhiteSpace($readError)) { $readError = [string]$_.Exception.Message }
        }
    }
    foreach ($candidate in @($candidates.ToArray())) {
        try {
            [xml]$candidateXml = [string]$candidate.ProduceXml($false)
            [void]$xmlDocuments.Add($candidateXml)
        } catch {
            if ([string]::IsNullOrWhiteSpace($readError)) { $readError = [string]$_.Exception.Message }
        }
    }
    if (-not [string]::IsNullOrWhiteSpace($projectFile)) {
        try {
            $projectFile = [IO.Path]::GetFullPath($projectFile)
            if (Test-Path -LiteralPath $projectFile -PathType Leaf) {
                [xml]$fileXml = Get-Content -LiteralPath $projectFile -Raw -Encoding UTF8
                [void]$xmlDocuments.Add($fileXml)
            }
        } catch {
            if ([string]::IsNullOrWhiteSpace($readError)) { $readError = [string]$_.Exception.Message }
        }
    }
    foreach ($field in @('ProjectName','InternalProjectName','IntProjName','Author','Worker',
                          'TargetSystem','TargetType','ProgrammingLanguage','Language',
                          'ProjectGuid','CRC','Checksum')) {
        $values = @()
        foreach ($document in @($xmlDocuments.ToArray())) {
            $nodes = @($document.SelectNodes("//*[local-name()='$field']"))
            $values += @($nodes | ForEach-Object { ([string]$_.InnerText).Trim() } |
                Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
        }
        $values = @($values | Select-Object -Unique)
        if ($values.Count) { $metadata[$field] = @($values) }
    }
    $itemType = 0; $itemSubtype = 0
    try { $itemType = [int]$Item.ItemType } catch { }
    try { $itemSubtype = [int]$Item.ItemSubType } catch { }
    [pscustomobject]@{
        name = [string]$Item.Name
        path = [string]$Item.PathName
        item_type = $itemType
        item_subtype = $itemSubtype
        project_file = $projectFile
        metadata = [pscustomobject]$metadata
        xml_readable = ($xmlDocuments.Count -gt 0)
        xml_error = $readError
    }
}

function Get-TcSafetyStructure {
    param([Parameter(Mandatory)]$Dte, [int]$MaxDepth = 8)
    $root = Get-TcSafetyRoot $Dte
    $depth = [Math]::Max(0, [Math]::Min($MaxDepth, 12))
    [pscustomobject]@{
        status = 'ok'
        target_netid = [string](Get-TcSystemManager $Dte).GetTargetNetId()
        max_depth = $depth
        root = Get-TcSystemTreeNode $root 'TISC' 0 $depth
        project_count = @(Get-TcSafetyChildren $root).Count
        readonly = $true
    }
}

function Get-TcSafetyProjectInfo {
    param([Parameter(Mandatory)]$Dte, [string]$Project = '')
    if ([string]::IsNullOrWhiteSpace($Project)) {
        $root = Get-TcSafetyRoot $Dte
        $items = @(Get-TcSafetyChildren $root | ForEach-Object { Get-TcSafetyMetadata $_ $Dte })
        return [pscustomobject]@{ status='ok'; project_count=$items.Count; projects=$items; readonly=$true }
    }
    $item = Resolve-TcSafetyProject $Dte $Project
    [pscustomobject]@{ status='ok'; project=Get-TcSafetyMetadata $item $Dte; readonly=$true }
}

function Resolve-TcSafetyProjectSource {
    param([Parameter(Mandatory)]$Dte, [Parameter(Mandatory)][string]$Project)
    $selector = $Project.Trim()
    if ([string]::IsNullOrWhiteSpace($selector)) { throw 'Safety project name or .splcproj path is required.' }
    if (Test-Path -LiteralPath $selector) {
        $full = [IO.Path]::GetFullPath($selector)
        if (Test-Path -LiteralPath $full -PathType Container) {
            $matches = @(Get-ChildItem -LiteralPath $full -File -Filter '*.splcproj')
            if ($matches.Count -ne 1) { throw "Safety directory must contain exactly one .splcproj file: $full" }
            $full = $matches[0].FullName
        }
        if (-not $full.EndsWith('.splcproj', [StringComparison]::OrdinalIgnoreCase) -or
            -not (Test-Path -LiteralPath $full -PathType Leaf)) {
            throw "Safety source must be an existing .splcproj file or its directory: $full"
        }
        return [pscustomobject]@{
            source='filesystem'; name=[IO.Path]::GetFileNameWithoutExtension($full)
            project_file=$full; project_directory=[IO.Path]::GetDirectoryName($full); tisc_path=''
        }
    }
    $item = Resolve-TcSafetyProject $Dte $selector
    $metadata = Get-TcSafetyMetadata $item $Dte
    $projectFile = [string]$metadata.project_file
    if ([string]::IsNullOrWhiteSpace($projectFile) -or
        -not (Test-Path -LiteralPath $projectFile -PathType Leaf)) {
        throw "Safety project file could not be resolved for '$selector'."
    }
    [pscustomobject]@{
        source='tisc'; name=[string]$item.Name; project_file=[IO.Path]::GetFullPath($projectFile)
        project_directory=[IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($projectFile))
        tisc_path=Get-TcSafetyPath ([string]$item.PathName) $false
    }
}

function Convert-TcXmlAttributes {
    param($Node)
    $result = [ordered]@{}
    if ($null -ne $Node -and $null -ne $Node.Attributes) {
        foreach ($attribute in @($Node.Attributes)) { $result[[string]$attribute.Name] = [string]$attribute.Value }
    }
    [pscustomobject]$result
}

function Get-TcSafetyFileRole {
    param([Parameter(Mandatory)][string]$RelativePath)
    $name = [IO.Path]::GetFileName($RelativePath)
    $extension = [IO.Path]::GetExtension($RelativePath).ToLowerInvariant()
    if ($name -ieq 'TargetSystemConfig.xml') { return 'target_configuration' }
    switch ($extension) {
        '.splcproj' { 'project'; break }
        '.sds' { 'alias_device'; break }
        '.sal' { 'graphical_application'; break }
        '.diagram' { 'graphical_layout'; break }
        '.grp' { 'group_configuration'; break }
        '.pdts' { 'project_data_types'; break }
        '.pluts' { 'lookup_tables'; break }
        '.cpp' { 'safety_c_source'; break }
        '.h' { 'safety_c_header'; break }
        '.saxml' { 'analysis_data'; break }
        default { 'supporting_file' }
    }
}

function Get-TcSafetyFileInventory {
    param([Parameter(Mandatory)]$Dte, [Parameter(Mandatory)][string]$Project)
    $source = Resolve-TcSafetyProjectSource $Dte $Project
    [xml]$projectXml = Get-Content -LiteralPath $source.project_file -Raw -Encoding UTF8
    $projectProperty = @($projectXml.SelectNodes("/*[local-name()='Project']/*[local-name()='PropertyGroup']")) |
        Select-Object -First 1
    $references = New-Object System.Collections.ArrayList
    foreach ($node in @($projectXml.SelectNodes("//*[local-name()='None' and @Include]"))) {
        [void]$references.Add(([string]$node.Include).Replace('/', '\'))
    }
    $folders = @($projectXml.SelectNodes("//*[local-name()='Folder' and @Include]") |
        ForEach-Object { ([string]$_.Include).Replace('/', '\').TrimEnd('\') })
    $diskFiles = @(Get-ChildItem -LiteralPath $source.project_directory -Recurse -File)
    $files = foreach ($file in $diskFiles) {
        $relative = $file.FullName.Substring($source.project_directory.Length).TrimStart('\')
        [pscustomobject]@{
            path=$relative; role=Get-TcSafetyFileRole $relative; extension=$file.Extension.ToLowerInvariant()
            bytes=[int64]$file.Length; referenced=(@($references) -contains $relative)
        }
    }
    $missing = foreach ($reference in @($references)) {
        if ($reference.Contains('[!output')) { continue }
        $candidate = Join-Path $source.project_directory $reference
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { $reference }
    }
    $groups = @($folders | Where-Object { $_ -notmatch '\\' } |
        Where-Object { $_ -notin @('GVLs','User FBs') } | Select-Object -Unique)
    [pscustomobject]@{
        status='ok'; project=$source; target_system=[string]$projectProperty.TargetSystem
        programming_language=[string]$projectProperty.ProgrammingLanguage
        groups=$groups; folders=$folders; files=@($files); missing_references=@($missing)
        file_count=@($files).Count; readonly=$true
    }
}

function Get-TcSafetyTargetConfiguration {
    param([Parameter(Mandatory)]$Dte, [Parameter(Mandatory)][string]$Project)
    $source = Resolve-TcSafetyProjectSource $Dte $Project
    [xml]$projectXml = Get-Content -LiteralPath $source.project_file -Raw -Encoding UTF8
    $targetFile = Join-Path $source.project_directory 'TargetSystemConfig.xml'
    if (-not (Test-Path -LiteralPath $targetFile -PathType Leaf)) {
        throw "Safety target configuration is missing: $targetFile"
    }
    [xml]$targetXml = Get-Content -LiteralPath $targetFile -Raw -Encoding UTF8
    $fields = [ordered]@{}
    foreach ($node in @($targetXml.DocumentElement.ChildNodes)) {
        if ($node.NodeType -eq [Xml.XmlNodeType]::Element) { $fields[[string]$node.LocalName] = [string]$node.InnerText }
    }
    $property = @($projectXml.SelectNodes("/*[local-name()='Project']/*[local-name()='PropertyGroup']")) |
        Select-Object -First 1
    [pscustomobject]@{
        status='ok'; project=$source; project_guid=[string]$property.ProjectGuid
        target_system=[string]$property.TargetSystem; programming_language=[string]$property.ProgrammingLanguage
        author=[string]$property.Worker; internal_project_name=[string]$property.IntProjName
        safety_project_version=[string]$property.SPlcProjVersion
        target_config_crc=[string]$targetXml.DocumentElement.Crc
        target_config_version=[string]$targetXml.DocumentElement.Version
        target=[pscustomobject]$fields; readonly=$true
        note='Configuration readback only; target identity, safe addresses and CRC are not validated against hardware.'
    }
}

function Get-TcSafetyAliasDevices {
    param([Parameter(Mandatory)]$Dte, [Parameter(Mandatory)][string]$Project, [string]$Group = '')
    $inventory = Get-TcSafetyFileInventory $Dte $Project
    $base = [string]$inventory.project.project_directory
    $aliases = foreach ($entry in @($inventory.files | Where-Object role -eq 'alias_device')) {
        $relative = [string]$entry.path
        $parts = @($relative -split '\\')
        $groupName = if ($parts.Count -ge 3 -and $parts[1] -ieq 'Alias Devices') { $parts[0] } else { '' }
        if (-not [string]::IsNullOrWhiteSpace($Group) -and $groupName -ine $Group) { continue }
        [xml]$xml = Get-Content -LiteralPath (Join-Path $base $relative) -Raw -Encoding UTF8
        $root = $xml.DocumentElement
        $standardNode = $root.SelectSingleNode("./*[local-name()='StandardAliasDevice']")
        $deviceKind = if ($null -ne $standardNode) { 'standard' } else { 'safety' }
        $io = foreach ($node in @($root.SelectNodes("//*[local-name()='IO']"))) {
            [pscustomobject]@{
                name=[string]$node.Name; data_type=[string]$node.DataType
                bit_size=[string]$node.BitSize; bit_offset=[string]$node.BitOffsMessage
                attributes=Convert-TcXmlAttributes $node
            }
        }
        $mappingFields = [ordered]@{}
        foreach ($fieldName in @('LinkingMode','TargetSystemObjectId','TargetSystemObjectName','FSOEAddress',
                                  'SafeAddress','DipSwitch','AmsNetID','AmsPort')) {
            $node = $root.SelectSingleNode("//*[local-name()='$fieldName']")
            if ($null -ne $node) { $mappingFields[$fieldName] = [string]$node.InnerText }
        }
        [pscustomobject]@{
            name=[IO.Path]::GetFileNameWithoutExtension($relative); group=$groupName; path=$relative
            crc=[string]$root.Crc; file_format_version=[string]$root.FileFormatVersion
            sds_id=[string]($root.SelectSingleNode("//*[local-name()='SDSID']").InnerText)
            kind=$deviceKind
            type=[string]($root.SelectSingleNode("//*[local-name()='AliasDeviceType']/*[local-name()='Type']").InnerText)
            subtype=[string]($root.SelectSingleNode("//*[local-name()='AliasDeviceType']/*[local-name()='SubType']").InnerText)
            vendor_id=[string]($root.SelectSingleNode("//*[local-name()='AliasDeviceType']/*[local-name()='VendorId']").InnerText)
            mapping=[pscustomobject]$mappingFields; channels=@($io)
        }
    }
    [pscustomobject]@{ status='ok'; project=$inventory.project; group=$Group
        alias_count=@($aliases).Count; aliases=@($aliases); readonly=$true
        note='Readback only; safe-address uniqueness and physical hardware correspondence are not validated.' }
}

function Get-TcSafetyFunctionBlockPort {
    param([Parameter(Mandatory)]$Port, [Parameter(Mandatory)][string]$Direction)
    $coreNames = @('Id','name','portName','portNum','objectIndex','varId','portDataType','filter')
    $properties = [ordered]@{}
    foreach ($attribute in @($Port.Attributes)) {
        if ([string]$attribute.Name -notin $coreNames) {
            $properties[[string]$attribute.Name] = [string]$attribute.Value
        }
    }
    $globalVariables = @($Port.SelectNodes(".//*[local-name()='fbPortGlobalVariableReference']") |
        ForEach-Object {
            [pscustomobject]@{
                variable_id=[string]$_.variableId
                path=[string]$_.lastKnownPath
                attributes=Convert-TcXmlAttributes $_
            }
        })
    $wiredTargets = New-Object System.Collections.ArrayList
    foreach ($wire in @($Port.SelectNodes(".//*[local-name()='wiredLink']"))) {
        $moniker = @($wire.SelectNodes(".//*[contains(local-name(),'PortMoniker')]")) | Select-Object -First 1
        if ($null -ne $moniker) {
            [void]$wiredTargets.Add([pscustomobject]@{
                connection_id=[string]$wire.Id
                port_kind=[string]$moniker.LocalName
                path=[string]$moniker.name
            })
        }
    }
    [pscustomobject]@{
        direction=$Direction; id=[string]$Port.Id; name=[string]$Port.name
        port_name=[string]$Port.portName; port_number=[string]$Port.portNum
        object_index=[string]$Port.objectIndex; variable_id=[string]$Port.varId
        data_type=[string]$Port.portDataType; filter=[string]$Port.filter
        properties=[pscustomobject]$properties
        global_variables=$globalVariables; wired_targets=@($wiredTargets.ToArray())
    }
}

function Get-TcSafetyApplicationStructure {
    param([Parameter(Mandatory)]$Dte, [Parameter(Mandatory)][string]$Project, [string]$Group = '')
    $inventory = Get-TcSafetyFileInventory $Dte $Project
    $base = [string]$inventory.project.project_directory
    $applications = New-Object System.Collections.ArrayList
    foreach ($entry in @($inventory.files | Where-Object { $_.role -in @('graphical_application','group_configuration') })) {
        $relative = [string]$entry.path
        $parts = @($relative -split '\\')
        $groupName = if ($parts.Count -gt 1) { $parts[0] } else { [IO.Path]::GetFileNameWithoutExtension($relative) }
        if (-not [string]::IsNullOrWhiteSpace($Group) -and $groupName -ine $Group) { continue }
        [xml]$xml = Get-Content -LiteralPath (Join-Path $base $relative) -Raw -Encoding UTF8
        if ($entry.role -eq 'graphical_application') {
            $functionBlocks = New-Object System.Collections.ArrayList
            $connections = New-Object System.Collections.ArrayList
            $networks = @($xml.SelectNodes("//*[local-name()='Network']") | ForEach-Object {
                $network = $_
                $networkName = if (-not [string]::IsNullOrWhiteSpace([string]$network.networkName)) {
                    [string]$network.networkName
                } else { [string]$network.name }
                foreach ($block in @($network.SelectNodes(".//*[local-name()='networkHasFunctionBlocks']/*"))) {
                    $inputs = @($block.SelectNodes(".//*[local-name()='inPort']") |
                        ForEach-Object { Get-TcSafetyFunctionBlockPort $_ 'input' })
                    $outputs = @($block.SelectNodes(".//*[local-name()='outPort']") |
                        ForEach-Object { Get-TcSafetyFunctionBlockPort $_ 'output' })
                    $parameters = @($block.SelectNodes(".//*[local-name()='parameterPort']") |
                        ForEach-Object { Get-TcSafetyFunctionBlockPort $_ 'parameter' })
                    $instanceName = if (-not [string]::IsNullOrWhiteSpace([string]$block.instanceName)) {
                        [string]$block.instanceName
                    } else { [string]$block.name }
                    $coreNames = @('Id','name','instanceName','functionName','orderOfExecution','mapState','mapDiag')
                    $blockProperties = [ordered]@{}
                    foreach ($attribute in @($block.Attributes)) {
                        if ([string]$attribute.Name -notin $coreNames) {
                            $blockProperties[[string]$attribute.Name] = [string]$attribute.Value
                        }
                    }
                    foreach ($output in @($outputs)) {
                        $sourcePort = if (-not [string]::IsNullOrWhiteSpace([string]$output.port_name)) {
                            [string]$output.port_name
                        } else { [string]$output.name }
                        $sourcePath = "//$networkName/$instanceName/$sourcePort"
                        foreach ($target in @($output.wired_targets)) {
                            [void]$connections.Add([pscustomobject]@{
                                network=$networkName; connection_id=[string]$target.connection_id
                                source=$sourcePath; target=[string]$target.path
                                target_port_kind=[string]$target.port_kind
                            })
                        }
                    }
                    [void]$functionBlocks.Add([pscustomobject]@{
                        network=$networkName; element_type=[string]$block.LocalName
                        id=[string]$block.Id; name=[string]$block.name; instance_name=$instanceName
                        function_name=[string]$block.functionName
                        execution_order=[string]$block.orderOfExecution
                        map_state=[string]$block.mapState; map_diag=[string]$block.mapDiag
                        properties=[pscustomobject]$blockProperties
                        inputs=$inputs; outputs=$outputs; parameters=$parameters
                    })
                }
                [pscustomobject]@{ name=[string]$network.name; network_name=$networkName
                    id=[string]$network.intId; order=[string]$network.networkOrderId
                    function_block_count=@($network.SelectNodes(".//*[local-name()='networkHasFunctionBlocks']/*")).Count
                    attributes=Convert-TcXmlAttributes $network }
            })
            $ports = @($xml.SelectNodes("//*[local-name()='twinSAFEGroupAliasPort' and @name and @portName]") | ForEach-Object {
                [pscustomobject]@{ name=[string]$_.name; port_name=[string]$_.portName
                    sds_id=[string]$_.sdsId; channel_id=[string]$_.channelId; function_id=[string]$_.functionId }
            })
            $variables = @($xml.SelectNodes("//*[local-name()='variable']") | ForEach-Object {
                $variable = $_
                [pscustomobject]@{
                    name=[string]$variable.name
                    alias_usages=@($variable.SelectNodes(".//*[local-name()='aliasDeviceIoUsage']") | ForEach-Object {
                        [pscustomobject]@{ sds_id=[string]$_.sdsId; channel_id=[string]$_.channelId; function_id=[string]$_.functionId }
                    })
                    group_port_targets=@($variable.SelectNodes(".//*[local-name()='twinSAFEGroupAliasPortMoniker']") |
                        ForEach-Object { [string]$_.name })
                    function_block_ports=@($variable.SelectNodes(".//*[contains(local-name(),'PortMoniker') and " +
                        "not(local-name()='twinSAFEGroupAliasPortMoniker')]") | ForEach-Object {
                            [pscustomobject]@{ port_kind=[string]$_.LocalName; path=[string]$_.name }
                        })
                }
            })
            $counts = [ordered]@{}
            foreach ($nodeGroup in @($xml.SelectNodes('//*') | Group-Object LocalName | Sort-Object Name)) {
                $counts[[string]$nodeGroup.Name] = [int]$nodeGroup.Count
            }
            [void]$applications.Add([pscustomobject]@{
                group=$groupName; path=$relative; language='GraphicalEditor'; crc=[string]$xml.DocumentElement.Crc
                networks=$networks; group_ports=$ports; variables=$variables
                function_blocks=@($functionBlocks.ToArray()); connections=@($connections.ToArray())
                function_block_count=$functionBlocks.Count; connection_count=$connections.Count
                parameter_count=[int](($functionBlocks | ForEach-Object { @($_.parameters).Count } |
                    Measure-Object -Sum).Sum)
                element_counts=[pscustomobject]$counts
            })
        } else {
            $general = [ordered]@{}
            $generalNode = $xml.SelectSingleNode("//*[local-name()='GroupGeneralConfig']")
            if ($null -ne $generalNode) {
                foreach ($node in @($generalNode.ChildNodes)) {
                    if ($node.NodeType -eq [Xml.XmlNodeType]::Element) { $general[[string]$node.LocalName]=[string]$node.InnerText }
                }
            }
            $mappings = @($xml.SelectNodes("//*[local-name()='GroupPortsConfig']/*/*[local-name()='Mapping']") | ForEach-Object {
                [pscustomobject]@{ port=[string]$_.ParentNode.LocalName; group_name=[string]$_.GroupName
                    sds_id=[string]$_.SdsId; direction=[string]$_.Direction
                    function_id=[string]$_.FunctionId; channel_id=[string]$_.ChannelId }
            })
            [void]$applications.Add([pscustomobject]@{
                group=$groupName; path=$relative; language='SafetyC'; crc=[string]$xml.DocumentElement.Crc
                general=[pscustomobject]$general; group_port_mappings=$mappings
                source_files=@($inventory.files | Where-Object {
                    $_.role -in @('safety_c_source','safety_c_header') -and
                    ([string]$_.path).StartsWith($groupName + '\', [StringComparison]::OrdinalIgnoreCase)
                } |
                    ForEach-Object path)
            })
        }
    }
    [pscustomobject]@{ status='ok'; project=$inventory.project; group=$Group
        application_count=$applications.Count; applications=@($applications.ToArray()); readonly=$true
        scope='structural_readback_only'
        note='This does not verify Safety semantics, certified function-block use, hardware mappings, CRC or commissioning.' }
}

function Test-TcSafetyLogicStructure {
    param([Parameter(Mandatory)]$Dte, [Parameter(Mandatory)][string]$Project, [string]$Group = '')
    $issues = New-Object System.Collections.ArrayList
    $addIssue = {
        param([string]$Severity, [string]$Code, [string]$Message, [string]$Path)
        [void]$issues.Add([pscustomobject]@{ severity=$Severity; code=$Code; message=$Message; path=$Path })
    }
    $aliasesResult = Get-TcSafetyAliasDevices $Dte $Project $Group
    $applicationResult = Get-TcSafetyApplicationStructure $Dte $Project $Group
    $targetResult = Get-TcSafetyTargetConfiguration $Dte $Project
    $aliases = @($aliasesResult.aliases)
    if ($aliases.Count -eq 0) {
        & $addIssue 'warning' 'no_alias_devices' 'No Alias Devices are stored in the selected Safety project/group.' ([string]$applicationResult.project.project_file)
    }
    if (@($applicationResult.applications).Count -eq 0) {
        & $addIssue 'warning' 'no_safety_application' 'No SAL application or Safety C group configuration is stored in the selected project/group.' ([string]$applicationResult.project.project_file)
    }
    $targetObjectId = [string]$targetResult.target.TargetSystemObjectId
    $targetObjectName = [string]$targetResult.target.TargetSystemObjectName
    if ([string]::IsNullOrWhiteSpace($targetObjectId) -and [string]::IsNullOrWhiteSpace($targetObjectName)) {
        & $addIssue 'warning' 'target_mapping_missing' 'The stored Safety target has no TargetSystemObjectId or TargetSystemObjectName mapping.' 'TargetSystemConfig.xml'
    }
    $knownSdsIds = @{}
    foreach ($alias in $aliases) {
        $sdsId = [string]$alias.sds_id
        if ([string]::IsNullOrWhiteSpace($sdsId)) {
            & $addIssue 'warning' 'alias_sds_id_missing' "Alias Device '$($alias.name)' has no stored SDS ID." ([string]$alias.path)
        } elseif ($knownSdsIds.ContainsKey($sdsId)) {
            & $addIssue 'error' 'duplicate_sds_id' "SDS ID '$sdsId' is used by more than one Alias Device." ([string]$alias.path)
        } else { $knownSdsIds[$sdsId] = [string]$alias.path }
    }
    foreach ($application in @($applicationResult.applications | Where-Object language -eq 'GraphicalEditor')) {
        if (@($application.function_blocks).Count -eq 0) {
            & $addIssue 'warning' 'no_function_blocks' (
                "Graphical Safety application '$($application.group)' contains no function blocks.") ([string]$application.path)
        }
        $knownPorts = @{}
        foreach ($block in @($application.function_blocks)) {
            foreach ($port in @($block.inputs) + @($block.outputs) + @($block.parameters)) {
                $portName = if (-not [string]::IsNullOrWhiteSpace([string]$port.port_name)) {
                    [string]$port.port_name
                } else { [string]$port.name }
                $knownPorts["//$($block.network)/$($block.instance_name)/$portName"] = $true
            }
        }
        foreach ($orderGroup in @($application.function_blocks | Where-Object {
                    -not [string]::IsNullOrWhiteSpace([string]$_.execution_order)
                } | Group-Object network,execution_order | Where-Object Count -gt 1)) {
            & $addIssue 'error' 'duplicate_execution_order' (
                "Function blocks share execution order '$($orderGroup.Group[0].execution_order)' " +
                "in network '$($orderGroup.Group[0].network)'.") ([string]$application.path)
        }
        foreach ($connection in @($application.connections)) {
            if (-not $knownPorts.ContainsKey([string]$connection.source)) {
                & $addIssue 'error' 'connection_source_missing' "Connection source port was not found: $($connection.source)" ([string]$application.path)
            }
            if (-not $knownPorts.ContainsKey([string]$connection.target)) {
                & $addIssue 'error' 'connection_target_missing' "Connection target port was not found: $($connection.target)" ([string]$application.path)
            }
        }
        foreach ($variable in @($application.variables)) {
            foreach ($usage in @($variable.alias_usages)) {
                $sdsId = [string]$usage.sds_id
                if (-not [string]::IsNullOrWhiteSpace($sdsId) -and -not $knownSdsIds.ContainsKey($sdsId)) {
                    & $addIssue 'error' 'alias_reference_missing' (
                        "Variable '$($variable.name)' references unknown SDS ID '$sdsId'.") ([string]$application.path)
                }
            }
            foreach ($usage in @($variable.function_block_ports)) {
                if (-not $knownPorts.ContainsKey([string]$usage.path)) {
                    & $addIssue 'error' 'variable_port_reference_missing' (
                        "Variable '$($variable.name)' references a missing function-block port: $($usage.path)") ([string]$application.path)
                }
            }
        }
    }
    $errors = @($issues | Where-Object severity -eq 'error').Count
    $warnings = @($issues | Where-Object severity -eq 'warning').Count
    [pscustomobject]@{
        status=if ($errors) { 'invalid' } elseif ($warnings) { 'incomplete' } else { 'valid' }
        valid=($errors -eq 0 -and $warnings -eq 0); structurally_consistent=($errors -eq 0)
        project=$applicationResult.project; group=$Group
        alias_count=$aliases.Count; application_count=@($applicationResult.applications).Count
        function_block_count=[int](($applicationResult.applications |
            Measure-Object function_block_count -Sum).Sum)
        connection_count=[int](($applicationResult.applications |
            Measure-Object connection_count -Sum).Sum)
        error_count=$errors; warning_count=$warnings; issues=@($issues.ToArray())
        readonly=$true; scope='stored_structure_consistency_only'
        note='This is not TwinSAFE Verify. It does not establish safety integrity, certified FB suitability, CRC acceptance, hardware identity, download correctness or commissioning.'
    }
}

function Test-TcSafetyConfiguration {
    param([Parameter(Mandatory)]$Dte, [string]$Source = '')
    $issues = New-Object System.Collections.ArrayList
    $addIssue = {
        param([string]$Severity, [string]$Code, [string]$Message, [string]$Path)
        [void]$issues.Add([pscustomobject]@{ severity=$Severity; code=$Code; message=$Message; path=$Path })
    }
    $sourceInfo = $null
    if (-not [string]::IsNullOrWhiteSpace($Source)) {
        $full = [IO.Path]::GetFullPath($Source)
        $extension = [IO.Path]::GetExtension($full).ToLowerInvariant()
        $exists = Test-Path -LiteralPath $full -PathType Leaf
        $sourceInfo = [pscustomobject]@{ path=$full; extension=$extension; exists=$exists
            readable=$false; validation_level='container_syntax_only' }
        if ($extension -notin @('.splcproj','.tfzip')) {
            & $addIssue 'error' 'unsupported_source_type' 'Safety source must be .splcproj or .tfzip.' $full
        } elseif (-not $exists) {
            & $addIssue 'error' 'source_not_found' 'Safety source file was not found.' $full
        } else {
            try {
                if ($extension -eq '.splcproj') { [xml](Get-Content -LiteralPath $full -Raw -Encoding UTF8) | Out-Null }
                else {
                    $stream = [IO.File]::OpenRead($full)
                    try {
                        if ($stream.ReadByte() -ne 0x50 -or $stream.ReadByte() -ne 0x4B) {
                            throw 'file does not have a ZIP signature'
                        }
                    } finally { $stream.Dispose() }
                }
                $sourceInfo.readable = $true
                & $addIssue 'warning' 'source_semantics_not_verified' (
                    'The source container is readable, but Safety project semantics, target compatibility, ' +
                    'logic and CRC are not validated offline.') $full
            } catch { & $addIssue 'error' 'source_invalid' ([string]$_.Exception.Message) $full }
        }
    }
    $projects = @()
    try {
        $root = Get-TcSafetyRoot $Dte
        $projects = @(Get-TcSafetyChildren $root | ForEach-Object { Get-TcSafetyMetadata $_ $Dte })
        foreach ($project in $projects) {
            if (-not $project.xml_readable) {
                & $addIssue 'warning' 'project_xml_unreadable' $project.xml_error $project.path
            }
        }
        if ($projects.Count -eq 0) {
            & $addIssue 'warning' 'no_safety_project' 'No Safety project exists under TISC.' 'TISC'
        }
    } catch { & $addIssue 'error' 'safety_root_unavailable' ([string]$_.Exception.Message) 'TISC' }
    $errors = @($issues | Where-Object severity -eq 'error').Count
    $warnings = @($issues | Where-Object severity -eq 'warning').Count
    [pscustomobject]@{
        status = if ($errors) { 'invalid' } else { 'valid' }
        valid = ($errors -eq 0); error_count=$errors; warning_count=$warnings
        issues=@($issues.ToArray()); source=$sourceInfo; projects=@($projects)
        scope='structural_only'
        note='This does not validate Safety logic, risk assessment, CRC acceptance, download or commissioning.'
        readonly=$true
    }
}

function Import-TcSafetyProject {
    param([Parameter(Mandatory)]$Dte, [string]$Name = '',
          [Parameter(Mandatory)][string]$Source,
          [ValidateSet('copy','move','reference')][string]$Mode = 'copy',
          [bool]$Apply = $false, [bool]$ConfirmSourceMove = $false,
          [bool]$AcknowledgeSafetyReview = $false)
    $full = [IO.Path]::GetFullPath($Source)
    if (-not (Test-Path -LiteralPath $full -PathType Leaf)) { throw "Safety source file not found: $full" }
    $extension = [IO.Path]::GetExtension($full).ToLowerInvariant()
    if ($extension -notin @('.splcproj','.tfzip')) { throw 'Safety source must be .splcproj or .tfzip.' }
    $projectName = $Name.Trim()
    if ($Mode -eq 'reference') { $projectName = '' }
    elseif ([string]::IsNullOrWhiteSpace($projectName) -or $projectName.Contains('^') -or $projectName -in @('.','..')) {
        throw 'Safety project name must be one non-empty path segment for copy/move mode.'
    }
    $subtype = @{ copy=0; move=1; reference=2 }[$Mode]
    $root = Get-TcSafetyRoot $Dte
    if ($projectName) {
        foreach ($child in @(Get-TcSafetyChildren $root)) {
            if ([string]$child.Name -ieq $projectName) { throw "Safety project already exists: $projectName" }
        }
    }
    $payload = [ordered]@{
        status='preview'; source=$full; extension=$extension; mode=$Mode; subtype=$subtype
        requested_name=$projectName; source_will_move=($Mode -eq 'move')
        requires_safety_review=$true; configuration_tree_will_change=$true
        configuration_tree_changed=$false
        downloads_to_safety_target=$false; activates_configuration=$false; applied=$false
    }
    if (-not $Apply) { return [pscustomobject]$payload }
    if (-not $AcknowledgeSafetyReview) { throw 'apply=true requires acknowledge_safety_review=true.' }
    if ($Mode -eq 'move' -and -not $ConfirmSourceMove) {
        throw 'move mode changes the source location; set confirm_source_move=true.'
    }
    $beforeCount = @(Get-TcSafetyChildren $root).Count
    $created = $root.CreateChild($projectName, [int]$subtype, '', $full)
    try { $Dte.ExecuteCommand('File.SaveAll') } catch { throw "Safety project was imported but File.SaveAll failed: $($_.Exception.Message)" }
    $afterCount = @(Get-TcSafetyChildren $root).Count
    if ($afterCount -le $beforeCount -and $null -eq $created) { throw 'Safety import returned without a verifiable new project.' }
    $payload.status='imported'; $payload.applied=$true; $payload.saved=$true
    $payload.configuration_tree_changed=$true
    $payload.created_name = try { [string]$created.Name } catch { '' }
    $payload.created_path = try { [string]$created.PathName } catch { '' }
    $payload.readback_verified = ($afterCount -gt $beforeCount -or $null -ne $created)
    [pscustomobject]$payload
}

function New-TcSafetyProject {
    param([Parameter(Mandatory)]$Dte, [Parameter(Mandatory)][string]$Name,
          [ValidateSet('hardware','twincat-safety-plc')][string]$Target = 'hardware',
          [ValidateSet('empty','preconfigured-errack','preconfigured-inputs')][string]$Template = 'preconfigured-inputs',
          [string]$Author = 'TwinCAT Agent', [string]$InternalProjectName = '',
          [bool]$Apply = $false, [bool]$AcknowledgeSafetyReview = $false)
    $projectName = $Name.Trim()
    if ([string]::IsNullOrWhiteSpace($projectName) -or $projectName.Contains('^') -or
        $projectName -in @('.','..') -or $projectName.IndexOfAny([IO.Path]::GetInvalidFileNameChars()) -ge 0) {
        throw 'Safety project name must be a valid non-empty file/path segment.'
    }
    $internalName = if ([string]::IsNullOrWhiteSpace($InternalProjectName)) {
        $projectName
    } else { $InternalProjectName.Trim() }
    $root = Get-TcSafetyRoot $Dte
    foreach ($child in @(Get-TcSafetyChildren $root)) {
        if ([string]$child.Name -ieq $projectName) { throw "Safety project already exists: $projectName" }
    }
    $templatePrefix = switch ($Template) {
        'empty' { 'Empty' }
        'preconfigured-errack' { 'Default' }
        'preconfigured-inputs' { 'DefaultExt' }
    }
    $templateConfigName = if ($Template -eq 'empty') {
        'TcSafetyEmptyProjectConfig.xml'
    } else { 'TcSafetyDefaultProjectConfig.xml' }
    $templateFolderName = if ($Target -eq 'hardware') {
        if ($Template -eq 'empty') { 'Empty_HardwareSafetyPLC' }
        else { "${templatePrefix}_HardwareSafetyPLC_GraphicalEditor" }
    } else { "${templatePrefix}_TwinCATSafetyPLC_SafetyC" }
    $targetSystem = if ($Target -eq 'hardware') { 'HSafetyPLC' } else { 'TSafetyPLC' }
    $language = if ($Target -eq 'hardware') { 'GraphicalEditor' } else { 'SafetyC' }
    $templateRoots = @(
        (Join-Path ${env:ProgramFiles(x86)} 'Beckhoff\TwinCAT\3.1\Components\Safety\ProjectTemplates'),
        (Join-Path ${env:ProgramFiles(x86)} 'Beckhoff\TwinCAT\3.1\Components\Base\SafetyTemplate\Templates'),
        'C:\TwinCAT\3.1\Components\Safety\ProjectTemplates'
    ) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) -and (Test-Path -LiteralPath $_ -PathType Container) }
    $templateCandidates = New-Object System.Collections.ArrayList
    foreach ($templateRoot in $templateRoots) {
        foreach ($versionDirectory in @(Get-ChildItem -LiteralPath $templateRoot -Directory -ErrorAction SilentlyContinue)) {
            $candidate = Join-Path (Join-Path $versionDirectory.FullName 'Templates') $templateFolderName
            if (-not (Test-Path -LiteralPath $candidate -PathType Container)) {
                $candidate = Join-Path $versionDirectory.FullName $templateFolderName
            }
            if (Test-Path -LiteralPath (Join-Path $candidate 'TcTemplateDrv.splcproj') -PathType Leaf) {
                $version = [version]'0.0'
                try { $version = [version]$versionDirectory.Name } catch { }
                [void]$templateCandidates.Add([pscustomobject]@{ version=$version; path=$candidate })
            }
        }
    }
    $selected = @($templateCandidates | Sort-Object version -Descending | Select-Object -First 1)
    if ($selected.Count -ne 1) { throw "No installed Safety template '$Template' was found for target '$Target'." }
    $templateDirectory = [IO.Path]::GetFullPath([string]$selected[0].path)
    $templateProject = Join-Path $templateDirectory 'TcTemplateDrv.splcproj'
    $templateConfig = Join-Path $templateDirectory $templateConfigName
    $targetConfig = Join-Path $templateDirectory 'TargetSystemConfig.xml'
    foreach ($required in @($templateProject, $templateConfig, $targetConfig)) {
        if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
            throw "Safety template is incomplete: $required"
        }
    }
    $payload = [ordered]@{
        status='preview'; name=$projectName; target=$Target; template=$Template; target_system=$targetSystem
        programming_language=$language; author=$Author; internal_project_name=$internalName
        template_version=[string]$selected[0].version; template_directory=$templateDirectory
        strategy='materialize_installed_beckhoff_template_then_import_copy'
        requires_safety_review=$true; downloads_to_safety_target=$false
        activates_configuration=$false; configuration_tree_will_change=$true
        configuration_tree_changed=$false; applied=$false
    }
    if (-not $Apply) { return [pscustomobject]$payload }
    if (-not $AcknowledgeSafetyReview) { throw 'apply=true requires acknowledge_safety_review=true.' }
    $staging = Join-Path ([IO.Path]::GetTempPath()) ('TwinCAT-Agent-Safety-' + [guid]::NewGuid().ToString('N'))
    try {
        [void][IO.Directory]::CreateDirectory($staging)
        $guid = ([guid]::NewGuid().ToString('B')).ToUpperInvariant()
        $replacements = [ordered]@{
            '[!output NEW_GUID_REGISTRY_FORMAT]' = $guid
            '[!output TARGET_SYSTEM]' = [Security.SecurityElement]::Escape($targetSystem)
            '[!output PROG_LANGUAGE]' = [Security.SecurityElement]::Escape($language)
            '[!output WORKER]' = [Security.SecurityElement]::Escape([string]$Author)
            '[!output INTERNPROJECTNAME]' = [Security.SecurityElement]::Escape($internalName)
            '[!output PROJECT_NAME]' = [Security.SecurityElement]::Escape($projectName)
            '[!output GROUP_NAME]' = 'TwinSafeGroup1'
            '[!output CREATION_DATE]' = [DateTime]::Now.ToString('yyyy-MM-dd')
        }
        [xml]$generatorConfig = [IO.File]::ReadAllText($templateConfig, [Text.Encoding]::UTF8)
        $fileDescriptions = @($generatorConfig.ProjectFileGeneratorConfig.FileDescription)
        foreach ($description in $fileDescriptions) {
            $symbolName = [string]$description.TargetFile.symbolName
            if (-not [string]::IsNullOrWhiteSpace($symbolName)) {
                $targetRelative = [string]$description.TargetFile.'#text'
                if ([string]::IsNullOrWhiteSpace($targetRelative)) { $targetRelative = [string]$description.TargetFile }
                $targetRelative = $targetRelative.Replace('[!output PROJECT_NAME]', $projectName)
                $replacements["[!output $symbolName]"] = $targetRelative
            }
        }
        $stagingRoot = [IO.Path]::GetFullPath($staging).TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
        foreach ($description in $fileDescriptions) {
            $sourceFile = Join-Path $templateDirectory ([string]$description.SourceFile)
            $targetRelative = [string]$description.TargetFile.'#text'
            if ([string]::IsNullOrWhiteSpace($targetRelative)) { $targetRelative = [string]$description.TargetFile }
            $targetRelative = $targetRelative.Replace('[!output PROJECT_NAME]', $projectName)
            $destination = [IO.Path]::GetFullPath((Join-Path $staging $targetRelative))
            if (-not $destination.StartsWith($stagingRoot, [StringComparison]::OrdinalIgnoreCase)) {
                throw "Safety template target escapes staging directory: $targetRelative"
            }
            [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($destination))
            $fileContent = [IO.File]::ReadAllText($sourceFile, [Text.Encoding]::UTF8)
            foreach ($entry in $replacements.GetEnumerator()) {
                $fileContent = $fileContent.Replace([string]$entry.Key, [string]$entry.Value)
            }
            if ($fileContent.Contains('[!output')) {
                throw "Installed Safety template contains unresolved wizard placeholders: $($description.SourceFile)"
            }
            [IO.File]::WriteAllText($destination, $fileContent, [Text.UTF8Encoding]::new($false))
        }
        $stagedProject = Join-Path $staging ($projectName + '.splcproj')
        $imported = Import-TcSafetyProject $Dte $projectName $stagedProject 'copy' $true $false $true
        $payload.status='created'; $payload.applied=$true; $payload.project_guid=$guid
        $payload.configuration_tree_changed=$true
        $payload.import=$imported; $payload.staging_cleaned=$false
    } finally {
        if (Test-Path -LiteralPath $staging -PathType Container) {
            [IO.Directory]::Delete($staging, $true)
        }
    }
    $payload.staging_cleaned=$true
    [pscustomobject]$payload
}

function Export-TcSafetyProject {
    param([Parameter(Mandatory)]$Dte, [Parameter(Mandatory)][string]$Project,
          [Parameter(Mandatory)][string]$OutputFile, [bool]$Overwrite = $false,
          [bool]$Apply = $false, [bool]$AcknowledgeSafetyReview = $false)
    $item = Resolve-TcSafetyProject $Dte $Project
    $full = [IO.Path]::GetFullPath($OutputFile)
    if ([IO.Path]::GetExtension($full).ToLowerInvariant() -ne '.tfzip') {
        throw 'Safety export output must use the .tfzip extension.'
    }
    $parentDirectory = [IO.Path]::GetDirectoryName($full)
    if (-not (Test-Path -LiteralPath $parentDirectory -PathType Container)) {
        throw "Safety export directory does not exist: $parentDirectory"
    }
    if ((Test-Path -LiteralPath $full) -and -not $Overwrite) { throw "Output already exists: $full" }
    $projectMetadata = Get-TcSafetyMetadata $item $Dte
    $projectFile = [string]$projectMetadata.project_file
    if ([string]::IsNullOrWhiteSpace($projectFile) -or
        -not (Test-Path -LiteralPath $projectFile -PathType Leaf)) {
        throw 'Safety project file could not be resolved from the current .tsproj.'
    }
    $projectDirectory = [IO.Path]::GetFullPath([IO.Path]::GetDirectoryName($projectFile))
    if ($full.StartsWith($projectDirectory + [IO.Path]::DirectorySeparatorChar,
                        [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Safety export output must be outside the Safety project directory.'
    }
    $payload = [ordered]@{ status='preview'; project=[string]$item.Name; path=[string]$item.PathName
        project_file=$projectFile; project_directory=$projectDirectory
        output_file=$full; overwrite=$Overwrite; requires_safety_review=$true
        strategy='zip_project_directory'; xae_reimport_verified=$false; applied=$false }
    if (-not $Apply) { return [pscustomobject]$payload }
    if (-not $AcknowledgeSafetyReview) { throw 'apply=true requires acknowledge_safety_review=true.' }
    if (Test-Path -LiteralPath $full) { [IO.File]::Delete($full) }
    Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction Stop
    [IO.Compression.ZipFile]::CreateFromDirectory(
        $projectDirectory, $full, [IO.Compression.CompressionLevel]::Optimal, $false)
    if (-not (Test-Path -LiteralPath $full -PathType Leaf) -or (Get-Item -LiteralPath $full).Length -le 0) {
        throw 'Safety export did not create a non-empty archive.'
    }
    $archive = $null
    try {
        $archive = [IO.Compression.ZipFile]::OpenRead($full)
        $projectEntries = @($archive.Entries | Where-Object {
            [IO.Path]::GetExtension([string]$_.FullName) -ieq '.splcproj'
        })
        if ($projectEntries.Count -lt 1) { throw 'Safety archive contains no .splcproj file.' }
        $payload.archive_entries = @($archive.Entries | ForEach-Object { [string]$_.FullName })
    } finally {
        if ($null -ne $archive) { $archive.Dispose() }
    }
    $payload.status='exported'; $payload.applied=$true
    $payload.bytes=(Get-Item -LiteralPath $full).Length; $payload.readback_verified=$true
    [pscustomobject]$payload
}

function Remove-TcSafetyProject {
    param([Parameter(Mandatory)]$Dte, [Parameter(Mandatory)][string]$Project,
          [bool]$Apply = $false, [string]$ConfirmProjectName = '',
          [bool]$AcknowledgeSafetyReview = $false)
    $item = Resolve-TcSafetyProject $Dte $Project
    $name = [string]$item.Name
    $path = Get-TcSafetyPath ([string]$item.PathName) $false
    $children = @(Get-TcSafetyChildren $item).Count
    $projectMetadata = Get-TcSafetyMetadata $item $Dte
    $projectFile = [string]$projectMetadata.project_file
    $projectDirectory = if ([string]::IsNullOrWhiteSpace($projectFile)) {
        ''
    } else { [IO.Path]::GetDirectoryName($projectFile) }
    $projectFileExistsBefore = (-not [string]::IsNullOrWhiteSpace($projectFile) -and
        (Test-Path -LiteralPath $projectFile -PathType Leaf))
    $payload = [ordered]@{ status='preview'; name=$name; path=$path; child_count=$children
        requires_export_or_backup=$true; requires_safety_review=$true
        operation='remove_from_tisc_configuration'; removed_from_configuration=$false
        project_file=$projectFile; project_directory=$projectDirectory
        project_file_exists_before=$projectFileExistsBefore
        project_files_deleted=$false; applied=$false }
    if (-not $Apply) { return [pscustomobject]$payload }
    if (-not $AcknowledgeSafetyReview) { throw 'apply=true requires acknowledge_safety_review=true.' }
    if ($ConfirmProjectName -cne $name) { throw "confirm_project_name must exactly match '$name'." }
    $root = Get-TcSafetyRoot $Dte
    [void]$root.DeleteChild($name)
    try { (Get-TcSystemManager $Dte).LookupTreeItem($path) | Out-Null; throw "Safety project still exists after removal: $path" } catch {
        if ([string]$_.Exception.Message -like 'Safety project still exists*') { throw }
    }
    try { $Dte.ExecuteCommand('File.SaveAll') } catch { throw "Safety project was removed but File.SaveAll failed: $($_.Exception.Message)" }
    $payload.status='removed'; $payload.applied=$true; $payload.saved=$true
    $payload.removed_from_configuration=$true; $payload.readback_verified=$true
    $payload.project_file_exists_after=(-not [string]::IsNullOrWhiteSpace($projectFile) -and
        (Test-Path -LiteralPath $projectFile -PathType Leaf))
    $payload.project_files_preserved=$payload.project_file_exists_after
    [pscustomobject]$payload
}

function Delete-TcSafetyProject {
    param([Parameter(Mandatory)]$Dte, [Parameter(Mandatory)][string]$Project,
          [string]$BackupFile = '', [bool]$Apply = $false,
          [string]$ConfirmProjectName = '', [bool]$ConfirmDeleteFiles = $false,
          [bool]$AcknowledgeSafetyReview = $false)
    $selector = $Project.Trim()
    if ([string]::IsNullOrWhiteSpace($selector)) { throw 'Safety project name, .splcproj path or project directory is required.' }
    $solutionFile = [IO.Path]::GetFullPath([string]$Dte.Solution.FullName)
    $solutionDirectory = [IO.Path]::GetFullPath([IO.Path]::GetDirectoryName($solutionFile)).TrimEnd('\')
    $solutionPrefix = $solutionDirectory + [IO.Path]::DirectorySeparatorChar
    $item = $null
    try { $item = Resolve-TcSafetyProject $Dte $selector } catch { }
    $locatedFrom = 'tisc'
    if ($null -ne $item) {
        $name = [string]$item.Name
        $path = Get-TcSafetyPath ([string]$item.PathName) $false
        $metadata = Get-TcSafetyMetadata $item $Dte
        $projectFile = [string]$metadata.project_file
    } else {
        $locatedFrom = 'filesystem_orphan'
        $path = ''
        $candidates = @()
        if (Test-Path -LiteralPath $selector) {
            $fullSelector = [IO.Path]::GetFullPath($selector)
            if (Test-Path -LiteralPath $fullSelector -PathType Container) {
                $candidates = @(Get-ChildItem -LiteralPath $fullSelector -File -Filter '*.splcproj')
            } elseif ($fullSelector.EndsWith('.splcproj', [StringComparison]::OrdinalIgnoreCase)) {
                $candidates = @((Get-Item -LiteralPath $fullSelector))
            }
        } else {
            $candidates = @(Get-ChildItem -LiteralPath $solutionDirectory -Recurse -File -Filter '*.splcproj' |
                Where-Object {
                    [IO.Path]::GetFileNameWithoutExtension($_.Name) -ieq $selector -or
                    $_.Directory.Name -ieq $selector
                })
        }
        if ($candidates.Count -ne 1) {
            throw "Safety project '$selector' was not found uniquely under TISC or as one .splcproj inside the current solution directory."
        }
        $projectFile = [string]$candidates[0].FullName
        $name = [IO.Path]::GetFileNameWithoutExtension($projectFile)
    }
    if (-not [string]::IsNullOrWhiteSpace($projectFile)) {
        $projectFile = [IO.Path]::GetFullPath($projectFile)
    }
    if ([string]::IsNullOrWhiteSpace($projectFile) -or
        -not $projectFile.EndsWith('.splcproj', [StringComparison]::OrdinalIgnoreCase) -or
        -not (Test-Path -LiteralPath $projectFile -PathType Leaf)) {
        throw 'Safety project file could not be resolved to an existing .splcproj; use tc_safety_remove instead.'
    }
    $projectDirectory = [IO.Path]::GetFullPath([IO.Path]::GetDirectoryName($projectFile)).TrimEnd('\')
    if ($projectDirectory -eq $solutionDirectory -or
        -not $projectDirectory.StartsWith($solutionPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw ('Refusing to delete a Safety project directory outside the current solution directory. ' +
               'Use tc_safety_remove to preserve externally located files.')
    }
    if ([string]::IsNullOrWhiteSpace($BackupFile)) {
        $backupDirectory = Join-Path $solutionDirectory '.TwinCATAgent\SafetyBackups'
        $backupName = $name + '-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.tfzip'
        $backupFull = Join-Path $backupDirectory $backupName
    } else {
        $backupFull = [IO.Path]::GetFullPath($BackupFile)
        if ([IO.Path]::GetExtension($backupFull).ToLowerInvariant() -ne '.tfzip') {
            throw 'backup_file must use the .tfzip extension.'
        }
        $backupDirectory = [IO.Path]::GetDirectoryName($backupFull)
    }
    if ($backupFull.StartsWith($projectDirectory + [IO.Path]::DirectorySeparatorChar,
                               [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Safety deletion backup must be outside the project directory.'
    }
    $payload = [ordered]@{
        status='preview'; name=$name; path=$path; operation='delete_project_and_files'
        located_from=$locatedFrom
        project_file=$projectFile; project_directory=$projectDirectory
        backup_file=$backupFull; backup_required=$true; backup_created=$false
        configuration_present=($null -ne $item); removed_from_configuration=$false
        configuration_removal_skipped=($null -eq $item); project_files_deleted=$false
        requires_safety_review=$true; applied=$false
    }
    if (-not $Apply) { return [pscustomobject]$payload }
    if (-not $AcknowledgeSafetyReview) { throw 'apply=true requires acknowledge_safety_review=true.' }
    if (-not $ConfirmDeleteFiles) { throw 'apply=true requires confirm_delete_files=true.' }
    if ($ConfirmProjectName -cne $name) { throw "confirm_project_name must exactly match '$name'." }
    if (-not (Test-Path -LiteralPath $backupDirectory -PathType Container)) {
        [void][IO.Directory]::CreateDirectory($backupDirectory)
    }
    if ($null -ne $item) {
        $exported = Export-TcSafetyProject $Dte $name $backupFull $false $true $true
    } else {
        if (Test-Path -LiteralPath $backupFull) { throw "Safety deletion backup already exists: $backupFull" }
        Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction Stop
        [IO.Compression.ZipFile]::CreateFromDirectory(
            $projectDirectory, $backupFull, [IO.Compression.CompressionLevel]::Optimal, $false)
        $archive = $null
        try {
            $archive = [IO.Compression.ZipFile]::OpenRead($backupFull)
            $projectEntries = @($archive.Entries | Where-Object {
                [IO.Path]::GetExtension([string]$_.FullName) -ieq '.splcproj'
            })
            if ($projectEntries.Count -lt 1) { throw 'Safety backup contains no .splcproj file.' }
            $archiveEntries = @($archive.Entries | ForEach-Object { [string]$_.FullName })
        } finally {
            if ($null -ne $archive) { $archive.Dispose() }
        }
        $exported = [pscustomobject]@{
            status='exported'; strategy='zip_orphan_project_directory'; project=$name
            project_file=$projectFile; project_directory=$projectDirectory; output_file=$backupFull
            archive_entries=$archiveEntries; bytes=(Get-Item -LiteralPath $backupFull).Length
            readback_verified=$true; applied=$true
        }
    }
    $payload.backup_created = [bool]$exported.readback_verified
    if (-not $payload.backup_created) { throw 'Safety backup was not verified; deletion aborted.' }
    if ($null -ne $item) {
        $removed = Remove-TcSafetyProject $Dte $name $true $name $true
        $payload.removed_from_configuration = [bool]$removed.readback_verified
    } else {
        $matchingItems = @(Get-TcSafetyChildren (Get-TcSafetyRoot $Dte) |
            Where-Object { [string]$_.Name -ieq $name })
        if ($matchingItems.Count -gt 0) {
            throw "Safety project '$name' appeared in TISC during deletion; rerun the command so it can be removed safely. Backup: $backupFull"
        }
        $removed = [pscustomobject]@{
            status='skipped'; reason='already_absent_from_tisc'; name=$name; readback_verified=$true
        }
    }
    try {
        [IO.Directory]::Delete($projectDirectory, $true)
    } catch {
        throw "Safety project was removed from TISC but its directory could not be deleted. Backup: $backupFull. $($_.Exception.Message)"
    }
    if (Test-Path -LiteralPath $projectDirectory) {
        throw "Safety project directory still exists after deletion: $projectDirectory"
    }
    $payload.status='deleted'; $payload.applied=$true; $payload.project_files_deleted=$true
    $payload.readback_verified=$true; $payload.backup=$exported; $payload.remove=$removed
    [pscustomobject]$payload
}

function Close-TcSolution {
    param([Parameter(Mandatory)]$Dte)
    $path = ''
    try { $path = [string]$Dte.Solution.FullName } catch { }
    if ([string]::IsNullOrWhiteSpace($path)) {
        return [pscustomobject]@{ status = 'skipped'; reason = 'no solution open' }
    }
    try { $Dte.ExecuteCommand('File.SaveAll') } catch { }
    $Dte.Solution.Close($true)
    [pscustomobject]@{ status = 'closed'; path = $path }
}

function Open-TcSolution {
    param([Parameter(Mandatory)]$Dte, [Parameter(Mandatory)][string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) { throw 'Solution path is required.' }
    $full = [System.IO.Path]::GetFullPath($Path)
    if (-not (Test-Path -LiteralPath $full -PathType Leaf)) {
        throw "Solution file not found: $full"
    }
    if ([System.IO.Path]::GetExtension($full) -ne '.sln') {
        throw "Only .sln solution files are supported: $full"
    }

    $current = ''
    try { $current = [string]$Dte.Solution.FullName } catch { }
    if (-not [string]::IsNullOrWhiteSpace($current)) {
        try { $Dte.ExecuteCommand('File.SaveAll') } catch { }
        $Dte.Solution.Close($true)
    }
    $Dte.Solution.Open($full)

    $loaded = $false
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Milliseconds 500
        try {
            if ([string]$Dte.Solution.FullName -eq $full -and
                [int]$Dte.Solution.Projects.Count -gt 0) {
                $loaded = $true
                break
            }
        } catch { }
    }
    [pscustomobject]@{
        status  = 'opened'
        path    = $full
        loaded  = $loaded
        warning = $(if ($loaded) { '' } else { 'solution may still be loading' })
    }
}

# ---- PLCopen XML 导入/导出 (ITcPlcIECProject.PlcOpenImport/Export) ----
function Import-PlcOpen {
    param([Parameter(Mandatory)]$Dte, [Parameter(Mandatory)][string]$File, [int]$Options = 0)
    $sys = Get-TcSystemManager $Dte
    $proj = $sys.LookupTreeItem((Get-PlcProjectPath $Dte))
    $proj.PlcOpenImport([string]$File, $Options)
    [pscustomobject]@{ file = $File; status = 'imported' }
}

function Export-PlcOpen {
    param([Parameter(Mandatory)]$Dte, [Parameter(Mandatory)][string]$File,
          [Parameter(Mandatory)][string[]]$Pous)
    $sys = Get-TcSystemManager $Dte
    $proj = $sys.LookupTreeItem((Get-PlcProjectPath $Dte))
    $proj.PlcOpenExport([string]$File, ($Pous -join ';'))
    [pscustomobject]@{ file = $File; pous = @($Pous); status = 'exported' }
}

# ---- 库管理 (References 节点即 ITcPlcLibraryManager, IDispatch 直接调) ----
#  $Action 在本函数作用域内拿到 refs COM 项并返回纯数据, COM 项不外泄。
function Invoke-OnRefs {
    param([Parameter(Mandatory)]$Dte, [Parameter(Mandatory)][scriptblock]$Action)
    $sys = Get-TcSystemManager $Dte
    $refs = $sys.LookupTreeItem((Get-PlcProjectPath $Dte) + '^References')
    & $Action $refs
}

function Get-LibraryList {
    param([Parameter(Mandatory)]$Dte)
    Invoke-OnRefs $Dte {
        param($refs)
        $out = @()
        foreach ($r in $refs) { $out += [pscustomobject]@{ name = [string]$r.Name } }
        ,$out
    }
}

function Get-InstalledLibraries {
    param([Parameter(Mandatory)]$Dte)
    Invoke-OnRefs $Dte {
        param($refs)
        $out = @()
        foreach ($lib in $refs.ScanLibraries()) {
            try {
                $out += [pscustomobject]@{
                    name         = [string]$lib.Name
                    library_name = [string]$lib.LibraryName
                    version      = [string]$lib.Version
                    distributor  = [string]$lib.Distributor
                    display_name = [string]$lib.DisplayName }
            } catch { }
        }
        ,$out
    }
}

function Add-LibraryRef {
    param([Parameter(Mandatory)]$Dte, [Parameter(Mandatory)][string]$Name,
          [string]$Version = '*', [string]$Distributor = '')
    Invoke-OnRefs $Dte { param($refs)
        $refs.AddLibrary($Name, $Version, $Distributor)
        [pscustomobject]@{ library = $Name; version = $Version; status = 'added' }
    }.GetNewClosure()
}

function Remove-LibraryRef {
    param([Parameter(Mandatory)]$Dte, [Parameter(Mandatory)][string]$Name,
          [string]$Version = '', [string]$Distributor = '')
    Invoke-OnRefs $Dte { param($refs)
        $refs.RemoveReference($Name, $Version, $Distributor)
        [pscustomobject]@{ library = $Name; status = 'removed' }
    }.GetNewClosure()
}

function Add-PlaceholderRef {
    param([Parameter(Mandatory)]$Dte, [Parameter(Mandatory)][string]$Name,
          [string]$DefaultLib = '', [string]$DefaultVersion = '*', [string]$DefaultDistributor = '')
    Invoke-OnRefs $Dte { param($refs)
        $refs.AddPlaceholder($Name, $DefaultLib, $DefaultVersion, $DefaultDistributor)
        [pscustomobject]@{ placeholder = $Name; status = 'added' }
    }.GetNewClosure()
}

function Set-PlaceholderFrozen {
    param([Parameter(Mandatory)]$Dte, [Parameter(Mandatory)][string]$Name)
    Invoke-OnRefs $Dte { param($refs)
        $refs.FreezePlaceholder($Name)
        [pscustomobject]@{ placeholder = $Name; status = 'frozen' }
    }.GetNewClosure()
}

function Add-LibRepository {
    param([Parameter(Mandatory)]$Dte, [Parameter(Mandatory)][string]$Name,
          [Parameter(Mandatory)][string]$RootFolder, [int]$Index = 0)
    Invoke-OnRefs $Dte { param($refs)
        $refs.InsertRepository($Name, $RootFolder, $Index)
        [pscustomobject]@{ repository = $Name; path = $RootFolder; status = 'added' }
    }.GetNewClosure()
}

function Remove-LibRepository {
    param([Parameter(Mandatory)]$Dte, [Parameter(Mandatory)][string]$Name)
    Invoke-OnRefs $Dte { param($refs)
        $refs.RemoveRepository($Name)
        [pscustomobject]@{ repository = $Name; status = 'removed' }
    }.GetNewClosure()
}

function Install-Library {
    param([Parameter(Mandatory)]$Dte, [Parameter(Mandatory)][string]$Repository,
          [Parameter(Mandatory)][string]$LibPath, [bool]$Overwrite = $false)
    Invoke-OnRefs $Dte { param($refs)
        $refs.InstallLibrary($Repository, $LibPath, $Overwrite)
        [pscustomobject]@{ repository = $Repository; path = $LibPath; status = 'installed' }
    }.GetNewClosure()
}

function Uninstall-Library {
    param([Parameter(Mandatory)]$Dte, [Parameter(Mandatory)][string]$Repository,
          [Parameter(Mandatory)][string]$Library, [string]$Version = '', [string]$Distributor = '')
    Invoke-OnRefs $Dte { param($refs)
        $refs.UninstallLibrary($Repository, $Library, $Version, $Distributor)
        [pscustomobject]@{ library = $Library; status = 'uninstalled' }
    }.GetNewClosure()
}

# ---- 按名称快速找 POU/FB/成员：只遍历元数据，不读取任何代码正文 ----
function Find-Pou {
    param([Parameter(Mandatory)]$Dte, [Parameter(Mandatory)][string]$Query,
          [string]$Folder = '', [int]$Limit = 30,
          [bool]$IncludeMembers = $true)
    if ([string]::IsNullOrWhiteSpace($Query)) { throw 'Query is required.' }
    if ($Limit -lt 1) { $Limit = 1 }; if ($Limit -gt 200) { $Limit = 200 }
    $folders = if ($Folder) { @($Folder) } else { @('POUs','DUTs','GVLs','Interfaces','VISUs') }
    $memberTypes = @(608,609,610,611,612,613,614,616,654,655)
    $kindMap = @{ 602='program'; 603='function'; 604='function_block'
                  605='enum'; 606='struct'; 607='union'; 623='alias'; 615='gvl'
                  618='interface'; 619='visualization' }

    $found = [Collections.Generic.List[object]]::new()
    $comparison = [StringComparison]::OrdinalIgnoreCase
    foreach ($frame in @(Get-PlcObjectFrames $Dte -Folders $folders)) {
        $o = $frame.node; $name = [string]$frame.name
        $folderName = [string]$frame.folder; $itemType = [int]$frame.itemType
        $itemPath = [string]$frame.path
        $index = $name.IndexOf($Query, $comparison)
        if ($index -ge 0) {
            $score = if ($name.Equals($Query, $comparison)) { 0 } elseif ($index -eq 0) { 1 } else { 2 }
            $members = @(); $memberCount = 0
            if ($IncludeMembers) { foreach ($child in $o) {
                $childType = 0; try { $childType = [int]$child.ItemType } catch { }
                if ($childType -notin $memberTypes) { continue }
                $cn = [string]$child.Name; if (-not $cn) { continue }
                $memberCount++; if ($members.Count -lt 100) { $members += $cn }
            } }
            $match = [pscustomobject]@{
                score = $score; name = $name; folder = $folderName
                kind = $kindMap[$itemType]; itemType = $itemType
                path = $itemPath; parent = [string]$frame.parent; member = $null
                member_count = $memberCount; members = @($members)
                members_truncated = ($memberCount -gt $members.Count) }
            [void]$found.Add($match)
        }
        if ($IncludeMembers) { foreach ($child in $o) {
            $childType = 0; try { $childType = [int]$child.ItemType } catch { }
            if ($childType -notin $memberTypes) { continue }
            $member = [string]$child.Name; if (-not $member) { continue }
            $qualified = "$name.$member"
            $memberIndex = $qualified.IndexOf($Query, $comparison)
            if ($memberIndex -ge 0) {
                $score = if ($qualified.Equals($Query, $comparison)) { 0 } elseif ($memberIndex -eq 0) { 1 } else { 3 }
                [void]$found.Add([pscustomobject]@{
                    score = $score; name = $name; folder = $folderName
                    kind = 'member'; itemType = $itemType; path = $itemPath
                    parent = [string]$frame.parent; member = $member
                    member_itemType = $childType; member_path = "$itemPath^$member" })
            }
        }
        }
    }
    $exactMatches = @($found | Where-Object { $_.score -eq 0 -and -not $_.member })
    $orderedSource = if ($exactMatches.Count -eq 1) { $exactMatches } else { $found }
    $ordered = @($orderedSource | Sort-Object score,name,member | Select-Object -First $Limit |
        Select-Object name,folder,kind,itemType,path,parent,member,member_itemType,member_path,
                      member_count,members,members_truncated)
    [pscustomobject]@{ query = $Query; exact = ($exactMatches.Count -eq 1); total = $found.Count; matches = $ordered }
}

# ---- 在 COM 进程内搜索代码，只返回命中行，避免全项目源码跨进程传输 ----
function Search-PlcCode {
    param([Parameter(Mandatory)]$Dte, [Parameter(Mandatory)][string]$Pattern,
          [bool]$Regex = $false, [bool]$CaseSensitive = $false,
          [string]$Pou = '', [int]$MaxResults = 50,
          [string]$TreePath = '')
    if ([string]::IsNullOrWhiteSpace($Pattern)) { throw 'Pattern is required.' }
    if ($MaxResults -lt 1) { $MaxResults = 1 }; if ($MaxResults -gt 500) { $MaxResults = 500 }
    $options = if ($CaseSensitive) { [Text.RegularExpressions.RegexOptions]::None } else {
        [Text.RegularExpressions.RegexOptions]::IgnoreCase }
    $rx = if ($Regex) { [regex]::new($Pattern, $options) } else { $null }
    $comparison = if ($CaseSensitive) { [StringComparison]::Ordinal } else {
        [StringComparison]::OrdinalIgnoreCase }
    $results = [Collections.Generic.List[object]]::new()
    $scan = {
        param($o, [string]$objectPath = '')
        $objectName = [string]$o.Name
        $areas = @(
            @('declaration', $(try { [string]$o.DeclarationText } catch { '' })),
            @('implementation', $(try { [string]$o.ImplementationText } catch { '' }))
        )
        foreach ($pair in $areas) {
            $lineNo = 0
            foreach ($line in ([string]$pair[1] -split "`r?`n")) {
                $lineNo++
                $hit = if ($Regex) { $rx.IsMatch($line) } else { $line.IndexOf($Pattern, $comparison) -ge 0 }
                if ($hit) {
                    $text = $line.Trim(); if ($text.Length -gt 500) { $text = $text.Substring(0,500) + '…' }
                    [void]$results.Add([pscustomobject]@{
                        pou = $objectName; path = $objectPath
                        area = $pair[0]; line = $lineNo; text = $text })
                    if ($results.Count -ge $MaxResults) { return }
                }
            }
            if ($results.Count -ge $MaxResults) { return }
        }
        foreach ($child in $o) {
            if ($results.Count -ge $MaxResults) { return }
            $memberName = [string]$child.Name; if (-not $memberName) { continue }
            $memberAreas = @(
                @('declaration', $(try { [string]$child.DeclarationText } catch { '' })),
                @('implementation', $(try { [string]$child.ImplementationText } catch { '' }))
            )
            foreach ($pair in $memberAreas) {
                $lineNo = 0
                foreach ($line in ([string]$pair[1] -split "`r?`n")) {
                    $lineNo++
                    $hit = if ($Regex) { $rx.IsMatch($line) } else { $line.IndexOf($Pattern, $comparison) -ge 0 }
                    if ($hit) {
                        $text = $line.Trim(); if ($text.Length -gt 500) { $text = $text.Substring(0,500) + '…' }
                        [void]$results.Add([pscustomobject]@{
                            pou = "$objectName.$memberName"; path = $objectPath
                            area = $pair[0]
                            line = $lineNo; text = $text })
                        if ($results.Count -ge $MaxResults) { return }
                    }
                }
            }
        }
    }.GetNewClosure()

    if ($Pou) {
        $targetScan = { param($o) & $scan $o $TreePath }.GetNewClosure()
        Invoke-OnPou $Dte $Pou $targetScan -TreePath $TreePath | Out-Null
    } else {
        foreach ($frame in @(Get-PlcObjectFrames $Dte -Folders @('POUs','DUTs','GVLs','Interfaces'))) {
            & $scan $frame.node ([string]$frame.path)
            if ($results.Count -ge $MaxResults) { break }
        }
    }
    [pscustomobject]@{
        pattern = $Pattern; pou = $Pou; path = $TreePath; count = $results.Count
        truncated = ($results.Count -ge $MaxResults); max_results = $MaxResults
        matches = @($results)
    }
}

# ---- 项目结构树：Agent 可限制每个文件夹数量且默认不展开成员 ----
function Get-Structure {
    param([Parameter(Mandatory)]$Dte,
          [string[]]$Folders = @('POUs','DUTs','GVLs','Interfaces','VISUs'),
          [int]$LimitPerFolder = 0, [bool]$IncludeMembers = $true)
    $projectName = ''; $result = @{}; $counts = @{}; $truncated = $false
    $sys = Get-TcSystemManager $Dte
    $base = Get-PlcProjectPath $Dte
    $memberTypes = @(608,609,610,611,612,613,614,616,654,655)
    $kindMap = @{ 602='program'; 603='function'; 604='function_block'
                  605='enum'; 606='struct'; 607='union'; 623='alias'; 615='gvl'
                  618='interface'; 619='visualization' }
    try { $projectName = [string]$sys.LookupTreeItem($base).Name } catch { }
    $frames = @(Get-PlcObjectFrames $Dte -Folders $Folders)
    foreach ($folderName in $Folders) {
        $entries = [Collections.Generic.List[object]]::new()
        $folderCount = 0
        foreach ($frame in $frames) {
            if ([string]$frame.folder -ne $folderName) { continue }
            $folderCount++
            if ($LimitPerFolder -gt 0 -and $entries.Count -ge $LimitPerFolder) {
                $truncated = $true
                continue
            }
            $methods = @()
            if ($IncludeMembers) {
                foreach ($child in $frame.node) {
                    $childType = 0; try { $childType = [int]$child.ItemType } catch { }
                    if ($childType -notin $memberTypes) { continue }
                    $cn = [string]$child.Name; if ($cn) { $methods += $cn }
                }
            }
            [void]$entries.Add([pscustomobject]@{
                name = [string]$frame.name; kind = $kindMap[[int]$frame.itemType]
                itemType = [int]$frame.itemType; path = [string]$frame.path
                parent = [string]$frame.parent; depth = [int]$frame.depth
                methods = @($methods) })
        }
        $counts[$folderName] = $folderCount
        if ($entries.Count -gt 0) { $result[$folderName] = @($entries.ToArray()) }
    }
    [pscustomobject]@{ project = $projectName; counts = $counts; folders = $result
                       truncated = $truncated; limit_per_folder = $LimitPerFolder
                       members_included = $IncludeMembers }
}

# ---- PLC 实时树：包含空文件夹，供后续精确创建/读取使用 ----
function Get-PlcTree {
    param([Parameter(Mandatory)]$Dte, [int]$MaxNodes = 2500)
    $sys = Get-TcSystemManager $Dte
    $base = Get-PlcProjectPath $Dte
    $nodes = [Collections.Generic.List[object]]::new()
    $add = {
        param([string]$path, [string]$name, [string]$parent, [int]$itemType)
        if (-not $name -or $nodes.Count -ge $MaxNodes) { return }
        [void]$nodes.Add([pscustomobject]@{
            path=$path; name=$name; parent=$parent; itemType=$itemType })
    }.GetNewClosure()
    $walk = $null
    $walk = {
        param($item, [string]$path, [int]$depth)
        if ($depth -ge 12 -or $nodes.Count -ge $MaxNodes) { return }
        foreach ($child in $item) {
            $childName = [string]$child.Name; if (-not $childName) { continue }
            $childPath = "$path^$childName"
            $childType = 0; try { $childType = [int]$child.ItemType } catch { }
            & $add $childPath $childName $path $childType
            & $walk $child $childPath ($depth + 1)
            if ($nodes.Count -ge $MaxNodes) { return }
        }
    }.GetNewClosure()
    & $add $base ([string]($base -split '\^')[-1]) ([string]($base -replace '\^[^\^]+$','')) 0
    try { & $walk ($sys.LookupTreeItem($base)) $base 0 } catch { }
    [pscustomobject]@{ root=$base; nodes=@($nodes.ToArray()); truncated=($nodes.Count -ge $MaxNodes) }
}

# ---- 一次性 dump 所有对象的 decl+impl+methods (供 Python 做 vars/search 解析) ----
#  单次 COM 遍历, 避免 Python 侧对每个 POU 各起一次 powershell 子进程。
function Get-AllCode {
    param([Parameter(Mandatory)]$Dte,
          [string[]]$Folders = @('POUs','DUTs','GVLs','Interfaces'))
    $out = @()
    foreach ($frame in @(Get-PlcObjectFrames $Dte -Folders $Folders)) {
        $o = $frame.node; $folderName = [string]$frame.folder
        $nm = [string]$frame.name; $decl = ''; $impl = ''
        try { $decl = [string]$o.DeclarationText } catch { }
        if ($folderName -eq 'POUs') { try { $impl = [string]$o.ImplementationText } catch { } }
        $methods = @()
        foreach ($child in $o) {
            $cn = [string]$child.Name; if (-not $cn) { continue }
            $md = ''; $mi = ''
            try { $md = [string]$child.DeclarationText } catch { }
            try { $mi = [string]$child.ImplementationText } catch { }
            if ($md -or $mi) {
                $methods += [pscustomobject]@{
                    name = $cn; declaration = $md; implementation = $mi }
            }
        }
        $out += [pscustomobject]@{
            name = $nm; folder = $folderName; path = [string]$frame.path
            declaration = $decl; implementation = $impl; methods = @($methods) }
    }
    ,$out
}

# ---- 版本快照清单：在 COM 进程内规范化并计算哈希，默认不传输代码正文 ----
function Get-NormalizedCode {
    param([AllowEmptyString()][string]$Text = '')
    if ([string]::IsNullOrEmpty($Text)) { return '' }
    [string]((@($Text -split "`r?`n") | ForEach-Object { $_.TrimEnd() }) -join "`n")
}

function Get-CodeHash {
    param([AllowEmptyString()][string]$Text = '')
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes((Get-NormalizedCode $Text))
        ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
    } finally { $sha.Dispose() }
}

function Get-CodeInventory {
    param([Parameter(Mandatory)]$Dte, [bool]$IncludeCode = $false,
          [string[]]$Paths = @(),
          [string[]]$Folders = @('POUs','DUTs','GVLs','Interfaces'))
    $sys = Get-TcSystemManager $Dte
    $base = Get-PlcProjectPath $Dte
    $memberTypes = @(608,609,610,611,612,613,614,616,654,655)
    $kindMap = @{ 602='program'; 603='function'; 604='function_block'
                  605='enum'; 606='struct'; 607='union'; 623='alias'; 615='gvl'; 618='interface' }
    $objects = [Collections.Generic.List[object]]::new()

    $areaData = {
        param([string]$Text)
        $normalized = Get-NormalizedCode $Text
        [pscustomobject]@{
            hash = Get-CodeHash $normalized
            lines = $(if ($normalized) { @($normalized -split "`n").Count } else { 0 })
            chars = $normalized.Length
            text = $(if ($IncludeCode) { $normalized } else { $null })
        }
    }.GetNewClosure()

    $buildEntry = {
        param($o, [string]$itemPath, [string]$folderName)
        $itemType = 0; try { $itemType = [int]$o.ItemType } catch { }
        $decl = ''; $impl = ''
        try { $decl = [string]$o.DeclarationText } catch { }
        try { $impl = [string]$o.ImplementationText } catch { }
        $members = [Collections.Generic.List[object]]::new()
        $memberStack = [Collections.Stack]::new()
        foreach ($child in $o) {
            $childType = 0; try { $childType = [int]$child.ItemType } catch { }
            if ($childType -in $memberTypes) {
                $memberStack.Push([pscustomobject]@{
                    node=$child; path="$itemPath^$([string]$child.Name)" })
            }
        }
        while ($memberStack.Count -gt 0) {
            $frame = $memberStack.Pop(); $member = $frame.node
            $memberType = 0; try { $memberType = [int]$member.ItemType } catch { }
            $memberDecl = ''; $memberImpl = ''
            try { $memberDecl = [string]$member.DeclarationText } catch { }
            try { $memberImpl = [string]$member.ImplementationText } catch { }
            [void]$members.Add([pscustomobject]@{
                name = [string]$member.Name; path = [string]$frame.path
                itemType = $memberType
                declaration = (& $areaData $memberDecl)
                implementation = (& $areaData $memberImpl)
            })
            foreach ($child in $member) {
                $childType = 0; try { $childType = [int]$child.ItemType } catch { }
                if ($childType -in $memberTypes) {
                    $memberStack.Push([pscustomobject]@{
                        node=$child; path="$($frame.path)^$([string]$child.Name)" })
                }
            }
        }
        [pscustomobject]@{
            name = [string]$o.Name; path = $itemPath; folder = $folderName
            itemType = $itemType; kind = $kindMap[$itemType]
            declaration = (& $areaData $decl)
            implementation = (& $areaData $impl)
            members = @($members.ToArray())
        }
    }.GetNewClosure()

    if ($Paths.Count -gt 0) {
        foreach ($itemPath in $Paths) {
            if (-not $itemPath.StartsWith("$base^", [StringComparison]::OrdinalIgnoreCase)) {
                throw "Tree path is outside the current PLC project: '$itemPath'"
            }
            $o = $sys.LookupTreeItem($itemPath)
            if ($null -eq $o -or [int]$o.ItemType -eq 601) { continue }
            $folderName = Get-PlcObjectCategory ([int]$o.ItemType)
            if (-not $folderName -or $folderName -notin $Folders) { continue }
            [void]$objects.Add((& $buildEntry $o $itemPath $folderName))
        }
    } else {
        foreach ($frame in @(Get-PlcObjectFrames $Dte -Folders $Folders)) {
            [void]$objects.Add((& $buildEntry $frame.node ([string]$frame.path) ([string]$frame.folder)))
        }
    }
    [pscustomobject]@{
        solution = [string]$Dte.Solution.FullName; project_path = $base
        generated_at = [DateTime]::UtcNow.ToString('o'); include_code = $IncludeCode
        object_count = $objects.Count; objects = @($objects.ToArray())
    }
}

# =====================================================================
#  运行时控制 (activate / restart / state / login / start / mode)
#  sysman = $Dte.Solution.Projects.Item(1).Object
# =====================================================================

# ---- 激活配置 (只写注册表, 必须紧跟 restart 才加载 Runtime) ----
function Invoke-Activate {
    param([Parameter(Mandatory)]$Dte)
    $sys = Get-TcSystemManager $Dte
    $sys.ActivateConfiguration()
    [pscustomobject]@{ status = 'activated'; message = 'Configuration activated. Restart to apply.' }
}

# ---- 启动/重启 TwinCAT Runtime, 轮询 IsTwinCATStarted ----
function Invoke-Restart {
    param([Parameter(Mandatory)]$Dte, [int]$TimeoutSec = 15)
    $sys = Get-TcSystemManager $Dte
    $sys.StartRestartTwinCAT()
    $deadline = (Get-Date).AddSeconds($TimeoutSec); $started = $false
    while ((Get-Date) -lt $deadline) {
        try { if ($sys.IsTwinCATStarted()) { $started = $true; break } } catch { }
        Start-Sleep -Milliseconds 1000
    }
    [pscustomobject]@{ status = 'restarted'; started = $started }
}

# ---- 读运行时状态: IsTwinCATStarted + TIRS ProduceXml 解析 ----
function Get-TcState {
    param([Parameter(Mandatory)]$Dte)
    $sys = Get-TcSystemManager $Dte
    $started = $false
    try { $started = [bool]$sys.IsTwinCATStarted() } catch { }
    if (-not $started) { return [pscustomobject]@{ state = 'NotStarted'; started = $false } }

    $tcState = ''; $runMode = ''; $state = 'Started'
    try {
        $tirs = $sys.LookupTreeItem('TIRS')
        $xml = [xml]$tirs.ProduceXml($false)
        $n = $xml.SelectSingleNode('//TwinCATState'); if ($n) { $tcState = [string]$n.InnerText }
        $n = $xml.SelectSingleNode('//RunMode');      if ($n) { $runMode = [string]$n.InnerText }
        $cur = ''
        $n = $xml.SelectSingleNode('//CurrentState'); if ($n) { $cur = [string]$n.InnerText }
        $probe = ("$tcState $runMode $cur").ToLower()
        if ($probe -match 'config') { $state = 'Config' }
        elseif ($probe -match 'run') { $state = 'Run' }
    } catch { }
    [pscustomobject]@{ state = $state; started = $true; twincatState = $tcState; runMode = $runMode }
}

# ---- 在线命令: 发到 NestedProject 节点 (Instance 节点不支持这些 ConsumeXml) ----
function Send-OnlineCommand {
    param([Parameter(Mandatory)]$Dte, [Parameter(Mandatory)][hashtable]$Cmds,
          [string]$Runtime = '', [bool]$AllPlcs = $false)
    $sys = Get-TcSystemManager $Dte
    $plc = $sys.LookupTreeItem('TIPC')
    $parts = ''
    foreach ($k in $Cmds.Keys) { $parts += "<$k>$([string]$Cmds[$k])</$k>" }
    $xml = "<TreeItem><IECProjectDef><OnlineSettings><Commands>$parts</Commands></OnlineSettings></IECProjectDef></TreeItem>"
    $roots = @($plc)
    if ($Runtime -and $AllPlcs) { throw 'runtime and all_plcs are mutually exclusive' }
    if ($Runtime) {
        $roots = @($roots | Where-Object { $_.Name -ieq $Runtime })
        if ($roots.Count -ne 1) { throw "PLC runtime must match exactly one configured name: $Runtime" }
    } elseif ($roots.Count -gt 1 -and -not $AllPlcs) {
        throw 'Multiple PLC runtimes: specify runtime or all_plcs=true'
    }
    $results = @()
    foreach ($c in $roots) {
        $nested = $null; try { $nested = $c.NestedProject } catch { }
        if ($null -eq $nested) {
            $results += [pscustomobject]@{name=[string]$c.Name;status='failed';error='No NestedProject'}
            continue
        }
        try {
            $nested.ConsumeXml($xml)
            $results += [pscustomobject]@{name=[string]$c.Name;status='accepted'}
        } catch {
            $results += [pscustomobject]@{name=[string]$c.Name;status='failed';error=$_.Exception.Message}
        }
    }
    $failed = @($results | Where-Object status -eq 'failed')
    [pscustomobject]@{ status=$(if(-not $results.Count){'skipped'}elseif($failed.Count){'failed'}else{'accepted'});
        plcs=$results;commands=@($Cmds.Keys);verified=$null }
}

function Invoke-Login  { param([Parameter(Mandatory)]$Dte) Send-OnlineCommand $Dte @{ LoginCmd = 'true' } }
function Invoke-Logout { param([Parameter(Mandatory)]$Dte) Send-OnlineCommand $Dte @{ LogoutCmd = 'true' } }
function Start-Plc     { param([Parameter(Mandatory)]$Dte) Send-OnlineCommand $Dte @{ StartCmd = 'true' } }
function Stop-Plc      { param([Parameter(Mandatory)]$Dte) Send-OnlineCommand $Dte @{ StopCmd = 'true' } }

# ---- online: logout -> login -> start (强制干净登录态) ----
function Invoke-Online {
    param([Parameter(Mandatory)]$Dte)
    $r1 = Send-OnlineCommand $Dte @{ LoginCmd = 'true' }
    [pscustomobject]@{ status='incomplete'; login=$r1; verified=$false; reason='login_state_unverified' }
}

# ---- Config/Run 模式切换: TIRS ConsumeXml <RTStateDef><CurrentState> ----
function Invoke-TcConfigRestartCommand {
    param([Parameter(Mandatory)]$Dte)

    # XAE versions do not all register the toolbar action under the same
    # canonical DTE name.  Start with the historical name, then discover
    # commands registered by this specific XAE instance.
    $candidates = New-Object System.Collections.Generic.List[string]
    [void]$candidates.Add('TwinCAT.RestartTwinCATConfigMode')
    try {
        foreach ($command in $Dte.Commands) {
            try { $name = [string]$command.Name } catch { continue }
            $folded = $name.ToLowerInvariant()
            if ($folded.Contains('twincat') -and $folded.Contains('config') -and
                ($folded.Contains('restart') -or $folded.Contains('start'))) {
                [void]$candidates.Add($name)
            }
        }
    } catch {
        # Keep the historical candidate for older XAE versions where the DTE
        # command collection cannot be enumerated.
    }

    $errors = New-Object System.Collections.Generic.List[string]
    foreach ($name in ($candidates | Select-Object -Unique)) {
        try {
            $Dte.ExecuteCommand($name)
            return $name
        } catch {
            [void]$errors.Add("${name}: $($_.Exception.Message)")
        }
    }
    $detail = if ($errors.Count) { $errors -join '; ' } else { 'no matching DTE command was found' }
    throw "No usable XAE command to restart TwinCAT in Config mode. $detail"
}

function Set-TcRtState {
    param(
        [Parameter(Mandatory)]$Dte,
        [ValidateSet('Config','Run')][string]$Target,
        [ValidateSet('consume','command')][string]$Strategy = 'consume'
    )
    $sys = Get-TcSystemManager $Dte
    try {
        if (-not $sys.IsTwinCATStarted()) {
            throw 'Target has no running TwinCAT runtime.'
        }
    } catch {
        if ($_.Exception.Message -eq 'Target has no running TwinCAT runtime.') { throw }
    }
    if ($Strategy -eq 'command') {
        if ($Target -ne 'Config') {
            throw "The command strategy is only supported for Config mode."
        }
        $commandName = Invoke-TcConfigRestartCommand $Dte
        return [pscustomobject]@{
            requested = $Target
            strategy = $commandName
            target_netid = [string]$sys.GetTargetNetId()
        }
    }
    $xml = "<TreeItem><RTStateDef><CurrentState>$Target</CurrentState></RTStateDef></TreeItem>"
    $rs = $sys.LookupTreeItem('TIRS')
    [void]$rs.ConsumeXml($xml)
    [pscustomobject]@{
        requested = $Target
        strategy = 'TIRS ConsumeXml'
        target_netid = [string]$sys.GetTargetNetId()
    }
}

# ---- 输出纯 ASCII JSON: 把所有 >127 字符转义成 \uXXXX ----
#  PS 5.1 直接 ConvertTo-Json 会用控制台代码页(中文 Windows = GBK/936)输出非 ASCII
#  字节, Python 桥按 UTF-8 解码就会得到 �。转义成 \uXXXX 后字节流恒为 ASCII,
#  与代码页无关, Python json.loads 再还原出正确的中文。
function ConvertTo-AsciiJson {
    param([Parameter(Mandatory)]$Obj)
    $json = $Obj | ConvertTo-Json -Depth 8 -Compress
    $sb = New-Object System.Text.StringBuilder
    foreach ($ch in $json.ToCharArray()) {
        $code = [int][char]$ch
        if ($code -gt 127) { [void]$sb.Append(('\u{0:x4}' -f $code)) }
        else { [void]$sb.Append($ch) }
    }
    $sb.ToString()
}

# =====================================================================
#  JSON dispatcher — 仅当以 -Command 运行脚本时执行 (dot-source 时 $Command='')
# =====================================================================
if ($Command) {
    $ErrorActionPreference = 'Stop'
    try {
        if ($ArgsFile) {
            if (-not (Test-Path -LiteralPath $ArgsFile -PathType Leaf)) {
                throw "ArgsFile not found: $ArgsFile"
            }
            $ArgsJson = [IO.File]::ReadAllText(
                [IO.Path]::GetFullPath($ArgsFile), [Text.Encoding]::UTF8)
        }
        $a = if ($ArgsJson) { $ArgsJson | ConvertFrom-Json } else { $null }
        $script:HmiWriteGate = if ($null -ne $a) { $a.hmi_write_gate } else { $null }
        $preferPid = 0
        if ($a -and $a.PSObject.Properties.Name -contains 'preferPid') { $preferPid = [int]$a.preferPid }
        $sticky = $true
        if ($a -and $a.PSObject.Properties.Name -contains 'sticky') { $sticky = [bool]$a.sticky }
        $strictPid = $false
        if ($a -and $a.PSObject.Properties.Name -contains 'strictPid') { $strictPid = [bool]$a.strictPid }
        $dte = Connect-Tc -PreferPid $preferPid -Sticky $sticky -StrictPid $strictPid
        # SilentMode is an XAE-global setting.  Limit it to operations that can
        # raise runtime/configuration confirmations and always restore the
        # user's previous value.  Read/code tools must not alter manual XAE UI.
        $silentSettings = $null
        $previousSilentMode = $false
        $restoreSilentMode = $false
        if ($Command -in @(
                'io-scan', 'activate', 'restart', 'login', 'logout', 'start',
                'stop', 'online', 'config-mode', 'run-mode')) {
            try {
                $silentSettings = $dte.GetObject('TcAutomationSettings')
                $previousSilentMode = [bool]$silentSettings.SilentMode
                $silentSettings.SilentMode = $true
                $restoreSilentMode = $true
            } catch { }
        }
        try {
            switch ($Command) {
            'connect-check' {
                $activeDocument = [pscustomobject]@{
                    name = ''; full_name = ''; kind = ''; saved = $null }
                try {
                    $document = $dte.ActiveDocument
                    if ($null -ne $document) {
                        $activeDocument = [pscustomobject]@{
                            name = [string]$document.Name
                            full_name = [string]$document.FullName
                            kind = [string]$document.Kind
                            saved = [bool]$document.Saved }
                    }
                } catch { }
                $data = [pscustomobject]@{
                    solution = [string]$dte.Solution.FullName
                    name     = [string]$dte.Name
                    pid      = [int]$script:TcChosenPid
                    active_document = $activeDocument }
            }
            'platform-list' { $data = Get-TcSolutionBuildPlatforms $dte }
            'platform-target-info' {
                $sys = Get-TcSystemManager $dte
                $cpuXml = ''; $probeError = ''
                try {
                    $settings = [xml]$sys.LookupTreeItem('TIRS').ProduceXml($false)
                    $cpu = $settings.SelectSingleNode('//TargetCPUInfo')
                    if ($cpu) { $cpuXml = $cpu.OuterXml }
                } catch { $probeError = $_.Exception.Message }
                $data = [pscustomobject]@{
                    solution=[string]$dte.Solution.FullName;target_netid=[string]$sys.GetTargetNetId()
                    cpu_info_xml=$cpuXml;source='TIRS.TargetCPUInfo';error=$probeError
                    os=$null;architecture=$null;target_platform=$null;target_match_verified=$false
                    note='CPUType alone does not identify RT/OS or target bitness; explicit confirmation required.'
                }
                try {
                    $serviceDll = Join-Path ${env:ProgramFiles(x86)} 'Beckhoff\TwinCAT\Functions\Target-Browser\Extensions\ADS\TwinCAT.SystemService.dll'
                    if (-not (Test-Path -LiteralPath $serviceDll)) { throw 'Beckhoff Target Browser SystemService library not installed' }
                    [Reflection.Assembly]::LoadFrom($serviceDll) | Out-Null
                    $svc = New-Object TwinCAT.SystemService.SystemServiceClass
                    $context = New-Object TwinCAT.SystemService.Commands.SystemServiceContext($svc)
                    $net = [TwinCAT.Ads.AmsNetId]::Parse($data.target_netid)
                    $query = New-Object TwinCAT.SystemService.Commands.GetDetailedDeviceInfoCommand($net,$context)
                    $query.Timeout = [TimeSpan]::FromSeconds(5)
                    $query.Execute($null) | Out-Null
                    if (-not $query.IsSucceeded) { throw "Target detail query failed: $($query.Exception)" }
                    $detail = $query.Result
                    if ([string]$detail.Target.NetId -ne $data.target_netid) { throw 'Target detail NetId mismatch' }
                    $data.target_platform = [string]$detail.Platform
                    $data.target_match_verified = -not [string]::IsNullOrWhiteSpace($data.target_platform)
                    $data.os = [string]$detail.Target.RTSystem.ShortOSName
                    $data.architecture = [string]$detail.TargetHardware.CPUArchitecture
                    $data.source = 'Beckhoff.SystemService.GetDetailedDeviceInfoCommand'
                    $data.error = ''
                    $data.note = 'Exact target response over existing route; no route/configuration writes.'
                } catch {
                    $data.error = [string]$_.Exception.Message
                    $data.note = "Target platform unavailable: $($_.Exception.Message)"
                }
            }
            'platform-show' {
                $platforms = Get-TcSolutionBuildPlatforms $dte
                $selected = @($platforms.platforms | Where-Object {
                    [string]$_.full -ieq [string]$platforms.active
                } | Select-Object -First 1)
                if ($selected.Count -eq 0) {
                    throw "Active build platform '$($platforms.active)' is not listed in the solution."
                }
                $data = [pscustomobject]@{
                    status = 'ok'; config = [string]$selected[0].config
                    platform = [string]$selected[0].platform; full = [string]$selected[0].full
                    index = [int]$selected[0].index; contexts = @(Get-TcActiveBuildContexts $dte)
                }
            }
            'platform-set' { $data = Set-TcSolutionBuildPlatform $dte ([string]$a.full) }
            'list'      { $data = Get-PouList $dte }
            'read-pou'  {
                $area = if ($a.area) { [string]$a.area } else { 'all' }
                $method = if ($a.method) { [string]$a.method } else { '' }
                $memberType = if ($a.member_type) { [string]$a.member_type } else { '' }
                $includeMemberCode = $true
                if ($a.PSObject.Properties.Name -contains 'include_member_code') {
                    $includeMemberCode = [bool]$a.include_member_code
                }
                $startLine = if ($a.start_line) { [int]$a.start_line } else { 1 }
                $maxLines = if ($a.max_lines) { [int]$a.max_lines } else { 0 }
                $treePath = if ($a.path) { [string]$a.path } else { '' }
                if ($treePath) {
                    $pathParts = @($treePath -split '\^')
                    $objectIndex = -1
                    for ($i = 0; $i -lt $pathParts.Count; $i++) {
                        if ([string]$pathParts[$i] -ieq [string]$a.name) { $objectIndex = $i }
                    }
                    if ($objectIndex -ge 0 -and $objectIndex -lt ($pathParts.Count - 1)) {
                        if (-not $method) {
                            $method = ($pathParts[($objectIndex + 1)..($pathParts.Count - 1)] -join '.')
                        }
                        $treePath = ($pathParts[0..$objectIndex] -join '^')
                    }
                }
                $data = Read-Pou $dte $a.name $area $method $memberType $includeMemberCode $startLine $maxLines $treePath
            }
            'read-batch' {
                $requests = @($a.requests)
                if ($requests.Count -lt 1 -or $requests.Count -gt 16) {
                    throw 'requests must contain between 1 and 16 items'
                }
                $batchResults = @()
                $batchIndex = 0
                foreach ($request in $requests) {
                    if (-not [string]$request.name) { throw "requests[$batchIndex].name is required" }
                    $area = if ($request.area) { [string]$request.area } else { 'all' }
                    $method = if ($request.method) { [string]$request.method } else { '' }
                    $memberType = if ($request.member_type) { [string]$request.member_type } else { '' }
                    $startLine = if ($request.start_line) { [int]$request.start_line } else { 1 }
                    $maxLines = if ($request.max_lines) { [int]$request.max_lines } else { 120 }
                    $treePath = if ($request.path) { [string]$request.path } else { '' }
                    if ($treePath) {
                        $parts = @($treePath -split '\^'); $objectIndex = -1
                        for ($i = 0; $i -lt $parts.Count; $i++) {
                            if ([string]$parts[$i] -ieq [string]$request.name) { $objectIndex = $i }
                        }
                        if ($objectIndex -ge 0 -and $objectIndex -lt ($parts.Count - 1)) {
                            if (-not $method) {
                                $method = ($parts[($objectIndex + 1)..($parts.Count - 1)] -join '.')
                            }
                            $treePath = ($parts[0..$objectIndex] -join '^')
                        }
                    }
                    $item = Read-Pou $dte $request.name $area $method $memberType $false $startLine $maxLines $treePath
                    $item | Add-Member -NotePropertyName request_index -NotePropertyValue $batchIndex
                    $requestId = if ($request.id) { [string]$request.id } else { [string]$batchIndex }
                    $item | Add-Member -NotePropertyName request_id -NotePropertyValue $requestId
                    $batchResults += $item
                    $batchIndex++
                }
                $data = [pscustomobject]@{
                    status='read'; count=$batchResults.Count; results=@($batchResults)
                    live_xae=$true; transport='powershell-fallback'
                }
            }
            'write-pou' {
                $mth = if ($a.method) { [string]$a.method } else { '' }
                $treePath = if ($a.path) { [string]$a.path } else { '' }
                if ($treePath) {
                    $pathParts = @($treePath -split '\^'); $objectIndex = -1
                    for ($i = 0; $i -lt $pathParts.Count; $i++) {
                        if ([string]$pathParts[$i] -ieq [string]$a.name) { $objectIndex = $i }
                    }
                    if ($objectIndex -ge 0 -and $objectIndex -lt ($pathParts.Count - 1)) {
                        if (-not $mth) { $mth = ($pathParts[($objectIndex + 1)..($pathParts.Count - 1)] -join '.') }
                        $treePath = ($pathParts[0..$objectIndex] -join '^')
                    }
                }
                $data = Write-Pou $dte $a.name $a.area $a.code $mth $treePath
            }
            'patch-pou' {
                $mth = if ($a.method) { [string]$a.method } else { '' }
                $newText = if ($null -ne $a.new_text) { [string]$a.new_text } else { '' }
                $treePath = if ($a.path) { [string]$a.path } else { '' }
                if ($treePath) {
                    $pathParts = @($treePath -split '\^'); $objectIndex = -1
                    for ($i = 0; $i -lt $pathParts.Count; $i++) {
                        if ([string]$pathParts[$i] -ieq [string]$a.name) { $objectIndex = $i }
                    }
                    if ($objectIndex -ge 0 -and $objectIndex -lt ($pathParts.Count - 1)) {
                        if (-not $mth) { $mth = ($pathParts[($objectIndex + 1)..($pathParts.Count - 1)] -join '.') }
                        $treePath = ($pathParts[0..$objectIndex] -join '^')
                    }
                }
                $data = Patch-Pou $dte $a.name $a.area ([string]$a.old_text) $newText $mth $treePath
            }
            'new-pou'   {
                $type = if ($a.type) { [string]$a.type } else { 'fb' }
                $decl = if ($a.declaration)    { [string]$a.declaration }    else { '' }
                $impl = if ($a.implementation) { [string]$a.implementation } else { '' }
                $lang = if ($a.language)       { [string]$a.language }       else { 'ST' }
                $ret = if ($a.return_type)     { [string]$a.return_type }    else { '' }
                $parentPath = if ($a.path) { [string]$a.path } elseif ($a.parent_path) {
                    [string]$a.parent_path } else { '' }
                $data = New-Pou $dte $a.name $type $decl $impl $lang $ret $parentPath
            }
            'new-folder' {
                $parentPath = if ($a.parent_path) { [string]$a.parent_path } else { '' }
                $data = New-PlcFolder $dte $a.name $parentPath
            }
            'new-member' {
                $mtype = if ($a.type) { [string]$a.type } else { 'method' }
                $ret   = if ($a.return_type)   { [string]$a.return_type }   else { 'BOOL' }
                $lang  = if ($a.language)      { [string]$a.language }      else { 'ST' }
                $decl  = if ($a.declaration)   { [string]$a.declaration }   else { '' }
                $impl  = if ($a.implementation){ [string]$a.implementation }else { '' }
                $treePath = if ($a.path) { [string]$a.path } else { '' }
                $data = New-PouMember $dte $a.pou $a.name $mtype $ret $lang $decl $impl $treePath
            }
            'delete-member' {
                $mtype = if ($a.type) { [string]$a.type } else { 'method' }
                $treePath = if ($a.path) { [string]$a.path } else { '' }
                $dryRun = if ($null -ne $a.dry_run) { [bool]$a.dry_run } else { $false }
                $force = if ($null -ne $a.force) { [bool]$a.force } else { $false }
                $data = Remove-PouMember $dte $a.pou $a.name $mtype $treePath $dryRun $force
            }
            'rename-member' {
                $treePath = if ($a.path) { [string]$a.path } else { '' }
                $data = Rename-PouMember $dte $a.pou $a.old $a.new $treePath
            }
            'build-state' { $data = Get-TcBuildState $dte }
            'build'     {
                $always = $false
                if ($null -ne $a -and $null -ne $a.always_read_errors) {
                    $always = [bool]$a.always_read_errors
                }
                $action = if ($a -and $a.action) { [string]$a.action } else { 'build' }
                $data = Invoke-TcBuild $dte -AlwaysReadErrors $always -Action $action
            }
            'delete-pou' {
                $treePath = if ($a.path) { [string]$a.path } else { '' }
                $dryRun = if ($null -ne $a.dry_run) { [bool]$a.dry_run } else { $false }
                $force = if ($null -ne $a.force) { [bool]$a.force } else { $false }
                $data = Remove-Pou $dte $a.name @('POUs','DUTs','GVLs','Interfaces','VISUs') $treePath $dryRun $force
            }
            'rename'    {
                $treePath = if ($a.path) { [string]$a.path } else { '' }
                $data = Rename-Pou $dte $a.old $a.new @('POUs','DUTs','GVLs','Interfaces','VISUs') $treePath
            }
            'create-plc-project' {
                $nm  = if ($a.name)     { [string]$a.name }     else { 'PLC1' }
                $tpl = if ($a.template) { [string]$a.template } else { 'Standard PLC Template' }
                $data = New-PlcProject $dte $nm $tpl
            }
            'remove-plc-project' { $data = Remove-PlcProject $dte $a.name }
            'delete-plc-project' { $data = Remove-PlcProjectFiles $dte $a.name }
            'projects' { $data = Get-TcProjects $dte }
            'project-info' { $data = Get-TcProjectInfo $dte }
            'plc-runtimes' { $data = Get-TcPlcRuntimes $dte }
            'plc-online-state' {
                $sys = Get-TcSystemManager $dte
                $plcRoot = $sys.LookupTreeItem('TIPC')
                $roots = @($plcRoot)
                if ($a.runtime) { $roots = @($roots | Where-Object Name -eq ([string]$a.runtime)) }
                if ($roots.Count -ne 1) { throw 'Exactly one PLC runtime required' }
                $node = $roots[0].NestedProject
                $xml = [xml]$node.ProduceXml($false)
                $online = $xml.SelectSingleNode('//OnlineSettings')
                $logged = $null
                if ([string]$online.LoggedIn -eq 'true') { $logged = $true }
                elseif ([string]$online.LoggedIn -eq 'false') { $logged = $false }
                $data = [pscustomobject]@{name=[string]$roots[0].Name;logged_in=$logged;
                    operation_state=[string]$online.PlcOpState;application_state=[string]$online.PlcAppState;
                    source='NestedProject.ProduceXml.OnlineSettings'}
            }
            'hmi-create-project' {
                $name = if ($a -and $a.name) { [string]$a.name } else { 'TcHmiProject' }
                $output = if ($a -and $a.output_directory) { [string]$a.output_directory } else { '' }
                $template = if ($a -and $a.template) { [string]$a.template } else { '' }
                $apply = if ($a -and $a.PSObject.Properties.Name -contains 'apply') { [bool]$a.apply } else { $false }
                $data = New-TcHmiProject $dte $name $output $template $apply
            }
            'hmi-project-info' {
                $project = if ($a -and $a.project) { [string]$a.project } else { '' }
                $data = Get-TcHmiProjectInfo $dte $project
            }
            'hmi-structure' {
                $project = if ($a -and $a.project) { [string]$a.project } else { '' }
                $data = Get-TcHmiStructure $dte $project
            }
            'hmi-read' {
                $project = if ($a -and $a.project) { [string]$a.project } else { '' }
                $maxChars = if ($a -and $a.max_chars) { [int]$a.max_chars } else { 200000 }
                $controlId = if ($a -and $a.control_id) { [string]$a.control_id } else { '' }
                $includeContent = if ($a -and $a.PSObject.Properties.Name -contains 'include_content') { [bool]$a.include_content } else { $true }
                $maxControls = if ($a -and $a.max_controls) { [int]$a.max_controls } else { 200 }
                $controlOffset = if ($a -and $a.control_offset) { [int]$a.control_offset } else { 0 }
                $contentOffset = if ($a -and $a.content_offset) { [int]$a.content_offset } else { 0 }
                $data = Read-TcHmiFile $dte $project ([string]$a.file) $maxChars $controlId $includeContent $maxControls $controlOffset $contentOffset
            }
            'hmi-write-markup' {
                $project = if ($a -and $a.project) { [string]$a.project } else { '' }
                $apply = if ($a -and $a.PSObject.Properties.Name -contains 'apply') { [bool]$a.apply } else { $false }
                $data = Set-TcHmiMarkup $dte $project ([string]$a.file) ([string]$a.markup) $apply
            }
            'hmi-ads-info' {
                $project = if ($a -and $a.project) { [string]$a.project } else { '' }
                $data = Get-TcHmiAdsInfo $dte $project
            }
            'hmi-validate' {
                $project = if ($a -and $a.project) { [string]$a.project } else { '' }
                $data = Test-TcHmiProject $dte $project
            }
            'hmi-item-apply' {
                $project = if ($a -and $a.project) { [string]$a.project } else { '' }
                $data = Invoke-TcHmiNativeItem -Dte $dte -ProjectName $project -Request $a
            }
            'hmi-item-delete' {
                $project = if ($a -and $a.project) { [string]$a.project } else { '' }
                $data = Invoke-TcHmiNativeDelete -Dte $dte -ProjectName $project -Plan $a.plan
            }
            'hmi-native-server-symbol-read' {
                $project = if ($a -and $a.project) { [string]$a.project } else { '' }
                $names = if ($a -and $a.PSObject.Properties.Name -contains 'symbol_names') { @($a.symbol_names | ForEach-Object { [string]$_ }) } else { @() }
                $data = Get-TcHmiNativeServerSymbol -Dte $dte -ProjectName $project -SymbolNames $names
            }
            'hmi-create-view' {
                $project = if ($a -and $a.project) { [string]$a.project } else { '' }
                $kind = if ($a -and $a.kind) { [string]$a.kind } else { 'view' }
                $controls = if ($a -and $a.PSObject.Properties.Name -contains 'controls') { @($a.controls) } else { @() }
                $apply = if ($a -and $a.PSObject.Properties.Name -contains 'apply') { [bool]$a.apply } else { $false }
                $data = Invoke-TcHmiNativePage $dte $project ([string]$a.name) $kind $controls $apply
            }
            'hmi-project-api' {
                $project = if ($a -and $a.project) { [string]$a.project } else { '' }
                $operation = if ($a -and $a.operation) { [string]$a.operation } else { '' }
                $arguments = if ($a -and $a.PSObject.Properties.Name -contains 'arguments') { $a.arguments } else { [pscustomobject]@{} }
                $apply = if ($a -and $a.PSObject.Properties.Name -contains 'apply') { [bool]$a.apply } else { $false }
                $data = Invoke-TcHmiProjectApi $dte $project $operation $arguments $apply
            }
            'hmi-startup-view-set' {
                $project = if ($a -and $a.project) { [string]$a.project } else { '' }
                $data = Set-TcHmiStartupView $dte $project ([string]$a.view)
            }
            'hmi-control-edit' {
                $project = if ($a -and $a.project) { [string]$a.project } else { '' }
                $parent = if ($a -and $a.parent_id) { [string]$a.parent_id } else { '' }
                $type = if ($a -and $a.type) { [string]$a.type } else { '' }
                $attributes = if ($a -and $a.PSObject.Properties.Name -contains 'attributes') { $a.attributes } else { $null }
                $apply = if ($a -and $a.PSObject.Properties.Name -contains 'apply') { [bool]$a.apply } else { $false }
                $data = Edit-TcHmiControl $dte $project ([string]$a.file) ([string]$a.action) `
                    ([string]$a.control_id) $type $parent $attributes $apply
            }
            'hmi-controls-batch' {
                $project = if ($a -and $a.project) { [string]$a.project } else { '' }
                $apply = if ($a -and $a.PSObject.Properties.Name -contains 'apply') { [bool]$a.apply } else { $false }
                $data = Edit-TcHmiControlsBatch $dte $project ([string]$a.file) @($a.operations) $apply
            }
            'hmi-delete-view' {
                throw 'Legacy direct deletion disabled. Use tc_hmi_item_delete or the CLI/Python delete wrapper for reference review and native cleanup.'
            }
            'hmi-ads-runtime-set' {
                $project = if ($a -and $a.project) { [string]$a.project } else { '' }
                $scope = if ($a -and $a.scope) { [string]$a.scope } else { 'both' }
                $action = if ($a -and $a.action) { [string]$a.action } else { 'upsert' }
                $settings = if ($a -and $a.PSObject.Properties.Name -contains 'settings') { $a.settings } else { $null }
                $apply = if ($a -and $a.PSObject.Properties.Name -contains 'apply') { [bool]$a.apply } else { $false }
                $data = Set-TcHmiAdsRuntime $dte $project $scope $action ([string]$a.name) $settings $apply
            }
            'hmi-ads-symbols' {
                $project = if ($a -and $a.project) { [string]$a.project } else { '' }
                $runtime = if ($a -and $a.runtime) { [string]$a.runtime } else { '' }
                $scope = if ($a -and $a.scope) { [string]$a.scope } else { '' }
                $data = Get-TcHmiAdsSymbols $dte $project $runtime $scope
            }
            'hmi-ads-symbol-set' {
                $project = if ($a -and $a.project) { [string]$a.project } else { '' }
                $scope = if ($a -and $a.scope) { [string]$a.scope } else { 'both' }
                $action = if ($a -and $a.action) { [string]$a.action } else { 'upsert' }
                $indexGroup = if ($a -and $a.PSObject.Properties.Name -contains 'index_group') { [uint32]$a.index_group } else { 0 }
                $indexOffset = if ($a -and $a.PSObject.Properties.Name -contains 'index_offset') { [uint32]$a.index_offset } else { 0 }
                $typeName = if ($a -and $a.type_name) { [string]$a.type_name } else { '' }
                $apply = if ($a -and $a.PSObject.Properties.Name -contains 'apply') { [bool]$a.apply } else { $false }
                $data = Set-TcHmiAdsSymbol $dte $project $scope ([string]$a.runtime) ([string]$a.name) `
                    $action $indexGroup $indexOffset $typeName $apply
            }
            'hmi-dynamic-symbols-set' {
                $project = if ($a -and $a.project) { [string]$a.project } else { '' }
                $symbols = if ($a -and $a.PSObject.Properties.Name -contains 'symbols') { $a.symbols } else { [pscustomobject]@{} }
                $definitions = if ($a -and $a.PSObject.Properties.Name -contains 'definitions') { $a.definitions } else { $null }
                $apply = if ($a -and $a.PSObject.Properties.Name -contains 'apply') { [bool]$a.apply } else { $false }
                $data = Set-TcHmiDynamicSymbols $dte $project $symbols $definitions $apply
            }
            'hmi-bind-plc-apply' {
                $project = if ($a -and $a.project) { [string]$a.project } else { '' }
                $scope = if ($a -and $a.scope) { [string]$a.scope } else { 'default' }
                $symbols = if ($a -and $a.PSObject.Properties.Name -contains 'symbols') { $a.symbols } else { [pscustomobject]@{} }
                $definitions = if ($a -and $a.PSObject.Properties.Name -contains 'definitions') { $a.definitions } else { $null }
                $apply = if ($a -and $a.PSObject.Properties.Name -contains 'apply') { [bool]$a.apply } else { $false }
                $data = Set-TcHmiPlcBinding $dte $project ([string]$a.runtime) ([string]$a.netid) `
                    ([int]$a.port) $scope $symbols $definitions $apply
            }
            'hmi-bindings' {
                $project = if ($a -and $a.project) { [string]$a.project } else { '' }
                $data = Get-TcHmiBindings $dte $project
            }
            'hmi-internal-symbols' {
                $project = if ($a -and $a.project) { [string]$a.project } else { '' }
                $data = Get-TcHmiInternalSymbols $dte $project
            }
            'hmi-internal-symbol-set' {
                $project = if ($a -and $a.project) { [string]$a.project } else { '' }
                $action = if ($a -and $a.action) { [string]$a.action } else { 'upsert' }
                $settings = if ($a -and $a.PSObject.Properties.Name -contains 'settings') { $a.settings } else { $null }
                $apply = if ($a -and $a.PSObject.Properties.Name -contains 'apply') { [bool]$a.apply } else { $false }
                $data = Set-TcHmiInternalSymbol $dte $project ([string]$a.name) $action $settings $apply
            }
            'hmi-localizations' {
                $project = if ($a -and $a.project) { [string]$a.project } else { '' }
                $data = Get-TcHmiLocalizations $dte $project
            }
            'hmi-localization-set' {
                $project = if ($a -and $a.project) { [string]$a.project } else { '' }
                $action = if ($a -and $a.action) { [string]$a.action } else { 'upsert' }
                $values = if ($a -and $a.PSObject.Properties.Name -contains 'values') { $a.values } else { $null }
                $apply = if ($a -and $a.PSObject.Properties.Name -contains 'apply') { [bool]$a.apply } else { $false }
                $data = Set-TcHmiLocalization $dte $project ([string]$a.key) $action $values $apply
            }
            'hmi-themes' {
                $project = if ($a -and $a.project) { [string]$a.project } else { '' }
                $data = Get-TcHmiThemes $dte $project
            }
            'hmi-themed-resource-set' {
                $project = if ($a -and $a.project) { [string]$a.project } else { '' }
                $action = if ($a -and $a.action) { [string]$a.action } else { 'upsert' }
                $settings = if ($a -and $a.PSObject.Properties.Name -contains 'settings') { $a.settings } else { $null }
                $apply = if ($a -and $a.PSObject.Properties.Name -contains 'apply') { [bool]$a.apply } else { $false }
                $data = Set-TcHmiThemedResource $dte $project ([string]$a.name) $action $settings $apply
            }
            'hmi-active-theme-set' {
                $project = if ($a -and $a.project) { [string]$a.project } else { '' }
                $apply = if ($a -and $a.PSObject.Properties.Name -contains 'apply') { [bool]$a.apply } else { $false }
                $data = Set-TcHmiActiveTheme $dte $project ([string]$a.theme) $apply
            }
            'hmi-user-controls' {
                $project = if ($a -and $a.project) { [string]$a.project } else { '' }
                $data = Get-TcHmiUserControls $dte $project
            }
            'hmi-user-control-create' {
                $project = if ($a -and $a.project) { [string]$a.project } else { '' }
                $parameters = if ($a -and $a.PSObject.Properties.Name -contains 'parameters') { @($a.parameters) } else { @() }
                $controls = if ($a -and $a.PSObject.Properties.Name -contains 'controls') { @($a.controls) } else { @() }
                $apply = if ($a -and $a.PSObject.Properties.Name -contains 'apply') { [bool]$a.apply } else { $false }
                # Named parameters preserve empty arrays.  Positional
                # invocation expands @() to zero arguments and shifts the
                # following values into the wrong parameters.
                $data = New-TcHmiUserControl -Dte $dte -ProjectName $project `
                    -Name ([string]$a.name) -Parameters ([object[]]$parameters) `
                    -Controls ([object[]]$controls) -Apply $apply
            }
            'hmi-user-control-parameter-set' {
                $project = if ($a -and $a.project) { [string]$a.project } else { '' }
                $action = if ($a -and $a.action) { [string]$a.action } else { 'upsert' }
                $settings = if ($a -and $a.PSObject.Properties.Name -contains 'settings') { $a.settings } else { $null }
                $apply = if ($a -and $a.PSObject.Properties.Name -contains 'apply') { [bool]$a.apply } else { $false }
                $data = Set-TcHmiUserControlParameter $dte $project ([string]$a.user_control) ([string]$a.name) $action $settings $apply
            }
            'hmi-user-control-delete' {
                throw 'Legacy direct deletion disabled. Use tc_hmi_item_delete or the CLI/Python delete wrapper for reference review and native cleanup.'
            }
            'hmi-framework-templates' {
                $project = if ($a -and $a.project) { [string]$a.project } else { '' }
                $data = Get-TcHmiFrameworkTemplates $dte $project
            }
            'hmi-framework-validate' {
                $data = Test-TcHmiFrameworkProject ([string]$a.source)
            }
            'hmi-framework-control-info' {
                $control = if ($a -and $a.control) { [string]$a.control } else { '' }
                $data = Get-TcHmiFrameworkControlInfo ([string]$a.source) $control
            }
            'hmi-framework-attribute-set' {
                $control = if ($a -and $a.control) { [string]$a.control } else { '' }
                $action = if ($a -and $a.action) { [string]$a.action } else { 'upsert' }
                $settings = if ($a -and $a.PSObject.Properties.Name -contains 'settings') { $a.settings } else { $null }
                $apply = if ($a -and $a.PSObject.Properties.Name -contains 'apply') { [bool]$a.apply } else { $false }
                $data = Set-TcHmiFrameworkAttribute -Source ([string]$a.source) -Control $control `
                    -Name ([string]$a.name) -Action $action -Settings $settings -Apply $apply
            }
            'hmi-framework-event-set' {
                $control = if ($a -and $a.control) { [string]$a.control } else { '' }
                $action = if ($a -and $a.action) { [string]$a.action } else { 'upsert' }
                $settings = if ($a -and $a.PSObject.Properties.Name -contains 'settings') { $a.settings } else { $null }
                $apply = if ($a -and $a.PSObject.Properties.Name -contains 'apply') { [bool]$a.apply } else { $false }
                $data = Set-TcHmiFrameworkEvent -Source ([string]$a.source) -Control $control `
                    -Name ([string]$a.name) -Action $action -Settings $settings -Apply $apply
            }
            'hmi-framework-create' {
                $project = if ($a -and $a.project) { [string]$a.project } else { '' }
                $language = if ($a -and $a.language) { [string]$a.language } else { 'typescript' }
                $description = if ($a -and $a.description) { [string]$a.description } else { '' }
                $apply = if ($a -and $a.PSObject.Properties.Name -contains 'apply') { [bool]$a.apply } else { $false }
                $data = New-TcHmiFrameworkProject -Dte $dte -ProjectName $project -Name ([string]$a.name) `
                    -OutputDirectory ([string]$a.output_directory) -Language $language `
                    -Description $description -Apply $apply
            }
            'hmi-framework-pack' {
                $outputDirectory = if ($a -and $a.output_directory) { [string]$a.output_directory } else { '' }
                $version = if ($a -and $a.version) { [string]$a.version } else { '' }
                $apply = if ($a -and $a.PSObject.Properties.Name -contains 'apply') { [bool]$a.apply } else { $false }
                $data = Invoke-TcHmiFrameworkPack -Source ([string]$a.source) `
                    -OutputDirectory $outputDirectory -Version $version -Apply $apply
            }
            'hmi-framework-packages' {
                $project = if ($a -and $a.project) { [string]$a.project } else { '' }
                $packageId = if ($a -and $a.package_id) { [string]$a.package_id } else { '' }
                $data = Get-TcHmiFrameworkPackages $dte $project $packageId
            }
            'hmi-framework-package-inspect' {
                $data = Read-TcHmiFrameworkPackage ([string]$a.package)
            }
            'hmi-framework-install' {
                $project = if ($a -and $a.project) { [string]$a.project } else { '' }
                $apply = if ($a -and $a.PSObject.Properties.Name -contains 'apply') { [bool]$a.apply } else { $false }
                $acknowledge = if ($a -and $a.PSObject.Properties.Name -contains 'acknowledge_package_change') { [bool]$a.acknowledge_package_change } else { $false }
                $data = Install-TcHmiFrameworkPackage -Dte $dte -ProjectName $project `
                    -Package ([string]$a.package) -Apply $apply -AcknowledgePackageChange $acknowledge
            }
            'hmi-framework-uninstall' {
                $project = if ($a -and $a.project) { [string]$a.project } else { '' }
                $force = if ($a -and $a.PSObject.Properties.Name -contains 'force') { [bool]$a.force } else { $false }
                $apply = if ($a -and $a.PSObject.Properties.Name -contains 'apply') { [bool]$a.apply } else { $false }
                $acknowledge = if ($a -and $a.PSObject.Properties.Name -contains 'acknowledge_package_change') { [bool]$a.acknowledge_package_change } else { $false }
                $data = Uninstall-TcHmiFrameworkPackage -Dte $dte -ProjectName $project `
                    -PackageId ([string]$a.package_id) -Force $force -Apply $apply `
                    -AcknowledgePackageChange $acknowledge
            }
            'hmi-runtime-info' {
                $project = if ($a -and $a.project) { [string]$a.project } else { '' }
                $data = Get-TcHmiRuntimeInfo $dte $project
            }
            'hmi-server-control' {
                $project = if ($a -and $a.project) { [string]$a.project } else { '' }
                $apply = if ($a -and $a.PSObject.Properties.Name -contains 'apply') { [bool]$a.apply } else { $false }
                $data = Invoke-TcHmiServerControl -Dte $dte -ProjectName $project `
                    -Action ([string]$a.action) -Apply $apply
            }
            'hmi-browser-validate' {
                $project = if ($a -and $a.project) { [string]$a.project } else { '' }
                $widths = if ($a -and $a.PSObject.Properties.Name -contains 'widths') { @($a.widths | ForEach-Object { [int]$_ }) } else { @(1280) }
                $height = if ($a -and $a.height) { [int]$a.height } else { 720 }
                $settleMs = if ($a -and $a.settle_ms) { [int]$a.settle_ms } else { 5000 }
                $data = Test-TcHmiBrowserRuntime -Dte $dte -ProjectName $project `
                    -Widths ([int[]]$widths) -Height $height -SettleMs $settleMs
            }
            'hmi-diagnostics' {
                $project = if ($a -and $a.project) { [string]$a.project } else { '' }
                $data = Get-TcHmiDiagnostics $dte $project
            }
            'hmi-build' {
                $project = if ($a -and $a.project) { [string]$a.project } else { '' }
                $data = Invoke-TcHmiBuild $dte $project
            }
            'safety-structure' {
                $depth = if ($a -and $a.max_depth) { [int]$a.max_depth } else { 8 }
                $data = Get-TcSafetyStructure $dte $depth
            }
            'safety-project-info' {
                $project = if ($a -and $a.project) { [string]$a.project } else { '' }
                $data = Get-TcSafetyProjectInfo $dte $project
            }
            'safety-files' {
                $data = Get-TcSafetyFileInventory $dte ([string]$a.project)
            }
            'safety-target-info' {
                $data = Get-TcSafetyTargetConfiguration $dte ([string]$a.project)
            }
            'safety-aliases' {
                $group = if ($a -and $a.group) { [string]$a.group } else { '' }
                $data = Get-TcSafetyAliasDevices $dte ([string]$a.project) $group
            }
            'safety-application' {
                $group = if ($a -and $a.group) { [string]$a.group } else { '' }
                $data = Get-TcSafetyApplicationStructure $dte ([string]$a.project) $group
            }
            'safety-logic-check' {
                $group = if ($a -and $a.group) { [string]$a.group } else { '' }
                $data = Test-TcSafetyLogicStructure $dte ([string]$a.project) $group
            }
            'safety-validate' {
                $source = if ($a -and $a.source) { [string]$a.source } else { '' }
                $data = Test-TcSafetyConfiguration $dte $source
            }
            'safety-import' {
                $name = if ($a -and $a.name) { [string]$a.name } else { '' }
                $mode = if ($a -and $a.mode) { [string]$a.mode } else { 'copy' }
                $apply = if ($a -and $a.PSObject.Properties.Name -contains 'apply') { [bool]$a.apply } else { $false }
                $confirmMove = if ($a -and $a.PSObject.Properties.Name -contains 'confirm_source_move') { [bool]$a.confirm_source_move } else { $false }
                $review = if ($a -and $a.PSObject.Properties.Name -contains 'acknowledge_safety_review') { [bool]$a.acknowledge_safety_review } else { $false }
                $data = Import-TcSafetyProject $dte $name ([string]$a.source) $mode $apply $confirmMove $review
            }
            'safety-create' {
                $target = if ($a -and $a.target) { [string]$a.target } else { 'hardware' }
                $template = if ($a -and $a.template) { [string]$a.template } else { 'preconfigured-inputs' }
                $author = if ($a -and $a.PSObject.Properties.Name -contains 'author') { [string]$a.author } else { 'TwinCAT Agent' }
                $internalName = if ($a -and $a.internal_project_name) { [string]$a.internal_project_name } else { '' }
                $apply = if ($a -and $a.PSObject.Properties.Name -contains 'apply') { [bool]$a.apply } else { $false }
                $review = if ($a -and $a.PSObject.Properties.Name -contains 'acknowledge_safety_review') { [bool]$a.acknowledge_safety_review } else { $false }
                $data = New-TcSafetyProject $dte ([string]$a.name) $target $template $author $internalName $apply $review
            }
            'safety-export' {
                $overwrite = if ($a -and $a.PSObject.Properties.Name -contains 'overwrite') { [bool]$a.overwrite } else { $false }
                $apply = if ($a -and $a.PSObject.Properties.Name -contains 'apply') { [bool]$a.apply } else { $false }
                $review = if ($a -and $a.PSObject.Properties.Name -contains 'acknowledge_safety_review') { [bool]$a.acknowledge_safety_review } else { $false }
                $data = Export-TcSafetyProject $dte ([string]$a.project) ([string]$a.output_file) $overwrite $apply $review
            }
            'safety-remove' {
                $apply = if ($a -and $a.PSObject.Properties.Name -contains 'apply') { [bool]$a.apply } else { $false }
                $confirmName = if ($a -and $a.confirm_project_name) { [string]$a.confirm_project_name } else { '' }
                $review = if ($a -and $a.PSObject.Properties.Name -contains 'acknowledge_safety_review') { [bool]$a.acknowledge_safety_review } else { $false }
                $data = Remove-TcSafetyProject $dte ([string]$a.project) $apply $confirmName $review
            }
            'safety-delete' {
                $backupFile = if ($a -and $a.backup_file) { [string]$a.backup_file } else { '' }
                $apply = if ($a -and $a.PSObject.Properties.Name -contains 'apply') { [bool]$a.apply } else { $false }
                $confirmName = if ($a -and $a.confirm_project_name) { [string]$a.confirm_project_name } else { '' }
                $confirmFiles = if ($a -and $a.PSObject.Properties.Name -contains 'confirm_delete_files') { [bool]$a.confirm_delete_files } else { $false }
                $review = if ($a -and $a.PSObject.Properties.Name -contains 'acknowledge_safety_review') { [bool]$a.acknowledge_safety_review } else { $false }
                $data = Delete-TcSafetyProject $dte ([string]$a.project) $backupFile $apply $confirmName $confirmFiles $review
            }
            'system-structure' {
                $depth = if ($a -and $a.max_depth) { [int]$a.max_depth } else { 6 }
                $roots = if ($a -and $a.PSObject.Properties.Name -contains 'roots') { @($a.roots) } else { $null }
                $data = Get-TcSystemStructure $dte $depth $roots
            }
            'system-settings' { $data = Get-TcSystemSettings $dte }
            'system-settings-set' {
                $apply = if ($a -and $a.PSObject.Properties.Name -contains 'apply') { [bool]$a.apply } else { $false }
                $data = Set-TcSystemSettings $dte $a.settings $apply
            }
            'core-info' { $data = Get-TcCoreInfo $dte }
            'realtime-info' { $data = Get-TcRealtimeInfo $dte }
            'core-assign' {
                $apply = if ($a -and $a.PSObject.Properties.Name -contains 'apply') { [bool]$a.apply } else { $false }
                $max = if ($a -and $a.PSObject.Properties.Name -contains 'max_cpus' -and $null -ne $a.max_cpus) { [Nullable[int]]([int]$a.max_cpus) } else { $null }
                $aff = if ($a -and $a.PSObject.Properties.Name -contains 'affinity' -and $null -ne $a.affinity) { [Nullable[long]]([long](Convert-TcSystemInteger $a.affinity 'affinity')) } else { $null }
                $pc = if ($a -and $a.PSObject.Properties.Name -contains 'p_core_affinity' -and $null -ne $a.p_core_affinity) { [Nullable[long]]([long]$a.p_core_affinity) } else { $null }
                $ec = if ($a -and $a.PSObject.Properties.Name -contains 'e_core_affinity' -and $null -ne $a.e_core_affinity) { [Nullable[long]]([long]$a.e_core_affinity) } else { $null }
                $data = Set-TcCoreAssignment $dte @($a.cpu_ids) $max $pc $ec $aff $apply
            }
            'task-info' {
                $depth = if ($a -and $a.max_depth) { [int]$a.max_depth } else { 6 }
                $data = Get-TcTaskInfo $dte $depth
            }
            'task-settings-set' {
                $apply = if ($a -and $a.PSObject.Properties.Name -contains 'apply') { [bool]$a.apply } else { $false }
                $data = Set-TcTaskSettings $dte $a.task_path $a.settings $apply
            }
            'task-core-assign' {
                $apply = if ($a -and $a.PSObject.Properties.Name -contains 'apply') { [bool]$a.apply } else { $false }
                $data = Set-TcTaskCoreAssignment $dte $a.task_path ([int]$a.cpu_id) $apply
            }
            'system-add' {
                $apply = if ($a -and $a.PSObject.Properties.Name -contains 'apply') { [bool]$a.apply } else { $false }
                $info = if ($a -and $a.info) { [string]$a.info } else { '' }
                $data = New-TcSystemItem $dte $a.parent_path $a.name ([int]$a.item_type) $info $apply
            }
            'system-remove' {
                $apply = if ($a -and $a.PSObject.Properties.Name -contains 'apply') { [bool]$a.apply } else { $false }
                $allowChildren = if ($a -and $a.PSObject.Properties.Name -contains 'allow_with_children') { [bool]$a.allow_with_children } else { $false }
                $data = Remove-TcSystemItem $dte $a.path $apply $allowChildren
            }
            'realtime-refresh' { $data = Invoke-TcRealtimeRefresh $dte }
            'target-show' { $data = Get-TcTarget $dte }
            'target-set' { $data = Set-TcTarget $dte $a.netid }
            'io-scan' {
                $confirmed = $false
                $allowUnknown = $false
                if ($a -and $a.PSObject.Properties.Name -contains 'config_confirmed') {
                    $confirmed = [bool]$a.config_confirmed
                }
                if ($a -and $a.PSObject.Properties.Name -contains 'allow_unknown') {
                    $allowUnknown = [bool]$a.allow_unknown
                }
                $data = Invoke-TcIoScan $dte $confirmed $allowUnknown
            }
            'close-solution' { $data = Close-TcSolution $dte }
            'open-solution' { $data = Open-TcSolution $dte $a.path }
            'import-plcopen' {
                $opt = if ($a.options) { [int]$a.options } else { 0 }
                $data = Import-PlcOpen $dte $a.file $opt
            }
            'export-plcopen' { $data = Export-PlcOpen $dte $a.file @($a.pous) }
            'lib-list'  { $data = Get-LibraryList $dte }
            'lib-scan'  { $data = Get-InstalledLibraries $dte }
            'lib-add'   {
                $ver = if ($a.version) { [string]$a.version } else { '*' }
                $dis = if ($a.distributor) { [string]$a.distributor } else { '' }
                $data = Add-LibraryRef $dte $a.name $ver $dis
            }
            'lib-remove' {
                $ver = if ($a.version) { [string]$a.version } else { '' }
                $dis = if ($a.distributor) { [string]$a.distributor } else { '' }
                $data = Remove-LibraryRef $dte $a.name $ver $dis
            }
            'placeholder-add' {
                $dl = if ($a.default_lib) { [string]$a.default_lib } else { '' }
                $dv = if ($a.default_version) { [string]$a.default_version } else { '*' }
                $dd = if ($a.default_distributor) { [string]$a.default_distributor } else { '' }
                $data = Add-PlaceholderRef $dte $a.name $dl $dv $dd
            }
            'placeholder-freeze' { $data = Set-PlaceholderFrozen $dte $a.name }
            'repo-insert' {
                $idx = if ($a.index) { [int]$a.index } else { 0 }
                $data = Add-LibRepository $dte $a.name $a.root_folder $idx
            }
            'repo-remove' { $data = Remove-LibRepository $dte $a.name }
            'lib-install' {
                $ow = if ($a.overwrite) { [bool]$a.overwrite } else { $false }
                $data = Install-Library $dte $a.repository $a.lib_path $ow
            }
            'lib-uninstall' {
                $ver = if ($a.version) { [string]$a.version } else { '' }
                $dis = if ($a.distributor) { [string]$a.distributor } else { '' }
                $data = Uninstall-Library $dte $a.repository $a.library $ver $dis
            }
            'find-pou' {
                $folder = if ($a.folder) { [string]$a.folder } else { '' }
                $limit = if ($a.limit) { [int]$a.limit } else { 30 }
                $includeMembers = $true
                if ($a -and $a.PSObject.Properties.Name -contains 'include_members') {
                    $includeMembers = [bool]$a.include_members
                }
                $data = Find-Pou $dte $a.query $folder $limit $includeMembers
            }
            'search-code' {
                $regex = if ($a.regex) { [bool]$a.regex } else { $false }
                $caseSensitive = if ($a.case_sensitive) { [bool]$a.case_sensitive } else { $false }
                $pou = if ($a.pou) { [string]$a.pou } else { '' }
                $maxResults = if ($a.max_results) { [int]$a.max_results } else { 50 }
                $treePath = if ($a.path) { [string]$a.path } else { '' }
                $data = Search-PlcCode $dte $a.pattern $regex $caseSensitive $pou $maxResults $treePath
            }
            'structure' {
                $limit = if ($a.limit_per_folder) { [int]$a.limit_per_folder } else { 0 }
                $includeMembers = $true
                if ($a -and $a.PSObject.Properties.Name -contains 'include_members') {
                    $includeMembers = [bool]$a.include_members
                }
                $data = Get-Structure $dte -LimitPerFolder $limit -IncludeMembers $includeMembers
            }
            'solution-tree' {
                $maxNodes = if ($a.max_nodes) { [int]$a.max_nodes } else { 2500 }
                $data = Get-PlcTree $dte -MaxNodes $maxNodes
            }
            'all-code'  { $data = Get-AllCode $dte }
            'code-inventory' {
                $includeCode = $false
                if ($a -and $a.PSObject.Properties.Name -contains 'include_code') {
                    $includeCode = [bool]$a.include_code
                }
                $paths = @()
                if ($a -and $a.PSObject.Properties.Name -contains 'paths') {
                    $paths = @($a.paths | ForEach-Object { [string]$_ })
                }
                $data = Get-CodeInventory $dte $includeCode $paths
                if ($a -and $a.output_file) {
                    $outputFile = [IO.Path]::GetFullPath([string]$a.output_file)
                    $outputParent = [IO.Path]::GetDirectoryName($outputFile)
                    if (-not [IO.Directory]::Exists($outputParent)) {
                        throw "Inventory output directory does not exist: '$outputParent'"
                    }
                    $inventoryJson = $data | ConvertTo-Json -Depth 8 -Compress
                    [IO.File]::WriteAllText(
                        $outputFile, $inventoryJson, [Text.UTF8Encoding]::new($false))
                    $data = [pscustomobject]@{
                        solution = $data.solution; object_count = $data.object_count
                        written_to = $outputFile
                    }
                }
            }
            'state'     { $data = Get-TcState $dte }
            'activate'  { $data = Invoke-Activate $dte }
            'restart'   { $data = Invoke-Restart $dte }
            'login'     { $data = Send-OnlineCommand $dte @{LoginCmd='true'} ([string]$a.runtime) ([bool]$a.all_plcs) }
            'logout'    { $data = Send-OnlineCommand $dte @{LogoutCmd='true'} ([string]$a.runtime) ([bool]$a.all_plcs) }
            'start'     { $data = Send-OnlineCommand $dte @{StartCmd='true'} ([string]$a.runtime) ([bool]$a.all_plcs) }
            'stop'      { $data = Send-OnlineCommand $dte @{StopCmd='true'} ([string]$a.runtime) ([bool]$a.all_plcs) }
            'online'    {
                $login = Send-OnlineCommand $dte @{LoginCmd='true'} ([string]$a.runtime) ([bool]$a.all_plcs)
                $data = [pscustomobject]@{status='incomplete';login=$login;verified=$false;reason='login_state_unverified'}
            }
            'config-mode' {
                $strategy = if ($a -and $a.strategy) { [string]$a.strategy } else { 'consume' }
                $data = Set-TcRtState $dte 'Config' $strategy
            }
            'run-mode'  { $data = Set-TcRtState $dte 'Run' }
                default     { throw "Unknown command: $Command" }
            }
        } finally {
            if ($restoreSilentMode -and $null -ne $silentSettings) {
                try { $silentSettings.SilentMode = $previousSilentMode } catch { }
            }
        }
        ConvertTo-AsciiJson ([pscustomobject]@{ ok = $true; data = $data })
    } catch {
        $failureMessage = [string]$_.Exception.Message
        if ($Command -eq 'hmi-write-markup') {
            $failureMessage += ' [HMI write location: ' + [string]$_.ScriptStackTrace + ']'
        }
        ConvertTo-AsciiJson ([pscustomobject]@{ ok = $false; error = $failureMessage })
    }
}
