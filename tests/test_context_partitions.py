import copy
import json

from tc_agent.context_partitions import project
from tc_agent import agent_core as ac


def test_partitions_reduce_old_bodies_without_mutating_history_or_pairs():
    messages = [{'role': 'user', 'text': 'repair'}]
    for i in range(7):
        messages.extend([{'role': 'assistant', 'tool_calls': [{'id': str(i), 'name': 'plc_read', 'args': {'name': 'MAIN'}}]},
                         {'role': 'tool', 'id': str(i), 'name': 'plc_read',
                          'result': {'status': 'read', 'source_hashes': {'decl': str(i)}, 'code': 'x' * 12000}}])
    original = copy.deepcopy(messages)
    compact = project(messages)
    assert messages == original
    assert len(json.dumps(compact)) < len(json.dumps(messages)) / 2
    assert compact[-1] == messages[-1]
    assert [m.get('id') for m in compact] == [m.get('id') for m in messages]
    assert compact[2]['result']['source_hashes'] == {'decl': '0'}


def test_approval_and_baseline_are_not_compacted():
    m = {'role': 'tool', 'result': {'build_plan_token': 'opaque', 'body': 'x' * 20000}}
    assert project([m], hot_results=0) == [m]


def test_larger_diagnostic_and_ordinary_results_remain_available():
    r = {'status': 'read', 'text': 'x' * 10000}
    assert ac.tool_result_for_context('tc_project_info', {}, r) == r
    diagnostic = {'status': 'read', 'findings': [{'message': 'x' * 500}] * 40}
    assert ac.tool_result_for_context('plc_diagnostics', {}, diagnostic) == diagnostic
