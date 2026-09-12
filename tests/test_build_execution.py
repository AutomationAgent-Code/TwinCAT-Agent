from unittest.mock import patch

from tc_agent import agent_core as ac
from tc_agent import build_execution as be
from tc_template import _ps_bridge


def _build_state(*, busy=False, last_build_info=0, build=True, rebuild=True):
    return {
        "status": "read",
        "build_state": 1 if not busy else 2,
        "last_build_info": last_build_info,
        "busy": busy,
        "commands": {
            "build": {"name": "Build.BuildSolution", "available": build},
            "rebuild": {"name": "Build.RebuildSolution", "available": rebuild},
        },
    }


def _raw_good_build():
    return {
        "buildPerformed": True, "failedProjects": 0, "errorCount": 0,
        "errors": [], "warnings": [], "diagnosticsAvailable": True,
    }


def _read_only_snapshot(state=None):
    state = state or _build_state()
    return {
        "connect-check": {"pid": 42, "solution": r"C:\Machine\Machine.sln"},
        "build-state": state,
        "project-info": {"solution": r"C:\Machine\Machine.sln",
                          "project_count": 1,
                          "plc_projects": [{"name": "PLC1", "path": "PLC1.plcproj"}]},
        "plc-runtimes": {"target_netid": "127.0.0.1.1.1", "plcs": []},
    }


def test_build_is_effectful_and_diagnostics_preflight_remain_readonly():
    assert ac.tool_metadata("plc_build")["readonly"] is False
    assert ac.tool_metadata("plc_verify")["readonly"] is False
    assert ac.tool_metadata("plc_diagnostics")["readonly"] is True
    assert ac.tool_metadata("plc_preflight")["readonly"] is True
    assert ac.decide("auto", "plc_build") == "ask"
    allowed, reason = ac.gate("auto", "plc_build", {
        "action": "build", "build_plan_token": "x"}, None)
    assert not allowed and "审批" in reason
    assert ac.decide("auto", "plc_diagnostics") == "allow"


def test_status_issues_exact_tokens_from_actual_state_only():
    calls = []
    responses = _read_only_snapshot()

    def fake_ps(command, **kwargs):
        calls.append(command)
        return responses[command]

    with patch.object(be, "ps_com", side_effect=fake_ps):
        with _ps_bridge.tool_target(42):
            result = be.capture_build_state({"action": "build"})
    assert result["status"] == "ready"
    assert result["action"] == "build"
    assert result["build_plan_token"]
    assert set(calls) == {"connect-check", "build-state", "project-info", "plc-runtimes"}
    assert "logout" not in calls and "stop" not in calls


def test_missing_or_stale_token_never_calls_com_build():
    responses = _read_only_snapshot()
    with patch.object(be, "ps_com", side_effect=lambda command, **kwargs: responses[command]), \
         patch("tc_template._ps_bridge.com_build") as build:
        with _ps_bridge.tool_target(42):
            missing = be.execute_plc_build({"action": "build"})
    assert missing["not_executed"] is True
    build.assert_not_called()

    with patch.object(be, "ps_com", side_effect=lambda command, **kwargs: responses[command]), \
         patch("tc_template._ps_bridge.com_build") as build:
        with _ps_bridge.tool_target(42):
            plan = be.capture_build_state({"action": "build"})
            responses["build-state"] = _build_state(last_build_info=7)
            stale = be.execute_plc_build({
                "action": "build", "build_plan_token": plan["build_plan_token"]})
    assert stale["status"] == "conflict"
    assert stale["build_performed"] is False
    build.assert_not_called()


def test_approved_token_executes_one_action_and_reads_post_state():
    responses = _read_only_snapshot()
    calls = []

    def fake_ps(command, **kwargs):
        calls.append(command)
        return responses[command]

    with patch.object(be, "ps_com", side_effect=fake_ps), \
         patch("tc_template._ps_bridge.com_build", return_value=_raw_good_build()) as build:
        with _ps_bridge.tool_target(42):
            plan = be.capture_build_state({"action": "rebuild"})
            result = be.execute_plc_build({
                "action": "rebuild", "build_plan_token": plan["build_plan_token"]})
    assert result["compiler_verified"] is True
    assert result["verification_status"] == "build_result_and_post_state_read"
    build.assert_called_once_with(always_read_errors=True, action="rebuild")
    assert calls.count("build-state") == 4  # status, preflight, locked recheck, post-state
    assert "logout" not in calls and "stop" not in calls


def test_agent_run_tool_rejects_build_without_status_token():
    result = ac.run_tool("plc_build", {"action": "build"})
    assert result["not_executed"] is True
    assert result["error_type"] == "tool_arguments"


def test_build_status_context_preserves_opaque_tokens():
    responses = _read_only_snapshot()
    with patch.object(be, "ps_com", side_effect=lambda command, **kwargs: responses[command]):
        with _ps_bridge.tool_target(42):
            status = be.capture_build_state({})
    context = ac.tool_result_for_context("plc_build_status", {}, status)
    assert context["plans"]["build"]["build_plan_token"] == status["plans"]["build"]["build_plan_token"]
    assert context["plans"]["rebuild"]["build_plan_token"] == status["plans"]["rebuild"]["build_plan_token"]
