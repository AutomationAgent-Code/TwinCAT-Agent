# ============================================================================
#  TwinCAT Agent - 卸载 XAE 扩展 + 停止后端
#  自动提权（删除 Program Files 下的扩展需要管理员）。
# ============================================================================
param(
    [string]$ShellPath = "",
    [switch]$NoSelfElevate,
    [switch]$Quiet
)

$ErrorActionPreference = 'Continue'

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    if ($NoSelfElevate) { throw "卸载 XAE 扩展需要管理员权限" }
    Write-Host "需要管理员权限，正在请求提权..." -ForegroundColor Yellow
    $argList = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    if ($ShellPath) { $argList += " -ShellPath `"$ShellPath`"" }
    Start-Process powershell -Verb RunAs -ArgumentList $argList
    exit
}

# 停止后端（占用 8765 的 python 进程）
try {
    $c = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
    if ($c) { Stop-Process -Id $c.OwningProcess -Force; Write-Host "已停止后端 (PID $($c.OwningProcess))。" -ForegroundColor Green }
    else { Write-Host "后端未在运行。" -ForegroundColor DarkGray }
} catch { Write-Host "停止后端时出错（可忽略）: $_" -ForegroundColor DarkYellow }

# 删除扩展
$cands = @()
if ($ShellPath) { $cands += $ShellPath }
$cands += @(
    "${env:ProgramFiles(x86)}\Beckhoff\TcXaeShell",
    "${env:ProgramFiles}\Beckhoff\TcXaeShell",
    $(if ($env:TWINCAT3DIR) { Join-Path $env:TWINCAT3DIR 'Components\Base\TcXaeShell' }),
    'C:\TwinCAT\3.1\Components\Base\TcXaeShell',
    $(if ($env:TWINCAT3DIR) { Join-Path $env:TWINCAT3DIR 'TcXaeShell' }),
    'C:\TwinCAT\3.1\TcXaeShell'
)
$removed = $false
foreach ($c in @($cands | Where-Object { $_ } | Select-Object -Unique)) {
    if (-not (Test-Path (Join-Path $c 'Common7\IDE\TcXaeShell.exe') -PathType Leaf)) { continue }
    $dest = Join-Path $c "Common7\IDE\Extensions\TwinCAT Agent"
    if (Test-Path $dest) {
        Remove-Item $dest -Recurse -Force
        Write-Host "✓ 已删除扩展: $dest" -ForegroundColor Green
        $removed = $true
    }
}
if (-not $removed) { Write-Host "未找到已安装的扩展（可能未装或路径不同）。" -ForegroundColor DarkGray }
if (-not $Quiet) { Write-Host "重启 TcXaeShell 使卸载生效。" }
