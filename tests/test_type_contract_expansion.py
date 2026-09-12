import pytest
from tc_template.st_preflight import review_candidate
from tc_template.plc_syntax import syntax_findings


def run(decl, impl, objects=None):
    return review_candidate({'declaration':decl,'implementation':impl}, lambda n:(objects or {}).get(n.upper()))


def test_array_element_mismatch_rejected():
    result=run('PROGRAM MAIN\nVAR fb:FB_Test; a:ARRAY[0..2] OF LREAL; END_VAR','fb(value := a);',
        {'FB_TEST':{'declaration':'FUNCTION_BLOCK FB_Test\nVAR_IN_OUT value:ARRAY[0..2] OF INT; END_VAR'}})
    assert not result['approved']
    assert any('element types' in f['message'] for f in result['findings'])


@pytest.mark.parametrize('value,ok',[('10',True),('11',False),('-1',False)])
def test_subrange_assignment(value,ok):
    assert run('PROGRAM MAIN\nVAR x:INT(0..10); END_VAR','x := '+value+';')['approved'] is ok


def test_alias_chain_and_subrange():
    objects={'T_A':{'declaration':'TYPE T_A:T_B; END_TYPE'},'T_B':{'declaration':'TYPE T_B:INT(0..10); END_TYPE'}}
    assert run('PROGRAM MAIN\nVAR x:T_A; END_VAR','x:=1;',objects)['approved']
    assert not run('PROGRAM MAIN\nVAR x:T_A; END_VAR','x:=11;',objects)['approved']


def test_alias_cycle_is_incomplete():
    result=run('PROGRAM MAIN\nVAR x:T_A; END_VAR','x:=1;',{'T_A':{'declaration':'TYPE T_A:T_A; END_TYPE'}})
    assert not result['approved']
    assert any('Cyclic' in f['message'] for f in result['findings'])


def test_region_is_not_semantic_pragma():
    assert run('PROGRAM MAIN\nVAR x:INT; END_VAR','{region "Example"}\nx:=1;\n{endregion}')['approved']
    assert not run('PROGRAM MAIN','{IF defined(X)}{END_IF}')['approved']


def test_open_array_signature_accepts_fixed_array():
    assert run('PROGRAM MAIN\nVAR fb:FB_Test; a:ARRAY[0..2] OF INT; END_VAR','fb(value := a);',
        {'FB_TEST':{'declaration':'FUNCTION_BLOCK FB_Test\nVAR_IN_OUT value:ARRAY[*] OF INT; END_VAR'}})['approved']


@pytest.mark.parametrize('name',['MIN','max','BOOL','ACTION'])
def test_st_keyword_is_not_variable_name(name):
    assert any(f['rule']=='syntax-keyword-identifier' for f in syntax_findings({'declaration':f'VAR {name}:INT; END_VAR'}))
