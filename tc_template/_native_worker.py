"""32-bit native TwinCAT COM worker.

The product backend is 64-bit, while TcXaeShell 15 is a 32-bit process.  A
small embedded 32-bit Python runtime launches this module for each bridge
request so ROT/COM discovery happens in the same bitness as XAE.  Requests and
responses use UTF-8 JSON over stdin/stdout; no PowerShell host is involved.
"""

from __future__ import annotations

import json
import ctypes
import os
from pathlib import Path
import sys
import traceback


_DLL_DIRECTORY_HANDLES: list[object] = []


def _force_utf8_stdio() -> None:
    """Keep the parent/32-bit COM helper JSON transport Unicode-safe.

    The backend writes UTF-8 JSON, but an embedded Python runtime launched on
    a Chinese Windows installation may default ``stdin`` to GBK.  That turns
    an otherwise correct Simplified-Chinese PLC comment into mojibake before
    it reaches the Automation Interface.
    """
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8", errors="strict")
        except (AttributeError, ValueError):
            # Python 3.8+ supports reconfigure; the fallback is only for a
            # redirected legacy host where JSON contains escaped ASCII anyway.
            pass


def prepare_native_runtime() -> str:
    """Make the installed architecture-matched TwinCAT ADS DLL visible."""
    override = os.environ.get("TC_AGENT_ADS_DLL", "").strip()
    pf86 = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    common = "Common64" if ctypes.sizeof(ctypes.c_void_p) == 8 else "Common32"
    windows_dir = "System32" if common == "Common64" else "SysWOW64"
    roots = [
        Path(os.environ["TWINCAT3DIR"]) if os.environ.get("TWINCAT3DIR") else None,
        Path(r"C:\TwinCAT\3.1"),
        Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
        / "Beckhoff" / "TwinCAT" / "3.1",
        pf86 / "Beckhoff" / "TwinCAT" / "3.1",
    ]
    candidates = [
        Path(override) if override else None,
        pf86 / "Beckhoff" / "TwinCAT" / common / "TcAdsDll.dll",
        *[root.parent / common / "TcAdsDll.dll" for root in roots if root],
        *[root.parent / "AdsApi" / "TcAdsDll" / "TcAdsDll.dll" for root in roots if root],
        Path(os.environ.get("WINDIR", r"C:\Windows"))
        / windows_dir / "TcAdsDll.dll",
    ]
    for dll_path in candidates:
        if dll_path and dll_path.is_file():
            dll_dir = str(dll_path.parent)
            os.environ["PATH"] = dll_dir + os.pathsep + os.environ.get("PATH", "")
            if hasattr(os, "add_dll_directory"):
                _DLL_DIRECTORY_HANDLES.append(os.add_dll_directory(dll_dir))
            return str(dll_path)
    raise FileNotFoundError(
        f"未找到匹配当前进程的 {common}/TcAdsDll.dll；请确认 TwinCAT XAE/ADS 已正确安装"
    )


def _execute(request: dict) -> object:
    prepare_native_runtime()
    kind = str(request.get("kind") or "com")
    command = str(request.get("command") or "")
    args = request.get("args") or {}
    if not isinstance(args, dict):
        raise ValueError("request.args must be an object")
    if not command:
        raise ValueError("request.command is required")

    if kind == "com":
        from ._native_bridge import dispatch

        return dispatch(command, args)
    if kind == "io":
        from .io_native import execute

        return execute(command, **args)
    raise ValueError(f"unsupported native worker request kind: {kind}")


def main() -> int:
    _force_utf8_stdio()
    try:
        request = json.load(sys.stdin)
        if not isinstance(request, dict):
            raise ValueError("request must be a JSON object")
        result = _execute(request)
        response = {"ok": True, "data": result}
    except Exception as exc:
        response = {
            "ok": False,
            "error": str(exc),
            "error_type": type(exc).__name__,
        }
        if str(request.get("debug") if "request" in locals() else "") == "1":
            response["traceback"] = traceback.format_exc()
    # Keep the subprocess transport ASCII-only. The embedded 32-bit helper can
    # inherit a GBK console even though the parent requests UTF-8 text mode.
    sys.stdout.write(json.dumps(response, ensure_ascii=True))
    sys.stdout.flush()
    return 0 if response["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
