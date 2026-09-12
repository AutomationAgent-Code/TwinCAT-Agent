from unittest.mock import Mock, patch
import pytest

from tc_template.st_parser import parse_implementation
from tc_template.st_preflight import review_candidate
from tc_template.plc_preflight import review_candidates
from tc_template.plc_build_diagnostics import normalize_compiler_severity


@pytest.mark.parametrize('source', [
    'n := 1;', 'IF b THEN n := 2; ELSIF c THEN n := 3; ELSE n := 4; END_IF',
    'FOR i := 0 TO 10 BY 2 DO a[i] := i; END_FOR;',
    'WHILE b DO fb(IN := TRUE, Q => b); END_WHILE;',
    'REPEAT n := n - 1; UNTIL n = 0 END_REPEAT;',
    'CASE e OF 0: n := 1; 1,2..4: n := 2; ELSE n := 0; END_CASE;',
    "s := 'IF ( $\' quoted'; (* nested (* END_IF *) *)",
    't := T#100ms; n := 16#FF; dt := DT#2026-09-10-12:00:00;',
    'p^ := n; n := a[1,2].value; p REF= n; b S= TRUE;',
])
def test_structured_statement_parsing(source):
    result = parse_implementation(source)
    assert result['status'] == 'parsed', result


def test_exst_assignment_expression():
    result = check('IF (b := TRUE) THEN n := 1; END_IF;')
    assert result['approved'], result
    assert check('n := n := 1;')['approved']


@pytest.mark.parametrize('source', [
    'x := 1 y := 2;', 'x := ;', 'IF b x := 1; END_IF;',
    'WHILE b x := 1; END_WHILE;', 'fb(IN := TRUE Q => b);',
    'CASE e OF 0 x := 1; END_CASE;', 'x := (1 + );',
    '1 := n;', 'x = 1;', 'FOR i := 0 10 DO x := 1; END_FOR;',
])
def test_errors_before_write(source):
    result = parse_implementation(source)
    assert result['status'] == 'invalid', result
    assert result['findings'][0]['line'] >= 1


def check(source, declaration='VAR n : INT; b : BOOL; END_VAR', objects=None):
    objects = objects or {}
    return review_candidate({'name': 'MAIN', 'declaration': declaration, 'implementation': source},
                            lambda name: objects.get(name.upper()))


@pytest.mark.parametrize('source,declaration', [
    ('b := 1;', 'VAR b : BOOL; END_VAR'),
    ("n := 'text';", 'VAR n : INT; END_VAR'),
    ('IF n THEN n := 1; END_IF;', 'VAR n : INT; END_VAR'),
    ('a[3] := 1;', 'VAR a : ARRAY[0..2] OF INT; END_VAR'),
    ('a[-1] := 1;', 'VAR a : ARRAY[0..2] OF INT; END_VAR'),
    ('a[1,2] := 1;', 'VAR a : ARRAY[0..2] OF INT; END_VAR'),
    ('n[1] := 1;', 'VAR n : INT; END_VAR'),
    ('FOR n := 0 TO 4 BY 0 DO END_FOR;', 'VAR n : INT; END_VAR'),
    ('EXIT;', ''), ('missing := 1;', ''),
    ('n := 1;', 'VAR n : INT b : BOOL; END_VAR'),
])
def test_semantic_errors_and_unknowns_do_not_pass(source, declaration):
    result = check(source, declaration)
    assert not result['approved'], result


def test_known_array_condition_and_conversion_pass():
    assert check('IF b THEN n := UDINT_TO_DINT(2); END_IF;')['approved']
    assert check('a[1] := 1;', 'VAR a : ARRAY[0..2] OF INT; END_VAR')['approved']


@pytest.mark.parametrize('declaration', ['VAR n:INT := TRUE; END_VAR',
    'VAR n:USINT := 256; END_VAR', 'VAR n:UINT := -1; END_VAR',
    'TYPE E_Test : (Idle Running); END_TYPE', 'VAR n:UnknownType; END_VAR'])
def test_declaration_only_candidate_does_not_bypass_semantics(declaration):
    assert not check('', declaration)['approved']


def test_valid_initializer_and_known_enum():
    assert check('', 'VAR n:INT := 2; b:BOOL := FALSE; END_VAR')['approved']
    assert check('', 'TYPE E_Test : (Idle := 0, Running := 1); END_TYPE')['approved']


def test_fb_signature_and_members():
    objects = {'FB_TEST': {'declaration': 'FUNCTION_BLOCK FB_Test\nVAR_INPUT bEnable : BOOL; END_VAR\nVAR_OUTPUT bDone : BOOL; END_VAR'}}
    decl = 'VAR fb : FB_Test; b : BOOL; END_VAR'
    assert check('fb(bEnable := TRUE, bDone => b); b := fb.bDone;', decl, objects)['approved']
    for source in ['fb(bWrong := TRUE);', 'fb(bEnable := 4);', 'b := fb.missing;',
                   'fb(bEnable := TRUE, bEnable := FALSE);', 'fb(bDone := TRUE);']:
        assert not check(source, decl, objects)['approved']


