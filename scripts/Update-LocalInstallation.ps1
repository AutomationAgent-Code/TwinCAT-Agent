# TwinCAT Agent local updater. No installer or administrator rights required.
[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'Programs\TwinCAT Agent'),
    [string]$PackageDir = '',
    [switch]$IncludeExtension,
    [switch]$IncludeRuntime,
    [switch]$NoRestart,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$Repo = Split-Path $PSScriptRoot -Parent
$InstallDir = [IO.Path]::GetFullPath($InstallDir)
$installedApp = Join-Path $InstallDir 'app'
$userDataRoot = Join-Path $env:LOCALAPPDATA 'TwinCAT Agent'
$stableConfig = Join-Path $userDataRoot 'config.json'

function Write-Step([string]$Message, [ConsoleColor]$Color = 'Cyan') {
    Write-Host $Message -ForegroundColor $Color
}

function Assert-SafeInstallRoot {
    if (-not (Test-Path -LiteralPath $InstallDir -PathType Container)) {
        throw "TwinCAT Agent installation was not found: $InstallDir"
    }
    foreach ($required in @('app', 'runtime', 'Start-Backend.vbs')) {
        if (-not (Test-Path -LiteralPath (Join-Path $InstallDir $required))) {
            throw "Target is not a TwinCAT Agent installation (missing $required): $InstallDir"
        }
    }
    $resolved = (Resolve-Path -LiteralPath $InstallDir).Path.TrimEnd('\')
    if ($resolved -in @([IO.Path]::GetPathRoot($resolved).TrimEnd('\'), $env:LOCALAPPDATA.TrimEnd('\'))) {
        throw "Refusing to update an unsafe broad directory: $resolved"
    }
}

function Copy-Tree([string]$Source, [string]$Destination, [switch]$ProgramOnly) {
    if (-not (Test-Path -LiteralPath $Source -PathType Container)) {
        throw "Update source directory was not found: $Source"
    }
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    # Release packages contain interpreter-specific, source-less bytecode.
    # Preserve it both when installing and backing up; only __pycache__ is
    # disposable. Each interpreter's package must use its matching source.
    $excluded = @('*.bak', '*.log', '*.out')
    if ($ProgramOnly) {
        # Runtime data belongs to the installation/user, never to the source
        # checkout or update package. In particular, replacing agent.db can
        # silently swap conversation history after a backend restart.
        $excluded += @('config.json', 'chat_history.json', '.env',
                       'agent.db', 'agent.db-wal', 'agent.db-shm')
    }
    & robocopy $Source $Destination /E /XD __pycache__ /XF $excluded /R:2 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) {
        throw "Copy failed: $Source -> $Destination (robocopy $LASTEXITCODE)"
    }
    if ($ProgramOnly) {
        # Source updates used on development machines must not shadow the
        # new release bytecode. Backups are completed before program copying.
        $sourcePrefix = [IO.Path]::GetFullPath($Source).TrimEnd('\') + '\'
        $destinationPrefix = [IO.Path]::GetFullPath($Destination).TrimEnd('\') + '\'
        foreach ($bytecode in Get-ChildItem -LiteralPath $Source -Filter '*.pyc' -File -Recurse) {
            if ($bytecode.FullName -match '\\__pycache__\\') { continue }
            if (Test-Path -LiteralPath ([IO.Path]::ChangeExtension($bytecode.FullName, '.py'))) { continue }
            $relative = $bytecode.FullName.Substring($sourcePrefix.Length)
            $staleSource = [IO.Path]::GetFullPath((Join-Path $Destination ([IO.Path]::ChangeExtension($relative, '.py'))))
            if (-not $staleSource.StartsWith($destinationPrefix, [StringComparison]::OrdinalIgnoreCase)) { throw 'Source cleanup path escaped destination' }
            if (Test-Path -LiteralPath $staleSource -PathType Leaf) { Remove-Item -LiteralPath $staleSource -Force }
            if ($bytecode.Name -eq '_script_bundle.pyc') {
                # Development fallback scripts otherwise take precedence over
                # the new embedded resources in the release package.
                foreach ($scriptName in @('TcCom.ps1', 'TcHmiBinding.ps1', 'TcHmiServer.ps1', 'TcHmiItems.ps1', 'TcHmiProject.ps1', 'TcIoConfiguration.ps1')) {
                    if (Test-Path -LiteralPath (Join-Path $bytecode.DirectoryName $scriptName)) { continue }
                    $oldScript = Join-Path (Split-Path -Parent $staleSource) $scriptName
                    if (Test-Path -LiteralPath $oldScript -PathType Leaf) { Remove-Item -LiteralPath $oldScript -Force }
                }
            }
        }
    }
}

function Stop-AgentBackend {
    $prefix = $InstallDir.TrimEnd('\') + '\'
    $processes = @(Get-CimInstance Win32_Process | Where-Object {
        $_.CommandLine -like '*tc_agent.backend*' -and
        ($_.ExecutablePath -like "$prefix*" -or $_.CommandLine -like "*$InstallDir*")
    })
    # A developer-started backend can own the product ports while its command
    # line contains no installation path.  Include only verified Agent module
    # owners; never terminate an unrelated process that happens to use a port.
    $portOwners = @(Get-NetTCPConnection -LocalPort 8765,8766 -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique)
    foreach ($ownerPid in $portOwners) {
        if ($processes.ProcessId -contains $ownerPid) { continue }
        $owner = Get-CimInstance Win32_Process -Filter "ProcessId=$ownerPid" -ErrorAction SilentlyContinue
        if ($owner -and $owner.CommandLine -like '*tc_agent.backend*') {
            $processes += $owner
        }
    }
    foreach ($process in $processes) {
        Write-Step "Stopping backend process PID $($process.ProcessId)..."
        Stop-Process -Id $process.ProcessId -Force -ErrorAction Stop
    }
    if ($processes) { Start-Sleep -Milliseconds 500 }
}

function Sync-DevelopmentExtension {
    $vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
    $msbuild = $null
    if (Test-Path -LiteralPath $vswhere) {
        $msbuild = & $vswhere -latest -requires Microsoft.Component.MSBuild -find 'MSBuild\**\Bin\MSBuild.exe' |
            Select-Object -First 1
    }
    if (-not $msbuild) {
        $msbuild = @(
            'C:\Program Files\Microsoft Visual Studio\2022\Community\MSBuild\Current\Bin\MSBuild.exe',
            'C:\Program Files\Microsoft Visual Studio\2022\Professional\MSBuild\Current\Bin\MSBuild.exe',
            'C:\Program Files\Microsoft Visual Studio\2022\Enterprise\MSBuild\Current\Bin\MSBuild.exe'
        ) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    }
    if (-not $msbuild) { throw '找不到 MSBuild，无法更新 XAE 扩展。' }
    Write-Step 'Building TwinCAT Agent XAE extension...'
    & $msbuild (Join-Path $Repo 'tc_agent_vsix\TwinCATAgent.Xae.csproj') /t:Build /p:Configuration=Release /p:Platform=AnyCPU /nologo /verbosity:minimal
    if ($LASTEXITCODE -ne 0) { throw "XAE extension build failed ($LASTEXITCODE)" }
    $stage = Join-Path $Repo 'tc_agent_vsix\deploy_stage\TwinCAT Agent'
    foreach ($entry in @(
        @{ Source = 'tc_agent_vsix\deploy.extension.vsixmanifest'; Destination = 'extension.vsixmanifest' },
        @{ Source = 'tc_agent_vsix\bin\Release\TwinCATAgent.Xae.dll'; Destination = 'TwinCATAgent.Xae.dll' },
        @{ Source = 'tc_agent_vsix\deploy.TwinCATAgent.Xae.pkgdef'; Destination = 'TwinCATAgent.Xae.pkgdef' },
        @{ Source = 'tc_agent\static\index.html'; Destination = 'webview\index.html' },
        @{ Source = 'tc_agent\static\twincat-agent-logo.svg'; Destination = 'webview\twincat-agent-logo.svg' }
    )) {
        $destination = Join-Path $stage $entry.Destination
        New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
        Copy-Item (Join-Path $Repo $entry.Source) $destination -Force
    }
}

Assert-SafeInstallRoot

if ($PackageDir) {
    $sourceRoot = [IO.Path]::GetFullPath($PackageDir)
    $sourceApp = Join-Path $sourceRoot 'app'
    $sourceExtension = Join-Path $sourceRoot 'extension\TwinCAT Agent'
    $sourceLabel = $sourceRoot
} else {
    & (Join-Path $PSScriptRoot 'build_ads_dynamic_bridge.ps1') -Quiet
    if ($LASTEXITCODE -ne 0) { throw 'Failed to build the dynamic ADS bridge.' }
    if ($IncludeExtension) { Sync-DevelopmentExtension }
    $sourceApp = $Repo
    $sourceExtension = Join-Path $Repo 'tc_agent_vsix\deploy_stage\TwinCAT Agent'
    $sourceLabel = $Repo
}

$components = @(
    @{ Name = 'tc_agent'; Source = (Join-Path $sourceApp 'tc_agent'); Destination = (Join-Path $installedApp 'tc_agent') },
    @{ Name = 'tc_template'; Source = (Join-Path $sourceApp 'tc_template'); Destination = (Join-Path $installedApp 'tc_template') }
)
# The x86 interpreter imports its private app before the main app. Updating
# only installedApp silently leaves every native COM command on old code.
if (-not $IncludeRuntime -and (Test-Path -LiteralPath (Join-Path $InstallDir 'runtime\com32\python.exe'))) {
    $comSource = $null
    if ($PackageDir) {
        $packagedComSource = Join-Path $sourceRoot 'runtime\com32\app\tc_template'
        if (Test-Path -LiteralPath $packagedComSource -PathType Container) {
            $comSource = $packagedComSource
        }
    }
    if (-not $comSource) {
        # Development/source updates can be imported by either interpreter.
        # Never copy Python 3.14-only bytecode into the Python 3.12 helper.
        $templateSource = Join-Path $sourceApp 'tc_template'
        if (-not (Test-Path -LiteralPath (Join-Path $templateSource '_native_worker.py'))) {
            throw 'Update package is missing the architecture-matched COM helper code. No files were changed.'
        }
        if (Get-ChildItem -LiteralPath $templateSource -Filter '*.pyc' -Recurse -File |
            Where-Object { $_.Directory.Name -ne '__pycache__' }) {
            throw 'Mixed source/bytecode COM update requires an architecture-matched helper package.'
        }
        $comSource = $templateSource
    }
    $components += @{ Name = 'com32-app'; Source = $comSource; Destination = (Join-Path $InstallDir 'runtime\com32\app\tc_template') }
}
$caseSource = Join-Path $sourceApp 'CaseLibrary'
$basicSource = Join-Path $sourceApp 'Repository\basic'
if (Test-Path -LiteralPath $basicSource) {
    $components += @{ Name = 'basic-solution-template'; Source = $basicSource; Destination = (Join-Path $installedApp 'Repository\basic') }
}
if (Test-Path -LiteralPath $caseSource) {
    $components += @{ Name = 'CaseLibrary'; Source = $caseSource; Destination = (Join-Path $installedApp 'CaseLibrary') }
}
$fbTemplateSource = Join-Path $sourceApp 'fblib'
if (Test-Path -LiteralPath $fbTemplateSource) {
    $components += @{ Name = 'fblib'; Source = $fbTemplateSource; Destination = (Join-Path $installedApp 'fblib') }
}
if ($IncludeExtension) {
    $components += @{ Name = 'extension'; Source = $sourceExtension; Destination = (Join-Path $InstallDir 'extension\TwinCAT Agent') }
}
if ($IncludeRuntime) {
    if (-not $PackageDir) {
        throw '-IncludeRuntime requires -PackageDir with a built portable package.'
    }
    $components += @{ Name = 'runtime'; Source = (Join-Path $sourceRoot 'runtime'); Destination = (Join-Path $InstallDir 'runtime') }
}

foreach ($component in $components) {
    if (-not (Test-Path -LiteralPath $component.Source -PathType Container)) {
        throw "Update source is missing $($component.Name): $($component.Source)"
    }
}

Write-Step "Update source: $sourceLabel"
Write-Step "Installation: $InstallDir"
Write-Step ('Components: ' + (($components | ForEach-Object Name) -join ', '))
if ($DryRun) {
    Write-Step 'Dry run completed. No process or file was changed.' 'Yellow'
    exit 0
}

# Provider/API settings are user data, not program files. Older developer and
# installed backends stored separate copies beside tc_agent\config.py. Seed the
# stable location once, preferring the checkout used to invoke this updater;
# never overwrite an existing stable configuration.
if (-not (Test-Path -LiteralPath $stableConfig -PathType Leaf)) {
    $legacyCandidates = @(
        (Join-Path $Repo 'tc_agent\config.json'),
        (Join-Path $installedApp 'tc_agent\config.json')
    ) | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf }
    foreach ($candidate in $legacyCandidates) {
        try {
            $legacyConfig = [IO.File]::ReadAllText($candidate, [Text.Encoding]::UTF8) | ConvertFrom-Json
            if ($null -eq $legacyConfig.providers) { continue }
            New-Item -ItemType Directory -Path $userDataRoot -Force | Out-Null
            Copy-Item -LiteralPath $candidate -Destination $stableConfig -Force
            Write-Step "Migrated provider settings to stable user-data storage."
            break
        } catch { }
    }
}

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backupRoot = Join-Path $InstallDir ".updates\backup-$stamp"
New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
if (Test-Path -LiteralPath $stableConfig -PathType Leaf) {
    Copy-Item -LiteralPath $stableConfig -Destination (Join-Path $backupRoot 'user-config.json') -Force
}

try {
    Stop-AgentBackend
    Write-Step "Backing up the current program to: $backupRoot"
    foreach ($component in $components) {
        if (Test-Path -LiteralPath $component.Destination) {
            Copy-Tree $component.Destination (Join-Path $backupRoot $component.Name)
        }
    }
    foreach ($component in $components) {
        Write-Step "Updating $($component.Name)..."
        Copy-Tree $component.Source $component.Destination -ProgramOnly
    }
    $versionSource = if ($PackageDir) { Join-Path $sourceApp 'VERSION' } else { Join-Path $Repo 'VERSION' }
    if (Test-Path -LiteralPath $versionSource) {
        Copy-Item -LiteralPath $versionSource -Destination (Join-Path $installedApp 'VERSION') -Force
    }
} catch {
    Write-Step "Update failed; restoring backup: $($_.Exception.Message)" 'Red'
    foreach ($component in $components) {
        $backup = Join-Path $backupRoot $component.Name
        if (Test-Path -LiteralPath $backup) { Copy-Tree $backup $component.Destination }
    }
    throw
}

if (-not $NoRestart) {
    Write-Step 'Starting TwinCAT Agent backend...'
    Start-Process -FilePath 'wscript.exe' -ArgumentList ('"{0}"' -f (Join-Path $InstallDir 'Start-Backend.vbs')) -WorkingDirectory $InstallDir -WindowStyle Hidden
}

Write-Step 'TwinCAT Agent local update completed.' 'Green'
Write-Step "Rollback backup: $backupRoot" 'DarkGray'
if ($IncludeExtension) {
    Write-Step 'The XAE extension was updated. Restart XAE to load it.' 'Yellow'
}
