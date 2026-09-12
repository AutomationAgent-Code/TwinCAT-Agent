"""Create privacy-conscious TwinCAT Agent bug and suggestion bundles."""

from __future__ import annotations

import json
import os
import platform
import re
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any


_SECRET_KEY = re.compile(
    r"(?i)(api[_-]?key|authorization|password|secret|token|license|activation)"
)
_SECRET_TEXT = re.compile(
    r"(?i)(bearer\s+[A-Za-z0-9._~+/=-]+|sk-[A-Za-z0-9_-]{8,}|"
    r"TCAG1\.[A-Za-z0-9._-]+|(?:api[_ -]?key|password|secret|token)\s*[:=]\s*\S+)"
)
_SOURCE_KEYS = {
    "code", "declaration", "implementation", "old_text", "new_text",
    "plcopen_xml", "source", "content",
}


def _redact_text(value: str, limit: int = 3000) -> str:
    cleaned = _SECRET_TEXT.sub("<redacted>", str(value or ""))
    return cleaned if len(cleaned) <= limit else cleaned[:limit] + "…<truncated>"


def sanitize(value: Any, key: str = "") -> Any:
    """Recursively remove credentials and PLC source from report payloads."""
    if _SECRET_KEY.search(str(key or "")):
        return "<redacted>"
    if str(key or "").casefold() in _SOURCE_KEYS:
        return f"<omitted {len(str(value or ''))} chars>"
    if isinstance(value, dict):
        return {str(k): sanitize(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        # Diagnostic collections must not silently lose the only late failure.
        # Source and secrets are still removed recursively from every entry.
        if key in {'findings', 'blocking_findings', 'unsupported_reasons', 'candidates', 'stages'}:
            return [sanitize(item) for item in value]
        items = [sanitize(item) for item in value[:80]]
        if len(value) > 80:
            items.append({'_omitted_items': len(value) - 80, '_total_items': len(value)})
        return items
    if isinstance(value, str):
        return _redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_text(str(value))


def _recent_tool_events(events: list[dict]) -> list[dict]:
    selected = []
    for event in events:
        kind = str(event.get("type") or "")
        if kind == "tool_use":
            selected.append({
                "type": kind,
                "name": str(event.get("name") or ""),
                "args": sanitize(event.get("input") or event.get("args") or {}),
            })
        elif kind == "tool_result":
            raw = event.get("result") or event.get("content") or event.get("error") or ""
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except (ValueError, TypeError):
                    pass
            selected.append({
                "type": kind,
                "name": str(event.get("name") or ""),
                "is_error": bool(event.get("is_error") or event.get("error")),
                "result": sanitize(raw),
            })
        elif kind in {"error", "stopped"}:
            selected.append(sanitize(event))
    return selected[-30:]


def _execution_trace(runs: list[dict]) -> list[dict]:
    """Keep execution identity/state while omitting prompts, arguments and results."""
    trace = []
    for snapshot in runs[-5:]:
        run = snapshot.get("run") or {}
        trace.append({
            "run": {key: sanitize(run.get(key)) for key in (
                "id", "thread_id", "provider_model", "target_pid", "solution",
                "status", "current_step", "model_calls", "tokens_in", "tokens_out",
                "started_at", "finished_at", "error",
            )},
            "steps": [{key: sanitize(step.get(key)) for key in (
                "id", "sequence", "kind", "status", "started_at", "finished_at", "error",
            )} for step in (snapshot.get("steps") or [])[-20:]],
            "tools": [{key: sanitize(tool.get(key)) for key in (
                "id", "step_id", "tool_call_id", "tool_name", "category", "danger",
                "readonly", "target_pid", "status", "started_at", "finished_at", "error",
            )} for tool in (snapshot.get("tool_executions") or [])[-40:]],
            "approvals": [{key: sanitize(approval.get(key)) for key in (
                "id", "tool_execution_id", "status", "created_at", "resolved_at",
            )} for approval in (snapshot.get("approvals") or [])[-20:]],
        })
    return trace


def _log_tail(project_dir: Path, max_lines: int = 160) -> list[str]:
    candidates = [project_dir.parent / "_backend.log", project_dir / "_backend.log"]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            return [_redact_text(line, 1200) for line in lines[-max_lines:]]
        except OSError:
            continue
    return []


def _safe_name(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_-]+", "-", str(value or "report")).strip("-")
    return text[:48] or "report"


def _markdown(payload: dict) -> str:
    user = payload["report"]
    lines = [
        f"# TwinCAT Agent {'Bug 报告' if user['type'] == 'bug' else '建议报告'}",
        "",
        f"- 报告 ID：`{payload['report_id']}`",
        f"- 创建时间：{payload['created_at']}",
        f"- Agent 版本：{payload['environment']['agent_version']}",
        f"- 标题：{user['title']}",
        "",
        "## 描述", "", user["description"] or "（未填写）", "",
    ]
    if user["type"] == "bug":
        lines += ["## 复现步骤", ""]
        lines += [f"{index}. {step}" for index, step in enumerate(user["steps"], 1)] or ["（未填写）"]
        lines += ["", "## 预期结果", "", user["expected"] or "（未填写）",
                  "", "## 实际结果", "", user["actual"] or "（未填写）"]
    else:
        lines += ["## 建议方案", "", user["proposal"] or "（未填写）",
                  "", "## 预期收益", "", user["benefit"] or "（未填写）"]
    lines += [
        "", "## 诊断摘要", "",
        f"- XAE：`{json.dumps(payload['xae'], ensure_ascii=False)}`",
        f"- 当前任务：`{payload['conversation'].get('title', '')}`",
        f"- 最近工具事件：{len(payload['recent_tool_events'])} 条",
        f"- 后台日志：{len(payload['backend_log_tail'])} 行",
        "", "完整脱敏诊断见 ZIP 内 `report.json`。PLC 代码正文与凭据默认不包含。", "",
    ]
    return "\n".join(lines)


def create_report(*, project_dir: Path, agent_version: str, solution: str,
                  conversation: dict, xae: dict, args: dict) -> dict:
    report_type = str(args.get("report_type") or "bug").casefold()
    if report_type not in {"bug", "suggestion"}:
        raise ValueError("report_type must be 'bug' or 'suggestion'")
    title = _redact_text(str(args.get("title") or "未命名报告"), 200)
    now = datetime.now().astimezone()
    report_id = f"TCA-{now:%Y%m%d-%H%M%S}-{_safe_name(title)}"
    solution_path = Path(solution) if solution else None
    output_dir = (
        solution_path.parent / ".TwinCATAgent" / "reports"
        if solution_path else project_dir / "reports"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "report_id": report_id,
        "created_at": now.isoformat(timespec="seconds"),
        "report": sanitize({
            "type": report_type,
            "title": title,
            "description": args.get("description") or "",
            "steps": list(args.get("steps") or []),
            "expected": args.get("expected") or "",
            "actual": args.get("actual") or "",
            "proposal": args.get("proposal") or "",
            "benefit": args.get("benefit") or "",
        }),
        "environment": {
            "agent_version": agent_version,
            "python": sys.version.split()[0],
            "os": platform.platform(),
            "process_id": os.getpid(),
        },
        "xae": sanitize(xae),
        "conversation": sanitize({
            "id": conversation.get("thread", {}).get("id", ""),
            "title": conversation.get("thread", {}).get("title", ""),
            "status": conversation.get("thread", {}).get("status", ""),
            "database": conversation.get("database", ""),
        }),
        "recent_tool_events": _recent_tool_events(conversation.get("events") or []),
        "execution_trace": _execution_trace(conversation.get("execution_trace") or []),
        "backend_log_tail": _log_tail(project_dir),
    }
    json_text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    markdown = _markdown(payload)
    archive = output_dir / f"{report_id}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("report.md", markdown.encode("utf-8"))
        bundle.writestr("report.json", json_text.encode("utf-8"))
    return {
        "status": "created", "report_id": report_id,
        "report_type": report_type, "file": str(archive),
        "privacy": "credentials and PLC source omitted; recent tool diagnostics sanitized",
        "size": archive.stat().st_size,
    }
