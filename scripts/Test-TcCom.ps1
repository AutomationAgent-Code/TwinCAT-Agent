param(
    [switch]$SkipRelaunch,
    [switch]$Pause
)

# TwinCAT Agent TcCom read-only diagnostic.
# It does not change the registry, TwinCAT configuration, routes, or projects.

$ErrorActionPreference = 'Stop'

# TwinCAT 4024/4026 isolated shells are 32-bit. Run the probe in the same
# Windows PowerShell bitness used by TwinCAT Agent.
if (-not $SkipRelaunch -and [Environment]::Is64BitOperatingSystem -and
    [Environment]::Is64BitProcess) {
    $ps32 = Join-Path $env:SystemRoot 'SysWOW64\WindowsPowerShell\v1.0\powershell.exe'
    if (Test-Path -LiteralPath $ps32) {
        $childArgs = @(
            '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
            '-File', $PSCommandPath, '-SkipRelaunch'
        )
        if ($Pause) { $childArgs += '-Pause' }
        & $ps32 @childArgs
        exit $LASTEXITCODE
    }
}

$script:Report = New-Object System.Collections.Generic.List[string]
function Write-ProbeLine {
    param([string]$Name, [string]$Status, [string]$Detail = '')
    $line = '[{0,-5}] {1,-24} {2}' -f $Status, $Name, $Detail
    $script:Report.Add($line)
    $color = switch ($Status) {
        'PASS' { 'Green' }
        'WARN' { 'Yellow' }
        'FAIL' { 'Red' }
        default { 'Gray' }
    }
    Write-Host $line -ForegroundColor $color
}

