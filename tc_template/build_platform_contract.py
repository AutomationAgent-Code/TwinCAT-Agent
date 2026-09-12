"""Build-platform evidence without conflating local compile and target compatibility."""


def assess(kind, call):
    """Return a read-only platform assessment; never select or mutate a platform."""
    try:
        current = call('platform-show')
        platform = str(current.get('platform') or '')
        suffix = '.hmiproj' if kind == 'hmi' else ('.tsproj', '.plcproj')
        contexts = [c for c in current.get('contexts', [])
                    if str(c.get('project', '')).lower().endswith(suffix)]
        target = None
        if kind == 'hmi':
            expected = 'TwinCAT HMI'
            local_valid = bool(contexts) and all(c.get('platform') == expected for c in contexts)
            target_verified = True
            target_compatible = True
        else:
            target = call('platform-target-info')
            expected = (str(target.get('target_platform') or '')
                        if target.get('target_match_verified') is True else '')
            local_valid = bool(platform) and bool(contexts) and all(
                c.get('platform') == platform and c.get('should_build') is not False
                for c in contexts)
            target_verified = bool(expected)
            target_compatible = platform == expected if target_verified else None
        allowed = local_valid and (kind == 'hmi' or not target_verified or target_compatible is True)
        reason = ''
        code = ''
        if not local_valid:
            code = 'build_platform_context_invalid'
            reason = 'Active platform and PLC/HMI project build contexts are incomplete, disabled or inconsistent.'
        elif kind != 'hmi' and target_verified and not target_compatible:
            code = 'build_platform_mismatch'
            reason = 'Active local build platform does not match the exact target response.'
        return {'allowed': allowed, 'platform': current, 'target': target,
                'local_build_context_verified': local_valid,
                'target_compatibility_verified': target_verified,
                'target_compatible': target_compatible,
                'error_code': code, 'reason': reason,
                'warning': '' if target_verified or kind == 'hmi' else
                    'Target platform could not be queried. Local compiler execution is allowed because the active platform and all PLC build contexts agree; target compatibility remains unverified.'}
    except Exception as exc:
        return {'allowed': False, 'platform': None, 'target': None,
                'local_build_context_verified': False,
                'target_compatibility_verified': False, 'target_compatible': None,
                'error_code': 'build_platform_unavailable', 'reason': str(exc), 'warning': ''}


def preflight(kind, call):
    evidence = assess(kind, call)
    if evidence['allowed']:
        return None
    current = evidence.get('platform') or {}
    target = evidence.get('target')
    expected = str((target or {}).get('target_platform') or '')
    return {'status': 'blocked', 'verified': False, 'build_succeeded': False,
            'build_performed': False, 'error_code': evidence['error_code'],
            'failedProjects': None, 'errorCount': None, 'errors': [],
            'errorsRead': False, 'diagnosticsAvailable': False,
            'diagnostics_complete': False, 'repair_allowed': False,
            'platform_preflight': evidence, 'platform': current, 'target': target,
            'error': evidence['reason'],
            'next_action': 'Inspect tc_platform_show/tc_platform_list and target evidence; platform changes require separate confirmation.',
            'required_full': (str(current.get('config') or '') + '|' + expected) if expected else None}