def test_public_method_signature_is_checked_without_execution():
    parent = {'declaration': 'FUNCTION_BLOCK FB_Test\nVAR n : INT; END_VAR'}
    method = {'declaration': 'METHOD PUBLIC Ready : BOOL\nVAR_INPUT nValue : INT; END_VAR'}
    resolve = Mock(return_value=parent)
    member = Mock(return_value=method)
    candidate = {'declaration': 'PROGRAM MAIN\nVAR fb : FB_Test; b : BOOL; END_VAR',
                 'implementation': 'b := fb.Ready(nValue := 1);'}
    assert review_candidate(candidate, resolve, member)['approved']
    candidate['implementation'] = 'b := fb.Ready(nWrong := 1);'
    assert not review_candidate(candidate, resolve, member)['approved']
    assert all(c.args == ('FB_TEST', 'Ready') for c in member.call_args_list)


def test_member_enclosing_variables_and_return_name():
    candidate = {'declaration': 'METHOD PUBLIC Ready : BOOL',
                 'enclosing_declaration': 'FUNCTION_BLOCK FB_Test\nVAR bReady:BOOL; END_VAR',
                 'implementation': 'Ready := bReady;'}
    assert review_candidate(candidate)['approved']


def test_output_argument_and_inout_require_writable_actuals():
    objects = {'FB_TEST': {'declaration': 'FUNCTION_BLOCK FB_Test\nVAR_IN_OUT n : INT; END_VAR\nVAR_OUTPUT b:BOOL; END_VAR'}}
    assert not check('fb(n := 1);', 'VAR fb:FB_Test; END_VAR', objects)['approved']
    assert not check('fb(b => TRUE);', 'VAR fb:FB_Test; END_VAR', objects)['approved']


def test_unknown_library_or_conditional_compilation_is_incomplete():
    for source, decl in [('fb(IN := TRUE);', 'VAR fb : TON; END_VAR'),
                         ('{IF defined(A)} n:=1; {END_IF}', 'VAR n:INT; END_VAR')]:
        result = check(source, decl)
        assert not result['approved'] and not result['semantic_complete']


def candidate(name, declaration, implementation='', project='PLC1'):
    return {'name': name, 'path': f'TIPC^{project}^Project^POUs^{name}',
            'declaration': declaration, 'implementation': implementation}


def test_batch_uses_future_enum_without_com_write_or_read():
    call = Mock(side_effect=AssertionError('No live dependency needed'))
    batch = [candidate('MAIN', 'PROGRAM MAIN\nVAR e : E_State; END_VAR', 'e := E_State.Starting;'),
             candidate('E_State', 'TYPE E_State : (Idle, Starting); END_TYPE')]
    result = review_candidates(batch, call)
    assert result['approved'], result
    assert not result['written'] and not result['compiler_verified']
    from tc_agent.execution_policy import tool_succeeded
    assert tool_succeeded(result)
    call.assert_not_called()
    batch[1]['declaration'] = 'TYPE E_State : (Idle); END_TYPE'
    assert not review_candidates(batch, call)['approved']


def test_batch_does_not_take_enum_from_other_project():
    call = Mock(return_value={'matches': []})
    result = review_candidates([
        candidate('MAIN', 'PROGRAM MAIN\nVAR e:E_State; END_VAR', 'e:=E_State.Idle;'),
        candidate('E_State', 'TYPE E_State : (Idle); END_TYPE', project='PLC2')], call)
    assert not result['approved']
    assert all(c.args[0] in {'find-pou', 'library-signatures'} for c in call.call_args_list)
    assert [c.kwargs['path'] for c in call.call_args_list if c.args[0] == 'library-signatures'] == ['TIPC^PLC1^Project']


def legacy():
    return {'ok': True, 'failedProjects': 2, 'errors': [], 'errorCount': 0,
            'warnings': [{'file': 'MAIN.TcPOU', 'line': 41, 'raw_error_level': 2,
                          'description': "Expression expected instead of 'DINT'"}],
            'warningCount': 1, 'diagnosticsPending': True, 'diagnosticsAvailable': True,
            'errorSource': 'dte-error-items-ui-thread', 'errorReadAttempts': 5,
            'message': 'Build failed, but no compiler error was exposed after 5 UI-thread reads; diagnosticsPending=true.'}


def test_known_old_pending_recovers_but_still_has_errors():
    result = normalize_compiler_severity(legacy())
    assert result['diagnosticsPending'] is False
    assert result['errorCount'] == 1 and result['original_diagnostics_pending']
    assert normalize_compiler_severity(result) == result


@pytest.mark.parametrize('change', [{'warningCount': 4}, {'truncated': True},
    {'diagnostics_complete': False}, {'message': 'timeout'}, {'errorSource': 'unknown'},
    {'diagnosticsAvailable': False}, {'ok': False}])
def test_unknown_pending_never_cleared(change):
    result = normalize_compiler_severity({**legacy(), **change})
    assert result['diagnosticsPending'] is True
