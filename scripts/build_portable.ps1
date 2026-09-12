# ============================================================================
#  build_portable.ps1 —— 打包 TwinCAT Agent 便携测试包（自带 Python，免安装）
#
#  产物: dist\TwinCAT-Agent-Portable\   （-Zip 时另出同名 .zip）
#  结构:
#    runtime\python\      便携 Python 3.14 + 后端、附件和 HMI Schema 校验依赖
#    app\tc_agent\        后端（含代理直连修复 + static/index.html）
#    app\tc_template\     COM 桥 _ps_bridge + plc + TcCom.ps1
#    app\data\ba-docs\    倍福文档索引 index.db（-NoDocs 可跳过）
#    extension\TwinCAT Agent\   XAE 扩展（DLL/清单/pkgdef/webview）
#    Start-Backend.vbs/.cmd, Install-Extension.ps1, Uninstall.ps1, README.txt
#
#  用法(普通 PowerShell): & "G:\claude\twin-cat-agent\scripts\build_portable.ps1"
#    -NoDocs  不打入 374MB 文档索引（包更小，docs_search 到目标机再补 index.db）
#    -Zip     额外打一个 .zip 方便拷贝
# ============================================================================
param(
    [switch]$NoDocs,
    [switch]$Zip,
    [string]$PythonExe = ""
)

$ErrorActionPreference = 'Stop'
$Repo    = Split-Path $PSScriptRoot -Parent
$Dist    = Join-Path $Repo "dist"
$Cache   = Join-Path $Repo "build\portable-cache"
$Pkg     = Join-Path $Dist "TwinCAT-Agent-Portable"
$Tpl     = Join-Path $Repo "packaging\portable"
$PyEmbed = Join-Path $Cache "python-3.14.0-embed-amd64.zip"
$PyUrl   = "https://www.python.org/ftp/python/3.14.0/python-3.14.0-embed-amd64.zip"
$ComPyEmbed = Join-Path $Cache "python-3.12.10-embed-win32.zip"
$ComPyUrl   = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-win32.zip"
$ComWheel   = Join-Path $Cache "pywin32-312-cp312-cp312-win32.whl"
$AgentVersion = (Get-Content (Join-Path $Repo 'VERSION') -Raw).Trim()

function Say($m,$c='Cyan'){ Write-Host $m -ForegroundColor $c }

if (-not $PythonExe) {
    $PythonExe = (Get-Command python -ErrorAction Stop).Source
}
if (-not (Test-Path $PythonExe)) {
    throw "找不到用于准备便携包依赖的 Python: $PythonExe"
}

# The customer backend is Python 3.14 amd64 while the COM helper remains
# Python 3.12 win32.  Do not copy native extensions (rpds/pywin32) from the
# helper interpreter into the main runtime; resolve a matching 3.14 host for
# the main runtime's architecture-specific packages.
$MainPythonExe = $null
$pyLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
if ($pyLauncher) {
    $candidate = (& $pyLauncher.Source '-3.14' '-c' 'import sys; print(sys.executable)' 2>$null | Select-Object -Last 1)
    if ($candidate) { $MainPythonExe = ([string]$candidate).Trim() }
}
if (-not $MainPythonExe -or -not (Test-Path -LiteralPath $MainPythonExe)) {
    throw "找不到与便携主运行时匹配的 Python 3.14；请先安装 Python 3.14（不要用 3.12 的原生扩展替代）。"
}

New-Item -ItemType Directory -Force $Cache | Out-Null

