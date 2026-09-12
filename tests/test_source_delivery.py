import json
import pytest
from tc_agent.agent_core import tool_result_for_context, compact_messages
from tc_agent.context_partitions import project
from tc_agent.backend import _tool_result_for_ui


@pytest.mark.parametrize('name', ['plc_read','plc_read_smart','plc_read_fast','plc_read_current'])
def test_full_source_survives_every_projection(name):
    text='    nCounter := nCounter + 1; // 正文\r\n'*10000
    raw={'name':'FB_Test','declaration':text,'implementation':text,
         'declaration_paging':{'truncated':False}}
    value=tool_result_for_context(name,{},raw,limit=500)
    messages=[{'role':'tool','name':name,'result':value}]
    messages.extend({'role':'tool','name':'other','result':{'status':'ok'}} for _ in range(4))
    compacted,_=compact_messages(messages,limit=500)
    projected=project(compacted,hot_results=2)
    assert projected[0]['result']['declaration']==text
    assert projected[0]['result']['implementation']==text
    assert json.loads(_tool_result_for_ui(raw))['declaration']==text
    assert '_pagination_required' not in value


def test_explicit_partial_page_is_not_claimed_complete():
    raw={'declaration':'VAR','declaration_paging':{'truncated':True,'next_start_line':2}}
    result=tool_result_for_context('plc_read',{},raw)
    assert result['declaration_paging']==raw['declaration_paging']


def test_single_live_read_defaults_to_full_source():
    from unittest.mock import patch
    from tc_agent import agent_core
    with patch.object(agent_core,'ps_com',return_value={'declaration':'VAR'}) as call:
        agent_core._plc_com_read({'name':'MAIN'})
    assert call.call_args.kwargs['max_lines']==0


def test_batch_source_survives_history_projection():
    raw={'results':[{'declaration':'x:INT;\n'*10000}]}
    messages=[{'role':'tool','result':raw}]
    assert project(messages,hot_results=0)[0]['result']==raw
    assert json.loads(_tool_result_for_ui(raw))==raw
