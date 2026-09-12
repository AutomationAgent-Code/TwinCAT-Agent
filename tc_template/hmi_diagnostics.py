"""Bounded, project-scoped HMI diagnostics. Never start servers or write ADS."""
from __future__ import annotations

import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path


def read_server_log(solution: str, project_file: str, *, now: float | None = None,
                    limit: int = 100) -> dict:
    """Read saved Engineering Server events, not a claim about live PLC health.

    This schema/time unit is verified against the installed TE2000 logger.
    Unknown schemas, locked databases and missing files remain unavailable.
    Only the known status-check event's non-secret parameters are returned.
    """
    result = {"available": False, "source": "HMI Engineering Server logger.db",
              "lookback_seconds": 3600, "events": [], "live_state_verified": False}
    if not solution or not project_file:
        return {**result, "reason": "Missing XAE solution/project identity."}
    root = Path(solution).resolve().parent
    project = Path(project_file).resolve()
    # Never interpret an arbitrary name/path as a log directory or follow an
    # escaping symlink. Linked HMI projects cannot safely use this convention.
    if (project.suffix.lower() != '.hmiproj' or not project.is_relative_to(root)
            or not project.stem or project.stem in {'.', '..'}):
        return {**result, "reason": "HMI project is outside the selected solution."}
    path = (root / '.engineering_servers' / project.stem / 'logger.db').resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return {**result, "reason": "Project-scoped logger database is unavailable."}
    current = time.time() if now is None else now
    limit = max(1, min(int(limit), 200))
    cutoff = int((current - 3600) * 1_000_000_000)
    try:
        # mode=ro preserves a live database and its journal; immutable would
        # incorrectly ignore in-flight transactions. Do not checkpoint/migrate.
        connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=1)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA query_only=ON')
            rows = connection.execute(
                'SELECT id, domain, eventName, severity, timeReceived '
                'FROM event_with_msg_or_alarm_as_payload '
                'WHERE timeReceived >= ? AND timeReceived <= ? ORDER BY id DESC LIMIT ?',
                (cutoff, int(current * 1_000_000_000), limit + 1)).fetchall()
            for row in rows[:limit]:
                event = str(row['eventName'] or '')[:512]
                entry = {"id": row['id'], "domain": str(row['domain'] or '')[:255],
                         "event": event, "severity_raw": row['severity'],
                         "received_at": datetime.fromtimestamp(row['timeReceived'] / 1e9,
                             timezone.utc).isoformat(),
                         "age_seconds": max(0, round(current - row['timeReceived'] / 1e9)),
                         "fault_record": bool(re.search(r'(?:^|_)(?:ERROR|FAILED|FAILURE)(?:_|$)', event))}
                if event == 'TWINCAT_RUNTIME_STATUS_CHECK_ERROR':
                    params = dict(connection.execute(
                        'SELECT name, data FROM msg_or_alarm_parameter WHERE id=? '
                        "AND name IN ('0','1','2','3') ORDER BY position", (row['id'],)).fetchall())
                    entry['connection_error'] = {
                        key: str(params.get(str(index)) or '')[:255]
                        for index, key in enumerate(('runtime', 'code', 'symbol', 'netid'))}
                result['events'].append(entry)
            result.update(available=True, truncated=len(rows) > limit,
                          observed_fault_records=sum(e['fault_record'] for e in result['events']))
        finally:
            connection.close()
    except (sqlite3.Error, OSError, ValueError, OverflowError) as exc:
        return {**result, "available": False, "reason": str(exc)}
    result['note'] = ('Historical records in the last hour, not active alarms or proof of current failure. '
                      'An empty/truncated log does not prove PLC connectivity; no ADS access was performed.')
    return result


