param(
    [Parameter(Mandatory)]
    [ValidateSet('check-manifest', 'esi-check', 'create', 'validate', 'export', 'remove', 'remove-master')]
    [string]$Command,

    [string]$Manifest,
    [string]$Output,
    [string]$TcComPath,
    [int]$PreferPid = 0,
    [switch]$AllowExisting,
    [switch]$ConfirmRemove,
    [switch]$AllowWithChildren
)

$ErrorActionPreference = 'Stop'
$script:MetaNames = @(
    'Image', 'Image-Info', 'Process Image', 'Process Image-Info',
    'Inputs', 'Outputs', 'InfoData', 'SyncUnits', '<default>'
)

function Write-JsonResult {
    param([Parameter(Mandatory)]$Value)
    $json = $Value | ConvertTo-Json -Depth 20 -Compress
    $builder = New-Object Text.StringBuilder
    foreach ($char in $json.ToCharArray()) {
        $code = [int][char]$char
        if ($code -gt 127) { [void]$builder.Append(('\u{0:x4}' -f $code)) }
        else { [void]$builder.Append($char) }
    }
    $builder.ToString()
}

function Read-Manifest {
    param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Manifest not found: $Path"
    }
    if ([IO.Path]::GetExtension($Path) -ne '.json') {
        throw 'The tool accepts JSON manifests only; this keeps the PowerShell path dependency-free.'
    }
    $raw = Get-Content -LiteralPath $Path -Raw -Encoding UTF8
    if (-not $raw.Trim()) { throw "Manifest is empty: $Path" }
    return ($raw | ConvertFrom-Json)
}

function Test-Manifest {
    param([Parameter(Mandatory)]$Data)
    $errors = @()
    $warnings = @()

    if ([int]$Data.schema_version -ne 1) {
        $errors += 'schema_version must be 1.'
    }
    if ($null -eq $Data.master -or -not [string]$Data.master.name) {
        $errors += 'master.name is required.'
    }
    if ($null -eq $Data.devices) {
        $errors += 'devices array is required.'
    }

    $ids = @{}
    $names = @{}
    foreach ($device in @($Data.devices)) {
        $id = [string]$device.id
        $name = [string]$device.name
        $parent = [string]$device.parent
        $products = @($device.product_candidates)
        if (-not $id) { $errors += "Device '$name' has no id."; continue }
        if ($ids.ContainsKey($id)) { $errors += "Duplicate device id: $id" }
        else { $ids[$id] = $true }
        if (-not $name) { $errors += "Device '$id' has no name." }
        elseif ($names.ContainsKey($name)) {
            $warnings += "Duplicate display name '$name'; ids remain authoritative."
        } else { $names[$name] = $true }
        if (-not $parent) { $errors += "Device '$id' has no parent." }
        if ($products.Count -eq 0 -or -not [string]$products[0]) {
            $errors += "Device '$id' has no product_candidates."
        }
        if ($null -ne $device.subtype -and [int]$device.subtype -lt 1) {
            $errors += "Device '$id' has invalid subtype."
        }
    }

    foreach ($device in @($Data.devices)) {
        $parent = [string]$device.parent
        if ($parent -ne '$master' -and -not $ids.ContainsKey($parent)) {
            $errors += "Device '$($device.id)' references unknown parent '$parent'."
        }
        $before = [string]$device.before
        if ($before -and -not $ids.ContainsKey($before)) {
            $errors += "Device '$($device.id)' references unknown before id '$before'."
        }
    }

    # Parent entries must precede their children so creation is deterministic.
    $seen = @{ '$master' = $true }
    foreach ($device in @($Data.devices)) {
        $parent = [string]$device.parent
        if (-not $seen.ContainsKey($parent)) {
            $errors += "Parent '$parent' must appear before child '$($device.id)'."
        }
        $seen[[string]$device.id] = $true
    }

    [pscustomobject]@{
        ok = ($errors.Count -eq 0)
        errors = @($errors)
        warnings = @($warnings)
        device_count = @($Data.devices).Count
    }
}

function Resolve-EsiRoot {
    param($Data)
    $candidates = @()
    if ($Data.esi_roots) { $candidates += @($Data.esi_roots) }
    $candidates += @(
        'C:\Program Files (x86)\Beckhoff\TwinCAT\3.1\Config\Io\EtherCAT',
        'C:\ProgramData\Beckhoff\TwinCAT\3.1\Config\Io\EtherCAT',
        'C:\TwinCAT\3.1\Config\Io\EtherCAT'
    )
    @($candidates | Where-Object { Test-Path -LiteralPath $_ -PathType Container } |
        Select-Object -Unique)
}

