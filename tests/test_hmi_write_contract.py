import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from tc_template.hmi_contract import Catalog, HmiContractError, guarded_call, digest


@pytest.fixture
def project(tmp_path):
    project = tmp_path / 'Hmi' / 'Hmi.hmiproj'
    project.parent.mkdir()
    project.write_text('<Project><TargetFramework>native1.12-tchmi</TargetFramework></Project>')
    (project.parent / 'packages.config').write_text('<packages><package id="Example" version="1.0"/></packages>')
    runtime = tmp_path / 'Packages/Example.1.0/runtimes/native1.12-tchmi'
    runtime.mkdir(parents=True)
    defs = {'Text': {'type': 'string'}, 'Number': {'type': 'number', 'minimum': 0},
            'Mode': {'enum': ['Content', 'Value']}, 'Flag': {'type': 'boolean'},
            'Color': {'type': 'object', 'required': ['color'], 'additionalProperties': False,
                      'properties': {'color': {'type': 'string'}}},
            'Items': {'type': 'array', 'items': {'type': 'integer'}},
            'Trigger': {'type': 'array', 'items': {'type': 'object'}},
            'Nullable': {'anyOf': [{'type': 'number'}, {'type': 'null'}]}}
    (runtime / 'Types.Schema.json').write_text(json.dumps({'definitions': defs}))
    base = {'name': 'Base', 'namespace': 'TcHmi.Controls.Test', 'dataTypes': [{'schema': 'Types.Schema.json'}],
            'attributes': [{'name': 'id', 'type': 'tchmi:framework#/definitions/Text'},
                           {'name': 'data-tchmi-type', 'type': 'tchmi:framework#/definitions/Text'}]}
    (runtime / 'Description.json').write_text(json.dumps(base))
    child = runtime / 'Label'; child.mkdir()
    (child / 'Description.json').write_text(json.dumps({'name': 'Label', 'namespace': 'TcHmi.Controls.Test',
        'base': 'TcHmi.Controls.Test.Base', 'attributes': [
            {'name': 'data-tchmi-' + k.lower(), 'type': 'tchmi:framework#/definitions/' + k, 'bindable': True}
            for k in defs] + [{'name': 'data-tchmi-readonly', 'type': 'tchmi:framework#/definitions/Text', 'readOnly': True}]}))
    return project


def markup(attrs='', kind='Label', name='A'):
    return f'<div id="{name}" data-tchmi-type="TcHmi.Controls.Test.{kind}" {attrs}></div>'


@pytest.mark.parametrize('attrs', [
    'data-tchmi-text="Hello"', 'data-tchmi-text="%"', 'data-tchmi-number="10"', 'data-tchmi-flag="true"',
    'data-tchmi-mode="Content"', 'data-tchmi-nullable="null"', 'data-tchmi-items="[1,2]"',
    'data-tchmi-color="{&quot;color&quot;:&quot;#fff&quot;}"',
])
def test_legal_values_and_inherited_attributes(project, attrs):
    assert Catalog(project).validate(markup(attrs))['validated']


@pytest.mark.parametrize('attrs', [
    'data-tchmi-number="-1"', 'data-tchmi-number="abc"', 'data-tchmi-mode="Wrong"',
    'data-tchmi-color="#fff"', 'data-tchmi-color="{}"', 'data-tchmi-items="[1,true]"',
    'data-tchmi-unknown="1"', 'data-tchmi-readonly="x"', 'data-tchmi-flag="yes"',
    'data-tchmi-text="%{PLC1.X}%"',
])
def test_invalid_values_block(project, attrs):
    with pytest.raises(HmiContractError) as err:
        Catalog(project).validate(markup(attrs))
    assert err.value.details['written'] is False
    assert err.value.details['findings'][0]['attribute'].startswith('data-tchmi-')


def test_missing_type_and_duplicate_id(project):
    catalog = Catalog(project)
    with pytest.raises(HmiContractError):
        catalog.validate(markup(kind='NotInstalled'))
    with pytest.raises(HmiContractError):
        catalog.validate('<div>' + markup() + markup() + '</div>')


