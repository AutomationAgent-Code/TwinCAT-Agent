"""Regression coverage for the user-configured third-party MCP bridge."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

from tc_agent import config
from tc_agent import backend
from tc_agent.mcp_client import MCP_MANAGER, MCP_TOOL_CATEGORY, McpManager


ROOT = Path(__file__).resolve().parents[1]

_FAKE_MCP_SERVER = r'''
import json
import sys

for raw in sys.stdin:
    request = json.loads(raw)
    method = request.get("method")
    request_id = request.get("id")
    if request_id is None:
        continue
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {},
                  "serverInfo": {"name": "fixture", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "template_find", "description": "Find a PLC template",
                    "inputSchema": {"type": "object", "properties": {
                        "query": {"type": "string"}}, "required": ["query"]}}]}
    elif method == "tools/call":
        params = request.get("params") or {}
        result = {"content": [{"type": "text", "text": "found:" +
                  str((params.get("arguments") or {}).get("query", ""))}]}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}), flush=True)
'''


def _server(**overrides):
    server = {
        "id": "templates",
        "name": "我的 PLC 模板库",
        "transport": "stdio",
        "enabled": True,
        "command": sys.executable,
        "args": ["-u", "-c", _FAKE_MCP_SERVER],
        "cwd": "",
        "env": {"TEMPLATE_ROOT": r"D:\\PLC\\Templates"},
        "url": "",
        "headers": {},
        "timeout_seconds": 5,
    }
    server.update(overrides)
    return server


def test_mcp_config_hides_secrets_from_public_settings():
    migrated = config._migrate({"mcp_servers": [_server(headers={"Authorization": "Bearer secret"})]})
    public = config.public_settings(migrated)["mcp_servers"][0]
    assert public["has_env"] is True
    assert public["has_headers"] is True
    assert "env" not in public
    assert "headers" not in public
    assert "secret" not in str(public)


def test_mcp_config_rejects_invalid_http_url():
    try:
        config.normalize_mcp_server(_server(transport="streamable_http", command="", url="file:///tmp/mcp"))
    except ValueError as exc:
        assert "http/https" in str(exc)
    else:
        raise AssertionError("file URL must not be accepted for Streamable HTTP MCP")


def test_stdio_mcp_is_discovered_and_called():
    manager = McpManager()
    try:
        manager.refresh([_server()])
        schemas = manager.tool_schemas()
        assert len(schemas) == 1
        assert schemas[0]["name"] == "mcp_templates_template_find"
        assert "我的 PLC 模板库" in schemas[0]["description"]
        metadata = manager.metadata(schemas[0]["name"])
        assert metadata["category"] == MCP_TOOL_CATEGORY
        assert metadata["readonly"] is False
        assert metadata["danger"] == "external_mcp"
        result = manager.call(schemas[0]["name"], {"query": "cylinder"})
        assert result["content"][0]["text"] == "found:cylinder"
    finally:
        manager.close_all()


def test_external_mcp_requires_approval_in_every_execution_mode():
    name = "mcp_templates_template_find"
    with patch.object(MCP_MANAGER, "has_tool", side_effect=lambda tool: tool == name):
        assert backend._tool_decide("plan", name) == "deny"
        for mode in ("ask", "accept", "auto"):
            assert backend._tool_decide(mode, name) == "ask"


def test_web_ui_and_backend_expose_mcp_settings_messages():
    html = (ROOT / "tc_agent" / "static" / "index.html").read_text(encoding="utf-8")
    source = Path(backend.__file__).read_text(encoding="utf-8")
    assert 'id="mcpList"' in html
    assert 'type:"save_mcp_server"' in html
    assert 'type:"test_mcp_server"' in html
    assert "mcp_settings_saved" in html
    assert "save_mcp_server / set_mcp_server / delete_mcp_server / test_mcp_server" in source
    assert "MCP_MANAGER.refresh" in source
