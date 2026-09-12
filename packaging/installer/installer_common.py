"""TwinCAT Agent 安装器、启动器和卸载器共用的 Windows 辅助函数。"""

from __future__ import annotations

import ctypes
import csv
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
import xml.etree.ElementTree as ET
from ctypes import wintypes
from pathlib import Path

PRODUCT_NAME = "TwinCAT Agent"
INSTALL_FOLDER = "TwinCAT Agent"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
TRAY_SHUTDOWN_EVENT = r"Local\TwinCATAgentTrayShutdown"
XAE_EXTENSION_REQUIRED_FILES = (
    "extension.vsixmanifest",
    "TwinCATAgent.Xae.dll",
    "TwinCATAgent.Xae.pkgdef",
)
# These names were used by this product before the namespace/assembly cleanup.
# They are intentionally scoped to our exact extension directory; never scan or
# remove Beckhoff's separate TwinCAT-CoAgent/ChatVs.dll installation.
XAE_EXTENSION_LEGACY_FILES = (
    "TcCoAgent.dll",
    "TcCoAgent.pkgdef",
    "TwinCATAgent.dll",
    "TwinCATAgent.pkgdef",
    "tcxaeshell",
)


def local_appdata() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")


def install_dir() -> Path:
    return local_appdata() / "Programs" / INSTALL_FOLDER


def legacy_install_dirs() -> list[Path]:
    """Product-owned install folders used before the spaced product name."""
    parent = local_appdata() / "Programs"
    return [parent / "TwinCATAgent", parent / "TwinCAT-Agent"]


def resource_path(*parts: str) -> Path:
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return root.joinpath(*parts)


def xae_shell_candidates(twincat_version: str | None = None) -> list[Path]:
    r"""Return supported 32-bit XAE Shell locations.

    TcXaeShell is a standalone VS isolated shell and normally lives below
    ``Program Files`` for both 4024 and 4026.  ``C:\TwinCAT\3.1`` is the 4024
    component root, not normally the shell executable root.  Keep the classic
    paths only as custom-install fallbacks.
    """
    program_files_x86 = Path(
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    )
    program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    twincat_root = (
        Path(os.environ["TWINCAT3DIR"])
        if os.environ.get("TWINCAT3DIR")
        else Path(r"C:\TwinCAT\3.1")
    )
    classic = [
        twincat_root / "Components/Base/TcXaeShell/Common7/IDE/TcXaeShell.exe",
        Path(r"C:\TwinCAT\3.1")
        / "Components/Base/TcXaeShell/Common7/IDE/TcXaeShell.exe",
        twincat_root / "TcXaeShell/Common7/IDE/TcXaeShell.exe",
        Path(r"C:\TwinCAT\3.1") / "TcXaeShell/Common7/IDE/TcXaeShell.exe",
    ]
    package = [
        program_files_x86 / "Beckhoff/TcXaeShell/Common7/IDE/TcXaeShell.exe",
        program_files / "Beckhoff/TcXaeShell/Common7/IDE/TcXaeShell.exe",
    ]
    candidates = package + classic
    result: list[Path] = []
    for candidate in candidates:
        if candidate not in result:
            result.append(candidate)
    return result


def find_xae_shell(
    candidates: list[Path] | None = None,
    *,
    twincat_version: str | None = None,
) -> Path | None:
    return next(
        (
            path
            for path in (candidates or xae_shell_candidates(twincat_version))
            if path.is_file()
        ),
        None,
    )


def xae_shell_root(shell: Path) -> Path:
    """Return the TcXaeShell root for ``.../Common7/IDE/TcXaeShell.exe``."""
    if shell.name.lower() != "tcxaeshell.exe" or len(shell.parents) < 3:
        raise ValueError(f"无效的 TcXaeShell 路径：{shell}")
    return shell.parents[2]