def test_manifest_declared_shared_types_are_loaded_and_stamped(project):
    runtime = project.parent.parent / 'Packages/Example.1.0/runtimes/native1.12-tchmi'
    schema = runtime / 'Shared.Schema.json'
    schema.write_text(json.dumps({'definitions': {'Shared': {'type': 'string'}}}))
    manifest = runtime / 'Manifest.json'
    manifest.write_text(json.dumps({'dataTypes': [{'schema': schema.name}]}))
    catalog = Catalog(project)
    assert not list(catalog.validator('tchmi:framework#/definitions/Shared').iter_errors('hello'))
    assert list(catalog.validator('tchmi:framework#/definitions/Shared').iter_errors(123))
    assert str(manifest.resolve()) in catalog.stamps
    assert str(schema.resolve()) in catalog.stamps


def test_manifest_types_cannot_escape_runtime(project):
    runtime = project.parent.parent / 'Packages/Example.1.0/runtimes/native1.12-tchmi'
    (runtime / 'Manifest.json').write_text(json.dumps({'dataTypes': [{'schema': '../Outside.json'}]}))
    with pytest.raises(HmiContractError, match='escapes installed runtime'):
        Catalog(project)


def test_manifest_conflicting_types_still_fail_closed(project):
    runtime = project.parent.parent / 'Packages/Example.1.0/runtimes/native1.12-tchmi'
    (runtime / 'Shared.Schema.json').write_text(json.dumps({'definitions': {'Text': {'type': 'number'}}}))
    (runtime / 'Manifest.json').write_text(json.dumps({'dataTypes': [{'schema': 'Shared.Schema.json'}]}))
    catalog = Catalog(project)
    assert 'Text' in catalog.conflicts
    assert 'Text' not in catalog.definitions


def test_manifest_alias_constraints_ignore_enum_order_not_property_names(project):
    runtime = project.parent.parent / 'Packages/Example.1.0/runtimes/native1.12-tchmi'
    (runtime/'Manifest.json').write_text(json.dumps({'dataTypes':[{'schema':'Shared.Schema.json'}]}))
    (runtime/'Shared.Schema.json').write_text(json.dumps({'definitions': {
        'Mode': {'$ref':'tchmi:framework#/definitions/Canonical'},
        'Canonical': {'enum':['Value','Content'], 'description':'Equivalent alias'},
        'Color': {'type':'object','required':['color'],'additionalProperties':False,
                  'properties':{'color':{'type':'string'},'default':{'type':'number'}}}}}))
    catalog = Catalog(project)
    assert 'Mode' not in catalog.conflicts
    assert 'Color' in catalog.conflicts  # actual property named default must not disappear
    assert not list(catalog.validator('tchmi:framework#/definitions/Mode').iter_errors('Value'))
    assert list(catalog.validator('tchmi:framework#/definitions/Mode').iter_errors('Wrong'))


def test_digit_first_id_is_valid_but_selector_punctuation_is_not(project):
    assert Catalog(project).validate(markup(name='02_Station'))['validated']
    with pytest.raises(HmiContractError):
        Catalog(project).validate(markup(name='02.Station'))


def test_owner_property_context_is_limited_to_typed_actions():
    from tc_template.hmi_symbols import check_symbol_value
    check_symbol_value({'objectType':'Symbol','symbolExpression':'%ctx%owner::ToggleState%/ctx%'})
    with pytest.raises(ValueError):
        check_symbol_value('%ctx%owner::ToggleState%/ctx%')


@pytest.mark.parametrize('declaration_path', ['dist/API/Trigger.d.ts', 'dist/TcHmiCore/_Types.d.ts', 'TcHmiFramework.d.ts'])
def test_condition_array_contract_needs_same_declared_runtime_evidence(project, declaration_path):
    runtime = project.parent.parent / 'Packages/Example.1.0/runtimes/native1.12-tchmi'
    path = runtime/'Types.Schema.json'
    schema = json.loads(path.read_text())
    schema['definitions']['Trigger'] = {'definitions': {'action': {'anyOf': [
        {'properties': {'objectType':{'enum':['Condition']}, 'parts': {'items': {'anyOf': [
            {'properties':{'if':{'$ref':'#/definitions/Trigger/definitions/expression'}}}
        ]}}}}]}}}
    path.write_text(json.dumps(schema))
    def comparison(catalog):
        return catalog.definitions['Trigger']['definitions']['action']['anyOf'][0]['properties']['parts']['items']['anyOf'][0]['properties']['if']
    assert '$ref' in comparison(Catalog(project))
    api = runtime/declaration_path
    api.parent.mkdir(parents=True, exist_ok=True)
    api.write_text('export interface ConditionIf { if: Expression[]; }\nexport interface ConditionElseIf { elseif: Expression[]; }')
    catalog = Catalog(project)
    assert comparison(catalog)['type'] == 'array'
    assert str(api.resolve()) in catalog.stamps


