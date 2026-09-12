"""Execute the actual process-local C# event hub without loading XAE."""
import os
from pathlib import Path
import subprocess

import pytest


@pytest.mark.skipif(os.name != "nt", reason="Requires Windows .NET Framework compiler")
def test_only_document_context_is_replayed_and_missing_service_reports_failure():
    source = (Path(__file__).resolve().parents[1] / "tc_agent_vsix" / "ShellInteractionService.cs").read_text(encoding="utf-8-sig")
    hub = source[source.index("    internal static class ShellInteractionHub"):source.index("    /// <summary>Publishes active-document")]
    probe = r'''
    public static class HubProbe {
        public static void Run() {
            string received = null;
            ShellInteractionHub.EventPublished += value => received = value;
            ShellInteractionHub.Publish(new { type = "xae_document", path = "MAIN.TcPOU" }, true);
            string document = ShellInteractionHub.LatestEventJson;
            ShellInteractionHub.Publish(new { type = "xae_command", ok = false });
            if (!received.Contains("xae_command") || ShellInteractionHub.LatestEventJson != document)
                throw new Exception("A transient command replaced cached document context.");
            ShellInteractionHub.Publish(new { type = "xae_build", phase = "started" });
            if (ShellInteractionHub.LatestEventJson != document) throw new Exception("Build replaced document.");
            ShellInteractionHub.OpenRequested = null;
            ShellInteractionHub.OpenDirectoryRequested = null;
            ShellInteractionHub.RequestOpen("MAIN.TcPOU", 0);
            if (!received.Contains("\"ok\":false") || !received.Contains("MAIN.TcPOU"))
                throw new Exception("Unavailable service silently dropped open request.");
            if (ShellInteractionHub.LatestEventJson != document) throw new Exception("Request error was replayed.");
        }
    }
'''
    code = "using System; using System.Web.Script.Serialization; namespace TwinCATAgent.Xae {\n" + hub + probe + "\n}"
    command = "$ErrorActionPreference='Stop'; Add-Type -ReferencedAssemblies System.Web.Extensions -TypeDefinition @'\n" + code + "\n'@\n[TwinCATAgent.Xae.HubProbe]::Run(); Write-Output 'HUB_OK'"
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "HUB_OK" in result.stdout
