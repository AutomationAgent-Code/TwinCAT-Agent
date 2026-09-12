from tc_template.semantic_evidence import apply_source_write_policy, classify, DEFERRED_MESSAGES


def test_only_explicit_deep_capabilities_defer_after_parsing():
    findings = [{'rule':'semantic-unresolved','severity':'error','message':m} for m in DEFERRED_MESSAGES]
    result = apply_source_write_policy(findings, {'syntax_status':'parsed'})
    assert all(f['severity']=='warning' and f['verification_required'] for f in result)
    assert all(f['severity']=='error' for f in findings)
    assert classify(result)['status']=='incomplete'
    assert not classify(result)['compiler_verified']


def test_syntax_dependency_and_real_errors_remain_blocking():
    findings = [{'rule':rule,'severity':'error','message':message} for rule,message in [
        ('semantic-unresolved','Unresolved symbol: typo'),
        ('syntax-unsupported','Unsupported expression'),
        ('dependency-context-unavailable','COM unavailable'),
        ('semantic-type','Array bounds are incompatible.'),
        ('semantic-unresolved','Future unknown capability')]]
    assert apply_source_write_policy(findings, {'syntax_status':'parsed'}) == findings


def test_missing_or_incomplete_syntax_never_defers():
    findings=[{'rule':'semantic-unresolved','severity':'error','message':next(iter(DEFERRED_MESSAGES))}]
    for review in [None, {}, {'syntax_status':'unsupported'}]:
        assert apply_source_write_policy(findings, review)==findings


def test_preflight_and_write_share_policy_without_claiming_verified():
    from unittest.mock import patch
    from tc_template.plc_preflight import review_candidates
    from tc_template.lint import review_write_candidate
    from tc_agent import agent_core
    candidate={'name':'MAIN','path':'TIPC^P^Proj^POUs^MAIN',
               'declaration':'PROGRAM MAIN\nVAR n:UDINT; u:__UXINT; END_VAR',
               'implementation':'n := u;'}
    def call(verb, **kwargs):
        if verb=='find-pou': return {'matches':[], 'total':0}
        raise AssertionError(verb)
    preflight=review_candidates([candidate],call)
    assert preflight['approved'],preflight
    context=preflight['candidates'][0]['review']
    assert context['semantic_evidence']['status']=='incomplete'
    assert not context['semantic_review']['approved']
    review=review_write_candidate(candidate)
    with patch.object(agent_core,'ps_com',side_effect=call):
        agent_core._attach_plc_dependencies(review,candidate,candidate['path'])
    assert review['approved'],review
    assert any(f.get('verification_required') for f in review['advisories'])
    assert not review['project_context']['compiler_verified']
