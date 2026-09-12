from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tc_agent.conversation_store import ConversationStore
from tc_agent.runtime import ActionRequest, DurableAction, DurableRun


class DurableRuntimeTests(unittest.TestCase):
    def make_store(self, folder: str) -> tuple[ConversationStore, str]:
        store = ConversationStore(Path(folder) / "agent.db")
        return store, store.list_threads()[0]["id"]

    def test_durable_run_owns_model_usage_and_terminal_state(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store, thread_id = self.make_store(folder)
            run = DurableRun(
                store, thread_id, "检查 MAIN", provider_model="test",
                target_pid=4024, solution="Machine.sln",
            )
            step_id = run.start_model_step({"history_messages": 2})
            self.assertTrue(step_id)
            self.assertEqual(step_id, run.complete_model_step({
                "text": "完成", "tool_calls": [], "usage": {"in": 12, "out": 3},
            }))
            run.finish("completed")
            snapshot = store.agent_run_snapshot(run.run_id)
            self.assertEqual("completed", snapshot["run"]["status"])
            self.assertEqual(1, snapshot["run"]["model_calls"])
            self.assertEqual(12, snapshot["run"]["tokens_in"])
            self.assertEqual(3, snapshot["run"]["tokens_out"])

    def test_durable_action_replays_completed_result(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store, thread_id = self.make_store(folder)
            run = DurableRun(store, thread_id, "读取变量")
            step_id = run.start_model_step()
            run.complete_model_step({"usage": {}})
            request = ActionRequest(
                run.run_id, step_id, "call-1", "plc_read", {"name": "MAIN"},
                category="代码", readonly=True, target_pid=4024,
            )
            first = DurableAction(store, request)
            self.assertEqual("execute", first.disposition)
            first.finish({"name": "MAIN"}, ok=True)
            replay = DurableAction(store, request)
            self.assertEqual("replay", replay.disposition)
            self.assertEqual(({"name": "MAIN"}, True), replay.replay_result())

    def test_durable_action_preserves_denied_and_uncertain_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store, thread_id = self.make_store(folder)
            run = DurableRun(store, thread_id, "修改项目")
            step_id = run.start_model_step()
            run.complete_model_step({"usage": {}})
            denied = DurableAction(store, ActionRequest(
                run.run_id, step_id, "deny", "plc_write", {}, readonly=False,
            ))
            denied.deny("审批拒绝")
            uncertain = DurableAction(store, ActionRequest(
                run.run_id, step_id, "uncertain", "plc_create", {}, readonly=False,
            ))
            uncertain.uncertain("取消时可能仍在执行")
            snapshot = store.agent_run_snapshot(run.run_id)
            statuses = {item["tool_call_id"]: item["status"]
                        for item in snapshot["tool_executions"]}
            self.assertEqual("denied", statuses["deny"])
            self.assertEqual("uncertain", statuses["uncertain"])

    def test_terminal_action_cannot_be_overwritten_by_late_cancellation(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store, thread_id = self.make_store(folder)
            run = DurableRun(store, thread_id, "写入 MAIN")
            step_id = run.start_model_step()
            run.complete_model_step({"usage": {}})
            action = DurableAction(store, ActionRequest(
                run.run_id, step_id, "call-race", "plc_write", {}, readonly=False,
            ))
            action.finish({"written": True}, ok=True)
            with self.assertRaisesRegex(ValueError, "already terminal"):
                action.uncertain("迟到的取消信号")
            execution = store.agent_run_snapshot(run.run_id)["tool_executions"][0]
            self.assertEqual("completed", execution["status"])
            self.assertEqual({"written": True}, execution["result"])


if __name__ == "__main__":
    unittest.main()
