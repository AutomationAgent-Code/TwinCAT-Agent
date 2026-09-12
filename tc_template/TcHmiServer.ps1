function Get-TcHmiServerLaunchPlan {
    param([Parameter(Mandatory)]$Dte, [string]$ProjectName = '')
    $project = Get-TcHmiProjectObject $Dte $ProjectName
    $projectFile = Get-TcHmiResolvedFullName $project
    $root = Get-TcHmiProjectPath $project
    try { [xml]$projectXml = [IO.File]::ReadAllText($projectFile, [Text.Encoding]::UTF8) }
    catch { throw "Invalid HMI project file: $($_.Exception.Message)" }
    function Project-Value([string]$Name) {
        $node = $projectXml.SelectSingleNode("//*[local-name()='$Name']")
        return $(if ($null -ne $node) { [string]$node.InnerText } else { '' })
    }
    $serverPort = [int](Project-Value 'HmiCommunicationServerPort')
    $authPort = [int](Project-Value 'HmiCommunicationServerAuthPort')
    if ($serverPort -lt 1 -or $serverPort -gt 65535) { throw 'HmiCommunicationServerPort is missing or invalid.' }
    if ($authPort -lt 1 -or $authPort -gt 65535) { throw 'HmiCommunicationServerAuthPort is missing or invalid.' }
    $useX64 = (Project-Value 'HmiUseX64') -notmatch '^(?i:false|0)$'
    $packagesConfig = Join-Path $root 'packages.config'
    if (-not [IO.File]::Exists($packagesConfig)) { throw "HMI packages.config is missing: $packagesConfig" }
    try { [xml]$packagesXml = [IO.File]::ReadAllText($packagesConfig, [Text.Encoding]::UTF8) }
    catch { throw "Invalid HMI packages.config: $($_.Exception.Message)" }
    $package = @($packagesXml.SelectNodes("//*[local-name()='package']") | Where-Object {
        [string]$_.id -ceq 'Beckhoff.TwinCAT.HMI.Server.Engineering'
    })
    if ($package.Count -ne 1) { throw 'Exactly one Beckhoff.TwinCAT.HMI.Server.Engineering package is required.' }
    $version = [string]$package[0].version
    $solutionRoot = [IO.Path]::GetDirectoryName([string]$Dte.Solution.FullName)
    $packageRoot = Join-Path $solutionRoot ("Packages\Beckhoff.TwinCAT.HMI.Server.Engineering.$version")
    $platform = if ($useX64) { 'win-x64' } else { 'win-x86' }
    $extensionDirectory = Join-Path $packageRoot "runtimes\$platform\native"
    $executable = Join-Path $extensionDirectory 'TcHmiSrv.exe'
    if (-not [IO.File]::Exists($executable)) { throw "HMI Engineering Server executable was not found: $executable" }
    $endpoint = "http://127.0.0.1:$serverPort"
    $authEndpoint = "http://127.0.0.1:$authPort"
    $arguments = @(
        "--endpoint=`"$endpoint`"", "--forceAuthEndpoint=`"$authEndpoint`"",
        '--requireAuth=0', '--silent=true', '--creator=true', '--storage=TcHmiTextStorage',
        "--storageDir=`"$root`"", '--storageName=Server',
        "--extensionDir=`"all=$extensionDirectory`""
    )
    [pscustomobject]@{
        project=Get-TcHmiResolvedName $project; project_file=$projectFile; project_root=$root
        executable=$executable; extension_directory=$extensionDirectory; package_version=$version
        platform=$platform; endpoint=$endpoint; auth_endpoint=$authEndpoint
        server_port=$serverPort; auth_port=$authPort; arguments=[object[]]$arguments
    }
}

