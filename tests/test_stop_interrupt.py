from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class StopInterruptRegressionTests(unittest.TestCase):
    def test_permission_gate_propagates_cancellation(self):
        source = (ROOT / "tc_agent" / "backend.py").read_text(encoding="utf-8")
        block = source[source.index("async def approve"):source.index("async def stream_complete")]
        self.assertIn("except asyncio.CancelledError:", block)
        self.assertIn("raise", block)
        self.assertNotIn("except asyncio.CancelledError:\n            return False", block)

    def test_interrupt_is_acknowledged_before_cleanup(self):
        source = (ROOT / "tc_agent" / "backend.py").read_text(encoding="utf-8")
        block = source[source.index('elif kind == "interrupt"'):source.index('elif kind == "permission_response"')]
        self.assertLess(block.index('type="stopped"'), block.index("await cancel_turn()"))

    def test_stop_button_has_immediate_feedback(self):
        source = (ROOT / "tc_agent" / "static" / "index.html").read_text(encoding="utf-8")
        block = source[source.index("stopBtn.onclick"):]
        self.assertIn('showWorking("正在停止…")', block)
        self.assertIn("ws.readyState === WebSocket.OPEN", block)
        self.assertNotIn("stopBtn.disabled = true", block)
        self.assertIn("stopRetryTimer = setTimeout", block)

    def test_processing_status_is_fixed_and_keeps_elapsed_time(self):
        source = (ROOT / "tc_agent" / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="workingStatus"', source)
        self.assertIn("function formatElapsed(ms)", source)
        self.assertIn("setInterval(refreshWorkingElapsed, 1000)", source)
        self.assertNotIn('if (live) hideWorking();                       // 文字开始流出', source)

    def test_provider_changes_do_not_cancel_active_turns(self):
        source = (ROOT / "tc_agent" / "backend.py").read_text(encoding="utf-8")
        start = source.index('elif kind in ("set_provider"')
        block = source[start:source.index("except Exception as e", start)]
        self.assertNotIn("was_running = await cancel_turn()", block)
        self.assertIn('event_type = "provider_switched"', block)
        self.assertIn('event_type = "provider_saved"', block)
        self.assertIn('event_type = "provider_deleted"', block)
        self.assertIn("request_id", block)

    def test_stream_worker_uses_an_immutable_provider_snapshot(self):
        source = (ROOT / "tc_agent" / "backend.py").read_text(encoding="utf-8")
        block = source[source.index("async def stream_complete"):source.index("async def call_model")]
        self.assertIn("request_provider = provider", block)
        self.assertIn("request_provider.complete_stream", block)
        self.assertNotIn("step = provider.complete_stream", block)

    def test_ready_does_not_reenable_send_during_active_turn(self):
        source = (ROOT / "tc_agent" / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn('case "ready": ready = true; sendBtn.disabled = busy;', source)

    def test_stale_permission_buttons_are_closed_on_stop_or_provider_change(self):
        source = (ROOT / "tc_agent" / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn('document.querySelectorAll(".perm:not(.decided)")', source)
        self.assertIn('closePendingPermissions(); add("result", "⏹ 已停止")', source)
        self.assertIn('if (el.classList.contains("decided")) return;', source)


if __name__ == "__main__":
    unittest.main()
