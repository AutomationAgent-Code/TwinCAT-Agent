"""Regression for the 2026-09-12 FB_PID.CalcPidiOutput diagnostic."""
import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock, patch

import pytest

from tc_template.plc_build_diagnostics import normalize_compiler_severity
from tc_template.xae_build_pipe import normalize_diagnostics
from tc_template._ps_bridge import com_diagnostics, tool_target
from tc_agent.execution_policy import FailurePolicy, tool_succeeded
from tc_agent.tool_recovery import recover_read


def raw_fixture():
    return {
        'ok': True, 'buildPerformed': False, 'failedProjects': 2,
        'errorCount': 0, 'warningCount': 3, 'errors': [],
        'warnings': [
            {'severity': 'warning', 'description': "Project 'Untitled1' build failed.", 'file': '', 'line': 0},
            {'severity': 'warning', 'description': 'New Version found for Visualization Profile', 'file': '', 'line': 0},
            {'severity': 'warning', 'raw_error_level': 2, 'description': 'VAR_TEMP declaration not allowed in this place',
             'project': 'Untitled1.plcproj', 'file': 'FB_PID.TcPOU@CalcPidiOutput (Decl)', 'line': 22},
        ],
        'errorsRead': True, 'diagnosticsAvailable': True,
        'errorSource': 'dte-error-items-ui-thread', 'errorReadAttempts': 5,
        'diagnosticsPending': True,
        'message': 'Build failed, but no compiler error was exposed after 5 UI-thread reads; diagnosticsPending=true.',
    }


def test_real_legacy_shape_recovers_error_and_unblocks_without_build():
    raw = raw_fixture()
    before = deepcopy(raw)
    normalized = normalize_diagnostics(raw)
    assert raw == before
    with tool_target(19584), patch('tc_template.xae_build_pipe.request_diagnostics', return_value=normalized), \
         patch('tc_template._ps_bridge.com_build', side_effect=AssertionError('must not build')):
        result = com_diagnostics()
    assert result['errorCount'] == 1 and result['warningCount'] == 2
    assert result['errors'][0]['line'] == 22 and result['errors'][0]['raw_error_level'] == 2
    assert result['errors'][0]['severity_inferred'] is True
    assert result['diagnostics_complete'] and result['compiler_verified'] is False
    assert result['buildPerformed'] is False and tool_succeeded(result)
    p = FailurePolicy()
    p.record('plc_build', {}, {'buildPerformed': True, 'compiler_verified': False, 'diagnostics_complete': False}, False, False)
    p.record('plc_diagnostics', {}, result, True, True)
    assert p.check('plc_patch', {}) is None
    invoke = AsyncMock(return_value=result)
    asyncio.run(recover_read('plc_diagnostics', invoke, AsyncMock()))
    assert invoke.await_count == 1


@pytest.mark.parametrize('changes', [
    {'failedProjects': 0},
    {'warnings': [{'severity': 'warning', 'description': 'VAR_TEMP declaration not allowed in this place', 'file': '', 'line': 0}]},
])
def test_no_promotion_without_failed_build_and_location(changes):
    assert normalize_compiler_severity({**raw_fixture(), **changes})['errorCount'] == 0


@pytest.mark.parametrize('changes', [{'diagnostics_complete': False}, {'truncated': True}, {'warningCount': 9}])
def test_recognized_error_does_not_override_incomplete_collection(changes):
    result = normalize_compiler_severity({**raw_fixture(), **changes})
    assert result['errorCount'] == 1 and result['diagnosticsPending'] is True


def test_collected_but_incomplete_is_not_reported_as_service_outage():
    invoke = AsyncMock(return_value={'status': 'incomplete', 'diagnosticsAvailable': True, 'diagnostics_complete': False})
    result = asyncio.run(recover_read('plc_diagnostics', invoke, AsyncMock()))
    assert result['recovery_reason'] == 'diagnostic_evidence_incomplete'
    assert result['next_action'].startswith('诊断通道已返回数据')
