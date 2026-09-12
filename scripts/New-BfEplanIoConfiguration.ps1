param(
    [int]$PreferPid = 0,
    [switch]$AllowExisting
)

$ErrorActionPreference = 'Stop'
$tcComPath = Join-Path (Split-Path $PSScriptRoot -Parent) 'tc_template\TcCom.ps1'
. $tcComPath

$dte = Connect-Tc -PreferPid $PreferPid -Sticky ($PreferPid -gt 0)
$sys = $dte.Solution.Projects.Item(1).Object
$io = $sys.LookupTreeItem('TIID')

$result = [ordered]@{
    solution = [string]$dte.Solution.FullName
    master = ''
    created = @()
    skipped = @()
    failed = @()
    activated = $false
}

function Find-Child {
    param($Parent, [string]$Name)
    foreach ($child in $Parent) {
        if ([string]$child.Name -eq $Name) { return ,$child }
    }
    return $null
}

function Add-EtherCatItem {
    param(
        $Parent,
        [string]$Name,
        [int]$SubType,
        [string[]]$ProductCandidates,
        [string]$Before = ''
    )
    $existing = Find-Child $Parent $Name
    if ($null -ne $existing) {
        $script:result.skipped += $Name
        return ,$existing
    }

    $errors = @()
    if ($Before -and $null -eq (Find-Child $Parent $Before)) {
        $Before = ''
    }
    foreach ($product in $ProductCandidates) {
        try {
            $item = $Parent.CreateChild($Name, $SubType, $Before, $product)
            $script:result.created += [pscustomobject]@{
                name = $Name
                product = $product
                subtype = $SubType
            }
            return ,$item
        } catch {
            $errors += "$product`: $($_.Exception.Message)"
        }
    }
    $script:result.failed += [pscustomobject]@{
        name = $Name
        products = @($ProductCandidates)
        error = ($errors -join ' | ')
    }
    return $null
}

$physicalChildren = @()
foreach ($child in $io) {
    if ([string]$child.Name -notin @('Image', 'Image-Info', 'Inputs', 'Outputs', 'InfoData', 'SyncUnits')) {
        $physicalChildren += $child
    }
}

$masterName = 'Device 1 (EtherCAT)'
$master = Find-Child $io $masterName
if ($null -eq $master) {
    if ($physicalChildren.Count -gt 0 -and -not $AllowExisting) {
        throw "I/O tree is not empty. Re-run with -AllowExisting only after checking the existing devices."
    }
    $master = $io.CreateChild($masterName, 111, $null, $null)
    $result.created += [pscustomobject]@{
        name = $masterName
        product = 'EtherCAT Master'
        subtype = 111
    }
} else {
    $result.skipped += $masterName
}
$result.master = $masterName

# Page 600: EtherCAT master -> Rexroth drive -> EK1100 -> Staubli CPT.
$ek1100 = Add-EtherCatItem $master '-A220-K016 (EK1100)' 9099 @('EK1100')
if ($null -ne $ek1100) {
    $terminals = @(
        @{ Name = '-A220-K016V1 (EL1918)'; Product = 'EL1918'; SubType = 9099 },
        @{ Name = '-A220-K016V2 (EL1918)'; Product = 'EL1918'; SubType = 9099 },
        @{ Name = '-A220-K016T1 (EL2904)'; Product = 'EL2904'; SubType = 9099 },
        @{ Name = '-A220-K016A1 (EL1808)'; Product = 'EL1808'; SubType = 9099 },
        @{ Name = '-A220-K016A2 (EL1808)'; Product = 'EL1808'; SubType = 9099 },
        @{ Name = '-A220-K016A3 (EL1808)'; Product = 'EL1808'; SubType = 9099 },
        @{ Name = '-A220-K016A4 (EL1808)'; Product = 'EL1808'; SubType = 9099 },
        @{ Name = '-A220-K016B1 (EL2008)'; Product = 'EL2008'; SubType = 9099 },
        @{ Name = '-A220-K016B2 (EL2008)'; Product = 'EL2008'; SubType = 9099 },
        @{ Name = '-A220-K016B3 (EL2008)'; Product = 'EL2008'; SubType = 9099 },
        @{ Name = '-A220-K016E1 (EL4004)'; Product = 'EL4004'; SubType = 9099 },
        @{ Name = '-A220-K016K1 (EL6001)'; Product = 'EL6001'; SubType = 9101 },
        @{ Name = '-A220-K016Z1 (EK1122)'; Product = 'EK1122'; SubType = 9099 },
        @{ Name = '-A220-K016Z2 (EL9011)'; Product = 'EL9011'; SubType = 9099 }
    )
    foreach ($terminal in $terminals) {
        [void](Add-EtherCatItem $ek1100 $terminal.Name $terminal.SubType @($terminal.Product))
    }
}

