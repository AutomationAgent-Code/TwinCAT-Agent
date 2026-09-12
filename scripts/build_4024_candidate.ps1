param(
    [switch]$NoDocs,
    [switch]$SkipBuild,
    [string]$Version
)

$ErrorActionPreference = 'Stop'
$Repo = Split-Path $PSScriptRoot -Parent
$Dist = Join-Path $Repo 'dist'
$Stage = Join-Path $Dist '_4024-candidate'
$VersionFile = Join-Path $Repo 'VERSION'
$AppVersion = if ($Version) { $Version.Trim() } else {
    (Get-Content -LiteralPath $VersionFile -Raw -Encoding UTF8).Trim()
}
if ($AppVersion -notmatch '^\d+\.\d+\.\d+(?:\.\d+)?$') {
    throw "Invalid version: '$AppVersion'"
}
$Setup = Join-Path $Dist "TwinCAT-Agent-Setup-v$AppVersion.exe"
$CandidateSetup = Join-Path $Dist "TwinCAT-Agent-4024-Setup-v$AppVersion.exe"
$Kit = Join-Path $Dist "TwinCAT-Agent-4024-TestKit-v$AppVersion.zip"

function Assert-RepoPath([string]$Path) {
    $repoFull = [IO.Path]::GetFullPath($Repo).TrimEnd('\') + '\'
    $pathFull = [IO.Path]::GetFullPath($Path)
    if (-not $pathFull.StartsWith($repoFull, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Build path is outside the repository: $pathFull"
    }
}

function Get-PeArchitecture([string]$Path) {
    $stream = [IO.File]::OpenRead($Path)
    try {
        $reader = New-Object IO.BinaryReader($stream)
        $stream.Position = 0x3c
        $peOffset = $reader.ReadInt32()
        $stream.Position = $peOffset + 4
        $machine = $reader.ReadUInt16()
        if ($machine -eq 0x014c) { return 'x86' }
        if ($machine -eq 0x8664) { return 'x64' }
        return ('0x{0:X4}' -f $machine)
    } finally {
        $stream.Dispose()
    }
}

function Assert-4024Manifest([string]$Path) {
    $manifest = New-Object Xml.XmlDocument
    $manifest.Load([IO.Path]::GetFullPath($Path))
    $ns = New-Object Xml.XmlNamespaceManager -ArgumentList $manifest.NameTable
    $ns.AddNamespace('v', 'http://schemas.microsoft.com/developer/vsx-schema/2011')
    $target = $manifest.SelectSingleNode('//v:InstallationTarget', $ns)
    $architecture = $manifest.SelectSingleNode('//v:ProductArchitecture', $ns)
    if (-not $target -or $target.Version -ne '[15.0,16.0)') {
        throw "Manifest is not restricted to Shell 15: $Path"
    }
    if (-not $architecture -or $architecture.InnerText.Trim() -ne 'x86') {
        throw "Manifest is not restricted to x86: $Path"
    }
}

Assert-RepoPath $Stage
Assert-RepoPath $CandidateSetup
Assert-RepoPath $Kit

$sourceManifest = Join-Path $Repo 'tc_agent_vsix\source.extension.vsixmanifest'
$deployManifest = Join-Path $Repo 'tc_agent_vsix\deploy.extension.vsixmanifest'
$loader = Join-Path $Repo 'tc_agent_vsix\deploy_stage\TwinCAT Agent\WebView2Loader.dll'
Assert-4024Manifest $sourceManifest
Assert-4024Manifest $deployManifest
if ((Get-PeArchitecture $loader) -ne 'x86') {
    throw 'The staged WebView2Loader.dll is not x86.'
}

if (-not $SkipBuild) {
    $args = @{ Version = $AppVersion }
    if ($NoDocs) { $args.NoDocs = $true }
    & (Join-Path $PSScriptRoot 'build_installer.ps1') @args
    if ($LASTEXITCODE -ne 0) { throw "Installer build failed ($LASTEXITCODE)" }
}
if (-not (Test-Path -LiteralPath $Setup -PathType Leaf)) {
    throw "Setup was not found: $Setup"
}

Copy-Item -LiteralPath $Setup -Destination $CandidateSetup -Force
if (Test-Path -LiteralPath $Stage) {
    Remove-Item -LiteralPath $Stage -Recurse -Force
}
New-Item -ItemType Directory -Path $Stage -Force | Out-Null
Copy-Item -LiteralPath $CandidateSetup -Destination `
    (Join-Path $Stage (Split-Path $CandidateSetup -Leaf))
Copy-Item -LiteralPath (Join-Path $Repo 'scripts\Test-TwinCAT4024Environment.ps1') `
    -Destination (Join-Path $Stage 'Test-TwinCAT4024Environment.ps1')
Copy-Item -LiteralPath (Join-Path $Repo 'packaging\4024\README.txt') `
    -Destination (Join-Path $Stage 'README.txt')
Copy-Item -LiteralPath (Join-Path $Repo 'docs\twincat_4024_vm_test.md') `
    -Destination (Join-Path $Stage 'VM-Test-Guide.md')

if (Test-Path -LiteralPath $Kit) { Remove-Item -LiteralPath $Kit -Force }
Compress-Archive -Path (Join-Path $Stage '*') -DestinationPath $Kit -CompressionLevel Optimal

$setupHash = (Get-FileHash -LiteralPath $CandidateSetup -Algorithm SHA256).Hash
$kitHash = (Get-FileHash -LiteralPath $Kit -Algorithm SHA256).Hash
Write-Host "4024 candidate setup: $CandidateSetup" -ForegroundColor Green
Write-Host "  SHA256: $setupHash" -ForegroundColor DarkGray
Write-Host "4024 VM test kit:    $Kit" -ForegroundColor Green
Write-Host "  SHA256: $kitHash" -ForegroundColor DarkGray
