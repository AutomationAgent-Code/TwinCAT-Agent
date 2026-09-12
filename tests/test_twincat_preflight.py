"""Regression fixtures for the Project14 syntax/diagnostic repair incident."""
from unittest.mock import patch

import pytest

from tc_template.plc_syntax import syntax_findings, generation_contract
from tc_template.plc_build_diagnostics import build_diagnostics, normalize_compiler_severity
from tc_agent.execution_policy import FailurePolicy
from tc_agent import agent_core as ac
from tc_template.plc_write_context import review_dependencies


@pytest.mark.parametrize('source', ['x := DINT(fb.nErrId);', 'x := REAL(n);',
                                   'x := UINT (1);', 'x := BOOL(n);'])
def test_invalid_cast_is_prewrite_error(source):
    findings = syntax_findings({'implementation': source})
    assert any(f['rule'] == 'syntax-invalid-conversion' for f in findings)


@pytest.mark.parametrize('source', ['x := UDINT_TO_DINT(n);', 'x := TO_DINT(n);',
                                   't := TIME();', "s := 'DINT(x)'; // INT(x)",
                                   '(* DINT(x) (* UINT(x) *) *) x := 1;'])
def test_valid_conversion_or_literal_not_blocked(source):
    assert not syntax_findings({'implementation': source})


def test_subrange_declaration_is_not_cast_but_initializer_is():
    assert not syntax_findings({'declaration': 'VAR n : INT(0..100); END_VAR'})
    assert syntax_findings({'declaration': 'VAR n : INT := DINT(x); END_VAR'})


@pytest.mark.parametrize('source', ["s := 'unfinished", 's := "unfinished',
                                   '(* outer (* closed *)'])
def test_unterminated_comment_or_literal(source):
    assert any(f['rule'] == 'syntax-unclosed-literal'
               for f in syntax_findings({'implementation': source}))


def test_existing_syntax_error_cannot_hide_in_delta_or_soft_gate():
    candidate = {'name': 'MAIN', 'declaration': 'PROGRAM MAIN',
                 'implementation': 'x := DINT(n);'}
    review = ac.review_write_candidate(candidate)
    delta = ac._delta_patch_review(review, review)
    with patch('tc_agent.config.load_config', return_value={'quality_gate_enabled': False}):
        assert ac._quality_review_blocks(delta)


def test_invalid_cast_never_calls_write_com():
    current = {'name': 'MAIN', 'path': 'TIPC^PLC1^Project^POUs^MAIN',
               'declaration': 'PROGRAM MAIN', 'implementation': ''}
    with patch.object(ac, 'ps_com', return_value=current) as com, \
            patch('tc_agent.config.load_config', return_value={'quality_gate_enabled': False}):
        result = ac._guarded_plc_write({'name': 'MAIN', 'area': 'implementation',
                                      'code': 'x := DINT(n);'})
    assert result['written'] is False
    assert [c.args[0] for c in com.call_args_list] == ['read-pou']


@pytest.mark.parametrize('source', ['CASE eState OF E_State.Starting: x := 1; END_CASE;',
                                  'IF eState = E_State.Starting THEN x := 1; END_IF;'])
def test_enum_member_checked_outside_assignments(source):
    def call(verb, **args):
        if verb == 'find-pou':
            return {'matches': [{'name': 'E_State', 'path': 'TIPC^PLC1^Project^DUTs^E_State'}]}
        return {'declaration': 'TYPE E_State : (Idle, Running); END_TYPE'}
    result = review_dependencies({'implementation': source}, 'TIPC^PLC1^Project^POUs^MAIN', call)
    assert any(f['rule'] == 'dependency-enum-member-missing' for f in result['findings'])


def raw_build(**overrides):
    return dict(buildPerformed=True, failedProjects=2, errorCount=0, errors=[],
                diagnosticsAvailable=True, warningCount=2, warnings=[
                    {'description': "Expression expected instead of 'DINT'", 'file': 'MAIN.TcPOU (Impl)',
                     'line': 41, 'raw_error_level': 2, 'severity': 'warning'},
                    {'description': 'Implicit conversion from enumeration type', 'file': 'FB.TcPOU',
                     'line': 5, 'raw_error_level': 2, 'severity': 'warning'}], **overrides)


