from tc_agent.agent_core import tool_result_for_context, compact_messages


def test_large_preflight_retains_decision_and_is_idempotent():
    r = {'status': 'blocked', 'approved': False, 'written': False, 'candidates': [
        {'name': 'FB_Test', 'approved': False, 'review': {
            'dependencies': ['x' * 50000],
            'semantic_evidence': {'status': 'incomplete', 'unsupported_reasons': ['missing signature']},
            'findings': [{'rule': 'semantic-unresolved', 'severity': 'error',
                          'line': 17, 'message': 'missing signature'}] * 100}}]}
    s = tool_result_for_context('plc_preflight', {}, r, limit=2500)
    assert s['unsupported_reason_count'] == 1
    assert s['findings_count'] == 100 and s['details_omitted']
    assert s['candidates'][0]['findings'][0]['line'] == 17
    assert s['candidates'][0]['semantic_status'] == 'incomplete'
    assert s['absence_is_not_clearance']
    again, _ = compact_messages([{'role': 'tool', 'name': 'plc_preflight', 'result': s}], limit=2500)
    assert again[0]['result'] == s


def test_missing_evidence_is_unknown_not_verified():
    s = tool_result_for_context('plc_preflight', {}, {'candidates': [{'review': {}}]})
    assert s['evidence_missing_count'] == 1
    assert s['candidates'][0]['semantic_status'] == 'unknown'
    assert s['candidates'][0]['unsupported_reason_count'] is None


def test_legacy_cap_does_not_imply_clearance():
    s = tool_result_for_context('plc_preflight', {}, {'_capped': True, 'summary': {'type': 'dict'}})
    assert s['evidence_missing_count'] == 1
    assert s['status'] == 'unknown'


def test_large_single_candidate_keeps_first_location_in_small_budget():
    r = {'status': 'blocked', 'candidates': [{'name': 'FB_Test', 'path': 'TIPC^' + 'x' * 5000,
         'approved': False, 'review': {'semantic_evidence': {
             'status': 'incomplete', 'unsupported_reasons': ['missing ' * 1000]},
         'findings': [{'severity': 'error', 'rule': 'semantic-unresolved',
                       'line': 88, 'message': 'details ' * 1000}] * 20}}]}
    s = tool_result_for_context('plc_preflight', {}, r, limit=1800)
    assert s['candidates'][0]['findings'][0]['line'] == 88
    assert s['candidates'][0]['semantic_status'] == 'incomplete'
    assert s['omitted_candidates'] == 0
    assert s['candidates'][0]['identity_truncated'] is True


def test_input_over_four_k_reaches_complete_review_unchanged():
    from unittest.mock import patch
    from tc_template.plc_preflight import review_candidates
    candidate = {'name': 'MAIN', 'path': 'TIPC^P^Project^POUs^MAIN',
                 'declaration': 'PROGRAM MAIN\nVAR\nEND_VAR',
                 'implementation': '(* source padding *)\n' * 1000 + 'END_MARKER'}
    with patch('tc_template.plc_preflight.review_write_candidate', return_value={
            'approved': True, 'blocking_findings': [], 'advisories': []}) as quality, \
         patch('tc_template.plc_preflight.review_dependencies', return_value={
             'findings': [], 'dependencies': []}) as semantic:
        result = review_candidates([candidate], lambda *a, **kw: {})
    assert quality.call_args.args[0]['implementation'] == candidate['implementation']
    assert semantic.call_args.args[0]['implementation'].endswith('END_MARKER')
    assert result['approved'] is True
