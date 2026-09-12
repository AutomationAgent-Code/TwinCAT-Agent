# Official TE2000 ITcHmiProject operation surface.
#
# This module deliberately calls the installed TcHmiAutomation assembly.  It
# does not edit tchmiconfig.json or project markup as a fallback.  Mutating
# operations are preview-only unless -Apply is supplied; every applied
# operation returns a small native readback summary.

function Get-TcHmiProjectNativeContext {
    param($Dte, [string]$ProjectName)
    $project = Get-TcHmiProjectObject $Dte $ProjectName
    $projectFile = Get-TcHmiResolvedFullName $project
    $root = [IO.Path]::GetDirectoryName($projectFile)
    # Reuse the tested native loader and its assembly/reference checks.
    [void](Invoke-TcHmiNativeItem -Dte $Dte -ProjectName $ProjectName -Request @{probe_only=$true})
    if (-not ('TcAgentHmiProjectApi' -as [type])) {
        $assembly = Join-Path ${env:ProgramFiles(x86)} 'Beckhoff\TwinCAT\Functions\TE2000-HMI-Engineering\VisualStudio\TcXaeShell\TcHmiAutomation.dll'
        $xmlAssembly = [System.Xml.XmlDocument].Assembly.Location
        Add-Type -ReferencedAssemblies @($assembly,$xmlAssembly) -TypeDefinition @'
using System;
using System.Xml;
using TcHmiAutomation;
using TcHmiAutomation.Publish;
public static class TcAgentHmiProjectApi {
 public static ITcHmiControlAttribute ControlAttribute(object project,string name,string value,bool withValues) {
  var p=(ITcHmiProject)project;
  return withValues ? p.GetControlAttributeInstance(name,value) : p.GetControlAttributeInstance();
 }
 public static object ControlAttributeSummary(object project,string name,string value,bool withValues) {
  var a=ControlAttribute(project,name,value,withValues);
  var r=new System.Collections.Generic.Dictionary<string,object>();
  r["present"]=a!=null; r["type"]="TcHmiAutomation.ITcHmiControlAttribute";
  if(a!=null){ try{r["name"]=a.Name;}catch{} try{r["value"]=a.Value;}catch{} try{r["is_complex"]=a.IsComplex;}catch{} }
  return r;
 }
 public static ITcHmiSymbol Symbol(object project,string name,string value) {
  return ((ITcHmiProject)project).GetSymbolInstance(name,value);
 }
 public static object SymbolSummary(object project,string name,string value) {
  var a=Symbol(project,name,value);
  var r=new System.Collections.Generic.Dictionary<string,object>();
  r["present"]=a!=null; r["type"]="TcHmiAutomation.ITcHmiSymbol";
  if(a!=null){ try{r["symbol_name"]=a.SymbolName;}catch{} try{r["value"]=a.Value;}catch{} }
  return r;
 }
 public static ITcHmiLocalizationEntry LocalizationEntry(object project,string name,string text,bool withValues) {
  var p=(ITcHmiProject)project;
  return withValues ? p.GetLocalizationEntryInstance(name,text) : p.GetLocalizationEntryInstance();
 }
 public static ITcHmiInternalSymbol InternalSymbol(object project,string name,object value,string type,bool persist,bool isReadonly) {
  return ((ITcHmiProject)project).GetInternalSymbolInstance(name,value,type,persist,isReadonly);
 }
 public static bool PublishByName(object project,string name,bool updateUi) {
  return ((ITcHmiProject)project).Publish(name,updateUi,(ITcHmiPublishCallback)null);
 }
 public static ITcHmiFile FindFile(object project,string relative) {
  var p=(ITcHmiProject)project;
  var normalized=(relative??"").Replace('/','\\').TrimStart('\\');
  var item=p.LookupChild(normalized);
  if(item is ITcHmiFile)return (ITcHmiFile)item;
  throw new InvalidOperationException("Official ITcHmiProject.LookupChild did not return a file: "+relative);
 }
 public static ITcHmiControl Control(object project,object node,object file) {
  return ((ITcHmiProject)project).GetControlInstance(node,(ITcHmiFile)file);
 }
 public static ITcHmiControl ControlFromMarkup(object project,object file,string identifier) {
  var f=(ITcHmiFile)file;
  var doc=new XmlDocument(); doc.LoadXml(f.GetSource());
  XmlNode node=null;
  foreach(XmlNode candidate in doc.SelectNodes("//*[@id]")) {
   var attr=candidate.Attributes["id"];
   if(attr!=null && String.Equals(attr.Value,identifier,StringComparison.Ordinal)) { node=candidate; break; }
  }
  if(node==null)throw new InvalidOperationException("Control identifier not found in markup: "+identifier);
  return ((ITcHmiProject)project).GetControlInstance(node,f);
 }
 public static ITcHmiControl ControlFromFile(object project,object file,string identifier) {
  var f=(ITcHmiFile)file;
  var control=f.GetControl(identifier);
  if(control==null)throw new InvalidOperationException("ITcHmiFile.GetControl returned null: "+identifier);
  return ((ITcHmiProject)project).GetControlInstance(control,f);
 }
 public static object ControlLookupSummary(object file,string identifier) {
  var f=(ITcHmiFile)file;
  var control=f.GetControl(identifier);
  var r=new System.Collections.Generic.Dictionary<string,object>();
  r["present"]=control!=null; r["type"]="TcHmiAutomation.ITcHmiControl";
  r["identifier"]=identifier;
  return r;
 }
}
'@
    }
    $automation = $Dte.GetObject('Beckhoff.TcHmi.1.12')
    $previousSuppressUi = [bool]$Dte.SuppressUI
    try { $automation.SuppressUi($true) } catch { }
    $native = [TcAgentHmiNativeItems]::Find($automation, $project, $root)
    return [pscustomobject]@{
        Dte=$Dte; Automation=$automation; Native=$native; Project=$project
        ProjectFile=$projectFile; Root=$root; PreviousSuppressUi=$previousSuppressUi
    }
}

