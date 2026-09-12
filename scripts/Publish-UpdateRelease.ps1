# Create a minimal, upload-ready TwinCAT Agent update channel.
# This script deliberately copies only distributable artifacts.  It never
# exports source code, build scripts, project rules, or local configuration.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$InstallerPath,
    [Parameter(Mandatory = $true)]
    [string]$PublicBaseUrl,
    [ValidateSet('stable', 'beta')]
    [string]$Channel = 'stable',
    [string]$OutputRoot = '',
    [string]$AssetBaseUrl = '',
    [string]$NotesFile = ''
)

$ErrorActionPreference = 'Stop'
if (-not $OutputRoot) { $OutputRoot = Join-Path (Split-Path $PSScriptRoot -Parent) 'dist\update-feed' }

function Get-NormalizedBaseUrl([string]$Url) {
    $value = $Url.Trim().TrimEnd('/')
    $parsed = [Uri]$value
    if ($parsed.Scheme -ne 'https') { throw '更新源必须使用 HTTPS。' }
    return $value
}

function Get-Sha256([string]$Path) {
    $stream = [IO.File]::OpenRead($Path)
    try {
        $sha = [Security.Cryptography.SHA256]::Create()
        try { return ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-', '').ToLowerInvariant() }
        finally { $sha.Dispose() }
    } finally { $stream.Dispose() }
}

$installer = [IO.Path]::GetFullPath($InstallerPath)
if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) { throw "未找到安装包：$installer" }
if ([IO.Path]::GetExtension($installer) -ne '.exe') { throw '安装包必须是 .exe 文件。' }
$match = [regex]::Match([IO.Path]::GetFileName($installer), '^TwinCAT-Agent-Setup-v(?<version>\d+\.\d+\.\d+(?:\.\d+)?)\.exe$', 'IgnoreCase')
if (-not $match.Success) { throw '安装包名称必须形如 TwinCAT-Agent-Setup-v1.2.3.exe。' }

$baseUrl = Get-NormalizedBaseUrl $PublicBaseUrl
$assetBaseUrl = if ($AssetBaseUrl) { Get-NormalizedBaseUrl $AssetBaseUrl } else { "$baseUrl/$Channel" }
$version = $match.Groups['version'].Value
$channelDir = Join-Path ([IO.Path]::GetFullPath($OutputRoot)) $Channel
New-Item -ItemType Directory -Path $channelDir -Force | Out-Null

$fileName = [IO.Path]::GetFileName($installer)
$targetInstaller = Join-Path $channelDir $fileName
Copy-Item -LiteralPath $installer -Destination $targetInstaller -Force
$hash = Get-Sha256 $targetInstaller
$hashFile = "$targetInstaller.sha256"
[IO.File]::WriteAllText($hashFile, "$hash  $fileName`r`n", [Text.UTF8Encoding]::new($false))

$notesName = ''
if ($NotesFile) {
    $notes = [IO.Path]::GetFullPath($NotesFile)
    if (-not (Test-Path -LiteralPath $notes -PathType Leaf)) { throw "未找到更新说明：$notes" }
    $notesName = 'release-notes.md'
    Copy-Item -LiteralPath $notes -Destination (Join-Path $channelDir $notesName) -Force
}

$publishedAt = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
$manifest = [ordered]@{
    schema_version = 1
    version = $version
    channel = $Channel
    published_at = $publishedAt
    installer = [ordered]@{
        name = $fileName
        url = "$assetBaseUrl/$fileName"
        sha256 = $hash
        size_bytes = (Get-Item -LiteralPath $targetInstaller).Length
    }
    notes_url = if ($notesName) { "$assetBaseUrl/$notesName" } else { $null }
}
$manifestPath = Join-Path $channelDir 'latest.json'
$manifestJson = $manifest | ConvertTo-Json -Depth 5
[IO.File]::WriteAllText($manifestPath, $manifestJson, [Text.UTF8Encoding]::new($false))

Write-Host "Update feed ready: $channelDir" -ForegroundColor Green
Get-ChildItem -LiteralPath $channelDir -File | Select-Object Name, Length | Format-Table -AutoSize
Write-Host "Upload only the files in this directory to: $assetBaseUrl/" -ForegroundColor Cyan
