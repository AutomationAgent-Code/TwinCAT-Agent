[CmdletBinding()]
param(
    [string]$OutputPath = '',
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'
$Repo = Split-Path $PSScriptRoot -Parent
if (-not $OutputPath) {
    $OutputPath = Join-Path $Repo 'tc_agent\bin\TcAdsDynamicBridge.exe'
}
$source = Join-Path $Repo 'tools\TcAdsDynamicProbe.cs'
$windowsRoot = if ($env:WINDIR) { $env:WINDIR } else { $env:SystemRoot }
$cscCandidates = @(
    (Join-Path $windowsRoot 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'),
    (Join-Path $windowsRoot 'Microsoft.NET\Framework\v4.0.30319\csc.exe')
)
$csc = $cscCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $csc) { throw '.NET Framework 4 C# compiler was not found.' }

$roots = @(
    (Join-Path ${env:ProgramFiles(x86)} 'Beckhoff\TwinCAT\3.1\Components\Plc\LacBinaries\GAC_MSIL\TwinCAT.Ads'),
    (Join-Path ${env:ProgramFiles(x86)} 'Beckhoff\TwinCAT\3.1\Components\Base')
)
$adsDll = $null
foreach ($root in $roots) {
    if (Test-Path -LiteralPath $root) {
        $adsDll = Get-ChildItem -LiteralPath $root -Filter TwinCAT.Ads.dll -Recurse -File |
            Sort-Object FullName | Select-Object -Last 1 -ExpandProperty FullName
        if ($adsDll) { break }
    }
}
if (-not $adsDll) { throw 'TwinCAT.Ads.dll was not found in the local TwinCAT installation.' }

$outputDir = Split-Path $OutputPath -Parent
New-Item -ItemType Directory -Path $outputDir -Force | Out-Null
& $csc /nologo /optimize+ /target:exe "/out:$OutputPath" "/reference:$adsDll" `
    /reference:System.Web.Extensions.dll $source
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $OutputPath)) {
    throw 'Failed to compile TcAdsDynamicBridge.exe.'
}
if (-not $Quiet) { Write-Host "Dynamic ADS bridge: $OutputPath" -ForegroundColor Green }
