import base64
import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from tc_agent.execution_policy import tool_succeeded
from tc_template.hmi_diagnostics import read_server_log, enrich_diagnostics, read_hmi_diagnostics


def log_fixture(tmp_path):
    project = tmp_path / 'LineHMI' / 'LineHMI.hmiproj'
    project.parent.mkdir()
    project.touch()
    path = tmp_path / '.engineering_servers' / 'LineHMI' / 'logger.db'
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE event_with_msg_or_alarm_as_payload (id INTEGER, domain TEXT, eventName TEXT, severity INTEGER, timeReceived INTEGER)')
        c.execute('CREATE TABLE msg_or_alarm_parameter (id INTEGER, position INTEGER, name TEXT, data TEXT)')
        c.executemany('INSERT INTO event_with_msg_or_alarm_as_payload VALUES (?,?,?,?,?)', [
            (1, 'ADS', 'OLD_ERROR', 1, 1000 * 10**9),
            (2, 'ADS', 'TWINCAT_RUNTIME_STATUS_CHECK_ERROR', 1, 9900 * 10**9),
            (3, 'TcHmiSrv', 'START_SRV', 1, 9950 * 10**9)])
        c.executemany('INSERT INTO msg_or_alarm_parameter VALUES (?,?,?,?)', [
            (2, 2, '0', 'PLC1'), (2, 4, '1', '0x00000006'),
            (2, 6, '2', 'ERR_TARGETPORTNOTFOUND'), (2, 8, '3', '1.2.3.4.1.1'),
            (2, 10, 'password', 'DO_NOT_EXPOSE')])
    return str(tmp_path / 'A.sln'), str(project), path


def test_saved_log_time_scope_and_known_status_error(tmp_path):
    solution, project, path = log_fixture(tmp_path)
    before = path.read_bytes()
    result = read_server_log(solution, project, now=10000)
    assert result['available'] and result['observed_fault_records'] == 1
    assert len(result['events']) == 2 and not result['live_state_verified']
    fault = result['events'][1]
    assert fault['connection_error']['symbol'] == 'ERR_TARGETPORTNOTFOUND'
    assert fault['age_seconds'] == 100
    assert 'DO_NOT_EXPOSE' not in json.dumps(result)
    assert path.read_bytes() == before


def test_log_truncated_and_empty_are_not_live_passes(tmp_path):
    solution, project, _ = log_fixture(tmp_path)
    limited = read_server_log(solution, project, now=10000, limit=1)
    assert limited['truncated'] and limited['observed_fault_records'] == 0
    empty = read_server_log(solution, project, now=20000)
    assert empty['available'] and empty['events'] == [] and not empty['live_state_verified']


def test_log_identity_and_missing_schema_fail_closed(tmp_path):
    solution, project, path = log_fixture(tmp_path)
    assert not read_server_log('', project)['available']
    assert not read_server_log(solution, str(tmp_path.parent / 'Other.hmiproj'))['available']
    with sqlite3.connect(path) as c:
        c.execute('DROP TABLE event_with_msg_or_alarm_as_payload')
    assert not read_server_log(solution, project)['available']


def test_page_http_success_does_not_prove_plc_or_browser():
    with patch('tc_template._ps_bridge.com_hmi_runtime_info', return_value={
            'application_ready': True, 'server_running': True, 'application_url': 'http://localhost/entry'}) as probe:
        result = enrich_diagnostics({'project': 'A'}, check_page=True)
    assert probe.call_count == 1
    assert result['page']['http_reachable']
    assert not result['page']['verified'] and not result['page']['browser_behavior_verified']
    assert result['plc_communication']['status'] == 'not_checked'
    assert not result['runtime_start_performed'] and not result['plc_write_performed']


def test_read_diagnostics_does_not_build_or_probe_when_disabled():
    with patch('tc_template._ps_bridge.ps_com', return_value={'project': 'A'}) as com:
        with patch('tc_template._ps_bridge.com_hmi_runtime_info') as probe:
            read_hmi_diagnostics('A', check_page=False)
    com.assert_called_once_with('hmi-diagnostics', project='A', timeout=20)
    probe.assert_not_called()


