from unittest.mock import Mock, patch

import pytest
from jsonschema import Draft202012Validator

from tc_agent import agent_core as ac
from tc_agent.tool_arguments import BatchFailureCounter


@pytest.mark.parametrize('tool', ac.REGISTRY, ids=lambda t: t['name'])
def test_registered_schemas_are_valid_and_missing_fields_never_dispatch(tool):
    Draft202012Validator.check_schema(tool['parameters'])
    if not tool['parameters'].get('required'):
        return
    with patch.object(ac, '_tool_precondition_failure') as probe, \
            patch.dict(tool, run=Mock(side_effect=AssertionError('must not execute'))):
        result = ac.run_tool(tool['name'], {})
    assert result['status'] == 'invalid_arguments'
    assert result['not_executed'] is True
    assert result['required_parameters'] == tool['parameters']['required']
    probe.assert_not_called()


@pytest.mark.parametrize('name,args', [
    ('plc_read_value', {'path': 'TIPC^Untitled1^Untitled1 Instance^fbPid.bEnable'}),
    ('plc_read_value', {'name': ''}),
    ('plc_read_value', {'name': 'TIPC^Untitled1^fbPid.bEnable'}),
    ('plc_write_value', {'name': 'MAIN.x'}),
    ('plc_read_values', {'symbols': [{'path': 'MAIN.x', 'type': 'bool'}]}),
    ('plc_write_values', {'values': [{'name': 'MAIN.x', 'type': 'bool', 'value': True, 'path': 'elsewhere'}]}),
    ('plc_read_value', {'name': 'MAIN.x', 'ads_port': 0}),
    ('plc_read_value', {'name': 'MAIN.x', 'max_depth': 9}),
    ('plc_read_value', []),
    ('plc_read_values', {'symbols': 'invalid json'}),
    ('plc_read_value', {'name': 'MAIN.x', 'max_depth': 'invalid'}),
])
def test_invalid_runtime_args_rejected_before_endpoint_or_permission_probes(name, args):
    with patch.object(ac, '_tool_precondition_failure') as probe, \
            patch.object(ac, 'ps_com') as com, patch.object(ac, 'read_ads_state') as ads:
        result = ac.run_tool(name, args)
    assert result['error_type'] == 'tool_arguments'
    assert result['not_executed'] is True
    assert 'ADS' in result['next_action']
    probe.assert_not_called()
    com.assert_not_called()
    ads.assert_not_called()


def test_correct_args_reach_handler_without_rewriting_symbol():
    tool = ac._BY_NAME['plc_read_value']
    handler = Mock(return_value={'status': 'read'})
    with patch.dict(tool, run=handler), patch.object(ac, '_tool_precondition_failure', return_value=None):
        assert ac.run_tool('plc_read_value', {'runtime': 'Untitled1', 'name': 'MAIN.fbPid.bEnable', 'max_depth': '3'}) == {'status': 'read'}
    handler.assert_called_once_with({'runtime': 'Untitled1', 'name': 'MAIN.fbPid.bEnable', 'max_depth': 3})


def test_hmi_legacy_normalization_and_open_property_bags_still_supported():
    assert ac.validate_tool_arguments('tc_hmi_controls_batch', {
        'file': 'Desktop.view', 'controls': [{'id': 'Button1', 'action': 'update',
                                             'attributes': {'data-tchmi-text': 'Hello'}}],
    }) is None


def test_invalid_value_not_echoed_in_schema_error():
    marker = 'private-user-value'
    result = ac.validate_tool_arguments('plc_read_value', {'name': [marker]})
    assert marker not in str(result)


def test_batch_of_five_bad_symbols_all_have_correction_results_and_one_strike():
    batch = BatchFailureCounter()
    streak = 0
    for member in ('bEnable', 'rOut', 'rFeedforward', 'rSetpoint', 'rActualValue'):
        result = ac.run_tool('plc_read_value', {'path': 'TIPC^P^fbPid.' + member})
        assert result['required_parameters'] == ['name']
        assert any('name' in issue.get('missing', []) for issue in result['issues'])
        streak = batch.record(streak, result, False)
    assert streak == 1
    # Five model responses with no correction still exhaust the existing limit.
    for _ in range(4):
        streak = BatchFailureCounter().record(streak, result, False)
    assert streak == 5


def test_real_failures_still_count_individually_and_success_resets():
    batch = BatchFailureCounter()
    streak = 0
    for _ in range(5):
        streak = batch.record(streak, {'error': 'ADS timeout'}, False)
    assert streak == 5
    assert batch.record(streak, {'status': 'read'}, True) == 0
    # Merely labelling an actual execution as a parameter error is not enough.
    assert batch.record(0, {'error_type': 'tool_arguments', 'not_executed': False}, False) == 1


def test_cli_returns_missing_arguments_to_model_before_asking_permission():
    provider = Mock()
    provider.complete.side_effect = [
        {'text': '', 'usage': {'in': 0, 'out': 0}, 'tool_calls': [
            {'id': 'bad1', 'name': 'plc_write_value', 'args': {'path': 'TIPC^P^x', 'value': True}}]},
        {'text': '需要先确认符号名称', 'usage': {'in': 0, 'out': 0}, 'tool_calls': []},
    ]
    with patch.object(ac, 'gate') as permission, patch.object(ac, 'run_tool') as run:
        ac.run('fixture', provider, verbose=False)
    permission.assert_not_called()
    run.assert_not_called()
    history = provider.complete.call_args.args[1]
    result = next(m for m in history if m.get('id') == 'bad1')['result']
    assert result['not_executed'] is True
