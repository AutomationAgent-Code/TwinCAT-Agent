"""
_ps_bridge.py — compatibility facade for TwinCAT COM commands.

Production uses the packaged native pywin32 bridge where available.  SYSTEM
Manager commands are deliberately routed through TcCom.ps1 in automatic mode
because the user-level updater can replace the app layer without replacing the
architecture-specific helper cache.  The public ``ps_com`` name is retained
for compatibility; set ``TC_AGENT_COM_BACKEND=powershell`` to force the same
route for all COM commands.

Contract with TcCom.ps1:
    powershell.exe -File TcCom.ps1 -Command <cmd> -ArgsJson '<json>'
    -> single line of JSON on stdout: {"ok":true,"data":...} | {"ok":false,"error":...}
ConvertTo-Json escapes all non-ASCII to \\uXXXX, so stdout is pure ASCII and
encoding-safe regardless of the console code page.

Must use Windows PowerShell 5.1 (powershell.exe), NOT pwsh 7+: the latter
removed [Marshal]::GetActiveObject, which is how we attach to the running XAE.
"""

from __future__ import annotations

import json
import ctypes
import os
import re
import shutil
import struct
import subprocess
import tempfile
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

_PS1 = Path(__file__).with_name("TcCom.ps1")
_IO_PS1_CANDIDATES = (
    Path(__file__).with_name("TcIoConfiguration.ps1"),
    Path(__file__).resolve().parent.parent / "scripts" / "Invoke-TcIoConfiguration.ps1",
)
_TOOL_TARGET_PID: ContextVar[int] = ContextVar("tc_tool_target_pid", default=0)
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# The SYSTEM Manager contract is implemented authoritatively in TcCom.ps1.
# Keeping these commands on the PowerShell path also prevents an older
# architecture-specific packaged worker from silently hiding newly added
# SYSTEM capabilities after a user-level app update.
_POWERSHELL_SYSTEM_COMMANDS = frozenset({
    "platform-list", "platform-show", "platform-set", "platform-target-info",
    "system-structure", "system-settings", "system-settings-set",
    "core-info", "core-assign", "realtime-info", "task-info", "task-core-assign",
    "task-settings-set", "plc-runtimes", "plc-online-state",
    "login", "logout", "start", "stop",
    "system-add", "system-remove", "realtime-refresh",
    "safety-structure", "safety-project-info", "safety-files", "safety-target-info",
    "safety-aliases", "safety-application", "safety-logic-check", "safety-validate",
    "safety-import", "safety-create", "safety-export", "safety-remove", "safety-delete",
    "hmi-project-info", "hmi-structure", "hmi-read", "hmi-write-markup", "hmi-ads-info",
    "hmi-validate", "hmi-create-view", "hmi-project-api", "hmi-startup-view-set", "hmi-control-edit", "hmi-controls-batch", "hmi-delete-view", "hmi-dynamic-symbols-set",
    "hmi-bind-plc-apply", "hmi-item-apply", "hmi-item-delete", "hmi-native-server-symbol-read",
    "hmi-ads-runtime-set", "hmi-ads-symbols", "hmi-ads-symbol-set", "hmi-bindings",
    "hmi-internal-symbols", "hmi-internal-symbol-set",
    "hmi-localizations", "hmi-localization-set", "hmi-themes",
    "hmi-themed-resource-set", "hmi-active-theme-set",
    "hmi-user-controls", "hmi-user-control-create", "hmi-user-control-parameter-set",
    "hmi-user-control-delete", "hmi-framework-templates", "hmi-framework-validate",
    "hmi-framework-control-info", "hmi-framework-attribute-set",
    "hmi-framework-event-set", "hmi-framework-create", "hmi-framework-pack",
    "hmi-framework-packages", "hmi-framework-package-inspect",
    "hmi-framework-install", "hmi-framework-uninstall",
    "hmi-runtime-info", "hmi-server-control", "hmi-browser-validate", "hmi-build", "hmi-diagnostics",
})

_TREE_PATH_ARGUMENTS = frozenset({"path", "parent_path", "task_path"})

_HMI_PROJECT_SCOPED_COMMANDS = frozenset({
    "hmi-project-info", "hmi-structure", "hmi-read", "hmi-write-markup", "hmi-ads-info",
    "hmi-validate", "hmi-create-view", "hmi-project-api", "hmi-startup-view-set", "hmi-control-edit", "hmi-controls-batch",
    "hmi-delete-view", "hmi-dynamic-symbols-set", "hmi-bind-plc-apply", "hmi-item-apply", "hmi-item-delete", "hmi-native-server-symbol-read",
    "hmi-ads-runtime-set", "hmi-ads-symbols", "hmi-ads-symbol-set", "hmi-bindings",
    "hmi-internal-symbols", "hmi-internal-symbol-set", "hmi-localizations",
    "hmi-localization-set", "hmi-themes", "hmi-themed-resource-set", "hmi-active-theme-set",
    "hmi-user-controls", "hmi-user-control-create", "hmi-user-control-parameter-set",
    "hmi-user-control-delete", "hmi-framework-packages", "hmi-framework-install",
    "hmi-framework-uninstall", "hmi-runtime-info", "hmi-server-control",
    "hmi-browser-validate", "hmi-build", "hmi-diagnostics",
})


def _normalize_tree_path_arguments(args: dict) -> None:
    """Accept slash-separated tree paths from shells that consume ``^``.

    Windows PowerShell/cmd can remove a caret while launching a native
    executable, so ``TIRT^PlcTask`` may arrive as ``TIRTPlcTask`` before
    Click sees it.  Slash-separated paths are an unambiguous CLI spelling;
    convert them at the bridge boundary while retaining TwinCAT's canonical
    caret paths internally and in results.
    """
    roots = ("TIRC", "TIRS", "TIRT", "TIPC", "TIID", "TINC")
    for key in _TREE_PATH_ARGUMENTS:
        value = args.get(key)
        if not isinstance(value, str) or "/" not in value:
            continue
        text = value.strip()
        if any(text.upper().startswith(root + "/") for root in roots):
            args[key] = text.replace("/", "^")


def _solution_hmi_projects(solution_file: str) -> list[str]:
    """Resolve exact .hmiproj entries from one selected .sln, including nesting."""
    solution = Path(solution_file or "").resolve()
    if solution.suffix.lower() != ".sln" or not solution.is_file():
        return []
    text = solution.read_text(encoding="utf-8-sig", errors="replace")
    entries = re.findall(
        r'^Project\("[^"]+"\)\s*=\s*"[^"]+",\s*"([^"]+\.hmiproj)",',
        text, flags=re.IGNORECASE | re.MULTILINE,
    )
    result = []
    for relative in entries:
        candidate = (solution.parent / Path(relative.replace("\\", "/"))).resolve()
        if candidate.is_file() and str(candidate).casefold() not in {value.casefold() for value in result}:
            result.append(str(candidate))
    return result


def _bind_hmi_project_selector(command: str, args: dict, timeout: float) -> dict:
    """Turn an omitted HMI selector into one exact solution path before COM."""
    if command not in _HMI_PROJECT_SCOPED_COMMANDS or str(args.get("project") or "").strip():
        return args
    context = _ps_com_raw("connect-check", min(timeout, 20.0))
    solution = str((context or {}).get("solution") or "")
    requested_pid = _TOOL_TARGET_PID.get()
    actual_pid = int((context or {}).get("pid") or 0)
    if requested_pid and actual_pid and actual_pid != requested_pid:
        raise TcComError(
            f"HMI resolver attached to XAE PID {actual_pid}, expected PID {requested_pid}; refusing to retarget."
        )
    projects = _solution_hmi_projects(solution)
    if len(projects) == 1:
        return dict(args, project=projects[0])
    if len(projects) > 1:
        active = str(((context or {}).get("active_document") or {}).get("full_name") or "")
        active_note = f" Active document: {active}." if active else ""
        raise TcComError(
            "Multiple TwinCAT HMI projects are registered in the selected solution; "
            "specify project explicitly: " + ", ".join(projects) + active_note
        )
    raise TcComError(
        f"No TwinCAT HMI .hmiproj entry was found in the selected solution: {solution or '(unknown)'}"
    )


class TcComError(RuntimeError):
    """A COM operation reported ok:false (e.g. XAE not running, object not found)."""


class PlcReviewError(ValueError):
    """A proposed PLC change violates the mandatory pre-write review gate."""


def _process_is_64bit(pid: int) -> bool | None:
    """Return target process bitness on Windows, or None if unknown."""
    if os.name != "nt" or pid <= 0:
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    handle = kernel32.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return None
    try:
        if hasattr(kernel32, "IsWow64Process2"):
            kernel32.IsWow64Process2.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_ushort),
                ctypes.POINTER(ctypes.c_ushort),
            ]
            kernel32.IsWow64Process2.restype = ctypes.c_int
            process_machine = ctypes.c_ushort()
            native_machine = ctypes.c_ushort()
            if kernel32.IsWow64Process2(
                handle, ctypes.byref(process_machine), ctypes.byref(native_machine)
            ):
                return process_machine.value == 0
        wow64 = ctypes.c_int()
        kernel32.IsWow64Process.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)
        ]
        kernel32.IsWow64Process.restype = ctypes.c_int
        if kernel32.IsWow64Process(handle, ctypes.byref(wow64)):
            return not bool(wow64.value) and struct.calcsize("P") == 8
    finally:
        kernel32.CloseHandle(handle)
    return None


