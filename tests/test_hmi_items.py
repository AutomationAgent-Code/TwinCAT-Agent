import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch
import pytest
from tc_template.hmi_items import plan_item, safe_relative
from tc_template.hmi_paths import HmiPathError, resolve_registered, registered_matches


def test_native_project_lookup_avoids_popup_enumeration():
    source = (Path(__file__).resolve().parents[1] / 'tc_template/TcHmiItems.ps1').read_text(encoding='utf-8-sig')
    native = source.split('function Invoke-TcHmiItemPlan')[0]
    assert '.GetHmiProjects()' not in native
    assert '.GetProjects()' not in native
    assert '.GetHmiProject((EnvDTE.Project)project)' in native
    assert '$previousSuppressUi=[bool]$Dte.SuppressUI' in native
    assert '.SuppressUi($previousSuppressUi)' in native
    assert native.index('.SuppressUi($true)') < native.index('[TcAgentHmiNativeItems]::Find(')
    assert 'ConfigurationWindows($xaePid)' in native
    assert 'name.EndsWith(".view", StringComparison.OrdinalIgnoreCase)' in native
    assert 'item.Name = name' in native


@pytest.mark.parametrize('apply', [False, True])
def test_public_creation_never_falls_back_to_legacy(apply):
    from tc_template.hmi_items import create_item
    with patch('tc_template._ps_bridge.com_hmi_project_info', return_value={'project_file': 'Demo.hmiproj'}), \
         patch('tc_template.hmi_items.Catalog') as catalog, \
         patch('tc_template._ps_bridge.ps_com', return_value={'status': 'preview', 'written': False, 'fallback_allowed': False}) as call, \
         patch('tc_template.hmi_items.plan_item') as legacy:
        catalog.return_value.framework = 'native1.12-tchmi'
        result = create_item('content', 'Example', apply=apply)
    assert result['status'] == 'preview'
    assert result['written'] is False and result['fallback_allowed'] is False
    assert call.call_args.kwargs['apply'] is apply
    assert call.call_args.kwargs['kind'] == 'content'
    legacy.assert_not_called()


@pytest.fixture
def fixture(tmp_path):
    p = tmp_path/'Demo.hmiproj'
    p.write_text('<Project><ItemGroup/></Project>')
    config = tmp_path/'Properties/tchmiconfig.json'; config.parent.mkdir()
    config.write_text('{"$schema":"../Schema/TchmiConfig.Schema.json"}')
    schema = tmp_path/'Schema'; schema.mkdir()
    for name in ('UserControlConfig','FunctionDescription'):
        (schema/(name+'.Schema.json')).write_text('{}')
    with patch('tc_template.hmi_items.Catalog') as cls:
        cls.return_value.framework = 'native1.12-tchmi'
        cls.return_value.stamps = {}
        yield p


@pytest.mark.parametrize('value', ['../x', '/x', 'C:/x', 'a//b', 'CON', 'a/NUL.txt', 'Server/new', 'a/../b'])
def test_unsafe_paths_rejected(tmp_path, value):
    with pytest.raises(ValueError): safe_relative(tmp_path,value)


def test_exact_registered_path_does_not_match_nested_same_basename(tmp_path):
    project = tmp_path / 'Demo.hmiproj'
    (tmp_path / '00 Content').mkdir()
    (tmp_path / '00 Content/Desktop.view').write_text('<div/>')
    project.write_text('<Project><Content Include="00 Content/Desktop.view" /></Project>')
    with pytest.raises(HmiPathError, match='not registered at exact path'):
        resolve_registered(project, 'Desktop.view')
    assert resolve_registered(project, '00 Content/Desktop.view')[0] == '00 Content/Desktop.view'


def test_duplicate_exact_project_registration_fails_closed(tmp_path):
    project = tmp_path / 'Demo.hmiproj'
    (tmp_path / 'Desktop.view').write_text('<div/>')
    project.write_text('<Project><Content Include="Desktop.view" /><Content Include="Desktop.view" /></Project>')
    assert len(registered_matches(project, 'Desktop.view')) == 2
    with pytest.raises(HmiPathError, match='duplicate exact registrations'):
        resolve_registered(project, 'Desktop.view')


def test_folder_plan_is_readonly(fixture):
    before = fixture.read_bytes()
    r = plan_item(fixture, 'folder', 'Widgets', 'Pages')
    assert '<Folder Include="Pages/Widgets/"' in r['project_content']
    assert not (fixture.parent/'Pages').exists() and fixture.read_bytes() == before


