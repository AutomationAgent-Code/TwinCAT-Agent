"""Small, guarded adapter for the TwinCAT System Manager WebSocket protocol.

This module deliberately contains no XAE automation and no project mutation
policy.  It translates the narrow protocol surface used by the embedded
workbench into stable Python records so the existing COM bridge can remain the
fallback when the optional direct endpoint is unavailable.

The protocol project id returned by ``projectList`` is *not* a Windows/XAE
process id.  Callers must provide an explicit mapping when both identities are
needed.
"""

from __future__ import annotations

from dataclasses import dataclass
import inspect
import json
import os
import threading
import time
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.parse import urlsplit


GAS_SUBPROTOCOL = "flare"
_ENV_NAMES = ("SYSMAN_WS_URL", "GAS_SERVER_URL")


class SystemManagerError(RuntimeError):
    """Base class for direct-interface failures."""


class EndpointUnavailable(SystemManagerError):
    """No explicitly configured or uniquely probed endpoint is available."""


class EndpointAmbiguous(SystemManagerError):
    """More than one candidate passed the protocol probe."""


class ProtocolError(SystemManagerError):
    """The System Manager returned a protocol-level error."""

    def __init__(self, command: str, detail: Any):
        self.command = command
        self.detail = detail
        super().__init__(f"{command} failed: {detail}")


class CommandTransport(Protocol):
    """Minimal synchronous transport used by the adapter and its fixtures."""

    def request(self, command: str, payload: Mapping[str, Any], *, timeout_s: float) -> Any:
        ...