def _native_helper(prefer_pid: int = 0) -> Path | None:
    """Return an architecture-matched packaged COM helper, if present.

    32-bit XAE uses ``runtime/com32``; 64-bit IDE 17 uses ``runtime/python``.
    Developers can override this path
    with TC_AGENT_COM_HELPER or force in-process diagnostics with ``direct``.
    """
    configured = os.environ.get("TC_AGENT_COM_HELPER", "").strip()
    if configured.lower() == "direct":
        return None
    if configured:
        candidate = Path(configured).expanduser().resolve()
        if not candidate.is_file():
            raise RuntimeError(f"Native COM helper not found: {candidate}")
        return candidate
    target_64 = _process_is_64bit(prefer_pid)
    if target_64 is None:
        target_64 = False
    runtime = Path(__file__).resolve().parent.parent.parent / "runtime"
    candidate = runtime / ("python" if target_64 else "com32") / "python.exe"
    # Installed builds always cross an OS process boundary, even when the
    # backend and XAE happen to have the same bitness. A wedged COM call is
    # then bounded by subprocess.run(timeout=...) and cannot freeze the HTTP/
    # WebSocket Agent process. Source checkouts without bundled runtimes keep
    # the direct path for developer diagnostics.
    if candidate.is_file():
        return candidate
    if target_64 and struct.calcsize("P") == 8:
        return None
    if not target_64 and struct.calcsize("P") <= 4:
        return None
    return None


def _native_request(kind: str, command: str, args: dict, timeout: float) -> object:
    helper = _native_helper(int(args.get("preferPid") or 0))
    if helper is None:
        if kind == "com":
            from ._native_bridge import available, dispatch

            ready, reason = available()
            if not ready:
                raise RuntimeError(
                    "Native COM bridge is unavailable (pywin32 missing): " + reason
                )
            return dispatch(command, args)
        from .io_native import execute

        return execute(command, **args)

    request = json.dumps(
        {"kind": kind, "command": command, "args": args}, ensure_ascii=False
    )
    try:
        proc = subprocess.run(
            [str(helper), "-m", "tc_template._native_worker"],
            input=request,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=_CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Native COM helper call '{command}' timed out after {timeout}s"
        ) from exc
    except OSError as exc:
        raise RuntimeError(f"Unable to start native COM helper: {exc}") from exc

    output = (proc.stdout or "").strip()
    if not output:
        detail = (proc.stderr or "").strip() or "empty output"
        raise RuntimeError(
            f"Native COM helper call '{command}' failed "
            f"(exit {proc.returncode}): {detail}"
        )
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Native COM helper call '{command}' returned invalid JSON: {output!r}"
        ) from exc
    if not payload.get("ok"):
        raise TcComError(payload.get("error") or "unknown native COM error")
    return payload.get("data")


def _powershell_exe() -> str:
    """Locate Windows PowerShell 5.1.

    TcXaeShell 15 is a 32-bit process.  Prefer SysWOW64 PowerShell so legacy
    TwinCAT 4024 COM registrations and proxies are resolved in the same bitness.
    """
    candidates = [
        os.path.expandvars(r"%SystemRoot%\SysWOW64\WindowsPowerShell\v1.0\powershell.exe"),
        os.path.expandvars(r"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"),
        "powershell.exe",
        shutil.which("powershell"),
    ]
    for c in candidates:
        if c and (os.path.isabs(c) is False or os.path.exists(c)):
            return c
    return "powershell.exe"


@contextmanager
def tool_target(prefer_pid: int = 0):
    """Bind all bridge calls in one Agent tool execution to one XAE process."""
    token = _TOOL_TARGET_PID.set(max(0, int(prefer_pid or 0)))
    try:
        yield
    finally:
        _TOOL_TARGET_PID.reset(token)


def ps_com(command: str, timeout: float = 120.0, **args) -> object:
    if command in {'member-baseline', 'document-baseline', 'save-document', 'library-evidence', 'library-signatures', 'compiler-settings'} or (command in {'delete-member', 'rename-member'} and args.get('expected_member_baseline') is not None):
        # Never fall back to a transport that could ignore the compare guard.
        pid = _TOOL_TARGET_PID.get()
        if not pid or not args.get('path'):
            from .member_baseline import conflict
            return conflict('A bound XAE PID and exact parent path are required')
        try:
            return _native_request('com', command, {**args, 'preferPid': pid, 'strictPid': True}, timeout)
        except Exception as exc:
            if command != 'save-document':
                raise
            # A helper timeout/lost response cannot prove Save was not executed.
            return {'status': 'uncertain', 'error_type': 'document_save', 'error': str(exc),
                    'written': 'unknown', 'not_executed': False, 'verified': False, 'retry_safe': False,
                    'next_action': '先只读检查文档和磁盘；禁止重放保存，不关闭或重开工程。'}
    if command in {'build', 'hmi-build'}:
        from .build_platform_contract import preflight
        blocked = preflight('hmi' if command == 'hmi-build' else 'plc',
                            lambda verb: _ps_com_raw(verb, min(timeout, 20), **{k: v for k, v in args.items() if k == 'preferPid'}))
        if blocked:
            return blocked
    if command == 'read-pou':
        from .plc_read_contract import validate_object_request, diagnose_read_failure
        validate_object_request(args.get('name', ''), args.get('path', ''))
        try:
            return _ps_com_raw(command, timeout, **args)
        except (RuntimeError, ValueError) as exc:
            # Fresh, read-only evidence after failure; do not reuse stale source
            # caches or change project state to make a read succeed.
            try:
                inventory = _ps_com_raw('plc-runtimes', min(timeout, 20),
                                        **{k: v for k, v in args.items() if k == 'preferPid'})
            except Exception:
                raise exc
            if not isinstance(inventory, dict) or inventory.get('error'):
                raise exc
            classified = diagnose_read_failure(args.get('name', ''), args.get('path', ''), exc, inventory)
            if classified is exc:
                raise
            raise classified from exc
    if command in {'login', 'logout', 'start', 'stop', 'online'}:
        from .runtime_contract import execute
        from tc_agent.ads import read_ads_state
        return execute(command, args,
                       lambda verb, **kw: _ps_com_raw(verb, timeout, **dict(
                           {k: v for k, v in args.items() if k not in {'runtime', 'all_plcs'}}, **kw)),
                       read_ads_state, timeout=min(timeout, 30))
    if command in {'state', 'restart'}:
        from tc_agent.ads import ADS_SYSTEM_SERVICE_PORT, read_ads_state, AdsStateError
        target = _ps_com_raw('target-show', timeout, **args)
        netid = str(target.get('target_netid') or '').strip()
        if not netid:
            return {'status': 'unavailable', 'verified': False, 'error': 'No target AMS NetId'}
        execution = _ps_com_raw(command, timeout, **args) if command == 'restart' else None
        from .runtime_contract import command_failed
        if command == 'restart' and command_failed(execution):
            return {'status': 'failed', 'verified': False, 'execution': execution,
                    'error': 'Restart request failed', 'configuration_loaded_verified': False}
        deadline = time.monotonic() + (min(timeout, 30.0) if command == 'restart' else 0)
        state = None
        error = ''
        while True:
            try:
                state = read_ads_state(netid, ADS_SYSTEM_SERVICE_PORT)
                error = ''
            except AdsStateError as exc:
                state = None
                error = str(exc)
            if command == 'state' or (state and state.get('state_code') == 5) or time.monotonic() >= deadline:
                break
            time.sleep(0.5)
        verified = bool(state) if command == 'state' else bool(state and state.get('state_code') == 5)
        return {'status': ('read' if command == 'state' else 'restarted') if verified else 'incomplete',
                'state': state.get('state_name', 'Unknown') if state else 'Unknown',
                'ads_state': state, 'target_netid': netid, 'port': ADS_SYSTEM_SERVICE_PORT,
                'source': 'ADS System Service', 'verified': verified, 'error': error,
                'execution': execution, 'configuration_loaded_verified': False,
                'plc_run_verified': False}
    if command == 'activate':
        result = _ps_com_raw(command, timeout, **args)
        from .runtime_contract import command_failed
        failed = command_failed(result)
        return dict(result, configuration_submitted=not failed, configuration_loaded_verified=False,
                    restart_performed=False, verified=False if failed else None)
    if command == 'hmi-dynamic-symbols-set':
        from .hmi_dynamic_symbols import normalize_dynamic_symbols
        symbols, definitions = normalize_dynamic_symbols(args.get('symbols'), args.get('definitions'))
        args = dict(args, symbols=symbols, definitions=definitions)
    if command in {'hmi-delete-view', 'hmi-user-control-delete'}:
        from .hmi_delete import delete_item
        file = str(args.get('file') or args.get('user_control') or '')
        if command == 'hmi-user-control-delete' and not file.lower().endswith('.usercontrol'):
            file += '.usercontrol'
        return delete_item(file, project=str(args.get('project') or ''), apply=bool(args.get('apply', False)))
    args = _bind_hmi_project_selector(command, args, timeout)
    if command in {'hmi-create-view', 'hmi-control-edit', 'hmi-controls-batch', 'hmi-write-markup', 'hmi-user-control-create'}:
        from .hmi_contract import guarded_call
        return guarded_call(command, args, lambda cmd, **params: _ps_com_raw(cmd, timeout, **params))
    if command == 'hmi-validate':
        from .hmi_contract import validate_project
        result = _ps_com_raw(command, timeout, **args)
        info = _ps_com_raw('hmi-project-info', timeout, **args)
        schema = validate_project(info['project_file'])
        result.update(schema)
        result['error_count'] = int(result.get('error_count', 0)) + schema['schema_error_count']
        result['valid'] = bool(result.get('valid')) and schema['schema_valid']
        result['note'] = 'Structural and installed control-schema checks only; browser behavior and live ADS values remain unverified.'
        return result
    if command == 'hmi-bindings':
        from .hmi_symbols import saved_symbol_findings
        result = _ps_com_raw(command, timeout, **args)
        info = _ps_com_raw('hmi-project-info', timeout, **args)
        python_findings = saved_symbol_findings(info['project_file'])
        merged = []
        seen = set()
        # Python performs stricter token validation. Put those findings first,
        # then retain distinct PowerShell reference findings without counting
        # the same control/attribute/expression twice.
        for item in python_findings + list(result.get('findings') or []):
            key = tuple(str(item.get(field) or '').casefold()
                        for field in ('file', 'control', 'attribute', 'expression'))
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
        errors = [item for item in merged if str(item.get('severity') or '').lower() == 'error']
        warnings = [item for item in merged if str(item.get('severity') or '').lower() == 'warning']
        result['error_count'] = len(errors)
        result['warning_count'] = len(warnings)
        result['findings'] = merged[:100]
        result['findings_truncated'] = len(merged) > 100
        result['malformed_expression_count'] = len(python_findings)
        result['valid'] = not errors
        return result
    if command == 'hmi-browser-validate':
        from .hmi_browser_evidence import compare_controls
        result = _ps_com_raw(command, timeout, **args)
        info = _ps_com_raw('hmi-project-info', timeout, project=args.get('project', ''))
        return compare_controls(result, info['project_file'])
    return _ps_com_raw(command, timeout, **args)


