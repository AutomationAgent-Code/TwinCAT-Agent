"""Run the real PowerShell creation state machine with isolated DTE doubles."""
import base64
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tc_agent import agent_core as ac
from tc_agent.execution_policy import tool_succeeded


ROOT = Path(__file__).resolve().parents[1]


def run_creation(tmp_path, mode, extra="", apply=True):
    if not shutil.which("powershell"):
        pytest.skip("Windows PowerShell required for actual script regression")
    script = r'''
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($env:HMI_SCRIPT, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
$names = @('Test-TcHmiComBusy','Get-TcHmiCreationState','New-TcHmiProject','Get-TcHmiFrameworkTemplates')
foreach ($definition in $ast.FindAll({ param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] }, $true)) {
    if ($definition.Name -in $names) { . ([scriptblock]::Create($definition.Extent.Text)) }
}
function Start-Sleep { param($Milliseconds) }
$script:LiveProjects = @(); $script:AddCount = 0; $script:SaveCount = 0
$script:Mode = $env:HMI_MODE
$script:SolutionFile = Join-Path $env:HMI_FIXTURE 'Machine.sln'
$script:ProjectFile = Join-Path $env:HMI_FIXTURE 'HMI1/HMI1.hmiproj'
$template = Join-Path $env:HMI_FIXTURE 'Template.vstemplate'
[IO.File]::WriteAllText($script:SolutionFile, '')
[IO.File]::WriteAllText($template, '<VSTemplate />')
function Get-TcHmiProjectObjects {
    param($Dte)
    if ($script:Mode -eq 'anonymous' -and [IO.File]::ReadAllText($script:SolutionFile) -notmatch 'HMI1') { return @() }
    $script:LiveProjects
}
function Get-TcHmiFrameworkTemplateRoot { $env:HMI_FIXTURE }
function Get-TcHmiProjectObject { throw 'Must not resolve a nonexistent project' }
function Make-Project {
    [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($script:ProjectFile))
    [IO.File]::WriteAllText($script:ProjectFile, '<Project />')
    $script:LiveProjects = @([pscustomobject]@{TcHmiResolvedFullName=$script:ProjectFile})
}
$solution = [pscustomobject]@{FullName=$script:SolutionFile}
$solution | Add-Member ScriptMethod AddFromTemplate {
    param($Template,$Destination,$Name,$Exclusive)
    $script:AddCount++
    if ($script:Mode -eq 'create_error') { throw 'Template wizard failed' }
    Make-Project
    if ($script:Mode -eq 'create_busy') { throw (New-Object Runtime.InteropServices.COMException('busy', -2147418111)) }
    if ($script:Mode -eq 'solution_changed') { $this.FullName = Join-Path $env:HMI_FIXTURE 'Other.sln' }
}
$solution | Add-Member ScriptMethod SaveAs {
    param($Path)
    $script:SaveCount++
    if ($script:Mode -eq 'save_busy' -and $script:SaveCount -lt 3 -or $script:Mode -eq 'save_timeout') {
        throw (New-Object Runtime.InteropServices.COMException('busy', -2147418111))
    }
    if ($script:Mode -eq 'save_error') { throw 'Access denied' }
    if ($script:Mode -eq 'save_no_effect') { return }
    [IO.File]::WriteAllText($Path, ('Project("{type}") = "HMI1", "HMI1\HMI1.hmiproj", "{id}"' + "`r`nEndProject"))
}
$dte = [pscustomobject]@{Solution=$solution}
if ($script:Mode -in @('existing','existing_preview','orphan','different_identity')) {
    Make-Project
    if ($script:Mode -eq 'orphan') { $script:LiveProjects = @() }
    if ($script:Mode -eq 'different_identity') {
        $script:LiveProjects = @([pscustomobject]@{TcHmiResolvedFullName=(Join-Path $env:HMI_FIXTURE 'Other/HMI1.hmiproj')})
    }
}
if ($script:Mode -eq 'empty_folder') { [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($script:ProjectFile)) }
$apply = $env:HMI_APPLY -eq 'true'
if ($script:Mode -eq 'templates') { $result = Get-TcHmiFrameworkTemplates $dte }
else { $result = New-TcHmiProject $dte 'HMI1' -Template $template -Apply $apply -WaitSeconds 0 -SaveAttempts 3 }
''' + extra + r'''
@{result=$result; creates=$script:AddCount; saves=$script:SaveCount; exists=[IO.File]::Exists($script:ProjectFile)} | ConvertTo-Json -Depth 12 -Compress
'''
    env = dict(os.environ, HMI_SCRIPT=str(ROOT / "tc_template/TcCom.ps1"),
               HMI_FIXTURE=str(tmp_path), HMI_MODE=mode, HMI_APPLY=str(apply).lower())
    completed = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand",
        base64.b64encode(script.encode("utf-16-le")).decode()], env=env,
        capture_output=True, encoding="utf-8", timeout=25)
    assert completed.returncode == 0, completed.stderr + completed.stdout
    return json.loads(completed.stdout)


