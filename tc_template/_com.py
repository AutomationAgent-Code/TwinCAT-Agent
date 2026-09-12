"""COM dispatch hardening for Python 3.14 + TwinCAT.

pywin32's gen_py *early-binding* wrappers (e.g. ``ITcSysManager17``) crash
with an access violation (0xC0000005) when calling TwinCAT Automation
Interface methods such as ``LookupTreeItem`` under Python 3.14.  The fix is
to force *late* (dynamic) binding everywhere:

  * disable gencache so pywin32 never generates early-bound modules, and
  * wipe any early-bound modules already generated for this interpreter.

With dynamic dispatch ``.Object`` returns a plain ``CDispatch`` and all
TwinCAT calls resolve property/method names at runtime via
``IDispatch::GetIDsOfNames`` — no fragile vtable involved.

Call :func:`force_dynamic_dispatch` once before the first COM call.
"""

from __future__ import annotations

import contextlib
import ctypes
import re
import shutil
import time
from contextvars import ContextVar

_hardened = False
_target_pid: ContextVar[int] = ContextVar("tc_native_target_pid", default=0)
_COM_BUSY_HRESULTS = {0x80010001, 0x8001010A, 0x8001010B}


def com_hresult(exc: BaseException) -> int | None:
    value = getattr(exc, "hresult", None)
    if value is None and getattr(exc, "args", None):
        value = exc.args[0]
    try:
        return int(value) & 0xFFFFFFFF
    except (TypeError, ValueError):
        return None


def is_com_busy_error(exc: BaseException) -> bool:
    """Whether XAE temporarily rejected an Automation Interface call."""
    return com_hresult(exc) in _COM_BUSY_HRESULTS


def retry_com_busy(
    call,
    *,
    attempts: int = 8,
    initial_delay: float = 0.12,
    max_delay: float = 1.0,
):
    """Retry transient RPC_E_CALL_REJECTED/RETRYLATER failures.

    XAE rejects COM calls while its UI thread is parsing, saving or compiling.
    A bounded exponential backoff mirrors COM IMessageFilter retry behavior but
    works in the small embedded pywin32 runtime without another dependency.
    """
    delay = max(0.01, float(initial_delay))
    total = max(1, int(attempts))
    last: BaseException | None = None
    for attempt in range(total):
        try:
            return call()
        except Exception as exc:
            if not is_com_busy_error(exc):
                raise
            last = exc
            if attempt + 1 >= total:
                break
            time.sleep(delay)
            delay = min(float(max_delay), delay * 1.7)
    raise RuntimeError(
        f"XAE remained busy and rejected the COM call after {total} attempts "
        "(RPC_E_CALL_REJECTED). Wait for save/build/UI refresh to finish and retry."
    ) from last


def force_dynamic_dispatch() -> None:
    """Force pywin32 into pure dynamic (late-bound) COM dispatch.

    Idempotent — safe to call before every COM entry point.
    """
    global _hardened
    if _hardened:
        return
    try:
        import win32com
        from win32com.client import gencache

        # Never auto-generate early-bound wrappers (they segfault on 3.14).
        gencache.is_readonly = True
        # Drop any early-bound modules already cached for this interpreter.
        shutil.rmtree(win32com.__gen_path__, ignore_errors=True)
    except Exception:
        # win32com not installed / nothing to harden — COM calls will raise
        # their own clear errors later.
        pass
    finally:
        _hardened = True


@contextlib.contextmanager
def com_apartment():
    """Initialize COM for the current worker thread.

    The backend executes tools through ``asyncio.to_thread``.  Unlike the old
    PowerShell subprocess, Python worker threads do not initialize COM by
    themselves, so every native bridge call must own an STA apartment.
    """
    import pythoncom

    pythoncom.CoInitialize()
    try:
        yield
    finally:
        pythoncom.CoUninitialize()


@contextlib.contextmanager
def target_process(pid: int = 0):
    token = _target_pid.set(max(0, int(pid or 0)))
    try:
        yield
    finally:
        _target_pid.reset(token)


def _window_pid(hwnd: int) -> int:
    if not hwnd:
        return 0
    process_id = ctypes.c_ulong()
    ctypes.windll.user32.GetWindowThreadProcessId(
        ctypes.c_void_p(int(hwnd)), ctypes.byref(process_id)
    )
    return int(process_id.value)


