import pytest
from unittest.mock import Mock, patch
from tc_agent.editor_offline import check_source_offline
from tc_agent import build_execution as be


@pytest.mark.parametrize('logged,blocked', [(True,True),(False,False),(None,True)])
def test_edit_checks_login_not_runtime_run(logged,blocked):
    call=Mock(return_value={'logged_in':logged,'operation_state':'Run'})
    result=check_source_offline({'tree_path':'TIPC^PLC1^Project^POUs^MAIN'},call)
    assert (result is not None)==blocked
    call.assert_called_once_with('plc-online-state',runtime='PLC1')


def test_unknown_project_does_not_allow_edit():
    assert check_source_offline({},Mock(return_value={'plcs':[]})) is not None


@pytest.mark.parametrize('action',['build','rebuild'])
def test_online_blocks_build_even_if_command_available(action):
    from test_build_execution import _read_only_snapshot
    responses=_read_only_snapshot()
    responses['plc-runtimes']={'plcs':[{'name':'PLC1','ads_port':851}]}
    responses['plc-online-state']={'logged_in':True}
    with patch.object(be,'ps_com',side_effect=lambda cmd,**kw:responses[cmd]) as call:
        result=be.capture_build_state({'action':action})
    assert result['status']=='blocked'
    assert 'plans' not in result
    assert result['online_runtimes']==['PLC1']
    assert not any(c.args[0] in {'logout','stop','build','rebuild'} for c in call.call_args_list)
