import ast
import json
from pathlib import Path

import pytest

from tc_agent.generation_context import GenerationContext, MAX_PACKET_CHARS, HEADER, _bounded


def packet(context, messages=()):
    return json.loads(context.render(messages).split('<generation_data>\n')[1].split('\n</generation_data>')[0])


def test_syntax_contract_is_present_without_project_or_history():
    data = packet(GenerationContext('', 'PLC MAIN'))['domains']['plc']
    assert data['syntax_rules']['version'] == 3
    assert any('DINT(value)' in r for r in data['syntax_rules']['rules'])


def test_failed_project_preparation_still_preserves_syntax_contract():
    from unittest.mock import patch
    context = GenerationContext('', 'PLC MAIN')
    with patch.object(context, '_plc', side_effect=ValueError('project unavailable')):
        data = packet(context)['domains']['plc']
    assert data['status'] == 'unavailable'
    assert data['syntax_rules']['version'] == 3


@pytest.fixture
def plc(tmp_path):
    solution = tmp_path / 'Main.sln'
    solution.write_text('Project("type") = "PLC", "PLC.plcproj", "id"\nEndProject\n')
    (tmp_path / 'PLC.plcproj').write_text('''<Project><PropertyGroup><Name>PLC</Name></PropertyGroup>
      <ItemGroup><Compile Include="MAIN.TcPOU"/><PlaceholderReference Include="Tc2_Standard">
      <DefaultResolution>Tc2_Standard, 3.3.3.0 (Beckhoff)</DefaultResolution>
      </PlaceholderReference></ItemGroup></Project>''')
    (tmp_path / 'MAIN.TcPOU').write_text('''<TcPlcObject><POU Name="MAIN">
      <Declaration>PROGRAM MAIN\nVAR\n nCount : INT;\nEND_VAR</Declaration>
      <Implementation><ST>nCount := nCount + 1;</ST></Implementation>
      <Method Name="Reset"><Declaration>METHOD Reset : BOOL</Declaration></Method>
      </POU></TcPlcObject>''')
    profile = tmp_path / '.TwinCATAgent' / 'coding_profile.json'
    profile.parent.mkdir()
    profile.write_text(json.dumps({'extra_rules': ['Project-specific rule'], 'indent_spaces': 2}))
    return solution


@pytest.fixture
def hmi(tmp_path):
    solution = tmp_path / 'Hmi.sln'
    solution.write_text('Project("type") = "Hmi", "Hmi/Hmi.hmiproj", "id"\nEndProject\n')
    project = tmp_path / 'Hmi/Hmi.hmiproj'
    project.parent.mkdir()
    project.write_text('<Project><TargetFramework>native1.12-tchmi</TargetFramework></Project>')
    (project.parent / 'packages.config').write_text('<packages><package id="Test" version="1.0"/></packages>')
    runtime = tmp_path / 'Packages/Test.1.0/runtimes/native1.12-tchmi'
    runtime.mkdir(parents=True)
    types = {'Text': {'type': 'string'}, 'MeasurementValue': {'type': 'number'},
             'SolidColor': {'type': 'object', 'required': ['color'],
                            'properties': {'color': {'type': 'string'}}}}
    (runtime / 'Types.json').write_text(json.dumps({'definitions': types}))
    desc = {'name': 'TcHmiTextblock', 'namespace': 'TcHmi.Controls.Beckhoff',
            'dataTypes': [{'schema': 'Types.json'}], 'attributes': []}
    for name, value in [('id', 'Text'), ('data-tchmi-type', 'Text'), ('data-tchmi-text', 'Text'),
                        ('data-tchmi-width', 'MeasurementValue'), ('data-tchmi-height', 'MeasurementValue'),
                        ('data-tchmi-text-color', 'SolidColor')]:
        desc['attributes'].append({'name': name, 'type': 'tchmi:framework#/definitions/' + value})
    (runtime / 'Description.json').write_text(json.dumps(desc))
    return solution, project, runtime


def test_generic_conversation_does_not_read_projects(monkeypatch):
    ctx = GenerationContext('', '你好')
    monkeypatch.setattr(ctx, '_plc', lambda _: pytest.fail('unexpected project read'))
    monkeypatch.setattr(ctx, '_hmi', lambda _: pytest.fail('unexpected project read'))
    assert ctx.render() == ''


@pytest.mark.parametrize('task_text', ['写一个气缸控制', '修改声明区', 'PLC MAIN', 'FB_Motor'])
def test_plc_rules_are_prepared_before_any_tool_call(plc, task_text):
    data = packet(GenerationContext(str(plc), task_text))['domains']['plc']
    assert data['profile']['indent_spaces'] == 2
    assert data['profile']['extra_rules'] == ['Project-specific rule']
    assert '循环服务型' in data['rules']
    assert 'DefaultResolution' in data['saved_projects'][0]['libraries'][0]['resolution']
    assert data['interfaces'][0]['saved_declarations'][1]['member'] == 'Reset'
    assert data['dirty_unknown'] is True and data['live_xae'] is False


def test_context_survives_compaction_and_updates_rules(plc):
    ctx = GenerationContext(str(plc), '继续', messages=[{'role': 'user', 'text': '修改 PLC MAIN'}])
    first = packet(ctx)
    rules = plc.parent / '.TwinCATAgent/coding_profile.json'
    rules.write_text(json.dumps({'indent_spaces': 8}))
    second = packet(ctx, [{'role': 'user', 'text': '继续'}])
    assert first['domains']['plc']['profile']['indent_spaces'] == 2
    assert second['domains']['plc']['profile']['indent_spaces'] == 8


