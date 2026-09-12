import pytest
from tc_template.plc_syntax import syntax_findings


@pytest.mark.parametrize('method,inputs', [('FB_init','bInitRetains:BOOL; bInCopyCode:BOOL; nExtra:INT;'),
    ('FB_exit','bInCopyCode:BOOL;'),('FB_reinit','')])
def test_official_interfaces(method,inputs):
    assert not syntax_findings({'declaration':f'METHOD {method}:BOOL\nVAR_INPUT {inputs} END_VAR'})


@pytest.mark.parametrize('decl', ['METHOD FB_init:INT\nVAR_INPUT bInitRetains:BOOL; bInCopyCode:BOOL; END_VAR',
    'METHOD FB_init:BOOL', 'METHOD FB_reinit', 'METHOD FB_exit:BOOL\nVAR_INPUT bInCopyCode:INT; END_VAR',
    'METHOD FB_reinit:BOOL\nVAR_OUTPUT result:BOOL; END_VAR'])
def test_bad_interface(decl):
    assert any(f['rule']=='syntax-lifecycle-interface' for f in syntax_findings({'declaration':decl}))


def test_only_super_init_is_forbidden():
    assert any(f['rule']=='syntax-lifecycle-super-init' for f in syntax_findings({
        'declaration':'METHOD FB_init:BOOL\nVAR_INPUT bInitRetains:BOOL; bInCopyCode:BOOL; END_VAR',
        'implementation':'SUPER^.FB_init();'}))
    assert not syntax_findings({'declaration':'METHOD FB_reinit:BOOL','implementation':'SUPER^.FB_reinit();'})
