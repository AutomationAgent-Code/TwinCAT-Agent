from tc_agent import agent_core as ac


def test_model_context_does_not_expose_saved_index_path_as_com_path():
    result = ac.tool_result_for_context('plc_read_smart', {'name': 'MAIN'}, {
        'status': 'read', 'name': 'MAIN',
        'path': 'Untitled1^POUs^MAIN.TcPOU',
        'source_path': 'Untitled1^POUs^MAIN.TcPOU',
        'path_kind': 'saved_source', 'source': 'disk_index',
        'tree_path': '', 'tree_path_available': False,
        'declaration': 'PROGRAM MAIN', 'implementation': 'n := 1;',
    })
    assert 'path' not in result
    assert result['source_path'].endswith('MAIN.TcPOU')
    assert result['path_kind'] == 'saved_source'
    assert result['tree_path_available'] is False
    assert 'plc_find' in result['next_action']


def test_saved_source_alias_is_read_only_and_write_schema_rejects_it():
    request = ac._plc_read_request({
        'name': 'MAIN', 'source_path': 'Untitled1^POUs^MAIN.TcPOU',
    })
    assert request['path'].endswith('MAIN.TcPOU')
    assert 'source_path' not in request
    invalid = ac.validate_tool_arguments('plc_patch', {
        'name': 'MAIN', 'area': 'implementation',
        'old_text': 'old', 'new_text': 'new',
        'source_path': 'Untitled1^POUs^MAIN.TcPOU',
    })
    assert invalid and invalid['error_type'] == 'tool_arguments'
