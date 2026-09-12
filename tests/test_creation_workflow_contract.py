from unittest.mock import patch
import pytest
from tc_template.creation_source import declaration_for_creation
from tc_agent import agent_core as ac


def test_function_review_and_creation_receive_same_complete_candidate():
    args={'name':'Demo_Add','type':'function','return_type':'DINT',
          'path':'TIPC^PLC^Proj^POUs','declaration':'VAR_INPUT\na:DINT; b:DINT;\nEND_VAR',
          'implementation':'Demo_Add := a + b;'}
    def com(command,**kw):
        if command=='new-pou':
            assert kw['declaration'].startswith('FUNCTION Demo_Add : DINT\n')
            return {'status':'created'}
        if command=='find-pou': return {'matches':[],'total':0}
        raise AssertionError(command)
    with patch.object(ac,'ps_com',side_effect=com) as calls:
        result=ac._guarded_plc_create(args)
    assert result['status']=='created',result
    assert not result['review']['blocking_findings']
    assert [c.args[0] for c in calls.call_args_list].count('new-pou')==1


def test_complete_signature_idempotent_and_mismatch_rejected():
    source=declaration_for_creation('function','F','VAR_INPUT\na:INT;\nEND_VAR','INT')
    assert declaration_for_creation('function','F',source,'INT')==source
    with pytest.raises(ValueError): declaration_for_creation('function','Other',source,'INT')


def test_missing_parent_path_resolves_once_then_captures_exact_baseline():
    path='TIPC^PLC^Proj^POUs^FB_Test'
    with patch.object(ac,'ps_com',side_effect=[{'matches':[{'name':'FB_Test','path':path}],'total':1},
                                              {'member_baseline':{'path':path}}]) as call:
        result=ac._plc_read({'name':'FB_Test','structure_baseline':True})
    assert result['member_baseline']['path']==path
    assert call.call_args.args[0]=='member-baseline'
    assert call.call_args.kwargs['path']==path


@pytest.mark.parametrize('found',[{'matches':[],'total':0},
    {'matches':[{'name':'FB_Test','path':'TIPC^P^Proj^FB_Test'}],'total':2},
    {'matches':[{'name':'FB_Test','path':'TIPC^P^Proj^FB_Test'}],'total':1,'truncated':True}])
def test_ambiguous_or_partial_lookup_does_not_capture_baseline(found):
    with patch.object(ac,'ps_com',return_value=found) as call:
        with pytest.raises(ValueError): ac._plc_read({'name':'FB_Test','structure_baseline':True})
    call.assert_called_once()


def test_missing_parent_folder_never_creates_an_object():
    from types import SimpleNamespace as NS
    from unittest.mock import Mock
    from tc_template import plc
    manager=Mock()
    manager.LookupTreeItem.side_effect=RuntimeError('Subitem not found')
    dte=NS(MainWindow=NS(Visible=False),Solution=NS(Projects=NS(Item=lambda n:NS(Object=manager))))
    with patch.object(plc,'_com_dte',return_value=dte), \
         patch.object(plc,'_find_nested_project',return_value=Mock()), \
         patch.object(plc,'_get_nested_base_path',return_value='TIPC^PLC^Proj'):
        with pytest.raises(FileNotFoundError,match='No object was created'):
            plc.create_pou('interface','I_Test',parent_path='TIPC^PLC^Proj^Interfaces')
    manager.CreateChild.assert_not_called()
