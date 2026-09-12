import unittest
from unittest.mock import patch
from tc_template.build_platform_contract import preflight, assess
from tc_template import _ps_bridge as bridge


class BuildPlatformTests(unittest.TestCase):
    def call(self, verb):
        if verb == 'platform-show':
            return self.current
        return {'target_match_verified': True, 'target_platform': 'TwinCAT RT (x64)'}

    def test_plc_rejects_os_when_target_reports_rt(self):
        self.current = {'config': 'Release', 'platform': 'TwinCAT OS (x64)', 'contexts': []}
        r = preflight('plc', self.call)
        self.assertFalse(r['build_performed'])
        self.assertEqual('Release|TwinCAT RT (x64)', r['required_full'])

    def test_hmi_does_not_query_plc(self):
        self.current = {'config': 'Release', 'platform': 'TwinCAT HMI',
                        'contexts': [{'project': 'HMI.hmiproj', 'platform': 'TwinCAT HMI'}]}
        calls = []
        def call(verb):
            calls.append(verb)
            return self.current
        self.assertIsNone(preflight('hmi', call))
        self.assertEqual(['platform-show'], calls)

    def test_hmi_project_mapping_passes_under_active_rt_platform(self):
        self.current = {'config': 'Release', 'platform': 'TwinCAT RT (x64)',
                        'contexts': [{'project': 'System.tsproj', 'platform': 'TwinCAT RT (x64)', 'should_build': True},
                                     {'project': 'HMI.hmiproj', 'platform': 'TwinCAT HMI', 'should_build': False}]}
        self.assertIsNone(preflight('hmi', self.call))

    def test_plc_matching_mappings_pass(self):
        self.current = {'platform': 'TwinCAT RT (x64)',
                        'contexts': [{'project': 'PLC.plcproj', 'platform': 'TwinCAT RT (x64)'}]}
        self.assertIsNone(preflight('plc', self.call))

    def test_local_compile_allowed_when_target_probe_unavailable(self):
        self.current = {'config': 'Release', 'platform': 'TwinCAT RT (x64)',
                        'contexts': [{'project': 'PLC.plcproj', 'platform': 'TwinCAT RT (x64)',
                                      'should_build': True}]}
        def call(verb):
            if verb == 'platform-show':
                return self.current
            return {'target_netid': '1.2.3.4.1.1', 'target_match_verified': False,
                    'error': 'Target detail query timed out'}
        evidence = assess('plc', call)
        self.assertTrue(evidence['allowed'])
        self.assertTrue(evidence['local_build_context_verified'])
        self.assertFalse(evidence['target_compatibility_verified'])
        self.assertIsNone(evidence['target_compatible'])
        self.assertIsNone(preflight('plc', call))

    def test_unknown_target_does_not_override_invalid_local_context(self):
        self.current = {'config': 'Release', 'platform': 'TwinCAT RT (x64)',
                        'contexts': [{'project': 'PLC.plcproj', 'platform': 'TwinCAT RT (x86)',
                                      'should_build': True}]}
        def call(verb):
            return self.current if verb == 'platform-show' else {'target_match_verified': False}
        result = preflight('plc', call)
        self.assertEqual('build_platform_context_invalid', result['error_code'])
        self.assertIsNone(result['errorCount'])
        self.assertIsNone(result['failedProjects'])
        self.assertFalse(result['build_performed'])

    def test_preflight_prevents_actual_build(self):
        with patch.object(bridge, '_ps_com_raw', return_value={
                'platform': 'TwinCAT RT (x64)',
                'contexts': [{'project': 'HMI.hmiproj', 'platform': 'TwinCAT RT (x64)'}],
        }) as raw:
            r = bridge.ps_com('hmi-build')
        self.assertFalse(r['build_performed'])
        self.assertEqual(['platform-show'], [c.args[0] for c in raw.call_args_list])

    def test_target_recommendation_rejects_wrong_platform_even_with_ack(self):
        target = {'solution': 'a.sln', 'target_netid': '1.2.3.4.1.1', 'target_match_verified': True,
                  'target_platform': 'TwinCAT RT (x64)'}
        info = {'platforms': [{'full': 'Release|TwinCAT OS (x64)', 'config': 'Release', 'platform': 'TwinCAT OS (x64)'}]}
        with patch.object(bridge, 'ps_com', side_effect=[info, {'config': 'Release'}, target]):
            r = bridge.com_select_build_platform('Release|TwinCAT OS (x64)', True, True)
        self.assertEqual('target_platform_mismatch', r['error_code'])