# --- 0) 拿到便携 Python（缓存复用）---
if (-not (Test-Path $PyEmbed)) {
    Say "下载 embeddable Python 3.14.0..."
    $ProgressPreference = 'SilentlyContinue'
    Invoke-WebRequest -Uri $PyUrl -OutFile $PyEmbed -TimeoutSec 180
}
if (-not (Test-Path $ComPyEmbed)) {
    Say "下载 32 位 COM helper Python 3.12.10..."
    $ProgressPreference = 'SilentlyContinue'
    Invoke-WebRequest -Uri $ComPyUrl -OutFile $ComPyEmbed -TimeoutSec 180
}
if (-not (Test-Path $ComWheel)) {
    Say "下载 32 位 pywin32 COM 组件..."
    & $PythonExe -m pip download pywin32 --only-binary=:all: --platform win32 `
        --python-version 3.12 --implementation cp --abi cp312 -d $Cache
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $ComWheel)) {
        throw "下载 32 位 pywin32 失败"
    }
}

# --- 1) 清理并重建包目录（下载缓存位于 build，不污染交付目录）---
if (Test-Path $Pkg) { Remove-Item $Pkg -Recurse -Force }
New-Item -ItemType Directory -Force $Pkg | Out-Null

# --- 2) 展开 Python + 配置 ._pth（把 app 加入 sys.path、开启 site）---
$PyDir = Join-Path $Pkg "runtime\python"
New-Item -ItemType Directory -Force $PyDir | Out-Null
Say "展开便携 Python..."
Expand-Archive -Path $PyEmbed -DestinationPath $PyDir -Force
$zipName = (Get-ChildItem $PyDir -Filter "python*.zip" | Select-Object -First 1).Name
$pth     = (Get-ChildItem $PyDir -Filter "python*._pth" | Select-Object -First 1).FullName
@"
$zipName
.
Lib\site-packages
Lib\site-packages\win32
Lib\site-packages\win32\lib
Lib\site-packages\pythonwin
..\..\app
import site
"@ | Set-Content -Path $pth -Encoding Ascii
Say "  ._pth -> 已加入 ..\..\app 与 site-packages"

# --- 3) 把第三方依赖塞进 site-packages（全是纯 Python，不破坏便携）---
#   websockets = WS 服务;pypdf/openpyxl(+et_xmlfile) = 附件抽文字(PDF/Excel);
#   pyads = 部署后读取系统和每个 PLC Runtime 的真实 ADS 状态;
#   pywin32 = 无 PowerShell 的 TwinCAT COM/ROT 原生桥。
$sp = Join-Path $PyDir "Lib\site-packages"
New-Item -ItemType Directory -Force $sp | Out-Null
$DevPy = $PythonExe
foreach ($mod in @("websockets", "pypdf", "openpyxl", "et_xmlfile", "pyads", "yaml",
                   "jsonschema", "jsonschema_specifications", "referencing", "rpds", "attr", "attrs")) {
    # find_spec avoids importing pyads during packaging; importing it requires
    # TcAdsDll.dll, which is resolved from the target TwinCAT installation.
    $modulePython = if ($mod -in @("jsonschema", "jsonschema_specifications", "referencing", "rpds")) { $MainPythonExe } else { $DevPy }
    $srcDir = & $modulePython -c "import importlib.util,os;s=importlib.util.find_spec('$mod');print(os.path.dirname(s.origin) if s and s.origin else '')"
    if (-not (Test-Path $srcDir)) { throw "找不到依赖 $mod 源: $srcDir（先 pip install $mod）" }
    $rc = robocopy $srcDir (Join-Path $sp $mod) /E /XD __pycache__ /XF "*.pyc" /NFL /NDL /NJH /NJS /NP
    if ($LASTEXITCODE -ge 8) { throw "复制 $mod 失败 (robocopy $LASTEXITCODE)" }
}
Say "  websockets + pypdf + openpyxl + et_xmlfile + pyads + yaml -> 已内置"
$typingSource = & $DevPy -c "import importlib.util;print(importlib.util.find_spec('typing_extensions').origin)"
Copy-Item -LiteralPath $typingSource -Destination (Join-Path $sp 'typing_extensions.py') -Force
# rpds contains an interpreter/architecture-specific extension. Verify the
# copied dependencies using the actual packaged interpreter, not the dev one.
& (Join-Path $PyDir 'python.exe') -c "from jsonschema import Draft4Validator;from referencing import Registry;Draft4Validator({'type':'string'}).validate('ready')"
if ($LASTEXITCODE -ne 0) { throw 'Packaged HMI Schema dependencies do not match the bundled interpreter.' }

$devSp = & $DevPy -c "import site;print(site.getsitepackages()[-1])"
$mainSp = & $MainPythonExe -c "import site;print(site.getsitepackages()[-1])"
foreach ($mod in @("win32", "win32com", "win32comext", "pythonwin", "pywin32_system32")) {
    $srcDir = Join-Path $mainSp $mod
    if (-not (Test-Path $srcDir)) { throw "找不到 pywin32 组件: $srcDir" }
    $rc = robocopy $srcDir (Join-Path $sp $mod) /E /XD __pycache__ /XF "*.pyc" /NFL /NDL /NJH /NJS /NP
    if ($LASTEXITCODE -ge 8) { throw "复制 pywin32/$mod 失败 (robocopy $LASTEXITCODE)" }
}
foreach ($file in @("pythoncom.py", "pywin32.pth", "pywin32.version.txt")) {
    $src = Join-Path $mainSp $file
    if (Test-Path $src) { Copy-Item $src (Join-Path $sp $file) -Force }
}
Say "  pywin32 (COM + ROT) -> 已内置，客户机无需 PowerShell"

# --- 3b) 32 位原生 COM helper（TcXaeShell 15 本身是 32 位）---
# 主后端保持 64 位；所有 XAE Automation Interface 调用通过 JSON stdin/stdout
# 交给此同位数 helper，避免 64 位进程看不到 32 位 XAE 的 ROT。
$ComPyDir = Join-Path $Pkg "runtime\com32"
New-Item -ItemType Directory -Force $ComPyDir | Out-Null
Expand-Archive -Path $ComPyEmbed -DestinationPath $ComPyDir -Force
$comZipName = (Get-ChildItem $ComPyDir -Filter "python*.zip" | Select-Object -First 1).Name
$comPth = (Get-ChildItem $ComPyDir -Filter "python*._pth" | Select-Object -First 1).FullName
@"
$comZipName
.
Lib\site-packages
Lib\site-packages\win32
Lib\site-packages\win32\lib
Lib\site-packages\pythonwin
app
..\..\app
import site
"@ | Set-Content -Path $comPth -Encoding Ascii
$comSp = Join-Path $ComPyDir "Lib\site-packages"
New-Item -ItemType Directory -Force $comSp | Out-Null
$wheelZip = Join-Path $Cache "pywin32-312-win32.zip"
Copy-Item $ComWheel $wheelZip -Force
try {
    Expand-Archive -Path $wheelZip -DestinationPath $comSp -Force
} finally {
    Remove-Item $wheelZip -Force -ErrorAction SilentlyContinue
}
Copy-Item (Join-Path $comSp "pywin32_system32\pythoncom312.dll") $ComPyDir -Force
Copy-Item (Join-Path $comSp "pywin32_system32\pywintypes312.dll") $ComPyDir -Force
$pyadsSource = (& $DevPy -c "import importlib.util,os;s=importlib.util.find_spec('pyads');print(os.path.dirname(s.origin) if s and s.origin else '')" | Select-Object -Last 1)
$pyadsSource = ([string]$pyadsSource).Trim()
if (-not $pyadsSource -or -not (Test-Path -LiteralPath $pyadsSource)) { throw "找不到 32 位 helper 所需的 pyads 源包: $pyadsSource" }
$rc = robocopy $pyadsSource (Join-Path $comSp "pyads") /E /XD __pycache__ /XF "*.pyc" /NFL /NDL /NJH /NJS /NP
if ($LASTEXITCODE -ge 8) { throw "复制 pyads 到 32 位 helper 失败 (robocopy $LASTEXITCODE)" }
Say "  runtime\com32 -> 已内置 32 位原生 COM helper + pyads（无 PowerShell）"

# --- 4) app\：tc_agent + tc_template（排除密钥/缓存/历史/备份）---
& (Join-Path $PSScriptRoot 'build_ads_dynamic_bridge.ps1') -Quiet
if ($LASTEXITCODE -ne 0) { throw '构建动态 ADS 桥失败' }
$App = Join-Path $Pkg "app"
$r1 = robocopy (Join-Path $Repo "tc_agent") (Join-Path $App "tc_agent") /E `
        /XD __pycache__ /XF config.json chat_history.json agent.db "agent.db-*" "*.bak" "*.pyc" "*.log" "*.out" `
        /NFL /NDL /NJH /NJS /NP
if ($LASTEXITCODE -ge 8) { throw "复制 tc_agent 失败 (robocopy $LASTEXITCODE)" }
$r2 = robocopy (Join-Path $Repo "tc_template") (Join-Path $App "tc_template") /E `
        /XD __pycache__ /XF "*.pyc" "*.ps1" `
        /NFL /NDL /NJH /NJS /NP
if ($LASTEXITCODE -ge 8) { throw "复制 tc_template 失败 (robocopy $LASTEXITCODE)" }

# The x86 COM worker uses Python 3.12 while the main Agent uses Python 3.14.
# Give it a private tc_template copy so each runtime can carry bytecode built
# for its own interpreter; sharing a single .pyc tree across both versions is
# invalid because Python bytecode magic numbers differ.
$ComApp = Join-Path $ComPyDir "app"
$rComApp = robocopy (Join-Path $Repo "tc_template") (Join-Path $ComApp "tc_template") /E `
           /XD __pycache__ /XF "*.pyc" "*.ps1" `
           /NFL /NDL /NJH /NJS /NP
if ($LASTEXITCODE -ge 8) { throw "复制 x86 COM worker 模块失败 (robocopy $LASTEXITCODE)" }
& $PythonExe (Join-Path $PSScriptRoot 'bundle_tool_scripts.py') $Repo (Join-Path $App 'tc_template') (Join-Path $ComApp 'tc_template')
if ($LASTEXITCODE -ne 0) { throw 'Failed to bundle fallback tool resources.' }
& (Join-Path $ComPyDir "python.exe") -c "from tc_template._native_worker import prepare_native_runtime; print('com32 ADS:', prepare_native_runtime()); import pyads; print('com32 pyads ok')"
if ($LASTEXITCODE -ne 0) { throw "32 位 COM helper 无法导入 pyads 或定位 x86 TcAdsDll.dll" }
& (Join-Path $PyDir "python.exe") -c "from tc_template._native_worker import prepare_native_runtime; print('com64 ADS:', prepare_native_runtime()); import pythoncom,win32com.client,pyads; print('com64 bridge ok')"
if ($LASTEXITCODE -ne 0) { throw "64 位 COM helper 无法导入 pywin32/pyads 或定位 x64 TcAdsDll.dll" }
Copy-Item (Join-Path $Repo "VERSION") (Join-Path $App "VERSION") -Force
Say "  app\tc_agent + app\tc_template -> 已复制（已排除密钥/历史/PowerShell 脚本）"

# Generation preparation and fblib tools require the same shipped template assets.
# Ship the blank solution template used before a system project exists.
$solutionTemplateSrc = Join-Path $Repo 'Repository\basic'
$solutionTemplateDst = Join-Path $App 'Repository\basic'
$solutionCopy = robocopy $solutionTemplateSrc $solutionTemplateDst '*.yaml' '*.json' '*.sln' '*.tsproj' /S /NFL /NDL /NJH /NJS /NP
if ($LASTEXITCODE -ge 8) { throw 'Failed to copy the basic solution template' }
# Copy only template formats, never repository caches, reports or configuration.
$fbTemplateSrc = Join-Path $Repo "fblib"
if (Test-Path $fbTemplateSrc) {
    $fbTemplateDst = Join-Path $App "fblib"
    $rc = robocopy $fbTemplateSrc $fbTemplateDst "*.yaml" "*.decl" "*.impl" "*.xml" /S /XD __pycache__ .git /NFL /NDL /NJH /NJS /NP
    if ($LASTEXITCODE -ge 8) { throw "Failed to copy generation template assets (robocopy $LASTEXITCODE)" }
}

# 可扩展客户案例库：内置案例位于 app\CaseLibrary，客户扩展放 ProgramData。
$caseSrc = Join-Path $Repo "CaseLibrary"
if (Test-Path $caseSrc) {
    $caseDst = Join-Path $App "CaseLibrary"
    $rc = robocopy $caseSrc $caseDst /E /XD __pycache__ /XF "*.pyc" `
          /NFL /NDL /NJH /NJS /NP
    if ($LASTEXITCODE -ge 8) { throw "复制 CaseLibrary 失败 (robocopy $LASTEXITCODE)" }
    Say "  app\CaseLibrary -> 已内置（客户扩展目录为 ProgramData\TwinCATAgent\CaseLibrary）"
}

