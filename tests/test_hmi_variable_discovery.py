from unittest.mock import patch
import pytest
from tc_template import hmi_variable_discovery as discovery


def context():
    return ({'project_file': 'Demo.hmiproj'},
            {'net_id': '1.2.3.4.1.1', 'port': 852, 'plc': 'PLC2'},
            {'gvl.start': [{'server_symbol': 'Actual.Start', 'page_expression': '%s%Actual.Start%/s%',
                            'schema': {}, 'access': 3, 'runtime': 'RuntimeB'}]})


def test_search_preserves_actual_alias_endpoint_and_no_value_read():
    with patch.object(discovery, '_context', return_value=context()), patch.object(
        discovery, 'browse_symbols', return_value={'source': 'ads-runtime', 'items': [
            {'path': 'GVL.Start', 'type': 'BOOL'}], 'next_offset': 40}) as browse:
        result = discovery.search_variables(query='Start', offset=20)
    assert result['items'][0]['binding_candidate']['mapping']['page_expression'] == '%s%Actual.Start%/s%'
    assert result['items'][0]['value_read'] is False
    assert browse.call_args.args == ('1.2.3.4.1.1', 852)
    assert result['next_offset'] == 40


def test_changed_endpoint_blocks_before_any_write():
    candidate = {'project': 'Demo.hmiproj', 'endpoint': {**context()[1], 'port': 851}}
    with patch.object(discovery, '_context', return_value=context()), patch.object(discovery.ps, 'ps_com') as com:
        with pytest.raises(ValueError, match='endpoint changed'):
            discovery.bind_candidate(candidate, 'Desktop.view', 'Start', 'data-tchmi-state-symbol', True)
    com.assert_not_called()


def test_member_uses_registered_root_and_hmi_member_separator():
    mappings = {'gvl.status': [{'server_symbol': 'Actual.Status'}]}
    option = discovery._mapping_options('GVL.Status.bReady', mappings)[0]
    assert option['page_expression'] == '%s%Actual.Status::bReady%/s%'
    assert discovery._mapping_options('GVL.StatusOther.bReady', mappings) == []


def test_type_change_blocks_binding():
    info, endpoint, mappings = context()
    candidate = {'project': info['project_file'], 'endpoint': endpoint, 'symbol': 'GVL.Start',
                 'type': 'BOOL', 'mapping': mappings['gvl.start'][0]}
    with patch.object(discovery, '_context', return_value=context()), patch.object(
        discovery, 'describe_symbol', return_value={'symbol': {'type': 'DINT'}}), patch.object(discovery.ps, 'ps_com') as com:
        with pytest.raises(ValueError, match='type changed'):
            discovery.bind_candidate(candidate, 'Desktop.view', 'Start', 'data-tchmi-state-symbol', True)
    com.assert_not_called()
