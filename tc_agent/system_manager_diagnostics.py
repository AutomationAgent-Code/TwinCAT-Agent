"""Fail-closed compiler diagnostics for the direct System Manager path."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _severity(message: Mapping[str, Any]) -> str:
    return str(message.get("Severity", message.get("severity", "")) or "").casefold()


def _messages(raw: Any) -> tuple[list[dict[str, Any]], bool, Any]:
    if not isinstance(raw, Mapping):
        return [], False, "reply is not an object"
    protocol_error = raw.get("protocolError", raw.get("protocol_error"))
    if protocol_error is not None:
        return [], False, protocol_error
    values = raw.get("messages")
    if not isinstance(values, list):
        return [], False, "messages are missing"
    messages = [dict(item) for item in values if isinstance(item, Mapping)]
    complete = raw.get("complete") is True or raw.get("diagnostics_complete") is True
    if raw.get("truncated") is True or raw.get("diagnosticsTruncated") is True:
        complete = False
    return messages, complete, None


def normalize_system_manager_diagnostics(raw: Any, *, project_id: int | str | None = None) -> dict[str, Any]:
    """Normalize ``sm.plccompilermsg`` without inferring clean diagnostics.

    The reference protocol's ``level=10`` and an empty message list are not a
    completeness guarantee.  Only an explicit completeness bit can verify the
    list; protocol errors and malformed replies remain incomplete.
    """
    messages, complete, error = _messages(raw)
    if project_id is None and isinstance(raw, Mapping):
        project_id = raw.get("project_id")
    counts = {"fatal": 0, "error": 0, "warning": 0}
    for message in messages:
        level = _severity(message)
        if level in counts:
            counts[level] += 1
    result = {
        "source": "sm.plccompilermsg",
        "project_id": project_id,
        "messages": messages,
        "fatals": counts["fatal"],
        "errors": counts["error"],
        "warnings": counts["warning"],
        "complete": bool(complete and error is None),
        "verified": bool(complete and error is None),
        "protocol_error": error,
    }
    if error is not None:
        result["status"] = "protocol_error"
    elif not complete:
        result["status"] = "incomplete"
    else:
        result["status"] = "failed" if counts["fatal"] or counts["error"] else "clean"
    return result


def combine_build_and_diagnostics(build: Any, diagnostics: Any, *, project_id: int | str | None = None) -> dict[str, Any]:
    """Join ``sm.build`` with compiler messages, preserving incomplete state."""
    normalized = normalize_system_manager_diagnostics(diagnostics, project_id=project_id)
    build_object = build if isinstance(build, Mapping) else {}
    build_error = build_object.get("protocolError", build_object.get("protocol_error"))
    build_performed = build_object.get("buildPerformed", build_object.get("performed"))
    if build_performed is None:
        build_performed = build_object.get("succeeded") is True or build_object.get("success") is True
    build_succeeded = build_object.get("succeeded", build_object.get("success")) is True
    complete = bool(build_error is None and build_performed is True and normalized["complete"])
    verified = bool(complete and build_succeeded and normalized["errors"] == 0 and normalized["fatals"] == 0)
    result = {
        **normalized,
        "build_performed": bool(build_performed is True),
        "build_succeeded": build_succeeded,
        "diagnostics_complete": complete,
        "compiler_verified": verified,
        "success": verified,
        "status": "succeeded" if verified else ("failed" if complete else "incomplete"),
        "build_protocol_error": build_error,
    }
    if not complete:
        result["incomplete_reason"] = (
            "build protocol error" if build_error is not None else
            "build was not performed" if build_performed is not True else
            "sm.plccompilermsg did not prove completeness"
        )
    return result

