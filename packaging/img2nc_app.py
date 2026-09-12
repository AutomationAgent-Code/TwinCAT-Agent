"""img2nc 桌面启动器 —— 打包成单文件 exe,双击即用(系统托盘版)。

双击后:启动本地 img2nc Web 服务 + 打开浏览器界面 + 在系统托盘放一个图标。
右键托盘图标可"打开界面 / 退出";退出前服务一直后台运行。
无托盘环境时自动退回一个 Tkinter 小窗口。

打包见 packaging/build_exe.py。
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import traceback
import urllib.request
import webbrowser
from http.server import ThreadingHTTPServer

FIXED_PORT = 8799


def _log_path() -> str:
    base = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else __file__)
    return os.path.join(base, "img2nc_error.log")


def _is_img2nc(port: int) -> bool:
    """该端口上是否已有 img2nc 实例在跑。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=0.6) as r:
            return r.read().strip() == b"img2nc"
    except Exception:
        return False


def _find_port(start: int = FIXED_PORT) -> int:
    for p in range(start, start + 60):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return start


def _tray_image(size: int = 256):
    """生成托盘图标(紫底 + 奶油色轮廓环),返回 PIL Image。"""
    from PIL import Image, ImageDraw
    S = size * 4
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.18), fill=(42, 18, 48))
    cx, cy = S / 2, S / 2
    ow, oh, iw, ih = S * 0.34, S * 0.40, S * 0.17, S * 0.22
    lw = int(S * 0.06)
    cream = (233, 220, 201)
    d.ellipse([cx - ow, cy - oh, cx + ow, cy + oh], outline=cream, width=lw)
    d.ellipse([cx - iw, cy - ih, cx + iw, cy + ih], outline=cream, width=lw)
    for fy in (0.40, 0.50, 0.60):
        y = cy - oh + oh * 2 * fy
        d.line([cx - iw * 0.1, y, cx + ow * 0.72, y], fill=cream, width=int(S * 0.03))
    return img.resize((size, size), Image.LANCZOS)


def _run():
    # 冻结后 tc_template 已随包打入;源码运行时补一下路径
    try:
        from tc_template.img2nc_ui import Handler
    except ImportError:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from tc_template.img2nc_ui import Handler

    # 单例:已有实例在跑就直接开浏览器,不再另起服务
    if _is_img2nc(FIXED_PORT):
        webbrowser.open(f"http://127.0.0.1:{FIXED_PORT}/")
        return

    port = _find_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.allow_reuse_address = True
    port = srv.server_address[1]
    url = f"http://127.0.0.1:{port}/"
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    # 首选:系统托盘图标
    try:
        import pystray
        from pystray import Menu, MenuItem

        def on_open(icon, item):
            webbrowser.open(url)

        def on_quit(icon, item):
            try:
                srv.shutdown()
            except Exception:
                pass
            icon.visible = False
            icon.stop()

        def setup(icon):
            icon.visible = True
            webbrowser.open(url)
            try:
                icon.notify(f"服务运行中: {url}\n右键托盘图标可打开界面或退出", "img2nc 已启动")
            except Exception:
                pass

        menu = Menu(
            MenuItem("打开界面", on_open, default=True),
            MenuItem("退出", on_quit),
        )
        icon = pystray.Icon("img2nc", _tray_image(), "img2nc — 图片转 TwinCAT NCI G 代码", menu)
        icon.run(setup=setup)   # 阻塞直到"退出"
        return
    except Exception:
        pass  # 无托盘环境 -> 退回小窗口

    _run_window(srv, url)


def _run_window(srv, url):
    """兜底:Tkinter 小窗口。"""
    try:
        import tkinter as tk
        from tkinter import font as tkfont

        root = tk.Tk()
        root.title("img2nc — 图片转 TwinCAT NCI G 代码")
        root.geometry("420x200")
        root.configure(bg="#1c1626")
        root.resizable(False, False)
        big = tkfont.Font(family="Segoe UI", size=13, weight="bold")
        mid = tkfont.Font(family="Segoe UI", size=10)

        tk.Label(root, text="img2nc 已启动 ✓", fg="#c58be0", bg="#1c1626", font=big).pack(pady=(20, 4))
        tk.Label(root, text="界面已在浏览器打开。若没弹出,点下方按钮。",
                 fg="#a596b5", bg="#1c1626", font=mid).pack(pady=(0, 10))
        btns = tk.Frame(root, bg="#1c1626")
        btns.pack()

        def do_quit():
            try:
                srv.shutdown()
            except Exception:
                pass
            root.destroy()

        tk.Button(btns, text="🌐 打开界面", command=lambda: webbrowser.open(url), font=mid,
                  bg="#c58be0", fg="#1a0f24", relief="flat", padx=16, pady=6).pack(side="left", padx=8)
        tk.Button(btns, text="✕ 退出", command=do_quit, font=mid,
                  bg="#2a2033", fg="#ece6f0", relief="flat", padx=16, pady=6).pack(side="left", padx=8)
        tk.Label(root, text="⚠ 关闭本窗口会停止服务", fg="#e6b25e", bg="#1c1626",
                 font=tkfont.Font(family="Segoe UI", size=9)).pack(side="bottom", pady=10)
        root.protocol("WM_DELETE_WINDOW", do_quit)
        root.mainloop()
    except Exception:
        print(f"[img2nc] 服务运行中: {url}  (Ctrl+C 退出)")
        threading.Event().wait()


def main():
    try:
        _run()
    except Exception:
        with open(_log_path(), "w", encoding="utf-8") as f:
            f.write(traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
