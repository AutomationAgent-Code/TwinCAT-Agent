# Publish a release to GitHub without publishing this repository's source tree.
# Requires GitHub CLI (https://cli.github.com/) and an authenticated account
# with write access to the selected repository.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$InstallerPath,
    [ValidateSet('stable', 'beta')]
    [string]$Channel = 'stable',
    [string]$NotesFile = '',
    [string]$Repository = 'AutomationAgent-Code/TwinCAT-Agent',
    [string]$OutputRoot = ''
)

$ErrorActionPreference = 'Stop'
if (-not $OutputRoot) { $OutputRoot = Join-Path (Split-Path $PSScriptRoot -Parent) 'dist\update-feed' }
$repoRoot = Split-Path $PSScriptRoot -Parent
$publisher = Join-Path $PSScriptRoot 'Publish-UpdateRelease.ps1'
$gh = Get-Command gh -ErrorAction SilentlyContinue
if (-not $gh -and (Test-Path -LiteralPath 'C:\Program Files\GitHub CLI\gh.exe')) {
    $ghPath = 'C:\Program Files\GitHub CLI\gh.exe'
} elseif ($gh) {
    $ghPath = $gh.Source
} else {
    throw '未安装 GitHub CLI（gh）。请先安装 https://cli.github.com/ 并执行 gh auth login。'
}
& $ghPath auth status -h github.com
if ($LASTEXITCODE -ne 0) { throw 'GitHub 未登录或授权无效。请执行 gh auth login。' }

$installer = [IO.Path]::GetFullPath($InstallerPath)
$match = [regex]::Match([IO.Path]::GetFileName($installer), '^TwinCAT-Agent-Setup-v(?<version>\d+\.\d+\.\d+(?:\.\d+)?)\.exe$', 'IgnoreCase')
if (-not $match.Success) { throw '安装包名称必须形如 TwinCAT-Agent-Setup-v1.2.3.exe。' }
$version = $match.Groups['version'].Value
$tag = "v$version"
$baseUrl = "https://github.com/$Repository/releases/download/$tag"

& $publisher -InstallerPath $installer -PublicBaseUrl $baseUrl -AssetBaseUrl $baseUrl -Channel $Channel -OutputRoot $OutputRoot -NotesFile $NotesFile
if ($LASTEXITCODE -ne 0) { throw '生成更新清单失败。' }

$channelDir = Join-Path ([IO.Path]::GetFullPath($OutputRoot)) $Channel
$assets = @(Get-ChildItem -LiteralPath $channelDir -File | Select-Object -ExpandProperty FullName)
if (-not $assets) { throw '没有找到可发布的更新文件。' }

$releaseExists = $false
try {
    & $ghPath release view $tag --repo $Repository 2>$null
    $releaseExists = ($LASTEXITCODE -eq 0)
} catch {
    $releaseExists = $false
}
if ($releaseExists) {
    & $ghPath release upload $tag @assets --repo $Repository --clobber
} else {
    $args = @('release', 'create', $tag) + $assets + @('--repo', $Repository, '--title', "TwinCAT Agent $version")
    if ($NotesFile) { $args += @('--notes-file', [IO.Path]::GetFullPath($NotesFile)) }
    else { $args += @('--notes', "TwinCAT Agent $version") }
    if ($Channel -eq 'beta') { $args += '--prerelease' }
    & $ghPath @args
}
if ($LASTEXITCODE -ne 0) { throw 'GitHub Release 发布失败。' }

Write-Host "Published $tag. Stable update URL:" -ForegroundColor Green
Write-Host "https://github.com/$Repository/releases/latest/download/latest.json" -ForegroundColor Cyan
