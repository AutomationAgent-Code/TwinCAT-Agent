import pytest
from unittest.mock import patch
from tc_template.plc_syntax import syntax_templates, syntax_findings
from tc_template.plc_build_diagnostics import build_diagnostics
from tc_template.lint import review_write_candidate
from tc_agent.completion_evidence import CompletionEvidence, completion_claims


@pytest.mark.parametrize('kind', list(syntax_templates()['templates']))
def test_template_syntax_consistency(kind):
    template = syntax_templates(kind)['templates'][kind]
    assert not syntax_findings(template)
    assert syntax_templates(kind)['compiler_verified'] is False


@pytest.mark.parametrize('area,source', [
    ('implementation', 'IF bRun THEN nX := 1;'),
    ('implementation', 'CASE nX OF 1: END_IF;'),
    ('implementation', 'fbTest(IN := TRUE;'),
    ('implementation', 'aX[1 := 2;'),
    ('declaration', 'VAR_INPUT\n bRun : BOOL;'),
    ('declaration', 'TYPE ST_X : STRUCT bX : BOOL; END_TYPE'),
    ('declaration', 'INTERFACE I_X\nEND_INTERFACE'),
    ('implementation', 'END_FUNCTION_BLOCK'),
])
def test_deterministic_syntax_failures(area, source):
    result = review_write_candidate({'name': 'X', area: source}, changed_area=area)
    assert any(f['rule'].startswith('syntax-') for f in result['blocking_findings'])


@pytest.mark.parametrize('source', [
    "s := 'IF END_CASE ('; // END_IF\n(* nested (* IF *) *)",
    'IF bA THEN CASE nA OF 0: fbX(); END_CASE; END_IF;',
    'REPEAT nA := nA + 1; UNTIL nA > 4 END_REPEAT;',
    '{IF defined(TEST)} IF bA THEN {ELSE} IF bB THEN {END_IF} nA := 1; END_IF;',
])
def test_valid_or_unsupported_syntax_is_not_false_blocked(source):
    assert not syntax_findings({'implementation': source})


def good(**kwargs):
    return dict(buildPerformed=True, failedProjects=0, errorCount=0, errors=[],
                diagnosticsAvailable=True, **kwargs)


def test_success_requires_explicit_complete_compiler_evidence():
    assert build_diagnostics(good())['compiler_verified']
    for field in ['buildPerformed', 'failedProjects', 'errorCount', 'errors', 'diagnosticsAvailable']:
        result = good()
        del result[field]
        assert not build_diagnostics(result)['compiler_verified']
    for override in [{'failedProjects': 1}, {'diagnosticsPending': True},
                     {'errorCount': 1}, {'truncated': True}, {'ok': False},
                     {'diagnosticsAvailable': False}, {'errorCount': False}]:
        result = build_diagnostics({**good(), **override})
        assert not result['compiler_verified']


def test_repair_requires_actual_diagnostics_not_synthetic_count():
    diagnostic = {'file': 'MAIN.TcPOU (Impl)', 'line': 3, 'description': "';' expected", 'code': 'C0006'}
    result = build_diagnostics({**good(), 'failedProjects': 1, 'errorCount': 1, 'errors': [diagnostic]})
    assert result['repair_allowed'] and result['status'] == 'failed'
    assert result['repair_diagnostics'][0]['line'] == 3
    assert result['repair_diagnostics'][0]['description'] == diagnostic['description']
    assert build_diagnostics(result)['diagnostic_fingerprint'] == result['diagnostic_fingerprint']
    result = build_diagnostics({**good(), 'failedProjects': 1, 'errorCount': 1,
                                'errorCountSource': 'failed-project-fallback'})
    assert not result['repair_allowed'] and result['status'] == 'incomplete'


