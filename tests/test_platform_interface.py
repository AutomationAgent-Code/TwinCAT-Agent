import unittest
from unittest.mock import patch
from tc_template import _ps_bridge as bridge
from tc_agent import agent_core


class PlatformInterfaceTests(unittest.TestCase):
    full = 'Release|TwinCAT OS (x64)'

    def setUp(self):
        self.current = {'full': self.full, 'config': 'Release', 'platform': 'TwinCAT OS (x64)',
                        'contexts': [{'project': 'PLC1.plcproj', 'platform': 'TwinCAT OS (x64)'}]}
        self.target = {'solution': 'Test.sln', 'target_netid': '1.2.3.4.1.1', 'cpu_info_xml': '86'}
        self.writes = []
        self.bad_readback = False

    def call(self, command, **args):
        if command == 'platform-list':
            return {'platforms': [
                {'full': self.full, 'config': 'Release', 'platform': 'TwinCAT OS (x64)'},
                {'full': 'Debug|TwinCAT OS (x64)', 'config': 'Debug', 'platform': 'TwinCAT OS (x64)'}]}
        if command == 'platform-show':
            return {} if self.bad_readback and self.writes else self.current
        if command == 'platform-target-info':
            return dict(self.target)
        self.writes.append(command)
        return {'status': 'ok'}

    def select(self, **args):
        with patch.object(bridge, 'ps_com', side_effect=self.call):
            return bridge.com_select_build_platform(**args)

    def test_preview_does_not_switch(self):
        self.assertEqual('preview', self.select(full=self.full)['status'])
        self.assertEqual([], self.writes)

    def test_unknown_target_requires_confirmation(self):
        self.assertEqual('target_platform_confirmation_required',
                         self.select(full=self.full, apply=True)['error_code'])
        self.assertEqual([], self.writes)

    def test_apply_separates_switch_from_target_verification(self):
        result = self.select(full=self.full, apply=True, acknowledge_target_platform=True)
        self.assertTrue(result['switch_verified'])
        self.assertFalse(result['target_match_verified'])
        self.assertEqual('switched', result['status'])

    def test_missing_readback_is_not_success(self):
        self.bad_readback = True
        result = self.select(full=self.full, apply=True, acknowledge_target_platform=True)
        self.assertFalse(result['switch_verified'])
        self.assertEqual('incomplete', result['status'])

    def test_unlisted_platform_is_rejected(self):
        with self.assertRaises(ValueError):
            self.select(full='guessed', apply=True)

    def test_keep_release_and_never_pick_first(self):
        result = self.select()
        self.assertEqual([self.full], result['candidates'])
        self.assertEqual('explicit_platform_required', result['error_code'])
        result = self.select(full='Debug|TwinCAT OS (x64)', apply=True, acknowledge_target_platform=True)
        self.assertEqual('configuration_change_requires_confirmation', result['error_code'])
        self.assertEqual([], self.writes)

    def test_changed_target_blocks_write(self):
        original = self.call
        count = 0
        def call(command, **args):
            nonlocal count
            result = original(command, **args)
            if command == 'platform-target-info':
                count += 1
                if count == 2:
                    result['target_netid'] = 'other'
            return result
        with patch.object(bridge, 'ps_com', side_effect=call):
            result = bridge.com_select_build_platform(self.full, True, True)
        self.assertEqual('context_changed', result['error_code'])
        self.assertEqual([], self.writes)

    def test_agent_tools_registered_with_hard_confirmation(self):
        tools = {t['name']: t for t in agent_core.REGISTRY}
        for name in ('tc_platform_list', 'tc_platform_show', 'tc_platform_set'):
            self.assertIn(name, tools)
        self.assertEqual('system', tools['tc_platform_set']['danger'])
