import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from tc_agent.execution_policy import FailurePolicy
from tc_agent.tool_recovery import recover_read


def pending(policy):
    policy.record('plc_build', {}, {'compiler_verified': False, 'buildPerformed': True,
                                   'diagnostics_complete': False}, False, True)


def test_recovered_diagnostics_unblocks_identical_write():
    p = FailurePolicy()
    pending(p)
    args = {'path': 'TIPC^PLC1^POUs^MAIN', 'code': 'x := 1;'}
    blocked = p.check('plc_write', args)
    p.record('plc_write', args, blocked, False, False)
    p.record('plc_diagnostics', {}, {'diagnostics_complete': True, 'errorCount': 1}, False, True)
    assert p.check('plc_write', args) is None


def test_scope_change_drops_old_diagnostics_without_claiming_success():
    p = FailurePolicy()
    p.bind(1, 'A.sln')
    pending(p)
    p.bind(1, 'A.sln')
    assert p.check('plc_write', {})
    p.bind(2, 'B.sln')
    assert p.check('plc_write', {}) is None


def test_import_and_snapshot_cannot_bypass_pending_source_gate():
    p = FailurePolicy()
    pending(p)
    for name in ('plc_import_plcopen', 'plc_restore_snapshot', 'fblib_add'):
        assert p.check(name, {'apply': True})
    assert p.check('plc_restore_snapshot', {'apply': False}) is None
    assert p.check('tc_hmi_control_edit', {}) is None


def test_temporary_conflict_not_cached_but_uncertain_write_is():
    p = FailurePolicy()
    p.record('plc_patch', {}, {'status': 'conflict', 'written': False, 'retry_safe': False}, False, False)
    assert p.check('plc_patch', {}) is None  # actual handler must recheck freshness
    p.record('plc_patch', {}, {'status': 'uncertain', 'written': True, 'verified': False}, False, False)
    assert p.check('plc_patch', {})


def test_read_recovery_hides_intermediate_errors_but_keeps_history():
    failed = {'status': 'incomplete', 'diagnostics_complete': False}
    invoke = AsyncMock(side_effect=[failed, {'status': 'diagnostics_only', 'diagnostics_complete': True}])
    progress = AsyncMock()
    result = asyncio.run(recover_read('plc_diagnostics', invoke, progress))
    assert invoke.await_count == 2 and progress.await_count == 1
    assert result['recovery']['recovered']
    assert result['recovery']['history'] == [failed]


def test_read_recovery_exhausts_at_two_retries():
    invoke = AsyncMock(return_value={'status': 'incomplete', 'diagnostics_complete': False})
    progress = AsyncMock()
    result = asyncio.run(recover_read('plc_diagnostics', invoke, progress))
    assert invoke.await_count == 3 and progress.await_count == 2
    assert result['recovery_exhausted'] and not result['recovery']['recovered']


def test_never_retries_writes_builds_approval_or_uncertain():
    for name, result in [
        ('plc_patch', {'status': 'unavailable'}),
        ('plc_build', {'status': 'incomplete'}),
        ('plc_diagnostics', {'status': 'uncertain', 'diagnostics_complete': False}),
        ('plc_diagnostics', {'denied': 'user', 'diagnostics_complete': False}),
        ('plc_build_status', {'status': 'approval_expired'}),
    ]:
        invoke, progress = AsyncMock(return_value=result), AsyncMock()
        assert asyncio.run(recover_read(name, invoke, progress)) == result
        assert invoke.await_count == 1 and progress.await_count == 0


def test_readonly_dependencies_exposed_without_extra_writes():
    from tc_agent.agent_core import tools_schema, is_readonly
    schemas = tools_schema(allowed_names={'plc_patch'})
    names = {t['name'] for t in schemas}
    assert {'plc_read', 'plc_diagnostics', 'plc_build_status'} <= names
    assert all(is_readonly(n) for n in names - {'plc_patch'})


def test_pipe_missing_uses_exact_pid_readonly_native_fallback():
    from tc_template._ps_bridge import com_diagnostics, tool_target
    raw = {'ok': True, 'diagnosticsAvailable': True, 'errors': [], 'errorCount': 0, 'failedProjects': 0}
    with tool_target(42), patch('tc_template.xae_build_pipe.request_diagnostics', return_value=None), \
         patch('tc_template._ps_bridge._native_request', return_value=raw) as native:
        result = com_diagnostics()
    assert result['diagnostics_complete'] and result['compiler_verified'] is False
    native.assert_called_once_with('com', 'diagnostics', {'preferPid': 42, 'strictPid': True}, 15.0)


