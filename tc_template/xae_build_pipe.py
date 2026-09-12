"""Local IPC client for the TwinCAT Agent XAE UI-thread build service.

The service is hosted by the VSIX inside a specific TcXaeShell process.  It
executes DTE SolutionBuild on that process's UI thread, so an Agent-initiated
build does not depend on the XAE window being foregrounded.  This module uses
only Win32 ``ctypes``; the normal COM fallback remains available when an older
VSIX is loaded or the user is not using the embedded panel.
"""

from __future__ import annotations

import ctypes
import json
import os
from ctypes import wintypes


_PIPE_PREFIX = "TwinCAT-Agent-Build-"
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_OPEN_EXISTING = 3
_ERROR_PIPE_BUSY = 231
_ERROR_BROKEN_PIPE = 109
_ERROR_MORE_DATA = 234
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024


def normalize_diagnostics(result):
    """Compatibility with VSIX versions where errorsRead meant itemCount > 0."""
    if (isinstance(result, dict) and 'diagnosticsAvailable' not in result
            and result.get('errorSource') == 'dte-error-items-ui-thread'):
        # That exact legacy source was emitted only after successful enumeration.
        # Exceptions used unavailable-no-focus, never this value.
        result = {**result, 'errorsRead': True, 'diagnosticsAvailable': True,
                  'diagnosticsContract': 'legacy-ui-thread-enumeration'}
    if isinstance(result, dict):
        from .plc_build_diagnostics import normalize_compiler_severity
        result = normalize_compiler_severity(result)
    return result


def pipe_name(pid: int) -> str:
    """Return the private, per-XAE-process named pipe path."""
    return rf"\\.\pipe\{_PIPE_PREFIX}{int(pid)}"


def request_build(pid: int, timeout_ms: int = 1500, *, command: str = 'build') -> dict | None:
    """Ask a loaded VSIX to build on XAE's UI thread.

    ``None`` means the optional service is unavailable, so callers must use
    the established COM bridge.  A response dictionary is returned verbatim,
    including an ``ok: false`` result produced by the VSIX.
    """
    if command not in {'build', 'rebuild', 'diagnostics'}:
        raise ValueError('Unsupported XAE pipe command')
    if os.name != "nt" or int(pid or 0) <= 0:
        return None

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    kernel32.WaitNamedPipeW.restype = wintypes.BOOL
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.WriteFile.argtypes = [
        wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
    ]
    kernel32.WriteFile.restype = wintypes.BOOL
    kernel32.ReadFile.argtypes = [
        wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
    ]
    kernel32.ReadFile.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    name = pipe_name(pid)
    # A pipe can be briefly busy while a previous request is being serviced.
    if not kernel32.WaitNamedPipeW(name, max(1, int(timeout_ms))):
        return None
    handle = kernel32.CreateFileW(
        name, _GENERIC_READ | _GENERIC_WRITE, 0, None, _OPEN_EXISTING, 0, None
    )
    if handle == _INVALID_HANDLE_VALUE:
        # An unavailable pipe is an expected condition during VSIX upgrades.
        if ctypes.get_last_error() in (_ERROR_PIPE_BUSY, 2):
            return None
        return None
    sent = False
    try:
        payload = ('{"command":"' + command + '"}\n').encode('ascii')
        written = wintypes.DWORD()
        if not kernel32.WriteFile(handle, payload, len(payload), ctypes.byref(written), None):
            return None
        sent = True
        # Named pipes in byte mode may split one JSON line across multiple
        # ReadFile calls.  Do not treat the first available fragment as a
        # complete response (that made valid UI-thread diagnostics look like
        # a missing pipe whenever the errors array was non-empty).
        chunks: list[bytes] = []
        total = 0
        while total < _MAX_RESPONSE_BYTES:
            buffer = ctypes.create_string_buffer(4096)
            read = wintypes.DWORD()
            ok = kernel32.ReadFile(
                handle, buffer, len(buffer) - 1, ctypes.byref(read), None
            )
            if read.value:
                chunk = buffer.raw[:read.value]
                chunks.append(chunk)
                total += len(chunk)
                if b"\n" in chunk:
                    break
            if ok:
                # Byte-mode pipes may return a partial successful read. Keep
                # receiving until the StreamWriter newline is observed.
                continue
            if ctypes.get_last_error() == _ERROR_MORE_DATA:
                continue
            if ctypes.get_last_error() == _ERROR_BROKEN_PIPE and chunks:
                break
            return {"ok": False, "uncertain": True,
                    "error_type": "xae_build_result_uncertain",
                    "error": "XAE build pipe closed before returning a result"}
        raw = b"".join(chunks).decode("utf-8", errors="replace").strip()
        if not raw:
            return {"ok": False, "uncertain": True,
                    "error_type": "xae_build_result_uncertain",
                    "error": "XAE build pipe returned no result"}
        result = json.loads(raw)
        # Older VSIX builds understand only build/diagnostics.  For the newer
        # explicit rebuild action, an unsupported-service response means the
        # optional transport is unavailable; it is safe to use the bridge's
        # equivalent Rebuild command.  A real UI-thread build error remains a
        # dictionary and must never trigger a duplicate build fallback.
        if (isinstance(result, dict) and result.get("ok") is False
                and command == "rebuild"
                and str(result.get("error") or "").strip().casefold()
                == "unsupported request"):
            return None
        return normalize_diagnostics(result) if isinstance(result, dict) else None
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        if sent:
            return {"ok": False, "uncertain": True,
                    "error_type": "xae_build_result_uncertain",
                    "error": f"XAE build result could not be read: {exc}"}
        return None
    finally:
        kernel32.CloseHandle(handle)


def request_diagnostics(pid: int, timeout_ms: int = 1500) -> dict | None:
    """Read UI-thread diagnostics only; an old extension may return unsupported.

    Never fall back to issuing a build merely to recover the Error List.
    """
    return request_build(pid, timeout_ms, command='diagnostics')