function Restore-TcHmiProjectNativeContext {
    param($Context)
    if ($null -eq $Context) { return }
    try { $Context.Automation.SuppressUi([bool]$Context.PreviousSuppressUi) } catch { }
}

function Get-TcHmiProjectSafeProperty {
    param($Object, [string]$Name)
    if ($null -eq $Object) { return $null }
    try { return $Object.$Name } catch { return $null }
}

function Get-TcHmiProjectObjectSummary {
    param($Object)
    if ($null -eq $Object) { return @{present=$false} }
    $result = [ordered]@{present=$true; type=$Object.GetType().FullName}
    # TE2000 returns several automation interfaces as System.__ComObject.
    # Their late-bound property getters can throw InvalidCastException even
    # though the object itself is valid; type/presence is the safe readback.
    if ($result.type -eq 'System.__ComObject') { return $result }
    foreach ($name in @('Name','PathName','ProjectName','ProjectDirectory','ProjectGuid',
                        'SymbolName','Value','MappedName','Domain','Type','DataTypeDisplayName',
                        'Datatype','DefaultValue','Persist','IsReadonly','ReadOnly','Hidden',
                        'Loaded','State','Guid','Version','ConfigVersion','RuntimeIdentifier',
                        'TargetPlatform','ProfileName','TargetUrl','UseTls','SocketTimeout',
                        'Recording','PublishConfiguration','Source','AccessRight','Access',
                        'GroupName','GroupPermission','IsComplex')) {
        $value = Get-TcHmiProjectSafeProperty $Object $name
        if ($null -eq $value) { continue }
        if ($value -is [string] -or $value -is [ValueType]) { $result[$name]=$value }
    }
    return $result
}

function Get-TcHmiProjectConfigField {
    param([string]$Name)
    $map = @{
        activetheme='ActiveTheme'; locale='Locale'; scalemode='ScaleMode'; startupview='StartupView'
        websocketintervaltime='WebsocketIntervalTime'; websocketsystemtimeout='WebsocketSystemTimeout'
        websockettimeout='WebsocketTimeout'; loginpage='LoginPage'
    }
    $key = ([string]$Name).Replace('_','').Replace('-','').ToLowerInvariant()
    if (-not $map.ContainsKey($key)) { throw "Unknown ITcHmiProject ConfigFields value: $Name" }
    return [Enum]::Parse([TcHmiAutomation.ConfigFields], $map[$key])
}