def test_changed_source_is_refreshed_without_sqlite_or_com(plc, monkeypatch):
    import tc_template._ps_bridge as bridge
    monkeypatch.setattr(bridge, 'ps_com', lambda *a, **k: pytest.fail('COM not allowed'))
    ctx = GenerationContext(str(plc), 'PLC MAIN')
    assert 'nCount' in json.dumps(packet(ctx))
    source = plc.parent / 'MAIN.TcPOU'
    source.write_text(source.read_text().replace('nCount', 'nUpdated'))
    assert 'nUpdated' in json.dumps(packet(ctx))
    assert not list(plc.parent.rglob('*.sqlite'))


def test_missing_project_does_not_guess_another():
    data = packet(GenerationContext('missing.sln', '修改PLC'))['domains']['plc']
    assert data['status'] == 'unavailable'
    assert 'next_action' in data and 'rules' in data


def test_hmi_contract_available_before_generation(hmi):
    solution, project, _ = hmi
    data = packet(GenerationContext(str(solution), '修改HMI'))['domains']['hmi']
    assert data['project_file'] == str(project)
    assert data['framework'] == 'native1.12-tchmi'
    assert data['types'][0]['type'] == 'TcHmi.Controls.Beckhoff.TcHmiTextblock'
    assert data['types'][0]['attributes']['data-tchmi-text-color'] == 'SolidColor'
    assert 'text-color' not in data['types'][0]['attributes']
    assert 'text_markup_example' in data
    assert data['online_verified'] is False


def test_hmi_contract_cache_refreshes_changed_schema(hmi):
    solution, _, runtime = hmi
    ctx = GenerationContext(str(solution), 'HMI')
    assert packet(ctx)['domains']['hmi']['value_schemas']['SolidColor']['type'] == 'object'
    path = runtime / 'Types.json'
    data = json.loads(path.read_text())
    data['definitions']['SolidColor'] = {'type': 'string', 'enum': ['changed']}
    path.write_text(json.dumps(data))
    result = packet(ctx)['domains']['hmi']
    assert result['value_schemas']['SolidColor']['type'] == 'string'
    assert result['text_markup_example']['status'] == 'unavailable'


def test_hmi_multiple_projects_require_selection(hmi):
    solution, project, _ = hmi
    other = project.parent / 'Other.hmiproj'
    other.write_text(project.read_text())
    solution.write_text(solution.read_text() + 'Project("type") = "Other", "Hmi/Other.hmiproj", "id2"\nEndProject\n')
    assert packet(GenerationContext(str(solution), '修改HMI'))['domains']['hmi']['status'] == 'project_selection_required'
    assert packet(GenerationContext(str(solution), '修改 Other HMI'))['domains']['hmi']['project_file'] == str(other)


def test_template_missing_is_not_reported_as_no_match(monkeypatch, tmp_path):
    import tc_template.fblib as fb
    monkeypatch.setattr(fb, 'fblib_dir', lambda: tmp_path / 'missing')
    assert GenerationContext()._templates('气缸')['status'] == 'unavailable'


def test_budget_is_atomic_and_keeps_explicit_omission(plc):
    assert _bounded({'source': 'x' * 500}, 100)['status'] == 'omitted'
    profile = plc.parent / '.TwinCATAgent/coding_profile.json'
    profile.write_text(json.dumps({'extra_rules': ['z' * 30000]}))
    rendered = GenerationContext(str(plc), 'PLC').render()
    assert len(rendered) <= MAX_PACKET_CHARS + len(HEADER) + 100
    assert 'omitted' in rendered
    assert packet(GenerationContext(str(plc), 'PLC'))['domains']['plc']['profile']['status'] == 'omitted'


def test_tool_outputs_and_reasoning_are_not_routing_instructions():
    ctx = GenerationContext('', '你好', messages=[
        {'role': 'tool', 'text': 'HMI PLC'},
        {'role': 'assistant', 'reasoning_content': 'HMI PLC', 'text': '你好'}])
    assert ctx.render() == ''


def test_explicit_tool_categories_prepare_for_terse_prompt(plc):
    assert 'plc' in packet(GenerationContext(str(plc), '修复', categories=['改代码']))['domains']


def test_sessions_do_not_share_project_catalogs(hmi, plc):
    a = GenerationContext(str(hmi[0]), 'HMI')
    packet(a)
    b = GenerationContext(str(plc), 'HMI')
    assert packet(b)['domains']['hmi']['status'] == 'unavailable'
    assert not b._catalogs


def test_both_backend_model_paths_use_prepared_system():
    source = (Path(__file__).parents[1] / 'tc_agent/backend.py').read_text(encoding='utf-8-sig')
    ast.parse(source)
    assert 'worker_provider.complete,' in source
    assert 'prepared_system + selected_contract_prompt(tools, ac.tool_metadata), context, tools' in source
    assert 'prepared_system, schema, visual=turn_visual' in source
    assert source.count('generation_context.render,') == 2
    assert 'if generation_context.solution != last_solution:' in source


def test_portable_contains_template_assets():
    source = (Path(__file__).parents[1] / 'scripts/build_portable.ps1').read_text(encoding='utf-8-sig')
    assert 'Join-Path $App "fblib"' in source
    assert '"*.yaml" "*.decl" "*.impl" "*.xml"' in source
    update = (Path(__file__).parents[1] / 'scripts/Update-LocalInstallation.ps1').read_text(encoding='utf-8-sig')
    assert "Name = 'fblib'" in update
