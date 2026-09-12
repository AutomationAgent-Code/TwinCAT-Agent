"""一键把 img2nc UI 打包成单文件 exe(打开即用)。

前置(用 Python 3.12,PyInstaller 支持最稳):
    python -m pip install pyinstaller opencv-python-headless pillow numpy pystray

打包:
    python packaging/build_exe.py            # 输出 dist 到 <repo>/dist/img2nc.exe
    python packaging/build_exe.py G:/out     # 指定输出目录

产物: 单文件 img2nc.exe(约 72MB,含 cv2),双击启动本地服务 + 弹小窗口。
"""
import os
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    distpath = sys.argv[1] if len(sys.argv) > 1 else os.path.join(REPO, "dist")
    workpath = os.path.join(tempfile.gettempdir(), "img2nc_pybuild")
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile", "--noconsole", "--name", "img2nc",
        "--collect-all", "cv2",
        "--collect-all", "pystray",
        "--paths", REPO,
        "--distpath", distpath,
        "--workpath", workpath,
        "--specpath", os.path.join(REPO, "packaging"),
        "--noconfirm",
    ]
    icon = os.path.join(REPO, "packaging", "img2nc.ico")
    if os.path.exists(icon):
        cmd += ["--icon", icon]
    cmd.append(os.path.join(REPO, "packaging", "img2nc_app.py"))
    print("[build_exe]", " ".join(cmd))
    rc = subprocess.call(cmd)
    if rc == 0:
        print(f"\n[build_exe] 完成 -> {os.path.join(distpath, 'img2nc.exe')}")
    sys.exit(rc)


if __name__ == "__main__":
    main()
