import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from tc_template.hmi_batch import validate_operations
from tc_template.hmi_contract import HmiContractError, guarded_call


@pytest.mark.parametrize('operations', [
    None, [], [{}] * 101, [None], [{'action': 'oops', 'control_id': 'A'}],
    [{'action': 'add', 'control_id': 'A'}],
    [{'action': 'update', 'control_id': 'A'}] * 2,
    [{'action': 'update', 'control_id': 'A', 'extra': 1}],
    [{'action': 'update', 'control_id': 'A', 'attributes': {'data-tchmi-x': {}}}],
    [{'action': 'update', 'control_id': 'A', 'attributes': {'width': '1'}}],
    [{'action': 'update', 'control_id': 'A', 'attributes': {'data-tchmi-type': 'wrong'}}],
    [{'action': 'remove', 'control_id': 'A', 'attributes': {'data-tchmi-x': '1'}}],
    [{'action': 'update', 'control_id': 'A', 'parent_id': 'B'}],
])
def test_bad_input_rejected_before_xae(operations):
    call = Mock()
    with pytest.raises(HmiContractError):
        guarded_call('hmi-controls-batch', {'operations': operations, 'apply': True}, call)
    call.assert_not_called()


def test_hundred_operations_allowed():
    validate_operations([{'action': 'add', 'control_id': f'C{i}',
        'control_type': 'TcHmi.Controls.Test.Label', 'attributes': {'data-tchmi-text': '中文'}} for i in range(100)])


def test_agent_batch_is_mutating_and_routes_through_guarded_bridge():
    from unittest.mock import patch
    from tc_agent import agent_core
    tool = next(t for t in agent_core.REGISTRY if t['name'] == 'tc_hmi_controls_batch')
    assert tool['readonly'] is False
    assert tool['side_effect'] == 'project'
    assert tool['idempotency'] == 'ledger_guarded'
    ops = [{'action': 'update', 'control_id': 'A'}]
    with patch.object(agent_core, 'ps_com', return_value={'status': 'preview'}) as call:
        tool['run']({'file': 'Desktop.view', 'operations': ops})
    call.assert_called_once_with('hmi-controls-batch', project='', file='Desktop.view', operations=ops, apply=False)


def test_agent_batch_recovers_stringified_provider_arguments():
    from unittest.mock import patch
    from tc_agent import agent_core
    operations = json.dumps([
        {'id': 'BtnStop', 'attributes': {
            'data-tchmi-state-symbol': '%s%ADS.PLC1.GVL_Hmi.bStop%/s%'}},
    ])
    with patch.object(agent_core, 'ps_com', return_value={'status': 'applied'}) as call:
        result = agent_core.run_tool('tc_hmi_controls_batch', {
            'file': 'Desktop.view', 'action': 'update',
            'operations': operations, 'apply': 'True',
        })
    assert result['status'] == 'applied'
    call.assert_called_once_with(
        'hmi-controls-batch', project='', file='Desktop.view',
        operations=[{'action': 'update', 'control_id': 'BtnStop', 'attributes': {
            'data-tchmi-state-symbol': '%s%ADS.PLC1.GVL_Hmi.bStop%/s%'}}],
        apply=True,
    )


def test_agent_single_edit_recovers_attributes_and_false_boolean():
    from unittest.mock import patch
    from tc_agent import agent_core
    attributes = json.dumps({
        'data-tchmi-text': '%s%ADS.PLC1.GVL_Hmi.sLineState%/s%',
    })
    with patch.object(agent_core, 'ps_com', return_value={'status': 'preview'}) as call:
        result = agent_core.run_tool('tc_hmi_control_edit', {
            'file': 'Desktop.view', 'action': 'update', 'control_id': 'Kpi1Value',
            'attributes': attributes, 'apply': 'False',
        })
    assert result['status'] == 'preview'
    call.assert_called_once_with(
        'hmi-control-edit', project='', file='Desktop.view', action='update',
        control_id='Kpi1Value', type='', parent_id='',
        attributes={'data-tchmi-text': '%s%ADS.PLC1.GVL_Hmi.sLineState%/s%'},
        apply=False,
    )


def test_agent_full_markup_fallback_requires_explicit_acknowledgement():
    from tc_agent import agent_core
    result = agent_core.run_tool('tc_hmi_write_markup', {
        'file': 'Desktop.view', 'markup': '<div id="Desktop" />', 'apply': 'True',
    })
    assert result['status'] == 'blocked'
    assert result['written'] is False
    assert result['error_type'] == 'hmi_full_rewrite_not_acknowledged'
    assert 'tc_hmi_controls_batch' in result['recommended_tools']


def function_source(name):
    source = (Path(__file__).parents[1] / 'tc_template/TcCom.ps1').read_text(encoding='utf-8-sig')
    return 'function ' + name + ' {' + source.split('function ' + name + ' {', 1)[1].split('\nfunction ', 1)[0]