def test_designer_attributes_use_installed_schema(project):
    path = project.parent.parent / 'Packages/Example.1.0/runtimes/native1.12-tchmi/Description.json'
    data = json.loads(path.read_text())
    data['creator'] = {'attributes': [{'name': 'data-tchmi-creator-locked',
                                      'type': 'tchmi:framework#/definitions/Flag'}]}
    path.write_text(json.dumps(data))
    assert Catalog(project).validate(markup('data-tchmi-creator-locked="True"'))['validated']
    with pytest.raises(HmiContractError):
        Catalog(project).validate(markup('data-tchmi-creator-locked="invalid"'))
    with pytest.raises(HmiContractError):
        Catalog(project).validate(markup('data-tchmi-creator-invented="true"'))


def test_raw_json_script_comparison_operator_is_not_xml_markup(project):
    source = markup().replace('</div>', '<script type="application/json" data-tchmi-target-attribute="data-tchmi-trigger">'
                             '[{"compareOperator":"<", "text":"a & b"}]</script></div>')
    assert Catalog(project).validate(source)['validated']


def test_usercontrol_host_parameters_are_scoped_to_registered_target(project):
    runtime = project.parent.parent / 'Packages/Example.1.0/runtimes/native1.12-tchmi'
    host = runtime / 'Host'
    host.mkdir()
    (host / 'Description.json').write_text(json.dumps({'name': 'TcHmiUserControlHost',
        'namespace': 'TcHmi.Controls.System', 'base': 'TcHmi.Controls.Test.Base', 'attributes': [
        {'name': 'data-tchmi-target-user-control', 'type': 'tchmi:framework#/definitions/Text',
         'readOnly': True, 'requiredOnCompile': True, 'bindable': False}]}))
    project.write_text('<Project><TargetFramework>native1.12-tchmi</TargetFramework><ItemGroup>'
                       '<Content Include="Example.usercontrol"/></ItemGroup></Project>')
    (project.parent / 'Example.usercontrol').write_text(markup())
    (project.parent / 'Example.usercontrol.json').write_text(json.dumps({'parameters': [
        {'name': 'data-tchmi-caption', 'type': 'tchmi:framework#/definitions/Text'}]}))
    source = '<div id="Host1" data-tchmi-type="TcHmi.Controls.System.TcHmiUserControlHost" '
    source += 'data-tchmi-target-user-control="Example.usercontrol" data-tchmi-caption="Hello"/>'
    assert Catalog(project).validate(source)['validated']
    with pytest.raises(HmiContractError):
        Catalog(project).validate(source.replace('Example.usercontrol', '../Example.usercontrol'))
    with pytest.raises(HmiContractError):
        Catalog(project).validate(source.replace('data-tchmi-caption', 'data-tchmi-invented'))
    with pytest.raises(HmiContractError):
        Catalog(project).validate(source.replace('Example.usercontrol', 'Unregistered.usercontrol'))


def test_action_code_and_function_expressions_have_typed_contexts():
    from tc_template.hmi_symbols import check_symbol_value
    check_symbol_value({'objectType': 'JavaScript', 'sourceLines': ["var s = '%i%Status%/i%';"]})
    check_symbol_value({'objectType': 'FunctionExpression', 'functionExpression': '%i%Status%/i% + 1'})
    with pytest.raises(ValueError):
        check_symbol_value({'objectType': 'StaticValue', 'value': '=%i%Status%/i%'})
    with pytest.raises(ValueError):
        check_symbol_value({'sourceLines': ["var s = '%i%Status%/i%';"]})
    with pytest.raises(ValueError):
        check_symbol_value({'objectType': 'FunctionExpression', 'functionExpression': '%i%Status%/s% + 1'})


