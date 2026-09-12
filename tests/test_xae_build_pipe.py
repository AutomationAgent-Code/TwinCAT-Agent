from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from tc_template import _ps_bridge as bridge
from tc_template.xae_build_pipe import pipe_name


ROOT = Path(__file__).resolve().parents[1]


class XaeBuildPipeTests(unittest.TestCase):
    def setUp(self):
        # Platform preflight has its own tests; these exercise IPC transport.
        guard = patch('tc_template.build_platform_contract.preflight', return_value=None)
        guard.start()
        self.addCleanup(guard.stop)
        assessment = patch('tc_template.build_platform_contract.assess', return_value={
            'allowed': True, 'target_compatibility_verified': True,
            'target_compatible': True, 'local_build_context_verified': True})
        assessment.start()
        self.addCleanup(assessment.stop)

    def test_pipe_is_scoped_to_the_xae_process(self) -> None:
        self.assertEqual(pipe_name(4567), r"\\.\pipe\TwinCAT-Agent-Build-4567")
        client = (ROOT / "tc_template" / "xae_build_pipe.py").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn("_ERROR_BROKEN_PIPE", client)
        self.assertIn("Named pipes in byte mode", client)
        self.assertIn("_MAX_RESPONSE_BYTES = 8 * 1024 * 1024", client)
        self.assertIn('sent = True', client)
        self.assertIn('"uncertain": True', client)
        self.assertIn('command == "rebuild"', client)

    def test_bound_agent_build_prefers_ui_thread_pipe(self) -> None:
        result = {"ok": True, "failedProjects": 0, "errors": []}
        with patch("tc_template.xae_build_pipe.request_build", return_value=result), \
             patch.object(bridge, "ps_com") as fallback:
            with bridge.tool_target(4567):
                actual = bridge.com_build()
        self.assertEqual(actual["execution"], "xae-ui-thread")
        self.assertEqual(actual["xaePid"], 4567)
        fallback.assert_not_called()

    def test_bound_agent_build_uses_ui_thread_pipe_when_diagnostics_requested(self) -> None:
        result = {"ok": True, "failedProjects": 1, "errorCount": 3, "errors": [{}, {}, {}]}
        with patch("tc_template.xae_build_pipe.request_build", return_value=result), \
             patch.object(bridge, "ps_com") as fallback:
            with bridge.tool_target(4567):
                actual = bridge.com_build(always_read_errors=True)
        self.assertEqual(3, actual["errorCount"])
        self.assertEqual("xae-ui-thread", actual["execution"])
        fallback.assert_not_called()

    def test_unavailable_pipe_falls_back_to_existing_com_build(self) -> None:
        expected = {"failedProjects": 0, "errors": []}
        with patch("tc_template.xae_build_pipe.request_build", return_value=None), \
             patch.object(bridge, "ps_com", return_value=expected) as fallback:
            with bridge.tool_target(4567):
                actual = bridge.com_build()
        self.assertIs(actual, expected)
        fallback.assert_called_once_with("build", always_read_errors=False)

    def test_ui_thread_build_failure_is_not_treated_as_success(self) -> None:
        with patch("tc_template.xae_build_pipe.request_build",
                   return_value={"ok": False, "error": "XAE build exception"}), \
             patch.object(bridge, "ps_com") as fallback:
            with bridge.tool_target(4567):
                actual = bridge.com_build()
        self.assertEqual(1, actual["failedProjects"])
        self.assertTrue(actual["diagnosticsPending"])
        self.assertIn("XAE build exception", actual["message"])
        fallback.assert_not_called()

    def test_uncertain_ui_thread_result_never_falls_back_to_second_build(self) -> None:
        with patch("tc_template.xae_build_pipe.request_build", return_value={
                "ok": False, "uncertain": True, "error": "pipe closed"}), \
             patch.object(bridge, "ps_com") as fallback:
            with bridge.tool_target(4567):
                actual = bridge.com_build()
        self.assertEqual("uncertain", actual["status"])
        self.assertIsNone(actual["buildPerformed"])
        fallback.assert_not_called()

    def test_powershell_error_array_wrapper_is_flattened(self) -> None:
        wrapped = {
            "failedProjects": 1,
            "errorCount": 1,
            "errors": [{"value": [
                {"code": "C0018", "description": "bad target"},
                {"code": "C0077", "description": "unknown type"},
            ], "Count": 2}],
            "warnings": [],
        }
        with patch.object(bridge, "ps_com", return_value=wrapped):
            actual = bridge.com_build(always_read_errors=True)
        self.assertEqual(2, actual["errorCount"])
        self.assertEqual(["C0018", "C0077"], [item["code"] for item in actual["errors"]])
        self.assertEqual("diagnostic-list", actual["errorCountSource"])

    def test_vsix_service_marshals_dte_build_to_the_ui_thread(self) -> None:
        source = (ROOT / "tc_agent_vsix" / "XaeBuildPipeService.cs").read_text(
            encoding="utf-8-sig"
        )
        package = (ROOT / "tc_agent_vsix" / "TwinCATAgentPackage.cs").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn("NamedPipeServerStream", source)
        self.assertIn("TwinCAT-Agent-Build-", source)
        self.assertIn("SwitchToMainThreadAsync", source)
        self.assertIn("build.Build(true);", source)
        self.assertIn("dte-error-items-ui-thread", source)
        self.assertIn("TcXaeShell 15 sometimes reports PLC compiler errors", source)
        self.assertIn("diagnosticsPending", source)
        self.assertIn("errorReadAttempts", source)
        self.assertNotIn("Activate()", source)
        self.assertNotIn("View.ErrorList", source)
        self.assertNotIn("SetForegroundWindow", source)
        self.assertNotIn("keybd_event", source)
        self.assertIn("new XaeBuildPipeService(this).RunAsync", package)

    def test_readonly_diagnostics_does_not_require_previous_build(self) -> None:
        source = (ROOT / "tc_agent_vsix" / "XaeBuildPipeService.cs").read_text(encoding="utf-8-sig")
        self.assertIn("int? failedProjects = null;", source)
        self.assertIn("catch when (!executeBuild)", source)
        self.assertIn("return ReadDiagnostics(dte, failedProjects);", source)
        self.assertNotIn("ReadDiagnostics(dte, build.LastBuildInfo)", source)
        self.assertIn('failedProjects.Value.ToString() : "null"', source)

    def test_powershell_build_reads_error_list_only_after_failure(self) -> None:
        source = (ROOT / "tc_template" / "TcCom.ps1").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn("$readOnFailure = ($failed -gt 0)", source)
        self.assertIn("$Dte.ToolWindows.ErrorList.Activate()", source)
        self.assertIn("clipboard-error-list-failure-fallback", source)
        start = source.index("function Read-ErrorList")
        end = source.index("function Invoke-TcBuild", start)
        read_error_list = source[start:end]
        self.assertNotRegex(
            read_error_list,
            r"(?m)^\s*return\s+,\$items\s*$",
        )


if __name__ == "__main__":
    unittest.main()
