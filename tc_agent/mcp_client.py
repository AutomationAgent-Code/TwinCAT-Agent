"""Small, dependency-free MCP client used by the local Agent.

The Agent intentionally does not import a third-party MCP SDK.  A user may
connect a local template server through MCP stdio or a remote Streamable HTTP
server, while the rest of the Agent remains independent of that server.  All
external tools are treated as mutating/high-risk tools and must go through the
normal Agent approval path.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


MCP_PROTOCOL_VERSION = "2025-06-18"
MCP_CLIENT_NAME = "TwinCAT Agent"
MCP_TOOL_CATEGORY = "外部 MCP"
MCP_TOOL_DANGER = "external_mcp"
MAX_TOOLS_PER_SERVER = 256
MAX_TOOL_NAME_LENGTH = 64


class McpError(RuntimeError):
    """An MCP transport or JSON-RPC error safe to show in the UI."""


def _error_text(exc: BaseException) -> str:
    text = str(exc).replace("\r", " ").replace("\n", " ").strip()
    return text[:500] or type(exc).__name__


def _tool_schema(item: dict[str, Any]) -> dict[str, Any] | None:
    name = str(item.get("name") or "").strip()
    if not name or len(name) > 256:
        return None
    schema = item.get("inputSchema")
    if not isinstance(schema, dict):
        schema = item.get("input_schema")
    if not isinstance(schema, dict):
        schema = {"type": "object", "properties": {}}
    return {
        "name": name,
        "description": str(item.get("description") or "")[:4000],
        "input_schema": schema,
    }


def _result_payload(result: Any) -> Any:
    """Keep MCP results JSON-safe and bounded before they enter model history."""
    if not isinstance(result, dict):
        return {"result": str(result)[:30000]}
    payload = dict(result)
    try:
        encoded = json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        return {"result": str(payload)[:30000]}
    if len(encoded) <= 30000:
        return payload
    # Content is the usual large field returned by template/document servers.
    content = payload.get("content")
    if isinstance(content, list):
        payload["content"] = content[:32]
    return {"result": json.dumps(payload, ensure_ascii=False)[:30000],
            "isError": payload.get("isError") is True,
            "truncated": True}


class _StdioClient:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.process: subprocess.Popen[bytes] | None = None
        self._messages: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._write_lock = threading.RLock()
        self._next_id = 0
        self.tools: list[dict[str, Any]] = []

    def _start(self) -> None:
        if self.process is not None and self.process.poll() is None:
            return
        command = str(self.config.get("command") or "").strip()
        args = [str(value) for value in self.config.get("args") or []]
        if not command:
            raise McpError("stdio MCP 缺少 command")
        env = os.environ.copy()
        env.update({str(k): str(v) for k, v in (self.config.get("env") or {}).items()})
        cwd = str(self.config.get("cwd") or "").strip() or None
        try:
            self.process = subprocess.Popen(
                [command, *args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            raise McpError(f"启动 MCP 程序失败：{_error_text(exc)}") from exc
        threading.Thread(target=self._read_stdout, daemon=True,
                         name="tc-agent-mcp-stdout").start()
        threading.Thread(target=self._drain_stderr, daemon=True,
                         name="tc-agent-mcp-stderr").start()

    def _read_stdout(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        try:
            for line in process.stdout:
                if line.strip():
                    self._messages.put(("line", line))
        finally:
            self._messages.put(("eof", process.poll()))

    def _drain_stderr(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        # MCP servers are allowed to log diagnostics to stderr.  Drain it so
        # a noisy server cannot block itself; diagnostics are not sent to the
        # model and are deliberately not persisted.
        try:
            while process.stderr.read(4096):
                pass
        except OSError:
            pass

    def _send(self, payload: dict[str, Any]) -> None:
        process = self.process
        if process is None or process.poll() is not None or process.stdin is None:
            raise McpError("MCP stdio 进程已退出")
        raw = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            process.stdin.write(raw)
            process.stdin.flush()
        except OSError as exc:
            raise McpError(f"写入 MCP stdio 失败：{_error_text(exc)}") from exc

    def _request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._write_lock:
            self._start()
            self._next_id += 1
            request_id = self._next_id
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method,
                        "params": params or {}})
            deadline = time.monotonic() + float(self.config.get("timeout_seconds") or 15)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise McpError(f"MCP 请求超时：{method}")
                try:
                    kind, value = self._messages.get(timeout=max(0.05, remaining))
                except queue.Empty as exc:
                    raise McpError(f"MCP 请求超时：{method}") from exc
                if kind == "eof":
                    raise McpError("MCP stdio 进程提前退出")
                try:
                    message = json.loads(value.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if message.get("id") != request_id:
                    continue
                if message.get("error"):
                    error = message["error"]
                    raise McpError(f"MCP {method} 失败：{error.get('message') or error}")
                return message.get("result") or {}

    def connect(self) -> list[dict[str, Any]]:
        self._start()
        self._request("initialize", {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": MCP_CLIENT_NAME, "version": "1"},
        })
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        self.tools = self._list_tools()
        return self.tools

    def _list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor = None
        while len(tools) < MAX_TOOLS_PER_SERVER:
            params = {"cursor": cursor} if cursor else {}
            result = self._request("tools/list", params)
            page = result.get("tools") if isinstance(result, dict) else []
            for item in page if isinstance(page, list) else []:
                if isinstance(item, dict):
                    parsed = _tool_schema(item)
                    if parsed:
                        tools.append(parsed)
            cursor = result.get("nextCursor") if isinstance(result, dict) else None
            if not cursor:
                break
        self.tools = tools[:MAX_TOOLS_PER_SERVER]
        return self.tools

    def call(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        with self._write_lock:
            result = self._request("tools/call", {
                "name": tool_name, "arguments": arguments or {},
            })
            return _result_payload(result)

    def close(self) -> None:
        process = self.process
        self.process = None
        if process is None:
            return
        try:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass


class _HttpClient:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.session_id = ""
        self._lock = threading.RLock()
        self._next_id = 0
        self.tools: list[dict[str, Any]] = []

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = str(self.config.get("url") or "").strip()
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream",
                   "User-Agent": "TwinCAT-Agent-MCP/1"}
        headers.update({str(k): str(v) for k, v in (self.config.get("headers") or {}).items()})
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(
                request, timeout=float(self.config.get("timeout_seconds") or 15)
            ) as response:
                session = response.headers.get("Mcp-Session-Id")
                if session:
                    self.session_id = session
                raw = response.read(2 * 1024 * 1024)
                content_type = response.headers.get("Content-Type", "").lower()
        except (OSError, urllib.error.URLError, urllib.error.HTTPError) as exc:
            raise McpError(f"MCP HTTP 请求失败：{_error_text(exc)}") from exc
        if not raw:
            return {}
        if "text/event-stream" in content_type:
            data_lines: list[str] = []
            messages: list[dict[str, Any]] = []
            for line in raw.decode("utf-8", errors="replace").splitlines():
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
                elif not line.strip() and data_lines:
                    try:
                        parsed = json.loads("\n".join(data_lines))
                        if isinstance(parsed, dict):
                            messages.append(parsed)
                    except json.JSONDecodeError:
                        pass
                    data_lines = []
            if data_lines:
                try:
                    parsed = json.loads("\n".join(data_lines))
                    if isinstance(parsed, dict):
                        messages.append(parsed)
                except json.JSONDecodeError:
                    pass
            for message in messages:
                if message.get("id") == payload.get("id"):
                    if message.get("error"):
                        raise McpError(f"MCP HTTP 调用失败：{message['error']}")
                    return message.get("result") or {}
            raise McpError("MCP HTTP 未返回匹配的 JSON-RPC 响应")
        try:
            message = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise McpError("MCP HTTP 返回的不是有效 JSON") from exc
        if not isinstance(message, dict):
            raise McpError("MCP HTTP 返回格式无效")
        if message.get("error"):
            error = message["error"]
            raise McpError(f"MCP 请求失败：{error.get('message') or error}")
        return message.get("result") or {}

    def _request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._next_id += 1
        return self._post({"jsonrpc": "2.0", "id": self._next_id, "method": method,
                           "params": params or {}})

    def connect(self) -> list[dict[str, Any]]:
        with self._lock:
            self._request("initialize", {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": MCP_CLIENT_NAME, "version": "1"},
            })
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
            tools: list[dict[str, Any]] = []
            cursor = None
            while len(tools) < MAX_TOOLS_PER_SERVER:
                result = self._request("tools/list", {"cursor": cursor} if cursor else {})
                page = result.get("tools") if isinstance(result, dict) else []
                for item in page if isinstance(page, list) else []:
                    if isinstance(item, dict):
                        parsed = _tool_schema(item)
                        if parsed:
                            tools.append(parsed)
                cursor = result.get("nextCursor") if isinstance(result, dict) else None
                if not cursor:
                    break
            self.tools = tools[:MAX_TOOLS_PER_SERVER]
            return self.tools

    def call(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        with self._lock:
            return _result_payload(self._request("tools/call", {
                "name": tool_name, "arguments": arguments or {},
            }))

    def close(self) -> None:
        self.session_id = ""


def _client_for(config: dict[str, Any]) -> _StdioClient | _HttpClient:
    if config.get("transport") == "streamable_http":
        return _HttpClient(config)
    return _StdioClient(config)


def _fingerprint(config: dict[str, Any]) -> str:
    fields = {
        key: config.get(key)
        for key in ("name", "transport", "command", "args", "cwd", "env",
                    "url", "headers", "timeout_seconds")
    }
    return json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _alias(server_id: str, tool_name: str, used: set[str]) -> str:
    clean = re.sub(r"[^A-Za-z0-9_-]+", "_", tool_name).strip("_") or "tool"
    value = f"mcp_{server_id}_{clean}"
    if len(value) > MAX_TOOL_NAME_LENGTH:
        digest = hashlib.sha1(tool_name.encode("utf-8")).hexdigest()[:8]
        value = value[:MAX_TOOL_NAME_LENGTH - 9] + "_" + digest
    candidate = value
    index = 2
    while candidate in used:
        suffix = f"_{index}"
        candidate = value[:MAX_TOOL_NAME_LENGTH - len(suffix)] + suffix
        index += 1
    used.add(candidate)
    return candidate


class McpManager:
    """Process-owned MCP connections and the alias-to-server tool map."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._refresh_lock = threading.RLock()
        self._retry_after: dict[str, float] = {}
        self._failures: dict[str, int] = {}
        self._configs: dict[str, dict[str, Any]] = {}
        self._clients: dict[str, _StdioClient | _HttpClient] = {}
        self._fingerprints: dict[str, str] = {}
        self._statuses: dict[str, dict[str, Any]] = {}
        self._tools: dict[str, dict[str, Any]] = {}

    def refresh(self, configs: list[dict[str, Any]], *, force: bool = False) -> None:
        configs = [item for item in configs if isinstance(item, dict)]
        incoming = {str(item.get("id") or ""): item for item in configs}
        # Serialize refreshes, but never hold the UI/catalog lock while waiting
        # for a third-party network connection or process startup.
        with self._refresh_lock:
            with self._lock:
                removed = []
                for server_id in set(self._configs) - set(incoming):
                    old = self._clients.pop(server_id, None)
                    if old:
                        removed.append(old)
                    self._fingerprints.pop(server_id, None)
                    self._statuses.pop(server_id, None)
                    self._retry_after.pop(server_id, None)
                    self._failures.pop(server_id, None)
                self._configs = incoming
                self._rebuild_tools()
            for old in removed:
                old.close()
            for server_id, config in incoming.items():
                if not server_id:
                    continue
                fingerprint = _fingerprint(config)
                enabled = bool(config.get("enabled", True))
                with self._lock:
                    same = self._fingerprints.get(server_id) == fingerprint
                    if enabled and same and not force:
                        if server_id in self._clients:
                            continue
                        if time.monotonic() < self._retry_after.get(server_id, 0):
                            continue
                    old = self._clients.pop(server_id, None)
                    if not same or not enabled:
                        self._failures.pop(server_id, None)
                        self._retry_after.pop(server_id, None)
                    self._fingerprints[server_id] = fingerprint
                    self._statuses[server_id] = {
                        "status": "connecting" if enabled else "disabled",
                        "tool_count": 0, "error": "",
                    }
                    self._rebuild_tools()
                if old:
                    old.close()
                if not enabled:
                    continue
                client = _client_for(config)
                try:
                    tools = client.connect()
                except Exception as exc:  # one broken server must not block Agent
                    client.close()
                    with self._lock:
                        failures = min(self._failures.get(server_id, 0) + 1, 5)
                        self._failures[server_id] = failures
                        self._retry_after[server_id] = time.monotonic() + min(300, 30 * 2 ** (failures - 1))
                        self._statuses[server_id] = {
                            "status": "error", "tool_count": 0, "error": _error_text(exc),
                        }
                    continue
                with self._lock:
                    self._clients[server_id] = client
                    self._retry_after.pop(server_id, None)
                    self._failures.pop(server_id, None)
                    self._statuses[server_id] = {
                        "status": "connected", "tool_count": len(tools), "error": "",
                    }
                    self._rebuild_tools()

    def _rebuild_tools(self) -> None:
        from tc_agent.tool_contracts import compile_contract
        self._tools = {}
        used: set[str] = set()
        for server_id, client in self._clients.items():
            config = self._configs.get(server_id) or {}
            for tool in client.tools:
                alias = _alias(server_id, tool["name"], used)
                self._tools[alias] = {
                    "name": alias,
                    "server_id": server_id,
                    "server_name": str(config.get("name") or server_id),
                    "remote_name": tool["name"],
                    "description": tool["description"],
                    "parameters": tool["input_schema"],
                    "category": MCP_TOOL_CATEGORY,
                    "danger": MCP_TOOL_DANGER,
                    "readonly": False,
                }
                self._tools[alias]["contract"] = compile_contract(self._tools[alias], external=True)

    def public_servers(self, configs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        with self._lock:
            result = []
            for config in configs:
                server_id = str(config.get("id") or "")
                status = self._statuses.get(server_id) or {
                    "status": "not_connected", "tool_count": 0, "error": "",
                }
                result.append({
                    "id": server_id,
                    "name": str(config.get("name") or ""),
                    "transport": str(config.get("transport") or "stdio"),
                    "enabled": bool(config.get("enabled", True)),
                    "command": str(config.get("command") or ""),
                    "args": [str(value) for value in config.get("args") or []],
                    "cwd": str(config.get("cwd") or ""),
                    "url": str(config.get("url") or ""),
                    "timeout_seconds": int(config.get("timeout_seconds") or 15),
                    "has_env": bool(config.get("env")),
                    "has_headers": bool(config.get("headers")),
                    "status": status.get("status", "not_connected"),
                    "tool_count": int(status.get("tool_count") or 0),
                    "error": str(status.get("error") or "")[:500],
                })
            return result

    def tool_schemas(self, allowed_categories: set[str] | None = None) -> list[dict[str, Any]]:
        from tc_agent.tool_contracts import schema_contract_note
        if allowed_categories is not None and MCP_TOOL_CATEGORY not in allowed_categories:
            return []
        with self._lock:
            return [{
                "name": item["name"],
                "description": f"【外部 MCP：{item['server_name']}】{item['description']}" + schema_contract_note(item["contract"]),
                "parameters": item["parameters"],
            } for item in self._tools.values()]

    def tool_catalog(self) -> list[dict[str, Any]]:
        with self._lock:
            tools = [{
                "name": item["name"],
                "description": f"【{item['server_name']}】{item['description']}",
                "readonly": False,
                "danger": MCP_TOOL_DANGER,
            } for item in self._tools.values()]
        return [{"category": MCP_TOOL_CATEGORY, "tools": sorted(tools, key=lambda x: x["name"])}] if tools else []

    def metadata(self, name: str) -> dict[str, Any]:
        with self._lock:
            return dict(self._tools.get(name) or {})

    def has_tool(self, name: str) -> bool:
        with self._lock:
            return name in self._tools

    def call(self, name: str, arguments: dict[str, Any]) -> Any:
        with self._lock:
            item = self._tools.get(name)
            if not item:
                return {"error": f"未知外部 MCP 工具：{name}"}
            client = self._clients.get(item["server_id"])
            if client is None:
                return {"error": f"MCP 服务未连接：{item['server_name']}"}
            server_id = item["server_id"]
            server_name = item["server_name"]
            remote_name = item["remote_name"]
        # The transport owns its own request lock. Do not hold the manager
        # catalog lock during a potentially slow third-party call.
        try:
            return client.call(remote_name, arguments or {})
        except Exception as exc:
            with self._lock:
                self._statuses[server_id] = {
                    "status": "error", "tool_count": len(client.tools), "error": _error_text(exc),
                }
            return {"error": f"外部 MCP「{server_name}」调用失败：{_error_text(exc)}",
                    "status": "uncertain", "uncertain": True, "not_executed": False,
                    "next_action": "远程动作可能已执行；先核对服务端状态，不得自动重发。"}

    def test(self, config: dict[str, Any]) -> dict[str, Any]:
        client = _client_for(config)
        try:
            tools = client.connect()
            return {"ok": True, "tool_count": len(tools),
                    "tools": [item["name"] for item in tools]}
        except Exception as exc:
            return {"ok": False, "tool_count": 0, "error": _error_text(exc), "tools": []}
        finally:
            client.close()

    def close_all(self) -> None:
        with self._refresh_lock:
            with self._lock:
                clients = list(self._clients.values())
                self._clients.clear()
                self._tools.clear()
                self._statuses.clear()
                self._fingerprints.clear()
                self._retry_after.clear()
                self._failures.clear()
                self._configs.clear()
            for client in clients:
                client.close()


MCP_MANAGER = McpManager()
