import pytest
from tc_template.conditional_compilation import preprocess_local


@pytest.mark.parametrize('condition,selected',[
    ('defined(A) AND NOT (defined(B))', True),
    ('defined(B) OR defined(A) AND defined(B)', False),
    ('(defined(B) OR defined(A)) AND NOT defined(B)', True),
    ('NOT NOT defined(A)', True),
    ("hasvalue(A, 'AND (OR)') AND defined(A)", True),
])
def test_boolean_conditions_preserve_positions(condition,selected):
    source="{define A 'AND (OR)'}\n{undefine B}\n{IF "+condition+'}\nx:=1;\n{ELSE}\nx:=2;\n{END_IF}'
    result=preprocess_local(source)
    assert result['status']=='processed'
    assert ('x:=1;' in result['source']) is selected
    assert ('x:=2;' in result['source']) is not selected
    assert len(result['source'])==len(source)
    assert result['source'].count('\n')==source.count('\n')


@pytest.mark.parametrize('condition',[
    'defined(A) OR defined(External)', 'defined(A) OR',
    '(defined(A)', 'defined(A) garbage', 'defined(A) XOR defined(A)',
    'NOT '*40+'defined(A)', '(' *40+'defined(A)'+')'*40,
])
def test_unknown_or_malformed_keeps_original(condition):
    source='{define A}{IF '+condition+'}x:=1;{END_IF}'
    result=preprocess_local(source)
    assert result['status']=='unknown'
    assert result['source']==source


def test_boolean_preflight_checks_selected_branch():
    from tc_template.st_preflight import review_candidate
    source='{define A}{undefine B}{IF defined(A) AND NOT (defined(B))}x:=1;{ELSE}invalid code;{END_IF}'
    candidate={'declaration':'PROGRAM MAIN\nVAR x:INT; END_VAR','implementation':source}
    assert review_candidate(candidate)['approved']
    candidate['implementation']=source.replace('x:=1;', 'x:=;')
    assert not review_candidate(candidate)['approved']


def test_expression_length_budget():
    source='{define A}{IF '+('defined(A) OR '*1000)+'defined(A)}x:=1;{END_IF}'
    result=preprocess_local(source)
    assert result['status']=='unknown'
    assert 'budget' in result['reason']
