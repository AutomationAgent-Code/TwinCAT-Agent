import pytest
from tc_template.st_preflight import review_candidate
from tc_template.compiler_context import layout_type


@pytest.mark.parametrize('pack,size,offsets',[
    (0,7,[0,1,3,5,6]),(1,7,[0,1,3,5,6]),
    (2,8,[0,2,4,6,7]),(4,8,[0,2,4,6,7]),(8,8,[0,2,4,6,7])])
def test_official_pack_example_from_source(pack,size,offsets):
    result=review_candidate({'declaration':"{attribute 'pack_mode' := '"+str(pack)+"'}\nTYPE ST_Test:STRUCT a:BOOL;b:INT;c:INT;d:BOOL;e:BOOL;END_STRUCT END_TYPE",'implementation':''})
    assert result['memory_layout']['size']==size
    assert [f['offset'] for f in result['memory_layout']['fields']]==offsets


def test_nested_struct_keeps_own_pack_and_array_stride():
    declarations={
        'ST_INNER':"{attribute 'pack_mode' := '1'} TYPE ST_Inner:STRUCT a:BYTE;b:DINT;END_STRUCT END_TYPE",
        'ST_OUTER':'TYPE ST_Outer:STRUCT prefix:BYTE;payload:ARRAY[0..1] OF ST_Inner;suffix:INT;END_STRUCT END_TYPE'}
    result=review_candidate({'declaration':declarations['ST_OUTER'],'implementation':''},
                            resolve=lambda name:{'declaration':declarations[name.upper()]})
    layout=result['memory_layout']
    assert layout['size']==14
    assert [f['offset'] for f in layout['fields']]==[0,1,12]


def test_union_members_share_zero_offset():
    result=review_candidate({'declaration':'TYPE U_Test:UNION a:LWORD; b:ARRAY[0..7] OF BYTE; END_UNION END_TYPE','implementation':''})
    assert result['memory_layout']['size']==8
    assert all(f['offset']==0 for f in result['memory_layout']['fields'])


@pytest.mark.parametrize('prefix',[
    "{attribute 'pack_mode' := '3'}", "{attribute 'unknown'}",
    "{attribute 'pack_mode' := '1'}{attribute 'pack_mode' := '2'}"])
def test_unknown_layout_attributes_never_default_to_eight(prefix):
    result=review_candidate({'declaration':prefix+'TYPE ST_Test:STRUCT a:BYTE;b:DINT;END_STRUCT END_TYPE','implementation':''})
    assert result['memory_layout']['status']=='unknown'


def test_qualified_alias_layout():
    result=review_candidate({'declaration':'TYPE T_Test:Lib.T_Value;END_TYPE','implementation':''},
        resolve=lambda name:{'declaration':'TYPE T_Value:INT(0..100);END_TYPE'} if name.upper()=='LIB.T_VALUE' else None)
    assert result['memory_layout']['size']==2


def test_invalid_subrange_layout_unknown():
    assert layout_type('INT(100..0)',lambda name:None)['status']=='unknown'
    assert layout_type('INT(0..40000)',lambda name:None)['status']=='unknown'