def _ps_com_raw(command: str, timeout: float = 120.0, **args) -> object:
    """Invoke a TcCom.ps1 dispatcher command and return its `data` payload.

    Args:
        command: dispatcher verb, e.g. "read-pou", "write-pou", "new-pou",
                 "list", "build", "connect-check".
        timeout: seconds before the PowerShell child is killed.
        **args:  passed to the .ps1 as a JSON object (ArgsJson).

    Raises:
        TcComError: the script returned ok:false (message is the PS exception).
        RuntimeError: transport failure (non-JSON output, timeout, launch error).
    """
    prefer_pid = _TOOL_TARGET_PID.get()
    if prefer_pid and "preferPid" not in args:
        args["preferPid"] = prefer_pid
        args.setdefault("sticky", True)
        args.setdefault("strictPid", True)
    _normalize_tree_path_arguments(args)

    backend = os.environ.get("TC_AGENT_COM_BACKEND", "auto").strip().lower()
    use_native = backend == "native"
    if backend == "auto" and command not in _POWERSHELL_SYSTEM_COMMANDS:
        helper = _native_helper(prefer_pid)
        if helper is not None:
            use_native = True
        else:
            try:
                from ._native_bridge import available
                use_native = bool(available()[0])
            except Exception:
                use_native = False
    if use_native:
        try:
            return _native_request("com", command, args, timeout)
        except TcComError:
            raise
        except Exception as exc:
            raise TcComError(f"Native TwinCAT COM call ({command}) failed: {exc}") from exc

    if 'paired_implementation' in args:
        raise TcComError('Paired write requires native COM; legacy fallback is not allowed')
    script = _PS1
    if not script.exists():
        from ._packaged_scripts import script_path
        script = script_path("TcCom.ps1")

    argv = [
        _powershell_exe(), "-NoProfile", "-NonInteractive",
        "-ExecutionPolicy", "Bypass", "-File", str(script),
        "-Command", command,
    ]
    args_file: Path | None = None
    if args:
        # PLC declarations/implementations can exceed Windows' command-line
        # limit. Use a short-lived UTF-8 JSON file for large payloads.
        args_json = json.dumps(args, ensure_ascii=False)
        # Windows PowerShell's native argument parsing can consume the caret
        # separator used by TwinCAT tree paths (for example TIRT^PlcTask),
        # even when the JSON itself is valid.  Route tree-path payloads via a
        # file so exact SYSTEM/PLC paths arrive at TcCom.ps1 unchanged.
        # SymbolExpression tags such as %s%...%/s% are altered by Windows
        # PowerShell's native argv binder on some hosts.  File transport keeps
        # those percent-delimited expressions byte-exact as well.
        if len(args_json) > 6000 or "^" in args_json or "%" in args_json:
            handle = tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", encoding="utf-8", delete=False
            )
            try:
                handle.write(args_json)
                args_file = Path(handle.name)
            finally:
                handle.close()
            argv += ["-ArgsFile", str(args_file)]
        else:
            argv += ["-ArgsJson", args_json]

    try:
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=timeout,
            )
        finally:
            if args_file is not None:
                args_file.unlink(missing_ok=True)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"PowerShell COM call '{command}' timed out after {timeout}s") from e

    out = (proc.stdout or "").strip()
    if not out:
        err = (proc.stderr or "").strip()
        raise RuntimeError(
            f"PowerShell COM call '{command}' produced no output "
            f"(exit {proc.returncode}). stderr: {err or '(empty)'}"
        )

    # The last non-empty line is the JSON result (earlier lines may be warnings).
    last = out.splitlines()[-1].strip()
    try:
        payload = json.loads(last)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"PowerShell COM call '{command}' returned non-JSON: {out!r}"
        ) from e

    if not payload.get("ok"):
        raise TcComError(payload.get("error", "unknown COM error"))
    return payload.get("data")


def _io_script() -> Path:
    for path in _IO_PS1_CANDIDATES:
        if path.is_file():
            return path
    from ._packaged_scripts import script_path
    return script_path("TcIoConfiguration.ps1")


def ps_io_configuration(
    command: str,
    *,
    manifest: str = "",
    configuration: dict | None = None,
    output: str = "",
    master: str = "",
    allow_existing: bool = False,
    confirm_remove: bool = False,
    allow_with_children: bool = False,
    timeout: float = 180.0,
) -> object:
    """Run the manifest-driven, offline EtherCAT configuration bridge."""
    if manifest and configuration is not None:
        raise ValueError("pass either manifest or configuration, not both")
    backend = os.environ.get("TC_AGENT_COM_BACKEND", "native").strip().lower()
    if backend != "powershell":
        return _native_request(
            "io",
            command,
            {
                "manifest": manifest,
                "configuration": configuration,
                "output": output,
                "master": master,
                "allow_existing": allow_existing,
                "confirm_remove": confirm_remove,
                "allow_with_children": allow_with_children,
                "prefer_pid": _TOOL_TARGET_PID.get(),
            },
            timeout,
        )

    temp_manifest = None
    if configuration is not None:
        handle = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", encoding="utf-8", delete=False)
        try:
            json.dump(configuration, handle, ensure_ascii=False, indent=2)
            temp_manifest = Path(handle.name)
        finally:
            handle.close()
        manifest = str(temp_manifest)

    script = _io_script()
    com_script = _PS1
    if not com_script.is_file():
        from ._packaged_scripts import script_path
        com_script = script_path("TcCom.ps1")
    argv = [
        _powershell_exe(), "-NoProfile", "-NonInteractive",
        "-ExecutionPolicy", "Bypass", "-File", str(script),
        "-Command", command,
        "-TcComPath", str(com_script),
    ]
    if manifest:
        argv += ["-Manifest", str(Path(manifest).expanduser().resolve())]
    if output:
        argv += ["-Output", str(Path(output).expanduser().resolve())]
    prefer_pid = _TOOL_TARGET_PID.get()
    if prefer_pid:
        argv += ["-PreferPid", str(prefer_pid)]
    if allow_existing:
        argv += ["-AllowExisting"]
    if confirm_remove:
        argv += ["-ConfirmRemove"]
    if allow_with_children:
        argv += ["-AllowWithChildren"]

    try:
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(
                f"I/O configuration call '{command}' timed out after {timeout}s"
            ) from e
    finally:
        if temp_manifest is not None:
            temp_manifest.unlink(missing_ok=True)

    out = (proc.stdout or "").strip()
    payload = None
    if out:
        try:
            payload = json.loads(out)
        except json.JSONDecodeError:
            # PowerShell warnings may precede the pretty-printed JSON document.
            start = out.find("{")
            if start >= 0:
                try:
                    payload = json.loads(out[start:])
                except json.JSONDecodeError:
                    pass
    if payload is not None:
        return payload

    err = (proc.stderr or "").strip()
    raise RuntimeError(
        f"I/O configuration call '{command}' failed (exit {proc.returncode}): "
        f"{err or out or '(no output)'}"
    )