def test_page_probe_failure_is_unavailable():
    with patch('tc_template._ps_bridge.com_hmi_runtime_info', side_effect=RuntimeError('offline')):
        result = enrich_diagnostics({'project': 'A'}, check_page=True)
    assert result['page']['status'] == 'unavailable'


def test_build_enrichment_exposes_independent_acceptance_flags():
    raw = {
        'status': 'succeeded', 'build_succeeded': True, 'diagnostics_pending': False,
        'error_list': {'available': True, 'complete': True, 'entries': []},
    }
    result = enrich_diagnostics(raw, check_page=False)
    assert result['success'] is True and result['status'] == 'succeeded'
    assert result['diagnostics_available'] is True
    assert result['diagnostics_complete'] is True
    assert result['page_verified'] is False
    assert result['runtime_bindings_verified'] is False
    assert result['acceptance']['page']['required_tool'] == 'tc_hmi_browser_validate'


def test_build_enrichment_never_turns_unavailable_error_list_into_zero_errors():
    result = enrich_diagnostics({
        'status': 'succeeded', 'build_succeeded': True, 'diagnostics_pending': False,
        'error_list': {'available': False, 'complete': False, 'entries': []},
    }, check_page=False)
    assert result['status'] == 'incomplete' and result['success'] is False
    assert result['diagnostics_available'] is False and result['diagnostics_complete'] is False


def test_old_extension_reports_actionable_reason_without_rebuilding():
    from tc_template import _ps_bridge as ps
    token = ps._TOOL_TARGET_PID.set(123)
    try:
        with patch('tc_template.xae_build_pipe.request_diagnostics', return_value={
                'ok': False, 'error': 'unsupported request'}) as probe:
            result = enrich_diagnostics({'build_succeeded': True,
                'error_list': {'available': False, 'complete': False}})
        probe.assert_called_once_with(123)
    finally:
        ps._TOOL_TARGET_PID.reset(token)
    assert result['diagnostics_recovery']['code'] == 'extension_update_required'
    assert result['status'] == 'incomplete' and not tool_succeeded(result)


def test_pipe_recovery_exception_is_not_silently_lost():
    from tc_template import _ps_bridge as ps
    token = ps._TOOL_TARGET_PID.set(123)
    try:
        with patch('tc_template.xae_build_pipe.request_diagnostics', side_effect=RuntimeError('private')):
            result = enrich_diagnostics({'build_succeeded': True})
    finally:
        ps._TOOL_TARGET_PID.reset(token)
    assert result['diagnostics_recovery']['error_type'] == 'RuntimeError'
    assert 'private' not in json.dumps(result)
    assert not tool_succeeded(result)


@pytest.mark.parametrize('build', [True, False, None])
def test_warnings_require_review_without_claiming_build_failure(build):
    warning = {'severity': 'warning', 'description': 'Tag mismatch', 'file': 'Desktop.view', 'line': 94}
    result = enrich_diagnostics({'build_succeeded': build, 'diagnostics_pending': False,
        'error_list': {'available': True, 'complete': True, 'entries': [warning]}})
    assert result['build_succeeded'] is build
    assert result['status'] == ('failed' if build is False else 'review_required')
    assert result['warning_count'] == 1 and result['error_count'] == 0
    assert result['warning_review_required'] and not tool_succeeded(result)
    assert result['error_list']['entries'] == [warning]
    assert result['acceptance']['warning_review']['status'] == 'pending'


def test_truncated_warnings_do_not_hide_incomplete_build_diagnostics():
    result = enrich_diagnostics({'build_succeeded': True,
        'error_list': {'available': True, 'complete': False, 'entries': [{'severity': 'warning'}]}})
    assert result['status'] == 'incomplete'
    assert result['warning_review_required'] and not result['success']


