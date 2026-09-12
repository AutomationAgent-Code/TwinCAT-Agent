# ============================================================================
# 构建供应商专用的 TwinCAT Agent 授权工具（不包含私钥）
#
# 产物：
#   dist\TwinCAT-Agent-License-Issuer\
#   dist\TwinCAT-Agent-License-Issuer.zip
# ============================================================================
param(
    [switch]$NoZip,
    [string]$OutputName = "TwinCAT-Agent-License-Issuer",
    [string]$ZipName = ""
)

$ErrorActionPreference = 'Stop'
$Repo = Split-Path $PSScriptRoot -Parent
$Py = "C:\Users\Aurora Home Office\AppData\Local\Programs\Python\Python314\python.exe"
if ($OutputName -notmatch '^[A-Za-z0-9._-]+$') { throw "OutputName 只能包含字母、数字、点、下划线和连字符" }
if ($ZipName -and $ZipName -notmatch '^[A-Za-z0-9._-]+\.zip$') { throw "ZipName 必须是安全的 .zip 文件名" }
$Out = Join-Path $Repo "dist\$OutputName"
$Work = Join-Path $Repo "build\$OutputName"
$Readme = Join-Path $Repo "packaging\license-tool\README.txt"

if (-not (Test-Path $Py)) { throw "找不到打包 Python: $Py" }
if (Test-Path $Out) {
    $outPrefix = [IO.Path]::GetFullPath($Out).TrimEnd('\') + '\'
    $runningFromOut = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $_.ExecutablePath -and
            [IO.Path]::GetFullPath($_.ExecutablePath).StartsWith(
                $outPrefix, [StringComparison]::OrdinalIgnoreCase
            )
        }
    if ($runningFromOut) {
        throw "旧版授权工具仍在运行，请关闭窗口后重新构建（PID: $($runningFromOut.ProcessId -join ', ')）"
    }
}
if (Test-Path $Out) { Remove-Item -LiteralPath $Out -Recurse -Force }
if (Test-Path $Work) { Remove-Item -LiteralPath $Work -Recurse -Force }
New-Item -ItemType Directory -Force $Out, $Work | Out-Null

Write-Host "构建授权工具 EXE..." -ForegroundColor Cyan
& $Py -m PyInstaller `
    --noconfirm --clean --onedir --windowed `
    --name "TwinCAT-Agent-License-Issuer" `
    --distpath $Out `
    --workpath (Join-Path $Work "work") `
    --specpath (Join-Path $Work "spec") `
    (Join-Path $Repo "scripts\license_issuer_ui.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 构建失败 ($LASTEXITCODE)" }

$AppDir = Join-Path $Out "TwinCAT-Agent-License-Issuer"
Copy-Item $Readme (Join-Path $AppDir "README.txt") -Force

# 安全闸：供应商包中也不能意外混入私钥。
$secret = Get-ChildItem $Out -Recurse -File |
    Where-Object { $_.Name -match '(?i)private.*key|secret|\.pem$' }
if ($secret) {
    throw "检测到疑似私钥文件，拒绝打包: $($secret.FullName -join ', ')"
}

# 源码级兜底：EXE 之外不应出现发码源码或 Python 脚本。
$looseSource = Get-ChildItem $AppDir -Recurse -File -Include *.py,*.pyc
if ($looseSource) { throw "检测到散落 Python 源码，拒绝交付" }

$size = "{0:N1} MB" -f ((Get-ChildItem $AppDir -Recurse -File |
    Measure-Object Length -Sum).Sum / 1MB)
Write-Host "✓ 授权工具: $AppDir ($size)" -ForegroundColor Green

if (-not $NoZip) {
    $Zip = if ($ZipName) {
        Join-Path $Repo "dist\$ZipName"
    } else {
        "$Out.zip"
    }
    if (Test-Path $Zip) { Remove-Item -LiteralPath $Zip -Force }
    Compress-Archive -Path (Join-Path $AppDir "*") -DestinationPath $Zip -CompressionLevel Optimal
    Write-Host "✓ ZIP: $Zip" -ForegroundColor Green
}
