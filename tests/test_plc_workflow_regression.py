"""Contract integration with a fake COM boundary; never touches a real XAE."""
from unittest.mock import patch
import pytest
from tc_agent import agent_core as ac, build_execution as be
from tc_template import _ps_bridge
from tc_template.plc_preflight import review_candidates

PATH = 'TIPC^PLC1^PLC1 Project^POUs^MAIN'


class FakeEngineering:
    def __init__(self):
        self.source = dict(name='MAIN', path=PATH, itemType=602,
                           declaration='PROGRAM MAIN\nVAR\n nCount : DINT; // test counter, count, default 0\nEND_VAR',
                           implementation='nCount := 0;', methods=[])
        self.calls = []

    def call(self, command, **args):
        self.calls.append(command)
        if command == 'read-pou':
            return dict(self.source)
        if command == 'write-pou':
            self.source[args['area']] = args['code']
            return {'status': 'written'}
        raise AssertionError('Unexpected operation: ' + command)


@pytest.mark.parametrize('build_success', [True, False])
def test_read_preflight_write_readback_build_boundary(build_success):
    fake = FakeEngineering()
    original = fake.call('read-pou')
    candidate = {**original, 'implementation': 'nCount := nCount + 1;'}
    preflight = review_candidates([candidate], fake.call)
    assert preflight['approved'] and not preflight['write_authorized']
    assert fake.calls == ['read-pou']
    with patch.object(ac, 'ps_com', side_effect=fake.call):
        written = ac._guarded_plc_write(dict(name='MAIN', path=PATH,
            area='implementation', code=candidate['implementation']))
    assert written['written'] and written['verified']
    assert not written['compiler_verified']
    assert fake.source['implementation'] == candidate['implementation']
    state = {'status': 'read', 'build_state': 1, 'last_build_info': 0, 'busy': False,
             'commands': {'build': {'name': 'Build.BuildSolution', 'available': True},
                          'rebuild': {'name': 'Build.RebuildSolution', 'available': True}}}
    responses = {'connect-check': {'pid': 42, 'solution': r'C:\Fake\Fake.sln'},
                 'build-state': state,
                 'project-info': {'solution': r'C:\Fake\Fake.sln', 'project_count': 1,
                                  'plc_projects': [{'name': 'PLC1', 'path': 'PLC1.plcproj'}]},
                 'plc-runtimes': {'target_netid': '127.0.0.1.1.1', 'plcs': []}}
    raw = {'buildPerformed': True, 'failedProjects': 0 if build_success else 1,
           'errorCount': 0 if build_success else 1,
           'errors': [] if build_success else [{'description': 'test compiler rejection', 'line': 1}],
           'warnings': [], 'diagnosticsAvailable': True}
    with patch.object(be, 'ps_com', side_effect=lambda c, **a: responses[c]), \
         patch('tc_template._ps_bridge.com_build', return_value=raw) as build, \
         _ps_bridge.tool_target(42):
        plan = be.capture_build_state({'action': 'build'})
        result = be.execute_plc_build({'action': 'build', 'build_plan_token': plan['build_plan_token']})
    build.assert_called_once()
    if build_success:
        assert result['compiler_verified']
    else:
        assert not result.get('compiler_verified')


def test_preflight_does_not_hide_other_write_quality_rules():
    candidate = dict(name='FB_Test', path=PATH.rsplit('^', 1)[0]+'^FB_Test',
                     declaration='FUNCTION_BLOCK FB_Test\nVAR_INPUT\nbInput:BOOL;\nEND_VAR',
                     implementation='bInput := TRUE;')
    r = review_candidates([candidate], lambda *a, **k: {})
    assert not r['approved']
    assert r['candidates'][0]['review']['write_review']['blocking_findings']


def test_passing_fragment_does_not_reset_repeated_capability_gap():
    from tc_agent.execution_policy import FailurePolicy
    policy = FailurePolicy()
    args = {'path': PATH}
    def failure():
        return {'semantic_evidence': {'status': 'incomplete', 'unsupported_reasons': ['missing_type']}}
    policy.record('plc_write', args, failure(), False, False)
    policy.record('plc_preflight', {'candidates': [args]}, {'approved': True}, True, True)
    again = failure()
    policy.record('plc_preflight', {'candidates': [args]}, again, False, True)
    assert again['capability_exhausted']


def test_write_readback_mismatch_does_not_claim_success_or_retry():
    fake = FakeEngineering()
    def ignored_write(command, **args):
        if command == 'write-pou':
            fake.calls.append(command)
            return {'status': 'written'}  # Transport success is not source proof.
        return fake.call(command, **args)
    with patch.object(ac, 'ps_com', side_effect=ignored_write):
        result = ac._guarded_plc_write(dict(name='MAIN', path=PATH,
            area='implementation', code='nCount := nCount + 1;'))
    assert result['written'] is True and result['verified'] is False
    assert result['failure_stage'] == 'post_write_readback'
    assert fake.calls.count('write-pou') == 1
    assert not result['compiler_verified']


def test_invalid_source_blocked_in_preflight_and_write_without_mutation():
    fake = FakeEngineering()
    candidate = {**fake.source, 'implementation': 'nCount := ;'}
    before = dict(fake.source)
    assert not review_candidates([candidate], fake.call)['approved']
    with patch.object(ac, 'ps_com', side_effect=fake.call):
        result = ac._guarded_plc_write(dict(name='MAIN', path=PATH,
            area='implementation', code=candidate['implementation']))
    assert result['written'] is False
    assert fake.source == before
    assert 'write-pou' not in fake.calls


def test_syntax_failure_does_not_attempt_library_recovery():
    def forbidden(*args, **kwargs):
        raise AssertionError('Syntax must be checked before external reads')
    result = review_candidates([{'name': 'MAIN', 'path': PATH,
        'declaration': 'PROGRAM MAIN\nVAR\nfb:UnknownFB;\nEND_VAR',
        'implementation': 'fb( ;'}], forbidden)
    assert not result['approved']
    assert result['candidates'][0]['review']['semantic_review']['reason'] == 'syntax_errors'