function Invoke-TcHmiServerControl {
    param([Parameter(Mandatory)]$Dte, [string]$ProjectName = '',
          [ValidateSet('start','stop','restart')][string]$Action,
          [bool]$Apply = $false)
    $plan = Get-TcHmiServerLaunchPlan $Dte $ProjectName
    $before = Get-TcHmiRuntimeInfo $Dte $plan.project_file
    $targetPids = @($before.processes | ForEach-Object { [int]$_.process_id })
    if (-not $Apply) {
        return [pscustomobject]@{
            status='preview'; action=$Action; project=$plan.project
            server_running=[bool]$before.server_running; application_ready=[bool]$before.application_ready
            process_ids=$targetPids; executable=$plan.executable; endpoint=$plan.endpoint
            auth_endpoint=$plan.auth_endpoint; apply_required=$true
            exact_project_process_scope=$true; build_performed=$false; publish_performed=$false
        }
    }

    $stopped = New-Object System.Collections.ArrayList
    $recoverUnready = $Action -eq 'start' -and $before.server_running -and -not $before.application_ready
    if (($Action -in @('stop','restart') -or $recoverUnready) -and $targetPids.Count -gt 0) {
        foreach ($processId in $targetPids) {
            $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
            if ($null -ne $process) {
                Stop-Process -Id $processId -Force -ErrorAction Stop
                [void]$stopped.Add($processId)
            }
        }
        for ($attempt=0; $attempt -lt 40; $attempt++) {
            $remaining = @($targetPids | Where-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue })
            if ($remaining.Count -eq 0) { break }
            Start-Sleep -Milliseconds 100
        }
        $remaining = @($targetPids | Where-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue })
        if ($remaining.Count -gt 0) { throw "HMI Engineering Server did not stop: $($remaining -join ', ')" }
    }
    if ($Action -eq 'stop') {
        $after = Get-TcHmiRuntimeInfo $Dte $plan.project_file
        return [pscustomobject]@{
            status=if($after.server_running){'stop-failed'}else{'stopped'}; action=$Action
            project=$plan.project; stopped_process_ids=@($stopped.ToArray())
            verified=(-not [bool]$after.server_running); application_ready=[bool]$after.application_ready
            build_performed=$false; publish_performed=$false
        }
    }

    if ($Action -eq 'start' -and $before.application_ready) {
        return [pscustomobject]@{
            status='already-running'; action=$Action; project=$plan.project
            process_ids=$targetPids; verified=[bool]$before.application_ready
            application_url=[string]$before.application_url; start_performed=$false
            build_performed=$false; publish_performed=$false
        }
    }

    $listeners = @()
    try {
        $listeners = @(Get-NetTCPConnection -State Listen -ErrorAction Stop | Where-Object {
            [int]$_.LocalPort -in @($plan.server_port,$plan.auth_port)
        })
    } catch { }
    if ($listeners.Count -gt 0) {
        $owners = @($listeners | Select-Object LocalAddress,LocalPort,OwningProcess -Unique)
        throw "HMI ports are already occupied after stopping the selected project: $($owners | ConvertTo-Json -Compress)"
    }

    # The Engineering Server process alone does not receive the in-memory
    # project configuration and serves /bin as 404.  Reloading the exact HMI
    # project lets the TE2000 project system launch and hydrate its server
    # without selecting a UI node or stealing focus.
    $project = Get-TcHmiProjectObject $Dte $plan.project_file
    $null = Reload-TcHmiProject $Dte $project $plan.project_file $true
    $probeCandidates = @($before.probes | Where-Object {
        ([string]$_.url).StartsWith($plan.endpoint, [StringComparison]::OrdinalIgnoreCase) -and
        ([string]$_.url) -match '/bin/'
    })
    $probeUrl = if ($probeCandidates.Count -gt 0) { [string]$probeCandidates[0].url } else { $plan.endpoint + '/bin/Default.html' }
    for ($attempt=0; $attempt -lt 40; $attempt++) {
        try {
            $response = Invoke-WebRequest -Uri $probeUrl -UseBasicParsing -TimeoutSec 1
            if ([int]$response.StatusCode -ge 200 -and [int]$response.StatusCode -lt 400 -and
                ([string]$response.Content).Contains('TCHMI_RUNTIME')) { break }
        } catch { }
        Start-Sleep -Milliseconds 250
    }
    $after = Get-TcHmiRuntimeInfo $Dte $plan.project_file
    $startedPids = @($after.processes | ForEach-Object { [int]$_.process_id } | Where-Object { $targetPids -notcontains $_ })
    [pscustomobject]@{
        status=if($after.application_ready){'started'}elseif($after.license_expired){'license-expired'}elseif($after.server_running){'server-running-app-unavailable'}else{'start-failed'}
        action=$Action; project=$plan.project; process_ids=$startedPids
        stopped_process_ids=@($stopped.ToArray()); server_running=[bool]$after.server_running
        application_ready=[bool]$after.application_ready; application_url=[string]$after.application_url
        license_expired=[bool]$after.license_expired; license_state=[string]$after.license_state
        verified=[bool]$after.application_ready; executable=$plan.executable
        endpoint=$plan.endpoint; start_strategy='xae-project-reload'
        recovered_unready_server=$recoverUnready
        build_performed=$false; publish_performed=$false
    }
}
