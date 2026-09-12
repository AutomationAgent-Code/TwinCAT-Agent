import json
from unittest.mock import patch
from tc_agent.execution_policy import FailurePolicy, tool_succeeded
from tc_agent import agent_core as ac
from tc_template.hmi_mapping_catalog import read_mappings


def test_empty_error_is_not_failure():
    assert tool_succeeded({'status': 'read', 'verified': True, 'error': '', 'state': 'Run'})
    assert not tool_succeeded({'verified': False, 'error': ''})
    assert not tool_succeeded({'error': 'COM failed'})


def test_deterministic_retry_and_project_absence_are_scoped():
    p = FailurePolicy()
    args = {'project': 'A'}
    p.record('tc_hmi_project_info', args, {'error': 'No TwinCAT HMI .hmiproj entry'}, False, True)
    assert p.check('tc_hmi_structure', args)
    assert not p.check('tc_hmi_structure', {'project': 'B'})
    p.record('plc_read', {'path': 'bad'}, {'error': '[invalid_object_path] bad'}, False, True)
    assert p.check('plc_read', {'path': 'bad'})
    assert not p.check('plc_read', {'path': 'TIPC^PLC1'})
    p.record('tc_hmi_create_project', args, {'status': 'created'}, True, False)
    assert not p.check('tc_hmi_structure', args)


def test_transient_errors_can_retry_but_uncertain_write_cannot():
    p = FailurePolicy()
    p.record('plc_read', {}, {'error': 'busy'}, False, True)
    assert not p.check('plc_read', {})
    p.record('plc_patch', {}, {'written': True, 'verified': False}, False, False)
    assert p.check('plc_patch', {})


def test_append_patch_uses_complete_expected_readback():
    expected = 'old\nnew'
    with patch.object(ac, '_review_pou_write', return_value={'approved': True, '_expected_area': expected}), \
         patch.object(ac, 'ps_com', side_effect=[{'status': 'patched'}, {'declaration': expected, 'path': 'TIPC^P'}]):
        r = ac._guarded_plc_patch({'name': 'GVL', 'area': 'declaration', 'old_text': 'old', 'new_text': expected})
    assert r['verified'] and r['readback']['criterion'] == 'exact_area_match'
    assert '_expected_area' not in r['review']


def test_unrelated_content_change_fails_patch_readback():
    with patch.object(ac, 'ps_com', return_value={'implementation': 'old\nnew\nunexpected'}):
        assert not ac._live_write_readback({'name': 'MAIN'}, expected='old\nnew')['verified']


def test_dynamic_mapping_catalog_does_not_use_ads(tmp_path):
    config = tmp_path / 'Server/TcHmiSrv/TcHmiSrv.Config.default.json'
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({'SYMBOLS': {'Alias': {'DOMAIN': 'ADS', 'DYNAMIC': True,
        'MAPPING': 'PLC2::GVL::bStart', 'SCHEMA': {'type': 'boolean'}}}}))
    with patch('tc_template._ps_bridge.com_hmi_project_info', return_value={'project_file': str(tmp_path/'A.hmiproj')}), \
         patch('tc_template._ps_bridge.ps_com') as com:
        r = read_mappings(mapping_kind='dynamic', runtime='PLC2')
    com.assert_not_called()
    assert r['dynamic_symbols'][0]['page_expression'] == '%s%Alias%/s%'
    assert r['dynamic_symbol_count'] == 1 and not r['online_verified']
