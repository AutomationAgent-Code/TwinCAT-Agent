"""Shared runtime selection and command-result semantics (no COM side effects)."""
import time
from xml.etree import ElementTree as ET


def has_loaded_program(state):
    """OnlineSettings reports PlcOpState flags, not a single enum value.

    Match whole flags so NotProgramLoaded cannot satisfy the login contract.
    Preserve the raw state in diagnostics; this proves neither binary identity
    nor the application's ADS Run state.
    """
    flags = state.get('operation_state')
    return (state.get('logged_in') is True and isinstance(flags, str) and
            'ProgramLoaded' in {flag.strip() for flag in flags.split('|')})


def parse_online_settings(xml):
    node = ET.fromstring(xml).find('.//OnlineSettings')
    if node is None:
        return {'logged_in': None, 'operation_state': '', 'source': 'unavailable'}
    value = (node.findtext('LoggedIn') or '').strip().lower()
    return {'logged_in': {'true': True, 'false': False}.get(value),
            'operation_state': node.findtext('PlcOpState', ''),
            'application_state': node.findtext('PlcAppState', ''),
            'source': 'NestedProject.ProduceXml.OnlineSettings'}

def select_runtimes(runtimes, runtime='', all_plcs=False):
    items = list(runtimes)
    if runtime and all_plcs:
        raise ValueError('runtime and all_plcs are mutually exclusive')
    if runtime:
        items = [item for item in items if runtime.casefold() == str(item.get('name', '')).casefold()]
        if len(items) != 1:
            raise ValueError('PLC runtime must match exactly one configured name: ' + runtime)
    elif len(items) > 1 and not all_plcs:
        raise ValueError('Multiple PLC runtimes: specify runtime or all_plcs=true; candidates: ' +
                         ', '.join(str(item.get('name')) for item in items))
    return items


def command_failed(result):
    return (not isinstance(result, dict) or bool(result.get('error')) or
            result.get('status') in {'failed', 'error', 'blocked', 'incomplete'} or
            result.get('success') is False or
            any(command_failed(item) for item in result.get('plcs', [])))


def check_online_platform(info):
    """Check live solution and TwinCAT project mappings, not cached build choice.

    A matching mapping does not independently verify target OS/bitness.
    """
    platform = str(info.get('platform') or '')
    contexts = [c for c in info.get('contexts', [])
                if str(c.get('project', '')).lower().endswith(('.tsproj', '.plcproj'))]
    if command_failed(info) or not platform or not info.get('full'):
        return 'platform_unavailable', 'Cannot read active XAE solution platform.'
    if not platform.startswith(('TwinCAT RT (', 'TwinCAT OS (')):
        return 'platform_mismatch', 'PLC online operations cannot use the active platform: ' + platform
    if not contexts:
        return 'platform_mapping_unavailable', 'No live TwinCAT project configuration mapping was returned.'
    if any(str(c.get('platform', '')).casefold() != platform.casefold() for c in contexts):
        return 'platform_mapping_mismatch', 'TwinCAT project platform mapping differs from the active solution platform.'
    return '', ''


