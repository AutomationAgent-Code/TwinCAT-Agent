import pytest
from tc_template.type_contract import enum_definition
from tc_template.st_preflight import review_candidate


def test_enum_radix_auto_increment_and_default():
    enum=enum_definition('TYPE E_Test:(a:=16#10,b,c:=2#11,d) BYTE := E_Test.c;END_TYPE')
    assert enum['members']=={'A':16,'B':17,'C':3,'D':4}
    assert enum['default']=='C'


@pytest.mark.parametrize('body',[
    '(a:=255,b) BYTE', '(a:=-1,b) USINT', '(a,b) BOOL',
    '(a,A)', '(a,b) INT := missing', '(a,b) INT := Other.a',
    '(a:=SINT#256,b) INT'])
def test_invalid_enum_contract(body):
    result=enum_definition('TYPE E_Test:'+body+';END_TYPE')
    assert result['status']=='error'


def test_known_enum_error_is_not_just_missing_evidence():
    result=review_candidate({'declaration':'TYPE E_Test:(a:=255,b) BYTE;END_TYPE','implementation':''})
    assert any(f['rule']=='syntax-enum-contract' for f in result['findings'])


def test_expression_is_unknown_not_bad_code():
    assert enum_definition('TYPE E_Test:(a:=Base+1,b);END_TYPE')['status']=='unknown'


@pytest.mark.parametrize('base,size',[('',2),('BYTE',1),('DWORD',4),('ULINT',8)])
def test_enum_layout_keeps_nominal_type(base,size):
    candidate={'declaration':"{attribute 'qualified_only'}\n{attribute 'strict'}\nTYPE E_Test:(a,b) "+base+';END_TYPE','implementation':''}
    result=review_candidate(candidate)
    assert result['approved'],result
    assert result['memory_layout']['size']==size
    assert not result['memory_layout']['layout_verified']
