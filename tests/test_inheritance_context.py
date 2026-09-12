from tc_template.st_preflight import review_candidate


def test_inherited_field_and_local_shadow():
    base={'declaration':'FUNCTION_BLOCK FB_Base\nVAR_INPUT value:INT; END_VAR'}
    result=review_candidate({'declaration':'FUNCTION_BLOCK FB_Child EXTENDS FB_Base\nVAR result:INT; END_VAR',
                             'implementation':'result := value;'},lambda n:base)
    assert result['approved'],result
    result=review_candidate({'declaration':'FUNCTION_BLOCK FB_Child EXTENDS FB_Base\nVAR value:BOOL; END_VAR',
                             'implementation':'value := TRUE;'},lambda n:base)
    assert result['approved'],result


def test_final_and_cycles_are_not_approved():
    child={'declaration':'FUNCTION_BLOCK FB_Child EXTENDS FB_Base','implementation':''}
    assert not review_candidate(child,lambda n:{'declaration':'FUNCTION_BLOCK FINAL FB_Base'})['approved']
    result=review_candidate(child,lambda n:{'declaration':'FUNCTION_BLOCK FB_Base EXTENDS FB_Base'})
    assert not result['approved']
    assert any('Cyclic' in f['message'] for f in result['findings'])