Write-Host '=== TwinCAT Agent TcCom Diagnostic ===' -ForegroundColor Cyan
Write-ProbeLine 'Timestamp' 'INFO' (Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz')
Write-ProbeLine 'PowerShell' 'INFO' ("$($PSVersionTable.PSVersion); " +
    $(if ([Environment]::Is64BitProcess) { '64-bit' } else { '32-bit' }))
Write-ProbeLine 'LanguageMode' `
    $(if ($ExecutionContext.SessionState.LanguageMode -eq 'FullLanguage') { 'PASS' } else { 'FAIL' }) `
    ([string]$ExecutionContext.SessionState.LanguageMode)
Write-ProbeLine 'ExecutionPolicy' 'INFO' ([string](Get-ExecutionPolicy))

$isAdmin = $false
try {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    $isAdmin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
} catch { }
Write-ProbeLine 'Agent privilege' 'INFO' $(if ($isAdmin) { 'Elevated/Administrator' } else { 'Standard user' })

$xae = @(Get-Process -Name TcXaeShell,devenv -ErrorAction SilentlyContinue)
if ($xae.Count -gt 0) {
    foreach ($process in $xae) {
        Write-ProbeLine 'XAE process' 'PASS' ("name=$($process.ProcessName), pid=$($process.Id), " +
            "window=0x$($process.MainWindowHandle.ToString('X'))")
    }
} else {
    Write-ProbeLine 'XAE process' 'WARN' 'No TcXaeShell.exe or devenv.exe is running.'
}

$installedCandidates = @(
    (Join-Path $env:LOCALAPPDATA 'Programs\TwinCAT Agent\app\tc_template\TcCom.ps1'),
    (Join-Path $env:ProgramFiles 'TwinCAT Agent\app\tc_template\TcCom.ps1'),
    (Join-Path ${env:ProgramFiles(x86)} 'TwinCAT Agent\app\tc_template\TcCom.ps1')
)
$installedTcCom = $installedCandidates | Where-Object { Test-Path -LiteralPath $_ } |
    Select-Object -First 1
if ($installedTcCom) {
    $item = Get-Item -LiteralPath $installedTcCom
    $hash = (Get-FileHash -LiteralPath $installedTcCom -Algorithm SHA256).Hash
    Write-ProbeLine 'Installed TcCom.ps1' 'PASS' "$installedTcCom"
    Write-ProbeLine 'TcCom file info' 'INFO' ("bytes=$($item.Length), modified=$($item.LastWriteTime), " +
        "sha256=$hash")
} else {
    Write-ProbeLine 'Installed TcCom.ps1' 'WARN' 'Not found in standard TwinCAT Agent paths.'
}

$addTypeOk = $false
$addTypeError = ''
try {
    Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
public static class TcRotProbe {
    [DllImport("ole32.dll")] static extern int GetRunningObjectTable(int r, out IRunningObjectTable rot);
    [DllImport("ole32.dll")] static extern int CreateBindCtx(int r, out IBindCtx ctx);
    public static List<string> Names() {
        var result = new List<string>();
        IRunningObjectTable rot; IBindCtx ctx;
        if (GetRunningObjectTable(0, out rot) != 0 || CreateBindCtx(0, out ctx) != 0) return result;
        IEnumMoniker iterator; rot.EnumRunning(out iterator); iterator.Reset();
        IMoniker[] monikers = new IMoniker[1];
        while (iterator.Next(1, monikers, IntPtr.Zero) == 0) {
            string name = null;
            try { monikers[0].GetDisplayName(ctx, null, out name); } catch { }
            if (!String.IsNullOrEmpty(name)) result.Add(name);
        }
        return result;
    }
}
'@ -ErrorAction Stop
    $addTypeOk = $null -ne ('TcRotProbe' -as [type])
} catch {
    $addTypeError = [string]$_.Exception.Message
}

if ($addTypeOk) {
    Write-ProbeLine 'Add-Type / TcRot' 'PASS' 'Dynamic C# type compilation is allowed.'
} else {
    Write-ProbeLine 'Add-Type / TcRot' 'FAIL' $addTypeError
}

$rotMatches = @()
if ($addTypeOk) {
    try {
        $allNames = @([TcRotProbe]::Names())
        $rotMatches = @($allNames | Where-Object {
            $_ -match '(?i)(TcXaeShell|VisualStudio)\.DTE'
        })
        if ($rotMatches.Count -gt 0) {
            foreach ($name in $rotMatches) { Write-ProbeLine 'ROT DTE' 'PASS' $name }
        } else {
            Write-ProbeLine 'ROT DTE' 'WARN' 'No TwinCAT/Visual Studio DTE moniker is visible.'
        }
    } catch {
        Write-ProbeLine 'ROT enumeration' 'FAIL' ([string]$_.Exception.Message)
    }
}

$progIds = @(
    'TcXaeShell.DTE.17.0',
    'TcXaeShell.DTE.15.0',
    'TcXaeShell.DTE.14.0',
    'VisualStudio.DTE.17.0',
    'VisualStudio.DTE.15.0'
)
$activeHits = @()
foreach ($progId in $progIds) {
    try {
        $dte = [System.Runtime.InteropServices.Marshal]::GetActiveObject($progId)
        $solution = ''
        $name = ''
        try { $solution = [string]$dte.Solution.FullName } catch { }
        try { $name = [string]$dte.Name } catch { }
        $activeHits += $progId
        Write-ProbeLine 'GetActiveObject' 'PASS' ("$progId; name=$name; solution=$solution")
    } catch {
        Write-ProbeLine 'GetActiveObject' 'INFO' ("$progId; unavailable")
    }
}

$exitCode = 0
if (-not $addTypeOk -or $ExecutionContext.SessionState.LanguageMode -ne 'FullLanguage') {
    $verdict = 'TCCOM ROT HELPER IS BLOCKED OR RESTRICTED.'
    $exitCode = 2
} elseif ($xae.Count -eq 0) {
    $verdict = 'TCCOM PROBE PASSED, BUT XAE IS NOT RUNNING. Open XAE and run this script again.'
    $exitCode = 3
} elseif ($rotMatches.Count -eq 0 -and $activeHits.Count -eq 0) {
    $verdict = 'XAE IS RUNNING BUT ITS DTE IS NOT VISIBLE. Check privilege mismatch or XAE DTE registration.'
    $exitCode = 4
} else {
    $verdict = 'TCCOM IS AVAILABLE AND XAE DTE IS VISIBLE.'
}

Write-Host ''
Write-Host "VERDICT: $verdict" -ForegroundColor $(if ($exitCode -eq 0) { 'Green' } else { 'Yellow' })
$script:Report.Add('')
$script:Report.Add("VERDICT: $verdict")

$reportDir = $PSScriptRoot
try {
    $probe = Join-Path $reportDir '.tccom-write-test.tmp'
    [IO.File]::WriteAllText($probe, '')
    [IO.File]::Delete($probe)
} catch {
    $reportDir = $env:TEMP
}
$reportPath = Join-Path $reportDir ("TcCom-Diagnostic-{0}.txt" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
[IO.File]::WriteAllLines($reportPath, $script:Report.ToArray(), [Text.Encoding]::UTF8)
Write-Host "Report: $reportPath" -ForegroundColor Cyan

if ($Pause) { [void](Read-Host 'Press Enter to close') }
exit $exitCode
