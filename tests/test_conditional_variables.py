import pytest
from tc_template.st_preflight import review_candidate
from tc_template.conditional_compilation import preprocess_editor


@pytest.mark.parametrize('query,selected',[
    ('defined(variable: x)',True),('hastype(variable: x, INT)',True),
    ('hastype(variable: x, BOOL)',False),
    ('defined(variable: x) AND NOT hastype(variable: x, BOOL)',True)])
def test_local_variable_queries(query,selected):
    source='{IF '+query+'}x:=1;{ELSE}x:=2;{END_IF}'
    result=review_candidate({'declaration':'PROGRAM MAIN\nVAR x:INT;END_VAR','implementation':source})
    assert result['approved'],result
    from tc_template.conditional_compilation import preprocess_candidate
    prepared,_=preprocess_candidate({'declaration':'PROGRAM MAIN\nVAR x:INT;END_VAR','implementation':source})
    assert ('x:=1;' in prepared['implementation']) is selected


def test_unknown_variable_not_absent():
    result=review_candidate({'declaration':'PROGRAM MAIN',
        'implementation':'{IF NOT defined(variable: GlobalOrInherited)}invalid;{END_IF}'})
    assert result['preprocessing']['implementation']['status']=='unknown'


def test_method_local_shadows_parent_type():
    result=review_candidate({'enclosing_declaration':'FUNCTION_BLOCK FB_Test\nVAR x:BOOL;END_VAR',
        'declaration':'METHOD Run\nVAR x:INT;END_VAR',
        'implementation':'{IF hastype(variable:x,INT)}x:=1;{ELSE}invalid;{END_IF}'})
    assert result['approved'],result


def test_primitive_alias_needs_resolution():
    result=review_candidate({'declaration':'PROGRAM MAIN\nVAR x:T_Alias;END_VAR',
        'implementation':'{IF hastype(variable:x,INT)}x:=1;{END_IF}'})
    assert result['preprocessing']['implementation']['status']=='unknown'


def test_variable_queries_forbidden_in_declarations():
    result=preprocess_editor('PROGRAM MAIN\nVAR {IF defined(variable:x)}x:INT;{END_IF}END_VAR','declaration',variable_lookup=lambda n:'INT')
    assert result['status']=='unknown'


def test_unknown_parent_scope_does_not_supply_positive_evidence():
    result=review_candidate({'enclosing_declaration':'FUNCTION_BLOCK FB_Test\nVAR {IF defined(External)}x:INT;{END_IF} END_VAR',
        'declaration':'METHOD Run','implementation':'{IF defined(variable:x)}x:=1;{END_IF}'})
    assert result['preprocessing']['implementation']['status']=='unknown'
