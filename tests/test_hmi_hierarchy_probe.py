"""Read-only bridge contract tests; no XAE, PLC or installed-extension mutation."""
import json
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(os.name != "nt", reason="Windows local paths / installed PowerShell")
def test_real_csharp_probe_contract(tmp_path):
    solution = tmp_path / "Test.sln"
    project = tmp_path / "Hmi" / "Demo.hmiproj"
    item = project.parent / "Pages" / "Test.content"
    item.parent.mkdir(parents=True)
    solution.write_text("")
    project.write_text("<Project/>")
    item.write_text("<div/>")
    empty = project.parent / "Empty"
    empty.mkdir()
    protected = project.parent / "Properties" / "Bad.content"
    protected.parent.mkdir()
    protected.write_text("<div/>")
    unsupported = project.parent / "Test.json"
    unsupported.write_text("{}")
    base = dict(command="probe-delete", solution_file=str(solution), project_file=str(project), item_file=str(item))
    cases = [(base, True), (dict(base, item_file=str(empty)), True)]
    for change in (
        {"apply": True}, {"command": "delete"}, {"command": "build"}, {"item_file": 12},
        {"item_file": str(project.parent)}, {"item_file": str(item.parent)},
        {"item_file": str(protected)}, {"item_file": str(unsupported)},
        {"item_file": str(tmp_path / "Outside.content")},
        {"item_file": str(project.parent / "Missing.content")},
        {"item_file": str(item.parent / ".." / "Pages" / "Test.content")},
        {"item_file": str(item) + ":stream"}, {"item_file": "C:Test.content"},
        {"item_file": r"\\localhost\c$\Test.content"},
        {"item_file": str(item) + " "}, {"solution_file": str(project)},
        {"project_file": str(solution)}, {"item_file": "x" * 9000},
    ):
        cases.append((dict(base, **change), False))
    cases.append(({k: v for k, v in base.items() if k != "command"}, False))
    cases_path = tmp_path / "cases.json"
    cases_path.write_text(json.dumps([{"request": json.dumps(r), "expected": e} for r, e in cases]))
    command = r'''
$ErrorActionPreference='Stop'
$source=[IO.File]::ReadAllText($env:HMI_CONTRACT)
$harness=@'
namespace TwinCATAgent.Xae {
 public static class HmiProbeContractTestHarness {
  public static bool Accept(string json) {
   try { HmiHierarchyProbeContract.Parse(json); return true; }
   catch (System.Exception) { return false; }
  }
 }
}
'@
Add-Type -TypeDefinition ($source + $harness) -ReferencedAssemblies System.Web.Extensions,System.Core
$cases=[IO.File]::ReadAllText($env:HMI_CASES) | ConvertFrom-Json
$index=0
foreach($case in $cases) {
 $actual=[TwinCATAgent.Xae.HmiProbeContractTestHarness]::Accept($case.request)
 if($actual -ne $case.expected) { throw "Contract case $index returned $actual" }
 $index++
}
Write-Output $index
'''
    result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        env=dict(os.environ, HMI_CONTRACT=str(ROOT / "tc_agent_vsix/HmiHierarchyProbeContract.cs"), HMI_CASES=str(cases_path)),
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert int(result.stdout.strip()) == len(cases)
    assert item.read_text() == "<div/>"


def test_probe_has_no_write_or_reload_route():
    source = (ROOT / "tc_agent_vsix/XaeHmiHierarchyPipeService.cs").read_text(encoding="utf-8-sig")
    assert ".QueryDeleteItems(" in source
    for forbidden in (".DeleteItems(", ".DeleteChild(", ".ExecuteCommand(", ".Save(", ".AddFromFile(", ".Remove("):
        assert forbidden not in source
    for required in ("EPF_LOADEDINSOLUTION", "GetMkDocument", "ParseCanonicalName", "GetCanonicalName",
                     "SwitchToMainThreadAsync", "execution_enabled = false", "verified = false",
                     "SetAccessRuleProtection(true, false)", "MaxRequestChars", "deadline.CancelAfter"):
        assert required in source
    create = (ROOT / "tc_template/TcHmiItems.ps1").read_text(encoding="utf-8-sig")
    assert "ResolveDeleteId" not in create
    assert "DeleteComplete" not in create


def test_probe_is_separate_from_build_and_included_in_package():
    package = (ROOT / "tc_agent_vsix/TwinCATAgentPackage.cs").read_text(encoding="utf-8-sig")
    project = (ROOT / "tc_agent_vsix/TwinCATAgent.Xae.csproj").read_text(encoding="utf-8-sig")
    assert "new XaeHmiHierarchyPipeService(this).RunAsync(this.DisposalToken)" in package
    assert 'Compile Include="XaeHmiHierarchyPipeService.cs"' in project
    assert 'Compile Include="HmiHierarchyProbeContract.cs"' in project
    assert "HmiHierarchy" not in (ROOT / "tc_agent_vsix/XaeBuildPipeService.cs").read_text(encoding="utf-8-sig")
    script = (ROOT / "scripts/Test-HmiDeleteHandler.ps1").read_text(encoding="utf-8-sig")
    assert "[switch]$Apply" not in script
    assert "bridge_unavailable" in script
    assert "36232" not in script
