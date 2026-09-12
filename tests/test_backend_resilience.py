from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tc_agent import agent_core as ac
from tc_agent import backend, licensing
from tc_agent.conversation_store import ConversationStore
from tc_template import _ps_bridge


class BackendResilienceTests(unittest.TestCase):
    def tearDown(self) -> None:
        backend.PROJECT_SUBSCRIBERS.clear()
        backend.FOREGROUND_CANCELS.clear()
        backend.FOREGROUND_PERMISSIONS.clear()

    def test_permission_request_uuid_dependency_is_available(self) -> None:
        request_id = f"p1-{backend.uuid.uuid4().hex}"
        self.assertRegex(request_id, r"^p1-[0-9a-f]{32}$")

    def test_current_architecture_docs_use_registry_and_sqlite(self) -> None:
        root = Path(__file__).resolve().parents[1]
        backend_source = Path(backend.__file__).read_text(encoding="utf-8")
        header = backend_source[:backend_source.index("from __future__")]
        self.assertIn(f"{len(ac.REGISTRY)} 个工具基线", header)
        self.assertIn(".TwinCATAgent\\\\agent.db（SQLite + WAL）", header)
        self.assertIn("一次性迁移来源", header)
        self.assertNotIn(" 20 个工具基线", header)

        readme = (root / "README.md").read_text(encoding="utf-8")
        installer = (root / "docs" / "installer.md").read_text(encoding="utf-8")
        self.assertIn(f"{len(ac.REGISTRY)} tools", readme)
        self.assertIn(".TwinCATAgent/agent.db", readme)
        self.assertIn(".TwinCATAgent/agent.db", installer)
        self.assertIn("一次性迁移来源", installer)

    def test_solution_tree_picker_uses_live_xae_tool(self) -> None:
        source = Path(backend.__file__).read_text(encoding="utf-8")
        self.assertIn('ps_com("solution-tree")', source)
        self.assertIn("not visible in the native ROT", source)
        self.assertNotIn("_project_directories", dir(backend))

    def test_project_event_bus_isolated_by_database_path(self) -> None:
        class Socket:
            def __init__(self): self.messages = []
            async def send(self, raw): self.messages.append(json.loads(raw))

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            first = ConversationStore(root / "a" / "agent.db")
            second = ConversationStore(root / "b" / "agent.db")
            one, two = Socket(), Socket()
            backend.PROJECT_SUBSCRIBERS[backend._project_channel(first)] = {one}
            backend.PROJECT_SUBSCRIBERS[backend._project_channel(second)] = {two}
            asyncio.run(backend._broadcast_project(first, type="worker_status", status="running"))
            self.assertEqual("running", one.messages[0]["status"])
            self.assertEqual([], two.messages)

    def test_project_event_bus_drops_closed_subscribers(self) -> None:
        class ClosedSocket:
            async def send(self, raw): raise RuntimeError("closed")

        with tempfile.TemporaryDirectory() as folder:
            store = ConversationStore(Path(folder) / "agent.db")
            channel = backend._project_channel(store)
            backend.PROJECT_SUBSCRIBERS[channel] = {ClosedSocket()}
            asyncio.run(backend._broadcast_project(store, type="test"))
            self.assertNotIn(channel, backend.PROJECT_SUBSCRIBERS)

    def test_background_workers_are_read_only_except_mailbox(self) -> None:
        self.assertTrue(backend._worker_tool_allowed("plc_read"))
        self.assertTrue(backend._worker_tool_allowed("docs_search"))
        self.assertTrue(backend._worker_tool_allowed("thread_send"))
        self.assertFalse(backend._worker_tool_allowed("plc_write"))
        self.assertFalse(backend._worker_tool_allowed("tc_run_mode"))

    def test_background_workers_are_process_owned_not_websocket_owned(self) -> None:
        source = Path(backend.__file__).read_text(encoding="utf-8")
        finally_block = source[source.index("    finally:\n", source.index("async def handler")):]
        self.assertIn("Background workers are process-owned", finally_block)
        self.assertNotIn("BACKGROUND_WORKER_TASKS.values()", finally_block)
        self.assertIn("BACKGROUND_WORKER_TASKS[key] = asyncio.create_task", source)
        self.assertIn("worker_solution = last_solution if solution_override is None else solution_override", source)
        self.assertIn("worker_pid = last_pid if pid_override is None else pid_override", source)
        self.assertIn("run_worker(conversation_store, thread_id, worker_solution, worker_pid, worker_provider)", source)

    def test_foreground_turns_keep_independent_execution_snapshots(self) -> None:
        first = backend.ForegroundTurn("thread-a", object(), 2, "A.sln", 101, object())
        second = backend.ForegroundTurn("thread-b", object(), 4, "B.sln", 202, object())
        first.visual.append({"type": "image"})
        first.streamed_text[0] = "partial A"
        first.read_cache["read"] = {"thread": "a"}
        self.assertEqual([], second.visual)
        self.assertEqual("", second.streamed_text[0])
        self.assertEqual({}, second.read_cache)
        self.assertEqual(("thread-a", "A.sln", 101), (first.thread_id, first.solution, first.pid))
        self.assertEqual(("thread-b", "B.sln", 202), (second.thread_id, second.solution, second.pid))

    def test_switching_threads_does_not_cancel_another_foreground_turn(self) -> None:
        source = Path(backend.__file__).read_text(encoding="utf-8")
        self.assertIn("foreground_tasks: dict[str, asyncio.Task]", source)
        self.assertIn("CURRENT_FOREGROUND_TURN.set(turn)", source)
        self.assertIn("active_foreground_task(user_thread_id)", source)
        switch = source[source.index('            elif kind == "switch_thread":'):source.index(
            '            elif kind == "archive_thread":'
        )]
        self.assertNotIn("任务执行中，请先停止再切换对话", switch)

    def test_worker_tool_loop_is_reachable_after_model_tool_call(self) -> None:
        source = Path(backend.__file__).read_text(encoding="utf-8")
        worker = source[source.index("    async def run_worker"):source.index("    async def start_worker")]
        no_calls = worker.index("if not calls:")
        return_at = worker.index("return", no_calls)
        tool_loop = worker.index("for call in calls:")
        self.assertGreater(tool_loop, return_at)
        # The tool loop must be aligned with the no-tool branch rather than
        # nested below its return statement.
        no_calls_indent = len(worker[worker.rfind("\n", 0, no_calls) + 1:no_calls])
        tool_loop_indent = len(worker[worker.rfind("\n", 0, tool_loop) + 1:tool_loop])
        self.assertEqual(no_calls_indent, tool_loop_indent)

    def test_foreground_and_worker_share_durable_runtime_primitives(self) -> None:
        source = Path(backend.__file__).read_text(encoding="utf-8")
        foreground = source[source.index("    async def run_turn"):source.index(
            "    async def adopt_solution"
        )]
        worker = source[source.index("    async def run_worker"):source.index(
            "    async def start_worker"
        )]
        for section in (foreground, worker):
            self.assertIn("DurableRun(", section)
            self.assertIn("DurableAction(", section)
            self.assertIn("complete_model_step", section)

    def test_summary_split_keeps_recent_complete_user_turns(self) -> None:
        messages = []
        for index in range(5):
            messages.extend([
                {"role": "user", "text": f"task-{index}" + "x" * 80},
                {"role": "assistant", "text": f"answer-{index}" + "y" * 80,
                 "tool_calls": []},
            ])
        split = backend._summary_split_index(messages, recent_budget=380)
        self.assertGreater(split, 0)
        self.assertEqual("user", messages[split]["role"])
        self.assertLessEqual(backend._messages_chars(messages[split:]), 380)

    def test_terse_choice_is_bound_to_compressed_pending_decision(self) -> None:
        messages = [{"role": "user", "text": "1"}]
        summary = "待用户选择：1. 打住；2. 重构 FB_TxtTableRead。"
        instruction = backend._terse_choice_instruction(messages, summary)
        self.assertIn("直接回答", instruction)
        self.assertIn("不要把它当作新会话", instruction)
        self.assertIn("thread_list", instruction)

    def test_short_acknowledgement_is_bound_to_preceding_proposal(self) -> None:
        messages = [{"role": "user", "text": "需要"}]
        summary = "助手刚询问是否需要把 S0 转成 ValueSmooth 供 HMI 使用。"
        instruction = backend._terse_choice_instruction(messages, summary)
        self.assertIn("最后一条助手提议", instruction)
        self.assertIn("恢复其指代", instruction)
        self.assertIn("不要把它当作新会话", instruction)

    def test_compaction_bridge_preserves_antecedent_for_short_reply(self) -> None:
        old = [
            {"role": "user", "text": "检查 S0"},
            {"role": "assistant", "text": "是否需要把 S0 转成 ValueSmooth？"},
        ]
        recent = [{"role": "user", "text": "需要"}]
        bridge = backend._compaction_boundary_bridge(old, recent)
        self.assertIn("压缩边界原文", bridge)
        self.assertIn("是否需要把 S0 转成 ValueSmooth？", bridge)

    def test_compaction_bridge_ignores_standalone_request(self) -> None:
        old = [{"role": "assistant", "text": "是否继续？"}]
        recent = [{"role": "user", "text": "检查 MAIN 的状态机"}]
        self.assertEqual("", backend._compaction_boundary_bridge(old, recent))

    def test_ordinary_request_does_not_get_choice_instruction(self) -> None:
        messages = [{"role": "user", "text": "检查 MAIN 的状态机"}]
        self.assertEqual("", backend._terse_choice_instruction(messages, "旧摘要"))
        self.assertFalse(backend._is_terse_choice("ok"))
        self.assertFalse(backend._is_terse_choice("hi"))
        self.assertTrue(backend._is_contextual_reply("需要"))
        self.assertTrue(backend._is_contextual_reply("按照你的来"))
        self.assertFalse(backend._is_contextual_reply("检查 MAIN 的状态机"))

    def test_last_assistant_text_preserves_choice_prompt(self) -> None:
        messages = [
            {"role": "user", "text": "怎么处理"},
            {"role": "assistant", "text": "请选择：1. 打住；2. 重构。"},
            {"role": "tool", "name": "plc_build", "result": {}},
        ]
        self.assertEqual("请选择：1. 打住；2. 重构。",
                         backend._last_assistant_text(messages))

    def test_ui_remembers_active_thread_per_xae_and_shows_restart_recovery(self) -> None:
        ui = (Path(backend.__file__).parent / "static" / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("tc-agent-active-thread:", ui)
        self.assertIn("thread_id: activeThreadId", ui)
        self.assertIn("上次任务因 Agent 后端重启而中断", ui)

    def test_tool_exceptions_are_converted_to_matching_results(self) -> None:
        source = Path(backend.__file__).read_text(encoding="utf-8")
        run_turn = source[source.index("    async def run_turn"):source.index(
            "    async def adopt_solution"
        )]
        self.assertIn('result = {"error": str(tool_exc)', run_turn)
        self.assertIn("repair_tool_results", run_turn)
        self.assertIn("fail_streak >= FAIL_STREAK", run_turn)

    def test_worker_cancel_repairs_tool_tail_and_keeps_recovery(self) -> None:
        source = Path(backend.__file__).read_text(encoding="utf-8")
        worker = source[source.index("    async def run_worker"):source.index(
            "    async def start_worker"
        )]
        cancel_start = worker.rindex("        except asyncio.CancelledError:")
        cancelled = worker[cancel_start:worker.index(
            "        except Exception as exc", cancel_start
        )]
        self.assertIn("repair_tool_results", cancelled)
        self.assertIn("set_recovery", cancelled)
        self.assertIn("fail_streak >= FAIL_STREAK", worker)

    def test_history_replay_retains_pending_tool_cards_while_running(self) -> None:
        ui = (Path(backend.__file__).parent / "static" / "index.html").read_text(
            encoding="utf-8"
        )
        history = ui[ui.index('      case "history": {'):ui.index(
            '      case "history_cleared":'
        )]
        self.assertLess(
            history.index("const backgroundRunning"), history.index("try {")
        )
        self.assertIn("if (!backgroundRunning)", ui)
        self.assertIn('log.classList.remove("history-replay")', history)
        self.assertIn("document.createDocumentFragment()", history)
        self.assertIn("function toolResultPresentation(result)", ui)

    def test_tool_result_presentation_keeps_error_and_incomplete_behavior(self) -> None:
        ui = (Path(backend.__file__).parent / "static" / "index.html").read_text(
            encoding="utf-8"
        )
        start = ui.index("  function toolResultPresentation(result) {")
        end = ui.index("  function render(ev, live) {", start)
        helper = ui[start:end]
        script = helper + "\nconsole.log(JSON.stringify([\n" \
            "toolResultPresentation({status:'incomplete', build_succeeded:true})," \
            "toolResultPresentation({status:'review_required', build_succeeded:true})," \
            "toolResultPresentation({status:'readback_mismatch', written:true, verified:false})," \
            "toolResultPresentation({status:'written_readback_unavailable', written:true, verified:false})," \
            "toolResultPresentation({status:'write_result_unknown', written:'unknown', verified:false})," \
            "toolResultPresentation({error:'X'})\n]));"
        result = subprocess.run(["node", "-e", script], capture_output=True,
                                text=True, encoding="utf-8", check=True)
        self.assertEqual([
            {"className": "tool-incomplete", "label": "构建成功，诊断未完成"},
            {"className": "tool-incomplete", "label": "构建成功，有警告待审查"},
            {"className": "tool-incomplete", "label": "已写入，回读不一致"},
            {"className": "tool-incomplete", "label": "已写入，验证未完成"},
            {"className": "tool-incomplete", "label": "写入结果未知，禁止重试"},
            {"className": "tool-error", "label": "失败"},
        ], json.loads(result.stdout))

    def test_foreground_turn_survives_webview_disconnect(self) -> None:
        source = Path(backend.__file__).read_text(encoding="utf-8")
        finally_block = source[source.index(
            "    finally:\n        if watcher is not None"
        ):source.index(
            "# ======================================================================\n"
            "#  静态 HTTP", source.index("    finally:\n        if watcher is not None")
        )]
        self.assertNotIn("await cancel_turn()", finally_block)
        self.assertIn("foreground\n        # model turns now follow the same rule", finally_block)
        self.assertIn('type="thread_event"', source)
        self.assertIn("FOREGROUND_PERMISSIONS", source)

    def test_project_thread_events_drive_live_ui_after_reconnect(self) -> None:
        ui = (Path(backend.__file__).parent / "static" / "index.html").read_text(
            encoding="utf-8"
        )
        thread_event = ui[ui.index('      case "thread_event":'):ui.index(
            '      case "worker_status":'
        )]
        self.assertIn("handle(ev.event)", thread_event)
        self.assertIn("render(ev.event, true)", thread_event)

    def test_persisted_events_have_stable_ids_and_single_foreground_emit(self) -> None:
        source = Path(backend.__file__).read_text(encoding="utf-8")
        self.assertIn('persisted = turn_history.add_event(payload)', source)
        self.assertIn('event = worker_hist.add_event(event)', source)
        self.assertNotIn('hist.add_event({"type": "assistant_replace"', source)

        ui = (Path(backend.__file__).parent / "static" / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("renderedEventIds", ui)
        self.assertIn("event_id", ui)

    def test_report_finalization_emits_one_report_with_evidence_gate(self) -> None:
        source = Path(backend.__file__).read_text(encoding="utf-8")
        self.assertIn('type": "assistant_draft"', source)
        self.assertIn('type="assistant_report"', source)
        self.assertNotIn('type="assistant_replace", text=retracted', source)
        self.assertNotIn('type": "assistant_replace", "text": retracted', source)
        self.assertIn('report_id=report_id', source)
        self.assertIn("_final_evidence_state", source)
        self.assertIn("guard_final", source)
        self.assertNotIn("retryable_validation_gap", source)
        self.assertNotIn("验收调度", source)
        self.assertIn("completion_evidence.instruction", source)
        self.assertIn("completion_evidence.record", source)
        self.assertIn("worker_evidence.instruction", source)
        self.assertIn("worker_evidence.record", source)

        self.assertFalse(backend._actual_tool_failure(
            {"status": "denied", "authorization_blocked": True}, False
        ))
        self.assertFalse(backend._actual_tool_failure(
            {"status": "duplicate_read_skipped"}, False
        ))
        self.assertTrue(backend._actual_tool_failure({"status": "failed"}, False))

        self.assertFalse(backend._is_user_interaction_response("在线行为验证完成。以下是结果总结"))
        self.assertTrue(backend._is_user_interaction_response("请确认是否继续在线验证？"))
        self.assertTrue(backend._is_user_interaction_response("需要您授权后再继续。"))

        for relative in ("tc_agent/static/index.html", "tc_agent_vsix/webview/index.html"):
            ui = (Path(backend.__file__).parents[1] / relative).read_text(encoding="utf-8")
            self.assertIn('case "assistant_report":', ui)
            self.assertIn("renderedReports", ui)
            self.assertIn("report_id", ui)

    def test_collapsed_history_tools_render_details_only_when_opened(self) -> None:
        ui = (Path(backend.__file__).parent / "static" / "index.html").read_text(
            encoding="utf-8"
        )
        tool_use = ui[ui.index('      case "tool_use": {'):ui.index(
            '      case "tool_result": {'
        )]
        self.assertNotIn("renderToolInput(el", tool_use)
        self.assertIn("renderDeferredToolDetails(el)", ui)
        self.assertIn("el._toolResult = {content:", ui)
        self.assertIn("folded._thinkingRendered", ui)

    def test_local_updater_never_replaces_conversation_database(self) -> None:
        updater = (Path(backend.__file__).parents[1] / "scripts" /
                   "Update-LocalInstallation.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("'agent.db', 'agent.db-wal', 'agent.db-shm'", updater)
        self.assertIn("-ProgramOnly", updater)

    def test_history_summary_survives_reload_and_clear(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".tc_agent_history.json"
            history = backend.History(path)
            history.summary = "目标：修复 MAIN；待办：编译验证"
            history._save()
            self.assertIn("修复 MAIN", backend.History(path).summary)
            history.clear()
            self.assertEqual("", backend.History(path).summary)

    def test_connection_reset_is_classified_as_recoverable(self) -> None:
        error = ConnectionResetError(10054, "远程主机强迫关闭了一个现有的连接")
        self.assertTrue(backend._is_connection_reset_error(error))
        self.assertFalse(backend._is_connection_reset_error(RuntimeError("API 400")))

    def test_continue_request_restores_persisted_interrupted_context(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".tc_agent_history.json"
            history = backend.History(path)
            history.add_message({"role": "user", "text": "检查并修复 MAIN"})
            history.add_message({"role": "tool", "id": "t1", "name": "plc_read",
                                 "result": {"name": "MAIN", "status": "ok"}})
            history.set_recovery("检查并修复 MAIN", "[WinError 10054] reset", "已读取 MAIN")

            restored = backend.History(path)
            prompt = restored.resume_prompt("继续")
            self.assertIn("检查并修复 MAIN", prompt)
            self.assertIn("plc_read", prompt)
            self.assertIn("不要声称没有上下文", prompt)
            self.assertEqual("新的独立任务", restored.resume_prompt("新的独立任务"))

            restored.clear_recovery()
            self.assertEqual("继续", backend.History(path).resume_prompt("继续"))

    def test_empty_model_step_is_not_valid_completion(self) -> None:
        self.assertFalse(backend._model_step_has_output({"text": "", "tool_calls": []}))
        self.assertTrue(backend._model_step_has_output({"text": "完成", "tool_calls": []}))
        self.assertTrue(
            backend._model_step_has_output(
                {"text": "", "tool_calls": [{"id": "1", "name": "tc_state"}]}
            )
        )

    def test_empty_response_has_bounded_retries(self) -> None:
        self.assertEqual(backend.EMPTY_RESPONSE_RETRIES, 2)

    def test_empty_model_step_does_not_claim_twincat_task_failed(self) -> None:
        source = Path(backend.__file__).read_text(encoding="utf-8")
        start = source.index("# Some API gateways occasionally end an SSE response")
        end = source.index("except asyncio.CancelledError:", start)
        block = source[start:end]
        self.assertIn("is_error=tool_failure_seen", block)
        self.assertNotIn("validation_note", block)
        self.assertIn("return", block)
        self.assertNotIn("任务可能未完成", block)

    def test_tool_result_always_returns_to_model_instead_of_false_failure(self) -> None:
        source = Path(backend.__file__).read_text(encoding="utf-8")
        start = source.index("# Tool results are input to the next model step.")
        end = source.index("else:\n                            # Some API gateways", start)
        block = source[start:end]
        self.assertIn("continue", block)
        self.assertNotIn("任务可能未完成", source)

    def test_legacy_xae_prefers_32_bit_powershell_bridge(self) -> None:
        bridge = _ps_bridge._powershell_exe().lower()
        if "syswow64" in bridge:
            self.assertIn("syswow64\\windowspowershell", bridge)

    def test_ui_tool_result_stays_valid_json_for_code_review(self) -> None:
        normal = {"name": "MAIN", "implementation": "x := x + 1;"}
        self.assertEqual(normal, json.loads(backend._tool_result_for_ui(normal)))
        huge = {"implementation": "x := x + 1;\n" * 10000}
        capped = json.loads(backend._tool_result_for_ui(huge))
        self.assertEqual(huge, capped)
        self.assertNotIn("ui_truncated", capped)

    def test_generic_context_cap_preserves_json_shape(self) -> None:
        result = {
            "status": "ok",
            "name": "large-result",
            "items": [{"id": index, "text": "x" * 1000} for index in range(40)],
        }
        capped = ac.cap_tool_result(result)
        self.assertIs(capped["_capped"], True)
        self.assertIsInstance(capped["summary"], dict)
        self.assertEqual("ok", capped["summary"]["status"])
        self.assertLessEqual(
            len(json.dumps(capped, ensure_ascii=False)), ac.TOOL_RESULT_CAP
        )

    def test_generic_context_cap_keeps_tail_failure(self) -> None:
        result = {
            "status": "failed",
            "items": ([{"status": "ok", "value": "x" * 800} for _ in range(12)]
                      + [{"status": "failed", "error": "reload E_FAIL"}]),
            "next_action": "inspect rollback",
        }
        capped = ac.cap_tool_result(result)
        encoded = json.dumps(capped, ensure_ascii=False)
        self.assertIn("reload E_FAIL", encoded)
        self.assertIn("inspect rollback", encoded)
        self.assertFalse(ac.tool_succeeded(capped))

    def test_hmi_context_uses_exact_control_and_drops_page_body(self) -> None:
        result = {
            "status": "read",
            "project": "Demo",
            "file": "Desktop.view",
            "control_id": "RuntimeCaption",
            "content": "<div>" + ("x" * 50000) + "</div>",
            "content_included": True,
            "control_count": 1,
            "total_control_count": 79,
            "controls": [
                {"id": "Other", "type": "Rectangle", "attributes": {"data-tchmi-left": "0"}},
                {"id": "RuntimeCaption", "type": "Textblock", "attributes": {
                    "data-tchmi-text": "ADS PLC1 · 192.168.1.4.1.1:851",
                }},
            ],
            "bindings": [
                {"control": "Other", "attribute": "data-tchmi-text", "expression": "%s%x%/s%"},
            ],
        }
        context = ac.tool_result_for_context(
            "tc_hmi_read", {"file": "Desktop.view", "control_id": "RuntimeCaption"}, result
        )
        self.assertNotIn("content", context)
        self.assertNotIn("content_preview", context)
        self.assertEqual(["RuntimeCaption"], [item["id"] for item in context["controls"]])
        self.assertLess(len(json.dumps(context, ensure_ascii=False)), 6000)

    def test_hmi_broad_context_requests_control_filter(self) -> None:
        controls = [
            {"id": f"Control{index}", "type": "Textblock", "attributes": {"large": "x" * 200}}
            for index in range(80)
        ]
        context = ac.tool_result_for_context("tc_hmi_read", {"file": "Desktop.view"}, {
            "status": "read", "file": "Desktop.view", "content": "x" * 20000,
            "control_count": 80, "controls": controls, "bindings": [],
        })
        self.assertGreater(len(context["controls"]), 24)
        self.assertEqual(len(context['controls']), context['next_control_offset'])
        self.assertIn("control_id", context["narrowing_hint"])
        self.assertLess(len(json.dumps(context, ensure_ascii=False)), 6000)

    def test_request_router_limits_hmi_turn_without_hiding_hmi_tools(self) -> None:
        categories = backend._auto_tool_categories("帮我把 HMI 的目标 PLC 显示修改一下")
        self.assertIn("HMI", categories)
        self.assertIn("目标", categories)
        self.assertNotIn("代码", categories)
        names = {item["name"] for item in ac.tools_schema(categories)}
        self.assertIn("tc_hmi_read", names)
        self.assertIn("tc_hmi_control_edit", names)
        self.assertIn("tc_target_show", names)
        self.assertNotIn("plc_write", names)

    def test_hmi_intent_profile_hides_unrelated_hmi_package_tools(self) -> None:
        request = "修改 HMI Main.view 的按钮事件"
        categories = backend._auto_tool_categories(request)
        names = backend._auto_tool_names(request, categories)
        schemas = {item["name"] for item in backend._tool_schema(categories, names)}
        self.assertIn("tc_hmi_project_info", schemas)
        self.assertIn("tc_hmi_control_events", schemas)
        self.assertIn("tc_hmi_control_schema", schemas)
        self.assertNotIn("tc_hmi_framework_install", schemas)
        self.assertNotIn("plc_write", schemas)

    def test_hmi_binding_failure_routes_to_single_diagnostic_entry(self) -> None:
        request = "为什么 HMI 绑定不了 PLC 变量"
        categories = backend._auto_tool_categories(request)
        names = backend._auto_tool_names(request, categories)
        schemas = {item["name"] for item in backend._tool_schema(categories, names)}
        self.assertIn("tc_hmi_binding_diagnose", schemas)
        self.assertIn("tc_hmi_bind_plc", schemas)
        self.assertNotIn("tc_hmi_framework_install", schemas)

    def test_hmi_build_does_not_route_complete_plc_stack(self) -> None:
        categories = backend._auto_tool_categories("验证 HMI build 和浏览器页面")
        self.assertIn("HMI", categories)
        self.assertNotIn("代码", categories)
        self.assertNotIn("外部 MCP", categories)

    def test_equivalent_hmi_reads_share_duplicate_signature(self) -> None:
        first = backend._read_call_signature("tc_hmi_read", {
            "file": "Desktop.view", "max_chars": 200000,
        })
        second = backend._read_call_signature("tc_hmi_read", {
            "file": "desktop.view", "max_chars": 1200, "include_content": False,
        })
        focused = backend._read_call_signature("tc_hmi_read", {
            "file": "Desktop.view", "control_id": "RuntimeCaption",
        })
        self.assertNotEqual(first, second)
        self.assertNotEqual(first, focused)

    def test_plc_read_uses_larger_source_budget_without_middle_compression(self) -> None:
        result = {
            "name": "FB_Large",
            "area": "all",
            "declaration": "VAR\n" + ("    nValue : DINT;\n" * 180) + "END_VAR",
            "implementation": "nValue := nValue + 1;\n" * 420,
            "methods": [],
            "ranges": {},
        }
        self.assertGreater(
            len(json.dumps(result, ensure_ascii=False)), ac.TOOL_RESULT_CAP
        )
        context = ac.tool_result_for_context("plc_read", {"name": "FB_Large"}, result)
        self.assertEqual(result, context)
        self.assertNotIn("_capped", context)

    def test_plc_read_fast_uses_the_same_contiguous_source_budget(self) -> None:
        result = {"status": "read", "results": [{
            "name": "MAIN", "implementation": "x := x + 1;\n" * 1000,
        }]}
        self.assertGreater(len(json.dumps(result, ensure_ascii=False)), ac.TOOL_RESULT_CAP)
        context = ac.tool_result_for_context(
            "plc_read_fast", {"requests": [{"name": "MAIN"}]}, result
        )
        self.assertEqual(result, context)
        self.assertNotIn("_capped", context)

    def test_oversized_plc_read_keeps_full_source(self) -> None:
        result = {
            "name": "FB_Huge",
            "path": "TIPC^PLC^POUs^FB_Huge",
            "area": "all",
            "declaration": "x : DINT;\n" * 2000,
            "implementation": "x := x + 1;\n" * 3000,
            "methods": [{"name": "Run", "implementation": "large" * 10000}],
            "ranges": {},
        }
        context = ac.tool_result_for_context(
            "plc_read", {"name": "FB_Huge", "path": result["path"]}, result
        )
        self.assertNotIn("_pagination_required", context)
        self.assertNotIn("_capped", context)
        self.assertEqual(result['declaration'], context['declaration'])
        self.assertEqual(result['implementation'], context['implementation'])
        self.assertEqual(result['methods'], context['methods'])

    def test_provider_wire_format_does_not_retruncate_bounded_tool_results(self) -> None:
        payload = {"implementation": "x := x + 1;\n" * 500}
        history = [
            {"role": "assistant", "text": "", "tool_calls": [
                {"id": "read1", "name": "plc_read", "args": {"name": "MAIN"}}
            ]},
            {"role": "tool", "id": "read1", "name": "plc_read", "result": payload},
        ]
        openai = ac.OpenAIProvider("https://example.invalid", "key", "model")
        openai_content = openai._messages("system", history)[-1]["content"]
        anthropic = ac.AnthropicProvider("https://example.invalid", "key", "model")
        anthropic_content = anthropic._messages(history)[-1]["content"][0]["content"]
        expected = json.dumps(payload, ensure_ascii=False)
        self.assertEqual(expected, openai_content)
        self.assertEqual(expected, anthropic_content)
        self.assertGreater(len(expected), 4000)


class XaeDetectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_slots_are_recreated_for_a_new_event_loop(self) -> None:
        first = backend._worker_slots()
        backend.BACKGROUND_WORKER_LOOP = object()
        second = backend._worker_slots()
        self.assertIsNot(first, second)

    async def test_license_call_times_out_without_blocking_panel(self) -> None:
        def stuck_reader():
            time.sleep(0.15)
            return {"valid": True}

        with patch.object(backend, "LICENSE_CALL_TIMEOUT", 0.01):
            started = time.monotonic()
            result = await backend._license_call(stuck_reader)
        self.assertLess(time.monotonic() - started, 0.1)
        self.assertFalse(result["valid"])
        self.assertIn("System ID", result["message"])

    async def test_detect_solution_preserves_requested_4024_pid(self) -> None:
        with patch.object(
            _ps_bridge,
            "ps_com",
            return_value={"solution": r"C:\Project\Machine.sln", "pid": 4024},
        ) as call:
            solution, pid, error = await backend._detect_solution(
                4024, sticky=True, strict_pid=True
            )
        self.assertEqual(r"C:\Project\Machine.sln", solution)
        self.assertEqual(4024, pid)
        self.assertEqual("", error)
        self.assertEqual(4024, call.call_args.kwargs["preferPid"])
        self.assertTrue(call.call_args.kwargs["strictPid"])

    async def test_detect_solution_returns_actionable_com_error(self) -> None:
        with patch.object(_ps_bridge, "ps_com", side_effect=RuntimeError("ROT unavailable")):
            solution, pid, error = await backend._detect_solution(4024)
        self.assertEqual("", solution)
        self.assertEqual(4024, pid)
        self.assertIn("ROT unavailable", error)


class LicensingAdsTimeoutTests(unittest.TestCase):
    class _Function:
        def __init__(self, result=0):
            self.result = result
            self.calls = []

        def __call__(self, *args):
            self.calls.append(args)
            return self.result

    def test_ads_system_id_read_sets_bounded_timeout(self) -> None:
        class FakeDll:
            AdsPortOpenEx = LicensingAdsTimeoutTests._Function(7)
            AdsPortCloseEx = LicensingAdsTimeoutTests._Function(0)
            AdsSyncSetTimeoutEx = LicensingAdsTimeoutTests._Function(0)
            AdsGetLocalAddressEx = LicensingAdsTimeoutTests._Function(1)
            AdsSyncReadReqEx2 = LicensingAdsTimeoutTests._Function(1)

        fake = FakeDll()
        with patch.object(licensing, "_ads_dll_candidates", return_value=["fake.dll"]), \
             patch.object(licensing.ctypes, "WinDLL", return_value=fake):
            with self.assertRaises(licensing.DeviceFingerprintError):
                licensing._read_system_id_ads()
        self.assertEqual(
            fake.AdsSyncSetTimeoutEx.calls[0],
            (7, licensing._ADS_SYSTEM_ID_TIMEOUT_MS),
        )

    def test_4024_custom_root_license_directory_is_discovered(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "TwinCAT" / "3.1"
            license_dir = root / "Target" / "License"
            license_dir.mkdir(parents=True)
            request = license_dir / "Machine.tclrq"
            request.write_text("<TcLicenseInfo />", encoding="utf-8")
            with patch.dict("os.environ", {"TWINCAT3DIR": str(root)}):
                candidates = licensing._license_file_candidates()
        self.assertIn(request, candidates)

    def test_namespaced_license_request_system_id_is_read(self) -> None:
        expected = "447DAA40-52F5-DB2C-D3F8-3B1804726B7E"
        with tempfile.TemporaryDirectory() as temp:
            request = Path(temp) / "Machine.tclrq"
            request.write_text(
                '<TcLicenseInfo xmlns="urn:beckhoff"><SystemId>{'
                + expected
                + "}</SystemId></TcLicenseInfo>",
                encoding="utf-8",
            )
            with patch.object(
                licensing, "_license_file_candidates", return_value=[request]
            ):
                actual = licensing._read_system_id_license_file()
        self.assertEqual(expected, actual)


if __name__ == "__main__":
    unittest.main()
