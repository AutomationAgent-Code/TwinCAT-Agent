import pytest

from tc_agent.tool_error_policy import annotate_error, classify_error


@pytest.mark.parametrize('result,ok,level,stop', [
    ({'status': 'ok'}, True, 'L0', False),
    ({'warnings': ['advisory']}, True, 'L1', False),
    ({'error': 'invalid argument', 'not_executed': True}, False, 'L2', False),
    ({'capability_exhausted': True}, False, 'L3', False),
    ({'recovery_exhausted': True}, False, 'L3', False),
    ({'status': 'approval_expired'}, False, 'L3', False),
    ({'authorization_blocked': True}, False, 'L3', True),
    ({'status': 'uncertain'}, False, 'L4', True),
    ({'written': True, 'verified': False}, False, 'L4', True),
    ({'written': True, 'verified': False, 'capability_exhausted': True}, False, 'L4', True),
])
def test_error_impact(result, ok, level, stop):
    before = dict(result)
    annotate_error(result, ok)
    assert result['error_policy']['level'] == level
    assert result['error_policy']['stop_turn'] is stop
    assert all(result[k] == v for k, v in before.items())
    assert 'retry_safe' not in result


def test_policy_is_recomputed_not_trusted_from_tool():
    result = {'uncertain': True, 'error_policy': {'stop_turn': False}}
    annotate_error(result, False)
    assert result['error_policy']['stop_turn']


def test_non_mapping_failure_is_not_success():
    assert classify_error('failure', False)['level'] == 'L2'
