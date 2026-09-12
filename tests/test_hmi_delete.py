import json
from pathlib import Path
import pytest
from tc_template.hmi_delete import plan_delete


@pytest.fixture
def project(tmp_path):
    (tmp_path/'Properties').mkdir()
    (tmp_path/'Pages').mkdir()
    (tmp_path/'Pages/Demo.content').write_text('<div/>')
    (tmp_path/'Demo.hmiproj').write_text('<Project><Content Include="Pages\\Demo.content"/><Content Include="Properties\\tchmiconfig.json"/></Project>')
    (tmp_path/'Properties/tchmiconfig.json').write_text(json.dumps({'startupView':'Desktop.view','content':[{'url':'Pages/Demo.content'}]}))
    return tmp_path/'Demo.hmiproj'


def test_plan_is_readonly_and_cleans_only_registration(project):
    before=project.read_bytes()
    result=plan_delete(project,'Pages/Demo.content')
    assert result['config_sections']==['content']
    assert result['reload_required'] is False
    assert project.read_bytes()==before
    assert (project.parent/'Pages/Demo.content').exists()


@pytest.mark.parametrize('field',['startupView','loginPage'])
def test_protected_page(project,field):
    cfg=project.parent/'Properties/tchmiconfig.json'
    cfg.write_text(json.dumps({field:'Pages/Demo.content'}))
    with pytest.raises(ValueError,match='Protected'): plan_delete(project,'Pages/Demo.content')


def test_reference_blocks(project):
    project.write_text(project.read_text().replace('</Project>','<Content Include="Other.js"/></Project>'))
    (project.parent/'Other.js').write_text("load('Pages/Demo.content')")
    with pytest.raises(ValueError,match='Referenced by'): plan_delete(project,'Pages/Demo.content')


def test_missing_is_not_success_and_orphan_repair_is_explicit(project):
    (project.parent/'Pages/Demo.content').unlink()
    with pytest.raises(ValueError,match='Missing'): plan_delete(project,'Pages/Demo.content')
    project.write_text('<Project/>')
    with pytest.raises(ValueError, match='separate offline repair'):
        plan_delete(project,'Pages/Demo.content',repair_orphan=True)


def test_nonempty_folder_is_protected(project):
    with pytest.raises(ValueError,match='empty folders'): plan_delete(project,'Pages')


def test_companion_owned_by_native_relative_path(project):
    root=project.parent
    (root/'Pages/Panel.usercontrol').write_text('<div/>')
    (root/'Pages/Panel.usercontrol.json').write_text('{}')
    project.write_text('<Project><Content Include="Pages/Panel.usercontrol"/><Content Include="Pages/Panel.usercontrol.json"><DependentUpon>Pages\\Panel.usercontrol</DependentUpon></Content></Project>')
    result=plan_delete(project,'Pages/Panel.usercontrol')
    assert result['files']==['Pages/Panel.usercontrol','Pages/Panel.usercontrol.json']


def test_delete_root_name_cannot_delete_nested_same_basename(project):
    root = project.parent
    (root/'00 Content').mkdir()
    (root/'00 Content/Demo.content').write_text('<div/>')
    project.write_text(project.read_text().replace('</Project>', '<Content Include="00 Content/Demo.content"/></Project>'))
    with pytest.raises(ValueError, match='not registered at exact path'):
        plan_delete(project, 'Demo.content')


def test_unknown_config_reference_blocks(project):
    cfg=project.parent/'Properties/tchmiconfig.json'
    cfg.write_text(json.dumps({'content':[{'url':'Pages/Demo.content'}],'custom':'Pages/Demo.content'}))
    with pytest.raises(ValueError,match='Other configuration'): plan_delete(project,'Pages/Demo.content')


def test_delete_uses_full_lifecycle_without_reload_or_file_patching():
    source=(Path(__file__).resolve().parents[1]/'tc_template/TcHmiItems.ps1').read_text(encoding='utf-8-sig')
    delete=source.split('function Invoke-TcHmiNativeDelete')[1].split('function Invoke-TcHmiItemPlan')[0]
    for forbidden in ('.Solution.Remove(','.Solution.AddFromFile(','.DeleteChild(', '[IO.File]::WriteAllText(', '::Delete($parent'):
        assert forbidden not in delete
    assert '[TcAgentHmiDeleteLifecycle]::Delete' in delete
    assert 'config_registration_removed' in delete and 'files_deleted' in delete
