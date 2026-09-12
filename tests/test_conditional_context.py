from tc_template.st_preflight import review_candidate
from tc_template.conditional_compilation import preprocess_local


def test_local_defines_select_without_global_guess():
    source="{define variant 'one'}\n{IF hasvalue(variant,'one')}\nx:=1;\n{ELSE}\nthis is invalid;\n{END_IF}"
    result=review_candidate({'declaration':'PROGRAM MAIN\nVAR x:INT; END_VAR','implementation':source})
    assert result['approved'],result
    processed=preprocess_local(source)['source']
    assert len(processed)==len(source)
    assert processed.count('\n')==source.count('\n')


def test_unknown_global_does_not_select_else():
    assert preprocess_local('{IF defined(External)}bad;{ELSE}good;{END_IF}')['status']=='unknown'


def test_undefine_and_nested_inactive_branch():
    source='{undefine A}{IF defined(A)}{IF defined(Unknown)}bad;{END_IF}{ELSE}x:=1;{END_IF}'
    assert 'x:=1;' in preprocess_local(source)['source']
    assert 'bad;' not in preprocess_local(source)['source']


def test_literal_pragmas_not_executed():
    source="text := '{define A}'; {IF defined(A)}bad;{END_IF}"
    assert preprocess_local(source)['status']=='unknown'
