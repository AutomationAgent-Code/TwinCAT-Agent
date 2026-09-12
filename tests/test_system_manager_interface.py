from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from tc_agent.cache_refresh import TreeItemChangeRouter
from tc_agent import backend
from tc_agent.system_manager_interface import (
    DirectSystemManager,
    EndpointAmbiguous,
    PlcSourceAdapter,
    ProjectIdentity,
    ProtocolError,
    discover_system_manager_endpoint,
)


class FakeTransport:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.subscribers = {}

    def request(self, command, payload, *, timeout_s):
        self.calls.append((command, dict(payload), timeout_s))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def subscribe(self, event, callback):
        self.subscribers[event] = callback
        return lambda: self.subscribers.pop(event, None)


def test_endpoint_discovery_requires_explicit_or_verified_unique_endpoint():
    assert discover_system_manager_endpoint(environ={}).status == "unavailable"
    configured = discover_system_manager_endpoint(
        environ={"SYSMAN_WS_URL": "ws://localhost:9123", "GAS_SERVER_URL": "ws://localhost:9124"}
    )
    assert configured.status == "configured"
    assert configured.endpoint.url == "ws://localhost:9123"

    seen = []
    discovered = discover_system_manager_endpoint(
        environ={}, candidates=["ws://localhost:1", "ws://localhost:2"],
        probe=lambda url, subprotocol: seen.append((url, subprotocol)) or url.endswith(":2"),
    )
    assert discovered.status == "discovered"
    assert discovered.endpoint.url.endswith(":2")
    assert all(subprotocol == "flare" for _, subprotocol in seen)

    ambiguous = discover_system_manager_endpoint(
        environ={}, candidates=["ws://localhost:1", "ws://localhost:2"], probe=lambda *_: True,
    )
    assert ambiguous.status == "ambiguous"
    assert ambiguous.endpoint is None


def test_invalid_explicit_endpoint_is_fail_closed():
    result = discover_system_manager_endpoint(environ={"SYSMAN_WS_URL": "http://localhost:8088"})
    assert result.status == "unavailable"
    assert "ws://" in result.reason


def test_direct_interface_keeps_protocol_project_id_separate_and_reads_null_fields():
    transport = FakeTransport([
        {},
        {"pid": [{"pid": 17, "name": "Machine", "path": "C:/Machine.tsproj"}]},
        {"interface": "VAR_INPUT\nEND_VAR", "implementation": "nX := 1;", "language": "ST"},
        {"info": {"Name": "MAIN", "CompilerMessages": [
            {"Severity": "error", "ErrorCode": "C0001", "Text": "bad"},
            {"Severity": "error", "ErrorCode": "C0001", "Text": "bad"},
        ]}},
    ])
    direct = DirectSystemManager(transport)
    projects = direct.attach()
    assert projects == (ProjectIdentity(17, "Machine", "C:/Machine.tsproj"),)
    read = direct.read_plc_pou(17, tid=21, tname="TIPC^PLC1^MAIN")
    assert read.as_dict()["declaration"].startswith("VAR_INPUT")
    command, payload, _ = transport.calls[2]
    assert command == "sm.plcpou"
    assert payload["interface"] is None and payload["implementation"] is None
    assert payload["tid"] == 21 and payload["tname"] == "TIPC^PLC1^MAIN"
    messages = direct.compiler_messages(17, tname="PLC1")
    assert len(messages["messages"]) == 1
    assert messages["complete"] is False


def test_protocol_failure_is_not_wrapped_as_success_and_com_fallback_is_observable():
    transport = FakeTransport([{}, {"pid": [3]}, {"protocolError": {"code": 9, "message": "denied"}}])
    direct = DirectSystemManager(transport)
    direct.attach()
    fallback = Mock(return_value={"source": "com", "live_xae": True, "declaration": "D", "implementation": "I"})
    result = PlcSourceAdapter(direct, fallback).read(3, tid=8, name="MAIN")
    assert result["source"] == "com"
    assert result["direct_fallback"] is True
    assert "sm.plcpou" in result["direct_error"]
    fallback.assert_called_once_with(name="MAIN")


def test_tree_router_uses_explicit_pid_mapping_and_drops_old_versions():
    queue = Mock()
    queue.submit.return_value = {"status": "queued", "generation": 1}
    router = TreeItemChangeRouter(queue)
    router.bind(17, 17124, "C:/Machine.sln")
    assert router.handle({"pid": 99, "path": "C:/PLC/MAIN.TcPOU"})["status"] == "resync_required"
    assert router.handle({"pid": 17, "path": "C:/PLC/MAIN.TcPOU", "member": "Run", "version": 2})["status"] == "queued"
    assert router.handle({"pid": 17, "path": "C:/PLC/MAIN.TcPOU", "member": "Run", "version": 1})["status"] == "stale"
    queue.submit.assert_called_once_with(
        17124, "C:/Machine.sln",
        {"path": "C:/PLC/MAIN.TcPOU", "member": "Run", "protocol_project_id": 17, "version": 2},
    )


def test_tree_router_handles_missing_path_disconnect_and_reconnect_resync():
    queue = Mock()
    queue.submit.return_value = {"status": "queued", "generation": 1}
    router = TreeItemChangeRouter(queue, resync=lambda: {
        "documents": [{"project_id": 17, "path": "C:/PLC/MAIN.TcPOU", "member": "Run", "version": 3}]
    })
    router.bind(17, 17124, "C:/Machine.sln")
    assert router.handle({"pid": 17})["status"] == "resync_required"
    assert router.on_disconnect()["resync_required"] is True
    result = router.on_reconnect()
    assert result["status"] == "reconnected"
    assert result["queued"][0]["status"] == "queued"


def test_backend_route_binds_protocol_identity_before_targeted_refresh():
    queue = Mock()
    queue.submit.return_value = {"status": "queued", "generation": 1}
    router = TreeItemChangeRouter(queue)
    with patch.object(backend, "PLC_TREE_CHANGE_ROUTER", router):
        result = backend.route_system_manager_tree_item_changed(
            {"protocol_project_id": 17, "path": "C:/PLC/MAIN.TcPOU", "member": "Stop", "version": 4},
            xae_pid=17124, solution="C:/Machine.sln",
        )
    assert result["status"] == "queued"
    queue.submit.assert_called_once()