def enrich_diagnostics(result: dict, *, check_page: bool = False) -> dict:
    """Keep build, diagnostic availability, HTTP reachability and ADS separate."""
    from . import _ps_bridge as ps
    result = dict(result)
    if not (result.get('error_list') or {}).get('available'):
        # Exact PID and solution match are required. An older extension simply
        # stays unavailable; diagnostics recovery must never trigger a build.
        try:
            from .xae_build_pipe import request_diagnostics
            pid = ps._TOOL_TARGET_PID.get()
            snapshot = request_diagnostics(pid) if pid else None
            recovery = {'source': 'XAE UI-thread read-only pipe', 'available': False}
            if not pid:
                recovery['reason'] = 'No exact XAE PID selected.'
            elif not snapshot:
                recovery['reason'] = 'No response from the selected XAE diagnostics pipe.'
            elif snapshot.get('error') == 'unsupported request':
                recovery['reason'] = 'Loaded XAE extension does not support read-only diagnostics; update the extension and reopen XAE.'
                recovery['code'] = 'extension_update_required'
            else:
                recovery['reason'] = 'Diagnostics response unavailable or solution identity not verified.'
            result['diagnostics_recovery'] = recovery
            expected = str(result.get('solution') or '')
            if (snapshot and snapshot.get('ok') is True and snapshot.get('buildPerformed') is False
                    and snapshot.get('diagnosticsAvailable') is True and expected
                    and Path(snapshot.get('solution', '')).resolve() == Path(expected).resolve()):
                entries = list(snapshot.get('errors') or []) + list(snapshot.get('warnings') or [])
                result['error_list'] = {'available': True, 'complete': True, 'entries': entries,
                    'total': len(entries), 'source': 'XAE UI-thread read-only pipe',
                    'scope': 'entire_solution', 'note': 'Snapshot may include other projects; no rebuild performed.'}
                result['diagnostics_pending'] = bool(snapshot.get('diagnosticsPending'))
                result['diagnostics_recovery'] = {'source': recovery['source'], 'available': True}
                if result.get('build_succeeded') is True and not entries and not result['diagnostics_pending']:
                    result.update(status='built', success=True)
        except Exception as exc:
            result['diagnostics_recovery'] = {'available': False,
                'reason': 'Read-only diagnostics recovery failed.', 'error_type': type(exc).__name__}
    error_list = result.get('error_list') or {}
    result['diagnostics_available'] = error_list.get('available') is True
    result['diagnostics_complete'] = error_list.get('complete') is True
    observed_errors = [item for item in (error_list.get('entries') or [])
                       if str(item.get('severity') or '').lower() == 'error']
    observed_warnings = [item for item in (error_list.get('entries') or [])
                         if str(item.get('severity') or '').lower() == 'warning']
    result['error_count'] = len(observed_errors) if result['diagnostics_available'] else None
    result['warning_count'] = len(observed_warnings) if result['diagnostics_available'] else None
    result['warning_review_required'] = bool(observed_warnings)
    complete = (result.get('build_succeeded') is True and
                result['diagnostics_available'] and result['diagnostics_complete'] and
                not observed_errors and not result.get('diagnostics_pending'))
    if result.get('build_succeeded') is not None:
        result['success'] = complete and not observed_warnings
        result['status'] = ('failed' if result.get('build_succeeded') is False
                            else 'review_required' if complete and observed_warnings
                            else 'succeeded' if complete else 'incomplete')
    elif observed_warnings and not observed_errors and result['diagnostics_complete']:
        result['status'] = 'review_required'
    if observed_warnings:
        result['next_action'] = ('Review every warning in error_list.entries, including project, file and line. '
            'Do not declare acceptance passed or rebuild just to clear warnings. '
            'Saved diagnostics may include other projects or earlier builds; confirm relevance before editing.')
    result['page_verified'] = False
    result['runtime_bindings_verified'] = False
    result['plc_communication_verified'] = False
    result['acceptance'] = {
        'build': {'verified': result.get('build_succeeded') is True,
                  'source': 'DTE LastBuildInfo'},
        'diagnostics': {'available': result['diagnostics_available'],
                        'complete': result['diagnostics_complete']},
        'warning_review': {'required': bool(observed_warnings),
                           'status': 'pending' if observed_warnings else 'not_required' if result['diagnostics_complete'] else 'unknown',
                           'observed_count': result['warning_count']},
        'page': {'verified': False, 'required_tool': 'tc_hmi_browser_validate'},
        'runtime_bindings': {'verified': False, 'required_tool': 'tc_hmi_ads_live_check'},
    }
    result['server_log'] = read_server_log(str(result.get('solution') or ''),
                                         str(result.get('project_file') or ''))
    result['plc_communication'] = {"status": "not_checked", "verified": False,
        "next_action": "Use tc_hmi_ads_live_check for an explicit read-only endpoint/symbol check; never auto-start or activate PLC."}
    result['page'] = {"status": "not_checked", "verified": False}
    if check_page:
        try:
            runtime = ps.com_hmi_runtime_info(str(result.get('project_file') or result.get('project') or ''))
            result['page'] = {"status": "reachable" if runtime.get('application_ready') is True else "unavailable",
                "http_reachable": runtime.get('application_ready') is True,
                "application_url": runtime.get('application_url'),
                "server_running": runtime.get('server_running'),
                "browser_behavior_verified": False, "verified": False,
                "note": "HTTP entry probe only; not browser behavior, user interactions or PLC communication."}
        except Exception as exc:
            result['page'] = {"status": "unavailable", "verified": False, "reason": str(exc)}
    result['runtime_start_performed'] = False
    result['plc_write_performed'] = False
    return result


def read_hmi_diagnostics(project: str = "", check_page: bool = True) -> dict:
    from . import _ps_bridge as ps
    result = ps.ps_com('hmi-diagnostics', project=project, timeout=20)
    return enrich_diagnostics(result, check_page=check_page)
