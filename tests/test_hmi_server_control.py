from __future__ import annotations

from pathlib import Path


def test_server_control_uses_xae_reload_and_exact_project_process_scope() -> None:
    source = (Path(__file__).resolve().parents[1] / "tc_template" /
              "TcHmiServer.ps1").read_text(encoding="utf-8")
    assert "Get-TcHmiRuntimeInfo $Dte $plan.project_file" in source
    assert "Reload-TcHmiProject $Dte $project $plan.project_file $true" in source
    assert "Start-Process -FilePath $plan.executable" not in source
    assert "storageDir" in source
    assert "Stop-Process -Id $processId" in source


def test_server_start_is_hidden_from_build_and_publish_workflows() -> None:
    source = (Path(__file__).resolve().parents[1] / "tc_template" /
              "TcHmiServer.ps1").read_text(encoding="utf-8")
    assert "build_performed=$false" in source
    assert "publish_performed=$false" in source
    assert "start_strategy='xae-project-reload'" in source


def test_runtime_probe_never_contacts_saved_or_force_auth_endpoints() -> None:
    source = (Path(__file__).resolve().parents[1] / "tc_template" /
              "TcCom.ps1").read_text(encoding="utf-8")
    start = source.index("function Get-TcHmiRuntimeInfo")
    end = source.index("function Invoke-TcHmiCdpCommand", start)
    runtime_source = source[start:end]
    assert "$probeEndpoints = @($processEndpoints.ToArray()" in runtime_source
    assert "foreach ($endpoint in $probeEndpoints)" in runtime_source
    assert "probe_endpoints=$probeEndpoints" in runtime_source
    assert "+ $configuredEndpoints" not in runtime_source
    assert "force-auth endpoint" in runtime_source
    assert "HTTP 460 License Expired" in runtime_source
    assert "status=if($applicationUrl){'ready'}elseif($licenseExpired){'license-expired'}" in runtime_source
