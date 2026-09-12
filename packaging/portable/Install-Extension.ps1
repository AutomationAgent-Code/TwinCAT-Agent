# ============================================================================
#  TwinCAT Agent - 安装 XAE 扩展（把面板装进 TcXaeShell）
#
#  作用：把 extension\TwinCAT Agent\ 复制到 TcXaeShell 的机器级扩展目录，
#        再用同一个管理员流程执行 TcXaeShell.exe /setup 重建包和菜单缓存。
#        目标目录：<TcXaeShell>\Common7\IDE\Extensions\TwinCAT Agent\
#  需要管理员（写 Program Files）——本脚本会自动请求提权。
#
#  用法：右键“用 PowerShell 运行”，或在管理员 PowerShell 里：
#        powershell -ExecutionPolicy Bypass -File .\Install-Extension.ps1
#        可选：-ShellPath "D:\...\Beckhoff\TcXaeShell" 手动指定 TcXaeShell 路径
# ============================================================================
param(
    [string]$ShellPath = "",
    [switch]$NoSelfElevate,
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'

# --- 自动提权（复制到 Program Files 需要管理员）---
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    if ($NoSelfElevate) { throw "安装 XAE 扩展需要管理员权限" }
    Write-Host "需要管理员权限，正在请求提权..." -ForegroundColor Yellow
    $argList = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    if ($ShellPath) { $argList += " -ShellPath `"$ShellPath`"" }
    Start-Process powershell -Verb RunAs -ArgumentList $argList
    exit
}

$Root = $PSScriptRoot
$Src  = Join-Path $Root "extension\TwinCAT Agent"
if (-not (Test-Path $Src)) { throw "找不到扩展源目录: $Src （包不完整？请重新整包拷贝）" }

# --- 升级时停止占用 8765 的旧 TwinCAT Agent 后端 ---
# 否则新版启动器会因端口已占用而退出，导致新 UI 一直等不到 license_status。
$listeners = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
foreach ($listener in $listeners) {
    $pidToCheck = $listener.OwningProcess
    $procInfo = Get-CimInstance Win32_Process -Filter "ProcessId=$pidToCheck" -ErrorAction SilentlyContinue
    if ($procInfo -and $procInfo.Name -match '^python(w)?\.exe$' -and
        $procInfo.CommandLine -match 'tc_agent\.backend') {
        Write-Host "停止旧版 TwinCAT Agent 后端 (PID $pidToCheck)..." -ForegroundColor DarkGray
        Stop-Process -Id $pidToCheck -Force -ErrorAction SilentlyContinue
    } else {
        Write-Host "警告：端口 8765 被其他程序占用 (PID $pidToCheck)，未自动停止。" -ForegroundColor Yellow
    }
}

# --- 定位 TcXaeShell ---
function Find-Shell {
    param([string]$Override)
    if ($Override) {
        if (Test-Path (Join-Path $Override "Common7\IDE")) { return $Override }
        throw "指定的 -ShellPath 下没有 Common7\IDE: $Override"
    }
    $cands = @(
        "${env:ProgramFiles(x86)}\Beckhoff\TcXaeShell",
        "${env:ProgramFiles}\Beckhoff\TcXaeShell",
        $(if ($env:TWINCAT3DIR) { Join-Path $env:TWINCAT3DIR 'Components\Base\TcXaeShell' }),
        'C:\TwinCAT\3.1\Components\Base\TcXaeShell',
        $(if ($env:TWINCAT3DIR) { Join-Path $env:TWINCAT3DIR 'TcXaeShell' }),
        'C:\TwinCAT\3.1\TcXaeShell'
    )
    foreach ($c in @($cands | Where-Object { $_ } | Select-Object -Unique)) {
        if ($c -and (Test-Path (Join-Path $c "Common7\IDE\TcXaeShell.exe"))) { return $c }
    }
    throw "没找到 32 位 TcXaeShell。请确认目标机已安装 TwinCAT XAE Shell (x86)，或用 -ShellPath 指定其安装目录。"
}

$Shell   = Find-Shell -Override $ShellPath
$ShellExe = Join-Path $Shell "Common7\IDE\TcXaeShell.exe"
if (Get-Process -Name TcXaeShell -ErrorAction SilentlyContinue) {
    throw "请先完全关闭 TwinCAT XAE，再安装或更新 TwinCAT Agent 扩展。"
}
$ExtRoot = Join-Path $Shell "Common7\IDE\Extensions"
$Dest    = Join-Path $ExtRoot "TwinCAT Agent"
Write-Host "TcXaeShell:  $Shell" -ForegroundColor Cyan
Write-Host "扩展目标:    $Dest" -ForegroundColor Cyan

# --- 拷贝（只清理本项目已知的旧重命名残留，不删除整个目录）---
if (Test-Path $Dest) {
    foreach ($legacyName in @("TcCoAgent.dll", "TcCoAgent.pkgdef", "TwinCATAgent.dll", "TwinCATAgent.pkgdef", "tcxaeshell")) {
        $legacyPath = Join-Path $Dest $legacyName
        if (Test-Path $legacyPath) {
            Write-Host "移除本项目旧残留: $legacyName" -ForegroundColor DarkGray
            Remove-Item $legacyPath -Recurse -Force
        }
    }
}
New-Item -ItemType Directory -Force $Dest | Out-Null
foreach ($requiredName in @("extension.vsixmanifest", "TwinCATAgent.Xae.dll", "TwinCATAgent.Xae.pkgdef")) {
    if (-not (Test-Path (Join-Path $Src $requiredName) -PathType Leaf)) {
        throw "扩展源缺少 TwinCATAgent.Xae 产物: $requiredName"
    }
}
Copy-Item (Join-Path $Src "*") $Dest -Recurse -Force
$n = (Get-ChildItem $Dest -Recurse -File | Measure-Object).Count
Write-Host "✓ 已复制 $n 个文件到扩展目录。" -ForegroundColor Green

# TcXaeShell 是独立 VS Shell，不能用传统 VSIX 注册工具或 devenv /setup 注册。
# /setup 必须在本次管理员流程内运行，否则会因 configurationchanged 无权限失败。
Write-Host "正在由 TcXaeShell 重建扩展缓存..." -ForegroundColor DarkGray
$setupProcess = Start-Process -FilePath $ShellExe -ArgumentList "/setup" -Wait -PassThru -WindowStyle Hidden
if ($setupProcess.ExitCode -ne 0) {
    throw "TcXaeShell 扩展缓存重建失败（退出码 $($setupProcess.ExitCode)）"
}
Write-Host "✓ XAE 扩展缓存已重建。" -ForegroundColor Green

if (-not $Quiet) {
    Write-Host ""
    Write-Host "===== 安装完成，接下来两步 =====" -ForegroundColor Green
    Write-Host "1) 非管理员双击运行  Start-Backend.vbs  启动后端（千万别用管理员）。"
    Write-Host "2) 完全关闭并重新打开 TcXaeShell —— 缓存已刷新，Shell 会加载该扩展；"
    Write-Host "   然后从菜单打开『TwinCAT Agent』面板即可。"
    Write-Host ""
    Write-Host "若面板打不开：确认后端在跑（浏览器开 http://127.0.0.1:8766 应能看到界面），"
    Write-Host "并确认后端是【非管理员】启动的。"
}
