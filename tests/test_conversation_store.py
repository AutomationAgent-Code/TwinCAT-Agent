from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tc_agent import agent_core
from tc_agent import conversation_store as conversation_module
from tc_agent.conversation_store import (
    ConversationHistory,
    ConversationStore,
    conversation_context,
)
from tc_agent.runtime import ActionRequest, action_idempotency_key


class ConversationStoreTests(unittest.TestCase):
    def test_legacy_history_migrates_to_main_thread_once(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            legacy = root / ".tc_agent_history.json"
            legacy.write_text(json.dumps({
                "messages": [{"role": "user", "text": "旧任务"}],
                "events": [{"type": "user", "text": "旧任务"}],
                "summary": "旧摘要",
            }, ensure_ascii=False), encoding="utf-8")
            store = ConversationStore(root / "agent.db", legacy)
            threads = store.list_threads()
            self.assertEqual(1, len(threads))
            state = store.load_state(threads[0]["id"])
            self.assertEqual("旧任务", state["messages"][0]["text"])
            self.assertEqual("旧摘要", state["summary"])

            # Editing the legacy JSON after migration must not overwrite DB state.
            legacy.write_text('{"messages":[]}', encoding="utf-8")
            ConversationStore(root / "agent.db", legacy)
            self.assertEqual("旧任务", store.load_state(threads[0]["id"])["messages"][0]["text"])

    def test_conversation_histories_are_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = ConversationStore(Path(folder) / "agent.db")
            first = store.list_threads()[0]
            second = store.create_thread("PLC 审查")
            one = ConversationHistory(store, first["id"])
            two = ConversationHistory(store, second["id"])
            one.add_message({"role": "user", "text": "主任务"})
            two.add_message({"role": "user", "text": "检查 FB_Motor"})
            self.assertEqual("主任务", ConversationHistory(store, first["id"]).messages[0]["text"])
            self.assertEqual("检查 FB_Motor", ConversationHistory(store, second["id"]).messages[0]["text"])

    def test_stale_history_adapter_cannot_overwrite_concurrent_appends(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = ConversationStore(Path(folder) / "agent.db")
            thread_id = store.list_threads()[0]["id"]
            first = ConversationHistory(store, thread_id)
            stale = ConversationHistory(store, thread_id)
            first.add_message({"role": "user", "text": "first"})
            stale.add_event({"type": "assistant_text", "text": "visible"})
            stale.add_message({"role": "assistant", "text": "second", "tool_calls": []})
            state = store.load_state(thread_id)
            self.assertEqual(["first", "second"], [m["text"] for m in state["messages"]])
            self.assertEqual("visible", state["events"][0]["text"])

    def test_foreground_claim_is_atomic_and_restart_marks_interrupted(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "agent.db"
            store = ConversationStore(path)
            thread_id = store.list_threads()[0]["id"]
            self.assertTrue(store.claim_foreground(thread_id))
            self.assertFalse(store.claim_foreground(thread_id))
            db = sqlite3.connect(path)
            try:
                db.execute("UPDATE metadata SET value='old-runtime' WHERE key='runtime_session'")
                db.commit()
            finally:
                db.close()
            reopened = ConversationStore(path)
            self.assertEqual("interrupted", reopened.get_thread(thread_id)["status"])

    def test_running_conversation_cannot_be_archived(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = ConversationStore(Path(folder) / "agent.db")
            thread_id = store.list_threads()[0]["id"]
            self.assertTrue(store.claim_foreground(thread_id))
            with self.assertRaises(RuntimeError):
                store.archive_thread(thread_id)

    def test_compaction_preserves_messages_appended_after_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = ConversationStore(Path(folder) / "agent.db")
            thread_id = store.list_threads()[0]["id"]
            history = ConversationHistory(store, thread_id)
            history.add_message({"role": "user", "text": "old"})
            source = list(history.messages)
            other = ConversationHistory(store, thread_id)
            other.add_message({"role": "assistant", "text": "tail", "tool_calls": []})
            self.assertTrue(history.commit_compaction(
                source, [{"role": "user", "text": "recent"}], "summary"
            ))
            state = store.load_state(thread_id)
            self.assertEqual(["recent", "tail"], [m["text"] for m in state["messages"]])
            self.assertEqual("summary", state["summary"])

    def test_thread_mailbox_routes_structured_messages(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = ConversationStore(Path(folder) / "agent.db")
            main = store.list_threads()[0]
            worker = store.create_thread("I/O 检查", kind="worker", parent_id=main["id"])
            sent = store.send(main["id"], worker["id"], {
                "objective": "检查 EL7201 链接", "constraints": ["只读"]
            })
            self.assertEqual([], store.inbox(main["id"]))
            inbox = store.inbox(worker["id"], unread_only=True)
            self.assertEqual(sent["id"], inbox[0]["id"])
            self.assertEqual("检查 EL7201 链接", inbox[0]["payload"]["objective"])
            worker_row = next(item for item in store.list_threads() if item["id"] == worker["id"])
            self.assertEqual(1, worker_row["unread_count"])
            store.mark_read(worker["id"], [sent["id"]])
            self.assertEqual([], store.inbox(worker["id"], unread_only=True))

    def test_registered_thread_tools_use_active_conversation_context(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = ConversationStore(Path(folder) / "agent.db")
            main = store.list_threads()[0]
            peer = store.create_thread("代码审查", kind="chat")
            with conversation_context(store, main["id"]):
                agent_core.run_tool("thread_send", {
                    "to_thread": peer["id"], "objective": "审查 FB_Main",
                })
                listed = agent_core.run_tool("thread_list", {})
            self.assertEqual(main["id"], listed["current_thread"])
            self.assertEqual(2, len(listed["threads"]))
            self.assertEqual("审查 FB_Main", store.inbox(peer["id"])[0]["payload"]["objective"])

    def test_cross_thread_message_can_be_persisted_as_visible_event(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = ConversationStore(Path(folder) / "agent.db")
            source = store.list_threads()[0]
            target = store.create_thread("目标对话")
            message = store.send(source["id"], target["id"], {"objective": "读取 FB"})
            ConversationHistory(store, target["id"]).add_event(
                {"type": "thread_message", "message": message}
            )
            state = store.load_state(target["id"])
            self.assertEqual("thread_message", state["events"][-1]["type"])
            self.assertEqual("读取 FB", state["events"][-1]["message"]["payload"]["objective"])

    def test_thread_status_is_persisted_and_validated(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = ConversationStore(Path(folder) / "agent.db")
            worker = store.create_thread("后台检查", kind="worker")
            store.set_status(worker["id"], "running")
            self.assertEqual("running", store.get_thread(worker["id"])["status"])
            with self.assertRaises(ValueError):
                store.set_status(worker["id"], "mystery")

    def test_running_worker_becomes_resumable_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "agent.db"
            store = ConversationStore(db)
            worker = store.create_thread("断点任务", kind="worker")
            store.set_status(worker["id"], "running")
            # A second WebView in the same backend process must not interrupt
            # the worker. Only a new runtime session (service restart) does.
            self.assertEqual("running", ConversationStore(db).get_thread(worker["id"])["status"])
            with patch.object(conversation_module, "RUNTIME_SESSION_ID", "new-process"):
                reopened = ConversationStore(db)
            self.assertEqual("interrupted", reopened.get_thread(worker["id"])["status"])

    def test_restart_reconciles_worker_run_and_lists_resume_audit(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "agent.db"
            store = ConversationStore(db)
            worker = store.create_thread("恢复审查", kind="review")
            store.set_status(worker["id"], "running")
            run_id = store.start_worker_run(worker["id"], "deepseek-chat")
            with patch.object(conversation_module, "RUNTIME_SESSION_ID", "restart-audit"):
                reopened = ConversationStore(db)
            run = reopened.worker_runs(worker["id"])[0]
            self.assertEqual(run_id, run["id"])
            self.assertEqual("interrupted", run["status"])
            self.assertIsNotNone(run["finished_at"])
            self.assertIn("服务重启", run["error"])
            resumable = reopened.resumable_workers()
            self.assertEqual(worker["id"], resumable[0]["id"])
            self.assertEqual(run_id, resumable[0]["latest_run"]["id"])

    def test_change_proposal_has_auditable_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = ConversationStore(Path(folder) / "agent.db")
            parent = store.list_threads()[0]
            worker = store.create_thread("审查", kind="worker", parent_id=parent["id"])
            proposal = store.create_proposal(
                worker["id"], parent["id"], "plc_patch",
                {"name": "MAIN", "old": "a", "new": "b"}, "修复输出",
            )
            self.assertEqual(proposal["id"], store.list_proposals(parent["id"])[0]["id"])
            resolved = store.resolve_proposal(proposal["id"], "approved", {"ok": True})
            self.assertEqual("approved", resolved["status"])
            self.assertEqual({"ok": True}, resolved["result"])
            self.assertEqual([], store.list_proposals(parent["id"]))

    def test_worker_usage_and_parent_collection_are_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = ConversationStore(Path(folder) / "agent.db")
            parent = store.list_threads()[0]
            worker = store.create_thread("查文档", kind="research", parent_id=parent["id"])
            run_id = store.start_worker_run(worker["id"], "deepseek-chat")
            store.set_status(worker["id"], "completed")
            store.finish_worker_run(
                run_id, "completed", model_calls=3, tokens_in=120, tokens_out=45
            )
            store.send(
                worker["id"], parent["id"], {"objective": "已找到参数"},
                message_type="task_result",
            )
            collected = store.collect_children(parent["id"])
            self.assertTrue(collected["all_done"])
            self.assertEqual(3, collected["children"][0]["latest_run"]["model_calls"])
            self.assertEqual(120, collected["children"][0]["latest_run"]["tokens_in"])
            self.assertEqual("已找到参数", collected["children"][0]["results"][0]["payload"]["objective"])

    def test_worker_queue_claim_is_atomic_across_views(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "agent.db"
            first = ConversationStore(db)
            worker = first.create_thread("并发检查", kind="worker")
            second = ConversationStore(db)
            self.assertTrue(first.queue_worker(worker["id"]))
            self.assertFalse(second.queue_worker(worker["id"]))

    def test_fork_copies_context_but_not_events_or_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = ConversationStore(Path(folder) / "agent.db")
            source = store.list_threads()[0]
            history = ConversationHistory(store, source["id"])
            history.add_message({"role": "user", "text": "保留的上下文"})
            history.add_event({"type": "user", "text": "UI 回放"})
            history.summary = "旧摘要"
            history.recovery = {"request": "不要复制"}
            history._save()
            forked = store.fork_thread(source["id"], "方案 B")
            state = store.load_state(forked["id"])
            self.assertEqual("保留的上下文", state["messages"][0]["text"])
            self.assertEqual("旧摘要", state["summary"])
            self.assertEqual([], state["events"])
            self.assertEqual({}, state["recovery"])

    def test_event_ids_make_replay_and_reconnect_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = ConversationStore(Path(folder) / "agent.db")
            thread = store.list_threads()[0]
            history = ConversationHistory(store, thread["id"])
            first = history.add_event({"type": "assistant_replace", "event_id": "stable-1",
                                       "text": "一次"})
            second = history.add_event({"type": "assistant_replace", "event_id": "stable-1",
                                        "text": "重复推送"})
            generated = history.add_event({"type": "result", "text": "生成 ID"})
            state = store.load_state(thread["id"])
            stable = [item for item in state["events"] if item.get("event_id") == "stable-1"]
            assert first["event_id"] == second["event_id"] == "stable-1"
            assert generated["event_id"]
            assert len(stable) == 1

    def test_tool_categories_are_per_thread_and_copied_to_fork(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = ConversationStore(Path(folder) / "agent.db")
            source = store.list_threads()[0]
            updated = store.set_tool_categories(source["id"], ["代码", "环境", "代码"])
            self.assertEqual(["代码", "环境"], json.loads(updated["tool_categories"]))
            forked = store.fork_thread(source["id"], "工具分配副本")
            self.assertEqual(["代码", "环境"], json.loads(forked["tool_categories"]))
            self.assertEqual(["代码", "环境"], ConversationStore.parse_tool_categories(forked))

    def test_normal_chat_can_be_claimed_for_dispatched_background_task(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = ConversationStore(Path(folder) / "agent.db")
            target = store.create_thread("目标会话", kind="chat")
            self.assertTrue(store.queue_worker(target["id"]))
            self.assertFalse(store.queue_worker(target["id"]))

    def test_handoff_reparents_child_and_rejects_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = ConversationStore(Path(folder) / "agent.db")
            first = store.list_threads()[0]
            second = store.create_thread("另一个父对话")
            child = store.create_thread("子任务", kind="worker", parent_id=first["id"])
            moved = store.reparent_thread(child["id"], second["id"])
            self.assertEqual(second["id"], moved["parent_id"])
            with self.assertRaises(ValueError):
                store.reparent_thread(second["id"], child["id"])

    def test_thread_can_be_renamed(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = ConversationStore(Path(folder) / "agent.db")
            thread = store.list_threads()[0]
            renamed = store.rename_thread(thread["id"], "PLC 重构")
            self.assertEqual("PLC 重构", renamed["title"])

    def test_thread_pin_is_persistent_and_sorts_before_recent_threads(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "agent.db"
            store = ConversationStore(db)
            first = store.list_threads()[0]
            recent = store.create_thread("最新对话")

            pinned = store.set_pinned(first["id"], True)
            self.assertEqual(1, pinned["pinned"])
            self.assertEqual(first["id"], store.list_threads()[0]["id"])

            reopened = ConversationStore(db)
            self.assertEqual(first["id"], reopened.list_threads()[0]["id"])
            self.assertEqual(1, reopened.get_thread(first["id"])["pinned"])

            reopened.set_pinned(first["id"], False)
            self.assertEqual(recent["id"], reopened.list_threads()[0]["id"])

    def test_thread_working_directory_is_relative_persistent_and_inherited(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "agent.db"
            store = ConversationStore(db)
            thread = store.list_threads()[0]
            updated = store.set_working_directory(thread["id"], r"PLC1\POUs")
            self.assertEqual("PLC1/POUs", updated["working_directory"])
            forked = store.fork_thread(thread["id"], "目录分叉")
            self.assertEqual("PLC1/POUs", forked["working_directory"])
            self.assertEqual(
                "PLC1/POUs", ConversationStore(db).get_thread(thread["id"])["working_directory"]
            )
            for invalid in (r"C:\PLC1", "../outside", "/absolute"):
                with self.assertRaises(ValueError):
                    store.set_working_directory(thread["id"], invalid)

    def test_schema_migrates_existing_threads_to_pinned_column(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "agent.db"
            connection = sqlite3.connect(db)
            connection.execute(
                "CREATE TABLE threads ("
                "id TEXT PRIMARY KEY,title TEXT NOT NULL,kind TEXT NOT NULL DEFAULT 'chat',"
                "parent_id TEXT,status TEXT NOT NULL DEFAULT 'idle',"
                "archived INTEGER NOT NULL DEFAULT 0,created_at REAL NOT NULL,"
                "updated_at REAL NOT NULL)"
            )
            connection.execute(
                "INSERT INTO threads(id,title,created_at,updated_at) VALUES('main','主对话',1,1)"
            )
            connection.commit()
            connection.close()

            store = ConversationStore(db)
            self.assertEqual(0, store.get_thread("main")["pinned"])
            self.assertEqual(".", store.get_thread("main")["working_directory"])
            self.assertEqual(1, store.set_pinned("main", True)["pinned"])

    def test_reopen_creates_active_thread_when_only_archived_rows_remain(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "agent.db"
            store = ConversationStore(db)
            only = store.list_threads()[0]
            store.archive_thread(only["id"])
            reopened = ConversationStore(db)
            active = reopened.list_threads()
            self.assertEqual(1, len(active))
            self.assertEqual("主对话", active[0]["title"])
            self.assertNotEqual(only["id"], active[0]["id"])

    def test_project_memory_is_shared_conflict_aware_and_forgettable(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = ConversationStore(Path(folder) / "agent.db")
            source = store.list_threads()[0]
            saved = store.remember(
                source["id"], "plc.naming", "decision", "沿用项目现有 PascalCase", 0.9
            )
            self.assertIn("PascalCase", store.memory_prompt())
            conflict = store.remember(
                source["id"], "plc.naming", "decision", "改为 snake_case", 0.8
            )
            self.assertTrue(conflict["conflict"])
            store.forget(saved["id"])
            self.assertEqual([], store.memories())

    def test_project_memory_rejects_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = ConversationStore(Path(folder) / "agent.db")
            source = store.list_threads()[0]
            for secret in ("api_key=abc123", "sk-1234567890abcdef", "TCAG1.secret-license"):
                with self.assertRaises(ValueError):
                    store.remember(source["id"], "secret", "fact", secret)

    def test_project_database_health_backup_and_export(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = ConversationStore(Path(folder) / "agent.db")
            main = store.list_threads()[0]
            ConversationHistory(store, main["id"]).add_message(
                {"role": "user", "text": "保留项目上下文"}
            )
            health = store.health()
            self.assertTrue(health["ok"])
            self.assertEqual(conversation_module.SCHEMA_VERSION, health["schema_version"])
            backup = Path(store.backup()["backup"])
            exported = Path(store.export_json()["export"])
            self.assertTrue(backup.is_file())
            self.assertTrue(exported.is_file())
            payload = json.loads(exported.read_text(encoding="utf-8"))
            self.assertEqual("twincat-agent-project-export-v1", payload["format"])
            self.assertIn("保留项目上下文", payload["thread_state"][0]["messages_json"])

    def test_agent_run_ledger_records_model_tool_and_approval(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = ConversationStore(Path(folder) / "agent.db")
            thread = store.list_threads()[0]
            run_id = store.start_agent_run(
                thread["id"], "修改 MAIN", provider_model="test-model",
                target_pid=4024, solution=r"C:\Project\Machine.sln",
            )
            step_id = store.start_agent_step(
                run_id, 1, "model", {"history_messages": 3}
            )
            store.finish_agent_step(
                step_id, "completed", output_payload={"tool_calls": ["call-1"]}
            )
            request = ActionRequest(
                run_id, step_id, "call-1", "plc_write", {"name": "MAIN"},
                category="代码", danger="project", target_pid=4024,
            ).envelope()
            reserved = store.begin_tool_execution(
                run_id=run_id, step_id=step_id, tool_call_id="call-1",
                tool_name="plc_write", args={"name": "MAIN"}, category="代码",
                danger="project", readonly=False, target_pid=4024,
                idempotency_key=request["idempotency_key"],
            )
            execution_id = reserved["execution"]["id"]
            approval_id = store.create_approval(
                run_id, {"tool_name": "plc_write"},
                tool_execution_id=execution_id,
            )
            store.resolve_approval(approval_id, True, "用户确认")
            store.finish_tool_execution(
                execution_id, "completed", result={"ok": True}
            )
            store.finish_agent_run(
                run_id, "completed", model_calls=1, tokens_in=20, tokens_out=5
            )

            snapshot = store.agent_run_snapshot(run_id)
            self.assertEqual("completed", snapshot["run"]["status"])
            self.assertEqual("completed", snapshot["steps"][0]["status"])
            self.assertEqual({"ok": True}, snapshot["tool_executions"][0]["result"])
            self.assertEqual("approved", snapshot["approvals"][0]["status"])
            self.assertEqual(1, store.health()["counts"]["agent_runs"])

    def test_completed_tool_action_is_replayed_without_second_reservation(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = ConversationStore(Path(folder) / "agent.db")
            thread = store.list_threads()[0]
            run_id = store.start_agent_run(thread["id"], "写变量")
            step_id = store.start_agent_step(run_id, 1, "model")
            key = action_idempotency_key(
                run_id, "same-call", "ads_write", {"symbol": "MAIN.nValue", "value": 1}
            )
            arguments = dict(
                run_id=run_id, step_id=step_id, tool_call_id="same-call",
                tool_name="ads_write", args={"symbol": "MAIN.nValue", "value": 1},
                category="ADS", danger="runtime", readonly=False, target_pid=4024,
                idempotency_key=key,
            )
            first = store.begin_tool_execution(**arguments)
            store.finish_tool_execution(
                first["execution"]["id"], "completed", result={"written": True}
            )
            second = store.begin_tool_execution(**arguments)
            self.assertEqual("replay", second["disposition"])
            self.assertEqual(first["execution"]["id"], second["execution"]["id"])
            self.assertEqual({"written": True}, second["execution"]["result"])

    def test_restart_marks_inflight_mutation_uncertain_instead_of_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "agent.db"
            store = ConversationStore(db)
            thread = store.list_threads()[0]
            run_id = store.start_agent_run(thread["id"], "创建对象")
            step_id = store.start_agent_step(run_id, 1, "model")
            ConversationHistory(store, thread["id"]).add_message({
                "role": "assistant", "text": "", "tool_calls": [{
                    "id": "call-x", "name": "plc_create", "args": {"name": "FB_X"},
                }],
            })
            key = action_idempotency_key(run_id, "call-x", "plc_create", {"name": "FB_X"})
            store.begin_tool_execution(
                run_id=run_id, step_id=step_id, tool_call_id="call-x",
                tool_name="plc_create", args={"name": "FB_X"}, category="代码",
                danger="project", readonly=False, target_pid=4024,
                idempotency_key=key,
            )
            with patch.object(conversation_module, "RUNTIME_SESSION_ID", "new-ledger-process"):
                reopened = ConversationStore(db)
            snapshot = reopened.agent_run_snapshot(run_id)
            self.assertEqual("interrupted", snapshot["run"]["status"])
            self.assertEqual("interrupted", snapshot["steps"][0]["status"])
            self.assertEqual("uncertain", snapshot["tool_executions"][0]["status"])
            repaired = reopened.load_state(thread["id"])["messages"]
            self.assertEqual("tool", repaired[1]["role"])
            self.assertEqual("call-x", repaired[1]["id"])
            self.assertIn("禁止自动重放", repaired[1]["result"]["error"])

    def test_replace_last_assistant_removes_unaccepted_claim_from_model_history(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = ConversationStore(Path(folder) / "agent.db")
            thread = store.list_threads()[0]
            history = ConversationHistory(store, thread["id"])
            history.add_message({"role": "user", "text": "构建 HMI"})
            history.add_message({"role": "assistant", "text": "HMI 构建成功", "tool_calls": []})
            history.replace_last_assistant("工程验收未通过")
            restored = ConversationHistory(store, thread["id"])
            self.assertEqual("工程验收未通过", restored.messages[-1]["text"])
            self.assertNotIn("HMI 构建成功", json.dumps(restored.messages, ensure_ascii=False))



if __name__ == "__main__":
    unittest.main()