[void](Add-EtherCatItem $master '-211-T711 (IndraDrive HCS01)' 9099 @(
    'HCS01.1E-W0018-A-03-B-ET-EC-EP-S4-HN-PW',
    'HCS01.1E-W0018-A-03-B-ET-EC-EP-S4',
    'HCS01'
) '-A220-K016 (EK1100)')

[void](Add-EtherCatItem $master '=140+M-A750 (Staubli CPT)' 9099 @(
    'CPT',
    'Computer-drawn unit CPT'
) '-K085S1 (EP1957-0022)')

# Pages 600b..601: field EtherCAT chain in drawing order.
$fieldItems = @(
    @{ Name = '+M-K020 (Aventics XV03-05)'; Products = @('XV03/05-XVES-EtherCAT', 'XV03-05-XVES-EtherCAT', 'XVES-EtherCAT'); SubType = 9099 },
    @{ Name = '-K085S1 (EP1957-0022)'; Products = @('EP1957-0022'); SubType = 9099 },
    @{ Name = '-K085S2 (EP1957-0022)'; Products = @('EP1957-0022'); SubType = 9099 },
    @{ Name = '=111-A830 (Balluff BIS V)'; Products = @('BIS V-6110-063-C002-SA13', 'BIS V-6110'); SubType = 9099 },
    @{ Name = '=141-A831 (Balluff BIS V)'; Products = @('BIS V-6110-063-C002-SA13', 'BIS V-6110'); SubType = 9099 },
    @{ Name = '=140+E-K016A1 (EP1018-0001)'; Products = @('EP1018-0001'); SubType = 9099 },
    @{ Name = '-K016A2 (EPP2624-0002)'; Products = @('EPP2624-0002'); SubType = 9099 },
    @{ Name = '-K016M1 (EPP2816-0010)'; Products = @('EPP2816-0010'); SubType = 9099 },
    @{ Name = '+E-K016A3 (EPP1018-0001)'; Products = @('EPP1018-0001'); SubType = 9099 },
    @{ Name = '+E-K016A4 (EPP1018-0001)'; Products = @('EPP1018-0001'); SubType = 9099 },
    @{ Name = '+E-K016A5 (EPP1018-0001)'; Products = @('EPP1018-0001'); SubType = 9099 },
    @{ Name = '+E-K016A6 (EPP1018-0001)'; Products = @('EPP1018-0001'); SubType = 9099 },
    @{ Name = '+E-K016A7 (EPP1018-0001)'; Products = @('EPP1018-0001'); SubType = 9099 },
    @{ Name = '+E-K016B1 (EPP2008-0001)'; Products = @('EPP2008-0001'); SubType = 9099 }
)
foreach ($item in $fieldItems) {
    $before = ''
    switch ($item.Name) {
        '+M-K020 (Aventics XV03-05)' { $before = '-K085S1 (EP1957-0022)' }
        '=111-A830 (Balluff BIS V)' { $before = '=140+E-K016A1 (EP1018-0001)' }
        '=141-A831 (Balluff BIS V)' { $before = '=140+E-K016A1 (EP1018-0001)' }
    }
    [void](Add-EtherCatItem $master $item.Name $item.SubType $item.Products $before)
}

$dte.ExecuteCommand('File.SaveAll')

[pscustomobject]$result | ConvertTo-Json -Depth 8
