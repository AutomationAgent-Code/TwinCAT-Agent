import json
from pathlib import Path
from unittest.mock import patch

import pytest

from tc_template.hmi_item_evidence import inspect_created_item, inspect_deleted_item
from tc_template.hmi_items import create_item


def fixture(tmp_path, kind):
    (tmp_path / 'Properties').mkdir()
    (tmp_path / 'Items').mkdir()
    extension = {'folder': '', 'view': '.view', 'content': '.content', 'usercontrol': '.usercontrol',
                 'function_js': '.js', 'javascript': '.js', 'codebehind_js': '.js', 'css': '.css'}[kind]
    relative = 'Items/Test' + extension
    target = tmp_path / relative
    if kind == 'folder':
        target.mkdir()
    else:
        target.write_text('<div/>', encoding='utf-8')
    entries = [f'<{"Folder" if kind == "folder" else "Content"} Include="{relative}"/>']
    config = {}
    section = {'view': 'views', 'content': 'content', 'usercontrol': 'userControls', 'function_js': 'userFunctions'}.get(kind)
    if section:
        config[section] = [{'url': relative}]
    if kind in {'javascript', 'codebehind_js', 'function_js', 'css'}:
        config['dependencyFiles'] = [{'name': relative, 'type': 'Stylesheet' if kind == 'css' else 'JavaScript'}]
    if kind in {'usercontrol', 'function_js'}:
        companion = relative + '.json' if kind == 'usercontrol' else 'Items/Test.function.json'
        (tmp_path / companion).write_text('{}')
        entries.append(f'<Content Include="{companion}"><DependentUpon>{relative}</DependentUpon></Content>')
        if kind == 'usercontrol':
            (tmp_path / 'Properties/tchmi.project.Schema.json').write_text(json.dumps({
                'definitions': {'Test': {'frameworkUserControlConfig': companion}}}))
    project = tmp_path / 'Demo.hmiproj'
    project.write_text('<Project><ItemGroup>' + ''.join(entries) + '</ItemGroup></Project>')
    (tmp_path / 'Properties/tchmiconfig.json').write_text(json.dumps(config))
    return project, relative


