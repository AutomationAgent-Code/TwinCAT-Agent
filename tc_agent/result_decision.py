"""Small evidence envelope retained even if a generic preview overflows."""


def decision(result):
    if not isinstance(result, dict):
        return {}
    if isinstance(result.get('decision_evidence'), dict):
        return result['decision_evidence']
    out = {'omission_is_not_clearance': True}
    for key in ('approved', 'written', 'verified', 'not_executed', 'retry_safe',
                'compiler_verified', 'diagnostics_complete', 'diagnostics_pending',
                'capability_exhausted', 'recovery_exhausted', 'authorization_blocked',
                'error_type', 'error_code', 'condition'):
        if key in result and isinstance(result[key], (str, bool, int, float, type(None))):
            out[key] = result[key][:160] if isinstance(result[key], str) else result[key]
    policy = result.get('error_policy')
    if isinstance(policy, dict):
        out['error_policy'] = {k: policy[k] for k in
                               ('level', 'family', 'impact', 'stop_turn', 'code', 'classified') if k in policy}
    for key in ('next_action', 'error'):
        if key in result:
            out[key] = str(result[key])[:400]
    review = result.get('review')
    owner = review if isinstance(review, dict) else result
    evidence = owner.get('semantic_evidence')
    if isinstance(evidence, dict):
        reasons = evidence.get('unsupported_reasons')
        out['semantic_status'] = evidence.get('status', 'unknown')
        out['unsupported_reason_count'] = len(reasons) if isinstance(reasons, list) else None
        out['unsupported_reasons'] = [str(r)[:160] for r in (reasons or [])[:2]]
    findings = owner.get('findings')
    if isinstance(findings, list):
        out['findings_count'] = len(findings)
        errors = [f for f in findings if isinstance(f, dict) and f.get('severity') == 'error']
        out['error_finding_count'] = len(errors)
        out['findings'] = [{k: str(f[k])[:160] if isinstance(f[k], str) else f[k]
                            for k in ('rule', 'severity', 'area', 'line', 'column', 'message') if k in f}
                           for f in (errors or findings)[:2] if isinstance(f, dict)]
        out['findings_omitted'] = len(findings) - len(out['findings'])
    return out