def refresh_xae_package_cache(shell: Path) -> None:
    """Rebuild TcXaeShell's package/menu cache after extension file copy.

    Its VSIXInstaller depends on a Visual Studio Setup component catalog that
    the standalone TwinCAT Shell does not expose, producing false 2003 errors.
    ``/setup`` is the shell-native scan of extension pkgdef files.
    """
    try:
        result = subprocess.run(
            [str(shell), "/setup"],
            timeout=120,
            creationflags=CREATE_NO_WINDOW,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("XAE 扩展缓存重建超时，请确认 XAE 已完全关闭后重试") from exc
    if result.returncode:
        raise RuntimeError(f"XAE 扩展缓存重建失败（退出码 {result.returncode}）")


def _twincat_roots() -> list[Path]:
    candidates = [
        Path(os.environ["TWINCAT3DIR"]) if os.environ.get("TWINCAT3DIR") else None,
        Path(r"C:\TwinCAT\3.1"),
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
        / "Beckhoff/TwinCAT/3.1",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        / "Beckhoff/TwinCAT/3.1",
        Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
        / "Beckhoff/TwinCAT/3.1",
    ]
    result: list[Path] = []
    for candidate in candidates:
        if candidate and candidate.is_dir() and candidate not in result:
            result.append(candidate)
    return result


def _read_twincat_version_xml(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        match = re.search(r"3\.1\.40\d{2}(?:\.\d+)?", text)
        if match:
            return match.group(0)
        root = ET.fromstring(text)
        settings = {
            str(item.attrib.get("key", "")): str(item.attrib.get("value", ""))
            for item in root.findall(".//add")
        }
        if settings.get("major") and settings.get("minor") and settings.get("build"):
            version = f"{settings['major']}.{settings['minor']}.{settings['build']}"
            if settings.get("revision"):
                version += f".{settings['revision']}"
            return version
    except (OSError, ET.ParseError):
        pass
    return None


def _read_file_version(path: Path) -> str | None:
    """Read VS_FIXEDFILEINFO without pywin32 (installer-safe)."""
    if not path.is_file() or os.name != "nt":
        return None
    try:
        version = ctypes.WinDLL("version", use_last_error=True)
        version.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, wintypes.LPDWORD]
        version.GetFileVersionInfoSizeW.restype = wintypes.DWORD
        version.GetFileVersionInfoW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
        ]
        version.GetFileVersionInfoW.restype = wintypes.BOOL
        version.VerQueryValueW.argtypes = [
            wintypes.LPCVOID,
            wintypes.LPCWSTR,
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.UINT),
        ]
        version.VerQueryValueW.restype = wintypes.BOOL

        ignored = wintypes.DWORD()
        size = version.GetFileVersionInfoSizeW(str(path), ctypes.byref(ignored))
        if not size:
            return None
        buffer = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(str(path), 0, size, buffer):
            return None
        pointer = wintypes.LPVOID()
        length = wintypes.UINT()
        if not version.VerQueryValueW(buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)):
            return None
        values = ctypes.cast(pointer, ctypes.POINTER(wintypes.DWORD * 13)).contents
        file_ms, file_ls = int(values[2]), int(values[3])
        return ".".join(
            str(value)
            for value in (
                file_ms >> 16,
                file_ms & 0xFFFF,
                file_ls >> 16,
                file_ls & 0xFFFF,
            )
        )
    except (OSError, ValueError):
        return None


def detect_twincat_version(roots: list[Path] | None = None) -> str | None:
    for root in roots if roots is not None else _twincat_roots():
        for relative in (
            "SDK/TwinCATVersion.xml",
            "System/TwinCATVersion.xml",
            "TwinCATVersion.xml",
        ):
            version = _read_twincat_version_xml(root / relative)
            if version:
                return version
        for relative in ("System/TcSysSrv.exe", "System/TcSystemService.exe"):
            version = _read_file_version(root / relative)
            if version:
                return version
    return None


def is_supported_twincat(version: str | None) -> bool:
    return bool(version and re.match(r"^3\.1\.402(?:4|6)(?:\.|$)", version))


def extension_is_installed(shell: Path | None = None) -> bool:
    shell = shell or find_xae_shell()
    return bool(
        shell
        and any((shell.parent / "Extensions" / "TwinCAT Agent" / name).is_file()
                for name in XAE_EXTENSION_REQUIRED_FILES + XAE_EXTENSION_LEGACY_FILES)
    )


def read_install_options(root: Path) -> dict[str, object]:
    path = root / "install_options.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    # Existing installations predate the option and always embedded into XAE.
    return {"embed_xae": True}


