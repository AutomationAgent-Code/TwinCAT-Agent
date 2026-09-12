"""Coalesce editor notifications without blocking the WebSocket receive loop."""
from __future__ import annotations

import threading
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Callable, Mapping


class StaleCacheNotification(ValueError):
    """The editor has moved on; never cache new code under the old identity."""


class InactivePlcDocument(ValueError):
    """The user switched to HMI/another non-PLC editor or closed the document."""


class CacheRefreshQueue:
    def __init__(self, cache, read_payload, delay=0.25, on_error=None):
        self.cache = cache
        self.read_payload = read_payload
        self.delay = delay
        self.on_error = on_error
        self._lock = threading.RLock()
        self._states = {}

    def submit(self, pid, solution, expected):
        pid, solution = int(pid or 0), str(solution or "")
        expected = dict(expected or {})
        if pid <= 0 or not solution:
            raise ValueError("PLC 缓存回读需要明确的 XAE PID 和解决方案")
        if Path(str(expected.get("path") or "")).suffix.casefold() not in {".tcpou", ".tcgvl", ".tcdut"}:
            raise ValueError("无效 PLC 缓存通知路径")
        with self._lock:
            if pid not in self._states:
                # Bound diagnostics from closed/replaced XAE sessions.
                for old in list(self._states):
                    s = self._states[old]
                    if len(self._states) < 32:
                        break
                    if not s["running"] and s["timer"] is None:
                        del self._states[old]
                if len(self._states) >= 32:
                    raise ValueError("PLC 缓存更新会话过多")
                self._states[pid] = {"generation": 0, "running": False, "timer": None,
                                     "status": "idle", "error": ""}
            state = self._states[pid]
            state["generation"] += 1
            state["latest"] = (solution, expected)
            state["status"], state["error"] = "queued", ""
            # Do not serve an old unsaved value while its refresh is pending.
            self.cache.invalidate_document(pid, solution, expected["path"], expected.get("member", ""))
            if not state["running"]:
                self._schedule(pid, state)
            return {"status": "queued", "xae_pid": pid, "generation": state["generation"]}

    def _schedule(self, pid, state):
        if state["timer"] is not None:
            state["timer"].cancel()
        timer = threading.Timer(self.delay, self._drain, args=(pid, state["generation"]))
        timer.daemon = True
        state["timer"] = timer
        timer.start()

    def _drain(self, pid, scheduled_generation):
        with self._lock:
            state = self._states[pid]
            if state["running"] or state["generation"] != scheduled_generation:
                return
            state["timer"], state["running"], state["status"] = None, True, "reading"
            solution, expected = state["latest"]
        payload, status, error = None, "updated", ""
        read_revision = self.cache.revision
        try:
            try:
                payload = self.read_payload(pid, solution, expected)
            except StaleCacheNotification:
                # Read the *current* identity afresh, retaining PID/solution checks.
                # No old event content or old path is used for this cache entry.
                with self._lock:
                    superseded = state["generation"] != scheduled_generation
                if not superseded:
                    payload = self.read_payload(pid, solution, None)
                status = "resynced"
        except InactivePlcDocument:
            status = "ignored_non_plc"
        except Exception as exc:
            status, error = "failed", str(exc)
        finally:
            with self._lock:
                try:
                    if state["generation"] == scheduled_generation:
                        if payload is not None and self.cache.revision != read_revision:
                            # A PLC write/global invalidation happened during COM
                            # readback. Do not resurrect pre-write cache content.
                            status = "superseded"
                        elif payload is not None:
                            try:
                                self.cache.put(payload)
                            except Exception as exc:
                                status, error = "failed", str(exc)
                        state["status"], state["error"] = status, error
                        if error and self.on_error:
                            self.on_error(pid, error)
                finally:
                    state["running"] = False
                    if state["generation"] != scheduled_generation:
                        self._schedule(pid, state)

    def snapshot(self):
        with self._lock:
            return [{"xae_pid": pid, **{k: s[k] for k in ("generation", "status", "error")}}
                    for pid, s in self._states.items()]


@dataclass(frozen=True)
class TreeItemChanged:
    """A protocol notification after its project identity was resolved."""

    protocol_project_id: int | str
    xae_pid: int
    solution: str
    path: str = ""
    member: str = ""
    version: int | str | None = None


class TreeItemChangeRouter:
    """Route precise System Manager changes into the existing reread queue.

    Protocol project IDs and XAE process IDs are intentionally separate.  An
    unbound notification is ignored and requests an explicit resync instead of
    guessing which XAE instance owns it.
    """

    def __init__(self, refresh_queue: CacheRefreshQueue, *, resync: Callable[[], Any] | None = None):
        self.refresh_queue = refresh_queue
        self.resync = resync
        self._bindings: dict[int | str, tuple[int, str]] = {}
        self._versions: dict[tuple[int | str, str, str], int | str] = {}
        self.connected = True

    def bind(self, protocol_project_id: int | str, xae_pid: int, solution: str) -> None:
        if not solution or int(xae_pid or 0) <= 0:
            raise ValueError("tree notification binding requires XAE PID and solution")
        self._bindings[protocol_project_id] = (int(xae_pid), str(solution))

    def unbind(self, protocol_project_id: int | str) -> None:
        self._bindings.pop(protocol_project_id, None)

    @staticmethod
    def _newer(previous: int | str | None, current: int | str | None) -> bool:
        if current is None or previous is None:
            return True
        try:
            return int(current) > int(previous)
        except (TypeError, ValueError):
            return str(current) != str(previous)

    def handle(self, note: Mapping[str, Any]) -> dict[str, Any]:
        protocol_id = note.get("pid", note.get("project_id"))
        if protocol_id not in self._bindings:
            return {"status": "resync_required", "reason": "unknown protocol project id"}
        xae_pid, solution = self._bindings[protocol_id]
        path = str(note.get("path") or note.get("source_file") or note.get("file") or "")
        member = str(note.get("member") or note.get("child_name") or "")
        version = note.get("version", note.get("revision"))
        if not path:
            return {"status": "resync_required", "reason": "notification has no source path"}
        key = (protocol_id, path.casefold(), member.casefold())
        if not self._newer(self._versions.get(key), version):
            return {"status": "stale", "protocol_project_id": protocol_id, "path": path, "member": member}
        if version is not None:
            self._versions[key] = version
        queued = self.refresh_queue.submit(
            xae_pid, solution,
            {"path": path, "member": member, "protocol_project_id": protocol_id, "version": version},
        )
        return {"status": "queued", "protocol_project_id": protocol_id, "xae_pid": xae_pid, **queued}

    def on_disconnect(self) -> dict[str, Any]:
        self.connected = False
        return {"status": "disconnected", "resync_required": True}

    def on_reconnect(self) -> dict[str, Any]:
        self.connected = True
        if self.resync is None:
            return {"status": "reconnected", "resync_required": True}
        refreshed = self.resync()
        if refreshed is None:
            return {"status": "reconnected", "resync_required": True}
        entries = refreshed.get("documents", []) if isinstance(refreshed, dict) else refreshed
        queued = []
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            note = {"pid": entry.get("project_id", entry.get("pid")), **entry}
            queued.append(self.handle(note))
        return {"status": "reconnected", "resync_required": False, "queued": queued}
