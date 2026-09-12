from tc_template.st_preflight import review_candidate
from tc_template.compiler_context import version_requirement


def test_implementation_long_literal_has_requirement():
    result=review_candidate({'declaration':'PROGRAM MAIN','implementation':'x:=LDATE#2026-01-01;'})
    assert any(r['feature']=='long_date_time' and r['area']=='implementation' for r in result['target_version_requirements'])
    assert result['target_compatibility_verified'] is False


def test_candidate_cannot_self_attest_version():
    result=review_candidate({'declaration':'PROGRAM MAIN\nVAR x:LDATE;END_VAR','implementation':'',
                             'compiler_version':'3.1.9999.0','target_version':'3.1.9999.0'})
    assert result['target_version_requirements'][0]['actual'] is None


def test_dependency_requirement_is_included():
    result=review_candidate({'declaration':'PROGRAM MAIN\nVAR x:FB_Time;END_VAR','implementation':''},
        resolve=lambda name:{'declaration':'FUNCTION_BLOCK FB_Time\nVAR stamp:LDATE;END_VAR'})
    assert any(r.get('dependency')=='FB_TIME' for r in result['target_version_requirements'])


def test_decl_requirement_and_hidden_literal_ignored():
    result=review_candidate({'declaration':'PROGRAM MAIN\n{define A}\nVAR {IF defined(A)}x:INT;{ELSE}x:LDATE;{END_IF}END_VAR','implementation':''})
    assert {r['feature'] for r in result['target_version_requirements']}=={'declaration_conditionals'}


def test_large_version_is_unknown_not_exception():
    assert version_requirement('9'*5000+'.1.1.1')['status']=='unknown'