class SystemManagerWebSocketTransport:
    """Synchronous, serialized transport for the local ``flare`` endpoint.

    The optional ``websockets`` dependency is imported only when this class is
    instantiated.  Requests use the observed command-array envelope and the
    transport waits for the matching reply id, ignoring notifications that
    arrive while a request is in flight.  It is intentionally small; an
    embedded frontend that already owns an event loop can implement the
    ``CommandTransport`` protocol instead.
    """

    def __init__(self, url: str, *, timeout_s: float = 30.0):
        self.url = _valid_ws_url(url)
        self.timeout_s = float(timeout_s)
        self._socket = None
        self._connect = None
        self._next_id = 1
        self._lock = threading.RLock()
        self._subscribers: dict[str, list[Callable[[Mapping[str, Any]], None]]] = {}

    def connect(self) -> None:
        if self._socket is not None:
            return
        try:
            from websockets.sync.client import connect
        except ImportError as exc:
            raise EndpointUnavailable("websockets package is not installed") from exc
        self._connect = connect
        self._socket = connect(
            self.url,
            subprotocols=[GAS_SUBPROTOCOL],
            open_timeout=max(0.05, self.timeout_s),
            close_timeout=max(0.05, self.timeout_s),
        )
        if self._socket.subprotocol != GAS_SUBPROTOCOL:
            self.close()
            raise EndpointUnavailable("System Manager did not negotiate the flare subprotocol")

    def close(self) -> None:
        socket, self._socket = self._socket, None
        if socket is not None:
            try:
                socket.close()
            except Exception:
                pass

    def _notify(self, value: Any) -> None:
        values = value if isinstance(value, list) else [value]
        for note in values:
            if not isinstance(note, Mapping):
                continue
            command = str(note.get("cmd") or "")
            if note.get("id") is not None and command.endswith("Reply"):
                continue
            event = command
            for callback in tuple(self._subscribers.get(event, ())):
                try:
                    callback(note)
                except Exception:
                    # Notification consumers must not break command replies.
                    pass

    @staticmethod
    def _reply(value: Any, request_id: int) -> Mapping[str, Any] | None:
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, Mapping) and item.get("id") == request_id:
                return item
        for item in values:
            if isinstance(item, Mapping) and str(item.get("cmd") or "").endswith("Reply"):
                return item
        return None

    def request(self, command: str, payload: Mapping[str, Any], *, timeout_s: float) -> Mapping[str, Any]:
        with self._lock:
            self.connect()
            request_id = self._next_id
            self._next_id += 1
            message = {"cmd": command, **dict(payload), "id": request_id}
            self._socket.send(json.dumps([message], ensure_ascii=False))
            deadline = time.monotonic() + max(0.001, float(timeout_s))
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"System Manager request {command} timed out")
                try:
                    value = json.loads(self._socket.recv(timeout=remaining))
                except TimeoutError:
                    raise TimeoutError(f"System Manager request {command} timed out") from None
                self._notify(value)
                reply = self._reply(value, request_id)
                if reply is not None:
                    return reply

    def batch_request(self, requests: Sequence[Mapping[str, Any]], *, timeout_s: float) -> list[Mapping[str, Any]]:
        """Send a read batch and map numeric wire IDs back to item IDs."""
        with self._lock:
            self.connect()
            wire_ids: dict[int, str] = {}
            wire_requests = []
            for item in requests:
                wire_id = self._next_id
                self._next_id += 1
                item_id = str(item.get("id"))
                wire_ids[wire_id] = item_id
                wire_requests.append({**dict(item), "id": wire_id})
            self._socket.send(json.dumps(wire_requests, ensure_ascii=False))
            pending = set(wire_ids)
            replies: list[Mapping[str, Any]] = []
            deadline = time.monotonic() + max(0.001, float(timeout_s))
            while pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("System Manager batch timed out")
                try:
                    value = json.loads(self._socket.recv(timeout=remaining))
                except TimeoutError:
                    raise TimeoutError("System Manager batch timed out") from None
                self._notify(value)
                values = value if isinstance(value, list) else [value]
                for reply in values:
                    if not isinstance(reply, Mapping) or reply.get("id") not in pending:
                        continue
                    pending.remove(reply["id"])
                    replies.append({**dict(reply), "id": wire_ids[reply["id"]]})
            return replies

    def subscribe(self, event: str, callback: Callable[[Mapping[str, Any]], None]) -> Callable[[], None]:
        self._subscribers.setdefault(str(event), []).append(callback)

        def unsubscribe() -> None:
            callbacks = self._subscribers.get(str(event), [])
            if callback in callbacks:
                callbacks.remove(callback)

        return unsubscribe

    def subscribe(self, event: str, callback: Callable[[Mapping[str, Any]], None]) -> Callable[[], None] | None:
        ...


@dataclass(frozen=True)
class SystemManagerEndpoint:
    url: str
    source: str
    subprotocol: str = GAS_SUBPROTOCOL


@dataclass(frozen=True)
class EndpointDiscovery:
    status: str
    endpoint: SystemManagerEndpoint | None
    candidates: tuple[str, ...]
    reachable: tuple[str, ...] = ()
    reason: str = ""

    @property
    def available(self) -> bool:
        return self.endpoint is not None and self.status in {"configured", "discovered"}


@dataclass(frozen=True)
class ProjectIdentity:
    """Identity returned by System Manager, kept separate from XAE PID."""

    protocol_id: int | str
    name: str = ""
    path: str = ""
    xae_pid: int | None = None

    def with_xae_pid(self, xae_pid: int) -> "ProjectIdentity":
        return ProjectIdentity(self.protocol_id, self.name, self.path, int(xae_pid))


@dataclass(frozen=True)
class DirectReadResult:
    project: ProjectIdentity
    tid: int | None
    tname: str | None
    declaration: str | None
    implementation: str | None
    language: str | None
    source: str = "system_manager"
    live_xae: bool = True
    saved: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": "read",
            "source": self.source,
            "live_xae": self.live_xae,
            "authoritative": True,
            "project_id": self.project.protocol_id,
            "tid": self.tid,
            "tname": self.tname,
            "language": self.language,
            "declaration": self.declaration or "",
            "implementation": self.implementation or "",
        }
        if self.saved is not None:
            result["saved"] = self.saved
        else:
            result["saved_status"] = "unknown"
        return result