def _dte_hwnd(dte) -> int:
    """Read the optional EnvDTE MainWindow handle without rejecting a DTE.

    Some TcXaeShell versions expose a deliberately small Automation
    Interface dispatch object.  It is valid for Solution/SystemManager calls
    but does not publish ``MainWindow``; probing that member must not make us
    discard an otherwise usable XAE instance.
    """
    try:
        window = getattr(dte, "MainWindow")
        return int(getattr(window, "HWnd", 0) or 0)
    except Exception:
        return 0


def _dynamic_dispatch(obj):
    force_dynamic_dispatch()
    import pythoncom
    from win32com.client import dynamic

    # IRunningObjectTable.GetObject returns PyIUnknown.  .NET performs this
    # COM interface conversion implicitly, while pywin32's dynamic.Dispatch
    # expects IDispatch and otherwise raises ``GetTypeInfo`` AttributeError.
    if not hasattr(obj, "GetTypeInfo") and hasattr(obj, "QueryInterface"):
        obj = obj.QueryInterface(pythoncom.IID_IDispatch)
    # dynamic.Dispatch swallows GetTypeInfo's RPC_E_CALL_REJECTED and creates an
    # untyped wrapper; subsequent .Solution becomes a misleading AttributeError.
    # Preserve busy HRESULTs so the existing bounded COM retry can handle them.
    try:
        obj.GetTypeInfo()
    except Exception as exc:
        if is_com_busy_error(exc):
            raise
    return dynamic.Dispatch(obj)


def running_dtes() -> list[dict]:
    """Enumerate every running TwinCAT/Visual Studio DTE via the ROT.

    Pure pywin32 implementation of the former ``TcRot`` C# helper.  It works
    without powershell.exe/Add-Type and keeps multi-XAE PID selection.
    Returned COM objects are only valid inside the caller's COM apartment.
    """
    force_dynamic_dispatch()
    import pythoncom

    rot = pythoncom.GetRunningObjectTable()
    bind = pythoncom.CreateBindCtx(0)
    enum = rot.EnumRunning()
    result: list[dict] = []
    while True:
        monikers = enum.Next(1)
        if not monikers:
            break
        moniker = monikers[0]
        try:
            display_name = str(moniker.GetDisplayName(bind, None) or "")
        except Exception as exc:
            if is_com_busy_error(exc):
                raise
            continue
        if not re.search(r"(?i)(TcXaeShell|VisualStudio)\.DTE", display_name):
            continue
        try:
            dte = _dynamic_dispatch(rot.GetObject(moniker))
            dte_name = str(getattr(dte, "Name", "") or "")
            solution = str(getattr(getattr(dte, "Solution", None), "FullName", "") or "")
            hwnd = _dte_hwnd(dte)
        except Exception as exc:
            if is_com_busy_error(exc):
                raise
            continue
        match = re.search(r":(\d+)\s*$", display_name)
        pid = int(match.group(1)) if match else _window_pid(hwnd)
        result.append({
            "moniker": display_name,
            "pid": pid,
            "solution": solution,
            "name": dte_name,
            "dte": dte,
        })
    return result


def get_active_dte(*, prefer_pid: int | None = None, strict_pid: bool = False):
    """Attach to the requested running XAE without launching a new instance."""
    force_dynamic_dispatch()
    wanted = max(0, int(prefer_pid if prefer_pid is not None else _target_pid.get()))
    candidates = running_dtes()
    if wanted:
        for candidate in candidates:
            if candidate["pid"] == wanted:
                return candidate["dte"]
        if strict_pid:
            raise RuntimeError(
                f"XAE PID {wanted} is running, but its DTE is not visible in the native ROT session. "
                "Ensure TwinCAT Agent and XAE run at the same Windows privilege level."
            )
    for candidate in candidates:
        if candidate["solution"]:
            return candidate["dte"]
    if candidates:
        return candidates[0]["dte"]

    # Single-instance fallback for shells with a non-standard ROT moniker.
    import win32com.client

    for prog_id in (
        "TcXaeShell.DTE.17.0", "TcXaeShell.DTE.15.0", "TcXaeShell.DTE.14.0",
        "VisualStudio.DTE.17.0", "VisualStudio.DTE.15.0",
    ):
        try:
            dte = _dynamic_dispatch(win32com.client.GetActiveObject(prog_id))
            hwnd = _dte_hwnd(dte)
            actual_pid = _window_pid(hwnd)
            if strict_pid and wanted and actual_pid and actual_pid != wanted:
                continue
            return dte
        except Exception:
            continue
    raise RuntimeError("No running TwinCAT/VS DTE found. Open XAE first.")