def run_batch(tmp_path, operations, *, apply=False, changed=False):
    page = tmp_path / 'Desktop.view'
    original = '<div id="Root" data-tchmi-type="TcHmi.Controls.System.TcHmiView"><div id="Old" data-tchmi-type="TcHmi.Controls.Test.Label" data-tchmi-text="old" /></div>'
    page.write_text(original, encoding='utf-8')
    code = '\n'.join(function_source(n) for n in (
        'Update-TcHmiControlXml', 'Edit-TcHmiControlsBatch', 'Get-TcHmiContractHash', 'Assert-TcHmiWriteContract'))
    code += '''
function Get-TcHmiProjectObject { return 'project' }
function Get-TcHmiResolvedFullName { return $script:Page }
function Get-TcHmiProjectPath { return 'root' }
function Resolve-TcHmiFilePath { return $script:Page }
function Convert-TcHmiXmlText($doc) { return $doc.OuterXml }
function Set-TcHmiDocumentText($dte, $path, $text) {
    $script:Writes++
    [IO.File]::WriteAllText($path, $text)
    return @{ verified=$true; load_mode='mock-dte'; backup='backup' }
}
'''
    # Only mock DTE/files inside pytest's temporary directory; no live XAE connection.
    code += "\n$script:Page = '" + str(page).replace("'", "''") + "'\n"
    code += "$ops = ConvertFrom-Json '" + json.dumps(operations).replace("'", "''") + "'\n"
    code += '$script:Writes=0\ntry {\n$p = Edit-TcHmiControlsBatch test project Desktop.view @($ops) $false\n'
    if apply:
        code += '''
$item = Get-Item -LiteralPath $script:Page
$script:HmiWriteGate = @{project_file=$script:Page; candidate_hash=(Get-TcHmiContractHash $p.markup)
    source_file=$script:Page; source_hash=$p.source_hash
    files=@(@{path=$script:Page; size=$item.Length; ticks=$item.LastWriteTimeUtc.Ticks})}
'''
        if changed:
            code += "[IO.File]::AppendAllText($script:Page, '<!-- changed -->')\n"
        code += '$p = Edit-TcHmiControlsBatch test project Desktop.view @($ops) $true\n'
    code += "@{writes=$script:Writes;result=$p} | ConvertTo-Json -Depth 12 -Compress\n} catch { @{writes=$script:Writes;error=$_.Exception.Message} | ConvertTo-Json -Compress }"
    result = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', code],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout), page.read_text(encoding='utf-8'), original


def test_batch_one_write_parent_child_update_remove(tmp_path):
    ops = [{'action': 'add', 'control_id': 'Parent', 'control_type': 'TcHmi.Controls.Test.Label'},
           {'action': 'add', 'control_id': 'Child', 'parent_id': 'Parent',
            'control_type': 'TcHmi.Controls.Test.Label', 'attributes': {'data-tchmi-text': 'hello'}},
           {'action': 'remove', 'control_id': 'Old'}]
    result, text, _ = run_batch(tmp_path, ops, apply=True)
    assert 'error' not in result, result
    assert result['writes'] == 1
    assert result['result']['operation_count'] == 3
    assert all(o['verified'] for o in result['result']['operations'])
    assert 'hello' in text and 'id="Old"' not in text


def test_batch_preview_never_writes(tmp_path):
    result, text, original = run_batch(tmp_path, [{'action': 'update', 'control_id': 'Old',
        'attributes': {'data-tchmi-text': None}}])
    assert 'error' not in result, result
    assert result['writes'] == 0 and text == original
    assert 'data-tchmi-text' not in result['result']['markup']


@pytest.mark.parametrize('ops', [
    [{'action': 'update', 'control_id': 'Old'}, {'action': 'remove', 'control_id': 'Missing'}],
    [{'action': 'remove', 'control_id': 'Root'}],
    [{'action': 'update', 'control_id': 'Old'}] * 2,
    [{'action': 'add', 'control_id': 'Child', 'parent_id': 'Old', 'control_type': 'TcHmi.Controls.Test.Label'},
     {'action': 'remove', 'control_id': 'Old'}],
])
def test_batch_failure_is_not_partial_write(tmp_path, ops):
    result, text, original = run_batch(tmp_path, ops, apply=True)
    assert result.get('error'), result
    assert result['writes'] == 0 and text == original


def test_batch_changed_source_is_preserved(tmp_path):
    result, text, original = run_batch(tmp_path, [{'action': 'update', 'control_id': 'Old'}], apply=True, changed=True)
    assert result.get('error'), result
    assert result['writes'] == 0 and text == original + '<!-- changed -->'


def test_batch_performance_contract_72_controls_one_save(tmp_path):
    ops = [{'action': 'add', 'control_id': f'C{i}', 'control_type': 'TcHmi.Controls.Test.Label'} for i in range(72)]
    result, _, _ = run_batch(tmp_path, ops, apply=True)
    assert 'error' not in result, result
    assert result['writes'] == 1 and result['result']['operation_count'] == 72