def xae_is_running() -> bool:
    result = subprocess.run(
        ["tasklist.exe", "/FI", "IMAGENAME eq TcXaeShell.exe", "/NH"],
        capture_output=True,
        creationflags=CREATE_NO_WINDOW,
        check=False,
    )
    return b"tcxaeshell.exe" in result.stdout.lower()


def backend_is_running() -> bool:
    try:
        connection = http.client.HTTPConnection("127.0.0.1", 8766, timeout=0.4)
        connection.request("GET", "/")
        response = connection.getresponse()
        body = response.read(4096)
        connection.close()
        return response.status == 200 and b"TwinCAT Agent" in body
    except Exception:
        return False


def request_tray_shutdown() -> bool:
    """Ask the installed tray process to exit without launching its old EXE.

    Upgrade and uninstall code must not execute an older ``TwinCAT-Agent.exe
    --shutdown``.  That executable may contain an obsolete PID discovery
    routine and can show an unhandled GUI exception before the current safety
    checks get a chance to run.  The tray already owns this named event, so
    signaling it is both version-independent and non-destructive.
    """
    if os.name != "nt":
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.OpenEventW.restype = wintypes.HANDLE
    kernel32.SetEvent.argtypes = [wintypes.HANDLE]
    kernel32.SetEvent.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenEventW(0x0002, False, TRAY_SHUTDOWN_EVENT)
    if not handle:
        return False
    try:
        return bool(kernel32.SetEvent(handle))
    finally:
        kernel32.CloseHandle(handle)


def _running_process_ids() -> set[int]:
    """Return visible Windows process IDs without using WMI or PowerShell."""
    if os.name != "nt":
        return set()
    result = subprocess.run(
        ["tasklist.exe", "/FO", "CSV", "/NH"],
        capture_output=True,
        creationflags=CREATE_NO_WINDOW,
        check=False,
    )
    process_ids: set[int] = set()
    for raw_line in (result.stdout or b"").splitlines():
        try:
            row = next(csv.reader([raw_line.decode("mbcs", errors="replace")]))
        except (StopIteration, UnicodeError):
            continue
        if len(row) < 2:
            continue
        try:
            pid = int(row[1])
        except ValueError:
            continue
        if pid > 0:
            process_ids.add(pid)
    return process_ids


def _owned_tray_pids(root: Path) -> set[int]:
    """Find only TwinCAT Agent tray processes from current/legacy installs."""
    roots = [Path(root).resolve(), *(path.resolve() for path in legacy_install_dirs())]
    expected = {
        str(owned_root / name).casefold()
        for owned_root in roots
        for name in ("TwinCAT-Agent.exe", "TwinCATAgent.exe")
    }
    result: set[int] = set()
    for pid in _running_process_ids():
        image = _process_image(pid)
        if image is None:
            continue
        try:
            image_key = str(image.resolve()).casefold()
        except OSError:
            image_key = str(image).casefold()
        if image_key in expected:
            result.add(pid)
    return result


def stop_installed_tray(root: Path, timeout: float = 8.0) -> bool:
    """Gracefully stop the product tray, then terminate only its exact image.

    The tray executable owns its own file while it is running. Upgrade and
    uninstall must therefore wait for the named shutdown event to be handled
    before replacing the installation directory. A stale tray from an older
    install can be force-stopped after its executable path has been validated;
    unrelated processes and the Beckhoff CoAgent are never candidates.
    """
    root = Path(root).resolve()
    before = _owned_tray_pids(root)
    if not before:
        return False
    request_tray_shutdown()
    deadline = time.monotonic() + max(0.1, min(30.0, timeout))
    while time.monotonic() < deadline:
        remaining = before.intersection(_owned_tray_pids(root))
        if not remaining:
            return True
        time.sleep(0.1)

    # Repeat the path check immediately before termination so a PID reused by
    # an unrelated process cannot be killed by the fallback.
    remaining = before.intersection(_owned_tray_pids(root))
    for pid in sorted(remaining):
        if not _terminate_process(pid, timeout=2.0):
            raise RuntimeError(
                f"无法关闭旧版 TwinCAT Agent 托盘（PID {pid}）。"
                "请手动退出托盘后重试。"
            )
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if not before.intersection(_owned_tray_pids(root)):
            return True
        time.sleep(0.1)
    raise RuntimeError("旧版 TwinCAT Agent 托盘仍占用安装文件，请退出托盘后重试。")


