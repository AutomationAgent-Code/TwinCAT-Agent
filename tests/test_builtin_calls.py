import pytest

from tc_template.st_preflight import review_candidate
from tc_template.library_signatures import parse_signatures
from tc_template.builtin_calls import UNARY_MATH


def check(code, resolve=None):
    return review_candidate({'name': 'FB_Test', 'declaration': '''FUNCTION_BLOCK FB_Test
VAR n:UDINT; d:DINT; r:LREAL; t:TIME; b:BOOL; w:WORD;
buffer:ARRAY[0..15] OF BYTE; other:ARRAY[0..15] OF BYTE; END_VAR''',
                             'implementation': code}, resolve)


@pytest.mark.parametrize('code', [
    't := TIME();', 'n := SIZEOF(buffer);', 'n := SIZEOF(TIME);',
    'n := SIZEOF(buffer[0]);', 'd := ABS(-2);', 'r := EXPT(7,2);',
    'd := TRUNC(1.9);', 'd := TRUNC_INT(1.9);', 'd := MIN(2,3,4);',
    'd := MAX(2,3);', 'd := LIMIT(0,3,5);', 'd := SEL(TRUE,2,3);',
    'd := MUX(1,2,3);', 'w := SHL(w,1);', 'w := SHR(w,1);',
    'w := ROL(w,1);', 'w := ROR(w,1);'] + ['r := ' + name + '(0.5);' for name in UNARY_MATH])
def test_documented_calls(code):
    result = check(code)
    assert result['approved'], result
    assert result['compiler_verified'] is False


@pytest.mark.parametrize('code', ['t := TIME(1);', 'n := SIZEOF();', 'n := SIZEOF(1+2);',
    'n := SIZEOF(missing);', 'd := ABS(TRUE);', 'r := SQRT(TRUE);',
    'd := SEL(1,2,3);', 'd := MUX(TRUE,2,3);', 'w := SHL(w,TRUE);',
    'w := ROL(d,1);', 'd := TRUNC(TRUE);', 'n := XSIZEOF(buffer);'])
def test_invalid_or_unverified_calls_not_approved(code):
    assert not check(code)['approved']


def signature(name='MEMSET', library='Tc2_System'):
    return parse_signatures(f'<Library><LibraryName>{library}</LibraryName><Version>1.0</Version>'
        f'<TypeSignatures><TypeSignature type="Function"><Name>{name}</Name></TypeSignature>'
        '</TypeSignatures></Library>')['objects'][0]


@pytest.mark.parametrize('code', ['n := MEMSET(ADR(buffer),0,SIZEOF(buffer));',
    'n := MEMSET(destAddr := ADR(buffer), fillByte := 0, n := 16);',
    'n := MEMCPY(ADR(buffer),ADR(other),16);',
    'n := MEMMOVE(ADR(buffer),ADR(other),16);',
    'd := MEMCMP(ADR(buffer),ADR(other),16);'])
def test_memory_public_interfaces(code):
    result = check(code, lambda name: signature(name))
    assert result['approved'], result
    assert result['pointer_safety_verified'] is False


@pytest.mark.parametrize('code', ['n := MEMSET(ADR(buffer),256,16);',
    'n := MEMSET(ADR(buffer),0,17);', 'n := MEMSET(0,0,16);',
    'n := MEMSET(ADR(buffer));', 'n := MEMSET(ADR(buffer),0,TRUE);'])
def test_bad_memory_arguments(code):
    assert not check(code, lambda name: signature(name))['approved']


def test_no_library_or_wrong_library_is_not_granted_signature():
    assert not check('n := MEMSET(ADR(buffer),0,16);')['approved']
    assert not signature(library='Other')['signature_complete']


