from unittest.mock import patch, Mock
from tc_agent import agent_core as ac
from tc_agent.execution_policy import FailurePolicy
from tc_template.semantic_evidence import classify


def failure():
    return {'status': 'blocked', 'written': False, 'review': {'project_context': {
        'semantic_evidence': classify([{'rule': 'semantic-unresolved', 'severity': 'error',
                                        'message': 'Unresolved declaration/library symbol: FB_SOCKETCONNECT'}])}}}


def test_template_contract_does_not_require_nonexistent_name():
    t = ac._BY_NAME['fblib_add']
    assert 'plc_object_scope' not in {c['condition'] for c in t['preconditions']}
    with patch.object(ac, 'ps_com', side_effect=lambda command, **kw:
                      {'logged_in':False} if command=='plc-online-state' else
                      {'plcs':[{'name':'PLC'}]} if command=='plc-runtimes' else
                      {'pid':123,'solution':'test.sln'}):
        assert ac._tool_precondition_failure('fblib_add', {'slug': 'tcpip-client'}, 123, t) is None


def test_template_slug_validation_before_import():
    with patch('tc_template.fblib.add_fb') as add:
        for slug in ('../other', 'x/y', 'C:\\other'):
            try: ac._fblib_add({'slug': slug})
            except ValueError: pass
            else: raise AssertionError('unsafe slug accepted')
        add.assert_not_called()


def test_valid_template_is_rendered_then_imported():
    with patch('tc_template.fblib.render_fb', return_value={'name': 'FB_Test'}) as render, \
         patch.object(ac, 'ps_com', return_value={'plcs': [{'name': 'PLC'}]}), \
         patch('tc_template.fblib.add_fb', return_value={'fb': 'FB_Test'}) as add:
        assert ac._fblib_add({'slug': 'tcpip-client'})['fb'] == 'FB_Test'
        render.assert_called_once()
        add.assert_called_once()


def test_template_ambiguous_project_never_imports():
    with patch('tc_template.fblib.render_fb', return_value={'name': 'FB_Test'}), \
         patch.object(ac, 'ps_com', return_value={'plcs': [{'name': 'A'}, {'name': 'B'}]}), \
         patch('tc_template.fblib.add_fb') as add:
        assert ac._fblib_add({'slug': 'tcpip-client'})['not_executed']
        add.assert_not_called()


def test_cross_tool_gap_stops_but_does_not_claim_read_recovery():
    policy = FailurePolicy()
    a = {'tree_path': 'TIPC^PLC^Project^POUs^FB_Test'}
    first, second = failure(), failure()
    policy.record('plc_write', a, first, False, False)
    policy.record('plc_patch', a, second, False, False)
    assert not first.get('capability_exhausted')
    assert second['capability_exhausted']
    assert not second.get('recovery_exhausted')


def test_different_project_and_library_change_reset():
    policy = FailurePolicy()
    policy.record('plc_write', {'path': 'TIPC^A^P^FB'}, failure(), False, False)
    result = failure()
    policy.record('plc_write', {'path': 'TIPC^B^P^FB'}, result, False, False)
    assert not result.get('capability_exhausted')
    policy.record('plc_lib_add', {}, {'status': 'added'}, True, False)
    assert not policy.semantic_failures


def test_unknown_is_not_code_error_but_still_incomplete():
    assert failure()['review']['project_context']['semantic_evidence']['code_error_count'] == 0
    assert classify([{'rule': 'semantic-type', 'severity': 'error'}])['status'] == 'invalid'
