from tc_template.st_preflight import declarations, review_candidate


def test_fb_owned_constants_resolve_before_call_comparison():
    fb = {'name': 'FB_Client', 'declaration': '''FUNCTION_BLOCK FB_Client
VAR_IN_OUT a:ARRAY[0..SIZE - 1] OF BYTE; END_VAR
VAR CONSTANT SIZE:UDINT:=1024; END_VAR''', 'implementation': ''}
    for size, approved in [(1023, True), (100, False)]:
        result = review_candidate({'name':'MAIN', 'declaration':f'PROGRAM MAIN\nVAR fb:FB_Client; a:ARRAY[0..{size}] OF BYTE; END_VAR',
            'implementation':'fb(a:=a);'}, lambda name: fb if name.upper()=='FB_CLIENT' else None)
        assert result['approved'] is approved, result
        if not approved:
            assert any(f['message']=='Array bounds are incompatible.' for f in result['findings'])


def test_forward_constants_arithmetic_and_multiple_dimensions():
    values, _ = declarations('PROGRAM MAIN\nVAR a:ARRAY[-1..N-1,0..M*2-1] OF BYTE; END_VAR\nVAR CONSTANT N:INT:=M+2; M:INT:=4; END_VAR')
    assert values['A']['type']=='ARRAY[-1..5,0..7] OF BYTE'


def test_mutable_cyclic_unknown_constants_are_not_guessed():
    for decl in ['VAR N:INT:=4; END_VAR', 'VAR CONSTANT N:INT:=M; M:INT:=N; END_VAR', 'VAR CONSTANT N:INT:=Unknown; END_VAR']:
        values, _ = declarations('PROGRAM MAIN\nVAR a:ARRAY[0..N-1] OF BYTE; END_VAR\n'+decl)
        assert 'N-1' in values['A']['type']
