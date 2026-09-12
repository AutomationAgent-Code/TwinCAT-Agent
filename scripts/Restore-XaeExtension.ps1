# Restores the last known-good TcXaeShell extension after a failed extension update.
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$backup = 'C:\Users\Aurora Home Office\AppData\Local\Programs\TwinCAT Agent\.updates\backup-20260830-230100\tcxaeshell-extension'
$target = 'C:\Program Files (x86)\Beckhoff\TcXaeShell\Common7\IDE\Extensions\TwinCAT Agent'

if (Get-Process -Name 'TcXaeShell' -ErrorAction SilentlyContinue) {
    throw 'TcXaeShell is running. Close XAE before restoring the extension.'
}
foreach ($name in @('TwinCATAgent.Xae.dll', 'TwinCATAgent.Xae.pkgdef')) {
    $source = Join-Path $backup $name
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        throw "Recovery backup is missing: $source"
    }
    Copy-Item -LiteralPath $source -Destination (Join-Path $target $name) -Force
}
Write-Host 'TwinCAT Agent XAE extension restored. You can reopen XAE now.' -ForegroundColor Green