# ---- Typed convenience wrappers (mirror plc.py's COM surface) ----

def com_read_pou(
    name: str,
    *,
    area: str = "all",
    method: str = "",
    member_type: str = "",
    include_member_code: bool = True,
    start_line: int = 1,
    max_lines: int = 0,
    path: str = "",
) -> dict:
    # Accept both contracts emitted by plc_find: ``path`` for the parent POU
    # and ``member_path`` for the child, as well as the common pasted form
    # ``...^POU^Action``.  The latter must be split before XAE LookupTreeItem.
    if path:
        parts = path.split("^")
        indexes = [i for i, part in enumerate(parts)
                   if part.casefold() == str(name).casefold()]
        if indexes and indexes[-1] < len(parts) - 1:
            object_index = indexes[-1]
            if not method:
                method = ".".join(parts[object_index + 1:])
            path = "^".join(parts[:object_index + 1])
    request = {
        "name": name, "area": area, "method": method,
        "include_member_code": include_member_code,
        "start_line": start_line, "max_lines": max_lines, "path": path,
    }
    if member_type:
        request["member_type"] = member_type
    return ps_com("read-pou", **request)


def _raise_if_review_blocked(review: dict) -> None:
    if review.get("approved"):
        return
    findings = review.get("blocking_findings") or []
    details = "; ".join(
        f"[{item.get('rule')}] {item.get('object')}: {item.get('message')}"
        for item in findings
    )
    raise PlcReviewError(f"PLC 写入审查未通过：{details}")


def _review_existing_write(name: str, code: str, area: str, method_name: str,
                           path: str, *, patch_old: str | None = None,
                           patch_new: str = "", style: str = "default") -> dict:
    """Read, assemble and review a post-write candidate without mutating XAE."""
    from .lint import review_write_candidate

    current = com_read_pou(name, area="all", method=method_name or "",
                           include_member_code=False, path=path)
    candidate = {
        "name": current.get("name") or name,
        "folder": "POUs",
        "declaration": current.get("declaration") or "",
        "implementation": current.get("implementation") or "",
        "methods": current.get("methods") or [],
    }
    if patch_old is None:
        candidate[area] = code
    else:
        existing = candidate.get(area) or ""
        if existing.count(patch_old) != 1:
            # Preserve the backend's exact old_text error; it is more useful
            # than reviewing a candidate that cannot be applied.
            return {"approved": True, "deferred": True}
        candidate[area] = existing.replace(patch_old, patch_new, 1)
    if method_name:
        method_decl = str(candidate.get("declaration") or "")
        header = re.match(
            rf"^\s*METHOD\s+{re.escape(method_name)}\b(?:\s*:\s*([^\s]+))?",
            method_decl,
            re.IGNORECASE,
        )
        if not header:
            return {
                "approved": False,
                "blocking_findings": [{
                    "rule": "method-declaration-header",
                    "severity": "error",
                    "object": f"{name}.{method_name}",
                    "message": (
                        f"Method declaration must start with 'METHOD {method_name}' "
                        "and include a return type when the implementation assigns the method result."
                    ),
                }],
            }
        assigns_result = bool(re.search(
            rf"\b{re.escape(method_name)}\s*:=", str(candidate.get("implementation") or ""),
            re.IGNORECASE,
        ))
        if assigns_result and not header.group(1):
            return {
                "approved": False,
                "blocking_findings": [{
                    "rule": "method-return-type",
                    "severity": "error",
                    "object": f"{name}.{method_name}",
                    "message": "Method assigns its result but its declaration has no return type.",
                }],
            }
    return review_write_candidate(candidate, changed_area=area, style=style)


def com_write_pou(name: str, code: str, area: str = "implementation",
                  method_name: str = "", path: str = "", style: str = "default") -> str:
    if area not in {"declaration", "implementation"}:
        raise ValueError("area must be 'declaration' or 'implementation'")
    _raise_if_review_blocked(_review_existing_write(
        name, code, area, method_name, path, style=style))
    return ps_com("write-pou", name=name, area=area, code=code,
                  method=method_name or "", path=path)


def com_patch_pou(name: str, old_text: str, new_text: str,
                  area: str = "implementation", method_name: str = "",
                  path: str = "", style: str = "default") -> dict:
    if area not in {"declaration", "implementation"}:
        raise ValueError("area must be 'declaration' or 'implementation'")
    if not old_text:
        raise ValueError("old_text must not be empty")
    _raise_if_review_blocked(_review_existing_write(
        name, "", area, method_name, path, patch_old=old_text,
        patch_new=new_text, style=style))
    return ps_com(
        "patch-pou", name=name, area=area, old_text=old_text,
        new_text=new_text, method=method_name or "", path=path,
    )


def com_new_pou(name: str, pou_type: str = "fb",
                declaration: str = "", implementation: str = "",
                language: str = "ST", style: str = "default",
                return_type: str = "", path: str = "") -> str:
    if declaration or implementation:
        from .lint import review_write_candidate
        _raise_if_review_blocked(review_write_candidate({
            "name": name, "folder": "POUs", "declaration": declaration,
            "implementation": implementation, "methods": [],
        }, changed_area="all", style=style))
    return ps_com("new-pou", name=name, type=pou_type,
                  declaration=declaration, implementation=implementation,
                  language=language, return_type=return_type, path=path)


def com_create_folder(name: str, parent_path: str = "") -> dict:
    """Create a PLC tree folder below an exact parent path."""
    if not str(name or "").strip() or "^" in str(name):
        raise ValueError("folder name must be a non-empty single tree segment")
    return ps_com("new-folder", name=name, parent_path=parent_path)


def com_new_member(pou: str, name: str, member_type: str = "method",
                   return_type: str = "BOOL", language: str = "ST",
                   declaration: str = "", implementation: str = "",
                   path: str = "", style: str = "default") -> dict:
    """Create a member under an existing POU or interface.

    member_type: method | property | action | transition | propget | propset.
    The parent being an interface (vs a POU) is detected at runtime and the
    matching subType is used automatically.
    """
    if member_type == "method" and declaration:
        header = re.match(
            rf"^\s*METHOD\s+{re.escape(name)}\b(?:\s*:\s*([^\s]+))?",
            declaration,
            re.IGNORECASE,
        )
        if not header:
            raise PlcReviewError(
                f"PLC 写入审查未通过：[method-declaration-header] {pou}.{name}: "
                f"声明必须以 'METHOD {name}' 开头。"
            )
        if re.search(rf"\b{re.escape(name)}\s*:=", implementation, re.IGNORECASE) \
                and not header.group(1):
            raise PlcReviewError(
                f"PLC 写入审查未通过：[method-return-type] {pou}.{name}: "
                "方法给返回值赋值，但声明缺少返回类型。"
            )
    if declaration or implementation:
        from .lint import review_write_candidate
        _raise_if_review_blocked(review_write_candidate({
            "name": f"{pou}.{name}", "folder": "POUs",
            "declaration": declaration, "implementation": implementation,
            "methods": [],
        }, changed_area="all", style=style))
    return ps_com("new-member", pou=pou, name=name, type=member_type,
                  return_type=return_type, language=language,
                  declaration=declaration, implementation=implementation,
                  path=path)


def com_delete_member(pou: str, name: str, member_type: str = "method",
                      path: str = "", dry_run: bool = False,
                      force: bool = False) -> dict:
    """Delete a method/property/action/transition or property accessor."""
    valid = {"method", "property", "action", "transition", "propget", "propset"}
    if member_type not in valid:
        raise ValueError(f"member_type must be one of {sorted(valid)}")
    return ps_com("delete-member", pou=pou, name=name, type=member_type, path=path,
                  dry_run=dry_run, force=force)


def com_rename_member(pou: str, old: str, new: str, path: str = "") -> dict:
    return ps_com("rename-member", pou=pou, old=old, new=new, path=path)


def com_list() -> list:
    return ps_com("list")


def com_diagnostics() -> dict:
    """Read the bound XAE's diagnostics without building or changing focus."""
    from .xae_build_pipe import request_diagnostics
    pid = _TOOL_TARGET_PID.get()
    if not pid:
        return {'status': 'incomplete', 'compiler_verified': False,
                'diagnostics_complete': False, 'error': 'An explicitly bound XAE PID is required.'}
    raw = request_diagnostics(pid)
    if not isinstance(raw, dict) or raw.get('ok') is False or raw.get('diagnosticsAvailable') is not True:
        pipe_result = raw
        try:
            raw = _native_request('com', 'diagnostics',
                                  {'preferPid': pid, 'strictPid': True}, 15.0)
            if isinstance(raw, dict):
                raw = {**raw, 'diagnostic_fallback': 'native-com-readonly',
                       'primary_diagnostics': pipe_result, 'xaePid': pid}
        except Exception as exc:
            raw = {'diagnostics_complete': False, 'diagnosticsAvailable': False,
                   'error': 'Read-only diagnostics services unavailable: ' + str(exc),
                   'primary_diagnostics': pipe_result}
    if not isinstance(raw, dict):
        return {'status': 'incomplete', 'compiler_verified': False, 'diagnostics_complete': False,
                'error': 'Read-only diagnostics service unavailable; do not substitute a build.'}
    errors = raw.get('errors')
    complete = (raw.get('ok') is not False and raw.get('diagnosticsAvailable') is True
                and raw.get('diagnostics_complete') is not False
                and raw.get('diagnosticsPending') is not True
                and not raw.get('truncated') and not raw.get('diagnosticsTruncated')
                and isinstance(errors, list) and all(isinstance(e, dict) for e in errors)
                and type(raw.get('errorCount')) is int and len(errors) == raw['errorCount']
                and type(raw.get('failedProjects')) is int and raw['failedProjects'] >= 0
                and not (raw['failedProjects'] > 0 and not errors))
    return {**raw, 'status': 'diagnostics_only' if complete else 'incomplete',
            'buildPerformed': False, 'compiler_verified': False,
            'diagnostics_complete': complete,
            'next_action': 'Read the exact source location before repair; existing diagnostics do not verify a new build.'}