def test_package_catalog_uses_exact_recorded_shared_package_root(project, tmp_path):
    import shutil
    shared = tmp_path / 'SharedSolution/Packages'
    shared.parent.mkdir()
    shutil.move(str(tmp_path / 'Packages'), str(shared))
    schema = shared / 'Example.1.0/runtimes/native1.12-tchmi/Schema/TchmiConfig.Schema.json'
    schema.parent.mkdir()
    schema.write_text('{}')
    config = project.parent / 'Properties/tchmiconfig.json'
    config.parent.mkdir()
    import os
    config.write_text(json.dumps({'$schema': os.path.relpath(schema, config.parent).replace('\\', '/')}))
    Catalog(project).validate(markup('data-tchmi-text="Hello"'))
    from tc_template.hmi_events import catalog as event_catalog
    events = event_catalog(str(project))
    assert Path(events['controls']['TcHmi.Controls.Test.Label']['description_file']).is_relative_to(shared)


def test_package_catalog_rejects_schema_version_not_declared(project, tmp_path):
    schema = tmp_path / 'Packages/Example.9.0/runtimes/native1.12-tchmi/Schema/TchmiConfig.Schema.json'
    schema.parent.mkdir(parents=True)
    schema.write_text('{}')
    config = project.parent / 'Properties/tchmiconfig.json'
    config.parent.mkdir()
    config.write_text(json.dumps({'$schema': '../../Packages/Example.9.0/runtimes/native1.12-tchmi/Schema/TchmiConfig.Schema.json'}))
    with pytest.raises(HmiContractError, match='does not match'):
        Catalog(project)


def test_script_attribute_and_deferred_binding(project):
    catalog = Catalog(project)
    text = markup().replace('</div>', '<script data-tchmi-target-attribute="data-tchmi-color">{"color":"red"}</script></div>')
    assert catalog.validate(text)['validated']
    result = catalog.validate(markup('data-tchmi-number="%s%ADS.PLC1.X%/s%"'))
    assert result['deferred_binding_count'] == 1
    assert result['runtime_bindings_verified'] is False
    with pytest.raises(HmiContractError):
        catalog.validate(text.replace('{"color":"red"}', 'bad JSON'))


def test_unknown_schema_never_fetches_network(project):
    p = project.parent.parent / 'Packages/Example.1.0/runtimes/native1.12-tchmi/Label/Description.json'
    d = json.loads(p.read_text()); d['attributes'][0]['type'] = 'https://untrusted.invalid/schema'
    p.write_text(json.dumps(d))
    with pytest.raises(HmiContractError):
        Catalog(project).validate(markup('data-tchmi-text="x"'))


@pytest.mark.parametrize('command', ['hmi-create-view', 'hmi-write-markup', 'hmi-user-control-create', 'hmi-control-edit'])
def test_all_facade_write_paths_validate_before_apply(project, command):
    calls = []
    def call(cmd, **args):
        calls.append(args)
        return {'status': 'preview', 'project_file': str(project), 'markup': markup('data-tchmi-color="#fff"')}
    with pytest.raises(HmiContractError):
        guarded_call(command, {'apply': True, 'control_id': 'A'}, call)
    assert len(calls) == 1 and calls[0]['apply'] is False


def test_apply_proof_binds_candidate_project_and_metadata(project):
    calls = []
    def call(cmd, **args):
        calls.append(args)
        return ({'status': 'preview', 'project_file': str(project), 'markup': markup()} if not args['apply']
                else {'status': 'applied', 'verified': True})
    result = guarded_call('hmi-write-markup', {'apply': True, 'hmi_write_gate': {'fake': True}}, call)
    assert len(calls) == 2
    assert calls[1]['hmi_write_gate']['candidate_hash'] == digest(markup())
    assert calls[1]['hmi_write_gate']['project_file'] == str(project.resolve())
    assert calls[1]['hmi_write_gate']['files']
    assert result['browser_verification_required'] is True


def test_targeted_repair_does_not_require_fixing_other_controls(project):
    assert Catalog(project).validate('<div>' + markup(name='A') + markup(kind='Bad', name='B') + '</div>', 'A')['validated']


def test_batch_gate_builds_one_catalog_and_validates_all_targets(project):
    from tc_template import _ps_bridge as bridge
    content = '<div>' + markup(name='A') + markup(name='B') + markup(kind='OldBad', name='Old') + '</div>'
    calls = []
    def raw(command, *args, **kwargs):
        calls.append((command, kwargs))
        return ({'status': 'preview', 'project_file': str(project), 'markup': content,
                 'source_file': 'Desktop.view', 'source_hash': 'source'} if not kwargs['apply']
                else {'status': 'applied'})
    operations = [{'action': 'update', 'control_id': name} for name in ['A', 'B']]
    with patch.object(bridge, '_ps_com_raw', side_effect=raw), patch('tc_template.hmi_contract.Catalog', wraps=Catalog) as factory:
        result = bridge.ps_com('hmi-controls-batch', project=str(project),
                               file='Desktop.view', operations=operations, apply=True)
    assert factory.call_count == 1
    assert len(calls) == 2
    assert calls[1][1]['hmi_write_gate']['candidate_hash'] == digest(content)
    assert calls[1][1]['hmi_write_gate']['source_hash'] == 'source'
    assert result['write_contract']['checked_controls'] == 2


