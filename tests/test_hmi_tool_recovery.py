import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from tc_agent import agent_core as ac, backend
from tc_agent.execution_policy import tool_succeeded, gate_rejected
from tc_agent.conversation_store import ConversationStore
from tc_agent.runtime import DurableRun, DurableAction, ActionRequest
from tc_template import hmi_events as events
from tc_template._ps_bridge import TcComError


@pytest.mark.parametrize('file', ['</function>\n</tool_call>', 'file>\nDesktop.view',
                                 'Desktop.view\x00', 'file:///C:/Desktop.view'])
def test_bad_hmi_file_never_reaches_com(file):
    with patch.object(ac, 'control_events') as invoked:
        result = ac.run_tool('tc_hmi_control_events', {'file': file, 'control_id': 'BtnStart'})
    assert '参数无效' in result['error']
    invoked.assert_not_called()
    with patch('tc_template._ps_bridge.com_hmi_project_info') as info:
        with pytest.raises(ValueError):
            events.control_events(file, 'BtnStart')
        info.assert_not_called()


@pytest.mark.parametrize('file', ['Desktop.view', 'Views/Panel.content', r'C:\HMI\Desktop.view',
                                 r'\\host\share\画面.usercontrol'])
def test_valid_hmi_file_selectors(file):
    events.validate_hmi_file(file)


@pytest.mark.parametrize('key', ['actionType', 'type', 'action'])
def test_action_guess_returns_contract_without_com(key):
    with patch('tc_template._ps_bridge.com_hmi_project_info') as info:
        result = ac.run_tool('tc_hmi_control_events', {'file': 'Desktop.view', 'control_id': 'BtnStart',
            'action': 'upsert', 'apply': False, 'actions': [{key: 'WriteToSymbol', 'value': True}]})
    info.assert_not_called()
    assert result['error_type'] == 'tool_arguments'
    assert result['not_executed'] is True
    assert result['action_contract']['discriminator'] == 'objectType'
    events.validate_actions([result['action_contract']['write_example']])


def test_schema_contains_required_action_and_value_shapes():
    schema = ac._BY_NAME['tc_hmi_control_events']['parameters']['properties']['actions']['items']
    assert schema['required'] == ['objectType']
    assert 'symbolExpression' in schema['properties']
    assert schema['properties']['value']['required'] == ['objectType']
    assert '先 action=read' in ac._BY_NAME['tc_hmi_control_events']['description']
    placement = ac._BY_NAME['tc_hmi_control_events']['parameters']['properties']['placement']
    assert placement['enum'] == ['native', 'custom']
    assert 'placement=native' in ac._BY_NAME['tc_hmi_control_events']['description']
    json.dumps(ac.tools_schema())  # No circular JSON schema references.


def test_event_action_rejects_raw_ads_mapping_path():
    action = events.action_contract()['write_example']
    action['symbolExpression'] = '%s%PLC1::GVL_Hmi::bStart%/s%'
    with pytest.raises(ValueError, match='Raw ADS MAPPING path'):
        events.validate_actions([action])


def test_unknown_event_lists_actual_available_events():
    markup = '<div id="Btn" data-tchmi-type="Button" />'
    with pytest.raises(events.HmiEventValidationError) as exc:
        events.plan_event(markup, 'Btn', '.onPress', [events.action_contract()['write_example']],
                          {'controls': {'Button': {'events': [{'name': '.onMouseDown'}]}}})
    assert exc.value.details['available_events'] == ['.onMouseDown']


@pytest.mark.parametrize('result', [{'status': 'blocked', 'written': False},
                                 {'review': {'approved': False}}, {'written': False}])
def test_gate_rejection_is_failure(result):
    assert gate_rejected(result)
    assert not tool_succeeded(result)


def test_preview_and_advisories_are_not_failures():
    assert tool_succeeded({'status': 'preview', 'written': False})
    assert tool_succeeded({'written': True, 'review': {'approved': True, 'advisories': ['warning']}})


def test_rejected_write_is_failed_in_durable_ledger(tmp_path):
    store = ConversationStore(tmp_path / 'agent.db')
    run = DurableRun(store, store.list_threads()[0]['id'], 'fixture', provider_model='offline', target_pid=12, solution='A.sln')
    action = DurableAction(store, ActionRequest(run_id=run.run_id, step_id='', tool_call_id='call1',
        tool_name='plc_write', arguments={'name': 'FB'}, category='代码', danger='', readonly=False, target_pid=12))
    result = {'status': 'blocked', 'written': False}
    action.finish(result, ok=tool_succeeded(result))
    assert action.execution['status'] == 'failed'
    run.finish('failed', 'fixture')


def test_actual_cli_batch_skips_writes_after_gate_but_keeps_reads():
    first = {'text': '', 'usage': {'in': 0, 'out': 0}, 'tool_calls': [
        {'id': '1', 'name': 'plc_write', 'args': {'name': 'FB', 'area': 'declaration', 'code': 'FUNCTION_BLOCK FB'}},
        {'id': '2', 'name': 'plc_write', 'args': {'name': 'FB', 'area': 'implementation', 'code': ''}},
        {'id': '3', 'name': 'plc_read', 'args': {'name': 'FB'}}]}
    provider = Mock()
    provider.complete.side_effect = [first, {'text': '需要修正声明', 'usage': {'in': 0, 'out': 0}, 'tool_calls': []}]
    with patch.object(ac, 'run_tool', side_effect=[{'status': 'blocked', 'written': False}, {'status': 'read'}]) as run:
        ac.run('fixture', provider, mode='auto', verbose=False)
    assert [c.args[0] for c in run.call_args_list] == ['plc_write', 'plc_read']
    history = provider.complete.call_args.args[1]
    assert next(m for m in history if m.get('id') == '2')['result']['written'] is False
    source = Path(backend.__file__).read_text(encoding='utf-8')
    assert source.count('elif batch_gate_blocked and not _tool_readonly(name):') == 2


def generated():
    return {'mode': 'transaction', 'enum_name': 'E_TestState', 'enum_declaration': 'TYPE E_TestState : (Idle); END_TYPE',
            'fb_name': 'FB_Test', 'fb_declaration': 'FUNCTION_BLOCK FB_Test', 'fb_implementation': ''}


def test_standard_fb_uses_empty_discovery_not_missing_read_exception():
    with patch.object(ac, '_generate_standard_fb', return_value=generated()), \
         patch.object(ac, 'ps_com', side_effect=[{'matches': [], 'total': 0}, {'status': 'created'}, {'status': 'created'}]) as com:
        result = ac._create_standard_fb({'name': 'FB_Test', 'with_enum': True})
    assert [c.args[0] for c in com.call_args_list] == ['find-pou', 'new-pou', 'new-pou']
    assert result['status'] == 'created'


@pytest.mark.parametrize('discovery', [TcComError('COM disconnected'),
    {'matches': [{'name': 'E_TestState', 'itemType': 604}]},
    {'matches': [{'name': 'E_TestState', 'itemType': 605}]*2}, {}])
def test_standard_fb_never_creates_on_connection_or_ambiguous_discovery(discovery):
    with patch.object(ac, '_generate_standard_fb', return_value=generated()), \
         patch.object(ac, 'ps_com', side_effect=[discovery]) as com:
        with pytest.raises((TcComError, ValueError)):
            ac._create_standard_fb({'name': 'FB_Test'})
    assert com.call_count == 1