def test_unlocated_build_failure_does_not_authorize_source_repair():
    raw = {**good(), 'failedProjects': 2, 'errorCount': 1,
           'errors': [{'description': 'Modbus/TcpIp license message from service log',
                       'file': '', 'line': 0}]}
    result = build_diagnostics(raw)
    assert result['status'] == 'failed'
    assert result['diagnostic_classification'] == 'unlocated_build_failure'
    assert not result['repair_allowed']
    assert 'Do not patch source' in result['next_action']


def test_final_evidence_retracts_positive_claim_after_failed_build():
    from tc_agent import backend

    evidence = CompletionEvidence()
    evidence.record('plc_build', {}, {
        **good(), 'failedProjects': 2, 'errorCount': 1,
        'errors': [{'description': 'old service/license message', 'file': '', 'line': 0}],
    }, True)
    assert completion_claims('代码语法正确，可以部署。')
    assert not completion_claims('编译未通过，不能部署。')
    state = backend._final_evidence_state(evidence, '代码语法正确，可以部署。', '', False)
    assert state['guarded']
    assert state['status'] == 'incomplete'
    assert state['is_error']
    assert '可部署' not in state['text']
    assert 'plc_build' in state['text']
    assert 'Do not patch source' in state['text']


def test_completion_invalidates_previous_build_after_uncertain_write():
    evidence = CompletionEvidence()
    evidence.record('plc_build', {}, good(), True)
    assert 'plc_build' in evidence.checked
    evidence.record('plc_patch', {}, {'status': 'uncertain', 'written': None, 'verified': False}, False)
    assert 'plc_build' not in evidence.checked
    assert 'plc' in evidence.changed
    evidence.record('plc_build', {}, {'errorCount': 0}, True)
    assert 'plc_build' not in evidence.checked
    evidence.record('plc_build', {}, good(), True)
    assert 'plc_build' in evidence.checked


def test_agent_build_exposes_repair_contract_and_template_is_readonly():
    from tc_agent import agent_core as ac
    # Build is effectful and now requires a token issued by a fresh read-only
    # status check; retain the existing diagnostics assertions through the
    # exact execution wrapper.
    tool = ac._BY_NAME['plc_build']
    original = tool['run']
    tool['run'] = lambda _args: good()
    try:
        with patch.object(ac, 'ps_com', return_value={
            'pid': 123, 'solution': r'C:\Machine\Machine.sln',
            'plc_projects': [], 'project_count': 1}), \
             patch.object(ac, '_tool_precondition_failure', return_value=None):
            result = ac.run_tool('plc_build', {
                'action': 'build', 'build_plan_token': 'test-token'})
    finally:
        tool['run'] = original
    assert result['buildPerformed'] is True
    assert ac.tool_metadata('plc_syntax_templates')['readonly']
    assert 'interface' in ac.run_tool('plc_syntax_templates', {'kind': 'interface'})['templates']


def test_repair_budget_and_unchanged_build_are_turn_local():
    from tc_agent.execution_policy import FailurePolicy
    policy = FailurePolicy()
    for i in range(4):
        assert policy.check('plc_build', {}) is None
        policy.record('plc_build', {}, {'compiler_verified': False, 'diagnostics_complete': True}, False, True)
        assert policy.check('plc_build', {})['status'] == 'blocked'
        policy.record('plc_patch', {'name': 'MAIN'}, {'written': True, 'verified': True}, True, False)
    assert policy.check('plc_build', {})['status'] == 'blocked'
    assert FailurePolicy().check('plc_build', {}) is None


def test_platform_preflight_failure_does_not_enter_source_repair_loop():
    from tc_agent.execution_policy import FailurePolicy
    policy = FailurePolicy()
    blocked = {'compiler_verified': False, 'diagnostics_complete': False,
               'build_performed': False, 'error_code': 'build_platform_context_invalid'}
    policy.record('plc_build', {}, blocked, False, True)
    assert policy.plc_failed_revision is None
    assert policy.plc_failed_builds == 0
    assert policy.plc_diagnostics_pending is False
    assert policy.check('plc_build', {}) is None
