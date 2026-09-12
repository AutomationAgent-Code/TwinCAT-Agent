"""Error impact is separate from success, permission and retry eligibility.

Never grants permission or replays an operation. Existing preconditions and
bounded no-progress counters remain authoritative.
"""


from tc_agent.tool_error_catalog import CONDITIONS, ERROR_CODES, ERROR_TYPES, FAILURE_STATUSES, FAMILIES


def _policy(family, code):
    level, impact, action = FAMILIES[family]
    return {'level': level, 'impact': impact, 'stop_turn': impact == 'turn',
            'action': action, 'family': family, 'code': code,
            'classification_version': 1, 'classified': family != 'unknown'}


def classify_error(result, ok):
    if not isinstance(result, dict):
        return {'level': 'L0' if ok else 'L2', 'impact': 'none' if ok else 'step',
                'stop_turn': False, 'action': 'continue' if ok else 'inspect_result'}
    status = result.get('status')
    error_type = result.get('error_type')
    error_code = result.get('error_code')
    condition = result.get('condition')
    if (FAILURE_STATUSES.get(status) == 'uncertain'
            or ERROR_TYPES.get(error_type) == 'uncertain'
            or result.get('written') == 'unknown'):
        return _policy('uncertain', str(error_type or status or 'write_result_unknown'))
    if (result.get('uncertain') is True or status == 'uncertain'
            or (result.get('written') is True and result.get('verified') is False)):
        return _policy('uncertain', 'mutation_unverified')
    if result.get('authorization_blocked') or 'denied' in result or status in {'denied', 'approval_expired'}:
        policy = _policy('authorization', str(status or 'authorization_blocked'))
        policy['stop_turn'] = bool(result.get('authorization_blocked'))
        return policy
    if ok and status not in FAILURE_STATUSES and not error_type and not error_code:
        return {'level': 'L1' if result.get('warnings') or result.get('warning_count') else 'L0',
                'impact': 'none', 'stop_turn': False, 'action': 'continue'}
    if result.get('capability_exhausted') or result.get('recovery_exhausted'):
        policy = _policy('capability' if result.get('capability_exhausted') else 'diagnostics',
                         'capability_exhausted' if result.get('capability_exhausted') else 'recovery_exhausted')
        policy['action'] += '恢复预算已耗尽，不再重复尝试；无独立工作时报告部分完成与阻塞。'
        return policy
    evidence = result.get('semantic_evidence')
    review = result.get('review')
    if not isinstance(evidence, dict) and isinstance(review, dict):
        evidence = review.get('semantic_evidence')
    if isinstance(evidence, dict):
        if evidence.get('status') == 'invalid':
            return _policy('source', 'semantic_invalid')
        if evidence.get('status') == 'incomplete':
            return _policy('capability', 'semantic_incomplete')
    if isinstance(review, dict) and review.get('approved') is False:
        return _policy('source', 'review_rejected')
    if condition in CONDITIONS:
        return _policy(CONDITIONS[condition], 'precondition.' + condition)
    if error_code in ERROR_CODES:
        return _policy(ERROR_CODES[error_code], error_code)
    if error_type in ERROR_TYPES:
        return _policy(ERROR_TYPES[error_type], error_type)
    if status in FAILURE_STATUSES:
        return _policy(FAILURE_STATUSES[status], 'status.' + status)
    if result.get('isError') is True:
        return _policy('unknown', 'external_mcp_error')
    if result.get('compiler_verified') is False and result.get('diagnostics_complete') is True:
        return _policy('source', 'compiler_rejected')
    return _policy('unknown', str(error_type or error_code or 'unclassified_failure'))


def annotate_error(result, ok):
    if isinstance(result, dict):
        result['error_policy'] = classify_error(result, ok)
    return result