@pytest.mark.parametrize("mode,saves", [("success", 1), ("create_busy", 1), ("save_busy", 3), ("anonymous", 1)])
def test_partial_creation_and_busy_save_reconcile_without_duplicate_create(tmp_path, mode, saves):
    outcome = run_creation(tmp_path, mode)
    assert outcome["creates"] == 1
    assert outcome["saves"] == saves
    assert outcome["result"]["ok"]
    assert outcome["result"]["verified_in_xae"]
    assert outcome["result"]["persisted_in_solution"]
    if mode == "create_busy":
        assert outcome["result"]["diagnostics"][0]["phase"] == "add_from_template"


def test_loaded_existing_project_only_resumes_registration(tmp_path):
    outcome = run_creation(tmp_path, "existing")
    assert outcome["result"]["status"] == "recovered"
    assert outcome["creates"] == 0
    assert outcome["saves"] == 1


@pytest.mark.parametrize("mode", ["success", "existing_preview"])
def test_preview_does_not_create_or_save(tmp_path, mode):
    outcome = run_creation(tmp_path, mode, apply=False)
    assert outcome["result"]["status"] == "preview"
    assert outcome["creates"] == outcome["saves"] == 0


@pytest.mark.parametrize("mode", ["orphan", "empty_folder", "different_identity"])
def test_unloaded_or_mismatched_output_is_preserved_without_recreate(tmp_path, mode):
    outcome = run_creation(tmp_path, mode)
    assert outcome["result"]["phase"] == "inspect_existing"
    assert outcome["result"]["files_preserved"]
    assert not outcome["result"]["retry_safe"]
    assert outcome["creates"] == outcome["saves"] == 0


@pytest.mark.parametrize("mode,saves", [("save_error", 1), ("save_timeout", 3), ("save_no_effect", 3)])
def test_unverified_save_never_reports_created(tmp_path, mode, saves):
    outcome = run_creation(tmp_path, mode)
    assert outcome["result"]["status"] == "incomplete"
    assert outcome["result"]["phase"] == "save_solution"
    assert not outcome["result"]["ok"]
    assert outcome["exists"]
    assert outcome["saves"] == saves


def test_second_request_recovers_busy_save_without_rerunning_wizard(tmp_path):
    outcome = run_creation(tmp_path, "save_timeout", extra=r'''
if ($result.phase -ne 'save_solution') { throw 'Expected incomplete save' }
$script:Mode = 'success'
$result = New-TcHmiProject $dte 'HMI1' -Template $template -Apply $true -WaitSeconds 0 -SaveAttempts 3
''')
    assert outcome["result"]["status"] == "recovered"
    assert outcome["creates"] == 1
    assert outcome["saves"] == 4


def test_failed_wizard_returns_stage_and_preserves_output(tmp_path):
    outcome = run_creation(tmp_path, "create_error")
    assert outcome["result"]["phase"] == "wait_for_project"
    assert outcome["result"]["diagnostics"][0]["phase"] == "add_from_template"
    assert outcome["creates"] == 1
    assert outcome["saves"] == 0


def test_framework_template_discovery_works_without_hmi_project(tmp_path):
    outcome = run_creation(tmp_path, "templates")
    assert outcome["result"]["status"] == "ok"
    assert outcome["result"]["project"] == ""
    assert len(outcome["result"]["templates"]) == 3


@pytest.mark.parametrize("result", [
    {"error": "No TwinCAT HMI .hmiproj project was found in the open solution."},
    {"status": "incomplete", "ok": False, "phase": "save_solution", "error": "busy", "files_preserved": True},
    {"isError": True, "content": ["failed"]},
])
def test_hmi_failures_survive_tool_context_and_history_compaction(result):
    assert ac._hmi_read_result_for_context({}, result) == result
    compacted, _ = ac.compact_messages([{"role": "tool", "id": "fixture", "name": "tc_hmi_read", "result": result}])
    assert compacted[0]["result"] == result
    assert not tool_succeeded(compacted[0]["result"])


def test_large_failure_keeps_error_phase_and_failure_flag():
    result = {"error": "RPC rejected", "status": "incomplete", "phase": "save_solution", "content": "x" * 50000}
    compacted = ac._hmi_read_result_for_context({}, result)
    assert compacted["error"] == result["error"]
    assert compacted["phase"] == result["phase"]
    assert not tool_succeeded(compacted)
    assert "controls" not in compacted
