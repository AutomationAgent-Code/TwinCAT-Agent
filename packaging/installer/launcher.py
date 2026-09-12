"""TwinCAT Agent tray launcher and backend service controller."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from tkinter import Tk, messagebox

from installer_common import (
    CREATE_NO_WINDOW, backend_is_running, find_xae_shell,
    read_install_options, stop_installed_backend,
)

TRAY_MUTEX = r"Local\TwinCATAgentTray"
TRAY_SHUTDOWN_EVENT = r"Local\TwinCATAgentTrayShutdown"
UI_URL = "http://127.0.0.1:8766"


def _error(message: str) -> None:
    root = Tk(); root.withdraw()
    messagebox.showerror("TwinCAT Agent", message, parent=root)
    root.destroy()


def _acquire_single_instance():
    if os.name != "nt":
        return object(), True
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, TRAY_MUTEX)
    if not handle:
        raise OSError(ctypes.get_last_error(), "无法创建托盘单实例锁")
    return handle, ctypes.get_last_error() != 183


def _close_handle(handle) -> None:
    if os.name == "nt" and handle:
        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(handle)


def _create_shutdown_event():
    if os.name != "nt":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateEventW.restype = ctypes.c_void_p
    return kernel32.CreateEventW(None, False, False, TRAY_SHUTDOWN_EVENT)


def _event_is_set(handle) -> bool:
    if os.name != "nt" or not handle:
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    return kernel32.WaitForSingleObject(handle, 0) == 0


def request_tray_shutdown() -> bool:
    if os.name != "nt":
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenEventW.restype = ctypes.c_void_p
    handle = kernel32.OpenEventW(0x0002, False, TRAY_SHUTDOWN_EVENT)
    if not handle:
        return False
    try:
        return bool(kernel32.SetEvent(handle))
    finally:
        kernel32.CloseHandle(handle)


def start_backend(root: Path) -> None:
    if backend_is_running():
        return
    python = root / "runtime" / "python" / "python.exe"
    app = root / "app"
    if not python.is_file() or not app.is_dir():
        raise RuntimeError(f"后台运行环境不完整：{python}")
    with (root / "_backend.out").open("ab") as stdout, \
         (root / "_backend.log").open("ab") as stderr:
        subprocess.Popen(
            [str(python), "-m", "tc_agent.backend"], cwd=app,
            stdout=stdout, stderr=stderr, stdin=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW, close_fds=True,
        )
    for _ in range(40):
        if backend_is_running():
            return
        time.sleep(0.15)
    raise RuntimeError("TwinCAT Agent 服务启动失败，请检查 _backend.log 和端口 8765/8766。")


def stop_backend(root: Path) -> None:
    if backend_is_running() and not stop_installed_backend(root):
        raise RuntimeError("无法安全停止服务：端口由非当前安装目录的进程占用。")


def _configured_shell(root: Path) -> Path | None:
    options = read_install_options(root)
    configured = Path(str(options.get("xae_shell") or ""))
    if str(configured) and configured.is_file():
        return configured
    return find_xae_shell(twincat_version=str(options.get("twincat_version") or "") or None)


def open_xae(root: Path) -> None:
    shell = _configured_shell(root)
    if not shell:
        raise RuntimeError("未找到 TwinCAT XAE（TcXaeShell）。")
    subprocess.Popen([str(shell)], cwd=shell.parent)


def open_primary(root: Path) -> None:
    options = read_install_options(root)
    if bool(options.get("embed_xae", True)):
        open_xae(root)
    else:
        webbrowser.open(UI_URL)


def _status_image(icon_path: Path, running: bool):
    from PIL import Image, ImageDraw
    image = Image.open(icon_path).convert("RGBA").resize((64, 64))
    draw = ImageDraw.Draw(image)
    draw.ellipse((39, 39, 63, 63), fill="white")
    draw.ellipse((42, 42, 60, 60), fill="#20b26b" if running else "#d83b3b")
    return image


def run_tray(root: Path) -> None:
    import pystray
    from pystray import Menu, MenuItem

    icon_path = root / "twincat-agent.ico"
    if not icon_path.is_file():
        raise RuntimeError(f"托盘图标不存在：{icon_path}")
    stopped = threading.Event()
    shutdown_event = _create_shutdown_event()
    state = {"running": backend_is_running()}

    def label(_item):
        return "服务状态：运行中" if state["running"] else "服务状态：已停止"

    def refresh(notify=False):
        running = backend_is_running(); changed = running != state["running"]
        state["running"] = running
        icon.icon = _status_image(icon_path, running)
        icon.title = f"TwinCAT Agent · {'服务运行中' if running else '服务已停止'}"
        icon.update_menu()
        if notify and changed:
            icon.notify("后台服务已启动" if running else "后台服务已停止", "TwinCAT Agent")

    def action(operation, message=""):
        try:
            operation(); refresh()
            if message: icon.notify(message, "TwinCAT Agent")
        except Exception as exc:
            refresh(); _error(str(exc))

    def do_open_ui(*_): action(lambda: (start_backend(root), webbrowser.open(UI_URL)))
    def do_open_xae(*_): action(lambda: (start_backend(root), open_xae(root)))
    def do_start(*_): action(lambda: start_backend(root), "后台服务已启动")
    def do_stop(*_): action(lambda: stop_backend(root), "后台服务已停止")
    def do_restart(*_): action(lambda: (stop_backend(root), start_backend(root)), "后台服务已重新启动")
    def do_exit(*_):
        try: stop_backend(root)
        except Exception as exc: _error(str(exc))
        finally: stopped.set(); icon.stop()

    menu = Menu(
        MenuItem(label, None, enabled=False), Menu.SEPARATOR,
        MenuItem("打开浏览器界面", do_open_ui, default=True),
        MenuItem("打开 TwinCAT XAE", do_open_xae), Menu.SEPARATOR,
        MenuItem("启动服务", do_start, enabled=lambda _: not state["running"]),
        MenuItem("停止服务", do_stop, enabled=lambda _: state["running"]),
        MenuItem("重启服务", do_restart), Menu.SEPARATOR,
        MenuItem("退出 TwinCAT Agent（同时停止服务）", do_exit),
    )
    icon = pystray.Icon("TwinCAT Agent", _status_image(icon_path, state["running"]),
                        "TwinCAT Agent", menu)

    def monitor():
        while not stopped.wait(0.5):
            try:
                if _event_is_set(shutdown_event): do_exit(); return
                refresh(notify=True)
            except Exception: pass

    threading.Thread(target=monitor, name="agent-service-monitor", daemon=True).start()
    try: icon.run()
    finally: stopped.set(); _close_handle(shutdown_event)


def main() -> int:
    root = Path(sys.executable).resolve().parent
    if "--shutdown" in sys.argv:
        if request_tray_shutdown():
            for _ in range(70):
                if not backend_is_running(): return 0
                time.sleep(0.1)
        return 0 if stop_installed_backend(root) else 1
    mutex = None
    try:
        start_backend(root)
        if "--backend-only" not in sys.argv: open_primary(root)
        mutex, first = _acquire_single_instance()
        if not first: return 0
        run_tray(root)
        return 0
    except Exception as exc:
        _error(str(exc)); return 1
    finally:
        _close_handle(mutex)


if __name__ == "__main__":
    raise SystemExit(main())
