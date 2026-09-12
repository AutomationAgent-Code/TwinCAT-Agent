import pytest
from tc_template.interface_types import interface_type, declared_return_type
from tc_template.library_signatures import parse_signatures
from tc_template.st_preflight import review_candidate


@pytest.mark.parametrize('raw,expected', [('STRING ( 80 )','STRING(80)'),
    ('pointer to byte','POINTER TO BYTE'), ('POINTER TO WSTRING(255)','POINTER TO WSTRING(255)'),
    ('STRING(size)',None), ('REFERENCE TO BYTE',None), ('ARRAY[0..3] OF BYTE',None),
    ('STRING(0)',None), ('BYTE; VAR hacked:INT',None)])
def test_type_grammar(raw,expected):
    assert interface_type(raw) == expected


def test_return_header_preserves_pointer():
    assert declared_return_type('FUNCTION F : POINTER TO BYTE\nVAR x:INT; END_VAR') == 'POINTER TO BYTE'


@pytest.mark.parametrize('return_type,destination,expected', [('POINTER TO BYTE','POINTER TO BYTE',True),
    ('POINTER TO BYTE','BOOL',False),('STRING(255)','STRING(255)',True)])
def test_live_return_metadata_preserved(return_type,destination,expected):
    xml='<Library><LibraryName>Demo</LibraryName><Version>1</Version><TypeSignatures><TypeSignature type="Function"><Name>F</Name><Outputs><Output><Name>F</Name><DataType>'+return_type+'</DataType></Output></Outputs></TypeSignature></TypeSignatures></Library>'
    obj=parse_signatures(xml)['objects'][0]
    assert obj['signature_complete']
    r=review_candidate({'declaration':'PROGRAM MAIN\nVAR\nx:'+destination+';\nEND_VAR','implementation':'x := F();'}, lambda n:obj)
    assert r['approved'] is expected
    if return_type.startswith('POINTER'):
        assert r['pointer_safety_verified'] is False


def test_function_return_assignment_uses_full_type():
    r=review_candidate({'declaration':'FUNCTION F : POINTER TO BYTE\nVAR\np:POINTER TO BYTE;\nEND_VAR',
                        'implementation':'F := p;'})
    assert r['approved']
