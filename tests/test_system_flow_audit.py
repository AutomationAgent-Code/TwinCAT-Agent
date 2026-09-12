"""System audit: isolated contracts, not live PLC acceptance.

Regression tests for the defects recorded by the system audit.
"""
from copy import deepcopy
from unittest.mock import patch

import pytest

from tc_agent.authorization import (
    make_plan, context_matches, plan_allows_action, consume_action,
    explicit_report_actions,
)
from tc_agent.conversation_store import ConversationStore
from tc_agent.runtime import DurableRun, DurableAction, ActionRequest
from tc_agent import build_execution as be
from tc_template import _ps_bridge


def plan(actions=None):
    p = make_plan(thread_id="main", solution="C:/audit/A.sln", pid=42,
                  process_identity=[42, 100], target_netid="1.2.3.4.1.1",
                  runtimes=[{"name": "PLC1", "ads_port": 851}],
                  backend_session_id="audit",
                  actions=actions or [{"name": "tc_login", "args": {"runtime": "PLC1"}}])
    p["status"] = "authorized"
    p["approved_actions"] = deepcopy(p["actions"])
    return p


@pytest.mark.parametrize("field,value", [
    ("thread_id", "other"), ("solution", "C:/audit/B.sln"), ("pid", 43),
    ("process_identity", [42, 101]), ("target_netid", "other"),
    ("runtimes", [{"name": "PLC1", "ads_port": 852}]),
    ("backend_session_id", "restarted"),
])
def test_identity_changes_invalidate_approval(field, value):
    p = plan()
    current = deepcopy(p["context"])
    current[field] = value
    assert not context_matches(p, current)[0]


@pytest.mark.parametrize("status", ["pending", "expired", "invalidated", "failed", "completed"])
def test_non_authorized_states_cannot_execute(status):
    p = plan()
    p["status"] = status
    assert not plan_allows_action(p, "tc_login", {"runtime": "PLC1"})


def test_parameter_change_cannot_expand_approval():
    assert not plan_allows_action(plan(), "tc_login", {"runtime": "PLC2"})


def test_consumed_action_cannot_replay():
    p = consume_action(plan(), "tc_login", {"runtime": "PLC1"}, success=True)
    assert not plan_allows_action(p, "tc_login", {"runtime": "PLC1"})


def test_status_prose_is_not_authorization():
    assert explicit_report_actions("当前PLC状态已确认：已登录且处于Run模式。请确认授权以下验证计划。") == []


def test_online_plan_cannot_start_before_login():
    p = plan([{"name": "tc_online", "args": {"runtime": "PLC1"}}])
    assert not plan_allows_action(p, "tc_start", {"runtime": "PLC1"})


def test_one_execution_does_not_consume_two_identical_steps():
    action = {"name": "tc_login", "args": {"runtime": "PLC1"}}
    p = consume_action(plan([action, action]), "tc_login", action["args"], success=True)
    assert p["status"] == "authorized"


def new_action(store, thread_id, call):
    run = DurableRun(store, thread_id, "audit-only simulated write")
    step = run.start_model_step()
    run.complete_model_step({"usage": {}})
    return DurableAction(store, ActionRequest(run.run_id, step, call, "plc_write_value",
                         {"name": "MAIN.bEnable", "value": True}, readonly=False))


def test_uncertain_mutation_requires_reconciliation_across_runs(tmp_path):
    store = ConversationStore(tmp_path / "audit.db")
    tid = store.list_threads()[0]["id"]
    first = new_action(store, tid, "call-a")
    first.uncertain("simulated transport loss after dispatch")
    second = new_action(store, tid, "call-b")
    assert second.disposition != "execute"


def snapshots():
    return {
        "connect-check": {"pid": 42, "solution": "C:/audit/A.sln"},
        "project-info": {"solution": "C:/audit/A.sln", "project_count": 1,
                         "plc_projects": [{"name": "PLC1", "path": "PLC1.plcproj"}]},
        "plc-runtimes": {"target_netid": "1.2.3.4.1.1", "plcs": []},
        "build-state": {"status": "read", "build_state": 1, "busy": False,
                        "last_build_info": 0, "commands": {
                            "build": {"name": "Build.BuildSolution", "available": True},
                            "rebuild": {"name": "Build.RebuildSolution", "available": True}}},
    }


def test_mixed_solution_snapshot_is_rejected():
    responses = snapshots()
    responses["project-info"]["solution"] = "C:/audit/B.sln"
    responses["build-state"]["solution"] = "C:/audit/B.sln"
    with patch.object(be, "ps_com", side_effect=lambda cmd, **kw: responses[cmd]), _ps_bridge.tool_target(42):
        result = be.capture_build_state({"action": "build"})
    assert result.get("verified") is not True