@pytest.mark.parametrize('kind', ['folder', 'view', 'content', 'usercontrol', 'function_js', 'javascript', 'codebehind_js', 'css'])
def test_creation_requires_all_applicable_saved_state(tmp_path, kind):
    project, relative = fixture(tmp_path, kind)
    before = {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    result = inspect_created_item(project, relative, kind)
    assert result['saved_state_verified'], result
    assert not result['descriptor_schema_verified'] and not result['browser_verified']
    assert before == {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}


@pytest.mark.parametrize('fault,check', [('config', 'framework_registration'), ('schema', 'generated_schema_registration'),
    ('dependent', 'companion_ownership'), ('companion', 'companion_file'), ('duplicate', 'project_registration')])
def test_uc_partial_creation_is_not_success(tmp_path, fault, check):
    project, relative = fixture(tmp_path, 'usercontrol')
    if fault == 'config':
        (tmp_path / 'Properties/tchmiconfig.json').write_text('{}')
    elif fault == 'schema':
        (tmp_path / 'Properties/tchmi.project.Schema.json').write_text('{"definitions":{}}')
    elif fault == 'dependent':
        project.write_text(project.read_text().replace('<DependentUpon>Items/Test.usercontrol', '<DependentUpon>Other.usercontrol'))
    elif fault == 'companion':
        (tmp_path / (relative + '.json')).unlink()
    else:
        project.write_text(project.read_text().replace('</ItemGroup>', f'<Content Include="{relative}"/></ItemGroup>'))
    evidence = inspect_created_item(project, relative, 'usercontrol')
    assert not evidence['saved_state_verified'] and check in evidence['failed_checks']


def test_public_creation_does_not_report_node_only_success(tmp_path):
    project, relative = fixture(tmp_path, 'content')
    (tmp_path / 'Properties/tchmiconfig.json').write_text('{}')
    with patch('tc_template._ps_bridge.com_hmi_project_info', return_value={'project_file': str(project)}), \
         patch('tc_template.hmi_items.Catalog') as catalog, \
         patch('tc_template._ps_bridge.ps_com', return_value={'written': True, 'verified': True, 'relative': relative}):
        catalog.return_value.framework = 'native1.12-tchmi'
        result = create_item('content', 'Test', folder='Items', apply=True)
    assert result['status'] == 'incomplete' and not result['verified'] and not result['retry_safe']
    assert result['schema_verified']  # Valid markup alone is insufficient.


def deleted_fixture(tmp_path, kind):
    project, relative = fixture(tmp_path, kind)
    files = [relative]
    if kind in ('usercontrol', 'function_js'):
        files.append(relative + '.json' if kind == 'usercontrol' else 'Items/Test.function.json')
    config_path = tmp_path / 'Properties/tchmiconfig.json'
    config = json.loads(config_path.read_text())
    config['startupView'] = 'Desktop.view'
    config['dependencyFiles'] = config.get('dependencyFiles', []) + [
        {'name': 'UnrelatedA.js', 'type': 'JavaScript'}, {'name': 'UnrelatedB.js', 'type': 'JavaScript'}]
    config_path.write_text(json.dumps(config))
    project.write_text(project.read_text().replace('</ItemGroup>', '<Content Include="Unrelated.js"/></ItemGroup>'))
    backup = tmp_path / 'backup'
    backup.mkdir()
    (backup / 'project.original').write_bytes(project.read_bytes())
    (backup / 'config.original').write_bytes(config_path.read_bytes())
    schema_path = tmp_path / 'Properties/tchmi.project.Schema.json'
    if kind == 'usercontrol':
        schema = json.loads(schema_path.read_text())
        schema['definitions']['Other'] = {'type': 'string'}
        (backup / 'schema.original').write_text(json.dumps(schema))
        schema_path.write_text(json.dumps({'definitions': {'Other': {'type': 'string'}}}))
    for name in files:
        path = tmp_path / name
        path.rmdir() if path.is_dir() else path.unlink()
    project.write_text('<Project><ItemGroup><Content Include="Unrelated.js"/></ItemGroup></Project>')
    for section in ('views', 'content', 'userControls', 'userFunctions', 'dependencyFiles'):
        if section in config:
            config[section] = [e for e in config[section] if e.get('url', e.get('name')) not in files]
    config_path.write_text(json.dumps(config))
    return project, files, backup


@pytest.mark.parametrize('kind', ['folder', 'view', 'content', 'usercontrol', 'function_js', 'javascript', 'codebehind_js', 'css'])
def test_deleted_state_requires_preservation_and_does_not_claim_live_verification(tmp_path, kind):
    project, files, backup = deleted_fixture(tmp_path, kind)
    before = {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    result = inspect_deleted_item(project, files, backup)
    assert result['saved_state_verified'], result
    assert not result['no_reload_verified'] and not result['live_hierarchy_verified']
    assert before == {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}


@pytest.mark.parametrize('fault,check', [
    ('file', 'files_deleted'), ('registration', 'project_registration_removed'),
    ('config_leftover', 'config_registration_removed'), ('unrelated_config', 'other_config_preserved'),
    ('dependency_order', 'other_config_preserved'), ('unrelated_item', 'other_project_registrations_preserved'),
    ('schema_leftover', 'generated_schema_removed'), ('missing_schema', 'generated_schema_available'),
    ('unrelated_schema', 'other_generated_schema_preserved')])
def test_delete_partial_or_collateral_changes_are_rejected(tmp_path, fault, check):
    project, files, backup = deleted_fixture(tmp_path, 'usercontrol')
    config_path = tmp_path / 'Properties/tchmiconfig.json'
    schema_path = tmp_path / 'Properties/tchmi.project.Schema.json'
    config = json.loads(config_path.read_text())
    if fault == 'file':
        (tmp_path / files[1]).write_text('{}')
    elif fault == 'registration':
        project.write_text(project.read_text().replace('</ItemGroup>', f'<Content Include="{files[0]}"/></ItemGroup>'))
    elif fault == 'config_leftover':
        config['userControls'] = [{'url': files[0]}]
    elif fault == 'unrelated_config':
        config['startupView'] = 'Wrong.view'
    elif fault == 'dependency_order':
        config['dependencyFiles'].reverse()
    elif fault == 'unrelated_item':
        project.write_text('<Project/>')
    elif fault == 'schema_leftover':
        schema_path.write_bytes((backup / 'schema.original').read_bytes())
    elif fault == 'missing_schema':
        schema_path.unlink()
    else:
        schema_path.write_text('{"definitions":{}}')
    config_path.write_text(json.dumps(config))
    result = inspect_deleted_item(project, files, backup)
    assert not result['saved_state_verified'] and check in result['failed_checks'], result


def test_delete_wrapper_rejects_false_com_success_without_reload(tmp_path):
    from tc_template.hmi_delete import delete_item
    project, files, backup = deleted_fixture(tmp_path, 'content')
    config_path = tmp_path / 'Properties/tchmiconfig.json'
    config_path.write_bytes((backup / 'config.original').read_bytes())
    with patch('tc_template._ps_bridge.com_hmi_project_info', return_value={'project_file': str(project)}), \
         patch('tc_template.hmi_contract.Catalog') as catalog, \
         patch('tc_template.hmi_delete.plan_delete', return_value={'files': files}), \
         patch('tc_template._ps_bridge.ps_com', return_value={'verified': True, 'status': 'deleted', 'backup_path': str(backup)}) as invoke:
        catalog.return_value.framework = 'native1.12-tchmi'
        result = delete_item(files[0], apply=True)
    assert result['status'] == 'incomplete' and not result['verified']
    assert 'config_registration_removed' in result['saved_state_evidence']['failed_checks']
    assert invoke.call_count == 1  # No hidden repair or reload invocation.
