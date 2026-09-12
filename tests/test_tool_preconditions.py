from hashlib import sha256
from unittest.mock import Mock, patch

from tc_agent import agent_core as ac
import pytest


@pytest.mark.parametrize('name', ['tc_restart', 'tc_activate', 'tc_config_mode', 'tc_run_mode'])
@pytest.mark.parametrize('target', ['192.168.1.4.1.1', ''])
def test_system_transition_requires_target_not_plc_inventory(name, target):
    tool, original, handler = _replace_handler(name)
    try:
        def com(command, **kwargs):
            if command == 'connect-check':
                return {'pid':15232, 'solution':r'C:\Test\Test.sln'}
            if command == 'target-show':
                return {'target_netid':target}
            raise AssertionError('Unexpected call (must not read PLC inventory or restart): '+command)
        with patch.object(ac, 'ps_com', side_effect=com) as calls:
            result = ac.run_tool(name, {}, prefer_pid=15232)
        assert 'plc-runtimes' not in [c.args[0] for c in calls.call_args_list]
        if target:
            assert result['status']=='handler-called', result
            handler.assert_called_once()
        else:
            assert result['condition']=='target_identity', result
            assert result['not_executed']
            handler.assert_not_called()
        assert 'approval' in {p['condition'] for p in ac.tool_metadata(name)['preconditions']}
    finally:
        _restore_handler(tool, original)


def _replace_handler(name, result=None):
    tool = ac._BY_NAME[name]
    original = tool["run"]
    handler = Mock(return_value=result or {"status": "handler-called"})
    tool["run"] = handler
    return tool, original, handler


def _restore_handler(tool, original):
    tool["run"] = original


def test_all_target_only_contracts_skip_plc_selection():
    checked = []
    for name, tool in ac._BY_NAME.items():
        conditions = {p['condition'] for p in (tool.get('contract') or {}).get('preconditions', [])}
        if 'target_identity' not in conditions or conditions & {'ads_endpoint','runtime_selection','plc_runtime_run'}:
            continue
        def com(command, **kwargs):
            if command == 'connect-check':
                return {'pid':15232,'solution':r'C:\Test\Test.sln'}
            if command == 'target-show':
                return {'target_netid':'192.168.1.4.1.1'}
            raise AssertionError((name,command))
        with patch.object(ac,'ps_com',side_effect=com):
            result=ac._tool_precondition_failure(name,{},15232,tool)
        assert result is None, (name,result)
        checked.append(name)
    assert 'tc_restart' in checked and 'tc_activate' in checked


def test_registry_exposes_distinct_precondition_contracts():
    source = ac.tool_metadata("plc_write")["preconditions"]
    value = ac.tool_metadata("plc_write_value")["preconditions"]
    transition = ac.tool_metadata("tc_online")["preconditions"]
    build = ac.tool_metadata("plc_build")["preconditions"]
    build_status = ac.tool_metadata("plc_build_status")["preconditions"]

    assert {item["condition"] for item in source} >= {
        "xae_identity", "plc_object_scope", "source_editor_state",
        "source_conflict", "code_review_gate", "approval",
    }
    assert "plc_runtime_run" in {item["condition"] for item in value}
    assert "source_editor_state" not in {item["condition"] for item in value}
    assert {item["condition"] for item in transition} >= {
        "xae_identity", "runtime_selection", "transition_state", "approval",
    }
    assert {item["condition"] for item in build} >= {
        "project_binding", "build_mutex", "source_freshness", "approval",
    }
    assert "approval" not in {item["condition"] for item in build_status}
    schema = {item["name"]: item for item in ac.tools_schema()}
    assert "前置检查" in schema["plc_write"]["description"]


def test_plc_run_does_not_mean_editor_online_and_does_not_block_source_write():
    tool, original, handler = _replace_handler("plc_write")
    try:
        context = {
            "pid": 101,
            "solution": r"C:\Machine\Machine.sln",
            "active_document": {
                "full_name": r"C:\Machine\Other.TcPOU",
                "saved": True,
                "editor_online": False,
            },
            "plc_runtime": {"state_name": "Run"},
        }
        with patch.object(ac, "ps_com", side_effect=lambda command, **kw:
                          {'logged_in':False} if command=='plc-online-state' else
                          {'plcs':[{'name':'PLC'}]} if command=='plc-runtimes' else context):
            result = ac.run_tool(
                "plc_write",
                {"name": "FB_Main", "area": "implementation", "code": "x := 1;"},
                prefer_pid=101,
            )
        assert result == {"status": "handler-called"}
        handler.assert_called_once()
    finally:
        _restore_handler(tool, original)


def test_unsaved_target_editor_is_a_conflict_without_save_or_discard():
    tool, original, handler = _replace_handler("plc_write")
    try:
        context = {
            "pid": 101,
            "solution": r"C:\Machine\Machine.sln",
            "active_document": {
                "full_name": r"C:\Machine\FB_Main.TcPOU",
                "saved": False,
            },
        }
        with patch.object(ac, "ps_com", return_value=context) as com:
            result = ac.run_tool(
                "plc_write",
                {"name": "FB_Main", "area": "implementation", "code": "x := 1;"},
                prefer_pid=101,
            )
        assert result["status"] == "conflict"
        assert result["condition"] == "source_editor_state"
        assert result["not_executed"] is True
        assert "save" in result["next_action"] or "保存" in result["next_action"]
        handler.assert_not_called()
        assert [call.args[0] for call in com.call_args_list] == ["connect-check"]
    finally:
        _restore_handler(tool, original)