def _backend_info() -> dict:
    """Read the identity endpoint exposed by current Agent backends."""
    try:
        connection = http.client.HTTPConnection("127.0.0.1", 8766, timeout=0.5)
        connection.request("GET", "/__agent_info")
        response = connection.getresponse()
        payload = json.loads(response.read(8192).decode("utf-8"))
        connection.close()
        if response.status == 200 and isinstance(payload, dict) \
                and payload.get("product") == PRODUCT_NAME:
            return payload
    except (OSError, ValueError, UnicodeError):
        pass
    return {}


def _backend_log_claims_project(root: Path) -> bool:
    """Compatibility identity for pre-identity-endpoint Python backends."""
    log = root / "_backend.log"
    try:
        text = log.read_text(encoding="utf-8", errors="ignore")[-65536:]
    except OSError:
        return False
    marker = f"project: {root / 'app'}"
    return marker.casefold() in text.casefold()


def _listener_pids(port: int) -> set[int]:
    """Return TCP listener/connection owners whose local endpoint uses port."""
    result = subprocess.run(
        ["netstat.exe", "-ano", "-p", "TCP"],
        capture_output=True,
        creationflags=CREATE_NO_WINDOW,
        check=False,
    )
    pids: set[int] = set()
    for raw_line in (result.stdout or b"").splitlines():
        parts = raw_line.decode("ascii", errors="ignore").split()
        if len(parts) < 4 or parts[0].upper() != "TCP":
            continue
        if not parts[1].endswith(f":{int(port)}"):
            continue
        try:
            pid = int(parts[-1])
            # Windows can report PID 0 for a kernel-owned/stale endpoint.
            # It is never a user process and must not enter the termination
            # allow-list below.
            if pid > 0:
                pids.add(pid)
        except ValueError:
            pass
    return pids


def _process_image(pid: int) -> Path | None:
    """Read a process executable path without PowerShell/WMI."""
    if os.name != "nt" or pid <= 0:
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return None
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(
            handle, 0, buffer, ctypes.byref(size)
        ):
            return None
        return Path(buffer.value)
    finally:
        kernel32.CloseHandle(handle)


def _terminate_process(pid: int, timeout: float = 5.0) -> bool:
    """Terminate one already-validated process and wait for file unlock."""
    if os.name != "nt" or pid <= 0:
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(0x0001 | 0x00100000, False, int(pid))
    if not handle:
        return False
    try:
        if not kernel32.TerminateProcess(handle, 0):
            return False
        wait_ms = max(1, min(30000, int(timeout * 1000)))
        kernel32.WaitForSingleObject(handle, wait_ms)
        return True
    finally:
        kernel32.CloseHandle(handle)


