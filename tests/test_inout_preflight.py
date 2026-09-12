import pytest

from tc_template.st_preflight import declarations, review_candidate


def check(actual, formal, expression='x', constant=False, readonly=False):
    obj = {'declaration': 'FUNCTION_BLOCK FB_Test\nVAR_IN_OUT' +
           (' CONSTANT' if readonly else '') + '\ny:' + formal + ';\nEND_VAR'}
    return review_candidate({
        'declaration': 'PROGRAM MAIN\nVAR fb:FB_Test; END_VAR\nVAR' +
                       (' CONSTANT' if constant else '') + '\nx:' + actual + '; END_VAR',
        'implementation': 'fb(y := ' + expression + ');',
    }, lambda name: obj if name.upper() == 'FB_TEST' else None)


@pytest.mark.parametrize('actual,formal', [('INT', 'DINT'), ('REAL', 'LREAL'), ('WORD', 'UINT')])
def test_inout_does_not_convert_storage(actual, formal):
    result = check(actual, formal)
    assert not result['approved']
    assert any('matching storage' in f['message'] for f in result['findings'])


def test_matching_variable_is_allowed():
    assert check('DINT', 'DINT')['approved']


@pytest.mark.parametrize('actual,formal,approved', [('STRING(10)', 'STRING(16)', False),
    ('STRING(20)', 'STRING(16)', True), ('WSTRING(10)', 'WSTRING(16)', False)])
def test_writable_string_capacity(actual, formal, approved):
    assert check(actual, formal)['approved'] is approved


def test_readonly_string_variable_may_be_shorter():
    assert check('STRING(10)', 'STRING(16)', readonly=True)['approved']


def test_writable_inout_rejects_constant():
    assert not check('DINT', 'DINT', constant=True)['approved']


def test_readonly_string_accepts_longer_literal():
    assert check('STRING(20)', 'STRING(2)', "'abcdef'", readonly=True)['approved']


def test_writable_string_rejects_literal():
    assert not check('STRING(20)', 'STRING(2)', "'abcdef'")['approved']


def test_constant_qualifier_is_retained():
    symbols, issues = declarations('VAR_IN_OUT CONSTANT s:STRING(2); END_VAR')
    assert not issues
    assert symbols['S']['constant'] is True


def test_non_string_readonly_constant_requires_compiler_option_evidence():
    result = review_candidate({'declaration': 'PROGRAM MAIN\nVAR fb:FB_Test; END_VAR\nVAR CONSTANT x:INT:=1; END_VAR',
                               'implementation': 'fb(y := x);'},
                              lambda n: {'declaration': 'FUNCTION_BLOCK FB_Test\nVAR_IN_OUT CONSTANT y:INT; END_VAR'})
    assert not result['approved']
    assert any(f['rule']=='semantic-unresolved' and 'Replace constants' in f['message'] for f in result['findings'])


def test_value_input_still_allows_numeric_conversion():
    result = review_candidate({'declaration': 'PROGRAM MAIN\nVAR fb:FB_Test; x:INT; END_VAR',
                              'implementation': 'fb(y := x);'},
                             lambda n: {'declaration': 'FUNCTION_BLOCK FB_Test\nVAR_INPUT y:DINT; END_VAR'})
    assert result['approved']
