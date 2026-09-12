import pytest
from tc_template.compiler_context import layout_type,version_requirement


@pytest.mark.parametrize('fields,size',[
    ([('a','LREAL'),('b','DINT'),('c','SINT')],16),
    ([('c','SINT'),('b','DINT'),('a','LREAL')],16),
    ([('c','SINT'),('a','LREAL'),('b','DINT')],24)])
def test_official_alignment_examples(fields,size):
    result=layout_type('ST_Test',lambda n:{'kind':'struct','fields':fields})
    assert result['size']==size
    assert not result['layout_verified']


def test_pack_and_array_stride():
    result=layout_type('ARRAY[0..1] OF ST_Test',lambda n:{'kind':'struct','pack':1,'fields':[('a','BYTE'),('b','DINT')]})
    assert result['size']==10


def test_pointer_context_and_hidden_fb():
    assert layout_type('POINTER TO INT',lambda n:None)['status']=='unknown'
    assert layout_type('POINTER TO INT',lambda n:None,pointer_bits=64)['size']==8
    assert layout_type('FB_Test',lambda n:{'kind':'fb','fields':[]})['status']=='unknown'


@pytest.mark.parametrize('version,status',[(None,'unknown'),('3.1.4024.0','incompatible'),('3.1.4026.0','compatible')])
def test_version_comparison(version,status):
    assert version_requirement(version)['status']==status


def test_semantic_report_contains_layout_not_runtime_proof():
    from tc_template.st_preflight import review_candidate
    result=review_candidate({'declaration':'TYPE ST_Test:STRUCT first:SINT; second:LREAL; third:DINT; END_STRUCT END_TYPE','implementation':''})
    assert result['memory_layout']['size']==24
    assert not result['memory_layout']['layout_verified']


def test_long_date_does_not_invent_compiler_version():
    from tc_template.st_preflight import review_candidate
    result=review_candidate({'declaration':'PROGRAM MAIN\nVAR stamp:LDATE; END_VAR','implementation':''})
    assert result['target_version_requirement']['status']=='unknown'