def stop_installed_backend(root: Path, timeout: float = 6.0) -> bool:
    """Stop only the backend executable installed below ``root``.

    Existing v1.0.9 and earlier installations do not have a PID file, so the
    HTTP listener PID is discovered through netstat.  The executable path is
    validated before termination; an unrelated process owning port 8766 is
    never killed.
    """
    root_hint = Path(root)
    root = root.resolve()
    expected_images: set[Path] = set()
    for owned_root in [root, *legacy_install_dirs()]:
        image = (owned_root / "runtime/python/python.exe").resolve()
        if image.is_file():
            expected_images.add(image)

    listener_pids = _listener_pids(8766)
    matched: list[int] = []
    for pid in listener_pids:
        image = _process_image(pid)
        if image is None:
            continue
        try:
            is_ours = image.resolve() in expected_images
        except OSError:
            is_ours = any(
                str(image).casefold() == str(expected).casefold()
                for expected in expected_images
            )
        if is_ours:
            matched.append(pid)

    # Some users start the backend with the system Python (``py -m
    # tc_agent.backend``) instead of the embedded interpreter.  It is still
    # our backend when its identity endpoint reports this installation.  The
    # log check keeps upgrades compatible with pre-identity versions and is
    # only used together with a successful TwinCAT Agent HTTP health check.
    info = _backend_info()
    expected_projects = {
        str((owned_root / "app").resolve()).casefold()
        for owned_root in [root, *legacy_install_dirs()]
    }
    info_project = str(info.get("project_dir") or "").strip()
    info_pid = int(info.get("pid") or 0) if str(info.get("pid") or "").isdigit() else 0
    try:
        normalized_info_project = str(Path(info_project).resolve()).casefold()
    except OSError:
        normalized_info_project = info_project.casefold()
    if (
        info_project
        and normalized_info_project in expected_projects
        and info_pid > 0
        and info_pid in listener_pids
    ):
        matched.append(info_pid)
    elif not matched and backend_is_running() and _backend_log_claims_project(root_hint):
        matched.extend(pid for pid in listener_pids if pid > 0)
    matched = list(dict.fromkeys(matched))

    if not matched:
        if backend_is_running():
            raise RuntimeError(
                "端口 8766 正被其他程序占用，无法安全停止旧版 Agent。"
                "请关闭占用该端口的程序后重试。"
            )
        return False

    for pid in matched:
        if not _terminate_process(pid, timeout=timeout):
            raise RuntimeError(
                f"无法停止旧版 TwinCAT Agent 后台（PID {pid}）。"
                "请关闭 Agent，或确认它没有以管理员身份运行后重试。"
            )

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(pid in _listener_pids(8766) for pid in matched):
            return True
        time.sleep(0.1)
    raise RuntimeError("旧版 TwinCAT Agent 后台停止超时，请重新启动电脑后再安装。")


class _ShellExecuteInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("fMask", wintypes.ULONG),
        ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", wintypes.LPVOID),
        ("lpClass", wintypes.LPCWSTR),
        ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD),
        ("hIconOrMonitor", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    ]


def run_elevated(program: str | Path, arguments: list[str]) -> int:
    """弹 UAC，以管理员权限运行一个子进程并等待完成。"""
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(_ShellExecuteInfo)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    info = _ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x00000040  # SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = str(program)
    info.lpParameters = subprocess.list2cmdline(arguments)
    info.nShow = 0
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        error = ctypes.get_last_error()
        if error == 1223:
            raise RuntimeError("用户取消了管理员授权")
        raise OSError(error, "无法启动管理员安装步骤")
    if not info.hProcess:
        raise RuntimeError("管理员安装步骤没有返回进程句柄")
    try:
        kernel32.WaitForSingleObject(info.hProcess, 0xFFFFFFFF)
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(
            info.hProcess, ctypes.byref(exit_code)
        ):
            raise OSError(ctypes.get_last_error(), "无法读取安装步骤结果")
        return int(exit_code.value)
    finally:
        kernel32.CloseHandle(info.hProcess)


def create_shortcut(
    shortcut: Path,
    target: Path,
    arguments: str = "",
    icon: Path | None = None,
    working_dir: Path | None = None,
) -> None:
    """Create a .lnk through WScript.Shell COM without PowerShell."""
    import win32com.client

    shortcut.parent.mkdir(parents=True, exist_ok=True)
    shell = win32com.client.Dispatch("WScript.Shell")
    link = shell.CreateShortcut(str(shortcut))
    link.TargetPath = str(target)
    link.Arguments = str(arguments or "")
    link.WorkingDirectory = str(working_dir or target.parent)
    if icon:
        link.IconLocation = f"{icon},0"
    link.Save()


def _validated_extension_path(shell_root: Path) -> Path:
    root = shell_root.resolve()
    shell = root / "Common7/IDE/TcXaeShell.exe"
    if not shell.is_file():
        raise RuntimeError(f"无效的 TcXaeShell 根目录：{root}")
    destination = (root / "Common7/IDE/Extensions/TwinCAT Agent").resolve()
    extensions = (root / "Common7/IDE/Extensions").resolve()
    if extensions not in destination.parents:
        raise RuntimeError(f"扩展目标路径越界：{destination}")
    return destination


