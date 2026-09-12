"""Small, deterministic policies shared by tool execution and model routing."""
from __future__ import annotations

import json


def request_action_scope(request: str) -> dict[str, object]:
    """Compatibility metadata; natural-language text no longer vetoes tools.

    The actual permission mode, tool allocation, explicit approval and
    device/project safety gates remain authoritative.  This function is kept
    for callers that display scope diagnostics, but it intentionally performs
    no keyword classification.
    """
    return {
        "mode": "normal",
        "allowed": {"code", "runtime"},
    }


def request_action_blocks(request: str, tool_name: str, readonly: bool) -> bool:
    """Deprecated compatibility shim: never veto based on request wording."""
    return False


def readonly_request_blocks(request: str, readonly: bool) -> bool:
    """Deprecated compatibility shim: never veto based on request wording."""
    return False
import asyncio
import threading
from pathlib import Path


async def cancellable_thread_call(invoke):
    """Cancel queued work; already-entered external calls remain uncertain."""
    cancelled = threading.Event()

    def checkpoint():
        if cancelled.is_set():
            raise asyncio.CancelledError("Tool cancelled before dispatch")

    try:
        return await asyncio.to_thread(invoke, checkpoint)
    except asyncio.CancelledError:
        cancelled.set()
        raise


def tool_succeeded(result: object) -> bool:
    """Honor explicit failure flags consistently across transports and loops."""
    if not isinstance(result, dict):
        return True
    from tc_agent.tool_error_catalog import FAILURE_STATUSES
    if result.get('status') in FAILURE_STATUSES:
        return False
    if gate_rejected(result):
        return False
    if result.get('engine') == 'TCSA' and int((result.get('summary') or {}).get('errors', 0)) > 0:
        return False
    if (bool(result.get("error")) or "denied" in result or result.get("isError") is True
            or any(result.get(key) is False for key in ("ok", "success", "verified"))
            or str(result.get("status", "")).lower() in
            {"failed", "error", "denied", "cancelled", "uncertain", "incomplete", "review_required",
             "conflict", "unknown", "unavailable", "unsupported", "not_found", "invalid_arguments",
             "verification_failed", "approval_expired"}
            or result.get("warning_review_required") is True
            or result.get("diagnosticsPending") is True
            or result.get("diagnostics_pending") is True):
        return False
    if result.get('status') == 'diagnostics_only' and result.get('diagnostics_complete') is True:
        # The read succeeded; this does not mean the compiler found no errors.
        return True
    for key in ("failedProjects", "errorCount", "failed_projects", "error_count"):
        if key in result:
            try:
                if int(result[key]) != 0:
                    return False
            except (TypeError, ValueError):
                return False
    return True


def gate_rejected(result: object) -> bool:
    if not isinstance(result, dict):
        return False
    review = result.get("review")
    return (str(result.get("status", "")).lower() in {"blocked", "conflict", "unknown", "unavailable"}
            or (isinstance(review, dict) and review.get("approved") is False)
            or (result.get("written") is True and result.get("retry_safe") is False)
            or (result.get("written") is False and result.get("status") not in {"preview", "preflight_passed"}))


def blocked_batch_result() -> dict:
    return {"status": "blocked", "written": False,
            "error": "本批次此前写入被门禁拒绝，后续写操作未执行。",
            "next_action": "先读取此前拒绝原因并修复，再重新提交依赖写入；只读检查仍可执行。"}


def saved_read(name: str, arguments: dict) -> bool:
    """Only known saved-source reads may be deduplicated; live reads must run."""
    if any(arguments.get(key) for key in ("live", "refresh", "force_refresh")):
        return False
    # PLC files can change in XAE without an Agent write (including old extensions
    # without push events). Let the scoped/editor and SQLite caches revalidate on
    # every tool call; never hide those checks behind whole-turn memoization.
    return name == "tc_hmi_read"