def _valid_ws_url(value: str) -> str:
    value = str(value or "").strip()
    parts = urlsplit(value)
    if parts.scheme not in {"ws", "wss"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("System Manager endpoint must be a ws:// or wss:// URL without credentials")
    return value


def _probe_result(value: Any) -> bool:
    """Interpret a probe that has already checked the ``flare`` subprotocol."""
    if isinstance(value, Mapping):
        if value.get("protocol_error") or value.get("error"):
            return False
        if value.get("subprotocol") not in (None, GAS_SUBPROTOCOL):
            return False
        return bool(value.get("reachable", value.get("ok", False)))
    return value is True


def websocket_flare_probe(url: str, *, timeout_s: float = 1.5) -> dict[str, Any]:
    """Perform one read-only WebSocket handshake with the required subprotocol.

    ``websockets`` is an optional agent dependency.  The import is lazy so the
    COM-only installation remains usable.  This helper opens no command
    session and closes immediately after negotiation.
    """
    try:
        from websockets.sync.client import connect
    except ImportError as exc:
        raise EndpointUnavailable("websockets package is not installed") from exc
    with connect(
        _valid_ws_url(url),
        subprotocols=[GAS_SUBPROTOCOL],
        open_timeout=max(0.05, float(timeout_s)),
        close_timeout=max(0.05, float(timeout_s)),
    ) as socket:
        return {"reachable": True, "subprotocol": socket.subprotocol}


def discover_system_manager_endpoint(
    *,
    environ: Mapping[str, str] | None = None,
    candidates: Sequence[str] = (),
    probe: Callable[..., Any] | None = None,
) -> EndpointDiscovery:
    """Resolve a direct endpoint without inventing a default port.

    ``SYSMAN_WS_URL`` wins over ``GAS_SERVER_URL``.  Candidate probing is
    intentionally injectable: the production host supplies a probe that
    performs a WebSocket handshake with the ``flare`` subprotocol; unit tests
    use a fixture.  If multiple candidates respond, the result is ambiguous
    instead of silently selecting the first responder.
    """
    env = os.environ if environ is None else environ
    for name in _ENV_NAMES:
        value = str(env.get(name) or "").strip()
        if value:
            try:
                url = _valid_ws_url(value)
            except ValueError as exc:
                return EndpointDiscovery("unavailable", None, (), reason=f"{name}: {exc}")
            return EndpointDiscovery(
                "configured", SystemManagerEndpoint(url, name), (url,), reason="explicit endpoint"
            )

    normalized: list[str] = []
    for candidate in candidates:
        try:
            url = _valid_ws_url(candidate)
        except ValueError:
            continue
        if url not in normalized:
            normalized.append(url)
    if not normalized:
        return EndpointDiscovery(
            "unavailable", None, (), reason="no explicit endpoint and no verified candidates"
        )
    if probe is None:
        return EndpointDiscovery(
            "unavailable", None, tuple(normalized), reason="candidate probe is required"
        )

    reachable: list[str] = []
    for url in normalized:
        try:
            result = probe(url, GAS_SUBPROTOCOL)
            if inspect.isawaitable(result):
                raise TypeError("async probe requires an async discovery host adapter")
            if _probe_result(result):
                reachable.append(url)
        except Exception:
            # A failed candidate is not evidence that another URL is valid.
            continue
    if len(reachable) == 1:
        return EndpointDiscovery(
            "discovered", SystemManagerEndpoint(reachable[0], "probe"),
            tuple(normalized), tuple(reachable), reason="unique flare endpoint"
        )
    if len(reachable) > 1:
        return EndpointDiscovery(
            "ambiguous", None, tuple(normalized), tuple(reachable),
            reason="multiple candidates negotiated the flare subprotocol"
        )
    return EndpointDiscovery(
        "unavailable", None, tuple(normalized), (), reason="no candidate negotiated the flare subprotocol"
    )


def _reply_or_raise(command: str, reply: Any) -> Mapping[str, Any]:
    if not isinstance(reply, Mapping):
        raise ProtocolError(command, "reply is not an object")
    error = reply.get("protocolError", reply.get("protocol_error"))
    if error is not None:
        raise ProtocolError(command, error)
    return reply


def _message_nodes(node: Any, output: list[dict[str, Any]], *, pou: str = "") -> None:
    if not isinstance(node, Mapping):
        return
    info = node.get("info")
    if isinstance(info, Mapping):
        name = str(info.get("Name") or pou or "")
        messages = info.get("CompilerMessages")
        if isinstance(messages, list):
            for message in messages:
                if isinstance(message, Mapping):
                    output.append({**dict(message), "pou": name,
                                   "description": info.get("Description", "")})
    items = node.get("items")
    if isinstance(items, list):
        for item in items:
            _message_nodes(item, output, pou=pou)


def _dedupe_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for message in messages:
        key = tuple(message.get(name) for name in (
            "pou", "ErrorPrefix", "ErrorCode", "Severity", "PositionLine", "PositionOffset", "Text"
        ))
        if key in seen:
            continue
        seen.add(key)
        result.append(message)
    return result


class DirectSystemManager:
    """Read-oriented direct interface with explicit, injectable transport."""

    def __init__(self, transport: CommandTransport, *, endpoint: SystemManagerEndpoint | None = None,
                 timeout_s: float = 30.0):
        self.transport = transport
        self.endpoint = endpoint
        self.timeout_s = float(timeout_s)
        self.attached = False
        self.projects: dict[int | str, ProjectIdentity] = {}
        self._unsubscribers: list[Callable[[], None]] = []

    def _request(self, command: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        reply = self.transport.request(command, dict(payload), timeout_s=self.timeout_s)
        return _reply_or_raise(command, reply)

    def attach(self) -> tuple[ProjectIdentity, ...]:
        self._request("workbenchAttach", {"wid": 1})
        self.attached = True
        reply = self._request("projectList", {})
        raw_projects = reply.get("pid", [])
        if not isinstance(raw_projects, list):
            raise ProtocolError("projectList", "pid must be an array")
        identities: list[ProjectIdentity] = []
        for raw in raw_projects:
            if isinstance(raw, Mapping):
                protocol_id = raw.get("pid", raw.get("id"))
                name = str(raw.get("name") or "")
                path = str(raw.get("path") or "")
            else:
                protocol_id, name, path = raw, "", ""
            if protocol_id is None:
                raise ProtocolError("projectList", "project entry has no protocol id")
            identity = ProjectIdentity(protocol_id, name, path)
            self.projects[protocol_id] = identity
            identities.append(identity)
        return tuple(identities)

    def require_project(self, project: ProjectIdentity | int | str) -> ProjectIdentity:
        if isinstance(project, ProjectIdentity):
            return project
        try:
            return self.projects[project]
        except KeyError as exc:
            raise ValueError(f"unknown System Manager project id: {project}") from exc

    def read_plc_pou(self, project: ProjectIdentity | int | str, *, tid: int | None = None,
                     tname: str | None = None) -> DirectReadResult:
        identity = self.require_project(project)
        payload: dict[str, Any] = {
            "pid": identity.protocol_id,
            # Both keys are required by the protocol for a read.  A missing
            # key is not equivalent to a null key.
            "interface": None,
            "implementation": None,
        }
        # The observed command builder strips undefined optional fields.  XAE
        # distinguishes an omitted identity field from an explicit null and
        # may return an empty, error-free reply for the latter.
        if tid is not None:
            payload["tid"] = tid
        if tname is not None:
            payload["tname"] = tname
        reply = self._request("sm.plcpou", payload)
        return DirectReadResult(
            project=identity,
            tid=tid,
            tname=tname,
            declaration=reply.get("interface"),
            implementation=reply.get("implementation"),
            language=reply.get("language"),
            saved=reply.get("saved") if isinstance(reply.get("saved"), bool) else None,
        )

    def compiler_messages(self, project: ProjectIdentity | int | str, *, tid: int | None = None,
                          tname: str | None = None, level: int = 10,
                          severity: Sequence[str] = ("fatal", "error", "warning")) -> dict[str, Any]:
        identity = self.require_project(project)
        payload: dict[str, Any] = {
            "pid": identity.protocol_id,
            "level": int(level),
            "param": {"severity": list(severity), "info": "nothing"},
        }
        if tid is not None:
            payload["tid"] = tid
        if tname is not None:
            payload["tname"] = tname
        reply = self._request("sm.plccompilermsg", payload)
        if isinstance(reply.get("messages"), list):
            raw = [dict(item) for item in reply["messages"] if isinstance(item, Mapping)]
        else:
            raw = []
            _message_nodes(reply, raw)
        return {
            "source": "sm.plccompilermsg",
            "project_id": identity.protocol_id,
            "tid": tid,
            "tname": tname,
            "level": int(level),
            # The protocol does not expose a completeness bit in this command;
            # callers must not turn an empty array into a verified clean build.
            "complete": reply.get("complete") is True or reply.get("diagnostics_complete") is True,
            "messages": _dedupe_messages(raw),
        }

    def subscribe_tree_item_changed(self, callback: Callable[[Mapping[str, Any]], None]) -> Callable[[], None] | None:
        """Subscribe to precise changes; callers decide which cache to reread."""
        unsubscribe = self.transport.subscribe("sm.treeItemChanged", callback)
        if unsubscribe:
            self._unsubscribers.append(unsubscribe)
        return unsubscribe

    def resync_projects(self) -> tuple[ProjectIdentity, ...]:
        """Reconnect resync: refresh project identities, not the whole PLC tree."""
        reply = self._request("projectList", {})
        raw_projects = reply.get("pid", [])
        if not isinstance(raw_projects, list):
            raise ProtocolError("projectList", "pid must be an array")
        self.projects.clear()
        result: list[ProjectIdentity] = []
        for raw in raw_projects:
            if isinstance(raw, Mapping):
                value = raw.get("pid", raw.get("id"))
                identity = ProjectIdentity(value, str(raw.get("name") or ""), str(raw.get("path") or ""))
            else:
                value = raw
                identity = ProjectIdentity(value)
            if value is None:
                continue
            self.projects[value] = identity
            result.append(identity)
        return tuple(result)

    def close(self) -> None:
        for unsubscribe in self._unsubscribers:
            try:
                unsubscribe()
            except Exception:
                pass
        self._unsubscribers.clear()


class PlcSourceAdapter:
    """Prefer direct reads while preserving the established COM fallback."""

    def __init__(self, direct: DirectSystemManager | None, com_reader: Callable[..., dict[str, Any]]):
        self.direct = direct
        self.com_reader = com_reader

    def read(self, project: ProjectIdentity | int | str, *, tid: int | None = None,
             tname: str | None = None, **com_kwargs: Any) -> dict[str, Any]:
        if self.direct is None:
            return self.com_reader(**com_kwargs)
        try:
            return self.direct.read_plc_pou(project, tid=tid, tname=tname).as_dict()
        except (EndpointUnavailable, EndpointAmbiguous, ProtocolError, TimeoutError, OSError) as direct_error:
            # Direct failure is observable, but does not turn the COM path into
            # a fake direct success or lose the original source of the result.
            result = dict(self.com_reader(**com_kwargs))
            result.setdefault("direct_fallback", True)
            result["direct_error"] = str(direct_error)
            return result