def test_xsizeof_symbolic_native_type_and_version_evidence():
    r = review_candidate({'declaration': 'PROGRAM MAIN\nVAR n:__UXINT; b:BYTE; END_VAR',
                          'implementation': 'n := XSIZEOF(b);'})
    assert r['approved'], r
    assert any(x['feature'] == 'xsizeof' for x in r['target_version_requirements'])
    assert r['target_compatibility_verified'] is False


def test_real_dependency_pipeline_does_not_search_builtin_names():
    from tc_template.plc_write_context import review_dependencies
    calls = []
    def call(verb, **args):
        calls.append((verb, args))
        if verb == 'find-pou':
            return {'matches': [], 'total': 0}
        if verb == 'library-signatures':
            return {'status': 'read', 'libraries': [{'objects': [signature()]}]}
        raise AssertionError(verb)
    r = review_dependencies({'name': 'MAIN', 'declaration':
        'PROGRAM MAIN\nVAR t:TIME; n:UDINT; a:ARRAY[0..15] OF BYTE; END_VAR',
        'implementation': 't := TIME(); n := MEMSET(ADR(a),0,SIZEOF(a));'},
        'TIPC^P^Proj^POUs^MAIN', call, semantic=True)
    assert r['semantic_review']['approved'], r
    assert not any(args.get('query') in {'TIME', 'SIZEOF'} for _, args in calls)


def test_documented_fallback_never_overrides_conflicting_live_input():
    xml = '''<Library><LibraryName>Tc2_System</LibraryName><Version>1.0</Version>
    <TypeSignatures><TypeSignature type="Function"><Name>MEMSET</Name><Inputs>
    <Input><Name>destAddr</Name><DataType>UDINT</DataType></Input>
    </Inputs></TypeSignature></TypeSignatures></Library>'''
    assert not parse_signatures(xml)['objects'][0]['signature_complete']


@pytest.mark.parametrize('code', ['n := LIMIT(0,n,100);', 'n := SEL(TRUE,n,0);',
                                  'n := MAX(n,1);', 'n := MOVE(n);'])
def test_contextual_integer_constants(code):
    r = check(code)
    assert r['approved'], r


def test_identical_memcpy_ranges_are_not_approved():
    assert not check('n := MEMCPY(ADR(buffer),ADR(buffer),16);', lambda n: signature(n))['approved']


@pytest.mark.parametrize('index,count,approved', [(0,16,True),(4,12,True),(4,13,False)])
def test_memory_array_remaining_extent(index, count, approved):
    result = check(f'n := MEMSET(ADR(buffer[{index}]),0,{count});', lambda n: signature(n))
    assert result['approved'] is approved, result


def test_sizeof_length_checks_remaining_extent():
    result = check('n := MEMSET(ADR(buffer[1]),0,SIZEOF(other));', lambda n: signature(n))
    assert not result['approved'], result


def test_memory_destination_cannot_write_constant():
    result = review_candidate({'declaration': 'PROGRAM MAIN\nVAR n:UDINT; END_VAR\nVAR CONSTANT c:BYTE:=0; END_VAR',
        'implementation': 'n := MEMSET(ADR(c),0,1);'}, lambda n: signature(n))
    assert not result['approved'], result


def test_dynamic_array_index_does_not_invent_single_byte_capacity():
    result = check('n := MEMSET(ADR(buffer[d]),0,16);', lambda n: signature(n))
    assert result['approved'], result
    assert result['pointer_safety_verified'] is False


def test_sizeof_type_detects_small_destination():
    result = check('n := MEMSET(ADR(w),0,SIZEOF(LREAL));', lambda n: signature(n))
    assert not result['approved'], result


def test_memcmp_may_read_constants():
    result = review_candidate({'declaration': 'PROGRAM MAIN\nVAR d:DINT; END_VAR\nVAR CONSTANT c:BYTE:=0; END_VAR',
        'implementation': 'd := MEMCMP(ADR(c),ADR(c),1);'}, lambda n: signature(n))
    assert result['approved'], result
    assert result['pointer_safety_verified'] is False
