import unittest
from unittest.mock import patch

from tc_template import _ps_bridge as bridge
from tc_template.plc_read_contract import validate_object_request, diagnose_read_failure, PlcReadError


class PlcReadContractTests(unittest.TestCase):
    def setUp(self):
        self.inventory = {'plcs': [{'name': 'PLC1', 'project_name': 'PLC1 Project'}]}

    def test_project_files_rejected_without_com(self):
        for name in ('PLC1.plcproj', 'A.TSPROJ', 'Test.sln'):
            with self.subTest(name=name), patch.object(bridge, '_ps_com_raw') as raw:
                with self.assertRaisesRegex(PlcReadError, 'project_not_code_object'):
                    bridge.ps_com('read-pou', name=name)
                raw.assert_not_called()

    def test_project_name_has_project_specific_guidance(self):
        result = diagnose_read_failure('PLC1', '', RuntimeError('not found'), self.inventory)
        self.assertEqual('project_not_code_object', result.code)

    def test_missing_nested_is_not_reported_as_missing_object(self):
        result = diagnose_read_failure('MAIN', '', RuntimeError('not found'),
                                       {'plcs': [{'name': 'PLC1', 'project_name': ''}]})
        self.assertEqual('plc_project_unavailable', result.code)
        self.assertIn('不能确认 Disabled', str(result))

    def test_valid_project_missing_object(self):
        result = diagnose_read_failure('Missing', '', RuntimeError('not found'), self.inventory)
        self.assertEqual('code_object_not_found', result.code)

    def test_invalid_path(self):
        result = diagnose_read_failure('MAIN', 'PLC1', RuntimeError('Tree path is outside the current PLC project'), self.inventory)
        self.assertEqual('invalid_object_path', result.code)

    def test_selected_unloaded_project_not_masked_by_other_loaded_project(self):
        self.inventory['plcs'].append({'name': 'PLC2', 'project_name': ''})
        result = diagnose_read_failure('MAIN', 'TIPC^PLC2^PLC2 Project^POUs^MAIN', RuntimeError('not found'), self.inventory)
        self.assertEqual('plc_project_unavailable', result.code)

    def test_valid_read_does_not_add_com_calls(self):
        with patch.object(bridge, '_ps_com_raw', return_value={'declaration': 'PROGRAM MAIN'}) as raw:
            result = bridge.ps_com('read-pou', name='MAIN')
        self.assertIn('declaration', result)
        self.assertEqual(1, raw.call_count)

    def test_failure_diagnoses_without_retry_or_reload(self):
        with patch.object(bridge, '_ps_com_raw', side_effect=[RuntimeError('not found'), self.inventory]) as raw:
            with self.assertRaisesRegex(PlcReadError, 'project_not_code_object'):
                bridge.ps_com('read-pou', name='PLC1')
        self.assertEqual(['read-pou', 'plc-runtimes'], [c.args[0] for c in raw.call_args_list])

    def test_unavailable_diagnostic_preserves_original_exception(self):
        original = RuntimeError('COM disconnected')
        with patch.object(bridge, '_ps_com_raw', side_effect=[original, RuntimeError('unavailable')]):
            with self.assertRaises(RuntimeError) as caught:
                bridge.ps_com('read-pou', name='MAIN')
        self.assertIs(original, caught.exception)

    def test_agent_normalization_rejects_project_before_member_rewrite(self):
        from tc_agent.agent_core import _plc_read_request
        with self.assertRaisesRegex(PlcReadError, 'project_not_code_object'):
            _plc_read_request({'name': 'PLC1.plcproj', 'path': 'PLC1^PLC1.plcproj'})


if __name__ == '__main__':
    unittest.main()