def execute(command, args, call, read_state, timeout=30):
    """Use one selected endpoint inventory and verify every stage before advancing."""
    if command not in {'login', 'logout', 'start', 'stop', 'online'}:
        raise ValueError('Unsupported runtime transition: ' + command)
    target = call('project-info')
    netid = str(target.get('target_netid') or '')
    if not netid or not target.get('solution'):
        return {'status': 'blocked', 'verified': False, 'error': 'Target/solution unavailable'}
    inventory = call('plc-runtimes')
    if command_failed(inventory) or inventory.get('target_netid', netid) != netid:
        return {'status': 'blocked', 'verified': False, 'error': 'PLC inventory unavailable or target mismatch'}
    selected = select_runtimes(inventory.get('plcs', []), args.get('runtime', ''), args.get('all_plcs', False))
    if not selected:
        return {'status': 'skipped', 'verified': None, 'plcs': []}
    if any(item.get('ads_port') is None for item in selected):
        return {'status': 'blocked', 'verified': False, 'error': 'PLC ADS port unavailable', 'plcs': selected}
    platform_info = None
    if command != 'logout':
        try:
            platform_info = call('platform-show')
            code, error = check_online_platform(platform_info)
            if not code:
                platform_target = call('platform-target-info')
                if platform_target.get('target_netid') != netid or platform_target.get('target_match_verified') is not True:
                    code, error = 'target_platform_unavailable', 'Cannot verify actual target platform'
                elif platform_info.get('platform') != platform_target.get('target_platform'):
                    code, error = 'target_platform_mismatch', 'Required target platform: ' + str(platform_target.get('target_platform'))
        except Exception as exc:
            code, error = 'platform_unavailable', str(exc)
        if code:
            return {'status': 'blocked', 'verified': False, 'error_code': code,
                    'error': error, 'platform': platform_info, 'failed_stage': 'platform-preflight',
                    'next_action': 'Use tc_platform_list; explicitly select the target-compatible PLC platform with tc_platform_set, verify its mapping, then rebuild/login. No automatic switch performed.',
                    'target_architecture_verified': False, 'plcs': [],
                    'pending_plcs': [p['name'] for p in selected]}
    def unchanged():
        current = call('project-info')
        if any(current.get(k) != target.get(k) for k in ('solution', 'target_netid')):
            raise RuntimeError('XAE target/solution changed during operation')
        now = call('plc-runtimes').get('plcs', [])
        for item in selected:
            match = select_runtimes(now, item['name'])
            if match[0].get('ads_port') != item['ads_port']:
                raise RuntimeError('PLC endpoint changed during operation')
        if platform_info is not None:
            current_platform = call('platform-show')
            code, error = check_online_platform(current_platform)
            current_target = call('platform-target-info')
            if current_target.get('target_match_verified') is not True or current_target.get('target_platform') != platform_info.get('platform') or current_target.get('target_netid') != netid:
                raise RuntimeError('Target platform changed or became unavailable')
            if code or any(current_platform.get(k) != platform_info.get(k) for k in ('full', 'contexts')):
                raise RuntimeError('XAE build platform/mapping changed during operation: ' + error)
    def poll(probe, predicate):
        deadline = time.monotonic() + timeout
        last = None
        error = ''
        while True:
            try:
                last = probe()
                error = ''
                if predicate(last):
                    return last
            except Exception as exc:
                error = str(exc)
            if time.monotonic() >= deadline:
                raise RuntimeError('State verification timed out: ' + (error or str(last)))
            time.sleep(0.25)
    results = []
    for runtime in selected:
        item = dict(runtime, verified=False, stages=[])
        results.append(item)
        try:
            unchanged()
            if command != 'logout':
                system = read_state(netid, 10000)
                if system.get('state_code') != 5:
                    raise RuntimeError('TwinCAT system must be Run; no mode change performed')
            def online():
                return call('plc-online-state', runtime=runtime['name'])
            before = online()
            item['before'] = before
            for stage in (['login', 'start'] if command == 'online' else [command]):
                unchanged()
                current = online()
                wanted_login = stage != 'logout'
                if stage in {'login', 'logout'}:
                    ready = lambda s: s.get('logged_in') is wanted_login and (
                        not wanted_login or has_loaded_program(s))
                else:
                    if not has_loaded_program(current):
                        raise RuntimeError('Start/Stop requires confirmed IDE login and ProgramLoaded')
                    expected = 5 if stage == 'start' else 6
                    ready = lambda s: s.get('state_code') == expected
                probe = online if stage in {'login', 'logout'} else lambda: read_state(netid, int(runtime['ads_port']))
                state = probe()
                if ready(state):
                    item['stages'].append({'command': stage, 'status': 'already_satisfied', 'state': state,
                                           'command_sent': False, 'message': '状态已满足，未发送命令'})
                    if stage == 'login' and state.get('application_state') == 'Run':
                        item['next_action'] = 'PLC 已是 Run，不要继续调用 tc_start；仅需刷新时使用只读状态检查。'
                    continue
                stage_result = {'command': stage, 'status': 'requested', 'before': state, 'command_sent': None}
                item['stages'].append(stage_result)
                result = call(stage, runtime=runtime['name'])
                if command_failed(result):
                    raise RuntimeError(str(result))
                stage_result['command_sent'] = True
                state = poll(probe, ready)
                stage_result.update(status='verified', state=state)
                if stage == 'login' and state.get('application_state') == 'Run':
                    item['next_action'] = 'PLC 已是 Run，不要继续调用 tc_start；仅需刷新时使用只读状态检查。'
            unchanged()
            item['verified'] = True
        except Exception as exc:
            item['error'] = str(exc)
            # Do not proceed to other PLCs after an uncertain transition.
            break
    verified = len(results) == len(selected) and all(x['verified'] for x in results)
    no_command_sent = bool(results) and all(p.get('stages') and all(s.get('command_sent') is False for s in p['stages']) for p in results)
    return {'status': 'verified' if verified else 'incomplete', 'verified': verified,
            'no_command_sent': no_command_sent,
            'execution_summary': '目标状态已满足，未发送操作命令；不得报告为执行启动成功。' if no_command_sent else '请按各阶段 command_sent 和状态报告实际动作。',
            'command': command, 'target_netid': netid, 'solution': target['solution'],
            'plcs': results, 'pending_plcs': [x['name'] for x in selected[len(results):]],
            'platform': platform_info, 'target_architecture_verified': False,
            'application_identity_verified': False}
