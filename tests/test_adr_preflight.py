import pytest
from tc_template.st_preflight import review_candidate


def check(code, extra=''):
    return review_candidate({'declaration': 'FUNCTION_BLOCK FB_Test\nVAR n:INT; b:BYTE; p:POINTER TO INT; addr:PVOID; x:__XWORD; wide:LWORD; narrow:DWORD; '+extra+' END_VAR',
                             'implementation': code})


@pytest.mark.parametrize('code', ['p := ADR(n);', 'addr := ADR(n);', 'x := ADR(n);', 'wide := ADR(n);', 'p := 0;'])
def test_address_assignment_subset(code):
    r = check(code)
    assert r['approved'], r
    assert r['pointer_safety_verified'] is False
    assert r['compiler_verified'] is False


@pytest.mark.parametrize('code', ['p := ADR(b);', 'n := ADR(n);', 'addr := ADR(1);', 'addr := ADR(n+1);',
                                 'addr := ADR();', 'addr := ADR(n,b);', 'addr := ADR(value := n);',
                                 'addr := ADR(missing);', 'p := 1234;', 'narrow := ADR(n);', 'p^ := 2;'])
def test_unsafe_or_invalid_cases_still_blocked(code):
    assert not check(code)['approved']


def test_buffer_element_address():
    r = check('addr := ADR(buffer[0]);', 'buffer:ARRAY[0..9] OF BYTE;')
    assert r['approved'], r
    assert not check('addr := ADR(buffer[10]);', 'buffer:ARRAY[0..9] OF BYTE;')['approved']


def test_unknown_pointee_not_accepted():
    assert not check('', 'q:POINTER TO MissingType;')['approved']