def com_build(always_read_errors: bool = False, *, action: str = 'build') -> dict:
    """Build using the focused XAE's UI-thread service when available.

    The VSIX service is optional.  It is selected only for the XAE instance
    explicitly bound to an Agent panel, then the established COM bridge is
    retained as a backwards-compatible fallback.  Reading warning details is
    deliberately kept on the legacy path because it needs Error List access.
    """
    from .build_platform_contract import assess, preflight
    action = str(action or 'build').strip().lower()
    if action not in {'build', 'rebuild'}:
        raise ValueError("build action must be 'build' or 'rebuild'")
    platform_evidence = assess('plc', ps_com)
    blocked = None if platform_evidence['allowed'] else preflight('plc', ps_com)
    if blocked:
        return blocked
    prefer_pid = _TOOL_TARGET_PID.get()
    if prefer_pid:
        try:
            from .xae_build_pipe import request_build

            result = request_build(prefer_pid, command=action)
            if isinstance(result, dict):
                if result.get("uncertain") is True:
                    return {
                        "status": "uncertain", "verified": False,
                        "buildPerformed": None, "build_performed": None,
                        "not_executed": False,
                        "error_type": "xae_build_result_uncertain",
                        "error": result.get("error") or "XAE build result is uncertain",
                        "build_action": action,
                        "next_action": "不要再次 Build/Rebuild；先读取 plc_build_status 和 plc_diagnostics。",
                    }
                if result.get("ok") is False:
                    return {
                        "failedProjects": 1, "errorCount": 0, "errors": [],
                        "warnings": [], "errorsRead": False,
                        "errorSource": "xae-ui-thread-error",
                        "diagnosticsPending": True,
                        "message": result.get("error") or "XAE UI-thread build failed",
                    }
                result.setdefault("execution", "xae-ui-thread")
                result.setdefault("xaePid", prefer_pid)
                result.setdefault("build_action", action)
                result.setdefault("buildPerformed", True)
                result.setdefault("platform_preflight", platform_evidence)
                result.setdefault("target_compatibility_verified", platform_evidence['target_compatibility_verified'])
                result.setdefault("target_compatible", platform_evidence['target_compatible'])
                return result
        except Exception:
            # The pipe is an optimisation, never a reason to lose the mature
            # COM build path (for example while an older VSIX remains loaded).
            pass
    build_args = {"always_read_errors": always_read_errors}
    if action != 'build':
        build_args["action"] = action
    result = ps_com("build", **build_args)
    # A failed project is already a build error even when TcXaeShell has not
    # exposed its Error List yet. Do not report the misleading combination
    # failedProjects>0/errorCount=0 to CLI, MCP, or Agent callers.
    if isinstance(result, dict):
        # Windows PowerShell may serialize an array through a wrapper object
        # shaped as {value: [...], Count: N}. Flatten it before counting so a
        # 223-error build is not presented as one diagnostic.
        for key in ("errors", "warnings"):
            items = result.get(key) or []
            if (isinstance(items, list) and len(items) == 1
                    and isinstance(items[0], dict)
                    and isinstance(items[0].get("value"), list)):
                result[key] = items[0]["value"]
        if result.get("errors"):
            result["errorCount"] = len(result["errors"])
            result["errorCountSource"] = "diagnostic-list"
        failed = int(result.get("failedProjects", 0) or 0)
        reported = int(result.get("errorCount", 0) or 0)
        if failed > 0 and reported == 0 and not (result.get("errors") or []):
            result["errorCount"] = failed
            result["errorCountSource"] = "failed-project-fallback"
            result.setdefault(
                "message",
                "Build failed; detailed compiler diagnostics are still pending.",
            )
        result.setdefault("platform_preflight", platform_evidence)
        result.setdefault("target_compatibility_verified", platform_evidence['target_compatibility_verified'])
        result.setdefault("target_compatible", platform_evidence['target_compatible'])
    return result


def com_connect_check() -> dict:
    return ps_com("connect-check")


def com_list_build_platforms() -> dict:
    result = ps_com("platform-list")
    if isinstance(result, dict):
        result.setdefault("status", "ok")
    return result


def com_get_build_platform() -> dict:
    return ps_com("platform-show")


def com_select_build_platform(full: str = '', apply: bool = False,
                              acknowledge_target_platform: bool = False,
                              allow_configuration_change: bool = False) -> dict:
    """Explicit, target-bound selection; unknown architecture never auto-selects."""
    from .runtime_contract import command_failed
    info = com_list_build_platforms()
    current = com_get_build_platform()
    target = ps_com('platform-target-info')
    config = str(current.get('config') or '')
    expected = str(target.get('target_platform') or '') if target.get('target_match_verified') is True else ''
    candidates = [p['full'] for p in info.get('platforms', [])
                  if p.get('config') == config and
                  str(p.get('platform', '')).startswith(('TwinCAT RT (', 'TwinCAT OS ('))]
    report = {'status': 'preview', 'current': current, 'target': target,
              'candidates': candidates, 'applied': False, 'switch_verified': False,
              'target_match_verified': False,
              'next_action': 'Target OS/bitness is not proven by CPUType. Confirm the target-compatible full value; apply requires acknowledge_target_platform=true.'}
    if expected:
        report['candidates'] = [p['full'] for p in info.get('platforms', []) if p.get('config') == config and p.get('platform') == expected]
        report['recommended'] = report['candidates'][0] if len(report['candidates']) == 1 else None
        report['next_action'] = 'Select the exact target-reported platform, keeping current configuration.'
    if not target.get('solution') or not target.get('target_netid') or not config or command_failed(target):
        return dict(report, status='blocked', error_code='target_or_configuration_unavailable')
    if not full:
        return dict(report, status='blocked' if apply else 'preview',
                    error_code='explicit_platform_required')
    matches = [p for p in info.get('platforms', [])
               if str(p.get('full', '')).casefold() == str(full).strip().casefold()]
    if len(matches) != 1:
        raise ValueError('Use an exact full value returned by tc_platform_list')
    selected = matches[0]
    report['requested'] = selected['full']
    is_hmi = selected.get('platform') == 'TwinCAT HMI'
    if expected and not is_hmi and selected.get('platform') != expected:
        return dict(report, status='blocked', error_code='target_platform_mismatch')
    if selected.get('config') != config and not allow_configuration_change:
        return dict(report, status='blocked', error_code='configuration_change_requires_confirmation')
    # No RT/OS/bitness inference from CPUType or from an already selected platform.
    if not apply:
        return report
    if not expected and not is_hmi and not acknowledge_target_platform:
        return dict(report, status='blocked', error_code='target_platform_confirmation_required')
    before = ps_com('platform-target-info')
    active = com_get_build_platform()
    if any(before.get(k) != target.get(k) for k in ('solution', 'target_netid', 'target_platform', 'target_match_verified')) or active != current:
        return dict(report, status='blocked', error_code='context_changed')
    result = ps_com('platform-set', full=selected['full'])
    if command_failed(result):
        return dict(report, status='failed', execution=result)
    actual = com_get_build_platform()
    after = ps_com('platform-target-info')
    switched = str(actual.get('full', '')).casefold() == selected['full'].casefold()
    context_same = all(after.get(k) == target.get(k) for k in ('solution', 'target_netid', 'target_platform', 'target_match_verified'))
    plc_platform = str(selected.get('platform', '')).startswith(('TwinCAT RT (', 'TwinCAT OS ('))
    mappings = [c for c in actual.get('contexts', [])
                if str(c.get('project', '')).lower().endswith(('.tsproj', '.plcproj'))]
    mapping_ok = bool(mappings) and all(c.get('platform') == selected.get('platform') for c in mappings)
    report.update(status='switched' if switched and context_same and (mapping_ok or not plc_platform) else 'incomplete',
                  applied=True, switch_verified=switched, context_verified=context_same,
                  mapping_verified=mapping_ok if plc_platform else None, readback=actual,
                  target_match_verified=bool(expected and not is_hmi and context_same and switched),
                  verified=bool(switched and context_same and (mapping_ok if plc_platform else is_hmi)),
                  target_confirmation='target_response' if expected else 'user', build_performed=False, runtime_change_performed=False)
    return report

