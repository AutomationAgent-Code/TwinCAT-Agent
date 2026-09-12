import pytest
from tc_template.library_signatures import parse_signatures
from tc_template.st_preflight import review_candidate


def library(return_type='UDINT', extra=''):
    return '<Library><LibraryName>Test</LibraryName><Version>1</Version><TypeSignatures><TypeSignature type="Function"><Name>F_Count</Name><Inputs><Input><Name>bEnable</Name><DataType>BOOL</DataType></Input></Inputs><Outputs><Output><Name>F_Count</Name><DataType>'+return_type+'</DataType></Output>'+extra+'</Outputs></TypeSignature></TypeSignatures></Library>'


@pytest.mark.parametrize('code,expected', [
    ('n := F_Count(TRUE);', True), ('n := F_Count(bEnable := TRUE);', True),
    ('n := F_Count(bMissing := TRUE);', False),
    ("n := F_Count(bEnable := 'text');", False),
    ('n := F_Count(F_Count => n);', False),
    ('b := F_Count(TRUE);', False)])
def test_function_signature_call_and_return(code, expected):
    obj = parse_signatures(library())['objects'][0]
    assert obj['signature_complete']
    assert obj['return_type'] == 'UDINT'
    result = review_candidate({'declaration': 'PROGRAM MAIN\nVAR\nn:UDINT;\nb:BOOL;\nEND_VAR',
                               'implementation': code}, lambda name: obj)
    assert result['approved'] is expected


def test_unsupported_or_duplicate_return_not_guessed():
    for xml in [library('REFERENCE TO BYTE'), library(extra='<Output><Name>F_Count</Name><DataType>BOOL</DataType></Output>'),
                library().replace('<Name>F_Count</Name><DataType>UDINT', '<Name>other</Name><DataType>UDINT')]:
        assert not parse_signatures(xml)['objects'][0]['signature_complete']


def test_conversion_shaped_library_function_uses_real_signature():
    from unittest.mock import Mock
    obj = parse_signatures(library('STRING').replace('F_Count', 'HSOCKET_TO_STRING'))['objects'][0]
    resolver = Mock(return_value=obj)
    result = review_candidate({'declaration':'PROGRAM MAIN\nVAR\ns:STRING;\nEND_VAR',
                              'implementation':'s := HSOCKET_TO_STRING(bEnable := TRUE);'}, resolver)
    resolver.assert_called_once_with('HSOCKET_TO_STRING')
    assert result['approved']