def test_native_empty_error_list_is_not_failed_collection():
    from tc_template._native_bridge import _diagnostics
    dte = NS(Solution=NS(FullName='A.sln', SolutionBuild=NS(BuildState=3, LastBuildInfo=0)),
             ToolWindows=NS(ErrorList=NS(ErrorItems=NS(Count=0))))
    result = _diagnostics(dte)
    assert result['diagnosticsAvailable'] and not result['diagnosticsPending']
    assert result['buildPerformed'] is False
    dte.ToolWindows = None
    assert _diagnostics(dte)['diagnosticsAvailable'] is False


def test_build_recovers_diagnostics_without_rebuilding_or_claiming_clean():
    from tc_agent.tool_recovery import recover_tool
    from tc_agent.execution_policy import tool_succeeded
    build = {'status': 'incomplete', 'buildPerformed': True,
             'diagnostics_complete': False, 'compiler_verified': False}
    diagnostic = {'status': 'diagnostics_only', 'diagnostics_complete': True,
                  'compiler_verified': False, 'failedProjects': 1, 'errorCount': 1,
                  'errors': [{'file': 'MAIN.TcPOU', 'line': 8}], 'next_action': 'Read exact location.'}
    invoke, diagnose, progress = AsyncMock(return_value=build), AsyncMock(return_value=diagnostic), AsyncMock()
    result = asyncio.run(recover_tool('plc_build', invoke, diagnose, progress))
    assert invoke.await_count == diagnose.await_count == 1
    assert result['recovery']['recovered'] and result['compiler_verified'] is False
    assert not tool_succeeded(result) and tool_succeeded(diagnostic)
    p = FailurePolicy()
    p.record('plc_build', {}, result, False, False)
    p.record('plc_diagnostics', {}, result['diagnostic_recovery'], True, True)
    assert not p.check('plc_patch', {})


def test_unexecuted_or_uncertain_build_never_starts_recovery():
    from tc_agent.tool_recovery import recover_tool
    for result in ({'buildPerformed': False, 'diagnostics_complete': False},
                   {'buildPerformed': None, 'status': 'uncertain', 'diagnostics_complete': False}):
        diagnose = AsyncMock()
        assert asyncio.run(recover_tool('plc_build', AsyncMock(return_value=result), diagnose, AsyncMock())) == result
        diagnose.assert_not_called()


def test_foreign_solution_or_project_diagnostics_cannot_clear_solution_gate():
    p = FailurePolicy()
    p.bind(42, 'A.sln')
    pending(p)
    for fields in ({'solution': 'B.sln'}, {'xaePid': 43}, {'diagnostic_scope': 'project'}):
        p.record('plc_diagnostics', {}, {'diagnostics_complete': True, **fields}, True, True)
        assert p.plc_diagnostics_pending


def test_external_source_progress_must_be_observed_not_assumed():
    p = FailurePolicy()
    args = {'path': 'TIPC^PLC1^POUs^MAIN'}
    p.record('plc_read', args, {'path': args['path'], 'source_hashes': {'implementation': 'before'}}, True, True)
    p.record('plc_build', {}, {'compiler_verified': False, 'diagnostics_complete': True}, False, False)
    assert p.check('plc_build', {})
    p.record('plc_read', args, {'path': args['path'], 'source_hashes': {'implementation': 'before'}}, True, True)
    assert p.check('plc_build', {})
    p.record('plc_read', args, {'path': args['path'], 'source_hashes': {'implementation': 'after'}}, True, True)
    assert not p.check('plc_build', {})


def test_recovery_details_survive_ui_but_not_repeat_in_model_context():
    from tc_agent.agent_core import tool_result_for_context
    raw = {'status': 'diagnostics_only', 'diagnostics_complete': True,
           'recovery': {'recovered': True, 'history': [{'error': 'transient busy'}]}}
    context = tool_result_for_context('plc_diagnostics', {}, raw)
    assert 'history' not in context['recovery']
    assert raw['recovery']['history']