def com_set_build_platform(platform: str, config: str = "") -> dict:
    platform = str(platform or "").strip()
    config = str(config or "").strip() or str(com_get_build_platform().get('config') or '')
    if not config:
        raise ValueError('Current configuration unavailable; specify config explicitly')
    if not platform:
        raise ValueError("platform is required for the PowerShell COM backend")
    full = platform if "|" in platform else f"{config}|{platform}"
    return ps_com("platform-set", full=full)


def com_delete_pou(name: str, path: str = "", dry_run: bool = False,
                   force: bool = False) -> dict:
    return ps_com("delete-pou", name=name, path=path, dry_run=dry_run, force=force)


def com_rename(old: str, new: str, path: str = "") -> dict:
    return ps_com("rename", old=old, new=new, path=path)


def com_structure(limit_per_folder: int = 0, include_members: bool = True) -> dict:
    return ps_com(
        "structure", limit_per_folder=limit_per_folder,
        include_members=include_members,
    )


def com_plc_tree(max_nodes: int = 2500) -> dict:
    return ps_com("solution-tree", max_nodes=max(1, min(int(max_nodes or 2500), 5000)))


def find_pou(query: str, folder: str = "", limit: int = 30,
             include_members: bool = True) -> dict:
    return ps_com(
        "find-pou", query=query, folder=folder, limit=limit,
        include_members=include_members,
    )


def com_all_code() -> list:
    """Dump declaration + implementation + methods for every PLC object (one COM pass)."""
    return ps_com("all-code")


def com_code_inventory(*, include_code: bool = False,
                       paths: list[str] | None = None) -> dict:
    """Return recursive PLC hashes/code without sending large source via stdout."""
    selected = list(paths or [])
    if not include_code:
        return ps_com(
            "code-inventory", include_code=False, paths=selected, timeout=180.0)
    handle = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    output = Path(handle.name)
    handle.close()
    try:
        ps_com(
            "code-inventory", include_code=True, paths=selected,
            output_file=str(output), timeout=180.0,
        )
        with output.open("r", encoding="utf-8") as stream:
            return json.load(stream)
    finally:
        output.unlink(missing_ok=True)


# ---- Pure-Python conveniences on top of com_all_code() (one COM pass) ----

def list_variables(pou: str | None = None) -> list:
    """{pou, name, type, scope} for every VAR-block entry (DUT members excluded)."""
    from .plc import _parse_variables  # pure text parser, no COM
    out: list[dict] = []
    for o in com_all_code():
        if pou and o.get("name", "").lower() != pou.lower():
            continue
        if o.get("folder") == "DUTs":
            continue
        for v in _parse_variables(o.get("declaration", "") or ""):
            out.append({"pou": o.get("name", ""), **v})
    out.sort(key=lambda v: (v["pou"], v["scope"], v["name"]))
    return out


def search_code_result(
    pattern: str,
    regex: bool = False,
    ignore_case: bool = True,
    *,
    pou: str = "",
    max_results: int = 50,
    path: str = "",
) -> dict:
    """Search inside the COM process and return only matching lines."""
    return ps_com(
        "search-code", pattern=pattern, regex=regex,
        case_sensitive=not ignore_case, pou=pou,
        max_results=max_results, path=path,
    )


def search_code(pattern: str, regex: bool = False,
                ignore_case: bool = True) -> list:
    """Backward-compatible CLI wrapper returning only the match list."""
    result = search_code_result(
        pattern, regex=regex, ignore_case=ignore_case,
    )
    return list(result.get("matches") or [])


# ---- PLC project CRUD / PLCopen XML ----

def com_create_plc_project(name: str = "PLC1",
                           template: str = "Standard PLC Template") -> dict:
    return ps_com("create-plc-project", name=name, template=template)


def com_delete_plc_project(name: str) -> dict:
    return ps_com("delete-plc-project", name=name)


def com_remove_plc_project(name: str) -> dict:
    return ps_com("remove-plc-project", name=name)


def com_import_plcopen(xml_file: str, options: int = 0) -> dict:
    return ps_com("import-plcopen", file=str(xml_file), options=options)


def com_export_plcopen(xml_file: str, pou_names: list) -> dict:
    return ps_com("export-plcopen", file=str(xml_file), pous=list(pou_names))


# ---- Library management ----

def com_list_libraries() -> list:
    return ps_com("lib-list")


def com_scan_libraries() -> list:
    return ps_com("lib-scan", timeout=180.0)


def com_add_library(name: str, version: str = "*", distributor: str = "") -> dict:
    return ps_com("lib-add", name=name, version=version, distributor=distributor)


def com_remove_library(name: str, version: str = "", distributor: str = "") -> dict:
    return ps_com("lib-remove", name=name, version=version, distributor=distributor)


def com_add_placeholder(name: str, default_lib: str = "", default_version: str = "*",
                        default_distributor: str = "") -> dict:
    return ps_com("placeholder-add", name=name, default_lib=default_lib,
                  default_version=default_version,
                  default_distributor=default_distributor)


def com_freeze_placeholder(name: str) -> dict:
    return ps_com("placeholder-freeze", name=name)


def com_insert_repository(name: str, root_folder: str, index: int = 0) -> dict:
    return ps_com("repo-insert", name=name, root_folder=root_folder, index=index)


def com_remove_repository(name: str) -> dict:
    return ps_com("repo-remove", name=name)


def com_install_library(repository: str, lib_path: str, overwrite: bool = False) -> dict:
    return ps_com("lib-install", repository=repository, lib_path=lib_path,
                  overwrite=overwrite, timeout=180.0)


def com_uninstall_library(repository: str, library: str,
                          version: str = "", distributor: str = "") -> dict:
    return ps_com("lib-uninstall", repository=repository, library=library,
                  version=version, distributor=distributor)


# ---- Runtime control ----

def com_state() -> dict:
    return ps_com("state")


def com_system_structure(max_depth: int = 6, roots: list[str] | None = None) -> dict:
    return ps_com("system-structure", max_depth=max(0, min(int(max_depth or 6), 12)),
                  roots=list(roots or []))


def com_system_settings() -> dict:
    return ps_com("system-settings")


def com_system_settings_set(settings: dict, apply: bool = False) -> dict:
    return ps_com("system-settings-set", settings=dict(settings or {}), apply=bool(apply))


def com_core_info() -> dict:
    return ps_com("core-info")


def com_realtime_info() -> dict:
    return ps_com("realtime-info")


def com_realtime_validate() -> dict:
    # Keep XAE access on the PowerShell backend, then run the same pure
    # validator used by the native bridge and unit tests.
    from .tc_platform import validate_realtime_snapshot
    snapshot = com_realtime_info()
    result = validate_realtime_snapshot(snapshot)
    result["snapshot"] = snapshot
    return result


def com_core_assign(cpu_ids: list[int], max_cpus: int | None = None,
                    affinity: int | None = None,
                    p_core_affinity: int | None = None,
                    e_core_affinity: int | None = None,
                    apply: bool = False) -> dict:
    return ps_com("core-assign", cpu_ids=list(cpu_ids or []), max_cpus=max_cpus,
                  affinity=affinity,
                  p_core_affinity=p_core_affinity, e_core_affinity=e_core_affinity,
                  apply=bool(apply))


def com_task_info(max_depth: int = 6) -> dict:
    return ps_com("task-info", max_depth=max(0, min(int(max_depth or 6), 12)))


def com_task_core_assign(task_path: str, cpu_id: int, apply: bool = False) -> dict:
    return ps_com("task-core-assign", task_path=task_path, cpu_id=int(cpu_id),
                  apply=bool(apply))


def com_task_settings_set(task_path: str, settings: dict,
                          apply: bool = False) -> dict:
    return ps_com("task-settings-set", task_path=task_path,
                  settings=dict(settings or {}), apply=bool(apply))


def com_system_add(parent_path: str, name: str, item_type: int, info: str = "",
                   apply: bool = False) -> dict:
    return ps_com("system-add", parent_path=parent_path, name=name,
                  item_type=int(item_type), info=info, apply=bool(apply))


def com_system_remove(path: str, apply: bool = False,
                      allow_with_children: bool = False) -> dict:
    return ps_com("system-remove", path=path, apply=bool(apply),
                  allow_with_children=bool(allow_with_children))


def com_realtime_refresh() -> dict:
    """Refresh the currently open XAE SYSTEM > Real-Time designer."""
    return ps_com("realtime-refresh")


# ---- TwinCAT HMI project management ----

def com_hmi_create_project(name: str, output_directory: str = "", template: str = "",
                           apply: bool = False) -> dict:
    return ps_com("hmi-create-project", name=str(name or ""),
                  output_directory=str(output_directory or ""), template=str(template or ""),
                  apply=bool(apply))

def com_hmi_project_info(project: str = "") -> dict:
    return ps_com("hmi-project-info", project=str(project or ""))


def com_hmi_structure(project: str = "") -> dict:
    return ps_com("hmi-structure", project=str(project or ""))