function Test-EsiProducts {
    param([Parameter(Mandatory)]$Data)
    $roots = Resolve-EsiRoot $Data
    $files = @()
    foreach ($root in $roots) {
        $files += Get-ChildItem -LiteralPath $root -File -Filter '*.xml' -ErrorAction SilentlyContinue
    }
    $cache = @{}
    $found = @()
    $missing = @()
    foreach ($device in @($Data.devices)) {
        $hits = @()
        foreach ($product in @($device.product_candidates)) {
            foreach ($file in $files) {
                if (-not $cache.ContainsKey($file.FullName)) {
                    $cache[$file.FullName] = Get-Content -LiteralPath $file.FullName -Raw -ErrorAction SilentlyContinue
                }
                if ($cache[$file.FullName] -match [regex]::Escape([string]$product)) {
                    $hits += [pscustomobject]@{
                        product = [string]$product
                        file = $file.FullName
                    }
                }
            }
        }
        $record = [pscustomobject]@{
            id = [string]$device.id
            name = [string]$device.name
            matches = @($hits | Sort-Object product, file -Unique)
        }
        if ($hits.Count) { $found += $record } else { $missing += $record }
    }
    [pscustomobject]@{
        roots = @($roots)
        esi_file_count = $files.Count
        found = @($found)
        missing = @($missing)
        ok = ($missing.Count -eq 0)
    }
}

function Resolve-TcCom {
    param([string]$Explicit)
    $candidates = @()
    if ($Explicit) { $candidates += $Explicit }
    $candidates += @(
        (Join-Path $PSScriptRoot 'TcCom.ps1'),
        (Join-Path (Split-Path $PSScriptRoot -Parent) 'tc_template\TcCom.ps1'),
        (Join-Path (Get-Location) 'tc_template\TcCom.ps1')
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    throw 'TcCom.ps1 was not found. Pass -TcComPath explicitly.'
}

function Connect-Configuration {
    param([int]$PidPreference, [string]$ComPath)
    . $ComPath
    $dte = Connect-Tc -PreferPid $PidPreference -Sticky ($PidPreference -gt 0) `
        -StrictPid ($PidPreference -gt 0)
    return ,$dte
}

function Get-ConfigurationSystemManager {
    param([Parameter(Mandatory)]$Dte)
    $count = 0
    try { $count = [int]$Dte.Solution.Projects.Count } catch { }
    for ($i = 1; $i -le $count; $i++) {
        try {
            $sys = $Dte.Solution.Projects.Item($i).Object
            $sys.LookupTreeItem('TIID') | Out-Null
            return ,$sys
        } catch { }
    }
    throw 'TwinCAT System Manager project not found in the current solution.'
}

function Find-Child {
    param($Parent, [string]$Name)
    foreach ($child in $Parent) {
        if ([string]$child.Name -eq $Name) { return ,$child }
    }
    return $null
}

function Get-PhysicalChildren {
    param($Parent)
    $items = @()
    foreach ($child in $Parent) {
        if ([string]$child.Name -notin $script:MetaNames) { $items += $child }
    }
    return ,$items
}

function New-Configuration {
    param($Data, $Dte, [bool]$PermitExisting)
    $sys = Get-ConfigurationSystemManager $Dte
    $io = $sys.LookupTreeItem('TIID')
    $created = @()
    $skipped = @()
    $failed = @()
    $nodes = @{}

    $masterName = [string]$Data.master.name
    $master = Find-Child $io $masterName
    if ($null -eq $master) {
        if ((Get-PhysicalChildren $io).Count -gt 0 -and -not $PermitExisting) {
            throw 'I/O tree is not empty. Use -AllowExisting only after reviewing it.'
        }
        $subtype = if ($Data.master.subtype) { [int]$Data.master.subtype } else { 111 }
        $master = $io.CreateChild($masterName, $subtype, $null, $null)
        $created += [pscustomobject]@{ id = '$master'; name = $masterName; product = 'EtherCAT Master' }
    } else {
        $skipped += [pscustomobject]@{ id = '$master'; name = $masterName }
    }
    $nodes['$master'] = $master

    foreach ($device in @($Data.devices)) {
        $id = [string]$device.id
        $name = [string]$device.name
        $parentId = [string]$device.parent
        $parent = $nodes[$parentId]
        if ($null -eq $parent) {
            $failed += [pscustomobject]@{ id = $id; name = $name; error = "Parent '$parentId' unavailable." }
            continue
        }
        $existing = Find-Child $parent $name
        if ($null -ne $existing) {
            $nodes[$id] = $existing
            $skipped += [pscustomobject]@{ id = $id; name = $name }
            continue
        }

        $beforeName = ''
        if ([string]$device.before) {
            $beforeDevice = @($Data.devices) | Where-Object { [string]$_.id -eq [string]$device.before } |
                Select-Object -First 1
            if ($beforeDevice -and $null -ne (Find-Child $parent ([string]$beforeDevice.name))) {
                $beforeName = [string]$beforeDevice.name
            }
        }

        $subtype = if ($device.subtype) { [int]$device.subtype } else { 9099 }
        $errors = @()
        foreach ($product in @($device.product_candidates)) {
            try {
                $node = $parent.CreateChild($name, $subtype, $beforeName, [string]$product)
                $nodes[$id] = $node
                $created += [pscustomobject]@{
                    id = $id; name = $name; product = [string]$product; subtype = $subtype
                }
                break
            } catch {
                $errors += "$product`: $($_.Exception.Message)"
            }
        }
        if (-not $nodes.ContainsKey($id)) {
            $failed += [pscustomobject]@{
                id = $id; name = $name; error = ($errors -join ' | ')
            }
        }
    }

    $Dte.ExecuteCommand('File.SaveAll')
    [pscustomobject]@{
        solution = [string]$Dte.Solution.FullName
        created = @($created)
        skipped = @($skipped)
        failed = @($failed)
        activated = $false
        scanned = $false
    }
}

