from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WebViewHostRecoveryTests(unittest.TestCase):
    def test_drag_recovery_uses_only_safe_wpf_host_events(self) -> None:
        xaml = (ROOT / "tc_agent_vsix" / "TwinCATAgentChatControl.xaml").read_text(
            encoding="utf-8-sig"
        )
        source = (ROOT / "tc_agent_vsix" / "TwinCATAgentChatControl.xaml.cs").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn('<wv2:WebView2 x:Name="Web"', xaml)
        self.assertIn('<Grid x:Name="Root">', xaml)
        self.assertNotIn("WebView2CompositionControl", xaml)
        self.assertIn("PresentationSource.AddSourceChangedHandler", source)
        self.assertIn("_hostWindow.LocationChanged", source)
        self.assertIn("_hostWindow.SizeChanged", source)
        self.assertIn("Web.UpdateWindowPos();", source)
        self.assertIn("RecreateWebViewAfterDockMoveAsync", source)
        self.assertIn("old.Dispose();", source)
        self.assertIn("Web = new WebView2", source)
        self.assertNotIn("SetWinEventHook", source)
        self.assertNotIn("NotifyParentWindowPositionChanged", source)
        self.assertNotIn("TryGetController", source)
        self.assertNotIn("Visibility.Hidden", source)
        self.assertNotIn("SyncControllerWithParentWindow", source)
        self.assertNotIn("controller.IsVisible", source)
        restore = source.split("private void RestoreAfterMove", 1)[1].split(
            "private void OnNavigationCompleted", 1
        )[0]
        self.assertNotIn("Reload", restore)

    def test_customer_build_refreshes_the_compiled_extension_dll(self) -> None:
        source = (ROOT / "scripts" / "build_portable.ps1").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn("TwinCATAgent.Xae.csproj", source)
        self.assertIn("tc_agent_vsix\\bin\\Release\\TwinCATAgent.Xae.dll", source)
        self.assertIn("deploy_stage\\TwinCAT Agent", source)

    def test_customer_build_copies_webview_logo_with_fallback_page(self) -> None:
        source = (ROOT / "scripts" / "build_portable.ps1").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn("tc_agent\\static\\index.html", source)
        self.assertIn("tc_agent\\static\\twincat-agent-logo.svg", source)
        self.assertIn("webview\\twincat-agent-logo.svg", source)

    def test_embedded_panel_reports_own_xae_pid(self) -> None:
        host = (ROOT / "tc_agent_vsix" / "TwinCATAgentChatControl.xaml.cs").read_text(
            encoding="utf-8-sig"
        )
        ui = (ROOT / "tc_agent" / "static" / "index.html").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn('"http://127.0.0.1:8766/?xae_pid="', host)
        self.assertIn('get("xae_pid")', ui)
        self.assertIn('type: "refresh_scope", xae_pid: XAE_PID', ui)

    def test_snapshot_directory_uses_verified_xae_host_path(self) -> None:
        host = (ROOT / "tc_agent_vsix" / "TwinCATAgentChatControl.xaml.cs").read_text(
            encoding="utf-8-sig"
        )
        shell = (ROOT / "tc_agent_vsix" / "ShellInteractionService.cs").read_text(
            encoding="utf-8-sig"
        )
        ui = (ROOT / "tc_agent" / "static" / "index.html").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn('xae_open_directory', host)
        self.assertIn('RequestOpenDirectory', shell)
        self.assertIn('OpenSnapshotDirectory', shell)
        self.assertIn('IsAllowedSnapshotDirectory', shell)
        self.assertIn('FileAttributes.ReparsePoint', shell)
        self.assertIn('type:"xae_open_directory"', ui)
        self.assertIn('snapshot_dir', ui)

    def test_conversation_sidebar_renders_valid_active_and_empty_states(self) -> None:
        ui = (ROOT / "tc_agent" / "static" / "index.html").read_text(
            encoding="utf-8-sig"
        )
        backend = (ROOT / "tc_agent" / "backend.py").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn("currentThreads.some(item => item.id === requested)", ui)
        self.assertIn('activeThreadId = selectedExists ? requested', ui)
        self.assertIn('empty.textContent = "暂无对话"', ui)
        self.assertIn('id="threadSidebar"', ui)
        self.assertIn('id="threadList"', ui)
        self.assertIn('className = "thread-item"', ui)
        self.assertIn('className = "thread-pin"', ui)
        self.assertIn('type:"pin_thread"', ui)
        self.assertIn('id="workdirPath"', ui)
        self.assertIn('type:"set_working_directory"', ui)
        self.assertIn('type:"list_working_directories"', ui)
        self.assertIn('id="toolAssign"', ui)
        self.assertIn('type:"get_tool_catalog"', ui)
        self.assertIn('type:"set_tool_categories"', ui)
        self.assertIn('toolCategoryList', ui)
        self.assertIn('case "thread_message_received":', ui)
        self.assertIn('case "thread_event":', ui)
        self.assertIn('case "worker_status":', ui)
        self.assertIn('case "worker_activity":', ui)
        self.assertIn('thread_status=thread.get("status", "idle")', backend)
        self.assertIn('type:"thread_message"', ui)
        self.assertIn('document.createElement("details")', ui)
        self.assertIn('思考过程（点击展开）', ui)
        self.assertIn('let replayingHistory = false;', ui)
        self.assertIn('history-replay', ui)
        self.assertIn('createTranscriptScrollController', ui)
        self.assertIn('scrollController.finishHistory(historyThreadId, historyReason, historyToken)', ui)
        self.assertIn('rememberThreadScroll(renderedHistoryThreadId)', ui)
        self.assertIn('id="workdirOverlay"', ui)
        self.assertIn('role="tree"', ui)
        self.assertIn('role", "treeitem"', ui)
        self.assertIn('id="workdirConfirm"', ui)
        self.assertIn("makeWorkingDirectoryTree", ui)
        self.assertIn("ev.nodes", ui)
        self.assertIn("split(\"^\")", ui)
        self.assertIn("solutionNodeIcon", ui)
        self.assertIn("604", ui)
        self.assertNotIn('prompt("工程中的相对工作目录', ui)
        self.assertIn("function switchThread(threadId)", ui)
        self.assertNotIn('id="threadSel"', ui)
        self.assertIn('projectData = $("projectData")', ui)
        self.assertNotIn('id="newWorker"', ui)
        self.assertNotIn('id="workerBtn"', ui)
        self.assertNotIn('id="collectWorkers"', ui)
        self.assertNotIn('id="resumeWorkers"', ui)

    def test_plc_tools_use_twincat_style_code_review(self) -> None:
        ui = (ROOT / "tc_agent" / "static" / "index.html").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn("function highlightStLine", ui)
        self.assertIn("function makeStViewer", ui)
        self.assertIn("max-height: 126px", ui)
        self.assertIn("function shouldCompactTool", ui)
        self.assertIn("compact-tool collapsed", ui)
        self.assertIn("function makeDiffViewer", ui)
        self.assertIn('name === "plc_write"', ui)
        self.assertIn('name === "plc_patch"', ui)
        self.assertIn('name === "plc_read"', ui)
        self.assertIn('name === "plc_diff"', ui)
        self.assertIn("--st-keyword: #0000ff", ui)
        self.assertIn("st-comment", ui)
        self.assertIn("st-string", ui)

    def test_4024_rot_fallback_uses_window_pid_and_dte_probe(self) -> None:
        source = (ROOT / "tc_template" / "TcCom.ps1").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn("WindowPid", source)
        self.assertIn("TcXaeShell|Visual Studio", source)
        self.assertIn("Where-Object { $_.Solution }", source)
        self.assertIn("StrictPid", source)

    def test_com_bridge_survives_when_add_type_is_blocked(self) -> None:
        source = (ROOT / "tc_template" / "TcCom.ps1").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn("$script:TcRotAvailable", source)
        self.assertIn("Marshal]::GetActiveObject", source)
        self.assertIn("Get-Process -ErrorAction SilentlyContinue", source)
        self.assertIn("try {\n    Add-Type -TypeDefinition", source)
        self.assertIn("'@ -ErrorAction Stop", source)


if __name__ == "__main__":
    unittest.main()
