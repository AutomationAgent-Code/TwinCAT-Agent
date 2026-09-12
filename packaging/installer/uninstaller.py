"""TwinCAT Agent 图形卸载器。"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from tkinter import Tk, messagebox

from installer_common import (
    CREATE_NO_WINDOW,
    install_dir,
    read_install_options,
    remove_extension_native,
    remove_shortcuts,
    run_elevated,
    stop_installed_backend,
    stop_installed_tray,
    xae_shell_root,
    xae_is_running,
)


def _schedule_self_delete(path: Path) -> None:
    # cmd.exe is available even where PowerShell is disabled by enterprise
    # policy. ping supplies a short wait so this process can exit first.
    command = f'ping 127.0.0.1 -n 3 >nul & del /f /q "{path}"'
    subprocess.Popen(
        ["cmd.exe", "/d", "/s", "/c", command],
        creationflags=CREATE_NO_WINDOW,
    )


def _remove(target: Path) -> int:
    expected = install_dir().resolve()
    if target.resolve() != expected:
        raise RuntimeError(f"拒绝卸载非标准目录：{target}")
    options = read_install_options(target)
    extension_present = bool(
        options.get("extension_installed", options.get("embed_xae", True))
    )
    if extension_present and xae_is_running():
        raise RuntimeError("请先完全关闭 TwinCAT XAE，再执行卸载")
    if extension_present:
        configured_shell = str(options.get("xae_shell") or "").strip()
        if configured_shell:
            code = run_elevated(
                sys.executable,
                ["--remove-extension-native", str(xae_shell_root(Path(configured_shell)))],
            )
            if code:
                raise RuntimeError(f"管理员扩展卸载步骤失败（退出码 {code}）")
    stop_installed_backend(target)
    stop_installed_tray(target)
    remove_shortcuts()
    if target.is_dir():
        shutil.rmtree(target)
    return 0


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--remove-extension-native":
        remove_extension_native(Path(sys.argv[2]))
        return 0
    if len(sys.argv) >= 3 and sys.argv[1] == "--remove":
        root = Tk()
        root.withdraw()
        try:
            _remove(Path(sys.argv[2]))
            messagebox.showinfo("TwinCAT Agent", "卸载完成。", parent=root)
            return 0
        except Exception as exc:
            messagebox.showerror("TwinCAT Agent", f"卸载失败：{exc}", parent=root)
            return 1
        finally:
            root.destroy()
            _schedule_self_delete(Path(sys.executable))

    root = Tk()
    root.withdraw()
    if not messagebox.askyesno(
        "卸载 TwinCAT Agent",
        "确定要卸载 TwinCAT Agent 吗？\n\n项目对话和本机授权文件不会被删除。",
        parent=root,
    ):
        root.destroy()
        return 0
    root.destroy()
    temporary = (
        Path(tempfile.gettempdir())
        / f"TwinCAT-Agent-Uninstall-{uuid.uuid4().hex}.exe"
    )
    shutil.copy2(sys.executable, temporary)
    subprocess.Popen(
        [str(temporary), "--remove", str(install_dir())],
        creationflags=CREATE_NO_WINDOW,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