def install_extension_native(source: Path, shell_root: Path, *, refresh_cache: bool = False) -> int:
    """Elevated worker: update the XAE extension without powershell.exe.

    Stage and validate the new package before touching the exact product-owned
    directory.  A copy/refresh failure restores the previous directory; the
    transaction never targets Beckhoff's separate CoAgent installation.
    """
    source = source.resolve()
    if not source.is_dir() or any(not (source / name).is_file() for name in XAE_EXTENSION_REQUIRED_FILES):
        raise RuntimeError(f"扩展源目录不完整：{source}")
    destination = _validated_extension_path(shell_root)
    transaction_root = Path(tempfile.mkdtemp(prefix="TwinCATAgent-Xae-"))
    stage = transaction_root / "stage"
    backup = transaction_root / "backup"
    had_destination = destination.exists()
    backup_ready = False
    try:
        for source_file in source.rglob("*"):
            relative = source_file.relative_to(source)
            if relative.name in XAE_EXTENSION_LEGACY_FILES:
                raise RuntimeError(f"扩展源包含旧版残留：{relative}")
            destination_file = stage / relative
            if source_file.is_dir():
                destination_file.mkdir(parents=True, exist_ok=True)
            else:
                destination_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_file, destination_file)
        if any(not (stage / name).is_file() for name in XAE_EXTENSION_REQUIRED_FILES):
            raise RuntimeError(f"扩展 staging 不完整：{stage}")
        if had_destination:
            shutil.copytree(destination, backup)
            backup_ready = True
        destination.mkdir(parents=True, exist_ok=True)
        for legacy in XAE_EXTENSION_LEGACY_FILES:
            stale = destination / legacy
            if stale.is_dir():
                shutil.rmtree(stale)
            elif stale.exists():
                stale.unlink()
        for staged_file in stage.rglob("*"):
            relative = staged_file.relative_to(stage)
            destination_file = destination / relative
            if staged_file.is_dir():
                destination_file.mkdir(parents=True, exist_ok=True)
            else:
                destination_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(staged_file, destination_file)
        # TcXaeShell writes extension.configurationchanged during `/setup`; both
        # the copy and cache rebuild must run elevated. Running `/setup` after
        # the elevated worker returns fails with "Elevation required".
        if refresh_cache:
            refresh_xae_package_cache(shell_root / "Common7/IDE/TcXaeShell.exe")
        return sum(1 for path in destination.rglob("*") if path.is_file())
    except Exception:
        try:
            if backup_ready:
                if destination.exists():
                    shutil.rmtree(destination)
                shutil.copytree(backup, destination)
            elif not had_destination and destination.exists():
                shutil.rmtree(destination)
        except Exception as rollback_error:
            raise RuntimeError(f"扩展更新失败，回滚也失败：{rollback_error}")
        raise
    finally:
        shutil.rmtree(transaction_root, ignore_errors=True)


def remove_extension_native(shell_root: Path) -> bool:
    """Elevated worker: remove only TwinCAT Agent's validated extension dir."""
    destination = _validated_extension_path(shell_root)
    if not destination.exists():
        return False
    parked = destination.parent / f".TwinCATAgent-Xae-remove-{uuid.uuid4().hex}"
    destination.rename(parked)
    try:
        shutil.rmtree(parked)
    except Exception:
        if not destination.exists() and parked.exists():
            parked.rename(destination)
        raise
    return True


def startup_shortcut() -> Path:
    return (
        Path(os.environ["APPDATA"])
        / "Microsoft/Windows/Start Menu/Programs/Startup/TwinCAT Agent.lnk"
    )


def start_menu_dir() -> Path:
    return (
        Path(os.environ["APPDATA"])
        / "Microsoft/Windows/Start Menu/Programs/TwinCAT Agent"
    )


def desktop_shortcut() -> Path:
    return Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop/TwinCAT Agent.lnk"


def safe_extract(zip_path: Path, destination: Path) -> None:
    root = destination.resolve()
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            target = (destination / member.filename).resolve()
            if target != root and root not in target.parents:
                raise RuntimeError(f"安装包包含越界路径：{member.filename}")
        archive.extractall(destination)


def remove_shortcuts() -> None:
    for path in (startup_shortcut(), desktop_shortcut()):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    menu = start_menu_dir()
    if menu.is_dir():
        for path in menu.iterdir():
            if path.is_file():
                path.unlink()
        try:
            menu.rmdir()
        except OSError:
            pass