def test_invalid_later_batch_control_never_applies(project):
    calls = []
    def raw(command, **kwargs):
        calls.append(kwargs)
        return {'status': 'preview', 'project_file': str(project),
                'markup': '<div>' + markup(name='A') + markup('data-tchmi-number="bad"', name='B') + '</div>'}
    with pytest.raises(HmiContractError):
        guarded_call('hmi-controls-batch', {'apply': True, 'operations': [
            {'action': 'update', 'control_id': 'A'}, {'action': 'update', 'control_id': 'B'}]}, raw)
    assert len(calls) == 1 and not calls[0]['apply']


def test_batch_missing_target_and_removal_only(project):
    catalog = Catalog(project)
    with pytest.raises(HmiContractError):
        catalog.validate(markup(), control_ids=['A', 'Missing'])
    assert catalog.validate(markup(kind='OldBad'), control_ids=[])['checked_controls'] == 0


def test_noncontainer_children_rejected_from_installed_contract(project):
    catalog = Catalog(project)
    catalog.controls['TcHmi.Controls.Test.Label']['properties'] = {'containerControl':False}
    with pytest.raises(HmiContractError):
        catalog.validate(markup(name='Parent').replace('</div>', markup(name='Child')+'</div>'))


def test_failed_page_retains_draft_for_exact_repair(project):
    from tc_template.hmi_candidates import write_markup
    page=project.parent/'Desktop.view'
    page.write_text(markup(),encoding='utf-8')
    preview={'status':'preview','project_file':str(project),'file':'Desktop.view','source_file':str(page),
             'source_hash':digest(markup()),'markup':markup('data-tchmi-number="bad"')}
    with pytest.raises(HmiContractError) as error:
        guarded_call('hmi-write-markup',{'file':'Desktop.view','apply':True},lambda *a,**k:preview)
    key=error.value.details['candidate_id']
    with patch('tc_template._ps_bridge.ps_com',return_value={'status':'applied'}) as call:
        write_markup({'file':'Desktop.view','candidate_id':key,'replacements':[{'old':'"bad"','new':'"1"'}],'apply':True})
        assert '"1"' in call.call_args.kwargs['markup']
        assert call.call_args.kwargs['_candidate_source_hash']==preview['source_hash']
    page.write_text(markup('data-tchmi-text="user change"'),encoding='utf-8')
    with patch('tc_template._ps_bridge.ps_com') as call, pytest.raises(HmiContractError):
        write_markup({'file':'Desktop.view','candidate_id':key,'replacements':[{'old':'"bad"','new':'"1"'}]})
    call.assert_not_called()


def test_draft_race_between_repair_and_preview_is_blocked(project):
    calls=[]
    def call(*a,**kw):
        calls.append(kw)
        return {'status':'preview','project_file':str(project),'source_hash':'new','markup':markup()}
    with pytest.raises(HmiContractError):
        guarded_call('hmi-write-markup',{'apply':True,'_candidate_source_hash':'old'},call)
    assert len(calls)==1


def test_raw_ads_mapping_path_is_blocked_before_hmi_apply(project):
    calls = []
    def call(command, **kwargs):
        calls.append(kwargs)
        return {'status': 'preview', 'project_file': str(project),
                'markup': markup('data-tchmi-text="%s%PLC1::GVL_Hmi::sState%/s%"')}
    with pytest.raises(HmiContractError) as error:
        guarded_call('hmi-write-markup', {'file': 'Desktop.view', 'apply': True}, call)
    assert 'Raw ADS MAPPING path' in error.value.details['findings'][0]['message']
    assert len(calls) == 1 and calls[0]['apply'] is False


