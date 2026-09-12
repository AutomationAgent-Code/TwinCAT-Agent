from unittest.mock import patch
import pytest
from tc_agent.plc_path_contract import normalize, model_paths
from tc_agent import agent_core as ac

TREE = 'TIPC^PLC^PLC Project^POUs^MAIN'
SOURCE = 'PLC^POUs^MAIN.TcPOU'


@pytest.mark.parametrize('name', ['plc_read_smart', 'plc_read_fast'])
@pytest.mark.parametrize('field,path,live', [('tree_path', TREE, True), ('path', TREE, True), ('source_path', SOURCE, False), ('path', SOURCE, False)])
def test_route_by_identity(name, field, path, live):
    args = {'name': 'MAIN', field: path}
    if name == 'plc_read_fast': args = {'requests': [args]}
    assert normalize(name, args)['live'] is live


@pytest.mark.parametrize('args', [
    {'tree_path': SOURCE}, {'source_path': TREE}, {'tree_path': TREE, 'source_path': SOURCE},
    {'tree_path': TREE, 'live': False}, {'source_path': SOURCE, 'live': True},
    {'path': 'C:\\project\\MAIN.TcPOU'}, {'tree_path': TREE, 'path': TREE + '2'},
])
def test_invalid_identity_fails_before_host(args):
    with patch.object(ac, 'ps_com', side_effect=AssertionError('no COM')):
        r = ac.run_tool('plc_read_smart', {'name': 'MAIN', **args})
        assert r['status'] == 'invalid_arguments'
        assert r['not_executed'] is True


def test_write_alias_and_saved_rejection():
    assert normalize('plc_patch', {'tree_path': TREE})['path'] == TREE
    with pytest.raises(ValueError): normalize('plc_patch', {'path': SOURCE})
    assert ac.validate_tool_arguments('plc_save_document', {'name': 'MAIN', 'tree_path': TREE, 'expected_document_baseline': {}}) is None


def test_model_does_not_receive_ambiguous_path_or_modified_token():
    token = {'path': TREE, 'sha256': 'abc'}
    r = model_paths({'results': [{'path': SOURCE}, {'path': TREE}], 'member_baseline': token})
    assert r['results'] == [{'source_path': SOURCE}, {'tree_path': TREE}]
    assert r['member_baseline'] == token


def test_explicit_source_failure_never_falls_back():
    with patch.object(ac, '_cached_plc_read', return_value=None), patch.object(ac, '_saved_solution', return_value='x.sln'), \
         patch.object(ac, 'read_indexed_source', side_effect=FileNotFoundError('missing')), \
         patch.object(ac, 'ps_com', side_effect=AssertionError('no COM')):
        with pytest.raises(ValueError, match='不回退'):
            ac._plc_read_smart({'name': 'MAIN', 'source_path': SOURCE})


def test_batch_tree_uses_only_live_bridge():
    with patch.object(ac, 'ps_com', return_value={'results': []}) as com, \
         patch.object(ac, 'read_many_indexed_source', side_effect=AssertionError('no disk')):
        ac._plc_read_fast({'requests': [{'name': 'MAIN', 'tree_path': TREE}]})
        assert com.call_args.args[0] == 'read-batch'


def test_mixed_batch_rejected_before_read():
    with pytest.raises(ValueError, match='两个批次'):
        normalize('plc_read_fast', {'requests': [{'name': 'MAIN', 'tree_path': TREE}, {'name': 'MAIN', 'source_path': SOURCE}]})


def test_physical_file_is_not_an_index_identity():
    r = model_paths({'path': TREE, 'source_path': r'C:\PLC\MAIN.TcPOU'})
    assert r == {'tree_path': TREE, 'file': r'C:\PLC\MAIN.TcPOU'}


def test_candidate_tree_alias():
    args = {'candidates': [{'name': 'MAIN', 'tree_path': TREE, 'declaration': 'PROGRAM MAIN', 'implementation': ''}]}
    assert ac.validate_tool_arguments('plc_preflight', args) is None
    assert normalize('plc_preflight', args)['candidates'][0]['path'] == TREE
