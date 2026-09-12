from hashlib import sha256
from unittest.mock import Mock, patch

import pytest

from tc_agent import agent_core as ac
from tc_agent.execution_policy import tool_succeeded, gate_rejected
from tc_agent.tool_preconditions import precondition_failure


@pytest.mark.parametrize("status", ["conflict", "unknown", "unavailable", "unsupported", "invalid_arguments", "verification_failed"])
def test_explicit_failure_status_is_never_success(status):
    assert not tool_succeeded({"status": status, "not_executed": True})


def test_precondition_has_error_and_blocks_dependent_batch():
    result = precondition_failure(status="conflict", condition="source_editor_state",
        expected={}, actual={}, scope={}, reason="dirty", next_action="read live")
    assert result["error"] == "dirty"
    assert not tool_succeeded(result) and gate_rejected(result)
    assert tool_succeeded({"status": "preview", "not_executed": True})


@pytest.mark.parametrize("scenario", ["matching", "stale", "partial", "missing", "truncated"])
def test_dirty_editor_requires_complete_matching_live_baseline(scenario):
    text = {"declaration": "METHOD M : BOOL", "implementation": "M := TRUE;"}
    hashes = {key: sha256(value.encode()).hexdigest() for key, value in text.items()}
    if scenario == "stale":
        hashes["implementation"] = "0" * 64
    if scenario == "partial":
        hashes.pop("declaration")
    args = {"name": "FB_Main", "method": "M", "path": "TIPC^PLC^POUs^FB_Main",
            "area": "implementation", "code": "M := FALSE;"}
    if scenario != "missing":
        args["expected_source_hashes"] = hashes
    context = {"pid": 101, "solution": "C:/Fixture.sln",
               "active_document": {"full_name": "C:/FB_Main.TcPOU", "saved": False}}
    calls = []
    def com(command, **kwargs):
        calls.append(command)
        if command == "connect-check":
            return context
        if command == "plc-online-state":
            return {'logged_in':False}
        assert command == "read-pou" and kwargs["method"] == "M"
        return {**text, "implementation_paging": {"truncated": scenario == "truncated"}}
    handler = Mock(return_value={"status": "written", "written": True})
    with patch.dict(ac._BY_NAME["plc_write"], {"run": handler}), patch.object(ac, "ps_com", side_effect=com):
        result = ac.run_tool("plc_write", args, prefer_pid=101)
    if scenario == "matching":
        handler.assert_called_once()
    else:
        handler.assert_not_called()
        assert not tool_succeeded(result) and result["not_executed"] is True
    assert set(calls) <= {"connect-check", "read-pou", "plc-online-state"}


def test_live_read_hashes_exclude_truncated_text():
    handler = Mock(return_value={"declaration": "METHOD M", "implementation": "partial",
                                "implementation_paging": {"truncated": True}})
    with patch.dict(ac._BY_NAME["plc_read"], {"run": handler}), \
         patch.object(ac, "_tool_precondition_failure", return_value=None):
        result = ac.run_tool("plc_read", {"name": "FB_Main", "method": "M"})
    assert result["source_hashes"] == {"declaration": sha256(b"METHOD M").hexdigest()}


def test_last_page_is_not_a_complete_write_baseline():
    with patch.dict(ac._BY_NAME["plc_read"], {"run": Mock(return_value={
        "implementation": "last line", "implementation_paging": {"start_line": 20, "truncated": False}})}), \
         patch.object(ac, "_tool_precondition_failure", return_value=None):
        result = ac.run_tool("plc_read", {"name": "FB_Main", "start_line": 20})
    assert result["source_hashes"] == {}