function Compare-Configuration {
    param($Data, $Dte)
    $sys = Get-ConfigurationSystemManager $Dte
    $io = $sys.LookupTreeItem('TIID')
    $nodes = @{}
    $missing = @()
    $present = @()
    $order_mismatches = @()

    $master = Find-Child $io ([string]$Data.master.name)
    if ($null -eq $master) {
        return [pscustomobject]@{
            ok = $false
            missing = @('$master')
            present = @()
            order_mismatches = @()
        }
    }
    $nodes['$master'] = $master

    foreach ($device in @($Data.devices)) {
        $id = [string]$device.id
        $parent = $nodes[[string]$device.parent]
        if ($null -eq $parent) { $missing += $id; continue }
        $node = Find-Child $parent ([string]$device.name)
        if ($null -eq $node) { $missing += $id; continue }
        $nodes[$id] = $node
        $present += $id
    }

    $groups = @($Data.devices) | Group-Object parent
    foreach ($group in $groups) {
        $parent = $nodes[[string]$group.Name]
        if ($null -eq $parent) { continue }
        $actual = @((Get-PhysicalChildren $parent) | ForEach-Object { [string]$_.Name })
        $expected = @($group.Group | ForEach-Object { [string]$_.name })
        $actualRelevant = @($actual | Where-Object { $_ -in $expected })
        $expectedPresent = @($expected | Where-Object { $_ -in $actual })
        if (($actualRelevant -join "`n") -ne ($expectedPresent -join "`n")) {
            $order_mismatches += [pscustomobject]@{
                parent = [string]$group.Name
                expected = $expectedPresent
                actual = $actualRelevant
            }
        }
    }
    [pscustomobject]@{
        ok = ($missing.Count -eq 0 -and $order_mismatches.Count -eq 0)
        missing = @($missing)
        present = @($present)
        order_mismatches = @($order_mismatches)
    }
}