def com_hmi_read(file: str, project: str = "", max_chars: int = 200000,
                 control_id: str = "", include_content: bool = True,
                 max_controls: int = 200, control_offset: int = 0, content_offset: int = 0) -> dict:
    return ps_com(
        "hmi-read", project=str(project or ""), file=str(file or ""),
        max_chars=max(1, min(int(max_chars or 200000), 1000000)),
        control_id=str(control_id or ""), include_content=bool(include_content),
        max_controls=max(1, min(int(max_controls or 200), 5000)),
        control_offset=max(0, int(control_offset)), content_offset=max(0, int(content_offset)),
    )


def com_hmi_write_markup(file: str, markup: str, project: str = "", apply: bool = False) -> dict:
    return ps_com("hmi-write-markup", project=str(project or ""), file=str(file or ""),
                  markup=str(markup or ""), apply=bool(apply))


def com_hmi_ads_info(project: str = "") -> dict:
    return ps_com("hmi-ads-info", project=str(project or ""))


def com_hmi_validate(project: str = "") -> dict:
    return ps_com("hmi-validate", project=str(project or ""))


def com_hmi_create_view(name: str, project: str = "", kind: str = "view",
                        controls: list[dict] | None = None,
                        apply: bool = False) -> dict:
    if kind not in {"view", "content"}:
        raise ValueError("kind must be view or content")
    return ps_com("hmi-create-view", project=str(project or ""), name=str(name or ""),
                  kind=kind, controls=list(controls or []), apply=bool(apply))


def com_hmi_project_api(operation: str, project: str = "",
                        arguments: dict | None = None, apply: bool = False) -> dict:
    if not str(operation or "").strip():
        raise ValueError("operation is required")
    if arguments is not None and not isinstance(arguments, dict):
        raise ValueError("arguments must be a JSON object")
    return ps_com("hmi-project-api", project=str(project or ""),
                  operation=str(operation), arguments=dict(arguments or {}),
                  apply=bool(apply))


def com_hmi_startup_view_set(view: str, project: str = "") -> dict:
    if not str(view or "").strip():
        raise ValueError("view must be a project-relative .view path")
    return ps_com("hmi-startup-view-set", project=str(project or ""), view=str(view))


def com_hmi_control_edit(file: str, action: str, control_id: str, project: str = "",
                         control_type: str = "", parent_id: str = "",
                         attributes: dict | None = None, apply: bool = False,
                         _event_placement: str = "") -> dict:
    if action not in {"add", "update", "remove"}:
        raise ValueError("action must be add, update or remove")
    arguments = dict(project=str(project or ""), file=str(file or ""),
                     action=action, control_id=str(control_id or ""), type=str(control_type or ""),
                     parent_id=str(parent_id or ""), attributes=dict(attributes or {}),
                     apply=bool(apply))
    if _event_placement:
        arguments['_event_placement'] = str(_event_placement)
    return ps_com("hmi-control-edit", **arguments)


def com_hmi_delete_view(file: str, project: str = "", apply: bool = False) -> dict:
    return ps_com("hmi-delete-view", project=str(project or ""), file=str(file or ""),
                  apply=bool(apply))


def com_hmi_ads_runtime_set(name: str, project: str = "", scope: str = "both",
                            action: str = "upsert", settings: dict | None = None,
                            apply: bool = False) -> dict:
    if scope not in {"default", "remote", "both"}:
        raise ValueError("scope must be default, remote or both")
    if action not in {"upsert", "remove"}:
        raise ValueError("action must be upsert or remove")
    return ps_com("hmi-ads-runtime-set", project=str(project or ""), name=str(name or ""),
                  scope=scope, action=action, settings=dict(settings or {}), apply=bool(apply))


def com_hmi_ads_symbols(project: str = "", runtime: str = "", scope: str = "") -> dict:
    if scope not in {"", "default", "remote"}:
        raise ValueError("scope must be empty, default or remote")
    return ps_com("hmi-ads-symbols", project=str(project or ""), runtime=str(runtime or ""), scope=scope)


def com_hmi_ads_symbol_set(runtime: str, name: str, project: str = "", scope: str = "both",
                           action: str = "upsert", index_group: int = 0,
                           index_offset: int = 0, type_name: str = "",
                           apply: bool = False) -> dict:
    if scope not in {"default", "remote", "both"}:
        raise ValueError("scope must be default, remote or both")
    if action not in {"upsert", "remove"}:
        raise ValueError("action must be upsert or remove")
    return ps_com("hmi-ads-symbol-set", project=str(project or ""), runtime=str(runtime or ""),
                  name=str(name or ""), scope=scope, action=action,
                  index_group=max(0, min(int(index_group), 0xFFFFFFFF)),
                  index_offset=max(0, min(int(index_offset), 0xFFFFFFFF)),
                  type_name=str(type_name or ""), apply=bool(apply))


def com_hmi_dynamic_symbols_set(symbols: dict, definitions: dict | None = None,
                                project: str = "", apply: bool = False) -> dict:
    return ps_com("hmi-dynamic-symbols-set", project=str(project or ""), symbols=dict(symbols or {}),
                  definitions=dict(definitions or {}), apply=bool(apply))


def com_hmi_bind_plc(project: str = "", plc: str = "", runtime_name: str = "",
                     symbol_roots: list[str] | None = None,
                     read_only_symbols: list[str] | None = None,
                     scope: str = "default", apply: bool = False) -> dict:
    from .hmi_binding import bind_plc
    return bind_plc(
        project=project, plc=plc, runtime_name=runtime_name,
        symbol_roots=list(symbol_roots or []),
        read_only_symbols=list(read_only_symbols or []),
        scope=scope, apply=bool(apply),
    )


def com_hmi_bindings(project: str = "") -> dict:
    return ps_com("hmi-bindings", project=str(project or ""))


def com_hmi_internal_symbols(project: str = "") -> dict:
    return ps_com("hmi-internal-symbols", project=str(project or ""))


def com_hmi_internal_symbol_set(name: str, project: str = "", action: str = "upsert",
                                 settings: dict | None = None, apply: bool = False) -> dict:
    if action not in {"upsert", "remove"}:
        raise ValueError("action must be upsert or remove")
    return ps_com("hmi-internal-symbol-set", project=str(project or ""), name=str(name or ""),
                  action=action, settings=dict(settings or {}), apply=bool(apply))


def com_hmi_localizations(project: str = "") -> dict:
    return ps_com("hmi-localizations", project=str(project or ""))


def com_hmi_localization_set(key: str, project: str = "", action: str = "upsert",
                             values: dict | None = None, apply: bool = False) -> dict:
    if action not in {"upsert", "remove"}:
        raise ValueError("action must be upsert or remove")
    return ps_com("hmi-localization-set", project=str(project or ""), key=str(key or ""),
                  action=action, values=dict(values or {}), apply=bool(apply))


def com_hmi_themes(project: str = "") -> dict:
    return ps_com("hmi-themes", project=str(project or ""))


def com_hmi_themed_resource_set(name: str, project: str = "", action: str = "upsert",
                                settings: dict | None = None, apply: bool = False) -> dict:
    if action not in {"upsert", "remove"}:
        raise ValueError("action must be upsert or remove")
    return ps_com("hmi-themed-resource-set", project=str(project or ""), name=str(name or ""),
                  action=action, settings=dict(settings or {}), apply=bool(apply))


def com_hmi_active_theme_set(theme: str, project: str = "", apply: bool = False) -> dict:
    return ps_com("hmi-active-theme-set", project=str(project or ""), theme=str(theme or ""),
                  apply=bool(apply))


def com_hmi_user_controls(project: str = "") -> dict:
    return ps_com("hmi-user-controls", project=str(project or ""))


def com_hmi_user_control_create(name: str, project: str = "",
                                parameters: list[dict] | None = None,
                                controls: list[dict] | None = None,
                                apply: bool = False) -> dict:
    return ps_com("hmi-user-control-create", project=str(project or ""), name=str(name or ""),
                  parameters=list(parameters or []), controls=list(controls or []), apply=bool(apply))


def com_hmi_user_control_parameter_set(user_control: str, name: str, project: str = "",
                                       action: str = "upsert", settings: dict | None = None,
                                       apply: bool = False) -> dict:
    if action not in {"upsert", "remove"}:
        raise ValueError("action must be upsert or remove")
    return ps_com("hmi-user-control-parameter-set", project=str(project or ""),
                  user_control=str(user_control or ""), name=str(name or ""), action=action,
                  settings=dict(settings or {}), apply=bool(apply))


def com_hmi_user_control_delete(user_control: str, project: str = "", apply: bool = False) -> dict:
    return ps_com("hmi-user-control-delete", project=str(project or ""),
                  user_control=str(user_control or ""), apply=bool(apply))


def com_hmi_framework_templates(project: str = "") -> dict:
    return ps_com("hmi-framework-templates", project=str(project or ""))


def com_hmi_framework_validate(source: str) -> dict:
    return ps_com("hmi-framework-validate", source=str(source or ""))


def com_hmi_framework_control_info(source: str, control: str = "") -> dict:
    return ps_com("hmi-framework-control-info", source=str(source or ""),
                  control=str(control or ""))


def com_hmi_framework_attribute_set(source: str, name: str, control: str = "",
                                    action: str = "upsert", settings: dict | None = None,
                                    apply: bool = False) -> dict:
    if action not in {"upsert", "remove"}:
        raise ValueError("action must be upsert or remove")
    return ps_com(
        "hmi-framework-attribute-set", source=str(source or ""),
        control=str(control or ""), name=str(name or ""), action=action,
        settings=dict(settings or {}), apply=bool(apply),
    )


