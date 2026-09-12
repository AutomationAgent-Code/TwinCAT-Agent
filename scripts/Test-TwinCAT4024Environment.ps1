param(
    [string]$Output = "",
    [switch]$Strict
)

$ErrorActionPreference = 'Stop'
if (-not $Output) {
    $Output = Join-Path $PSScriptRoot 'TwinCAT4024-Environment.json'
}

function Get-PeArchitecture {
    param([Parameter(Mandatory)][string]$Path)
    $stream = [IO.File]::OpenRead($Path)
    try {
        $reader = New-Object IO.BinaryReader($stream)
        $stream.Position = 0x3c
        $peOffset = $reader.ReadInt32()
        $stream.Position = $peOffset + 4
        $machine = $reader.ReadUInt16()
        switch ($machine) {
            0x014c { return 'x86' }
            0x8664 { return 'x64' }
            default { return ('0x{0:X4}' -f $machine) }
        }
    } finally {
        $stream.Dispose()
    }
}

function Find-XaeShell {
    $candidates = @(
        "${env:ProgramFiles(x86)}\Beckhoff\TcXaeShell\Common7\IDE\TcXaeShell.exe",
        "${env:ProgramFiles}\Beckhoff\TcXaeShell\Common7\IDE\TcXaeShell.exe",
        $(if ($env:TWINCAT3DIR) { Join-Path $env:TWINCAT3DIR 'Components\Base\TcXaeShell\Common7\IDE\TcXaeShell.exe' }),
        'C:\TwinCAT\3.1\Components\Base\TcXaeShell\Common7\IDE\TcXaeShell.exe',
        $(if ($env:TWINCAT3DIR) { Join-Path $env:TWINCAT3DIR 'TcXaeShell\Common7\IDE\TcXaeShell.exe' }),
        'C:\TwinCAT\3.1\TcXaeShell\Common7\IDE\TcXaeShell.exe'
    )
    foreach ($candidate in @($candidates | Where-Object { $_ } | Select-Object -Unique)) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    return ''
}

function Find-TwinCATVersion {
    $roots = @(
        $env:TWINCAT3DIR,
        'C:\TwinCAT\3.1',
        "${env:ProgramFiles(x86)}\Beckhoff\TwinCAT\3.1",
        "${env:ProgramFiles}\Beckhoff\TwinCAT\3.1"
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Container) } |
        Select-Object -Unique

    $records = @()
    foreach ($root in $roots) {
        foreach ($relative in @(
            'System\TcSysSrv.exe',
            'System\TcSystemService.exe',
            'SDK\TwinCATVersion.xml',
            'System\TwinCATVersion.xml',
            'TwinCATVersion.xml'
        )) {
            $path = Join-Path $root $relative
            if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { continue }
            $version = ''
            if ([IO.Path]::GetExtension($path) -eq '.xml') {
                $text = Get-Content -LiteralPath $path -Raw -ErrorAction SilentlyContinue
                $match = [regex]::Match([string]$text, '3\.1\.40\d{2}(?:\.\d+)?')
                if ($match.Success) {
                    $version = $match.Value
                } else {
                    try {
                        [xml]$xml = $text
                        $settings = @{}
                        foreach ($item in @($xml.configuration.appSettings.add)) {
                            $settings[[string]$item.key] = [string]$item.value
                        }
                        if ($settings.major -and $settings.minor -and $settings.build) {
                            $version = "$($settings.major).$($settings.minor).$($settings.build)"
                            if ($settings.revision) { $version += ".$($settings.revision)" }
                        }
                    } catch { }
                }
            } else {
                $info = (Get-Item -LiteralPath $path).VersionInfo
                $version = [string]$info.ProductVersion
                if (-not $version) { $version = [string]$info.FileVersion }
            }
            $records += [pscustomobject]@{ path = $path; version = $version }
        }
    }
    $preferred = @($records | Where-Object { $_.version -match '3\.1\.4024' } |
        Select-Object -First 1)
    if (-not $preferred.Count) {
        $preferred = @($records | Where-Object { $_.version } | Select-Object -First 1)
    }
    [pscustomobject]@{
        version = if ($preferred.Count) { [string]$preferred[0].version } else { '' }
        sources = @($records)
    }
}

function Get-AssemblyRecord {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return [pscustomobject]@{ path = $Path; exists = $false }
    }
    try {
        $name = [Reflection.AssemblyName]::GetAssemblyName($Path)
        return [pscustomobject]@{
            path = $Path
            exists = $true
            name = [string]$name.Name
            version = [string]$name.Version
            processor = [string]$name.ProcessorArchitecture
        }
    } catch {
        return [pscustomobject]@{ path = $Path; exists = $true; error = [string]$_.Exception.Message }
    }
}

