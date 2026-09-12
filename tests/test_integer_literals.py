import pytest
from tc_template.st_preflight import review_candidate
from tc_template.integer_literals import integer_literal
from tc_template.semantic_evidence import classify


@pytest.mark.parametrize('text,expected', [('16#FF',255),('2#1001_0011',147),
    ('8#67',55),('DINT#16#A1',161),('DINT#-1',-1),('1_000',1000)])
def test_integer_forms(text, expected):
    assert integer_literal(text)[0] == expected
    result = review_candidate({'declaration':'PROGRAM MAIN\nVAR x:DINT; END_VAR',
                               'implementation':'x := '+text+';'})
    assert result['approved'], result


@pytest.mark.parametrize('code', ['x := SINT#128;', 'x := UINT#-1;',
    'x := 8 / 16#0;', 'x := a[16#3];'])
def test_typed_range_bounds_and_zero(code):
    result = review_candidate({'declaration':'PROGRAM MAIN\nVAR x:DINT; a:ARRAY[0..2] OF DINT; END_VAR',
                               'implementation':code})
    assert not result['approved']
    assert any(f['rule'] in {'semantic-range','semantic-zero-divisor','semantic-array-bounds'} for f in result['findings'])


def test_unsupported_parser_is_not_a_code_error():
    result=review_candidate({'declaration':'PROGRAM MAIN','implementation':'{some_pragma} x := 1;'})
    evidence=classify(result['findings'])
    assert evidence['status']=='incomplete'
    assert evidence['code_error_count']==0
    assert not result['approved']
