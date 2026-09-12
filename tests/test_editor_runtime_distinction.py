from unittest.mock import Mock, patch

import pytest

from tc_agent import agent_core as ac
from tc_agent.execution_policy import tool_succeeded
from tc_agent.tool_preconditions import editor_login_failure
from tc_agent.tool_usage_contract import prompt_contract


@pytest.mark.parametrize("logged_in", [True, False, None])
def test_editor_probe_never_infers_runtime_state(logged_in):
    with patch.object(ac, "ps_com", return_value={
        "logged_in": logged_in, "operation_state": "Run", "name": "PLC1"}) as com:
        result = ac.run_tool("plc_editor_state", {"runtime": "PLC1"}, prefer_pid=123)
    com.assert_called_once_with("plc-online-state", runtime="PLC1")
    assert result["editor_logged_in"] is logged_in
    assert result["runtime_state"] == "not_queried"
    assert result["verified"] is (logged_in is not None)
    assert ac.tool_metadata("plc_editor_state")["readonly"] is True


def test_explicit_createchild_login_rejection_has_correct_recovery():
    error = "Cannot add an object because it affects a device you are currently logged into."
    result = editor_login_failure(error)
    assert result["condition"] == "plc_editor_login"
    assert not tool_succeeded(result)
    assert "tc_logout" in result["next_action"] and "审批" in result["next_action"]
    # A composite operation may already have performed earlier changes.
    assert "not_executed" not in result
    assert editor_login_failure("PLC Runtime is in Run") is None


def test_real_dispatch_classifies_login_rejection_without_transition():
    handler = Mock(side_effect=ac.TcComError(
        "Cannot add an object because it affects a device you are currently logged into."))
    with patch.dict(ac._BY_NAME["plc_create_member"], {"run": handler}), \
         patch.object(ac, "_tool_precondition_failure", return_value=None), \
         patch.object(ac, "ps_com", side_effect=AssertionError("No transition allowed")):
        result = ac.run_tool("plc_create_member", {"pou": "FB", "name": "M", "member_type": "method"})
    assert result["error_type"] == "plc_editor_online_conflict"


def test_prompt_distinguishes_all_three_state_dimensions():
    text = prompt_contract()
    for phrase in ("Run/Stop", "Login/Logout", "Run/Config", "Logout 不等于 Stop", "plc_editor_state"):
        assert phrase in text
