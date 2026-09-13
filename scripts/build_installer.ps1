# ============================================================================
# 构建 TwinCAT Agent 客户一键安装程序
#
# 产物：
#   dist\TwinCAT-Agent-Setup-v<version>.exe
#   dist\TwinCAT-Agent-Installer\README.txt
#
# 安装器内嵌完整便携包（含 Python、XAE 扩展和倍福文档索引），目标电脑只需
# 双击一个 EXE。安装主体位于 LocalAppData，只有复制 XAE 扩展时请求一次 UAC。
# ============================================================================
param(
    [switch]$NoDocs,
    [string]$Version
)

$ErrorActionPreference = 'Stop'
$Repo = Split-Path $PSScriptRoot -Parent
$Py = "C:\Users\Aurora Home Office\AppData\Local\Programs\Python\Python314\python.exe"
$InstallerSrc = Join-Path $Repo "packaging\installer"
$Assets = Join-Path $Repo "packaging\assets"
$Build = Join-Path $Repo "build\installer"
$Out = Join-Path $Repo "dist\TwinCAT-Agent-Installer"
$Helpers = Join-Path $Build "helpers"
$PortableDir = Join-Path $Repo "dist\TwinCAT-Agent-Portable"
$Icon = Join-Path $Assets "twincat-agent.ico"
$Logo = Join-Path $Assets "twincat-agent-logo-256.png"
$Agreement = Join-Path $InstallerSrc "AGREEMENT.txt"
$VersionFile = Join-Path $Repo "VERSION"
$VersionInfoTemplate = Join-Path $InstallerSrc "version_info.txt"

