import asyncio
from unittest.mock import patch

import pytest

from tc_template.hmi_symbols import check_symbol_value
from tc_template.lint import _st_code, _conditional_call
from tc_agent.host_lease import HostLease, HostUnavailable


@pytest.mark.parametrize('source', [
    'CASE eLineCmd OF 1: fbFeed(); END_CASE',
    'IF bRun THEN fbFeed(); END_IF',
    'FOR nI:=1 TO 3 DO fbFeed(); END_FOR',
    'WHILE bRun DO fbFeed(); END_WHILE',
    'REPEAT fbFeed(); UNTIL bDone END_REPEAT',
    'CASE nStep OF 1: IF bX THEN fbFeed(); END_IF END_CASE',
])
def test_conditional_calls_do_not_depend_on_state_name(source):
    assert _conditional_call(_st_code(source), 'fbFeed')


def test_calls_after_input_preparation_are_unconditional():
    source = "(* CASE x (* nested *) OF *) s := 'IF fbFeed()'; IF x THEN y:=1; END_IF; fbFeed();"
    assert not _conditional_call(_st_code(source), 'fbFeed')


@pytest.mark.parametrize('raw', [
    '=%s%ADS.PLC1.X%/s%', '=%s%ADS.PLC1.X%/dint%', '%s%ADS.PLC1.X%/real%',
    '%s%X%/i%', '%s%X', '{"expression":"=%s%X%/real%"}',
    '%s%PLC1::GVL_Hmi::bStart%/s%',
    '%f%%s%PLC1::GVL_Hmi::nCount%/s% + 1%/f%',
])
def test_damaged_bindings_rejected(raw):
    with pytest.raises(ValueError): check_symbol_value(raw)


@pytest.mark.parametrize('raw', ['100%', 'hello', '%s%ADS.PLC1.X%/s%',
    '%f%%s%ADS.PLC1.X%/s% * 100%/f%', '{"expression":"%s%ADS.PLC1.X%/s%"}'])
def test_legitimate_bindings_and_text_preserved(raw):
    check_symbol_value(raw)


def test_pid_reuse_is_not_same_host():
    with patch('tc_agent.host_lease.process_identity', side_effect=[(1,0,100),(1,0,200)]):
        lease = HostLease(1, 'test.sln')
        with pytest.raises(HostUnavailable): lease.check()


def test_host_exit_cancels_model_not_retargets():
    async def scenario():
        cancelled = asyncio.Event()
        async def model():
            try: await asyncio.Event().wait()
            finally: cancelled.set()
        with patch('tc_agent.host_lease.process_identity', side_effect=[(1,0,1),(1,0,1),HostUnavailable('closed')]):
            lease = HostLease(1, 'test.sln')
            with pytest.raises(HostUnavailable): await lease.watch_model(model, interval=.001)
        assert cancelled.is_set()
    asyncio.run(scenario())


def test_solution_change_blocks_write():
    with patch('tc_agent.host_lease.process_identity', return_value=(1,0,1)), \
         patch('tc_template._ps_bridge.ps_com', return_value={'solution':'other.sln'}):
        with pytest.raises(HostUnavailable): HostLease(1,'original.sln').check_solution()


def test_incomplete_run_is_durable(tmp_path):
    from tc_agent.conversation_store import ConversationStore
    from tc_agent.runtime import DurableRun
    store = ConversationStore(tmp_path / 'agent.db')
    thread = store.list_threads()[0]
    run = DurableRun(store,thread['id'],'build this')
    run.finish('incomplete','browser unavailable')
    store.set_status(thread['id'],'incomplete')
    assert store.get_agent_run(run.run_id)['status'] == 'incomplete'


def test_browser_root_alone_cannot_pass_acceptance(tmp_path):
    import json
    from tc_template.hmi_browser_evidence import compare_controls
    (tmp_path/'Properties').mkdir()
    (tmp_path/'Properties/tchmiconfig.json').write_text(json.dumps({'startupView':'Desktop.view'}))
    (tmp_path/'Desktop.view').write_text('<div id="Root" data-tchmi-type="View"><div id="Button" data-tchmi-type="Button"/></div>')
    result={'success':True,'status':'passed','viewport_results':[{'valid':True,'metrics':{'control_ids':['Root']}}]}
    checked=compare_controls(result,tmp_path/'Hmi.hmiproj')
    assert checked['success'] is False
    assert checked['loaded_view'] == 'Desktop.view'
    assert checked['saved_target_page_verified'] is False
    assert checked['viewport_results'][0]['missing_saved_control_ids']==['Button']


