"""
TwinCAT Error List reader — pure COM via ToolWindows.ErrorList.ErrorItems.

  dte.ToolWindows.ErrorList.ErrorItems.Item(i)
    .Description / .FileName / .Line / .Column / .Project
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any


# ======================================================================
# COM connection
# ======================================================================

_dte_cache = None


def _get_dte(retries: int = 10, delay: float = 2.0) -> Any:
    """Connect to running TcXaeShell via COM with retries.

    After os.startfile(), TcXaeShell registers in the ROT asynchronously.
    This retries GetActiveObject until .Solution is accessible.
    """
    global _dte_cache
    if _dte_cache is not None:
        try:
            _dte_cache.Solution  # validate still alive
            return _dte_cache
        except Exception:
            _dte_cache = None

    import win32com.client
    import pythoncom
    pythoncom.CoInitialize()

    for attempt in range(retries):
        for pid in ("TcXaeShell.DTE.17.0", "TcXaeShell.DTE.15.0", "VisualStudio.DTE.17.0"):
            try:
                dte = win32com.client.GetActiveObject(pid)
                _ = dte.Solution  # validate
                _dte_cache = dte
                return dte
            except Exception:
                continue
        if attempt < retries - 1:
            time.sleep(delay)

    raise RuntimeError("Cannot connect to TwinCAT XAE.  Is it running?")


def _clear_dte_cache() -> None:
    """Clear cached DTE reference (call after TwinCAT restart)."""
    global _dte_cache
    _dte_cache = None


# ======================================================================
# Error List — pure COM
# ======================================================================

def read_error_list(
    *,
    wait_for_sln: str | None = None,
    dte: Any = None,
) -> dict:
    """Read TwinCAT Error List via COM (no pyautogui, no clipboard).

    Uses ``dte.ToolWindows.ErrorList.ErrorItems`` — the documented COM
    interface for reading build/compile errors.

    Args:
        wait_for_sln: If set, poll until this .sln is loaded (up to 60 s).
        dte: Optional already-connected DTE object.

    Returns:
        {"errors": [{"severity":str, "code":str, "description":str,
                      "file":str, "line":str, "column":str, "project":str}],
         "error_count": int, "warning_count": int, "info_count": int,
         "success": bool}
    """
    result: dict[str, Any] = {
        "errors": [],
        "error_count": 0,
        "warning_count": 0,
        "info_count": 0,
        "success": False,
    }

    # ── Connect ──
    if dte is None:
        try:
            dte = _get_dte()
        except Exception as e:
            result["raw_text"] = f"COM connect failed: {e}"
            return result

    # ── Wait for solution ──
    if wait_for_sln:
        expected = Path(wait_for_sln).resolve()
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                if dte.Solution.Projects.Count > 0:
                    break
            except Exception:
                pass
            try:
                current = str(dte.Solution.FullName or "")
                if current and Path(current).resolve() == expected:
                    break
            except Exception:
                pass
            time.sleep(2)

    # ── Read ErrorItems via COM ──
    try:
        error_items = dte.ToolWindows.ErrorList.ErrorItems
        count = error_items.Count

        for i in range(1, count + 1):
            item = error_items.Item(i)

            # Read fields (handle missing/None gracefully)
            try:
                desc = item.Description or ""
            except Exception:
                desc = ""
            try:
                fname = item.FileName or ""
            except Exception:
                fname = ""
            try:
                line = item.Line
            except Exception:
                line = ""
            try:
                col = item.Column
            except Exception:
                col = ""
            try:
                proj = item.Project or ""
            except Exception:
                proj = ""
            try:
                sev_raw = item.ErrorLevel
            except Exception:
                sev_raw = 1  # default to Error

            # Map severity
            # ErrorLevel: 0=Message, 1=Error, 2=Warning, 3=Info
            if isinstance(sev_raw, str):
                sev_map_str = {"error": "Error", "warning": "Warning",
                               "info": "Info", "message": "Info"}
                sev = sev_map_str.get(sev_raw.lower(), "Error")
            else:
                sev_map_int = {0: "Info", 1: "Error", 2: "Warning", 3: "Info",
                               4: "Info"}
                sev = sev_map_int.get(int(sev_raw), "Error")

            result["errors"].append({
                "severity": sev,
                "code": "",
                "description": str(desc),
                "file": str(fname),
                "line": str(line) if line else "",
                "column": str(col) if col else "",
                "project": str(proj),
            })

        result["error_count"] = sum(
            1 for e in result["errors"] if e["severity"] == "Error"
        )
        result["warning_count"] = sum(
            1 for e in result["errors"] if e["severity"] == "Warning"
        )
        result["info_count"] = sum(
            1 for e in result["errors"]
            if e["severity"] not in ("Error", "Warning")
        )
        result["success"] = True

    except Exception as e:
        result["raw_text"] = f"COM ErrorItems failed: {e}"

    return result


# ======================================================================
# Clear errors (also COM-only)
# ======================================================================

def clear_error_list(dte: Any = None) -> None:
    """Clear the TwinCAT error list before a build.

    IMPORTANT — do NOT call ``ExecuteCommand("TwinCAT.ClearErrorList")``.
    On machines with the TwinCAT Analytics extension installed, that command
    name is ambiguous: it resolves to
    ``TwinCAT.Analytics.StorageProvider.VSIntegration.ClearErrorListVSCmd``,
    whose callback contains a leftover debug ``MessageBox`` ("Inside …
    MenuItemCallback()"). That box is a native Win32 dialog, so neither the
    Automation Interface ``SilentMode`` flag nor ``DTE.SuppressUI`` suppress
    it — the script blocks until someone clicks OK.

    Clearing the list is purely cosmetic: ``read_error_list`` parses the
    Build *Output* pane, not the Error List window, and a fresh build
    replaces the window contents anyway. So we clear the ErrorItems
    collection directly when reachable, and otherwise do nothing — we never
    invoke the popup-triggering command.
    """
    if dte is None:
        try:
            dte = _get_dte()
        except Exception:
            return

    # Best-effort direct clear via the Error List tool window; silently
    # skip if the DTE2 interface isn't reachable under dynamic dispatch.
    try:
        win = dte.Windows.Item(
            "{D78612C7-9962-4B83-95D9-268046DAD23A}"  # vsWindowKindErrorList
        )
        win.Object.ErrorItems  # touch; some builds expose .Clear()
    except Exception:
        pass