def test_blocked_build_must_not_be_reported_performed():
    responses = snapshots()
    with patch.object(be, "ps_com", side_effect=lambda cmd, **kw: responses[cmd]), \
         patch("tc_template._ps_bridge.com_build", return_value={
             "status": "blocked", "buildPerformed": False, "not_executed": True}), \
         _ps_bridge.tool_target(42):
        p = be.capture_build_state({"action": "build"})
        result = be.execute_plc_build({"action": "build", "build_plan_token": p["build_plan_token"]})
    assert result["buildPerformed"] is False


def test_preflight_does_not_consume_build_token():
    responses = snapshots()
    with patch.object(be, "ps_com", side_effect=lambda cmd, **kw: responses[cmd]), _ps_bridge.tool_target(42):
        p = be.capture_build_state({"action": "build"})
        args = {"action": "build", "build_plan_token": p["build_plan_token"]}
        assert be.verify_build_plan(args) is None
        assert be.verify_build_plan(args) is None


def test_partial_execution_report_preserves_completed_write(tmp_path):
    from tc_agent.runtime import execution_failure_report
    store = ConversationStore(tmp_path / "audit.db")
    tid = store.list_threads()[0]["id"]
    first = new_action(store, tid, "enable")
    first.finish({"value": True}, ok=True)
    report = execution_failure_report(store, first.request.run_id, "恢复动作未授权")
    assert "已执行" in report and '"value":true' in report
    assert "恢复未获执行证据" in report


def test_uncertain_transport_result_is_persisted(tmp_path):
    store = ConversationStore(tmp_path / "audit.db")
    first = new_action(store, store.list_threads()[0]["id"], "lost")
    first.finish({"status": "uncertain", "error": "timeout"}, ok=False)
    assert first.execution["status"] == "uncertain"


def test_identical_steps_are_consumed_one_at_a_time():
    action = {"name": "tc_login", "args": {"runtime": "PLC1"}}
    p = plan([action, action])
    p = consume_action(p, action["name"], action["args"], success=True)
    assert plan_allows_action(p, action["name"], action["args"])
    p = consume_action(p, action["name"], action["args"], success=True)
    assert p["status"] == "completed"


def test_partial_approval_cannot_skip_predecessor():
    from tc_agent.authorization import select_actions, AuthorizationError
    p = plan([{"name": "tc_online", "args": {"runtime": "PLC1"}}])
    with pytest.raises(AuthorizationError):
        select_actions(p, [p["actions"][1]["key"]])


def test_known_completed_write_does_not_block_new_authorized_run(tmp_path):
    store = ConversationStore(tmp_path / "audit.db")
    tid = store.list_threads()[0]["id"]
    first = new_action(store, tid, "done")
    first.finish({"value": True}, ok=True)
    assert new_action(store, tid, "next").disposition == "execute"


def test_mcp_failure_after_dispatch_is_unknown():
    from tc_agent.mcp_client import McpManager
    from types import SimpleNamespace
    manager = McpManager()
    manager._tools["test"] = {"server_id": "s", "server_name": "fixture", "remote_name": "write"}
    def lost(*args):
        raise TimeoutError("simulated response loss")
    manager._clients["s"] = SimpleNamespace(tools=[], call=lost)
    result = manager.call("test", {})
    assert result["uncertain"] and result["not_executed"] is False


def test_preflight_rejects_state_change_inside_lock():
    responses = snapshots()
    calls = 0
    def read(cmd, **kw):
        nonlocal calls
        if cmd == "build-state":
            calls += 1
            if calls == 3:
                return {**responses[cmd], "busy": True}
        return responses[cmd]
    with patch.object(be, "ps_com", side_effect=read), \
         patch("tc_template._ps_bridge.com_build") as dispatch, _ps_bridge.tool_target(42):
        p = be.capture_build_state({"action": "build"})
        result = be.execute_plc_build({"action": "build", "build_plan_token": p["build_plan_token"]})
    assert result["not_executed"]
    dispatch.assert_not_called()


@pytest.mark.parametrize("states,expected", [([3] * 12, "uncertain"), ([2, 3], "completed")])
def test_rebuild_does_not_use_previous_done(states, expected):
    import ast
    from pathlib import Path
    from types import SimpleNamespace
    tree = ast.parse((Path(__file__).parents[1] / "tc_template/_native_bridge.py").read_text(encoding="utf-8-sig"))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_build")
    ns = {"retry_com_busy": lambda fn, **kw: fn(), "time": SimpleNamespace(sleep=lambda _: None)}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "_build", "exec"), ns)
    class Build:
        LastBuildInfo = 0
        @property
        def BuildState(self):
            return states.pop(0) if len(states) > 1 else states[0]
    dte = SimpleNamespace(Solution=SimpleNamespace(SolutionBuild=Build()), ExecuteCommand=lambda _: None)
    result = ns["_build"](dte, {"action": "rebuild"})
    if expected == "uncertain":
        assert result["uncertain"] and result["compiler_verified"] is False
    else:
        assert result["buildPerformed"] and not result.get("uncertain")