def test_source_syntax_error_promoted_but_warning_retained():
    raw = raw_build()
    result = build_diagnostics(raw)
    assert result['errorCount'] == 1 and result['warningCount'] == 1
    assert result['repair_allowed'] and not result['compiler_verified']
    assert result['repair_diagnostics'][0]['line'] == 41
    assert result['errors'][0]['raw_error_level'] == 2
    assert result['errors'][0]['severity_inferred']
    assert raw['errorCount'] == 0
    assert normalize_compiler_severity(result) == result


def test_pending_is_not_silently_cleared_by_promotion():
    result = build_diagnostics(raw_build(diagnosticsPending=True))
    assert not result['diagnostics_complete'] and not result['repair_allowed']
    assert result['repair_diagnostics']


def test_reclassification_cannot_hide_missing_diagnostics():
    raw = raw_build()
    raw['errorCount'] = 3
    assert not build_diagnostics(raw)['diagnostics_complete']


def test_diagnostics_failure_does_not_exhaust_repair_budget():
    policy = FailurePolicy()
    policy.record('plc_build', {}, build_diagnostics(raw_build(diagnosticsPending=True)), False, True)
    assert policy.plc_failed_builds == 0
    assert policy.check('plc_verify', {})['status'] == 'blocked'
    assert policy.check('plc_patch', {})['written'] is False
    assert policy.check('plc_diagnostics', {}) is None
    policy.record('plc_diagnostics', {}, {'diagnostics_complete': True, 'errorCount': 1}, False, True)
    assert policy.check('plc_patch', {}) is None
    assert policy.check('plc_build', {})['status'] == 'blocked'
    policy.record('plc_patch', {}, {'written': True, 'verified': True}, True, False)
    assert policy.check('plc_build', {}) is None


def test_verify_and_build_share_repair_budget():
    policy = FailurePolicy()
    policy.record('plc_verify', {}, {'stages': [{'stage': 'build', 'result': raw_build()}]}, False, True)
    assert policy.plc_failed_builds == 1
    assert policy.check('plc_build', {})['status'] == 'blocked'


def test_rule_contract_contains_actual_generation_rules():
    contract = generation_contract()
    assert contract['compiler_verified'] is False
    assert any('DINT(value)' in r for r in contract['rules'])
    assert any('qualified existing members' in r for r in contract['rules'])


def test_verify_does_not_accept_zero_without_complete_diagnostics():
    with patch.object(ac, 'execute_plc_build', return_value={'failedProjects': 0, 'errorCount': 0, 'errors': []}), \
            patch.object(ac, 'ps_com', return_value=[{'name': 'MAIN'}]), \
            patch.object(ac, 'analyze_objects', return_value={'summary': {'errors': 0}}), \
            patch.object(ac, '_plc_read_values') as online:
        result = ac._plc_verify_workflow({'symbols': [{'name': 'MAIN.x'}]})
    assert not result['source_verified']
    online.assert_not_called()


def test_readonly_diagnostics_requires_bound_pid_and_never_builds():
    from tc_template._ps_bridge import com_diagnostics, tool_target
    with tool_target(0), patch('tc_template.xae_build_pipe.request_diagnostics') as read:
        assert not com_diagnostics()['diagnostics_complete']
        read.assert_not_called()
    with tool_target(123), patch('tc_template.xae_build_pipe.request_diagnostics', return_value=raw_build()) as read:
        result = com_diagnostics()
        assert not result['compiler_verified'] and result['buildPerformed'] is False
        assert not result['diagnostics_complete']  # failure with no classified errors
        read.assert_called_once_with(123)


def test_readonly_diagnostics_recovers_without_claiming_compilation():
    from tc_template._ps_bridge import com_diagnostics, tool_target
    raw = normalize_compiler_severity(raw_build())
    with tool_target(123), patch('tc_template.xae_build_pipe.request_diagnostics', return_value=raw):
        result = com_diagnostics()
    assert result['diagnostics_complete'] and not result['compiler_verified']
    assert ac.tool_metadata('plc_diagnostics')['readonly']