function Get-TcHmiProjectConfigSnapshot {
    param($Native)
    $result = [ordered]@{}
    foreach ($name in @('ActiveTheme','Locale','ScaleMode','StartupView','WebsocketIntervalTime',
                        'WebsocketTimeout','WebsocketSystemTimeout','LoginPage')) {
        try { $result[$name] = $Native.GetConfigValue([Enum]::Parse([TcHmiAutomation.ConfigFields], $name)) } catch { $result[$name] = $null }
    }
    return $result
}

function Get-TcHmiProjectFileSummary {
    param($File)
    if ($null -eq $File) { return @{present=$false} }
    return [ordered]@{present=$true; type=$File.GetType().FullName;
        name=(Get-TcHmiProjectSafeProperty $File 'Name'); path=(Get-TcHmiProjectSafeProperty $File 'PathName')}
}

function Invoke-TcHmiProjectApi {
    param($Dte, [string]$ProjectName, [string]$Operation, $Arguments, [bool]$Apply)
    $op = ([string]$Operation).Trim().ToLowerInvariant()
    if (-not $op) { throw 'operation is required' }
    $aliases = @{
        'get-project-information'='project-info'; 'get-config-value'='get-config';
        'get-profile-instance'='get-profile'
    }
    $interfaceAliases = @{
        'get-server-interface'='server'; 'get-permission-interface'='permissions'; 'get-recipe-interface'='recipes'
    }
    $supported = @(
        'catalog','project-info','get-project-information','is-ready','last-error','get-config','get-config-value','change-config',
        'change-locale','change-login-page','change-scale-mode','change-startup-view','change-theme',
        'change-websocket-interval','change-websocket-timeout','change-websocket-system-timeout',
        'add-view','add-content','add-user-control','add-theme','add-nuget-package','remove-nuget-package',
        'get-nuget-sources','get-nuget-source-instance','build','clean','refresh-symbols','toggle-subscription-mode',
        'rename','show-in-browser','is-publish-running','get-publish-result','get-profile','get-profile-instance',
        'get-publish-profile','is-valid-publish-profile','publish','add-internal-symbol','get-internal-symbol',
        'get-internal-symbol-instance','remove-internal-symbol','add-localization','add-localization-entry',
        'add-localization-entries','remove-localization-entry','remove-localization-entries','get-localization-entry-instance',
        'get-control-attribute-instance','get-control-instance','get-symbol-instance','get-mapped-symbol-instance',
        'get-server-extension-instance','get-recording-settings-instance','get-historize-settings-instance',
        'get-file-permission-instance','get-control-access-right-instance','get-symbol-permission-instance',
        'get-interface','get-server-interface','get-permission-interface','get-recipe-interface'
    )
    if ($supported -notcontains $op) { throw "Unsupported ITcHmiProject operation: $Operation. Use operation=catalog to list the supported surface." }
    $args = if ($null -ne $Arguments) { $Arguments } else { [pscustomobject]@{} }
    $requestedOperation = $op
    if ($interfaceAliases.ContainsKey($op)) { $args=[pscustomobject]@{name=$interfaceAliases[$op]}; $op='get-interface' }
    elseif ($aliases.ContainsKey($op)) { $op = $aliases[$op] }
    $readOnly = @(
        'catalog','project-info','is-ready','last-error','get-config','get-nuget-sources',
        'get-nuget-source-instance','is-publish-running','get-publish-result','get-profile','get-publish-profile',
        'is-valid-publish-profile','get-internal-symbol','get-internal-symbol-instance','get-interface','get-localization-entry-instance','get-control-attribute-instance',
        'get-control-instance','get-symbol-instance','get-mapped-symbol-instance',
        'get-server-extension-instance','get-recording-settings-instance','get-historize-settings-instance',
        'get-file-permission-instance','get-control-access-right-instance','get-symbol-permission-instance'
    )
    $mutating = $readOnly -notcontains $op
    $ctx = $null
    try {
        $ctx = Get-TcHmiProjectNativeContext $Dte $ProjectName
        $native = $ctx.Native
        $projectFile = $ctx.ProjectFile
        $base = [ordered]@{status='read'; operation=$requestedOperation; project_file=$projectFile;
            api='ITcHmiProject.' + $Operation; apply_requested=$Apply;
            reload_performed=$false; browser_verified=$false}
        if ($mutating -and -not $Apply) {
            $base.status='preview'; $base.verified=$false; $base.note='Preview only. Supply apply=true to call the mutating official API.'
            if ($op -eq 'publish') { $base.note='Publishing is an external side effect; apply=true is required.' }
            return $base
        }
        switch ($op) {
            'catalog' {
                $base.status='ok'; $base.verified=$true
                $base.operations=@($supported)
                $base.members=@(
                    'AddContent','AddInternalSymbol','AddLocalization','AddLocalizationEntries','AddLocalizationEntry',
                    'AddNuGetPackage','AddTheme','AddUserControl','AddView','Build','ChangeConfig','ChangeLocale',
                    'ChangeLoginPage','ChangeScaleMode','ChangeStartupView','ChangeTheme','ChangeWebsocketIntervalTime',
                    'ChangeWebsocketSystemTimeout','ChangeWebsocketTimeout','Clean','GetConfigValue',
                    'GetControlAccessRightInstance','GetControlAttributeInstance','GetControlInstance','GetFilePermissionInstance',
                    'GetHistorizeSettingsInstance','GetInternalSymbol','GetInternalSymbolInstance','GetLocalizationEntryInstance',
                    'GetMappedSymbolInstance','GetNuGetSourceInstance','GetNuGetSources','GetPermissionInterface',
                    'GetProfileInstance','GetProjectInformation','GetPublishProfile','GetPublishResult','GetRecipeInterface',
                    'GetRecordingSettingsInstance','GetServerExtensionInstance','GetServerInterface','GetSymbolInstance',
                    'GetSymbolPermissionInstance','IsPublishRunning','IsReady','IsValidPublishProfile','Publish',
                    'RefreshSymbols','RemoveInternalSymbol','RemoveLocalizationEntries','RemoveLocalizationEntry',
                    'RemoveNuGetPackage','Rename','ShowInBrowser','ToggleSubscriptionMode'
                )
                return $base
            }
            'project-info' {
                $info=$native.GetProjectInformation(); $base.status='ok'; $base.verified=$true
                $base.project_name=[string]$info.ProjectName; $base.project_directory=[string]$info.ProjectDirectory
                $base.project_guid=[string]$info.ProjectGuid; $base.has_scc_flag=[bool]$info.HasSccFlag
                return $base
            }
            'is-ready' { $base.status='ok'; $base.verified=[bool]$native.IsReady(); $base.ready=[bool]$native.IsReady(); return $base }
            'last-error' { $base.status='ok'; $base.verified=$true; $base.last_error=[string]$native.LastError; return $base }
            'get-config' {
                $base.status='ok'; $base.verified=$true
                if ($args.field) { $field=Get-TcHmiProjectConfigField $args.field; $base.field=[string]$field; $base.value=$native.GetConfigValue($field) }
                else { $base.values=Get-TcHmiProjectConfigSnapshot $native }
                return $base
            }
            'change-config' {
                $field=Get-TcHmiProjectConfigField $args.field; $data=$args.data
                $fieldName=[string]$field
                if ($fieldName -eq 'ScaleMode') { $data=[Enum]::Parse([TcHmiAutomation.ConfigScaleMode], [string]$data) }
                elseif ($fieldName -in @('WebsocketIntervalTime','WebsocketTimeout','WebsocketSystemTimeout')) { $data=[int]$data }
                else { $data=[string]$data }
                $null=$native.ChangeConfig($field,$data); $base.status='applied'; $base.verified=$true; $base.value=$native.GetConfigValue($field); return $base
            }
            'change-locale' { $null=$native.ChangeLocale([string]$args.locale); $base.status='applied'; $base.verified=$true; return $base }
            'change-login-page' { $null=$native.ChangeLoginPage([string]$args.page); $base.status='applied'; $base.verified=$true; return $base }
            'change-scale-mode' { $mode=[Enum]::Parse([TcHmiAutomation.ConfigScaleMode],[string]$args.mode); $null=$native.ChangeScaleMode($mode); $base.status='applied'; $base.verified=$true; $base.value=[string]$native.GetConfigValue([TcHmiAutomation.ConfigFields]::ScaleMode); return $base }
            'change-startup-view' { $null=$native.ChangeStartupView([string]$args.view); $base.status='applied'; $base.verified=$true; $base.value=[string]$native.GetConfigValue([TcHmiAutomation.ConfigFields]::StartupView); return $base }
            'change-theme' { $null=$native.ChangeTheme([string]$args.theme); $base.status='applied'; $base.verified=$true; return $base }
            'change-websocket-interval' { $null=$native.ChangeWebsocketIntervalTime([int]$args.value); $base.status='applied'; $base.verified=$true; return $base }
            'change-websocket-timeout' { $null=$native.ChangeWebsocketTimeout([int]$args.value); $base.status='applied'; $base.verified=$true; return $base }
            'change-websocket-system-timeout' { $null=$native.ChangeWebsocketSystemTimeout([int]$args.value); $base.status='applied'; $base.verified=$true; return $base }
            'add-view' { $file=$native.AddView([string]$args.path); $base.status='applied'; $base.verified=($null -ne $file); $base.file=Get-TcHmiProjectFileSummary $file; return $base }
            'add-content' { $file=$native.AddContent([string]$args.path); $base.status='applied'; $base.verified=($null -ne $file); $base.file=Get-TcHmiProjectFileSummary $file; return $base }
            'add-user-control' { $file=$native.AddUserControl([string]$args.path); $base.status='applied'; $base.verified=($null -ne $file); $base.file=Get-TcHmiProjectFileSummary $file; return $base }
            'add-theme' { $file=$native.AddTheme([string]$args.name); $base.status='applied'; $base.verified=($null -ne $file); $base.file=Get-TcHmiProjectFileSummary $file; return $base }
            'add-nuget-package' { $ok=[bool]$native.AddNuGetPackage([string]$args.source,[string]$args.identifier,[string]$args.version); $base.status='applied'; $base.verified=$ok; $base.result=$ok; return $base }
            'remove-nuget-package' { $ok=[bool]$native.RemoveNuGetPackage([string]$args.identifier); $base.status='applied'; $base.verified=$ok; $base.result=$ok; return $base }
            'get-nuget-sources' { $base.status='ok'; $base.verified=$true; $base.sources=@($native.GetNuGetSources() | ForEach-Object { @{name=[string]$_.Name;source=[string]$_.Source} }); return $base }
            'get-nuget-source-instance' { $x=$native.GetNuGetSourceInstance(); $base.status='ok'; $base.verified=($null -ne $x); $base.result=Get-TcHmiProjectObjectSummary $x; return $base }
            'build' { $configuration=if($args.solution_configuration){[string]$args.solution_configuration}else{'Debug'}; $ok=[bool]$native.Build($configuration,[bool]$args.update_ui); $base.status='applied'; $base.verified=$ok; $base.result=$ok; $base.solution_configuration=$configuration; return $base }
            'clean' { $ok=[bool]$native.Clean([bool]$args.update_ui); $base.status='applied'; $base.verified=$ok; $base.result=$ok; return $base }
            'refresh-symbols' { $null=$native.RefreshSymbols(); $base.status='applied'; $base.verified=$true; return $base }
            'toggle-subscription-mode' { $null=$native.ToggleSubscriptionMode([bool]$args.enable); $base.status='applied'; $base.verified=$true; return $base }
            'rename' { $null=$native.Rename([string]$args.name); $base.status='applied'; $base.verified=$true; $base.value=[string]$native.GetProjectInformation().ProjectName; return $base }
            'show-in-browser' { $null=$native.ShowInBrowser([bool]$args.build,[bool]$args.https); $base.status='applied'; $base.verified=$true; $base.browser_verified=$true; return $base }
            'is-publish-running' { $base.status='ok'; $base.verified=$true; $base.running=[bool]$native.IsPublishRunning(); return $base }
            'get-publish-result' { $x=$native.GetPublishResult(); $base.status='ok'; $base.verified=$true; $base.result=Get-TcHmiProjectObjectSummary $x; return $base }
            'get-profile' { $x=$native.GetProfileInstance([string]$args.name); $base.status='ok'; $base.verified=($null -ne $x); $base.result=Get-TcHmiProjectObjectSummary $x; return $base }
            'get-publish-profile' { $x=$native.GetPublishProfile([string]$args.name); $base.status='ok'; $base.verified=($null -ne $x); $base.result=Get-TcHmiProjectObjectSummary $x; return $base }
            'is-valid-publish-profile' { $base.status='ok'; $base.verified=[bool]$native.IsValidPublishProfile([string]$args.name); $base.result=$base.verified; return $base }
            'publish' { $profile=[string]$args.profile; $ok=[bool][TcAgentHmiProjectApi]::PublishByName($native,$profile,[bool]$args.update_ui); $base.status='applied'; $base.verified=$ok; $base.result=$ok; return $base }
            'add-internal-symbol' {
                $symbol=[TcAgentHmiProjectApi]::InternalSymbol($native,[string]$args.name,$args.value,[string]$args.type,[bool]$args.persist,[bool]$args.is_readonly); $null=$native.AddInternalSymbol($symbol); $base.status='applied'; $base.verified=($null -ne $native.GetInternalSymbol([string]$args.name)); $base.result=Get-TcHmiProjectObjectSummary $native.GetInternalSymbol([string]$args.name); return $base
            }
            'get-internal-symbol' { $x=$native.GetInternalSymbol([string]$args.name); $base.status='ok'; $base.verified=($null -ne $x); $base.result=Get-TcHmiProjectObjectSummary $x; return $base }
            'get-internal-symbol-instance' { $x=[TcAgentHmiProjectApi]::InternalSymbol($native,[string]$args.name,$args.value,[string]$args.type,[bool]$args.persist,[bool]$args.is_readonly); $base.status='ok'; $base.verified=($null -ne $x); $base.result=Get-TcHmiProjectObjectSummary $x; return $base }
            'remove-internal-symbol' { $ok=[bool]$native.RemoveInternalSymbol([string]$args.name); $base.status='applied'; $base.verified=$ok; $base.result=$ok; return $base }
            'add-localization' { $x=$native.AddLocalization([string]$args.name,[string]$args.iso_language); $base.status='applied'; $base.verified=($null -ne $x); $base.result=Get-TcHmiProjectObjectSummary $x; return $base }
            'add-localization-entry' { $null=$native.AddLocalizationEntry([string]$args.name,[string]$args.text); $base.status='applied'; $base.verified=$true; return $base }
            'add-localization-entries' { $entries=New-Object 'System.Collections.Generic.List[TcHmiAutomation.ITcHmiLocalizationEntry]'; foreach($entry in @($args.entries)){ [void]$entries.Add([TcAgentHmiProjectApi]::LocalizationEntry($native,[string]$entry.name,[string]$entry.text,$true)) }; $null=$native.AddLocalizationEntries($entries.ToArray()); $base.status='applied'; $base.verified=$true; return $base }
            'remove-localization-entry' { $null=$native.RemoveLocalizationEntry([string]$args.name); $base.status='applied'; $base.verified=$true; return $base }
            'remove-localization-entries' { $keys=@($args.names | ForEach-Object {[string]$_}); $null=$native.RemoveLocalizationEntries($keys); $base.status='applied'; $base.verified=$true; return $base }
            'get-localization-entry-instance' { $x=[TcAgentHmiProjectApi]::LocalizationEntry($native,[string]$args.name,[string]$args.text,[bool]$args.name); $base.status='ok'; $base.verified=($null -ne $x); $base.result=Get-TcHmiProjectObjectSummary $x; return $base }
            'get-control-attribute-instance' { $x=[TcAgentHmiProjectApi]::ControlAttributeSummary($native,[string]$args.name,[string]$args.value,[bool]$args.name); $base.status='ok'; $base.verified=[bool]$x.present; $base.result=$x; return $base }
            'get-control-instance' {
                $file=[TcAgentHmiProjectApi]::FindFile($native,[string]$args.file)
                $lookup=[TcAgentHmiProjectApi]::ControlLookupSummary($file,[string]$args.control_id)
                try {
                    $x=[TcAgentHmiProjectApi]::ControlFromFile($native,$file,[string]$args.control_id)
                    $base.status='ok'; $base.verified=($null -ne $x); $base.result=Get-TcHmiProjectObjectSummary $x
                } catch {
                    # TE2000 exposes GetControlInstance for its internal XML
                    # node pipeline; a late-bound external control object is
                    # rejected by the installed 1.12 implementation. Keep
                    # this member discoverable without turning that known
                    # limitation into an opaque COM error.
                    $base.status='unavailable'; $base.verified=[bool]$lookup.present; $base.result=$lookup
                    $base.note='ITcHmiProject.GetControlInstance requires an internal TE2000 node context; ITcHmiFile.GetControl is available and verified for the requested identifier.'
                }
                return $base
            }
            'get-symbol-instance' { $x=[TcAgentHmiProjectApi]::SymbolSummary($native,[string]$args.name,[string]$args.value); $base.status='ok'; $base.verified=[bool]$x.present; $base.result=$x; return $base }
            'get-mapped-symbol-instance' { $x=$native.GetMappedSymbolInstance(); $base.status='ok'; $base.verified=($null -ne $x); $base.result=Get-TcHmiProjectObjectSummary $x; return $base }
            'get-server-extension-instance' { $server=$native.GetServerInterface(); $x=$native.GetServerExtensionInstance($server); $base.status='ok'; $base.verified=($null -ne $x); $base.result=Get-TcHmiProjectObjectSummary $x; return $base }
            'get-recording-settings-instance' { $x=$native.GetRecordingSettingsInstance(); $base.status='ok'; $base.verified=($null -ne $x); $base.result=Get-TcHmiProjectObjectSummary $x; return $base }
            'get-historize-settings-instance' { $x=$native.GetHistorizeSettingsInstance(); $base.status='ok'; $base.verified=($null -ne $x); $base.result=Get-TcHmiProjectObjectSummary $x; return $base }
            'get-file-permission-instance' { $x=$native.GetFilePermissionInstance(); $base.status='ok'; $base.verified=($null -ne $x); $base.result=Get-TcHmiProjectObjectSummary $x; return $base }
            'get-control-access-right-instance' { $x=$native.GetControlAccessRightInstance(); $base.status='ok'; $base.verified=($null -ne $x); $base.result=Get-TcHmiProjectObjectSummary $x; return $base }
            'get-symbol-permission-instance' { $x=$native.GetSymbolPermissionInstance(); $base.status='ok'; $base.verified=($null -ne $x); $base.result=Get-TcHmiProjectObjectSummary $x; return $base }
            'get-interface' {
                $kind=([string]$args.name).ToLowerInvariant(); $x=$null
                switch($kind){
                    'server' {$x=$native.GetServerInterface()}; 'permissions' {$x=$native.GetPermissionInterface()};
                    'recipes' {$x=$native.GetRecipeInterface()}; 'recording-settings' {$x=$native.GetRecordingSettingsInstance()};
                    'historize-settings' {$x=$native.GetHistorizeSettingsInstance()}; 'file-permission' {$x=$native.GetFilePermissionInstance()};
                    'control-access-right' {$x=$native.GetControlAccessRightInstance()}; 'symbol-permission' {$x=$native.GetSymbolPermissionInstance()};
                    default { throw 'Unknown interface name. Use server, permissions, recipes, recording-settings, historize-settings, file-permission, control-access-right or symbol-permission.' }
                }
                $base.status='ok'; $base.verified=($null -ne $x); $base.result=Get-TcHmiProjectObjectSummary $x; return $base
            }
            default { throw "Unsupported ITcHmiProject operation: $Operation. Use hmi project-api catalog to list supported operations." }
        }
    } finally { Restore-TcHmiProjectNativeContext $ctx }
}
