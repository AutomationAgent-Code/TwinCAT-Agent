import pytest
from tc_template.st_preflight import review_candidate
from tc_template.plc_syntax import generation_contract


def review(code, declaration='VAR x:DINT; r:REAL; END_VAR', resolve=None):
    return review_candidate({'declaration': 'PROGRAM MAIN\n' + declaration,
                             'implementation': code}, resolve)


@pytest.mark.parametrize('code', ['x := 1;', 'FOR x := 1 TO 3 DO END_FOR'])
def test_constant_write_blocked(code):
    result = review(code, 'VAR CONSTANT x:DINT := 0; END_VAR')
    assert any(f['rule'] == 'semantic-constant-write' for f in result['findings'])


def test_constant_initializer_allowed():
    assert review('', 'VAR CONSTANT x:DINT := 0; END_VAR')['approved']


def test_constant_array_element_blocked():
    result = review('a[0] := 2;', 'VAR CONSTANT a:ARRAY[0..1] OF DINT; END_VAR')
    assert any(f['rule'] == 'semantic-constant-write' for f in result['findings'])


def test_output_to_constant_blocked():
    result = review('fb(y => x);', 'VAR fb:FB_Test; END_VAR VAR CONSTANT x:DINT:=0; END_VAR',
                    lambda n: {'declaration': 'FUNCTION_BLOCK FB_Test\nVAR_OUTPUT y:DINT; END_VAR'})
    assert any(f['rule'] == 'semantic-constant-write' for f in result['findings'])


@pytest.mark.parametrize('code', ['x := 4 / 0;', 'x := 4 MOD -0;'])
def test_literal_zero_divisor(code):
    assert any(f['rule'] == 'semantic-zero-divisor' for f in review(code)['findings'])


def test_mod_real_rejected():
    assert any(f['rule'] == 'semantic-operator' for f in review('r := r MOD 2;')['findings'])


@pytest.mark.parametrize('code', ['x := 4 MOD 2;', 'r := r / 2;', 'x := 4 / x;'])
def test_no_guessed_dynamic_zero(code):
    assert review(code)['approved']


def test_generation_includes_official_rules_and_sources():
    contract = generation_contract()
    assert contract['version'] == 3
    assert any('2528880779' in url for url in contract['sources'])
    assert any('CONSTANT' in rule for rule in contract['rules'])
    assert contract['compiler_verified'] is False
