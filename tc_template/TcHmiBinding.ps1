function Set-TcHmiPlcBinding {
    param([Parameter(Mandatory)]$Dte, [string]$ProjectName = '',
          [Parameter(Mandatory)][string]$Runtime,
          [Parameter(Mandatory)][string]$NetId,
          [Parameter(Mandatory)][int]$Port,
          [ValidateSet('default','both')][string]$Scope = 'default',
          [Parameter(Mandatory)][object]$Symbols,
          [object]$Definitions = $null, [bool]$Apply = $false)
    if (-not ($Runtime -match '^[A-Za-z_][A-Za-z0-9_.-]*$')) { throw "Invalid ADS Runtime name '$Runtime'." }
    if (-not ($NetId -match '^\d{1,3}(\.\d{1,3}){5}$') -or @($NetId.Split('.') | Where-Object { [int]$_ -gt 255 }).Count -gt 0) {
        throw "Invalid AMS NetId '$NetId'."
    }
    if ($Port -lt 1 -or $Port -gt 65535) { throw "Invalid ADS port '$Port'." }

    $project = Get-TcHmiProjectObject $Dte $ProjectName
    try { $Dte.ExecuteCommand('File.SaveAll') } catch { }
    $projectDisplayName = Get-TcHmiResolvedName $project
    $projectFile = Get-TcHmiResolvedFullName $project
    $root = Get-TcHmiProjectPath $project
    $scopes = if ($Scope -eq 'both') { @('default','remote') } else { @('default') }
    $plans = New-Object System.Collections.ArrayList
    $originals = [ordered]@{}

    foreach ($currentScope in $scopes) {
        $relative = "Server/ADS/ADS.Config.$currentScope.json"
        $path = Join-Path $root $relative.Replace('/','\')
        if (-not [IO.File]::Exists($path)) { throw "ADS configuration file not found: $relative" }
        $original = [IO.File]::ReadAllText($path, [Text.Encoding]::UTF8)
        $originals[$path] = $original
        try { $config = $original | ConvertFrom-Json } catch { throw "Invalid ADS configuration '$relative': $($_.Exception.Message)" }
        $runtimes = [ordered]@{}
        foreach ($property in @($config.RUNTIMES.PSObject.Properties)) { $runtimes[[string]$property.Name] = $property.Value }
        $existingKey = @($runtimes.Keys | Where-Object { ([string]$_).Equals($Runtime, [StringComparison]::OrdinalIgnoreCase) })
        if ($existingKey.Count -gt 1) { throw "ADS Runtime '$Runtime' is duplicated in $currentScope configuration." }
        $runtimeKey = if ($existingKey.Count -eq 1) { [string]$existingKey[0] } else { $Runtime }
        $before = if ($existingKey.Count -eq 1) { $runtimes[$runtimeKey] } else { $null }
        $mappedSymbols = if ($null -ne $before -and $null -ne $before.SYMBOLS) { $before.SYMBOLS } else { [pscustomobject]@{} }
        $enabled = if ($null -ne $before) { [bool]$before.ENABLED } else { $true }
        $readOnly = if ($null -ne $before) { [bool]$before.READ_ONLY } else { $false }
        $whitelist = if ($null -ne $before) { [bool]$before.USE_WHITELISTING } else { $false }
        $runtimes[$runtimeKey] = [pscustomobject][ordered]@{
            ENABLED=$enabled; NETID=$NetId; PORT=$Port; READ_ONLY=$readOnly
            SYMBOLS=$mappedSymbols; USE_WHITELISTING=$whitelist
        }
        $config.RUNTIMES = [pscustomobject]$runtimes
        [void]$plans.Add([pscustomobject]@{
            kind='ads-runtime'; scope=$currentScope; path=$path; before=$before
            after=$runtimes[$runtimeKey]; text=($config | ConvertTo-Json -Depth 100)
        })
    }

    $serverRelative = 'Server/TcHmiSrv/TcHmiSrv.Config.default.json'
    $serverPath = Join-Path $root $serverRelative.Replace('/','\')
    if (-not [IO.File]::Exists($serverPath)) { throw 'TcHmiSrv default configuration was not found.' }
    $serverOriginal = [IO.File]::ReadAllText($serverPath, [Text.Encoding]::UTF8)
    $originals[$serverPath] = $serverOriginal
    try { $serverConfig = $serverOriginal | ConvertFrom-Json } catch { throw "Invalid TcHmiSrv configuration: $($_.Exception.Message)" }
    $symbolMap = [ordered]@{}
    foreach ($property in @($serverConfig.SYMBOLS.PSObject.Properties)) { $symbolMap[[string]$property.Name] = $property.Value }
    $changedNames = New-Object System.Collections.ArrayList
    foreach ($property in @($Symbols.PSObject.Properties)) {
        $name = [string]$property.Name; $item = $property.Value
        if (-not $name.StartsWith("ADS.$Runtime.", [StringComparison]::Ordinal)) { throw "Dynamic symbol '$name' does not belong to Runtime '$Runtime'." }
        if ([string]$item.DOMAIN -cne 'ADS' -or -not [bool]$item.DYNAMIC -or -not [bool]$item.USEMAPPING) {
            throw "Dynamic symbol '$name' must use DOMAIN=ADS, DYNAMIC=true and USEMAPPING=true."
        }
        if (-not ([string]$item.MAPPING).StartsWith("$Runtime::", [StringComparison]::Ordinal)) { throw "Dynamic symbol '$name' has a mapping for another Runtime." }
        if ($null -eq $item.SCHEMA) { throw "Dynamic symbol '$name' requires a schema." }
        $symbolMap[$name] = $item
        [void]$changedNames.Add($name)
    }
    if ($changedNames.Count -eq 0) { throw 'At least one dynamic ADS symbol is required.' }
    $serverConfig.SYMBOLS = [pscustomobject]$symbolMap

    $definitionNames = New-Object System.Collections.ArrayList
    if ($null -ne $Definitions) {
        if ($null -eq $serverConfig.DEFINITIONS) { $serverConfig | Add-Member -NotePropertyName DEFINITIONS -NotePropertyValue ([pscustomobject]@{}) -Force }
        if ($null -eq $serverConfig.DEFINITIONS.ADS) { $serverConfig.DEFINITIONS | Add-Member -NotePropertyName ADS -NotePropertyValue ([pscustomobject]@{}) -Force }
        $definitionMap = [ordered]@{}
        foreach ($property in @($serverConfig.DEFINITIONS.ADS.PSObject.Properties)) { $definitionMap[[string]$property.Name] = $property.Value }
        foreach ($property in @($Definitions.PSObject.Properties)) {
            $name = [string]$property.Name
            if (-not $name.StartsWith("ADS-$Runtime.", [StringComparison]::Ordinal)) { throw "ADS definition '$name' does not belong to Runtime '$Runtime'." }
            $definitionMap[$name] = $property.Value
            [void]$definitionNames.Add($name)
        }
        $serverConfig.DEFINITIONS.ADS = [pscustomobject]$definitionMap
    }
    [void]$plans.Add([pscustomobject]@{
        kind='server-symbols'; scope='default'; path=$serverPath; before=$null
        after=$null; text=($serverConfig | ConvertTo-Json -Depth 100)
    })

    if (-not $Apply) {
        return [pscustomobject]@{
            status='preview'; project=$projectDisplayName; runtime=$Runtime; target_netid=$NetId
            ads_port=$Port; scope=$Scope; symbols=@($changedNames.ToArray())
            definitions=@($definitionNames.ToArray()); files=@($plans | Select-Object kind,scope,path)
            apply_required=$true; activation_performed=$false; server_restart_performed=$false
        }
    }

    $stamp = Get-Date -Format 'yyyyMMddHHmmssfff'
    $backups = New-Object System.Collections.ArrayList
    foreach ($path in $originals.Keys) {
        $backup = $path + '.agent-' + $stamp + '.bak'
        [IO.File]::Copy($path, $backup, $false)
        [void]$backups.Add($backup)
    }
    $stage = 'xae-remove-project'
    $filesRestored = $false; $projectReloaded = $false; $rollbackError = ''
    try {
        $Dte.Solution.Remove($project)
        $stage = 'xae-persist-removal'
        try { $Dte.ExecuteCommand('File.SaveAll') } catch { }
        Start-Sleep -Milliseconds 200
        $stage = 'file-write'
        foreach ($plan in $plans) {
            [IO.File]::WriteAllText([string]$plan.path, [string]$plan.text, (New-Object Text.UTF8Encoding($false)))
        }
        $stage = 'xae-reload'
        $project = $Dte.Solution.AddFromFile($projectFile, $false)
        if ($null -eq $project) { throw 'XAE did not reload the HMI project.' }
        $projectReloaded = $true
        $stage = 'xae-save-reloaded-project'
        try { $Dte.ExecuteCommand('File.SaveAll') } catch { }
        $stage = 'readback-runtime'
        foreach ($currentScope in $scopes) {
            $adsPath = Join-Path $root "Server\ADS\ADS.Config.$currentScope.json"
            $adsReadback = [IO.File]::ReadAllText($adsPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
            $runtimeProperty = @($adsReadback.RUNTIMES.PSObject.Properties | Where-Object { ([string]$_.Name).Equals($Runtime, [StringComparison]::OrdinalIgnoreCase) })
            if ($runtimeProperty.Count -ne 1 -or [string]$runtimeProperty[0].Value.NETID -cne $NetId -or [int]$runtimeProperty[0].Value.PORT -ne $Port) {
                throw "ADS Runtime '$Runtime' readback failed in $currentScope configuration."
            }
        }
        $stage = 'readback-symbols'
        $serverReadback = [IO.File]::ReadAllText($serverPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
        foreach ($name in @($changedNames.ToArray())) {
            $property = @($serverReadback.SYMBOLS.PSObject.Properties | Where-Object { [string]$_.Name -ceq $name })
            if ($property.Count -ne 1 -or [string]$property[0].Value.DOMAIN -cne 'ADS' -or -not [bool]$property[0].Value.DYNAMIC) { throw "Dynamic symbol readback failed: $name" }
        }
        foreach ($name in @($definitionNames.ToArray())) {
            if ($serverReadback.DEFINITIONS.ADS.PSObject.Properties.Name -cnotcontains $name) { throw "ADS definition readback failed: $name" }
        }
    } catch {
        $failure = $_.Exception.Message
        try {
            foreach ($path in $originals.Keys) {
                [IO.File]::WriteAllText([string]$path, [string]$originals[$path], (New-Object Text.UTF8Encoding($false)))
            }
            $filesRestored = $true
        } catch { $rollbackError = $_.Exception.Message }
        if (-not $projectReloaded) {
            try {
                $restoredProject = $Dte.Solution.AddFromFile($projectFile, $false)
                $projectReloaded = $null -ne $restoredProject
            } catch {
                if ($rollbackError) { $rollbackError += '; ' }
                $rollbackError += $_.Exception.Message
            }
        }
        return [pscustomobject]@{
            status='failed'; success=$false; verified=$false; stage=$stage; error=$failure
            project=$projectDisplayName; project_file=$projectFile; solution=[string]$Dte.Solution.FullName
            rollback=[pscustomobject]@{ files_restored=$filesRestored; project_reloaded=$projectReloaded; error=$rollbackError }
            backups=@($backups.ToArray()); retry_safe=($filesRestored -and $projectReloaded)
            next_action='Inspect the exact failed stage and rollback result before retrying. Never report the binding as applied.'
            activation_performed=$false; server_restart_performed=$false
        }
    }
    [pscustomobject]@{
        status='applied'; project=$projectDisplayName; runtime=$Runtime; target_netid=$NetId
        ads_port=$Port; scope=$Scope; symbol_count=$changedNames.Count
        definition_count=$definitionNames.Count; verified=$true; backups=@($backups.ToArray())
        load_mode='atomic-config-update-and-com-reload'; activation_performed=$false
        server_restart_performed=$false; plc_online_tested=$false; server_health_tested=$false
    }
}