def com_hmi_framework_event_set(source: str, name: str, control: str = "",
                                action: str = "upsert", settings: dict | None = None,
                                apply: bool = False) -> dict:
    if action not in {"upsert", "remove"}:
        raise ValueError("action must be upsert or remove")
    return ps_com(
        "hmi-framework-event-set", source=str(source or ""),
        control=str(control or ""), name=str(name or ""), action=action,
        settings=dict(settings or {}), apply=bool(apply),
    )


def com_hmi_framework_create(name: str, output_directory: str, project: str = "",
                             language: str = "typescript", description: str = "",
                             apply: bool = False) -> dict:
    if language not in {"typescript", "javascript"}:
        raise ValueError("language must be typescript or javascript")
    return ps_com(
        "hmi-framework-create", project=str(project or ""), name=str(name or ""),
        output_directory=str(output_directory or ""), language=language,
        description=str(description or ""), apply=bool(apply), timeout=120.0,
    )


def com_hmi_framework_pack(source: str, output_directory: str = "",
                           version: str = "", apply: bool = False) -> dict:
    return ps_com(
        "hmi-framework-pack", source=str(source or ""),
        output_directory=str(output_directory or ""), version=str(version or ""),
        apply=bool(apply), timeout=120.0,
    )


def com_hmi_framework_packages(project: str = "", package_id: str = "") -> dict:
    return ps_com("hmi-framework-packages", project=str(project or ""), package_id=str(package_id or ""))


def com_hmi_framework_package_inspect(package: str) -> dict:
    return ps_com("hmi-framework-package-inspect", package=str(package or ""))


def com_hmi_framework_install(package: str, project: str = "", apply: bool = False,
                              acknowledge_package_change: bool = False) -> dict:
    return ps_com(
        "hmi-framework-install", package=str(package or ""), project=str(project or ""),
        apply=bool(apply), acknowledge_package_change=bool(acknowledge_package_change),
        timeout=180.0,
    )


def com_hmi_framework_uninstall(package_id: str, project: str = "", force: bool = False,
                                apply: bool = False,
                                acknowledge_package_change: bool = False) -> dict:
    return ps_com(
        "hmi-framework-uninstall", package_id=str(package_id or ""),
        project=str(project or ""), force=bool(force), apply=bool(apply),
        acknowledge_package_change=bool(acknowledge_package_change), timeout=180.0,
    )


def com_hmi_runtime_info(project: str = "") -> dict:
    return ps_com("hmi-runtime-info", project=str(project or ""))


def com_hmi_server_control(action: str, project: str = "", apply: bool = False) -> dict:
    if action not in {"start", "stop", "restart"}:
        raise ValueError("action must be start, stop or restart")
    return ps_com("hmi-server-control", project=str(project or ""), action=action,
                  apply=bool(apply), timeout=60.0)


def com_hmi_ads_live_check(project: str = "", runtime: str = "", plc: str = "",
                           symbols: list[str] | None = None, max_depth: int = 3,
                           max_symbols: int = 32) -> dict:
    from .hmi_live import check_hmi_ads_online
    return check_hmi_ads_online(
        project=project, runtime=runtime, plc=plc, symbols=list(symbols or []),
        max_depth=max_depth, max_symbols=max_symbols,
    )


def com_hmi_binding_diagnose(project: str = "", runtime: str = "", plc: str = "",
                             max_symbols: int = 32) -> dict:
    from .hmi_live import diagnose_hmi_bindings
    return diagnose_hmi_bindings(
        project=project, runtime=runtime, plc=plc,
        max_symbols=max_symbols,
    )


def com_hmi_browser_validate(project: str = "", widths: list[int] | None = None,
                              height: int = 720, settle_ms: int = 5000,
                              entry_page: str = "") -> dict:
    viewport_widths = [int(value) for value in (widths or [1280])]
    if entry_page:
        from .hmi_browser_target import validate_entry_page
        return validate_entry_page(project, entry_page, viewport_widths, int(height), int(settle_ms))
    result = ps_com(
        "hmi-browser-validate", project=str(project or ""), widths=viewport_widths,
        height=int(height), settle_ms=int(settle_ms), timeout=180.0,
    )
    if result.get('status') == 'failed':
        from .hmi_live import explain_browser_binding_mismatch
        try:
            result = explain_browser_binding_mismatch(result, com_hmi_bindings(project))
        except (TcComError, ValueError, OSError) as exc:
            result['binding_comparison_error'] = str(exc)
    return result


def com_hmi_build(project: str = "") -> dict:
    from .hmi_diagnostics import enrich_diagnostics
    result = ps_com("hmi-build", project=str(project or ""), timeout=180.0)
    if result.get('build_performed') is False:
        return result
    return enrich_diagnostics(result, check_page=False)


# ---- TwinSAFE project management ----

def com_safety_structure(max_depth: int = 8) -> dict:
    return ps_com("safety-structure", max_depth=max(0, min(int(max_depth or 8), 12)))


def com_safety_project_info(project: str = "") -> dict:
    return ps_com("safety-project-info", project=str(project or ""))


def com_safety_files(project: str) -> dict:
    return ps_com("safety-files", project=project)


def com_safety_target_info(project: str) -> dict:
    return ps_com("safety-target-info", project=project)


def com_safety_aliases(project: str, group: str = "") -> dict:
    return ps_com("safety-aliases", project=project, group=group)


def com_safety_application(project: str, group: str = "") -> dict:
    return ps_com("safety-application", project=project, group=group)


def com_safety_logic_check(project: str, group: str = "") -> dict:
    return ps_com("safety-logic-check", project=project, group=group)


def com_safety_validate(source: str = "") -> dict:
    return ps_com("safety-validate", source=str(source or ""))


def com_safety_import(source: str, name: str = "", mode: str = "copy",
                      apply: bool = False, confirm_source_move: bool = False,
                      acknowledge_safety_review: bool = False) -> dict:
    return ps_com("safety-import", source=source, name=name, mode=mode,
                  apply=bool(apply), confirm_source_move=bool(confirm_source_move),
                  acknowledge_safety_review=bool(acknowledge_safety_review))


def com_safety_create(name: str, target: str = "hardware",
                      template: str = "preconfigured-inputs",
                      author: str = "TwinCAT Agent", internal_project_name: str = "",
                      apply: bool = False,
                      acknowledge_safety_review: bool = False) -> dict:
    return ps_com("safety-create", name=name, target=target, template=template, author=author,
                  internal_project_name=internal_project_name, apply=bool(apply),
                  acknowledge_safety_review=bool(acknowledge_safety_review))


def com_safety_export(project: str, output_file: str, overwrite: bool = False,
                      apply: bool = False,
                      acknowledge_safety_review: bool = False) -> dict:
    return ps_com("safety-export", project=project, output_file=output_file,
                  overwrite=bool(overwrite), apply=bool(apply),
                  acknowledge_safety_review=bool(acknowledge_safety_review))


def com_safety_remove(project: str, apply: bool = False,
                      confirm_project_name: str = "",
                      acknowledge_safety_review: bool = False) -> dict:
    return ps_com("safety-remove", project=project, apply=bool(apply),
                  confirm_project_name=confirm_project_name,
                  acknowledge_safety_review=bool(acknowledge_safety_review))


def com_safety_delete(project: str, backup_file: str = "", apply: bool = False,
                      confirm_project_name: str = "", confirm_delete_files: bool = False,
                      acknowledge_safety_review: bool = False) -> dict:
    return ps_com("safety-delete", project=project, backup_file=backup_file,
                  apply=bool(apply), confirm_project_name=confirm_project_name,
                  confirm_delete_files=bool(confirm_delete_files),
                  acknowledge_safety_review=bool(acknowledge_safety_review))


def com_activate() -> dict:
    return ps_com("activate")


def com_restart() -> dict:
    return ps_com("restart", timeout=60.0)


def com_login(runtime='', all_plcs=False) -> dict:
    return ps_com("login", runtime=runtime, all_plcs=all_plcs)


def com_logout(runtime='', all_plcs=False) -> dict:
    return ps_com("logout", runtime=runtime, all_plcs=all_plcs)


def com_start(runtime='', all_plcs=False) -> dict:
    return ps_com("start", runtime=runtime, all_plcs=all_plcs)


def com_stop(runtime='', all_plcs=False) -> dict:
    return ps_com("stop", runtime=runtime, all_plcs=all_plcs)


def com_online(runtime='', all_plcs=False) -> dict:
    return ps_com("online", timeout=60.0, runtime=runtime, all_plcs=all_plcs)


def com_config_mode() -> dict:
    return ps_com("config-mode")


def com_run_mode() -> dict:
    return ps_com("run-mode")


if __name__ == "__main__":  # quick manual smoke test
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "connect-check"
    try:
        print(json.dumps(ps_com(cmd), ensure_ascii=False, indent=2))
    except TcComError as e:
        print(f"[COM ERROR] {e}")
    except RuntimeError as e:
        print(f"[BRIDGE ERROR] {e}")