function Export-Configuration {
    param($Dte)
    $sys = Get-ConfigurationSystemManager $Dte
    $io = $sys.LookupTreeItem('TIID')
    $masters = Get-PhysicalChildren $io
    if ($masters.Count -ne 1) {
        throw "Export requires exactly one configured I/O master; found $($masters.Count)."
    }
    $master = $masters[0]
    $script:IoExportDevices = @()
    $script:IoExportUsedIds = @{}

    function Make-Id {
        param([string]$Name)
        $base = ($Name.ToLowerInvariant() -replace '[^a-z0-9]+', '-').Trim('-')
        if (-not $base) { $base = 'device' }
        $id = $base
        $n = 2
        while ($script:IoExportUsedIds.ContainsKey($id)) { $id = "$base-$n"; $n++ }
        $script:IoExportUsedIds[$id] = $true
        return $id
    }

    function Walk-Items {
        param($Parent, [string]$ParentId)
        foreach ($child in (Get-PhysicalChildren $Parent)) {
            $name = [string]$child.Name
            $id = Make-Id $name
            $product = ''
            try {
                [xml]$xml = $child.ProduceXml()
                $product = [string]$xml.TreeItem.EtherCAT.Slave.Info.ProductRevision
            } catch { }
            if (-not $product) { $product = [string]$child.ItemSubTypeName }
            $script:IoExportDevices += [pscustomobject]@{
                id = $id
                name = $name
                parent = $ParentId
                subtype = [int]$child.ItemSubType
                product_candidates = @($product)
            }
            Walk-Items $child $id
        }
    }

    Walk-Items $master '$master'
    [pscustomobject]@{
        schema_version = 1
        master = [pscustomobject]@{
            name = [string]$master.Name
            subtype = [int]$master.ItemSubType
        }
        devices = @($script:IoExportDevices)
        policy = [pscustomobject]@{
            scan = $false
            activate = $false
            restart = $false
        }
    }
}

function Remove-ConfigurationItems {
    param($Data, $Dte, [bool]$Confirmed)
    if (-not $Confirmed) {
        throw 'Removal requires -ConfirmRemove.'
    }
    $sys = Get-ConfigurationSystemManager $Dte
    $io = $sys.LookupTreeItem('TIID')
    $master = Find-Child $io ([string]$Data.master.name)
    if ($null -eq $master) {
        return [pscustomobject]@{ removed = @(); missing = @('$master'); activated = $false }
    }
    $nodes = @{ '$master' = $master }
    foreach ($device in @($Data.devices)) {
        $parent = $nodes[[string]$device.parent]
        if ($null -ne $parent) {
            $child = Find-Child $parent ([string]$device.name)
            if ($null -ne $child) { $nodes[[string]$device.id] = $child }
        }
    }

    $removed = @()
    $missing = @()
    $reversed = @($Data.devices)
    [array]::Reverse($reversed)
    foreach ($device in $reversed) {
        $parent = $nodes[[string]$device.parent]
        if ($null -eq $parent -or $null -eq (Find-Child $parent ([string]$device.name))) {
            $missing += [string]$device.id
            continue
        }
        $parent.DeleteChild([string]$device.name)
        $removed += [string]$device.id
    }
    $Dte.ExecuteCommand('File.SaveAll')
    [pscustomobject]@{
        removed = @($removed)
        missing = @($missing)
        master_preserved = $true
        activated = $false
    }
}

$needsManifest = $Command -in @('check-manifest', 'esi-check', 'create', 'validate', 'remove')
$data = if ($needsManifest) { Read-Manifest $Manifest } else { $null }
if ($data) {
    $manifestCheck = Test-Manifest $data
    if (-not $manifestCheck.ok) {
        Write-JsonResult ([pscustomobject]@{
            command = $Command
            status = 'invalid-manifest'
            validation = $manifestCheck
        })
        exit 2
    }
}

switch ($Command) {
    'check-manifest' {
        Write-JsonResult ([pscustomobject]@{ command = $Command; status = 'ok'; validation = $manifestCheck })
    }
    'esi-check' {
        Write-JsonResult ([pscustomobject]@{ command = $Command; status = 'ok'; result = (Test-EsiProducts $data) })
    }
    default {
        $resolvedTcCom = Resolve-TcCom $TcComPath
        $dte = Connect-Configuration $PreferPid $resolvedTcCom
        switch ($Command) {
            'create' {
                $result = New-Configuration $data $dte ([bool]$AllowExisting)
            }
            'validate' {
                $result = Compare-Configuration $data $dte
            }
            'export' {
                $result = Export-Configuration $dte
                if ($Output) {
                    $json = $result | ConvertTo-Json -Depth 20
                    [IO.File]::WriteAllText(
                        [IO.Path]::GetFullPath($Output),
                        $json,
                        (New-Object Text.UTF8Encoding($false))
                    )
                }
            }
            'remove' {
                $result = Remove-ConfigurationItems $data $dte ([bool]$ConfirmRemove)
            }
        }
        Write-JsonResult ([pscustomobject]@{
            command = $Command
            status = 'ok'
            result = $result
        })
    }
}
