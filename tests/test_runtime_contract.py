import unittest
from unittest.mock import patch

from tc_template.runtime_contract import select_runtimes, execute, parse_online_settings
from tc_agent import agent_core
from tc_template import tc_platform, _ps_bridge


class RuntimeContractTests(unittest.TestCase):
    def setUp(self):
        self.plcs = [{'name': 'A', 'ads_port': 851}, {'name': 'B', 'ads_port': 861}]
        self.writes = []
        self.logged = False
        self.running = False
        self.reject = False
        self.pending = False
        self.ports = []
        self.system = 5
        self.changed = False
        self.operation_state = 'ProgramLoaded'
        self.platform = {'platform': 'TwinCAT RT (x64)', 'full': 'Release|TwinCAT RT (x64)',
                         'contexts': [{'project': 'Test.tsproj', 'platform': 'TwinCAT RT (x64)'}]}

    def call(self, verb, *transport_args, **args):
        if verb == 'platform-target-info':
            return {'target_match_verified': True, 'target_platform': 'TwinCAT RT (x64)', 'target_netid': '1.2.3.4.1.1'}
        if verb == 'platform-show':
            return self.platform
        if verb == 'project-info':
            return {'target_netid': 'other' if self.changed else '1.2.3.4.1.1', 'solution': 'Test.sln'}
        if verb == 'plc-runtimes':
            return {'plcs': self.plcs}
        if verb == 'plc-online-state':
            return {'logged_in': self.logged, 'operation_state': self.operation_state if self.logged else ''}
        self.writes.append((verb, args))
        if self.reject:
            return {'status': 'failed', 'error': 'rejected'}
        if verb == 'login' and not self.pending:
            self.logged = True
        if verb == 'logout':
            self.logged = False
        if verb in {'start', 'stop'}:
            self.running = verb == 'start'
        return {'status': 'accepted'}

    def read_state(self, netid, port):
        self.ports.append(port)
        return {'state_code': self.system if port == 10000 else (5 if self.running else 6)}

    def run_command(self, command, **args):
        return execute(command, args or {'runtime': 'A'}, self.call, self.read_state, timeout=0)

    def test_multiple_requires_explicit_selection(self):
        with self.assertRaises(ValueError):
            select_runtimes(self.plcs)
        self.assertEqual([self.plcs[1]], select_runtimes(self.plcs, 'B'))
        self.assertEqual(self.plcs, select_runtimes(self.plcs, all_plcs=True))

    def test_hmi_platform_blocks_before_login_or_ads(self):
        self.platform = {'platform': 'TwinCAT HMI', 'full': 'Release|TwinCAT HMI'}
        result = self.run_command('online')
        self.assertEqual('platform_mismatch', result['error_code'])
        self.assertFalse(result['verified'])
        self.assertEqual([], self.writes)
        self.assertEqual([], self.ports)

    def test_wrong_project_mapping_blocks(self):
        self.platform['contexts'][0]['platform'] = 'TwinCAT RT (x86)'
        self.assertEqual('platform_mapping_mismatch', self.run_command('login')['error_code'])
        self.assertEqual([], self.writes)

    def test_plc_mapping_checked_and_hmi_mapping_ignored(self):
        self.platform['contexts'].append({'project': 'UI.hmiproj', 'platform': 'TwinCAT HMI'})
        self.assertTrue(self.run_command('login')['verified'])
        self.platform['contexts'].append({'project': 'PLC1.plcproj', 'platform': 'TwinCAT RT (x86)'})
        self.assertEqual('platform_mapping_mismatch', self.run_command('login')['error_code'])

    def test_missing_platform_fails_closed(self):
        self.platform = {}
        self.assertEqual('platform_unavailable', self.run_command('start')['error_code'])

    def test_logout_does_not_require_plc_build_platform(self):
        self.platform = {'platform': 'TwinCAT HMI'}
        self.logged = True
        self.assertTrue(self.run_command('logout')['verified'])

    def test_platform_change_after_login_blocks_start(self):
        original = self.call
        def call(verb, **args):
            result = original(verb, **args)
            if verb == 'login':
                self.platform = {'platform': 'TwinCAT HMI', 'full': 'Release|TwinCAT HMI'}
            return result
        result = execute('online', {'runtime': 'A'}, call, self.read_state, timeout=0)
        self.assertFalse(result['verified'])
        self.assertEqual(['login'], [v for v, _ in self.writes])

    def test_empty_is_skipped_not_verified(self):
        self.plcs = []
        result = self.run_command('online', all_plcs=True)
        self.assertEqual('skipped', result['status'])
        self.assertIsNone(result['verified'])

    def test_online_waits_for_login_then_starts_without_logout(self):
        result = self.run_command('online')
        self.assertTrue(result['verified'])
        self.assertEqual(['login', 'start'], [v for v, _ in self.writes])
        self.assertFalse(result['application_identity_verified'])

    def test_pending_login_never_starts(self):
        self.pending = True
        result = self.run_command('online')
        self.assertFalse(result['verified'])
        self.assertEqual(['login'], [v for v, _ in self.writes])

    def test_failed_command_cannot_be_masked(self):
        self.logged = True
        self.reject = True
        result = self.run_command('start')
        self.assertFalse(result['verified'])
        self.assertIn('rejected', result['plcs'][0]['error'])

    def test_config_blocks_start_without_changing_mode(self):
        self.system = 15
        self.assertFalse(self.run_command('start')['verified'])
        self.assertEqual([], self.writes)

    def test_all_plcs_use_actual_ports_and_exact_selectors(self):
        self.logged = True
        states = {851: 6, 861: 6}
        def call(verb, **args):
            result = self.call(verb, **args)
            if verb == 'start':
                port = next(p['ads_port'] for p in self.plcs if p['name'] == args['runtime'])
                states[port] = 5
            return result
        def read(netid, port):
            self.ports.append(port)
            return {'state_code': 5 if port == 10000 else states[port]}
        result = execute('start', {'all_plcs': True}, call, read, timeout=0)
        self.assertTrue(result['verified'])
        self.assertIn(851, self.ports)
        self.assertIn(861, self.ports)
        self.assertEqual([('start', {'runtime': 'A'}), ('start', {'runtime': 'B'})], self.writes)

    def test_pending_login_leaves_other_plcs_pending(self):
        self.pending = True
        result = self.run_command('online', all_plcs=True)
        self.assertEqual(['B'], result['pending_plcs'])
        self.assertEqual([('login', {'runtime': 'A'})], self.writes)

    def test_logout_verifies_without_requiring_system_run(self):
        self.logged = True
        self.system = 15
        self.assertTrue(self.run_command('logout')['verified'])
        self.assertNotIn(10000, self.ports)

    def test_already_running_is_idempotent(self):
        self.logged = self.running = True
        result = self.run_command('online')
        self.assertTrue(result['verified'])
        self.assertTrue(all(s['command_sent'] is False for s in result['plcs'][0]['stages']))
        self.assertEqual([], self.writes)

    def test_combined_loaded_flags_skip_login_and_online_commands(self):
        for flags in ('ProgramLoaded|BootprojectValid',
                      'BootprojectValid | ProgramLoaded', 'ProgramLoaded'):
            for command in ('login', 'online'):
                with self.subTest(flags=flags, command=command):
                    self.logged = self.running = True
                    self.operation_state = flags
                    result = self.run_command(command)
                    self.assertTrue(result['verified'])
                    self.assertTrue(result['no_command_sent'])
                    self.assertFalse(result['application_identity_verified'])
                    self.assertEqual([], self.writes)
                    self.assertEqual(flags, result['plcs'][0]['before']['operation_state'])

    def test_login_poll_accepts_combined_loaded_flags(self):
        self.operation_state = 'ProgramLoaded|BootprojectValid'
        result = self.run_command('online')
        self.assertTrue(result['verified'])
        self.assertEqual(['login', 'start'], [v for v, _ in self.writes])

    def test_start_and_stop_accept_combined_flags_but_verify_ads(self):
        self.logged = True
        self.operation_state = 'ProgramLoaded|BootprojectValid'
        self.assertTrue(self.run_command('start')['verified'])
        self.assertTrue(self.run_command('stop')['verified'])
        self.assertEqual(['start', 'stop'], [v for v, _ in self.writes])
        self.assertIn(851, self.ports)

    def test_loaded_flag_requires_exact_token_and_true_login(self):
        for flags in ('NotProgramLoaded|BootprojectValid', 'BootprojectValid', '', None):
            with self.subTest(flags=flags):
                self.logged = True
                self.operation_state = flags
                result = self.run_command('start')
                self.assertFalse(result['verified'])
                self.assertEqual([], self.writes)
        self.logged = False
        self.operation_state = 'ProgramLoaded|BootprojectValid'
        self.assertFalse(self.run_command('start')['verified'])
        self.assertEqual([], self.writes)

    def test_target_change_after_login_blocks_start(self):
        original = self.call
        def call(verb, **args):
            result = original(verb, **args)
            if verb == 'login':
                self.changed = True
            return result
        result = execute('online', {'runtime': 'A'}, call, self.read_state, timeout=0)
        self.assertFalse(result['verified'])
        self.assertEqual(['login'], [v for v, _ in self.writes])

    def test_parse_online_evidence(self):
        self.assertIsNone(parse_online_settings('<TreeItem/>')['logged_in'])
        result = parse_online_settings('<TreeItem><OnlineSettings><LoggedIn>true</LoggedIn><PlcOpState>ProgramLoaded</PlcOpState></OnlineSettings></TreeItem>')
        self.assertTrue(result['logged_in'])
        self.assertEqual('ProgramLoaded', result['operation_state'])

    def test_frontends_share_contract_and_strip_all_selector(self):
        with patch.object(_ps_bridge, '_ps_com_raw', side_effect=self.call), patch('tc_agent.ads.read_ads_state', side_effect=self.read_state):
            result = _ps_bridge.ps_com('online', all_plcs=True)
        self.assertTrue(result['verified'])
        self.assertTrue(all('all_plcs' not in args for _, args in self.writes))

    def test_native_online_delegates(self):
        with patch.object(_ps_bridge, 'ps_com', return_value={'verified': False}) as call:
            self.assertFalse(tc_platform.full_online_cycle(runtime='B')['verified'])
        call.assert_called_once_with('online', runtime='B', all_plcs=False, timeout=60)

    def test_state_uses_system_service_without_com_state_guess(self):
        with patch.object(_ps_bridge, '_ps_com_raw', return_value={'target_netid': '1.2.3.4.1.1'}) as raw, patch('tc_agent.ads.read_ads_state', return_value={'state_code': 15, 'state_name': 'Config'}):
            result = _ps_bridge.ps_com('state')
        self.assertEqual('Config', result['state'])
        self.assertEqual(10000, result['port'])
        self.assertEqual(1, raw.call_count)

    def test_rejected_restart_is_not_verified_from_existing_run(self):
        with patch.object(_ps_bridge, '_ps_com_raw', side_effect=[{'target_netid': '1.2.3.4.1.1'}, {'status': 'failed'}]), patch('tc_agent.ads.read_ads_state') as read:
            result = _ps_bridge.ps_com('restart')
        self.assertFalse(result['verified'])
        read.assert_not_called()

    def test_rejected_activation_is_not_submitted(self):
        with patch.object(_ps_bridge, '_ps_com_raw', return_value={'status': 'failed'}):
            result = _ps_bridge.ps_com('activate')
        self.assertFalse(result['configuration_submitted'])


if __name__ == '__main__':
    unittest.main()
