import pytest
from tc_template.library_signatures import parse_signatures
from tc_template.st_preflight import review_candidate


def signature(library='Tc2_TcpIp', pointer_type='POINTER TO BYTE'):
    return parse_signatures('<Library><LibraryName>'+library+'</LibraryName><Version>3.4.4.0</Version><TypeSignatures>'
        '<TypeSignature type="FunctionBlock"><Name>FB_SocketSend</Name><Inputs>'
        '<Input><Name>pSrc</Name><DataType>'+pointer_type+'</DataType></Input>'
        '<Input><Name>cbLen</Name><DataType>UDINT</DataType></Input></Inputs></TypeSignature></TypeSignatures></Library>')['objects'][0]


@pytest.mark.parametrize('count,approved', [(4, True), (5, False), (-1, False)])
def test_exact_byte_buffer_capacity(count, approved):
    obj=signature()
    r=review_candidate({'declaration':'PROGRAM MAIN\nVAR\nf:FB_SocketSend;\na:ARRAY[2..5] OF BYTE;\nEND_VAR',
                        'implementation':f'f(pSrc := ADR(a), cbLen := {count});'}, lambda n:obj)
    assert r['approved'] is approved
    assert r['pointer_safety_verified'] is False


def test_knowledge_requires_matching_library_and_signature():
    assert 'interface_knowledge' in signature()
    assert 'interface_knowledge' not in signature('Custom')
    assert 'interface_knowledge' not in signature(pointer_type='DWORD')


def test_unknown_dynamic_length_is_not_falsely_proven_safe():
    obj=signature()
    r=review_candidate({'declaration':'PROGRAM MAIN\nVAR\nf:FB_SocketSend;\na:ARRAY[0..3] OF BYTE;\nn:UDINT;\nEND_VAR',
                        'implementation':'f(pSrc := ADR(a), cbLen := n);'}, lambda n:obj)
    assert r['approved']
    assert r['pointer_safety_verified'] is False


@pytest.mark.parametrize('text,approved', [('123', True), ('1234', False)])
def test_literal_string_capacity(text,approved):
    r=review_candidate({'declaration':'PROGRAM MAIN\nVAR\ns:STRING(3);\nEND_VAR',
                        'implementation':"s := '"+text+"';"})
    assert r['approved'] is approved
