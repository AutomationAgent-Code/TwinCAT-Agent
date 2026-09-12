import pytest
from tc_template.interface_types import string_spec, interface_type
from tc_template.st_preflight import review_candidate


@pytest.mark.parametrize('spec,expected',[('STRING',('STRING',80)),('WSTRING',('WSTRING',80)),
    ('STRING[12]',('STRING',12)),('STRING(12)',('STRING',12)),('STRING(size)',None)])
def test_string_capacity(spec,expected):
    assert string_spec(spec)==expected


def test_square_interface_is_preserved():
    assert interface_type('STRING[12]')=='STRING(12)'


@pytest.mark.parametrize('spec,quote',[('STRING',"'"),('WSTRING','"'),('STRING[80]',"'")])
def test_default_and_square_capacity_checked(spec,quote):
    result=review_candidate({'declaration':f'PROGRAM MAIN\nVAR text:{spec}; END_VAR',
                             'implementation':'text := '+quote+'a'*81+quote+';'})
    assert any(f['rule']=='semantic-range' for f in result['findings'])


def test_default_reference_capacity():
    result=review_candidate({'declaration':'PROGRAM MAIN\nVAR fb:FB_Test; text:STRING[10]; END_VAR',
                             'implementation':'fb(value := text);'},
                            lambda n:{'declaration':'FUNCTION_BLOCK FB_Test\nVAR_IN_OUT value:STRING; END_VAR'})
    assert any('too short' in f['message'] for f in result['findings'])