# 双保险：确认没把密钥带进去
foreach ($leak in @(
    "app\tc_agent\config.json", "app\tc_agent\chat_history.json",
    "app\tc_agent\agent.db", "app\tc_agent\agent.db-wal", "app\tc_agent\agent.db-shm"
)) {
    $p = Join-Path $Pkg $leak
    if (Test-Path $p) { Remove-Item $p -Force; Say "  ! 清除了误入的 $leak" 'Yellow' }
}

# --- 5) 文档索引（374MB，可 -NoDocs 跳过）---
if (-not $NoDocs) {
    $dbSrc = Join-Path $Repo "data\ba-docs\index.db"
    if (Test-Path $dbSrc) {
        $dbDst = Join-Path $App "data\ba-docs"
        New-Item -ItemType Directory -Force $dbDst | Out-Null
        Say "复制文档索引 index.db (374MB，稍等)..."
        Copy-Item $dbSrc (Join-Path $dbDst "index.db") -Force
        Say "  data\ba-docs\index.db -> 已内置"
    } else { Say "  ! 未找到 $dbSrc，跳过文档索引" 'Yellow' }
} else { Say "  -NoDocs：不打入文档索引（目标机 docs_search 需另补 index.db）" 'Yellow' }

# --- 6) XAE 扩展 ---
$vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
$msbuild = $null
if (Test-Path $vswhere) {
    $msbuild = & $vswhere -latest -requires Microsoft.Component.MSBuild `
        -find "MSBuild\**\Bin\MSBuild.exe" | Select-Object -First 1
}
if (-not $msbuild) {
    $fallbacks = @(
        "C:\Program Files\Microsoft Visual Studio\2022\Community\MSBuild\Current\Bin\MSBuild.exe",
        "C:\Program Files\Microsoft Visual Studio\2022\Professional\MSBuild\Current\Bin\MSBuild.exe",
        "C:\Program Files\Microsoft Visual Studio\2022\Enterprise\MSBuild\Current\Bin\MSBuild.exe"
    )
    $msbuild = $fallbacks | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $msbuild) { throw "找不到 MSBuild，不能确保客户包包含最新 XAE 扩展" }
Say "构建最新 XAE 扩展..."
& $msbuild (Join-Path $Repo "tc_agent_vsix\TwinCATAgent.Xae.csproj") `
    /t:Build /p:Configuration=Release /p:Platform=AnyCPU /nologo /verbosity:minimal
if ($LASTEXITCODE -ne 0) { throw "XAE 扩展构建失败 (MSBuild $LASTEXITCODE)" }

$extSrc = Join-Path $Repo "tc_agent_vsix\deploy_stage\TwinCAT Agent"
if (-not (Test-Path $extSrc)) { throw "找不到扩展 staging: $extSrc" }
# deploy_stage 的 manifest/pkgdef 是为已验证可用的 VS Community 15.x
# XAE 注册面手工适配的，不能被 VS2022 构建输出覆盖；这里从受 Git 管理的
# 单一源刷新清单，并刷新托管 DLL。
Copy-Item (Join-Path $Repo "tc_agent_vsix\deploy.extension.vsixmanifest") `
    (Join-Path $extSrc "extension.vsixmanifest") -Force
Copy-Item (Join-Path $Repo "tc_agent_vsix\bin\Release\TwinCATAgent.Xae.dll") `
    (Join-Path $extSrc "TwinCATAgent.Xae.dll") -Force
Copy-Item (Join-Path $Repo "tc_agent_vsix\deploy.TwinCATAgent.Xae.pkgdef") `
    (Join-Path $extSrc "TwinCATAgent.Xae.pkgdef") -Force
# A previous renamed-registration build could leave files in this persistent
# staging directory.  They must not be copied into a local installation or
# make two VS packages compete for the same command set.
foreach ($staleName in @('TcCoAgent.dll', 'TcCoAgent.pkgdef', 'TwinCATAgent.dll', 'TwinCATAgent.pkgdef', 'tcxaeshell')) {
    $stalePath = Join-Path $extSrc $staleName
    if (Test-Path -LiteralPath $stalePath) {
        Remove-Item -LiteralPath $stalePath -Recurse -Force
    }
}
$extDst = Join-Path $Pkg "extension\TwinCAT Agent"
# Keep the stable package/VSIX identity while the managed XAE assembly uses the
# unambiguous TwinCATAgent.Xae name. The visible product name remains TwinCAT Agent.
New-Item -ItemType Directory -Force $extDst | Out-Null
foreach ($name in @(
    'extension.vsixmanifest', 'TwinCATAgent.Xae.dll', 'TwinCATAgent.Xae.pkgdef',
    'Microsoft.Web.WebView2.Core.dll', 'Microsoft.Web.WebView2.Wpf.dll', 'WebView2Loader.dll'
)) {
    $sourceFile = Join-Path $extSrc $name
    if (-not (Test-Path -LiteralPath $sourceFile -PathType Leaf)) {
        throw "扩展构建产物缺失：$sourceFile"
    }
    Copy-Item -LiteralPath $sourceFile -Destination (Join-Path $extDst $name) -Force
}
Copy-Item -LiteralPath (Join-Path $extSrc 'webview') -Destination (Join-Path $extDst 'webview') -Recurse -Force
# UI 单一真源 = tc_agent\static\index.html。扩展正常从 127.0.0.1:8766 加载；
# 仍同步 webview 副本作为安装诊断资产，禁止旧副本反向覆盖静态真源。
Copy-Item (Join-Path $Repo "tc_agent\static\index.html") (Join-Path $extDst "webview\index.html") -Force
Copy-Item (Join-Path $Repo "tc_agent\static\twincat-agent-logo.svg") `
    (Join-Path $extDst "webview\twincat-agent-logo.svg") -Force
# A VSIX is an OPC package, not an arbitrary ZIP.  In particular it must keep
# the VSSDK-created [Content_Types].xml and package metadata; Compress-Archive
# produced a file that looked right but VSIXInstaller rejected with 1011.
# Reuse the valid VSIX emitted by the VSSDK build.  Its manifest version is
# kept in sync with VERSION as part of the release change.
$builtVsix = Join-Path $Repo 'tc_agent_vsix\bin\Release\TwinCATAgent.Xae.vsix'
if (-not (Test-Path -LiteralPath $builtVsix -PathType Leaf)) {
    throw "扩展 VSIX 构建产物缺失：$builtVsix"
}
$xaeVsix = Join-Path $Pkg 'TwinCAT-Agent-XAE.vsix'
Copy-Item -LiteralPath $builtVsix -Destination $xaeVsix -Force
# VSSDK requires a VS Setup prerequisite at build time.  Replace only the
# manifest entry in the already-valid OPC container with the XAE deployment
# manifest.  Do not recreate the archive (that caused VSIX 1011).
Add-Type -AssemblyName System.IO.Compression.FileSystem
$archive = [IO.Compression.ZipFile]::Open($xaeVsix, [IO.Compression.ZipArchiveMode]::Update)
try {
    $entry = $archive.GetEntry('extension.vsixmanifest')
    if ($null -eq $entry) { throw '合法 VSIX 中缺少 extension.vsixmanifest。' }
    $entry.Delete()
    $replacement = $archive.CreateEntry('extension.vsixmanifest')
    $writer = New-Object IO.StreamWriter($replacement.Open(), (New-Object Text.UTF8Encoding($false)))
    try { $writer.Write([IO.File]::ReadAllText((Join-Path $Repo 'tc_agent_vsix\deploy.extension.vsixmanifest'))) }
    finally { $writer.Dispose() }
} finally { $archive.Dispose() }
Say "  extension\TwinCAT Agent -> 已复制（webview\index.html 已同步自 static，防陈旧）"
Say "  TwinCAT-Agent-XAE.vsix -> 已复制（VSSDK 合法注册包，版本 $AgentVersion）"

# --- 7) 启动器 / README（无散装 PowerShell 源码；兼容资源内置于字节码）---
foreach ($file in @("Start-Backend.vbs", "Start-Backend.cmd", "README.txt")) {
    Copy-Item (Join-Path $Tpl $file) $Pkg -Force
}
$utf8bom = New-Object System.Text.UTF8Encoding($true)
foreach ($f in @("README.txt")) {
    $p = Join-Path $Pkg $f
    if (Test-Path $p) { [IO.File]::WriteAllText($p, [IO.File]::ReadAllText($p), $utf8bom) }
}
Say "  启动器 + README -> 已放置（兼容脚本资源以字节码内置，无散装源码）"

# Do not ship readable Agent/tool source.  `.pyc` is an obfuscation layer, not
# a cryptographic boundary; secrets and truly proprietary decision logic still
# belong on a server.  Build each interpreter's bytecode separately.
function Convert-ToSourcelessBytecode([string]$Python, [string]$Tree, [string]$Label) {
    & $Python -m compileall -q -b $Tree
    if ($LASTEXITCODE -ne 0) { throw "$Label 字节码编译失败" }
    $sources = @(Get-ChildItem -LiteralPath $Tree -File -Recurse -Force -Filter '*.py')
    foreach ($source in $sources) { [IO.File]::Delete($source.FullName) }
    if (Get-ChildItem -LiteralPath $Tree -File -Recurse -Force -Filter '*.py' -ErrorAction SilentlyContinue) {
        throw "$Label 仍包含 Python 源码" }
    if (-not (Get-ChildItem -LiteralPath $Tree -File -Recurse -Force -Filter '*.pyc' -ErrorAction SilentlyContinue)) {
        throw "$Label 未生成 Python 字节码" }
}
Convert-ToSourcelessBytecode (Join-Path $PyDir 'python.exe') (Join-Path $App 'tc_agent') '主 Agent'
Convert-ToSourcelessBytecode (Join-Path $PyDir 'python.exe') (Join-Path $App 'tc_template') '主 COM 工具层'
Convert-ToSourcelessBytecode (Join-Path $ComPyDir 'python.exe') (Join-Path $ComApp 'tc_template') 'x86 COM worker'
& (Join-Path $PyDir 'python.exe') -c "import tc_agent.backend, tc_template; from tc_agent.openai_responses import ResponsesProvider; from tc_template._packaged_scripts import script_path; assert script_path('TcCom.ps1').is_file(); assert script_path('TcHmiBinding.ps1').is_file(); assert script_path('TcHmiServer.ps1').is_file(); print('main sourceless Responses and tool resources ok')"
if ($LASTEXITCODE -ne 0) { throw '主 Agent 字节码导入验证失败' }
& (Join-Path $ComPyDir 'python.exe') -c "import tc_template; print('x86 sourceless import ok')"
if ($LASTEXITCODE -ne 0) { throw 'x86 COM worker 字节码导入验证失败' }

# 构建期导入测试会在 app 和内嵌 Python runtime 下重新生成缓存；交付前统一清除。
$generatedCaches = @(Get-ChildItem -LiteralPath $Pkg -Directory -Recurse -Force -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -eq '__pycache__' })
foreach ($cache in ($generatedCaches | Sort-Object { $_.FullName.Length } -Descending)) {
    [IO.Directory]::Delete($cache.FullName, $true)
}
# 发布硬门禁：客户包不得包含开发机配置、历史或 SQLite 对话数据库。
$privatePayload = @(Get-ChildItem -LiteralPath $App -File -Recurse -Force | Where-Object {
    $_.Name -match '^(?i:config\.json|chat_history\.json|agent\.db(?:-.+)?)$'
})
if ($privatePayload.Count) {
    throw "客户程序包含本地私有数据：$($privatePayload.FullName -join ', ')"
}
$readableToolSource = @(Get-ChildItem -LiteralPath $App -File -Recurse -Force -Filter '*.py' -ErrorAction SilentlyContinue)
if ($readableToolSource.Count) {
    throw "客户程序包含可读 Python 工具源码：$($readableToolSource.FullName -join ', ')"
}

# --- 8) 汇总 ---
$size = "{0:N0} MB" -f ((Get-ChildItem $Pkg -Recurse -File | Measure-Object Length -Sum).Sum / 1MB)
Say ""
Say "✓ 便携包就绪: $Pkg  ($size)" 'Green'

if ($Zip) {
    $zipOut = "$Pkg.zip"
    if (Test-Path $zipOut) { Remove-Item $zipOut -Force }
    Say "压缩为 zip（可能较慢）..."
    Compress-Archive -Path (Join-Path $Pkg "*") -DestinationPath $zipOut -CompressionLevel Optimal
    $zsize = "{0:N0} MB" -f ((Get-Item $zipOut).Length / 1MB)
    Say "✓ 压缩包: $zipOut  ($zsize)" 'Green'
}

exit 0   # robocopy 会把 $LASTEXITCODE 置成 1(=有文件复制,仍是成功),显式归零避免误判失败
