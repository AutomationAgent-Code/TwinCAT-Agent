from concurrent.futures import ThreadPoolExecutor
import threading
from unittest.mock import Mock, patch

from tc_agent import backend
from tc_agent.execution_policy import ReadPolicy, tool_succeeded
from tc_agent.mcp_client import McpManager, _result_payload


def test_saved_read_deduplicates_only_successful_results():
    policy = ReadPolicy()
    args = {"file": "Desktop.view"}
    policy.record("tc_hmi_read", args, ok=False)
    assert not policy.duplicate("tc_hmi_read", args)
    policy.record("tc_hmi_read", args, ok=True)
    assert policy.duplicate("tc_hmi_read", args)
    policy.invalidate()
    assert not policy.duplicate("tc_hmi_read", args)


def test_mcp_and_build_failures_stay_failed_even_when_summarized():
    assert not tool_succeeded({"ok": False, "errors": ["build failed"]})
    assert not tool_succeeded({"isError": True, "content": []})
    assert not tool_succeeded(_result_payload({"isError": True, "content": ["x" * 40000]}))
    assert tool_succeeded({"ok": True, "warnings": ["advisory"]})
    assert tool_succeeded({"isError": False, "content": []})


def test_online_reads_builds_and_refreshes_are_never_deduplicated():
    policy = ReadPolicy()
    for name, args in [
        ("plc_read_smart", {"live": True}), ("plc_read_fast", {"live": True}),
        ("plc_source_index", {"refresh": True}), ("plc_read_current", {}),
        ("plc_dirty_current", {}), ("tc_state", {}), ("plc_build", {}),
        ("ads_read", {"symbol": "MAIN.nValue"}), ("tc_hmi_ads_live_check", {}),
    ]:
        policy.record(name, args, ok=True)
        assert not policy.duplicate(name, args)


def test_focused_or_paginated_reads_are_progress():
    policy = ReadPolicy()
    policy.record("tc_hmi_read", {"file": "Desktop.view", "max_chars": 1000}, ok=True)
    assert not policy.duplicate("tc_hmi_read", {"file": "desktop.view", "max_chars": 20000})
    assert not policy.duplicate("tc_hmi_read", {"file": "Desktop.view", "control_id": "Button"})
    policy.record("plc_read_smart", {"name": "MAIN", "start_line": 1}, ok=True)
    assert not policy.duplicate("plc_read_smart", {"name": "MAIN", "start_line": 101})


def test_narrowed_routes_only_add_auxiliary_catalogs_on_explicit_intent():
    categories = backend._auto_tool_categories("PLC function block")
    assert "代码" in categories
    assert {"文档", "版本", "外部 MCP"}.isdisjoint(categories)
    assert "外部 MCP" in backend._auto_tool_categories("template MCP")
    assert "文档" in backend._auto_tool_categories("查官方文档")
    assert "版本" in backend._auto_tool_categories("检查版本 diff")
    assert backend._auto_tool_categories("hello") == set()


def test_continue_inherits_hmi_context_but_new_task_does_not():
    messages = [{"role": "user", "text": "HMI theme"},
                {"role": "assistant", "text": "Continue?"}]
    assert "HMI" in backend._routing_categories("继续", messages)
    assert "HMI" not in backend._routing_categories("PLC function block", messages)
    assert "HMI" in backend._routing_categories("1", [], "HMI theme")


def test_mcp_failed_connection_uses_backoff_and_config_change_retries():
    manager = McpManager()
    client = Mock()
    client.connect.side_effect = RuntimeError("offline")
    cfg = {"id": "one", "command": "fixture", "enabled": True}
    with patch("tc_agent.mcp_client._client_for", return_value=client), \
            patch("tc_agent.mcp_client.time.monotonic", return_value=100) as clock:
        manager.refresh([cfg])
        manager.refresh([cfg])
        assert client.connect.call_count == 1
        clock.return_value = 131
        manager.refresh([cfg])
        assert client.connect.call_count == 2
        clock.return_value = 160
        manager.refresh([cfg])
        assert client.connect.call_count == 2
        manager.refresh([{**cfg, "command": "updated"}])
        assert client.connect.call_count == 3
        manager.refresh([cfg], force=True)
        assert client.connect.call_count == 4
    manager.close_all()


def test_mcp_catalog_remains_accessible_during_connection():
    manager = McpManager()
    entered, release = threading.Event(), threading.Event()
    client = Mock(tools=[])

    def connect():
        entered.set()
        assert release.wait(3)
        return []

    client.connect.side_effect = connect
    with patch("tc_agent.mcp_client._client_for", return_value=client), \
            ThreadPoolExecutor(max_workers=2) as pool:
        task = pool.submit(manager.refresh, [{"id": "one", "command": "fixture"}])
        try:
            assert entered.wait(2)
            assert pool.submit(manager.tool_catalog).result(timeout=1) == []
        finally:
            release.set()
            task.result(timeout=3)
    manager.close_all()


def test_removing_failed_server_clears_retry_state():
    manager = McpManager()
    client = Mock()
    client.connect.side_effect = RuntimeError("offline")
    cfg = {"id": "one", "command": "fixture"}
    with patch("tc_agent.mcp_client._client_for", return_value=client):
        manager.refresh([cfg])
        manager.refresh([])
        manager.refresh([cfg])
        assert client.connect.call_count == 2
    manager.close_all()