def test_generic_markup_cannot_bypass_event_tool(project):
    calls = []
    trigger = json.dumps([{'event': '.onPressed', 'actions': []}])
    candidate = markup('data-tchmi-trigger="' + trigger.replace('"', '&quot;') + '"')
    def call(command, **kwargs):
        calls.append(kwargs)
        return {'status': 'preview', 'project_file': str(project), 'markup': candidate}
    with pytest.raises(HmiContractError) as error:
        guarded_call('hmi-create-view', {'name': 'Desktop', 'apply': True}, call)
    assert 'tc_hmi_control_events' in str(error.value)
    assert len(calls) == 1


def test_dedicated_event_tool_carries_internal_placement_proof(project):
    trigger = json.dumps([{'event': '.onPressed', 'actions': [
        {'objectType': 'WriteToSymbol', 'symbolExpression': '%s%ADS.PLC1.GVL.bStart%/s%',
         'value': {'objectType': 'StaticValue', 'value': True}}]}])
    candidate = markup('data-tchmi-trigger="' + trigger.replace('"', '&quot;') + '"')
    calls = []
    def call(command, **kwargs):
        calls.append(kwargs)
        return ({'status': 'preview', 'project_file': str(project), 'markup': candidate}
                if not kwargs['apply'] else {'status': 'applied', 'verified': True})
    result = guarded_call('hmi-control-edit', {
        'file': 'Desktop.view', 'control_id': 'A', 'action': 'update',
        'attributes': {'data-tchmi-trigger': trigger}, '_event_placement': 'native',
        'apply': True,
    }, call)
    assert result['status'] == 'applied'
    assert '_event_placement' not in calls[0] and '_event_placement' not in calls[1]


def test_project_validation_enriches_old_zero_error_result(project):
    from tc_template import _ps_bridge as bridge
    project.write_text('<Project><TargetFramework>native1.12-tchmi</TargetFramework><ItemGroup>'
                       '<Content Include="Desktop.view"/></ItemGroup></Project>')
    (project.parent / 'Desktop.view').write_text('<div>' + ''.join(markup(kind='Unknown', name=f'C{i}') for i in range(60)) + '</div>')
    def raw(command, *args, **kwargs):
        return {'project_file': str(project)} if command == 'hmi-project-info' else {'valid': True, 'error_count': 0}
    with patch.object(bridge, '_ps_com_raw', side_effect=raw):
        result = bridge.ps_com('hmi-validate', project=str(project))
    assert result['valid'] is False
    assert result['schema_error_count'] == result['error_count'] == 60
    assert result['schema_findings_truncated'] is True


def test_powershell_raw_writes_require_matching_proof(tmp_path):
    source = (Path(__file__).parents[1] / 'tc_template/TcCom.ps1').read_text(encoding='utf-8-sig')
    function = source.split('function Get-TcHmiContractHash {', 1)[1].split('\nfunction New-TcHmiView', 1)[0]
    script = 'function Get-TcHmiContractHash {' + function + '''
    try { Assert-TcHmiWriteContract 'C:\\test.hmiproj' '<div/>' $true; throw 'unexpected-pass' }
    catch { if ($_.Exception.Message -notlike '*contract missing*') { throw }; 'missing-proof-blocked' }
    $script:HmiWriteGate = @{project_file='C:\\test.hmiproj';candidate_hash='wrong';files=@(@{path='missing'})}
    try { Assert-TcHmiWriteContract 'C:\\test.hmiproj' '<div/>' $true; throw 'unexpected-pass' }
    catch { if ($_.Exception.Message -notlike '*candidate changed*') { throw }; 'changed-candidate-blocked' }
    '''
    completed = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', script],
        capture_output=True, text=True, timeout=20)
    assert completed.returncode == 0, completed.stderr
    assert 'missing-proof-blocked' in completed.stdout and 'changed-candidate-blocked' in completed.stdout


def test_powershell_gate_precedes_mutation_and_preview_never_saves_all():
    source = (Path(__file__).parents[1] / 'tc_template/TcCom.ps1').read_text(encoding='utf-8-sig')
    for name in ['New-TcHmiView', 'Set-TcHmiMarkup', 'Edit-TcHmiControl', 'Edit-TcHmiControlsBatch', 'New-TcHmiUserControl']:
        body = source.split('function ' + name + ' {', 1)[1].split('\nfunction ', 1)[0]
        gate = body.index('Assert-TcHmiWriteContract')
        assert 'WriteAllText' not in body[:gate]
        assert "ExecuteCommand('File.SaveAll')" not in body[:gate]
        assert 'project_file=' in body