@pytest.mark.parametrize('result', [
    {'diagnostics_pending': True}, {'error_count': 2}, {'failed_projects': 1},
    {'failed_projects': None}, {'status': 'incomplete', 'build_succeeded': True}])
def test_snake_case_failures_are_not_success(result):
    assert not tool_succeeded(result)


@pytest.mark.parametrize('mode,expected,build_success', [
    ('clean', 'succeeded', True), ('missing', 'incomplete', True),
    ('errors', 'incomplete', True), ('truncated', 'incomplete', True),
    ('failed', 'failed', False), ('unknown', 'incomplete', None),
    ('partial', 'incomplete', True)])
def test_real_powershell_build_contract(tmp_path, mode, expected, build_success):
    if not shutil.which('powershell'):
        pytest.skip('Windows PowerShell required')
    script = r'''
$ErrorActionPreference = 'Stop'
$tokens=$null; $errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($env:HMI_SCRIPT,[ref]$tokens,[ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
foreach ($d in $ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst]},$true)) {
 if ($d.Name -in @('Invoke-TcHmiBuild','Get-TcHmiDiagnostics')) { . ([scriptblock]::Create($d.Extent.Text)) }
}
function Get-TcHmiProjectObject { param($Dte,$ProjectName) [pscustomobject]@{Name='HMI'} }
function Get-TcHmiResolvedName { param($Project) 'HMI' }
function Get-TcHmiResolvedFullName { param($Project) 'C:\Fixture\HMI\HMI.hmiproj' }
function Get-TcHmiProjectPath { param($Project) 'C:\Fixture\HMI' }
function Get-TcHmiResolvedUniqueName { param($Project) 'HMI\HMI.hmiproj' }
$build=[pscustomobject]@{LastBuildInfo=0; ActiveConfiguration=[pscustomobject]@{Name='Release'}}
$script:buildCalls=0
$build | Add-Member ScriptMethod BuildProject { param($c,$p,$wait) $script:buildCalls++ }
if ($env:HMI_MODE -eq 'unknown') { $build.LastBuildInfo=$null }
if ($env:HMI_MODE -eq 'failed') { $build.LastBuildInfo=1 }
$items=[pscustomobject]@{Count=0}
if ($env:HMI_MODE -in @('errors','partial')) { $items.Count=2 }
if ($env:HMI_MODE -eq 'truncated') { $items.Count=201 }
$items | Add-Member ScriptMethod Item {
 param($i)
 if ($env:HMI_MODE -eq 'partial' -and $i -eq 2) { throw 'snapshot failed' }
 [pscustomobject]@{ErrorLevel=4;Description='fixture error';Project='HMI';FileName='Desktop.view';Line=1}
}
$dte=[pscustomobject]@{Solution=[pscustomobject]@{SolutionBuild=$build;FullName='C:\Fixture\A.sln'};
 ToolWindows=[pscustomobject]@{ErrorList=[pscustomobject]@{ErrorItems=$items}}}
if ($env:HMI_MODE -eq 'missing') { $dte.ToolWindows=$null }
$result=Invoke-TcHmiBuild $dte 'HMI'
@{result=$result;build_calls=$script:buildCalls} | ConvertTo-Json -Depth 9 -Compress
'''
    env = dict(os.environ, HMI_SCRIPT=str(Path(__file__).resolve().parents[1] / 'tc_template/TcCom.ps1'), HMI_MODE=mode)
    proc = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-EncodedCommand',
        base64.b64encode(script.encode('utf-16-le')).decode()], env=env, capture_output=True, timeout=20)
    assert proc.returncode == 0, proc.stderr.decode(errors='replace')
    payload = json.loads(proc.stdout)
    result = payload['result']
    assert payload['build_calls'] == 1
    assert result['status'] == expected and result['build_succeeded'] is build_success
    assert result['success'] is (expected == 'succeeded')
    assert result['diagnostics_pending'] is (expected != 'succeeded')
    assert not result['page_verified'] and not result['plc_communication_verified']
    if mode in ('missing', 'partial'):
        assert not result['error_list']['available']
        assert result['error_list']['read_error']
