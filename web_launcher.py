#!/usr/bin/env python3
"""UCloud Cleaner Web 启动器（带 macOS 菜单栏托盘）
打包为 .app 后双击自动启动 Flask 服务并打开系统浏览器。
"""
import os
import subprocess
import sys
import threading
import time

# 自动安装缺失依赖
REQUIRED = ["flask", "pystray", "pillow"]

def _ensure_deps():
    for pkg in REQUIRED:
        try:
            __import__(pkg.replace("pillow", "PIL").replace("pystray", "pystray"))
        except ImportError:
            subprocess.check_call([sys.executable, "-m", "pip", "install", pkg, "-q"])

_ensure_deps()

import pystray
from PIL import Image, ImageDraw
from web.app import create_app, _load_config


# ---------- 图标 ----------
def _create_icon():
    """加载项目根目录的 UCloud_Cleaner.png 作为托盘图标"""
    import sys
    # 打包后 PyInstaller 会把资源放到 _MEIPASS；开发模式直接取脚本同级目录
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    icon_path = os.path.join(base, 'UCloud_Cleaner.png')
    return Image.open(icon_path).convert('RGBA')


# ---------- 服务管理 ----------
class UCloudCleanerApp:
    def __init__(self):
        self.port = None
        self.url = None
        self.tray = None
        self._stop_event = threading.Event()

    def start(self):
        app, _ = create_app(testing=False)
        cfg = _load_config()
        self.port = int(cfg.get("port", 8080))
        self.url = f"http://127.0.0.1:{self.port}"

        # 启动 Flask
        def _serve():
            app.run(host="127.0.0.1", port=self.port, threaded=True, use_reloader=False)

        threading.Thread(target=_serve, daemon=True).start()

        # 等待服务就绪
        for _ in range(100):
            import socket
            try:
                s = socket.create_connection(("127.0.0.1", self.port), timeout=0.05)
                s.close()
                break
            except Exception:
                time.sleep(0.05)

        # 打开浏览器
        self._open_browser()

        # 创建托盘
        self.tray = pystray.Icon(
            "UCloudCleaner",
            _create_icon(),
            "UCloud Cleaner Web",
            menu=pystray.Menu(
                pystray.MenuItem("打开UCloud Cleaner", self._on_open_browser),
                pystray.MenuItem("停止服务并退出", self._on_exit),
            ),
        )
        self.tray.run()

    def _open_browser(self):
        if sys.platform == "darwin":
            subprocess.run(["open", self.url])
        elif sys.platform.startswith("win"):
            os.startfile(self.url)  # type: ignore[attr-defined]
        else:
            subprocess.run(["xdg-open", self.url])

    def _on_open_browser(self):
        self._open_browser()

    def _on_exit(self):
        self.tray.stop()
        sys.exit(0)


def main():
    UCloudCleanerApp().start()


if __name__ == "__main__":
    main()