function Find-WebView2Runtime {
    $roots = @(
        "${env:ProgramFiles(x86)}\Microsoft\EdgeWebView\Application",
        "${env:ProgramFiles}\Microsoft\EdgeWebView\Application",
        "${env:LOCALAPPDATA}\Microsoft\EdgeWebView\Application"
    )
    $executables = @()
    foreach ($root in $roots) {
        if (-not ($root -and (Test-Path -LiteralPath $root -PathType Container))) { continue }
        $executables += Get-ChildItem -LiteralPath $root -Filter msedgewebview2.exe -File `
            -Recurse -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName
    }
    @($executables | Select-Object -Unique)
}

$errors = @()
$warnings = @()
$windowsVersion = [Environment]::OSVersion.Version
$shellExe = Find-XaeShell
$shellRoot = if ($shellExe) { Split-Path (Split-Path (Split-Path $shellExe -Parent) -Parent) -Parent } else { '' }
$shellIde = if ($shellExe) { Split-Path $shellExe -Parent } else { '' }
$tc = Find-TwinCATVersion
$webView = Find-WebView2Runtime
$dotNetRelease = 0
try {
    $dotNetRelease = [int](Get-ItemProperty `
        'HKLM:\SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full' `
        -Name Release -ErrorAction Stop).Release
} catch { }

$shell = $null
$assemblies = @()
if ($shellExe) {
    $info = (Get-Item -LiteralPath $shellExe).VersionInfo
    $shell = [pscustomobject]@{
        path = $shellExe
        architecture = Get-PeArchitecture $shellExe
        file_version = [string]$info.FileVersion
        product_version = [string]$info.ProductVersion
    }
    $assemblies = @(
        Get-AssemblyRecord (Join-Path $shellIde 'PublicAssemblies\Microsoft.VisualStudio.Shell.15.0.dll')
        Get-AssemblyRecord (Join-Path $shellIde 'PublicAssemblies\Microsoft.VisualStudio.Shell.Framework.dll')
        Get-AssemblyRecord (Join-Path $shellIde 'PrivateAssemblies\Microsoft.VisualStudio.Threading.dll')
    )
} else {
    $errors += 'TwinCAT XAE Shell (TcXaeShell.exe) was not found.'
}

if (-not $tc.version) { $errors += 'TwinCAT version could not be detected.' }
elseif ($tc.version -notmatch '3\.1\.4024') {
    $errors += "Expected TwinCAT 3.1 Build 4024, detected '$($tc.version)'."
}
if ($windowsVersion.Major -lt 10) {
    $errors += "Windows 10 or newer is required for composition-based WebView2 (detected $windowsVersion)."
}
if ($shell -and $shell.architecture -ne 'x86') {
    $errors += "4024 candidate expects the 32-bit XAE Shell; detected '$($shell.architecture)'."
}
$shell15 = @($assemblies | Where-Object {
    $_.name -eq 'Microsoft.VisualStudio.Shell.15.0' -and $_.version -match '^15\.'
})
if (-not $shell15.Count) {
    $errors += 'Microsoft.VisualStudio.Shell.15.0 assembly was not found.'
}
if ($dotNetRelease -lt 461808) {
    $errors += ".NET Framework 4.7.2 or newer is required (Release=$dotNetRelease)."
}
if (-not $webView.Count) {
    $warnings += 'Microsoft Edge WebView2 Runtime was not found; the panel will be blank until it is installed.'
}

$adsCandidates = @(
    "${env:ProgramFiles(x86)}\Beckhoff\TwinCAT\Common64\TcAdsDll.dll",
    "${env:ProgramFiles(x86)}\Beckhoff\TwinCAT\Common32\TcAdsDll.dll",
    'C:\TwinCAT\AdsApi\TcAdsDll\x64\TcAdsDll.dll',
    'C:\TwinCAT\AdsApi\TcAdsDll\TcAdsDll.dll'
) | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) }
if (-not $adsCandidates.Count) {
    $warnings += 'TcAdsDll.dll was not found in the known 4024/4026 locations.'
}

$dteRegistered = Test-Path 'Registry::HKEY_CLASSES_ROOT\TcXaeShell.DTE.15.0'
if (-not $dteRegistered) {
    $warnings += 'TcXaeShell.DTE.15.0 ProgID is not registered; COM tools may not connect.'
}

$report = [pscustomobject]@{
    generated_at = (Get-Date).ToString('o')
    computer = [Environment]::MachineName
    windows_version = [string]$windowsVersion
    compatible = ($errors.Count -eq 0)
    twincat = $tc
    xae_shell = $shell
    shell_root = $shellRoot
    assemblies = @($assemblies)
    dte_15_registered = [bool]$dteRegistered
    dotnet_release = $dotNetRelease
    webview2_executables = @($webView)
    ads_dlls = @($adsCandidates)
    errors = @($errors)
    warnings = @($warnings)
}

$fullOutput = [IO.Path]::GetFullPath($Output)
$parent = Split-Path $fullOutput -Parent
if ($parent -and -not (Test-Path -LiteralPath $parent)) {
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
}
$json = $report | ConvertTo-Json -Depth 8
[IO.File]::WriteAllText($fullOutput, $json, (New-Object Text.UTF8Encoding($false)))

Write-Host "TwinCAT:  $($tc.version)" -ForegroundColor Cyan
if ($shell) {
    Write-Host "XAE Shell: $($shell.path) [$($shell.architecture)]" -ForegroundColor Cyan
}
Write-Host "Compatible: $($report.compatible)" -ForegroundColor $(if ($report.compatible) { 'Green' } else { 'Red' })
foreach ($message in $errors) { Write-Host "ERROR: $message" -ForegroundColor Red }
foreach ($message in $warnings) { Write-Host "WARN:  $message" -ForegroundColor Yellow }
Write-Host "Report: $fullOutput" -ForegroundColor DarkGray

if ($Strict -and -not $report.compatible) { exit 2 }