def read_signature(name: str, arguments: dict) -> str:
    normalized = dict(arguments or {})
    if name == "tc_hmi_read":
        normalized = {
            "project": str(normalized.get("project") or ""),
            "file": str(normalized.get("file") or "").replace("\\", "/").lower(),
            "control_id": str(normalized.get("control_id") or ""),
            "include_content": bool(normalized.get("include_content", not bool(normalized.get("control_id")))),
            "control_offset": max(0, int(normalized.get("control_offset") or 0)) if not normalized.get("control_id") else 0,
            "content_offset": max(0, int(normalized.get("content_offset") or 0)) if normalized.get("include_content", not bool(normalized.get("control_id"))) else 0,
        }
    return name + "|" + json.dumps(
        normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _read_limits(arguments: dict) -> tuple[int, int]:
    include_content = arguments.get('include_content', not bool(arguments.get('control_id')))
    return (1 if arguments.get('control_id') else max(1, min(int(arguments.get('max_controls') or 40), 5000)),
            max(1, min(int(arguments.get('max_chars') or 12000), 1000000)) if include_content else 0)


def _unchanged_source(result: dict) -> bool:
    if result.get('source_consistent') is False:
        return False
    if not result.get('full_path'):
        # Legacy/test results have no fingerprint; never infer completeness.
        return True
    try:
        stat = Path(result['full_path']).stat()
        return (stat.st_size == result['source_size'] and
                stat.st_mtime_ns // 100 + 621355968000000000 == result['source_mtime_ticks'])
    except (OSError, KeyError, TypeError, ValueError):
        return False


class ReadPolicy:
    """Deduplicate only already-delivered HMI ranges on the same saved version."""

    def __init__(self):
        self._completed: dict[str, list[tuple[dict, dict]]] = {}
        self._cache_revision = -1

    def _refresh(self):
        from tc_agent.plc_cache import CACHE
        if self._cache_revision != CACHE.revision:
            self._completed.clear()
            self._cache_revision = CACHE.revision

    def duplicate(self, name: str, arguments: dict) -> bool:
        self._refresh()
        if not saved_read(name, arguments):
            return False
        controls, chars = _read_limits(arguments)
        include_content = arguments.get('include_content', not bool(arguments.get('control_id')))
        key = read_signature(name, arguments)
        records = self._completed.get(key, [])
        records[:] = [(a, r) for a, r in records if _unchanged_source(r)]
        for previous, result in records:
            if _read_limits(previous) == (controls, chars):
                return True
            controls_covered = (result.get('controls_truncated') is False or
                                controls <= result.get('returned_control_count', 0))
            content_covered = (not include_content or result.get('truncated') is False or
                               chars <= result.get('content_chars', 0))
            bindings_covered = result.get('bindings_truncated') is False
            if controls_covered and content_covered and bindings_covered:
                return True
        return False

    def record(self, name: str, arguments: dict, *, ok: bool, result: dict | None = None) -> None:
        self._refresh()
        if ok and saved_read(name, arguments):
            snapshot = dict(result or {})
            if _unchanged_source(snapshot):
                records = self._completed.setdefault(read_signature(name, arguments), [])
                records.append((dict(arguments), snapshot))
                del records[:-8]

    def invalidate(self) -> None:
        self._completed.clear()


class FailurePolicy:
    """Turn-local deterministic failures only; transient failures remain retryable."""
    def __init__(self):
        self.records = {}
        self.missing_hmi = set()
        self.plc_revision = 0
        self.plc_failed_revision = None
        self.plc_failed_builds = 0
        self.plc_diagnostics_pending = False
        self.scope = None
        self.diagnostic_scope = ''
        self.source_versions = {}
        self.semantic_failures = {}

    def bind(self, pid, solution):
        """Never carry failure evidence across host/solution identities."""
        scope = (int(pid or 0), str(solution or '').replace('\\', '/').casefold())
        if self.scope is not None and scope != self.scope:
            self.__init__()
        self.scope = scope

    @staticmethod
    def project_scope(args):
        # Only an exact TIPC path is comparable across source tools. A build
        # without project provenance remains solution-scoped, not guessed.
        path = str(args.get('tree_path') or args.get('path') or args.get('parent_path') or '')
        parts = path.split('^')
        return parts[1].casefold() if len(parts) > 1 and parts[0].upper() == 'TIPC' else ''

    def check(self, name, args):
        from tc_agent.tool_preconditions import is_source_mutation_tool
        scope = self.project_scope(args)
        pending = self.plc_diagnostics_pending and not (
            scope and self.diagnostic_scope and scope != self.diagnostic_scope)
        if pending and name in {'plc_build', 'plc_verify'}:
            return {'status': 'blocked', 'retry_safe': False, 'not_executed': True,
                    'error_type': 'diagnostics_pending', 'recoverable': True,
                    'error': 'PLC compiler diagnostics are incomplete; another build cannot replace missing evidence.',
                    'next_action': 'Use plc_diagnostics to recover exact compiler locations before source repair or another build.'}
        preview = (args.get('apply') is False or
                   (name in {'plc_restore_snapshot', 'plc_git_sync'}
                    and not args.get('apply')))
        if pending and is_source_mutation_tool(name) and not preview:
            return {'status': 'blocked', 'written': False, 'retry_safe': False,
                    'not_executed': True, 'error_type': 'diagnostics_pending', 'recoverable': True,
                    'error': 'PLC compiler diagnostics are incomplete; speculative source edits are blocked.',
                    'next_action': 'Use plc_diagnostics to recover exact compiler locations first.'}
        if name in {'plc_build', 'plc_verify'} and (
                self.plc_failed_builds >= 4 or self.plc_failed_revision == self.plc_revision):
            return {'status': 'blocked', 'retry_safe': False,
                    'error': 'PLC repair loop stopped: no source progress or three repair rounds exhausted.',
                    'next_action': 'Use plc_diagnostics when diagnostics are incomplete. Otherwise correct the exact source location before building; report remaining errors if the repair budget is exhausted.'}
        key = read_signature(name, args)
        if key in self.records:
            return {'status': 'blocked', 'retry_safe': False,
                    'error': 'Repeated deterministic failure; no operation performed.',
                    'previous_failure': self.records[key],
                    'next_action': 'Change the invalid arguments or resolve the prerequisite; do not repeat this request.'}
        if name in {'tc_hmi_project_info', 'tc_hmi_structure', 'tc_hmi_source_catalog'} and str(args.get('project') or '') in self.missing_hmi:
            return {'status': 'blocked', 'error': 'Selected HMI project is known to be absent in this turn.',
                    'next_action': 'List solution projects; create a project only when authorized.'}

    def record(self, name, args, result, ok, readonly):
        if not isinstance(result, dict):
            return
        if name in {'plc_preflight', 'plc_write', 'plc_patch'}:
            scope = self.project_scope(args)
            candidates = args.get('candidates') or []
            scopes = {self.project_scope(c) for c in candidates}
            if not scope and len(scopes) == 1:
                scope = next(iter(scopes))
            reasons = set()
            def collect(value):
                if isinstance(value, dict):
                    evidence = value.get('semantic_evidence') or {}
                    if evidence.get('status') == 'incomplete':
                        reasons.update(evidence.get('unsupported_reasons') or [])
                    for key, child in value.items():
                        if key != 'semantic_evidence':
                            collect(child)
                elif isinstance(value, list):
                    for child in value:
                        collect(child)
            collect(result)
            if scope and reasons and not ok:
                repeated = []
                for reason in reasons:
                    key = (scope, reason)
                    self.semantic_failures[key] = self.semantic_failures.get(key, 0) + 1
                    if self.semantic_failures[key] >= 2:
                        repeated.append(reason)
                if repeated:
                    result['capability_exhausted'] = True
                    result['unsupported_reasons'] = sorted(repeated)
                    result['next_action'] = '同一离线解析能力缺口已重复出现；暂停依赖该能力的步骤，报告缺失签名或语义支持及已完成修改。可继续独立且已授权的工作；禁止拆分写入、换工具试错或手工绕过门禁。无独立工作时报告部分完成与阻塞。'
            elif ok and scope and name != 'plc_preflight':
                # A successful fragment preflight does not establish that a
                # previously missing capability has been repaired. Otherwise
                # alternating failing calls with trivial fragments loops forever.
                self.semantic_failures = {k: v for k, v in self.semantic_failures.items() if k[0] != scope}
        if ok and name in {'plc_lib_add', 'plc_lib_remove', 'plc_placeholder_add'}:
            self.semantic_failures.clear()
        if name in {'plc_read', 'plc_read_current'} and ok:
            path = str(result.get('path') or args.get('path') or '')
            hashes = result.get('source_hashes')
            if path.startswith('TIPC^') and isinstance(hashes, dict) and hashes and not result.get('truncated'):
                key = path.casefold()
                previous = self.source_versions.get(key, {})
                changed = any(area in previous and previous[area] != value for area, value in hashes.items())
                self.source_versions[key] = {**previous, **hashes}
                if changed and (not self.diagnostic_scope or self.project_scope({'path': path}) == self.diagnostic_scope):
                    self.plc_revision += 1
        if name == 'plc_diagnostics':
            recovered_scope = self.project_scope(result)
            matching = (recovered_scope == self.diagnostic_scope if self.diagnostic_scope else
                        result.get('diagnostic_scope') != 'project')
            if self.scope:
                result_solution = str(result.get('solution') or '').replace('\\', '/').casefold()
                if result_solution and result_solution != self.scope[1]:
                    matching = False
                if result.get('xaePid') and int(result['xaePid']) != self.scope[0]:
                    matching = False
            if result.get('diagnostics_complete') is True and matching:
                self.plc_diagnostics_pending = False
                if result.get('errorCount') == 0 and result.get('failedProjects') == 0:
                    self.plc_failed_revision = None
                elif type(result.get('errorCount')) is int and result.get('errorCount') > 0:
                    self.plc_failed_revision = self.plc_revision
            return
        if name == 'plc_verify':
            build = next((s.get('result') for s in result.get('stages', [])
                          if s.get('stage') == 'build'), None)
            if isinstance(build, dict):
                from tc_template.plc_build_diagnostics import build_diagnostics
                self.record('plc_build', args, build_diagnostics(build), ok, True)
            return
        if name == 'plc_build' and 'compiler_verified' in result:
            performed = (result.get('buildPerformed') if 'buildPerformed' in result
                         else result.get('build_performed'))
            self.plc_diagnostics_pending = (performed is True
                                            and result.get('diagnostics_complete') is not True)
            if performed is True:
                self.diagnostic_scope = self.project_scope(result)
            if result['compiler_verified'] is True:
                self.plc_failed_builds = 0
                self.plc_failed_revision = None
            else:
                # Collection failures must not consume source-repair rounds.
                if result.get('diagnostics_complete') is True:
                    self.plc_failed_builds += 1
                    self.plc_failed_revision = self.plc_revision
        elif (name.startswith('plc_') and not readonly and ok
              and result.get('written') is True and result.get('verified') is not False
              and name not in {'plc_write_value', 'plc_write_values'}):
            self.plc_revision += 1
        if ok and not readonly:
            self.records.clear()
            self.missing_hmi.clear()
            return
        if ok:
            return
        # A temporary precondition rejection performed no write. Re-evaluate
        # the live gate after recovery; retry_safe=false alone is not a cache key.
        if (result.get('error_type') == 'diagnostics_pending' or
                (result.get('written') is not True and 'denied' not in result
                 and not result.get('authorization_blocked')
                 and str(result.get('status', '')).lower() in
                 {'conflict', 'unavailable', 'unknown', 'approval_expired'})):
            return
        message = str(result.get('error') or result.get('denied') or '')
        if ('No TwinCAT HMI .hmiproj entry' in message or 'No saved HMI project reference' in message):
            self.missing_hmi.add(str(args.get('project') or ''))
        deterministic = (result.get('retry_safe') is False or 'denied' in result or
            any(s in message for s in ('invalid_object_path', 'Attribute does not exist',
                'Unknown installed control', 'Unknown/cyclic installed control',
                'member list', 'At least one dynamic ADS symbol')) or result.get('written') is True and result.get('verified') is False)
        if deterministic:
            self.records[read_signature(name, args)] = {k: result[k] for k in
                ('status', 'error', 'denied', 'next_action', 'candidates', 'written', 'verified') if k in result}
