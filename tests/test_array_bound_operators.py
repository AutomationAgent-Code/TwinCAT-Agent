import pytest
from tc_template.st_preflight import review_candidate


@pytest.mark.parametrize('expression,approved', [('LOWER_BOUND(a,1)',True),
    ('UPPER_BOUND(a,2)',True),('LOWER_BOUND(a,0)',False),('UPPER_BOUND(a,3)',False),
    ('LOWER_BOUND(n,1)',False),('LOWER_BOUND(a)',False),('UPPER_BOUND(a,TRUE)',False)])
def test_bound_operator(expression,approved):
    result=review_candidate({'declaration':'PROGRAM MAIN\nVAR a:ARRAY[-2..2,1..3] OF INT; n:DINT; END_VAR',
                             'implementation':'n := '+expression+';'})
    assert result['approved'] is approved, result


def test_open_array_bound_does_not_require_library_lookup():
    def unexpected(name):
        raise AssertionError(name)
    result=review_candidate({'declaration':'FUNCTION F:DINT\nVAR_IN_OUT a:ARRAY[*] OF INT; END_VAR',
                             'implementation':'F := LOWER_BOUND(a,1);'}, unexpected)
    assert result['approved']
    assert not result['compiler_verified']
