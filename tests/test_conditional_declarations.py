import pytest
from tc_template.st_preflight import review_candidate, declarations
from tc_template.plc_write_context import review_dependencies
from tc_template.conditional_compilation import preprocess_editor


def test_selected_declaration_and_initializer():
    declaration="PROGRAM MAIN\n{define A}\nVAR\n{IF defined(A)}\nx:INT:=1;\n{ELSE}\nx:MissingType:=missing;\n{END_IF}\nEND_VAR"
    result=review_candidate({'declaration':declaration,'implementation':'x:=2;'})
    assert result['approved'],result
    assert result['preprocessing']['declaration']['minimum_twincat_version']=='3.1.4024.0'
    bad=review_candidate({'declaration':declaration.replace('x:INT:=1;','x:INT:=TRUE;'),'implementation':''})
    assert not bad['approved']


def test_macros_never_leak_between_editors():
    result=review_candidate({'declaration':'PROGRAM MAIN\n{define A}\nVAR x:INT; END_VAR',
                             'implementation':'{IF defined(A)}x:=1;{END_IF}'})
    assert not result['approved']
    assert result['preprocessing']['implementation']['status']=='unknown'


def test_unknown_declarations_never_merge_branches():
    symbols,issues=declarations('PROGRAM MAIN\nVAR {IF defined(External)}x:INT;{ELSE}x:BOOL;{END_IF} END_VAR')
    assert not symbols and issues
    assert not any('Duplicate' in issue for issue in issues)


@pytest.mark.parametrize('body',[
    'TYPE E_Test:({IF defined(A)}a,{END_IF}b);END_TYPE',
    'TYPE T_Test:{IF defined(A)}INT{ELSE}BOOL{END_IF};END_TYPE'])
def test_forbidden_conditional_enum_and_alias_remain_unknown(body):
    result=preprocess_editor('{define A}'+body,'declaration')
    assert result['status']=='unknown'


def test_dependency_scan_ignores_inactive_branch():
    calls=[]
    def call(verb,**args):
        calls.append(verb)
        raise AssertionError('Inactive references must not trigger COM reads')
    result=review_dependencies({'declaration':'PROGRAM MAIN\nVAR x:INT; END_VAR',
        'implementation':'{define A}{IF defined(A)}x:=1;{ELSE}x:=GVL_Missing.bad;{END_IF}'},
        'TIPC^PLC^Project^POUs^MAIN',call,semantic=True)
    assert not result['findings'],result
    assert not calls


def test_unknown_dependency_declaration_not_reported_as_missing_member():
    def call(verb,**args):
        if verb=='find-pou':
            return {'total':1,'matches':[{'name':'GVL_Test','path':'TIPC^PLC^Project^GVLs^GVL_Test'}]}
        return {'declaration':'VAR_GLOBAL {IF defined(External)}x:INT;{END_IF} END_VAR'}
    result=review_dependencies({'declaration':'PROGRAM MAIN\nVAR y:INT; END_VAR',
        'implementation':'y:=GVL_Test.x;'},'TIPC^PLC^Project^POUs^MAIN',call,semantic=True)
    assert result['unresolved']
    assert not any(f['rule']=='dependency-member-missing' for f in result['findings'])