def test_xae_config_normalization_preserves_meaning():
    script = r'''
$ErrorActionPreference='Stop'
$tokens=$null; $errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($env:ITEM_SCRIPT,[ref]$tokens,[ref]$errors)
foreach($f in $ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst]},$true)) {
 if($f.Name -in @('ConvertTo-CanonicalConfig','ConvertTo-CanonicalValue')) { . ([scriptblock]::Create($f.Extent.Text)) }
}
$a=ConvertTo-CanonicalConfig '{"views":[{"url":"B","preload":false},{"url":"A","preload":false}],"content":[{"url":"C","preload":false}]}'
$b=ConvertTo-CanonicalConfig '{"content":[{"url":"C","loadSync":false,"preload":false,"keepAlive":false,"preloadBindings":false}],"views":[{"url":"A","preload":false,"keepAlive":false,"preloadBindings":false},{"url":"B","preload":false,"keepAlive":false,"preloadBindings":false}]}'
if($a -cne $b) { throw 'Equivalent defaults/order not normalized' }
$c=ConvertTo-CanonicalConfig '{"views":[{"url":"B","preload":true},{"url":"A","preload":false}],"content":[{"url":"C","preload":false}]}'
if($a -ceq $c) { throw 'Meaningful change hidden' }
$d=ConvertTo-CanonicalConfig '{"dependencyFiles":[{"name":"A"},{"name":"B"}]}'
$e=ConvertTo-CanonicalConfig '{"dependencyFiles":[{"name":"B"},{"name":"A"}]}'
if($d -ceq $e) { throw 'Script ordering hidden' }
'''
    env = dict(os.environ, ITEM_SCRIPT=str(Path(__file__).resolve().parents[1]/'tc_template/TcHmiItems.ps1'))
    run = subprocess.run(['powershell','-NoProfile','-NonInteractive','-Command',script],env=env,capture_output=True,timeout=20)
    assert run.returncode == 0, run.stderr


@pytest.mark.parametrize('mode', ['success','reload_failure','stale'])
@pytest.mark.parametrize('kind', ['folder','javascript'])
def test_real_powershell_item_transaction(fixture, mode, kind):
    templates = fixture.parent/'templates'
    source = templates/'General/Blank_Js/Blank_Js.js'; source.parent.mkdir(parents=True)
    source.write_text('// $rootname$')
    r = plan_item(fixture,kind,'Widgets','Pages',template_root=templates)
    original = fixture.read_bytes()
    plan = fixture.parent/'plan.json'; plan.write_text(json.dumps(r))
    script = r'''
$ErrorActionPreference='Stop'
. $env:ITEM_SCRIPT
$plan=Get-Content -LiteralPath $env:ITEM_PLAN -Raw | ConvertFrom-Json
function Get-TcHmiProjectObject { param($Dte,$ProjectName) [pscustomobject]@{Name='Demo'} }
function Get-TcHmiResolvedFullName { param($Project) $plan.project_file }
$solution=New-Object psobject
$solution | Add-Member ScriptMethod Remove { param($p) }
$script:attempt=0
$solution | Add-Member ScriptMethod AddFromFile { param($p,$v) $script:attempt++; if($env:ITEM_MODE -eq 'reload_failure' -and $script:attempt -eq 1){throw 'fixture reload failure'}; [pscustomobject]@{Name='Demo'} }
$dte=[pscustomobject]@{Solution=$solution}
$dte | Add-Member ScriptMethod ExecuteCommand { param($c) }
if($env:ITEM_MODE -eq 'stale'){[IO.File]::AppendAllText($plan.project_file,' ')}
try { Invoke-TcHmiItemPlan $dte 'Demo' $plan | ConvertTo-Json -Depth 5 -Compress }
catch { @{error=$_.Exception.Message} | ConvertTo-Json -Compress }
'''
    env = dict(os.environ, ITEM_SCRIPT=str(Path(__file__).resolve().parents[1]/'tc_template/TcHmiItems.ps1'),
               ITEM_PLAN=str(plan),ITEM_MODE=mode)
    run = subprocess.run(['powershell','-NoProfile','-NonInteractive','-Command',script],env=env,capture_output=True,timeout=25)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    if mode == 'success':
        assert 'error' not in result, result
        assert result['verified']
        if kind == 'folder':
            assert (fixture.parent/'Pages/Widgets').is_dir()
        else:
            assert (fixture.parent/'Pages/Widgets.js').read_text() == '// Widgets'
            config = json.loads((fixture.parent/'Properties/tchmiconfig.json').read_text())
            assert config['dependencyFiles'][0]['name'] == 'Pages/Widgets.js'
    else:
        assert 'error' in result and not (fixture.parent/'Pages').exists()
        assert fixture.read_bytes() == original + (b' ' if mode == 'stale' else b'')
