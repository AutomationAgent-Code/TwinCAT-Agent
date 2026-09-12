# Narrow developer update: existing registered extension DLL only, no config/UI/runtime update.
[CmdletBinding()]
param([string]$ResultPath='')
$ErrorActionPreference='Stop'
$principal=New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if(-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){throw 'Run this update in the approved administrator process.'}
if (Get-Process TcXaeShell -ErrorAction SilentlyContinue) { throw 'Close XAE before updating the extension binary.' }
$repo=Split-Path $PSScriptRoot -Parent
$source=Join-Path $repo 'tc_agent_vsix\bin\Release\TwinCATAgent.Xae.dll'
$options=Get-Content -LiteralPath (Join-Path $env:LOCALAPPDATA 'Programs\TwinCAT Agent\install_options.json') -Raw | ConvertFrom-Json
$shellExe=[IO.Path]::GetFullPath([string]$options.xae_shell)
$shellRoot=Split-Path (Split-Path (Split-Path $shellExe -Parent) -Parent) -Parent
$machineDir=Join-Path (Split-Path $shellExe -Parent) 'Extensions\TwinCAT Agent'
if($shellExe -ine 'C:\Program Files (x86)\Beckhoff\TcXaeShell\Common7\IDE\TcXaeShell.exe'){throw 'This developer update requires the verified x86 TcXaeShell.'}
$targets=@(
    (Join-Path $machineDir 'TwinCATAgent.Xae.dll'),
    (Join-Path $env:LOCALAPPDATA 'Programs\TwinCAT Agent\extension\TwinCATAgent.Xae.dll')
)
$protected=@(
    (Join-Path $env:LOCALAPPDATA 'TwinCAT Agent\config.json'),
    (Join-Path $env:LOCALAPPDATA 'Programs\TwinCAT Agent\app\tc_agent\config.json')
)
$before=@{}
foreach($path in $protected) {
    $before[$path]=if(Test-Path -LiteralPath $path){(Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash}else{'missing'}
}
$sourceIdentity=[Reflection.AssemblyName]::GetAssemblyName($source).FullName
foreach($target in $targets) {
    if(-not (Test-Path -LiteralPath $target -PathType Leaf)){throw "Existing extension not found: $target"}
    if([Reflection.AssemblyName]::GetAssemblyName($target).FullName -ne $sourceIdentity){throw 'Assembly identity differs; use the full reviewed extension upgrade instead.'}
}
$backup=Join-Path $env:LOCALAPPDATA ('Programs\TwinCAT Agent\.updates\hmi-binary-'+(Get-Date -Format 'yyyyMMdd-HHmmss')+'-'+[guid]::NewGuid().ToString('N'))
[void](New-Item -ItemType Directory -Path $backup)
for($index=0;$index -lt $targets.Count;$index++) {
    Copy-Item -LiteralPath $targets[$index] -Destination (Join-Path $backup ("original-$index.dll"))
}
$changed=New-Object System.Collections.ArrayList
try {
    if (Get-Process TcXaeShell -ErrorAction SilentlyContinue) { throw 'XAE reopened; update cancelled before writing.' }
    $expected=(Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash
    $stage=Join-Path $backup 'stage'
    [void](New-Item -ItemType Directory -Path $stage)
    foreach($name in @('TwinCATAgent.Xae.pkgdef','extension.vsixmanifest')){Copy-Item -LiteralPath (Join-Path $machineDir $name) -Destination (Join-Path $stage $name)}
    Copy-Item -LiteralPath $source -Destination (Join-Path $stage 'TwinCATAgent.Xae.dll')
    # Use the existing installer contract; registration files remain byte-identical.
    $code=@'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / 'packaging' / 'installer'))
from installer_common import install_extension_native
install_extension_native(Path(sys.argv[2]), Path(sys.argv[3]), refresh_cache=True)
'@
    [void]$changed.Add(0)
    & 'C:\Users\Aurora Home Office\AppData\Local\Programs\Python\Python314\python.exe' -c $code $repo $stage $shellRoot
    if($LASTEXITCODE -ne 0){throw 'Native extension install/cache refresh failed.'}
    [void]$changed.Add(1)
    Copy-Item -LiteralPath $source -Destination $targets[1] -Force
    foreach($target in $targets){if((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash -ne $expected){throw 'Installed extension hash mismatch.'}}
    foreach($path in $protected) {
        $after=if(Test-Path -LiteralPath $path){(Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash}else{'missing'}
        if($before[$path] -ne $after){throw 'Configuration changed during update; no automatic config overwrite is allowed.'}
    }
    $report=@{status='updated';backup_path=$backup;targets=$targets;sha256=$expected;config_unchanged=$true;
      backend_updated=$false;requires_xae_restart=$true;cache_refreshed=$true} | ConvertTo-Json -Depth 4
    if($ResultPath){[IO.File]::WriteAllText($ResultPath,$report)}
    $report
} catch {
    $failure=$_.Exception.Message
    $rollbackErrors=@()
    foreach($index in $changed){try{Copy-Item -LiteralPath (Join-Path $backup ("original-$index.dll")) -Destination $targets[$index] -Force}catch{$rollbackErrors+=$_.Exception.Message}}
    if($ResultPath){[IO.File]::WriteAllText($ResultPath,(@{status='failed';error=$failure;backup_path=$backup;rollback_errors=$rollbackErrors}|ConvertTo-Json -Depth 4))}
    throw $failure
}