def test_unknown_xae_identity_is_structured_and_has_no_execution():
    tool, original, handler = _replace_handler("plc_write")
    try:
        with patch.object(ac, "ps_com", side_effect=RuntimeError("XAE unavailable")) as com:
            result = ac.run_tool(
                "plc_write",
                {"name": "FB_Main", "area": "implementation", "code": "x := 1;"},
                prefer_pid=101,
            )
        assert result["status"] == "unavailable"
        assert result["error_type"] == "tool_precondition"
        assert result["condition"] == "xae_identity"
        assert result["not_executed"] is True
        handler.assert_not_called()
        com.assert_called_once_with("connect-check")
    finally:
        _restore_handler(tool, original)


def test_unknown_target_document_save_state_is_not_treated_as_offline():
    tool, original, handler = _replace_handler("plc_write")
    try:
        context = {
            "pid": 101,
            "solution": r"C:\Machine\Machine.sln",
            "active_document": {
                "full_name": r"C:\Machine\FB_Main.TcPOU",
                "saved": None,
            },
        }
        with patch.object(ac, "ps_com", return_value=context):
            result = ac.run_tool(
                "plc_write",
                {"name": "FB_Main", "area": "implementation", "code": "x := 1;"},
                prefer_pid=101,
            )
        assert result["status"] == "unknown"
        assert result["condition"] == "source_editor_state"
        assert result["not_executed"] is True
        handler.assert_not_called()
    finally:
        _restore_handler(tool, original)


def test_source_revision_mismatch_blocks_before_handler():
    tool, original, handler = _replace_handler("plc_patch")
    try:
        live = "x := 2;"
        context = {
            "pid": 101,
            "solution": r"C:\Machine\Machine.sln",
            "active_document": {"full_name": r"C:\Machine\Other.TcPOU", "saved": True},
        }

        def com(command, **kwargs):
            if command == "connect-check":
                return context
            if command == "read-pou":
                return {"implementation": live, "declaration": ""}
            raise AssertionError(f"unexpected COM command: {command}")

        with patch.object(ac, "ps_com", side_effect=com):
            result = ac.run_tool(
                "plc_patch",
                {
                    "name": "FB_Main", "area": "implementation", "old_text": "x := 1;",
                    "new_text": "x := 3;",
                    "expected_source_hash": sha256(b"x := 1;").hexdigest(),
                },
                prefer_pid=101,
            )
        assert result["status"] == "conflict"
        assert result["condition"] == "source_conflict"
        assert result["not_executed"] is True
        handler.assert_not_called()
    finally:
        _restore_handler(tool, original)


def test_multi_plc_requires_explicit_runtime_and_never_guesses_a_port():
    tool, original, handler = _replace_handler("plc_write_value")
    try:
        def com(command, **kwargs):
            if command == "target-show":
                return {"target_netid": "5.6.7.8.1.1"}
            if command == "plc-runtimes":
                return {"plcs": [
                    {"name": "PLC1", "ads_port": 851},
                    {"name": "PLC2", "ads_port": 852},
                ]}
            raise AssertionError(f"unexpected COM command: {command}")

        with patch.object(ac, "ps_com", side_effect=com) as com_mock:
            result = ac.run_tool("plc_write_value", {"name": "GVL.x", "value": 1})
        assert result["status"] == "blocked"
        assert result["condition"] == "runtime_selection"
        assert result["not_executed"] is True
        handler.assert_not_called()
        assert [call.args[0] for call in com_mock.call_args_list] == ["target-show", "plc-runtimes"]
    finally:
        _restore_handler(tool, original)


def test_ads_write_checks_runtime_run_but_does_not_require_editor_state():
    tool, original, handler = _replace_handler("plc_write_value")
    try:
        def com(command, **kwargs):
            if command == "target-show":
                return {"target_netid": "5.6.7.8.1.1"}
            if command == "plc-runtimes":
                return {"plcs": [{"name": "PLC1", "ads_port": 851}]}
            raise AssertionError(f"unexpected COM command: {command}")

        with patch.object(ac, "ps_com", side_effect=com), \
             patch.object(ac, "read_ads_state", return_value={"state_code": 5, "state_name": "Run"}):
            result = ac.run_tool("plc_write_value", {"name": "GVL.x", "value": 1})
        assert result == {"status": "handler-called"}
        handler.assert_called_once()
    finally:
        _restore_handler(tool, original)


def test_ads_write_in_stop_is_blocked_without_hidden_transition():
    tool, original, handler = _replace_handler("plc_write_value")
    try:
        calls = []

        def com(command, **kwargs):
            calls.append(command)
            if command == "target-show":
                return {"target_netid": "5.6.7.8.1.1"}
            if command == "plc-runtimes":
                return {"plcs": [{"name": "PLC1", "ads_port": 851}]}
            raise AssertionError(f"unexpected COM command: {command}")

        with patch.object(ac, "ps_com", side_effect=com), \
             patch.object(ac, "read_ads_state", return_value={"state_code": 4, "state_name": "Stop"}):
            result = ac.run_tool("plc_write_value", {"name": "GVL.x", "value": 1})
        assert result["status"] == "blocked"
        assert result["condition"] == "plc_runtime_run"
        assert result["not_executed"] is True
        handler.assert_not_called()
        assert not {"login", "logout", "start", "stop", "config", "restart"} & set(calls)
    finally:
        _restore_handler(tool, original)


def test_target_selection_is_not_blocked_by_missing_current_target():
    tool, original, handler = _replace_handler("tc_target_set")
    try:
        with patch.object(ac, "ps_com", return_value={"pid": 101, "solution": ""}):
            result = ac.run_tool("tc_target_set", {"target": "CX-01"})
        assert result == {"status": "handler-called"}
        handler.assert_called_once()
    finally:
        _restore_handler(tool, original)
