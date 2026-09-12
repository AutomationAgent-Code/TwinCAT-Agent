"""TwinCAT Agent 单文件图形安装器。"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import traceback
from pathlib import Path
from tkinter import BooleanVar, PhotoImage, StringVar, Tk, messagebox, ttk

from installer_common import (
    CREATE_NO_WINDOW,
    create_shortcut,
    detect_twincat_version,
    desktop_shortcut,
    extension_is_installed,
    find_xae_shell,
    install_dir,
    install_extension_native,
    is_supported_twincat,
    legacy_install_dirs,
    resource_path,
    run_elevated,
    safe_extract,
    start_menu_dir,
    stop_installed_backend,
    stop_installed_tray,
    startup_shortcut,
    xae_shell_root,
    xae_is_running,
)

def _installer_version() -> str:
    """Read the exact version embedded with this Setup executable.

    ``build_installer.ps1`` first builds the portable payload from VERSION, so
    this makes the file name, Windows metadata and visible installer version
    all use the same source of truth.
    """
    candidates = (
        resource_path("payload", "TwinCAT-Agent-Portable", "app", "VERSION"),
        Path(__file__).resolve().parents[2] / "VERSION",
    )
    for candidate in candidates:
        try:
            value = candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            return value
    return "未知版本"


APP_VERSION = _installer_version()
APP_TITLE = f"TwinCAT Agent 安装程序 v{APP_VERSION}"
ACCENT = "#078de5"
BG = "#f3f6f9"
PANEL = "#ffffff"


def _preserve_files(root: Path) -> dict[str, bytes]:
    preserved: dict[str, bytes] = {}
    for relative in (
        "app/tc_agent/config.json",
        "app/tc_agent/chat_history.json",
        "app/tc_agent/agent.db",
        "app/tc_agent/agent.db-wal",
        "app/tc_agent/agent.db-shm",
    ):
        path = root / relative
        if path.is_file():
            preserved[relative] = path.read_bytes()
    return preserved


def _restore_files(root: Path, preserved: dict[str, bytes]) -> None:
    for relative, content in preserved.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def _install(
    notify,
    create_desktop: bool,
    launch_after: bool,
    test_root: Path | None = None,
    *,
    embed_xae: bool = True,
) -> Path:
    target = test_root.resolve() if test_root else install_dir().resolve()
    skip_system = test_root is not None
    twincat_version = detect_twincat_version()
    shell = find_xae_shell(twincat_version=twincat_version)
    if not skip_system:
        if embed_xae and not shell:
            raise RuntimeError("未找到 TwinCAT XAE，请取消“嵌入 TwinCAT XAE”或先安装 XAE")
        if embed_xae and twincat_version and not is_supported_twincat(twincat_version):
            raise RuntimeError(
                f"当前只支持 TwinCAT Build 4024/4026，检测到 {twincat_version}"
            )
        if embed_xae and xae_is_running():
            raise RuntimeError("请先完全关闭 TwinCAT XAE，再开始安装")

    payload_dir = resource_path("payload", "TwinCAT-Agent-Portable")
    launcher = resource_path("payload", "TwinCAT-Agent.exe")
    uninstaller = resource_path("payload", "TwinCAT-Agent-Uninstall.exe")
    icon = resource_path("assets", "twincat-agent.ico")
    for required in (payload_dir, launcher, uninstaller, icon):
        if required == payload_dir and required.is_dir():
            continue
        if not required.is_file():
            raise RuntimeError(f"安装器资源缺失：{required.name}")

    notify("正在解压程序文件…", 20)
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    staging = parent / f".{target.name}.installing"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        shutil.copytree(payload_dir, staging, dirs_exist_ok=True)
        shutil.copy2(launcher, staging / "TwinCAT-Agent.exe")
        shutil.copy2(uninstaller, staging / "TwinCAT-Agent-Uninstall.exe")
        shutil.copy2(icon, staging / "twincat-agent.ico")

        previous_root = target
        if not previous_root.exists():
            previous_root = next(
                (path for path in legacy_install_dirs() if path.exists()), target
            )
        preserved = _preserve_files(previous_root)
        _restore_files(staging, preserved)

        extension_present = bool(embed_xae)
        if not skip_system:
            # Not selecting the option means "do not modify XAE". Remember an
            # older extension separately so the uninstaller can still clean it.
            extension_present = bool(embed_xae or extension_is_installed(shell))
        (staging / "install_options.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "embed_xae": bool(embed_xae),
                    "embed_xae_requested": bool(embed_xae),
                    "extension_installed": extension_present,
                    "tray_enabled": True,
                    "twincat_version": twincat_version,
                    "xae_shell": str(shell) if shell else None,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        if not skip_system and embed_xae:
            notify("正在安装 TwinCAT XAE 扩展（请确认 UAC）…", 48)
            code = run_elevated(
                sys.executable,
                ["--install-extension-native",
                 str(staging / "extension" / "TwinCAT Agent"),
                 str(xae_shell_root(shell)),
                 "--refresh-cache"],
            )
            if code:
                raise RuntimeError(f"管理员扩展安装步骤失败（退出码 {code}）")
            notify("TwinCAT XAE 菜单和面板缓存已刷新。", 55)
        elif not skip_system:
            notify("已选择仅安装 Agent 后端，不修改 XAE…", 48)

        if not skip_system:
            notify("正在停止旧版 Agent 后台…", 64)
            stop_installed_backend(target)
            # The tray owns its executable. Stop it and wait for the file
            # handle to be released before replacing the installation root.
            stop_installed_tray(target)
        else:
            notify("测试模式：不修改正式 Agent 服务…", 64)

        notify("正在完成升级与快捷方式…", 72)
        if target.exists():
            shutil.rmtree(target)
        if not skip_system:
            for legacy_root in legacy_install_dirs():
                if legacy_root.exists() and legacy_root.resolve() != target:
                    shutil.rmtree(legacy_root)
        os.replace(staging, target)

        if not skip_system:
            icon_path = target / "twincat-agent.ico"
            launcher_path = target / "TwinCAT-Agent.exe"
            uninstaller_path = target / "TwinCAT-Agent-Uninstall.exe"
            create_shortcut(
                startup_shortcut(),
                launcher_path,
                "--backend-only",
                icon_path,
                target,
            )
            menu = start_menu_dir()
            create_shortcut(
                menu / "TwinCAT Agent.lnk",
                launcher_path,
                "",
                icon_path,
                target,
            )
            create_shortcut(
                menu / "卸载 TwinCAT Agent.lnk",
                uninstaller_path,
                "",
                icon_path,
                target,
            )
            if create_desktop:
                create_shortcut(
                    desktop_shortcut(),
                    launcher_path,
                    "",
                    icon_path,
                    target,
                )
            elif desktop_shortcut().exists():
                desktop_shortcut().unlink()
            notify("安装完成", 100)
            if launch_after:
                subprocess.Popen(
                    [str(launcher_path)],
                    cwd=target,
                    creationflags=CREATE_NO_WINDOW,
                )
        else:
            manifest = {
                "target": str(target),
                "files": sum(1 for path in target.rglob("*") if path.is_file()),
                "embed_xae": bool(embed_xae),
            }
            (target / ".installer-test.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            notify("测试安装完成", 100)
        return target
    except Exception:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise


class SetupWindow:
    def __init__(self, root: Tk) -> None:
        self.root = root
        self.root.title(APP_TITLE)
        # PyInstaller's default Tk icon is the feather shown in the title bar.
        # Use the same branded ICO as Setup.exe, shortcuts and the launcher.
        self.root.iconbitmap(default=str(resource_path("assets", "twincat-agent.ico")))
        self.root.geometry("650x570")
        self.root.resizable(False, False)
        self.root.configure(bg=BG)
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.status = StringVar(value="准备安装")
        self.twincat_version = detect_twincat_version()
        self.shell = find_xae_shell(twincat_version=self.twincat_version)
        self.embed_allowed = bool(
            self.shell
            and (
                self.twincat_version is None
                or is_supported_twincat(self.twincat_version)
            )
        )
        self.embed = BooleanVar(value=self.embed_allowed)
        self.desktop = BooleanVar(value=True)
        self.launch = BooleanVar(value=True)
        self.embed_xae = self.embed_allowed
        self.create_desktop = True
        self.launch_after = True
        self.logo: PhotoImage | None = None
        self._build()
        self.root.after(80, self._poll)

    def _build(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("TLabel", background=PANEL, font=("Microsoft YaHei UI", 10))
        style.configure(
            "Title.TLabel",
            background=PANEL,
            foreground="#152235",
            font=("Microsoft YaHei UI", 20, "bold"),
        )
        style.configure(
            "Accent.TButton",
            foreground="white",
            background=ACCENT,
            font=("Microsoft YaHei UI", 11, "bold"),
        )
        style.map("Accent.TButton", background=[("active", "#0879c1")])

        panel = ttk.Frame(self.root, style="Panel.TFrame", padding=28)
        panel.pack(fill="both", expand=True, padx=14, pady=14)
        top = ttk.Frame(panel, style="Panel.TFrame")
        top.pack(fill="x")
        logo_path = resource_path("assets", "twincat-agent-logo-256.png")
        self.logo = PhotoImage(file=str(logo_path)).subsample(2, 2)
        ttk.Label(top, image=self.logo).pack(side="left", padx=(0, 22))
        title = ttk.Frame(top, style="Panel.TFrame")
        title.pack(side="left", fill="x", expand=True, pady=(12, 0))
        ttk.Label(
            title, text=f"TwinCAT Agent  ·  v{APP_VERSION}", style="Title.TLabel"
        ).pack(anchor="w")
        ttk.Label(
            title,
            text="TwinCAT XAE 内的 AI 自动化开发助手",
            foreground="#5e6b78",
        ).pack(anchor="w", pady=(5, 0))
        ttk.Label(
            title,
            text="同一安装包支持 Build 4024 / 4026，XAE 嵌入可选",
            foreground="#5e6b78",
        ).pack(anchor="w", pady=(3, 0))

        ttk.Separator(panel).pack(fill="x", pady=22)
        ttk.Label(
            panel,
            text=f"安装位置：{install_dir()}",
            foreground="#5e6b78",
        ).pack(anchor="w")
        if self.shell:
            detected = self.twincat_version or "版本未识别"
            environment_text = (
                f"已检测：TwinCAT {detected}\n"
                f"将嵌入：{self.shell}"
            )
            if self.twincat_version and not is_supported_twincat(self.twincat_version):
                environment_text += "（嵌入仅支持 4024/4026）"
        else:
            environment_text = "未检测到 XAE：可仅安装后端并在浏览器使用"
        ttk.Label(
            panel,
            text=environment_text,
            foreground="#5e6b78",
            wraplength=570,
        ).pack(anchor="w", pady=(6, 0))
        self.embed_check = ttk.Checkbutton(
            panel,
            text="嵌入 TwinCAT XAE（Build 4024 / 4026，需要一次 UAC）",
            variable=self.embed,
        )
        self.embed_check.pack(anchor="w", pady=(16, 4))
        if not self.embed_allowed:
            self.embed_check.configure(state="disabled")
        ttk.Checkbutton(
            panel, text="创建桌面快捷方式", variable=self.desktop
        ).pack(anchor="w", pady=4)
        ttk.Checkbutton(
            panel, text="安装完成后启动 TwinCAT Agent", variable=self.launch
        ).pack(anchor="w")

        self.progress = ttk.Progressbar(panel, maximum=100, mode="determinate")
        self.progress.pack(fill="x", pady=(24, 7))
        ttk.Label(panel, textvariable=self.status, foreground="#5e6b78").pack(
            anchor="w"
        )
        self.button = ttk.Button(
            panel,
            text="立即安装",
            style="Accent.TButton",
            command=self.start,
        )
        self.button.pack(fill="x", ipady=8, pady=(18, 0))

    def start(self) -> None:
        self.button.configure(state="disabled")
        self.status.set("正在检查安装环境…")
        self.embed_xae = bool(self.embed.get())
        self.create_desktop = bool(self.desktop.get())
        self.launch_after = bool(self.launch.get())
        threading.Thread(target=self._worker, daemon=True).start()

    def _notify(self, text: str, progress: int) -> None:
        self.events.put(("progress", (text, progress)))

    def _worker(self) -> None:
        try:
            target = _install(
                self._notify,
                self.create_desktop,
                self.launch_after,
                embed_xae=self.embed_xae,
            )
            self.events.put(("done", target))
        except Exception as exc:
            self.events.put(("error", str(exc)))

    def _poll(self) -> None:
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "progress":
                    text, progress = value
                    self.status.set(text)
                    self.progress["value"] = progress
                elif kind == "done":
                    self.status.set("✓ 安装完成")
                    self.progress["value"] = 100
                    self.button.configure(text="完成", command=self.root.destroy)
                    self.button.configure(state="normal")
                    messagebox.showinfo(
                        APP_TITLE,
                        (
                            "安装完成。\n\n以后双击桌面的 TwinCAT Agent 即可启动"
                            + (" XAE 嵌入面板。" if self.embed_xae else "浏览器界面。")
                        ),
                        parent=self.root,
                    )
                elif kind == "error":
                    self.status.set("安装失败")
                    self.button.configure(state="normal")
                    messagebox.showerror(APP_TITLE, str(value), parent=self.root)
        except queue.Empty:
            pass
        self.root.after(80, self._poll)


def main() -> int:
    if len(sys.argv) in (4, 5) and sys.argv[1] == "--install-extension-native":
        install_extension_native(
            Path(sys.argv[2]),
            Path(sys.argv[3]),
            refresh_cache="--refresh-cache" in sys.argv[4:],
        )
        return 0
    if "--test-install" in sys.argv:
        index = sys.argv.index("--test-install")
        target = Path(sys.argv[index + 1])
        try:
            _install(
                lambda text, value: print(value, text),
                False,
                False,
                target,
                embed_xae="--no-embed" not in sys.argv,
            )
            return 0
        except Exception:
            # The production installer is a windowed executable, so test-mode
            # failures have no console. Persist the traceback beside the test
            # target to keep release smoke checks diagnosable.
            error_log = target.parent / f"{target.name}.error.log"
            error_log.write_text(traceback.format_exc(), encoding="utf-8")
            return 1
    root = Tk()
    SetupWindow(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
