function Invoke-TcHmiNativeItem {
    param($Dte, [string]$ProjectName, $Request)
    $project = Get-TcHmiProjectObject $Dte $ProjectName
    $projectFile = Get-TcHmiResolvedFullName $project
    $root = [IO.Path]::GetDirectoryName($projectFile)
    $assembly = Join-Path ${env:ProgramFiles(x86)} 'Beckhoff\TwinCAT\Functions\TE2000-HMI-Engineering\VisualStudio\TcXaeShell\TcHmiAutomation.dll'
    if (-not (Test-Path -LiteralPath $assembly)) { throw 'Installed Beckhoff HMI Automation assembly unavailable; no fallback.' }
    Add-Type -Path $assembly
    if (-not ('TcAgentHmiNativeItems' -as [type])) {
        $envDte = Join-Path ([IO.Path]::GetDirectoryName([string]$Dte.FullName)) 'PublicAssemblies\envdte.dll'
        Add-Type -Path $envDte
        $xmlAssembly = [System.Xml.XmlDocument].Assembly.Location
        Add-Type -ReferencedAssemblies @($assembly,$envDte,$xmlAssembly) -TypeDefinition @'
using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Xml;
using TcHmiAutomation;
public static class TcAgentHmiNativeItems {
 delegate bool EnumWindowsProc(IntPtr hwnd, IntPtr parameter);
 [DllImport("user32.dll")] static extern bool EnumWindows(EnumWindowsProc callback, IntPtr parameter);
 [DllImport("user32.dll")] static extern bool EnumChildWindows(IntPtr parent, EnumWindowsProc callback, IntPtr parameter);
 [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint processId);
 [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern int GetWindowText(IntPtr hwnd, StringBuilder text, int maximum);
 [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr hwnd);
 static void InspectWindow(IntPtr hwnd, int processId, ArrayList captions) {
  uint owner; GetWindowThreadProcessId(hwnd, out owner);
  if (owner != (uint)processId || !IsWindowVisible(hwnd)) return;
  var text = new StringBuilder(512); GetWindowText(hwnd, text, text.Capacity);
  var caption = text.ToString();
  if (caption.IndexOf("TwinCAT HMI Configuration", StringComparison.OrdinalIgnoreCase) >= 0 && !captions.Contains(caption)) captions.Add(caption);
 }
 public static string[] ConfigurationWindows(int processId) {
  var captions = new ArrayList();
  EnumWindows((top, ignored) => {
   uint owner; GetWindowThreadProcessId(top, out owner);
   if (owner != (uint)processId) return true;
   InspectWindow(top, processId, captions);
   EnumChildWindows(top, (child, nested) => { InspectWindow(child, processId, captions); return true; }, IntPtr.Zero);
   return true;
  }, IntPtr.Zero);
  return (string[])captions.ToArray(typeof(string));
 }
 public static bool IsSaved(object project) { return ((ITcHmiProject)project).DteProject.Saved; }
 public static bool Delete(object parent, string name) { return ((ITcHmiItem)parent).DeleteChild(name); }
 public static void Save(object project) { ((ITcHmiProject)project).DteProject.Save(""); }
 public static ITcHmiProject Find(object automation, object project, string directory) {
  // GetHmiProjects and the string overload explicitly show SymbolsManagement.
  // The official Project overload avoids that enumeration/UI side effect.
  var p = ((ITcHmiAutomation)automation).GetHmiProject((EnvDTE.Project)project);
  if (p != null && String.Equals(Path.GetFullPath(p.GetProjectInformation().ProjectDirectory).TrimEnd('\\'), Path.GetFullPath(directory).TrimEnd('\\'), StringComparison.OrdinalIgnoreCase)) return p;
  throw new InvalidOperationException("Exact HMI project not found in native automation.");
 }
 public static object Parent(object project, string folder) {
  ITcHmiItem item = (ITcHmiItem)project;
  if (!String.IsNullOrEmpty(folder)) foreach (var part in folder.Split('/')) {
   ITcHmiItem next = null;
   foreach (var child in item.Childs) if (String.Equals(child.Name, part, StringComparison.OrdinalIgnoreCase)) { next = child; break; }
   if (next == null || !(next is ITcHmiFolder)) throw new InvalidOperationException("Create the parent folder explicitly first: " + part);
   item = next;
  }
  return item;
 }
 public static bool Exists(object parent, string name) {
  foreach (var child in ((ITcHmiItem)parent).Childs) if (String.Equals(child.Name, name, StringComparison.OrdinalIgnoreCase)) return true;
  return false;
 }
 public static string[] Create(object parent, string name, string template) {
  var p = (ITcHmiItem)parent;
  // Current TE2000's View template appends .view. Passing the complete saved
  // file name would create Name.view.view, unlike Content/UserControl.
  var createName = template == "View" && name.EndsWith(".view", StringComparison.OrdinalIgnoreCase)
   ? name.Substring(0, name.Length - 5) : name;
  var item = template == "folder" ? p.AddFolder(name) : p.AddItem(createName, (HmiTemplates)Enum.Parse(typeof(HmiTemplates), template));
  if (item == null) throw new InvalidOperationException("Native creation returned null; inspect output before retrying.");
  if (template == "View" && !String.Equals(item.Name, name, StringComparison.OrdinalIgnoreCase)) {
   if (item.Name.EndsWith(".view.view", StringComparison.OrdinalIgnoreCase) && name.EndsWith(".view", StringComparison.OrdinalIgnoreCase)) item.Name = name;
   else throw new InvalidOperationException("Native View template returned an unexpected item name: " + item.Name);
  }
  return new [] {item.Name, item.PathName};
 }
 public static ITcHmiItem AddExisting(object parent,string name,string source,string[] dependents) {
  var p=(ITcHmiItem)parent;
  var item=(dependents!=null && dependents.Length>0)
   ? p.AddExistingItem(name,source,dependents) : p.AddExistingItem(name,source);
  if(item==null)throw new InvalidOperationException("Official AddExistingItem returned null: "+name);
  return item;
 }
 private static bool SamePath(string left,string right) {
  return String.Equals(Path.GetFullPath(left).TrimEnd('\\','/'),Path.GetFullPath(right).TrimEnd('\\','/'),StringComparison.OrdinalIgnoreCase);
 }
 private static void CollectFiles(ITcHmiItem item,string target,List<ITcHmiFile> matches) {
  if(item is ITcHmiFile && SamePath(item.PathName,target))matches.Add((ITcHmiFile)item);
  foreach(var child in item.Childs)CollectFiles(child,target,matches);
 }
 private static void CollectItems(ITcHmiItem item,string target,List<ITcHmiItem> matches) {
  if(SamePath(item.PathName,target))matches.Add(item);
  foreach(var child in item.Childs)CollectItems(child,target,matches);
 }
 private static string ExactTarget(string projectRoot,string relative) {
  if(String.IsNullOrWhiteSpace(projectRoot) || String.IsNullOrWhiteSpace(relative))throw new ArgumentException("Project root and relative path are required.");
  var normalized=relative.Replace('/','\\');
  if(Path.IsPathRooted(normalized))throw new ArgumentException("HMI path must be relative: "+relative);
  foreach(var segment in normalized.Split('\\')){
   if(String.IsNullOrEmpty(segment) || segment=="." || segment==".." || segment.IndexOfAny(Path.GetInvalidPathChars())>=0)
    throw new ArgumentException("Unsafe HMI relative path: "+relative);
  }
  var root=Path.GetFullPath(projectRoot).TrimEnd('\\','/')+Path.DirectorySeparatorChar;
  var target=Path.GetFullPath(Path.Combine(root,normalized));
  if(!target.StartsWith(root,StringComparison.OrdinalIgnoreCase))throw new ArgumentException("HMI path escapes project root: "+relative);
  return target;
 }
 public static ITcHmiItem FindItem(object parent,string projectRoot,string relative) {
  var target=ExactTarget(projectRoot,relative);
  var matches=new List<ITcHmiItem>();CollectItems((ITcHmiItem)parent,target,matches);
  if(matches.Count==0)throw new InvalidOperationException("Exact native HMI item was not found: "+relative);
  if(matches.Count>1)throw new InvalidOperationException("Exact native HMI item is ambiguous: "+relative);
  return matches[0];
 }
 public static ITcHmiFile FindFile(object parent,string projectRoot,string relative) {
  var target=ExactTarget(projectRoot,relative);
  var matches=new List<ITcHmiFile>();CollectFiles((ITcHmiItem)parent,target,matches);
  if(matches.Count==0)throw new InvalidOperationException("Exact native HMI file was not found: "+relative);
  if(matches.Count>1)throw new InvalidOperationException("Exact native HMI file is ambiguous: "+relative);
  return matches[0];
 }
 public static ITcHmiFile AddPage(object project,string relative,string kind) {
  var p=(ITcHmiProject)project;
  var file=String.Equals(kind,"view",StringComparison.OrdinalIgnoreCase)
   ? p.AddView(relative) : p.AddContent(relative);
  if(file==null)throw new InvalidOperationException("Official AddView/AddContent returned null: "+relative);
  return file;
 }
 public static void ChangeStartupView(object project,string relative) {
  ((ITcHmiProject)project).ChangeStartupView(relative);
 }
 public static string[] Ids(object file){return ((ITcHmiFile)file).GetAllIdentifiers();}
 public static bool AddControl(object project,object file,string parent,string id,string type,string[] names,string[] values,bool[] complex) {
  var p=(ITcHmiProject)project;var f=(ITcHmiFile)file;var attrs=new ArrayList();
  for(var i=0;i<names.Length;i++){
   var a=p.GetControlAttributeInstance(names[i],values[i]);a.IsComplex=complex[i];attrs.Add(a);
  }
  return f.AddControl(parent,id,type,(ITcHmiControlAttribute[])attrs.ToArray(typeof(ITcHmiControlAttribute)))!=null;
 }
 public static bool ChangeAttributes(object project,object file,string id,string[] names,string[] values,bool[] complex) {
  var p=(ITcHmiProject)project;var f=(ITcHmiFile)file;var attrs=new ArrayList();
  for(var i=0;i<names.Length;i++){
   var a=p.GetControlAttributeInstance(names[i],values[i]);a.IsComplex=complex[i];attrs.Add(a);
  }
  return f.GetControl(id).ChangeAttributes((ITcHmiControlAttribute[])attrs.ToArray(typeof(ITcHmiControlAttribute)));
 }
 public static string Source(object file){return ((ITcHmiFile)file).GetSource();}
 public static bool RemoveComplexScripts(object file,string id,string[] scalarNames) {
  var f=(ITcHmiFile)file;var source=f.GetSource();var doc=new XmlDocument();doc.LoadXml(source);
  var node=doc.SelectSingleNode("//*[@id='"+id.Replace("'","&apos;")+"']");
  if(node==null)return false;var remove=new List<XmlNode>();
  foreach(XmlNode child in node.ChildNodes){
   if(!String.Equals(child.LocalName,"script",StringComparison.OrdinalIgnoreCase))continue;
   var attribute=child.Attributes["data-tchmi-target-attribute"];
   var target=attribute==null ? null : attribute.Value;
   if(Array.IndexOf(scalarNames,target)>=0)remove.Add(child);
  }
  foreach(var child in remove)node.RemoveChild(child);
  return remove.Count==0 || f.SetSource(doc.OuterXml);
 }
 }
'@
    }
    $automation = $Dte.GetObject('Beckhoff.TcHmi.1.12')
    # GetHmiProject() itself can initialize the HMI configuration surface.
    # Suppressing only around AddItem() is therefore too late.
    $previousSuppressUi=[bool]$Dte.SuppressUI
    $suppressionRequested=$false
    try {
        $automation.SuppressUi($true)
        $suppressionRequested=$true
        $native = [TcAgentHmiNativeItems]::Find($automation, $project, $root)
    } catch {
        if ($suppressionRequested) { try { $automation.SuppressUi($previousSuppressUi) } catch { } }
        throw
    }
    if (-not $native.IsReady()) {
        if ($suppressionRequested) { try { $automation.SuppressUi($previousSuppressUi) } catch { } }
        throw 'Native HMI project is not ready; nothing written.'
    }
    if ($Request.probe_only) {
        $probeResult = @{status='available'; native_pid=$automation.GetProcessId(); project_file=$projectFile; written=$false; api='Beckhoff.TcHmi.1.12'; fallback_allowed=$false; ui_suppression_started_before_project_lookup=$true}
        if ($suppressionRequested) { try { $automation.SuppressUi($previousSuppressUi) } catch { $probeResult.ui_restore_error=$_.Exception.Message } }
        return $probeResult
    }
    $templates = @{folder='folder'; view='View'; content='Content'; usercontrol='UserControl'; codebehind_js='CodeBehindJavaScript'; javascript='GeneralBlankJavaScript'; javascript_classic='AddExistingItem'; css='GeneralBlankCss'; function_js='FunctionJavaScript_Hmi'; function_js_classic='AddExistingItem'}
    $extensions = @{folder='';view='.view';content='.content';usercontrol='.usercontrol';codebehind_js='.js';javascript='.js';javascript_classic='.js';css='.css';function_js='.js';function_js_classic='.js'}
    $kind = [string]$Request.kind; $name = [string]$Request.name; $folder = ([string]$Request.folder).Replace('\','/')
    if (-not $templates.ContainsKey($kind) -or $name -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') { throw 'Invalid native HMI item kind/name.' }
    if ($Request.parent_file) { throw 'Manual DependentUpon is not supported; native template controls hierarchy.' }
    foreach ($part in @($folder.Split('/') | Where-Object { $_ })) {
        if ($part -notmatch '^[A-Za-z0-9_][A-Za-z0-9_. -]*$' -or $part.Trim() -cne $part -or $part.EndsWith('.')) { throw 'Unsafe folder path.' }
    }
    if ($folder -and ($folder.StartsWith('/') -or $folder.EndsWith('/') -or $folder.Contains('//'))) { throw 'Unsafe folder path.' }
    if (($folder.Split('/')[0]) -in @('Properties','Server','bin','obj','Packages','.TwinCATAgent')) { throw 'Reserved HMI directory.' }
    if ($name -match '^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$') { throw 'Reserved item name.' }
    $parent = [TcAgentHmiNativeItems]::Parent($native, $folder)
    $fileName = $name + $extensions[$kind]
    $relative = (@($folder,$fileName) | Where-Object { $_ }) -join '/'
    $target = Join-Path $root $relative
    $cursor = $root
    foreach ($part in $relative.Split('/')) {
        $cursor = Join-Path $cursor $part
        if ((Test-Path -LiteralPath $cursor) -and ((Get-Item -LiteralPath $cursor).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Reparse points are not supported for native item creation.' }
    }
    if ((Test-Path -LiteralPath $target) -or [TcAgentHmiNativeItems]::Exists($parent,$fileName)) { throw 'Item exists; native create will not overwrite.' }
    $companion = if ($kind -eq 'usercontrol') { $target + '.json' } elseif ($kind -in @('function_js','function_js_classic')) { [IO.Path]::ChangeExtension($target,'.function.json') } else { '' }
    if ($companion -and (Test-Path -LiteralPath $companion)) { throw 'Native companion file exists; refusing overwrite.' }
    $result = @{status='preview'; project_file=$projectFile; relative=$relative; api=if($kind -in @('javascript_classic','function_js_classic')){'ITcHmiItem.AddExistingItem'}else{'ITcHmiItem.AddItem'}; template=$templates[$kind]; written=$false; verified=$false; reload_required=$false; fallback_allowed=$false; browser_verified=$false}
    $result.ui_suppression_started_before_project_lookup=$true
    if ($kind -eq 'folder') { $result.api='ITcHmiItem.AddFolder' }
    if (-not $Request.apply) {
        if ($suppressionRequested) { try { $automation.SuppressUi($previousSuppressUi) } catch { $result.ui_restore_error=$_.Exception.Message } }
        return $result
    }
    $backup = Join-Path $root ('.TwinCATAgent\backups\native-item-' + [guid]::NewGuid().ToString('N'))
    [void][IO.Directory]::CreateDirectory($backup)
    [IO.File]::Copy($projectFile,(Join-Path $backup 'project.original'))
    [IO.File]::Copy((Join-Path $root 'Properties\tchmiconfig.json'),(Join-Path $backup 'config.original'))
    $result.backup_path=$backup
    $uiTrace = New-Object System.Collections.ArrayList
    $xaePid=0
    try { $xaePid=Get-TcWindowPid ([IntPtr][int64]$Dte.MainWindow.HWnd) } catch { }
    function Read-NativeUiPhase([string]$Phase) {
        $captions = @()
        try { foreach ($window in $Dte.Windows) { if ([string]$window.Caption -match 'TwinCAT HMI Configuration') { $captions += [string]$window.Caption } } } catch { }
        if ($xaePid -gt 0) {
            try { $captions += @([TcAgentHmiNativeItems]::ConfigurationWindows($xaePid)) } catch { }
        }
        $captions = @($captions | Sort-Object -Unique)
        [void]$uiTrace.Add(@{phase=$Phase; configuration_windows=$captions})
    }
    try {
        Read-NativeUiPhase 'before_create'
        $result.ui_suppression_requested=$true
        $result.ui_suppression_verified=(@([TcAgentHmiNativeItems]::ConfigurationWindows($xaePid)).Count -eq 0)
        if ($kind -in @('javascript_classic','function_js_classic')) {
            # AddExistingItem is the supported way to import classic JS.  The
            # AddItem JavaScript templates are intentionally not used because
            # TE2000 marks them as JavascriptModule/EsModule.
            $templateSource=Join-Path ${env:ProgramFiles(x86)} 'Beckhoff\TwinCAT\Functions\TE2000-HMI-Engineering\Templates\ItemTemplates\TwinCAT HMI\General\Blank_Js\Blank_Js.js'
            if (-not [IO.File]::Exists($templateSource)) { throw "Official classic JavaScript template not found: $templateSource" }
            $stagedSource=Join-Path $backup ('classic-source\'+$fileName)
            [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($stagedSource)) | Out-Null
            $sourceText=[IO.File]::ReadAllText($templateSource,[Text.Encoding]::UTF8).Replace('$rootname$',$name)
            [IO.File]::WriteAllText($stagedSource,$sourceText,(New-Object Text.UTF8Encoding($false)))
            $dependentPaths=@()
            if($kind -eq 'function_js_classic') {
                $descriptorTemplate=Join-Path ${env:ProgramFiles(x86)} 'Beckhoff\TwinCAT\Functions\TE2000-HMI-Engineering\Templates\ItemTemplates\TwinCAT HMI\Functions\FunctionJs_Hmi\Function.function.json'
                if(-not [IO.File]::Exists($descriptorTemplate)){throw "Official Function descriptor template not found: $descriptorTemplate"}
                $stagedDescriptor=Join-Path $backup ('classic-source\'+[IO.Path]::GetFileName($companion))
                $descriptorText=[IO.File]::ReadAllText($descriptorTemplate,[Text.Encoding]::UTF8).Replace('$rootname$',$name)
                [IO.File]::WriteAllText($stagedDescriptor,$descriptorText,(New-Object Text.UTF8Encoding($false)))
                $dependentPaths=@($stagedDescriptor)
            }
            $created=[TcAgentHmiNativeItems]::AddExisting($parent,$fileName,$stagedSource,[string[]]$dependentPaths)
            $result.classic_javascript=$true
        } else {
            $created = [TcAgentHmiNativeItems]::Create($parent,$fileName,$templates[$kind])
        }
        Read-NativeUiPhase 'after_create'
        $result.written=$true
        if($kind -in @('javascript_classic','function_js_classic')) {
            $result.native_name=[string]$created.Name; $result.native_path=[string]$created.PathName
        } else {
            $result.native_name=$created[0]; $result.native_path=$created[1]
        }
        [TcAgentHmiNativeItems]::Save($native)
        Read-NativeUiPhase 'after_save'
        [xml]$saved = [IO.File]::ReadAllText($projectFile)
        $registered = @($saved.SelectNodes('//*[@Include]') | Where-Object { $_.GetAttribute('Include').Replace('\','/').TrimEnd('/') -ieq $relative })
        $result.persisted_in_project=($registered.Count -eq 1)
        $result.verified=([TcAgentHmiNativeItems]::Exists($parent,$fileName) -and (Test-Path -LiteralPath $target) -and $result.persisted_in_project)
        $result.status=if($result.verified){'created'}else{'incomplete'}
    } catch {
        # Native wizard can partially succeed. Never retry or delete ambiguous output.
        $result.status='incomplete'; $result.written='unknown'; $result.error=$_.Exception.Message; $result.retry_safe=$false
    } finally {
        if ($suppressionRequested) {
            try { $automation.SuppressUi($previousSuppressUi) } catch { $result.ui_restore_error=$_.Exception.Message }
            Read-NativeUiPhase 'after_restore'
        }
    }
    $result.ui_trace=@($uiTrace.ToArray())
    return $result
}

function Invoke-TcHmiNativePage {
    <#
      Create a View/Content through the installed TE2000 automation object.
      The page source is never copied from a template: AddView/AddContent
      creates the native root and AddControl/ChangeAttributes adds only the
      requested semantic controls.  This path intentionally does not reload
      the HMI project.
    #>
    param($Dte, [string]$ProjectName, [string]$Name,
          [ValidateSet('view','content')][string]$Kind = 'view',
          [object[]]$Controls = @(), [bool]$Apply = $false)
    $project = Get-TcHmiProjectObject $Dte $ProjectName
    $projectFile = [IO.Path]::GetFullPath((Get-TcHmiResolvedFullName $project))
    $root = Get-TcHmiProjectPath $project
    # The native type loader lives in Invoke-TcHmiNativeItem.  Initialize it
    # before the semantic page path touches TcAgentHmiNativeItems; otherwise a
    # fresh process could fail before AddView and report a misleading recovery.
    $probe = Invoke-TcHmiNativeItem $Dte $projectFile @{probe_only=$true}
    if (-not ('TcAgentHmiNativeItems' -as [type])) { throw 'Native HMI item automation type initialization failed.' }
    $baseName = [IO.Path]::GetFileNameWithoutExtension($Name)
    if (-not ($baseName -match '^[A-Za-z_][A-Za-z0-9_]*$') -or $Name.IndexOfAny(@([char]'\',[char]'/')) -ge 0) {
        throw 'HMI page name must be a single identifier using letters, digits and underscore.'
    }
    $extension = if ($Kind -eq 'view') { '.view' } else { '.content' }
    $relative = $baseName + $extension
    $target = Join-Path $root $relative
    if (Test-Path -LiteralPath $target) { throw "HMI page already exists: $relative" }
    $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
    foreach ($control in @($Controls)) {
        $id=[string]$control.id; $type=[string]$control.type
        if (-not ($id -match '^[A-Za-z_][A-Za-z0-9_]*$') -or -not $seen.Add($id)) { throw "Invalid or duplicate HMI control id '$id'." }
        if (-not $type.StartsWith('TcHmi.Controls.',[StringComparison]::Ordinal)) { throw "Invalid HMI control type '$type'." }
        if ($control.parent_id -and -not ([string]$control.parent_id -match '^[A-Za-z_][A-Za-z0-9_]*$')) { throw "Invalid parent control id '$($control.parent_id)'." }
        foreach ($property in @($control.attributes.PSObject.Properties)) {
            if (-not ([string]$property.Name).StartsWith('data-tchmi-',[StringComparison]::OrdinalIgnoreCase) -or
                [string]$property.Name -ieq 'data-tchmi-type' -or [string]$property.Name -ieq 'data-tchmi-trigger') {
                throw "Only non-event data-tchmi-* attributes are allowed for '$id'."
            }
        }
    }
    $result=@{status='preview';project_file=$projectFile;file=$relative;kind=$Kind;control_count=@($Controls).Count
        api='ITcHmiProject.AddView/AddContent + ITcHmiFile.AddControl/ChangeAttributes'
        source_copied=$false;reload_required=$false;reload_performed=$false;written=$false;verified=$false}
    if (-not $Apply) { return $result }
    $automation=$Dte.GetObject('Beckhoff.TcHmi.1.12')
    $previous=[bool]$Dte.SuppressUI
    $automation.SuppressUi($true)
    $backup=Join-Path $root ('.TwinCATAgent\backups\native-page-'+[guid]::NewGuid().ToString('N'))
    [IO.Directory]::CreateDirectory($backup) | Out-Null
    [IO.File]::Copy($projectFile,(Join-Path $backup 'project.original'),$false)
    $configPath=Join-Path $root 'Properties\tchmiconfig.json'
    if ([IO.File]::Exists($configPath)) { [IO.File]::Copy($configPath,(Join-Path $backup 'config.original'),$false) }
    try {
        $native=[TcAgentHmiNativeItems]::Find($automation,$project,$root)
        $file=[TcAgentHmiNativeItems]::AddPage($native,$relative,$Kind)
        $rootIds=@([TcAgentHmiNativeItems]::Ids($file))
        if ($rootIds.Count -ne 1) { throw "Native page root was not created uniquely: $relative" }
        $rootId=[string]$rootIds[0]
        foreach ($control in @($Controls)) {
            $attrs=@();$values=@();$complex=@()
            foreach ($property in @($control.attributes.PSObject.Properties)) {
                $attrs += [string]$property.Name
                $value=$property.Value
                if ($value -is [System.Management.Automation.PSCustomObject] -or $value -is [Array]) {
                    $values += ($value | ConvertTo-Json -Depth 100 -Compress); $complex += $true
                } else { $values += [string]$value; $complex += $false }
            }
            $parent=if($control.parent_id){[string]$control.parent_id}else{$rootId}
            if (-not [TcAgentHmiNativeItems]::AddControl($native,$file,$parent,[string]$control.id,[string]$control.type,[string[]]$attrs,[string[]]$values,[bool[]]$complex)) {
                throw "Official AddControl rejected: $($control.id)"
            }
            $scalarNames=@(); for($i=0;$i -lt $attrs.Count;$i++){if(-not $complex[$i]){$scalarNames+=[string]$attrs[$i]}}
            if($scalarNames.Count -gt 0 -and -not [TcAgentHmiNativeItems]::RemoveComplexScripts($file,[string]$control.id,[string[]]$scalarNames)){
                throw "Native default script cleanup failed: $($control.id)"
            }
        }
        [TcAgentHmiNativeItems]::Save($native)
        try { $Dte.ExecuteCommand('File.SaveAll') } catch { }
        if (-not (Test-Path -LiteralPath $target)) { throw "Native page file was not persisted: $relative" }
        [xml]$saved=[IO.File]::ReadAllText($projectFile,[Text.Encoding]::UTF8)
        $registered=@($saved.SelectNodes('//*[@Include]') | Where-Object { $_.GetAttribute('Include').Replace('\','/').TrimEnd('/') -ieq $relative })
        $ids=@([TcAgentHmiNativeItems]::Ids($file))
        $savedConfig=[IO.File]::ReadAllText($configPath,[Text.Encoding]::UTF8) | ConvertFrom-Json
        $section=if($Kind -eq 'view'){'views'}else{'content'}
        $configEntries=@($savedConfig.$section | Where-Object { ([string]$_.url).Replace('\','/').TrimEnd('/') -ieq $relative })
        $result.written=$true;$result.registered=($registered.Count -eq 1);$result.config_registration=($configEntries.Count -eq 1);$result.control_ids=$ids
        $result.verified=($registered.Count -eq 1 -and $configEntries.Count -eq 1 -and $ids.Count -eq (@($Controls).Count+1))
        if (-not $result.verified) { throw 'Native page readback did not match registration/control count.' }
        $result.status='created';$result.backup_path=$backup
    } catch {
        $result.status='incomplete';$result.error=$_.Exception.Message;$result.recovery_required=$true;$result.backup_path=$backup
    } finally {
        try { $automation.SuppressUi($previous) } catch { }
        try { $Dte.SuppressUI=$previous } catch { }
    }
    return $result
}

function Set-TcHmiStartupView {
    <#
      Set the startup view through ITcHmiProject.ChangeStartupView.  This is
      a project-level native operation: it does not edit tchmiconfig.json on
      disk and does not reload the HMI project.
    #>
    param($Dte, [string]$ProjectName, [Parameter(Mandatory)][string]$View)
    $project = Get-TcHmiProjectObject $Dte $ProjectName
    $projectFile = [IO.Path]::GetFullPath((Get-TcHmiResolvedFullName $project))
    $root = Get-TcHmiProjectPath $project
    $viewPath = Resolve-TcHmiFilePath $root $View @('.view') -RequireRegistered
    if (-not [IO.File]::Exists($viewPath)) { throw "HMI startup view does not exist: $View" }
    $relative = $viewPath.Substring($root.Length).TrimStart('\','/').Replace('\','/')
    $probe = Invoke-TcHmiNativeItem $Dte $projectFile @{probe_only=$true}
    if (-not ('TcAgentHmiNativeItems' -as [type])) { throw 'Native HMI item automation type initialization failed.' }
    $automation = $Dte.GetObject('Beckhoff.TcHmi.1.12')
    $previous = [bool]$Dte.SuppressUI
    $automation.SuppressUi($true)
    $configPath = Join-Path $root 'Properties\tchmiconfig.json'
    $before = ''
    if ([IO.File]::Exists($configPath)) { $before = [IO.File]::ReadAllText($configPath,[Text.Encoding]::UTF8) }
    $beforeConfig = if ($before) { $before | ConvertFrom-Json } else { $null }
    try {
        $native = [TcAgentHmiNativeItems]::Find($automation,$project,$root)
        [TcAgentHmiNativeItems]::ChangeStartupView($native,$relative)
        [TcAgentHmiNativeItems]::Save($native)
        try { $Dte.ExecuteCommand('File.SaveAll') } catch { }
        if (-not [IO.File]::Exists($configPath)) { throw 'HMI configuration file was not persisted.' }
        $readback = [IO.File]::ReadAllText($configPath,[Text.Encoding]::UTF8) | ConvertFrom-Json
        $actual = ([string]$readback.startupView).Replace('\','/').TrimEnd('/')
        if ($actual -cne $relative.TrimEnd('/')) { throw "Startup view readback mismatch: expected '$relative', got '$actual'." }
        [pscustomobject]@{
            status='applied'; project_file=$projectFile; startup_view=$actual
            previous_startup_view=if($null -ne $beforeConfig){[string]$beforeConfig.startupView}else{''}
            api='ITcHmiProject.ChangeStartupView'; reload_performed=$false; verified=$true
        }
    } finally {
        try { $automation.SuppressUi($previous) } catch { }
        try { $Dte.SuppressUI=$previous } catch { }
    }
}

function Get-TcHmiNativeServerSymbol {
    param($Dte, [string]$ProjectName, [Parameter(Mandatory)][string[]]$SymbolNames)
    if ($SymbolNames.Count -lt 1 -or $SymbolNames.Count -gt 32) { throw 'Provide 1..32 HMI Server symbol names.' }
    foreach ($name in $SymbolNames) {
        if ([string]::IsNullOrWhiteSpace($name) -or $name.Length -gt 256 -or $name -match '[\x00-\x1F]') {
            throw 'Invalid HMI Server symbol name.'
        }
    }
    $project = Get-TcHmiProjectObject $Dte $ProjectName
    $projectFile = Get-TcHmiResolvedFullName $project
    $root = [IO.Path]::GetDirectoryName($projectFile)
    # Initialize the installed official Automation types through the same
    # no-fallback path used by native item creation.
    $probe = Invoke-TcHmiNativeItem $Dte $projectFile @{probe_only=$true}
    $automation = $Dte.GetObject('Beckhoff.TcHmi.1.12')
    $previousSuppressUi=[bool]$Dte.SuppressUI
    $automation.SuppressUi($true)
    try {
        $native = [TcAgentHmiNativeItems]::Find($automation, $project, $root)
        if (-not $native.IsReady()) { throw 'Native HMI project is not ready.' }
        $server = $native.GetServerInterface()
        $metadata=''; $metadataTruncated=$false
        try {
            $metadata=[string]$server.GetMetadata()
            if ($metadata.Length -gt 20000) { $metadata=$metadata.Substring(0,20000); $metadataTruncated=$true }
        } catch { $metadataError=[string]$_.Exception.Message }
        $extensions = New-Object System.Collections.ArrayList
        try {
            foreach ($extension in @($server.GetServerExtensions($true))) {
                [void]$extensions.Add([pscustomobject]@{
                    domain=[string]$extension.DomainName; runtime_identifier=[string]$extension.RuntimeIdentifier
                    target_platform=[string]$extension.TargetPlatform; state=[string]$extension.State
                    loaded=[bool]$extension.Loaded; config_version=[string]$extension.ConfigVersion
                    version=[string]$extension.Version; guid=[string]$extension.Guid
                })
            }
        } catch { $extensionsError=[string]$_.Exception.Message }
        $items = New-Object System.Collections.ArrayList
        foreach ($name in $SymbolNames) {
            try {
                $symbol = $server.ReadSymbol($name)
                [void]$items.Add([pscustomobject]@{
                    requested_name=$name; available=($null -ne $symbol)
                    symbol_name=if($null -ne $symbol){[string]$symbol.SymbolName}else{''}
                    value=if($null -ne $symbol){[string]$symbol.Value}else{''}
                    error=''
                })
            } catch {
                [void]$items.Add([pscustomobject]@{
                    requested_name=$name; available=$false; symbol_name=''; value=''
                    error=[string]$_.Exception.Message
                })
            }
        }
        return [pscustomobject]@{
            status='read'; project_file=$projectFile; api='ITcHmiServer.ReadSymbol'
            symbols=@($items.ToArray()); readonly=$true; reload_performed=$false
            extensions=@($extensions.ToArray()); extensions_error=$extensionsError
            metadata=$metadata; metadata_truncated=$metadataTruncated; metadata_error=$metadataError
            ui_suppression_started_before_project_lookup=[bool]$probe.ui_suppression_started_before_project_lookup
        }
    } finally {
        try { $automation.SuppressUi($previousSuppressUi) } catch { }
    }
}

function Initialize-TcHmiDeleteLifecycle {
    param($Dte)
    if ('TcAgentHmiDeleteLifecycle' -as [type]) { return }
    $sdk=Join-Path ([IO.Path]::GetDirectoryName([string]$Dte.FullName)) 'PublicAssemblies'
    $refs=@('Microsoft.VisualStudio.OLE.Interop.dll','Microsoft.VisualStudio.Shell.Interop.dll','Microsoft.VisualStudio.Shell.Interop.11.0.dll') | ForEach-Object { Join-Path $sdk $_ }
    foreach($ref in $refs){Add-Type -Path $ref}
    Add-Type -ReferencedAssemblies $refs -TypeDefinition @'
using System;
using System.IO;
using System.Runtime.InteropServices;
using Microsoft.VisualStudio.Shell.Interop;
public static class TcAgentHmiDeleteLifecycle {
 private static bool Same(string a,string b) {
  return !String.IsNullOrEmpty(a) && String.Equals(Path.GetFullPath(a).TrimEnd('\\','/'),Path.GetFullPath(b).TrimEnd('\\','/'),StringComparison.OrdinalIgnoreCase);
 }
 private static IVsHierarchy Find(object dte,string solutionFile,string projectFile) {
  var provider=(Microsoft.VisualStudio.OLE.Interop.IServiceProvider)dte;
  Guid service=typeof(SVsSolution).GUID, iid=typeof(IVsSolution).GUID; IntPtr ptr;
  Marshal.ThrowExceptionForHR(provider.QueryService(ref service,ref iid,out ptr));
  IVsSolution solution;
  try { solution=(IVsSolution)Marshal.GetObjectForIUnknown(ptr); } finally { Marshal.Release(ptr); }
  string dir,current,options;
  Marshal.ThrowExceptionForHR(solution.GetSolutionInfo(out dir,out current,out options));
  if(!Same(current,solutionFile))throw new InvalidOperationException("Solution changed; deletion refused.");
  Guid all=Guid.Empty; IEnumHierarchies enumerator;
  Marshal.ThrowExceptionForHR(solution.GetProjectEnum((uint)__VSENUMPROJFLAGS.EPF_LOADEDINSOLUTION,ref all,out enumerator));
  var row=new IVsHierarchy[1]; uint count; IVsHierarchy match=null; int hr;
  while((hr=enumerator.Next(1,row,out count))==0 && count==1) {
   var project=row[0] as IVsProject; string path;
   if(project==null || project.GetMkDocument(0xFFFFFFFE,out path)<0 || !Same(path,projectFile))continue;
   if(match!=null)throw new InvalidOperationException("Ambiguous HMI hierarchy.");
   match=row[0];
  }
  Marshal.ThrowExceptionForHR(hr);
  if(match==null)throw new InvalidOperationException("Exact HMI hierarchy not found.");
  return match;
 }
 private static uint Resolve(IVsHierarchy hierarchy,string target) {
  uint id; int hr=hierarchy.ParseCanonicalName(target,out id);
  // Folder URL monikers in TE2000 may require a trailing directory separator.
  if(hr<0 && Directory.Exists(target))hr=hierarchy.ParseCanonicalName(target.TrimEnd('\\','/')+"\\",out id);
  if(hr<0)throw new InvalidOperationException("ParseCanonicalName failed for exact item: 0x"+hr.ToString("X8"));
  if(id>=0xFFFFFFFD)throw new InvalidOperationException("Root/selection/nil deletion refused.");
  string path; hr=hierarchy.GetCanonicalName(id,out path);
  if(hr<0)throw new InvalidOperationException("GetCanonicalName failed for exact item: 0x"+hr.ToString("X8"));
  if(!Same(path,target))throw new InvalidOperationException("Delete item identity mismatch.");
  var allowed=new bool[1];
  Marshal.ThrowExceptionForHR(((IVsHierarchyDeleteHandler3)hierarchy).QueryDeleteItems(1,1,new[]{id},allowed));
  if(!allowed[0])throw new InvalidOperationException("Storage deletion is not allowed by the native hierarchy.");
  return id;
 }
 public static uint Probe(object dte,string solutionFile,string projectFile,string target) {
  return Resolve(Find(dte,solutionFile,projectFile),target);
 }
 public static void Delete(object dte,string solutionFile,string projectFile,string target) {
  var hierarchy=Find(dte,solutionFile,projectFile); uint id=Resolve(hierarchy,target);
  Marshal.ThrowExceptionForHR(((IVsHierarchyDeleteHandler3)hierarchy).DeleteItems(1,1,new[]{id},0));
 }
}
'@
}

function Invoke-TcHmiNativeDelete {
    param($Dte, [string]$ProjectName, $Plan)
    if($Plan.repair_orphan){throw 'Orphan repair requires a separately reviewed offline repair; native no-reload deletion requires an existing item.'}
    $probe = Invoke-TcHmiNativeItem $Dte $ProjectName @{probe_only=$true}
    $projectFile = [IO.Path]::GetFullPath($probe.project_file)
    if ($projectFile -ine [IO.Path]::GetFullPath([string]$Plan.project_file)) { throw 'Delete plan project mismatch.' }
    $root = [IO.Path]::GetDirectoryName($projectFile)
    $relative = ([string]$Plan.relative).Replace('\','/')
    if ($relative -notmatch '^[A-Za-z0-9_][A-Za-z0-9_. -]*(/[A-Za-z0-9_][A-Za-z0-9_. -]*)*$' -or
        @($relative.Split('/') | Where-Object { $_.Trim() -cne $_ -or $_.EndsWith('.') }).Count -gt 0) { throw 'Unsafe deletion path.' }
    if ($relative.Split('/')[0] -in @('Properties','Server','Packages','bin','obj','.TwinCATAgent')) { throw 'Protected directory.' }
    $target = Resolve-TcHmiFilePath $root $relative -RequireRegistered
    if (-not $target.StartsWith($root + '\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Deletion target escapes project.' }
    $allowedFiles = @($relative)
    if ([IO.Path]::GetExtension($relative) -eq '.usercontrol') { $allowedFiles += $relative + '.json' }
    if ([IO.Path]::GetExtension($relative) -eq '.js') { $allowedFiles += [IO.Path]::ChangeExtension($relative,'.function.json').Replace('\','/') }
    foreach ($file in @($Plan.files)) { if ([string]$file -notin $allowedFiles) { throw 'Unexpected delete-plan file.' } }
    foreach ($doc in $Dte.Documents) {
        if ([string]$doc.FullName -and ([string]$doc.FullName).StartsWith($root + '\',[StringComparison]::OrdinalIgnoreCase) -and -not $doc.Saved) { throw 'Unsaved HMI document; save or discard manually before deleting.' }
    }
    $sha = [Security.Cryptography.SHA256]::Create()
    try { foreach ($stamp in @($Plan.stamps)) {
        $path = [IO.Path]::GetFullPath([string]$stamp.path)
        if (-not $path.StartsWith($root + '\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Snapshot escapes project.' }
        $hash = ([BitConverter]::ToString($sha.ComputeHash([IO.File]::ReadAllBytes($path)))).Replace('-','')
        if ($hash -ine [string]$stamp.sha256) { throw 'Project/source changed since deletion review; replan.' }
    } } finally { $sha.Dispose() }
    $configFile = Join-Path $root 'Properties\tchmiconfig.json'
    $config = [IO.File]::ReadAllText($configFile) | ConvertFrom-Json
    if (([string]$config.startupView).Replace('\','/') -ieq $relative -or ([string]$config.loginPage).Replace('\','/') -ieq $relative) { throw 'Startup/login page protected.' }
    if ($Plan.folder -and @(Get-ChildItem -LiteralPath $target -Force).Count) { throw 'Nonempty folder deletion refused.' }
    foreach ($file in @($Plan.files)) {
        $cursor = $root
        foreach ($part in ([string]$file).Split('/')) {
            if ((Get-Item -LiteralPath $cursor).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Reparse point refused.' }
            $cursor = Join-Path $cursor $part
        }
        if ((Test-Path -LiteralPath $cursor) -and ((Get-Item -LiteralPath $cursor).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Reparse point refused.' }
    }
    $project = Get-TcHmiProjectObject $Dte $projectFile
    $automation = $Dte.GetObject('Beckhoff.TcHmi.1.12')
    $native = [TcAgentHmiNativeItems]::Find($automation,$project,$root)
    if (-not [TcAgentHmiNativeItems]::IsSaved($native)) { throw 'Unsaved HMI project metadata; save explicitly before deleting.' }
    $folder = [IO.Path]::GetDirectoryName($relative).Replace('\','/')
    $parent = [TcAgentHmiNativeItems]::Parent($native,$folder)
    $name = [IO.Path]::GetFileName($relative)
    $exists = [TcAgentHmiNativeItems]::Exists($parent,$name)
    if ($Plan.repair_orphan) { if ($exists -or (Test-Path -LiteralPath $target)) { throw 'Orphan repair target exists.' } }
    elseif (-not $exists -or -not (Test-Path -LiteralPath $target)) { throw 'Native delete target missing.' }
    $solutionFile=[string]$Dte.Solution.FullName
    Initialize-TcHmiDeleteLifecycle $Dte
    $deleteItemId=[TcAgentHmiDeleteLifecycle]::Probe($Dte,$solutionFile,$projectFile,$target)
    $backup = Join-Path $root ('.TwinCATAgent\backups\native-delete-' + [guid]::NewGuid().ToString('N'))
    [void][IO.Directory]::CreateDirectory($backup)
    [IO.File]::Copy($projectFile,(Join-Path $backup 'project.original'))
    [IO.File]::Copy($configFile,(Join-Path $backup 'config.original'))
    $generatedSchema = Join-Path $root 'Properties\tchmi.project.Schema.json'
    if ([IO.File]::Exists($generatedSchema)) { [IO.File]::Copy($generatedSchema,(Join-Path $backup 'schema.original')) }
    foreach ($file in @($Plan.files)) {
        $source = Join-Path $root $file
        if ([IO.File]::Exists($source)) { [IO.File]::Copy($source,(Join-Path $backup ([IO.Path]::GetFileName($file)))) }
    }
    $previous = [bool]$Dte.SuppressUI
    $result=@{status='incomplete';verified=$false;written=$false;backup_path=$backup;file=$relative;api='IVsHierarchyDeleteHandler3.DeleteItems';retry_safe=$false;reload_required=$false;reload_performed=$false;item_id=$deleteItemId}
    try {
        $automation.SuppressUi($true)
        if (-not $Plan.repair_orphan) {
            $result.written='unknown'
            [TcAgentHmiDeleteLifecycle]::Delete($Dte,$solutionFile,$projectFile,$target)
            [TcAgentHmiNativeItems]::Save($native)
        }
        foreach ($file in @($Plan.files)) { if (Test-Path -LiteralPath (Join-Path $root $file)) { throw 'Native deletion left a file/directory; no false success.' } }
        # Full native lifecycle owns configuration/schema synchronization.
        # No disk patching, reload, or companion-delete retry is permitted here.
        [xml]$xml=[IO.File]::ReadAllText($projectFile)
        $remaining=@($xml.SelectNodes('//*[@Include]') | Where-Object { $_.GetAttribute('Include').Replace('\','/').TrimEnd('/') -in @($Plan.files) })
        $savedConfig=[IO.File]::ReadAllText($configFile) | ConvertFrom-Json
        $left=@()
        foreach ($section in @('views','content','userControls','userFunctions','dependencyFiles')) {
            $left += @($savedConfig.$section | Where-Object { if ($null -eq $_) { return $false }; $id=if($_.PSObject.Properties.Name -contains 'url'){[string]$_.url}else{[string]$_.name}; $id.Replace('\','/').TrimEnd('/') -in @($Plan.files) })
        }
        $result.files_deleted=(@($Plan.files | Where-Object { Test-Path -LiteralPath (Join-Path $root $_) }).Count -eq 0)
        $result.project_registration_removed=($remaining.Count -eq 0)
        $result.config_registration_removed=($left.Count -eq 0)
        $result.node_removed=(-not [TcAgentHmiNativeItems]::Exists($parent,$name))
        if ([IO.Path]::GetExtension($relative) -eq '.usercontrol' -and [IO.File]::Exists($generatedSchema)) {
            $schema=[IO.File]::ReadAllText($generatedSchema) | ConvertFrom-Json
            $generatedLeft=@($schema.definitions.PSObject.Properties | Where-Object { ([string]$_.Value.frameworkUserControlConfig).Replace('\','/') -in @($Plan.files) })
            $result.generated_schema_removed=($generatedLeft.Count -eq 0)
            if (-not $result.generated_schema_removed) { throw 'XAE has not regenerated the UserControl schema; not complete.' }
        }
        $result.verified=$result.files_deleted -and $result.project_registration_removed -and $result.config_registration_removed -and $result.node_removed
        if (-not $result.verified) { throw 'Deletion readback did not close all checks.' }
        $result.status=if($Plan.repair_orphan){'repaired'}else{'deleted'}; $result.written=$true
    } catch {
        $result.error=$_.Exception.Message
        $result.failure_checks=@{files_deleted=$result.files_deleted;project_registration_removed=$result.project_registration_removed;config_registration_removed=$result.config_registration_removed;node_removed=$result.node_removed}
        # Leave the authoritative native outcome intact. Recovery is a separate
        # user-reviewed action; silently reloading for rollback defeats this contract.
        $result.recovery_required=$true
        $result.rollback_attempted=$false
        $result.status='incomplete';$result.verified=$false
    } finally { try { $Dte.SuppressUI=$previous } catch { $result.ui_restore_error=$_.Exception.Message } }
    return $result
}

function Invoke-TcHmiItemPlan {
    param($Dte, [string]$ProjectName, $Plan)
    $project = Get-TcHmiProjectObject $Dte $ProjectName
    $projectFile = Get-TcHmiResolvedFullName $project
    if ([IO.Path]::GetFullPath($projectFile) -ine [IO.Path]::GetFullPath([string]$Plan.project_file)) { throw 'HMI plan project mismatch.' }
    $root = [IO.Path]::GetDirectoryName($projectFile)
    $configFile = Join-Path $root 'Properties\tchmiconfig.json'
    function ConvertTo-CanonicalValue($Value) {
        if ($null -eq $Value) { return $null }
        if ($Value -is [System.Management.Automation.PSCustomObject]) {
            $sorted = [ordered]@{}
            foreach ($property in @($Value.PSObject.Properties | Sort-Object Name)) { $sorted[$property.Name] = ConvertTo-CanonicalValue $property.Value }
            return $sorted
        }
        if ($Value -is [System.Array]) {
            $items = @(); foreach ($element in $Value) { $items += ,(ConvertTo-CanonicalValue $element) }
            return ,$items
        }
        return $Value
    }
    function ConvertTo-CanonicalConfig([string]$Text) {
        $config = $Text | ConvertFrom-Json
        # XAE fills documented false defaults and sorts page registrations.
        # dependencyFiles order is intentionally NOT normalized (script order matters).
        foreach ($section in @('views','content','userControls')) {
            if ($config.PSObject.Properties.Name -contains $section) {
                foreach ($entry in @($config.$section)) {
                    if ($section -in @('views','content')) {
                        foreach ($field in @('preload','keepAlive','preloadBindings')) {
                            if ($entry.PSObject.Properties.Name -notcontains $field) { $entry | Add-Member -NotePropertyName $field -NotePropertyValue $false }
                        }
                        if ($section -eq 'content' -and $entry.PSObject.Properties.Name -notcontains 'loadSync') { $entry | Add-Member -NotePropertyName loadSync -NotePropertyValue $false }
                    }
                }
                $config.$section = @($config.$section | Sort-Object url)
            }
        }
        return (ConvertTo-CanonicalValue $config | ConvertTo-Json -Depth 100 -Compress)
    }
    function Get-ItemDigest([string]$Path) {
        $algorithm = [Security.Cryptography.SHA256]::Create()
        try { return ([BitConverter]::ToString($algorithm.ComputeHash([IO.File]::ReadAllBytes($Path)))).Replace('-','') }
        finally { $algorithm.Dispose() }
    }
    function Assert-PlanSnapshot {
        foreach ($stamp in @($Plan.contract_stamps)) {
            $item = Get-Item -LiteralPath ([string]$stamp.path) -ErrorAction Stop
            if ($item.Length -ne $stamp.size -or $item.LastWriteTimeUtc.Ticks -ne $stamp.ticks) { throw "Installed contract changed: $($stamp.path); generate a fresh plan." }
        }
        if ((Get-ItemDigest $projectFile) -ine $Plan.project_sha256 -or
            (Get-ItemDigest $configFile) -ine $Plan.config_sha256) { throw 'Project/config changed after preview; create a fresh plan.' }
    }
    function Resolve-NewItem([string]$Relative) {
        if ($Relative -notmatch '^[A-Za-z_][A-Za-z0-9_.\-/]*$' -or $Relative -match '(^|/)\.\.?(/|$)') { throw 'Unsafe HMI item path.' }
        $path = [IO.Path]::GetFullPath((Join-Path $root $Relative))
        if (-not $path.StartsWith($root.TrimEnd('\')+'\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Item path escapes project.' }
        $probe = Split-Path -Parent $path
        while ($probe -and $probe.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) {
            if ((Test-Path -LiteralPath $probe) -and ((Get-Item -LiteralPath $probe).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Reparse-point parent refused.' }
            if ($probe -ieq $root) { break }; $probe = Split-Path -Parent $probe
        }
        if (Test-Path -LiteralPath $path) { throw "Item already exists: $Relative" }
        return $path
    }
    $targets = @(); foreach ($file in @($Plan.files)) { $targets += Resolve-NewItem ([string]$file.relative) }
    $folderTarget = if ($Plan.kind -eq 'folder') { Resolve-NewItem ([string]$Plan.relative) } else { '' }
    Assert-PlanSnapshot
    # Saving is part of explicit apply; any changed editor/config invalidates the plan.
    $Dte.ExecuteCommand('File.SaveAll')
    Assert-PlanSnapshot
    $backup = Join-Path $root ('.TwinCATAgent\backups\items-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $backup -Force | Out-Null
    Copy-Item -LiteralPath $projectFile -Destination (Join-Path $backup 'project.original')
    Copy-Item -LiteralPath $configFile -Destination (Join-Path $backup 'config.original')
    $created = New-Object System.Collections.ArrayList
    $directories = New-Object System.Collections.ArrayList
    function Ensure-NewDirectory([string]$Directory) {
        if (Test-Path -LiteralPath $Directory) { return }
        $parent = Split-Path -Parent $Directory
        if ($parent -and -not (Test-Path -LiteralPath $parent)) { Ensure-NewDirectory $parent }
        New-Item -ItemType Directory -Path $Directory -ErrorAction Stop | Out-Null
        [void]$directories.Add($Directory)
    }
    $removed = $false; $changed = $false
    try {
        $Dte.Solution.Remove($project); $removed = $true
        Assert-PlanSnapshot
        if ($folderTarget) { Ensure-NewDirectory $folderTarget }
        for ($i=0; $i -lt $targets.Count; $i++) {
            Ensure-NewDirectory (Split-Path -Parent $targets[$i])
            # CreateNew prevents overwriting files created concurrently.
            $stream = [IO.File]::Open($targets[$i], [IO.FileMode]::CreateNew)
            [void]$created.Add($targets[$i])
            try { $bytes = [Text.Encoding]::UTF8.GetBytes([string]$Plan.files[$i].content); $stream.Write($bytes,0,$bytes.Length) } finally { $stream.Dispose() }
        }
        $changed = $true
        [IO.File]::WriteAllText($projectFile, [string]$Plan.project_content, (New-Object Text.UTF8Encoding($true)))
        [IO.File]::WriteAllText($configFile, [string]$Plan.config_content, (New-Object Text.UTF8Encoding($false)))
        $project = $Dte.Solution.AddFromFile($projectFile, $false)
        if ($null -eq $project) { throw 'XAE did not reload the project.' }
        $removed = $false
        for ($i=0; $i -lt $targets.Count; $i++) {
            if ([IO.File]::ReadAllText($targets[$i]) -cne [string]$Plan.files[$i].content) { throw 'Generated file readback mismatch.' }
        }
        [xml]$readback = [IO.File]::ReadAllText($projectFile)
        $includes = @($readback.SelectNodes('//*[@Include]') | ForEach-Object { $_.GetAttribute('Include').Replace('\','/').TrimEnd('/') })
        foreach ($file in @($Plan.files)) { if ($includes -notcontains [string]$file.relative) { throw 'Project registration readback mismatch.' } }
        if ($folderTarget -and $includes -notcontains [string]$Plan.relative) { throw 'Folder registration readback mismatch.' }
        $actualConfig = ConvertTo-CanonicalConfig ([IO.File]::ReadAllText($configFile))
        $expectedConfig = ConvertTo-CanonicalConfig ([string]$Plan.config_content)
        if ($actualConfig -cne $expectedConfig) {
            [IO.File]::WriteAllText((Join-Path $backup 'config.readback.json'), $actualConfig)
            [IO.File]::WriteAllText((Join-Path $backup 'config.expected.json'), $expectedConfig)
            throw 'Framework configuration semantic readback mismatch.'
        }
        return [pscustomobject]@{status='created'; written=$true; verified=$true; project_file=$projectFile; kind=$Plan.kind
            file=$Plan.relative; backup=$backup; reload_performed=$true; browser_verified=$false; note=$Plan.note}
    } catch {
        $failure = $_.Exception.Message
        if ($changed) {
            if (-not $removed) { $Dte.Solution.Remove($project); $removed = $true }
            Copy-Item -LiteralPath (Join-Path $backup 'project.original') -Destination $projectFile -Force
            Copy-Item -LiteralPath (Join-Path $backup 'config.original') -Destination $configFile -Force
        }
        foreach ($path in $created) { if ([IO.File]::Exists($path)) { [IO.File]::Delete($path) } }
        for ($i=$directories.Count-1; $i -ge 0; $i--) { [IO.Directory]::Delete($directories[$i], $false) }
        if ($removed) { $Dte.Solution.AddFromFile($projectFile,$false) | Out-Null }
        throw "HMI item creation failed; original files restored where changed. Backup: $backup. $failure"
    }
}