function Assert-RepoPath([string]$Path) {
    $repoFull = [IO.Path]::GetFullPath($Repo).TrimEnd('\') + '\'
    $pathFull = [IO.Path]::GetFullPath($Path)
    if (-not $pathFull.StartsWith($repoFull, [StringComparison]::OrdinalIgnoreCase)) {
        throw "构建路径越界：$pathFull"
    }
}

function Get-Sha256([string]$Path) {
    $stream = [IO.File]::OpenRead($Path)
    try {
        $sha = [Security.Cryptography.SHA256]::Create()
        try { return ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-', '') }
        finally { $sha.Dispose() }
    } finally { $stream.Dispose() }
}

if (-not (Test-Path $Py)) { throw "找不到打包 Python：$Py" }
foreach ($required in @($Icon, $Logo, $Agreement, $VersionFile, $VersionInfoTemplate)) {
    if (-not (Test-Path $required)) { throw "缺少安装器资源：$required" }
}

$AppVersion = if ($Version) { $Version.Trim() } else {
    (Get-Content -LiteralPath $VersionFile -Raw -Encoding UTF8).Trim()
}
if ($AppVersion -notmatch '^\d+\.\d+\.\d+(?:\.\d+)?$') {
    throw "版本号格式无效：'$AppVersion'（应为 1.2.3 或 1.2.3.4）"
}
$versionParts = @($AppVersion.Split('.') | ForEach-Object { [int]$_ })
while ($versionParts.Count -lt 4) { $versionParts += 0 }
$VersionTuple = $versionParts -join ', '
$FinalName = "TwinCAT-Agent-Setup-v$AppVersion.exe"
$GeneratedVersionInfo = Join-Path $Build "version_info.generated.txt"
Assert-RepoPath $Build
Assert-RepoPath $Out
if (Test-Path $Build) { Remove-Item -LiteralPath $Build -Recurse -Force }
if (Test-Path $Out) { Remove-Item -LiteralPath $Out -Recurse -Force }
New-Item -ItemType Directory -Force $Build, $Out, $Helpers | Out-Null

$versionText = Get-Content -LiteralPath $VersionInfoTemplate -Raw -Encoding UTF8
$versionText = $versionText.Replace('__VERSION_TUPLE__', $VersionTuple)
$versionText = $versionText.Replace('__VERSION__', $AppVersion)
$versionText = $versionText.Replace('__SETUP_FILENAME__', $FinalName)
[IO.File]::WriteAllText($GeneratedVersionInfo, $versionText, [Text.UTF8Encoding]::new($false))

Write-Host "1/4 构建最新客户程序包..." -ForegroundColor Cyan
$portableArgs = @{ Zip = $false; PythonExe = $Py }
if ($NoDocs) { $portableArgs.NoDocs = $true }
& (Join-Path $PSScriptRoot "build_portable.ps1") @portableArgs
if ($LASTEXITCODE -ne 0) { throw "客户程序包构建失败 ($LASTEXITCODE)" }

Write-Host "2/4 构建带图标的启动器与卸载器..." -ForegroundColor Cyan
& $Py -c "import pystray, PIL"
if ($LASTEXITCODE -ne 0) {
    throw "缺少托盘构建依赖：请先运行 '$Py -m pip install pystray Pillow'"
}
foreach ($helper in @(
    @{Name="TwinCAT-Agent"; Source="launcher.py"},
    @{Name="TwinCAT-Agent-Uninstall"; Source="uninstaller.py"}
)) {
    $collectArgs = @()
    if ($helper.Name -eq "TwinCAT-Agent") {
        $collectArgs = @("--collect-all", "pystray", "--collect-all", "PIL")
    }
    & $Py -m PyInstaller `
        --noconfirm --clean --onefile --windowed `
        --name $helper.Name `
        --icon $Icon `
        --hidden-import win32com.client `
        --hidden-import pythoncom `
        --hidden-import pywintypes `
        @collectArgs `
        --paths $InstallerSrc `
        --distpath $Helpers `
        --workpath (Join-Path $Build "$($helper.Name)-work") `
        --specpath (Join-Path $Build "spec") `
        (Join-Path $InstallerSrc $helper.Source)
    if ($LASTEXITCODE -ne 0) { throw "$($helper.Name) 构建失败 ($LASTEXITCODE)" }
}

Write-Host "3/4 构建单文件 Setup.exe（内嵌完整程序，稍等）..." -ForegroundColor Cyan
& $Py -m PyInstaller `
    --noconfirm --clean --onefile --windowed `
    --name "TwinCAT-Agent-Setup" `
    --icon $Icon `
    --version-file $GeneratedVersionInfo `
    --hidden-import win32com.client `
    --hidden-import pythoncom `
    --hidden-import pywintypes `
    --paths $InstallerSrc `
    --add-data "$PortableDir;payload/TwinCAT-Agent-Portable" `
    --add-data "$(Join-Path $Helpers 'TwinCAT-Agent.exe');payload" `
    --add-data "$(Join-Path $Helpers 'TwinCAT-Agent-Uninstall.exe');payload" `
    --add-data "$Icon;assets" `
    --add-data "$Logo;assets" `
    --add-data "$Agreement;assets" `
    --distpath $Out `
    --workpath (Join-Path $Build "setup-work") `
    --specpath (Join-Path $Build "spec") `
    (Join-Path $InstallerSrc "setup.py")
if ($LASTEXITCODE -ne 0) { throw "安装器构建失败 ($LASTEXITCODE)" }

Write-Host "4/4 安全检查与整理产物..." -ForegroundColor Cyan
$setup = Join-Path $Out "TwinCAT-Agent-Setup.exe"
if (-not (Test-Path $setup)) { throw "未生成 $setup" }
Copy-Item (Join-Path $InstallerSrc "README.txt") (Join-Path $Out "README.txt") -Force
$finalSetup = Join-Path $Repo "dist\$FinalName"

# dist 只保留最新的正式安装包，避免多个无版本/旧版本 EXE 混在一起。
$distDir = Join-Path $Repo "dist"
Get-ChildItem -LiteralPath $distDir -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -eq 'TwinCAT-Agent-Setup.exe' -or
                   $_.Name -like 'TwinCAT-Agent-Setup-v*.exe' } |
    ForEach-Object {
        Assert-RepoPath $_.FullName
        try {
            Remove-Item -LiteralPath $_.FullName -Force -ErrorAction Stop
        } catch {
            Write-Host "  ! 旧安装包正被占用，暂时保留：$($_.Name)" -ForegroundColor Yellow
        }
    }
Copy-Item $setup $finalSetup -Force

# 输入目录与输出目录都不能混入私钥/API 配置。
$leaks = @(Get-ChildItem $PortableDir -Recurse -File | Where-Object {
    $_.FullName -match '(?i)(private.*key|secret|\.pem$|config\.json$|chat_history\.json$|agent\.db(?:-.+)?$|__pycache__|\.ps1$)' -or
    $_.FullName -match '(?i)\\app\\(?:tc_agent|tc_template)\\.*\.py$'
})
if ($leaks.Count) { throw "客户程序包含敏感文件：$($leaks.FullName -join ', ')" }
$looseLeaks = Get-ChildItem $Out -Recurse -File | Where-Object {
    $_.Name -match '(?i)(private.*key|secret|\.pem$|config\.json$|chat_history\.json$|agent\.db(?:-.+)?$)'
}
if ($looseLeaks) { throw "安装器目录含敏感文件：$($looseLeaks.FullName -join ', ')" }

$size = "{0:N1} MB" -f ((Get-Item $finalSetup).Length / 1MB)
$hash = Get-Sha256 $finalSetup
$hashFile = "$finalSetup.sha256"
[IO.File]::WriteAllText($hashFile, "$hash  $([IO.Path]::GetFileName($finalSetup))`r`n", [Text.UTF8Encoding]::new($false))

# Setup 已直接内嵌便携目录、启动器和卸载器，下面这些只用于本次构建。
# 交付目录默认只留最终 Setup（以及单独构建的内部授权工具）。
foreach ($temporaryOutput in @($PortableDir, $Out)) {
    if (-not (Test-Path -LiteralPath $temporaryOutput)) { continue }
    Assert-RepoPath $temporaryOutput
    Remove-Item -LiteralPath $temporaryOutput -Recurse -Force
}

Write-Host "✓ 一键安装程序：$finalSetup ($size)" -ForegroundColor Green
Write-Host "  Version: $AppVersion" -ForegroundColor DarkGray
Write-Host "  SHA256: $hash" -ForegroundColor DarkGray
Write-Host "  Hash file: $hashFile" -ForegroundColor DarkGray