def test_hmi_acceptance_requires_complete_phased_evidence():
    from tc_agent.completion_evidence import CompletionEvidence
    evidence = CompletionEvidence()
    evidence.record('tc_hmi_control_edit', {'file': 'Main.view', 'apply': True},
                    {'status': 'applied', 'verified': True}, False)
    evidence.record('tc_hmi_build', {}, {
        'status': 'succeeded', 'build_succeeded': True,
        'diagnostics_available': True, 'diagnostics_complete': True,
    }, False)
    evidence.record('tc_hmi_validate', {}, {'status': 'validated', 'valid': True}, True)
    evidence.record('tc_hmi_bindings', {}, {'status': 'ok', 'valid': True, 'binding_count': 3}, True)
    evidence.record('tc_hmi_browser_validate', {}, {
        'status': 'passed', 'success': True, 'saved_target_page_verified': True,
        'loaded_view': 'Main.view',
    }, True)
    issues = evidence.issues('')
    assert any('tc_hmi_ads_live_check' in issue for issue in issues)
    evidence.record('tc_hmi_ads_live_check', {}, {'status': 'verified', 'verified': True}, True)
    assert evidence.issues('') == []


def test_ready_binding_diagnosis_supplies_static_and_online_acceptance_evidence():
    from tc_agent.completion_evidence import CompletionEvidence
    evidence = CompletionEvidence()
    evidence.record('tc_hmi_control_edit', {'file': 'Main.view', 'apply': True},
                    {'status': 'applied', 'verified': True}, False)
    evidence.record('tc_hmi_build', {}, {
        'status': 'succeeded', 'build_succeeded': True,
        'diagnostics_available': True, 'diagnostics_complete': True,
    }, False)
    evidence.record('tc_hmi_validate', {}, {'status': 'validated', 'valid': True}, True)
    evidence.record('tc_hmi_binding_diagnose', {}, {
        'status': 'ready', 'verified': True, 'static_verified': True,
        'binding_count': 3, 'online': {'status': 'verified', 'verified': True},
    }, True)
    evidence.record('tc_hmi_browser_validate', {}, {
        'status': 'passed', 'success': True, 'saved_target_page_verified': True,
        'loaded_view': 'Main.view',
    }, True)
    assert evidence.issues('') == []


def test_hmi_acceptance_rejects_wrong_browser_view_and_missing_diagnostics():
    from tc_agent.completion_evidence import CompletionEvidence
    evidence = CompletionEvidence()
    evidence.record('tc_hmi_control_edit', {'file': 'Main.view', 'apply': True},
                    {'status': 'applied', 'verified': True}, False)
    evidence.record('tc_hmi_build', {}, {'status': 'succeeded', 'build_succeeded': True}, False)
    evidence.record('tc_hmi_validate', {}, {'status': 'validated', 'valid': True}, True)
    evidence.record('tc_hmi_bindings', {}, {'status': 'ok', 'valid': True, 'binding_count': 0}, True)
    evidence.record('tc_hmi_browser_validate', {}, {
        'status': 'passed', 'success': True, 'saved_target_page_verified': True,
        'loaded_view': 'Desktop.view',
    }, True)
    issues = evidence.issues('')
    assert any('完整诊断' in issue for issue in issues)
    assert any('不是本轮修改' in issue for issue in issues)


def test_failed_binding_diagnosis_is_preserved_and_blocks_success_claim():
    from tc_agent.completion_evidence import CompletionEvidence
    evidence = CompletionEvidence()
    evidence.record('tc_hmi_control_edit', {'file': 'Main.view', 'apply': True},
                    {'status': 'applied', 'verified': True}, False)
    evidence.record('tc_hmi_binding_diagnose', {}, {
        'status': 'blocked', 'verified': False, 'static_verified': False,
        'blocking_stage': 'static-bindings',
    }, True)
    assert evidence.hmi_checks['tc_hmi_binding_diagnose']['blocking_stage'] == 'static-bindings'
    assert 'static-bindings' in evidence.rejected_final('')
    assert '已撤回' in evidence.rejected_final('')


def test_failed_write_blocks_success_even_when_nothing_was_changed():
    from tc_agent.completion_evidence import CompletionEvidence
    evidence = CompletionEvidence()
    evidence.record('tc_hmi_write_markup', {'file': 'Main.view', 'apply': True},
                    {'status': 'blocked', 'written': False,
                     'reason': 'binding contract rejected candidate'}, False)
    assert evidence.changed == set()
    assert 'tc_hmi_write_markup' in evidence.rejected_final('')


def test_permission_denial_is_not_a_retryable_validation_gap():
    from tc_agent.completion_evidence import CompletionEvidence
    evidence = CompletionEvidence()
    evidence.record('plc_create_member', {'name': 'FB_Test'}, {
        'status': 'denied', 'written': False, 'not_executed': True,
        'authorization_blocked': True, 'denied': '本轮未授权',
    }, False)
    assert evidence.changed == set()
    assert evidence.retryable_validation_gap('') is False
    assert '未执行' in evidence.final_note('')


def test_failed_validation_without_progress_is_reported_once():
    from tc_agent.completion_evidence import CompletionEvidence
    evidence = CompletionEvidence()
    evidence.record('plc_build', {}, {
        'status': 'failed', 'compiler_verified': False,
        'diagnostics_complete': True, 'errorCount': 1,
    }, False)
    assert evidence.retryable_validation_gap('') is False
    assert evidence.final_note('').count('plc_build') == 1
