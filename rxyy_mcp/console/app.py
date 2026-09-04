"""rxyy mcp 桌面壳 —— pywebview 入口。不含账号池 / 公司内网模块。"""
from __future__ import annotations

import functools
import http.server
import socket
import sys
import threading
from pathlib import Path

if getattr(sys, "frozen", False):
    _MEI = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    _CONSOLE_DIR = _MEI / "console"
    _PROJECT_ROOT = _MEI
else:
    _CONSOLE_DIR = Path(__file__).resolve().parent
    _PROJECT_ROOT = _CONSOLE_DIR.parent
for _p in (str(_CONSOLE_DIR), str(_PROJECT_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

WEB_DIR = _CONSOLE_DIR / "web"
WEB_PORT = 38010


class _ExclusiveWebServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def _focus_existing_window() -> None:
    import ctypes
    from ctypes import wintypes

    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        hwnd = user32.FindWindowW(None, "rxyy mcp")
        if not hwnd:
            return
        user32.ShowWindow(hwnd, 9)
        user32.ShowWindow(hwnd, 5)
        HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
        SWP_NOMOVE, SWP_NOSIZE, SWP_SHOWWINDOW = 0x0002, 0x0001, 0x0040
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
        user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
        fg = user32.GetForegroundWindow()
        fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        cur_tid = kernel32.GetCurrentThreadId()
        target_tid = user32.GetWindowThreadProcessId(hwnd, None)
        if fg_tid and fg_tid != cur_tid:
            user32.AttachThreadInput(cur_tid, fg_tid, True)
        if target_tid and target_tid != cur_tid:
            user32.AttachThreadInput(cur_tid, target_tid, True)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        if target_tid and target_tid != cur_tid:
            user32.AttachThreadInput(cur_tid, target_tid, False)
        if fg_tid and fg_tid != cur_tid:
            user32.AttachThreadInput(cur_tid, fg_tid, False)
    except Exception:
        pass


def _start_web_server() -> bool:
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(WEB_DIR)
    )
    try:
        httpd = _ExclusiveWebServer(("127.0.0.1", WEB_PORT), handler)
    except OSError:
        return False
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, name="rxyy-web", daemon=True).start()
    return True


def _ensure_hub() -> None:
    import json
    import subprocess

    hub_py = _PROJECT_ROOT / "hub.py"
    if not hub_py.is_file():
        return
    if str(_PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(_PROJECT_ROOT))
    try:
        from datadir import CONFIG_PATH as cfg_path
    except Exception:
        cfg_path = _PROJECT_ROOT / "config.json"
    try:
        from instance_owner import should_stand_down
        stand_down, _why = should_stand_down(_PROJECT_ROOT)
    except Exception:
        stand_down = False
    if stand_down:
        return
    try:
        cfg = json.loads(Path(cfg_path).read_text(encoding="utf-8"))
    except Exception:
        cfg = {}
    port = int(cfg.get("port", 38999) or 38999)

    def _port_open(p: int) -> bool:
        try:
            s = socket.create_connection(("127.0.0.1", p), timeout=1)
            s.close()
            return True
        except OSError:
            return False

    try:
        from frozen_boot import hidden_popen_kwargs, script_dir, spawn_argv
    except Exception:
        spawn_argv = None

    python = Path(sys.executable)
    pythonw = python.with_name("pythonw.exe")
    exe = str(pythonw if pythonw.exists() else python)

    def _spawn(script: str, *args: str) -> None:
        if spawn_argv is not None:
            cmd = spawn_argv(_PROJECT_ROOT / script, *args)
            subprocess.Popen(
                cmd, cwd=script_dir(cmd, _PROJECT_ROOT),
                **hidden_popen_kwargs())
            return
        flags = 0x08000000 | 0x00000200
        subprocess.Popen(
            [exe, str(_PROJECT_ROOT / script), *args],
            cwd=str(_PROJECT_ROOT), creationflags=flags,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True)

    try:
        if not _port_open(port):
            _spawn("hub.py", "--daemon")
        _spawn("watchdog.py")
    except Exception:
        pass


def _start_remote_gateway(api) -> None:
    try:
        from api.webgw import DEFAULT_PORT, ensure_token, start_web_gateway
        from api.workflow_db import wfdb

        kv = wfdb()
        if (kv.kv_get("console_remote_enabled") or "1").strip().lower() in (
            "0", "false", "off",
        ):
            return
        port = int((kv.kv_get("console_remote_port") or "").strip() or DEFAULT_PORT)
        token = ensure_token(kv)
        start_web_gateway(api, WEB_DIR, token, port=port, path_prefix="")
    except Exception:
        pass


def main() -> int:
    import webview
    from api.bridge import Api

    _ensure_hub()
    if not _start_web_server():
        _focus_existing_window()
        return 0
    api = Api()
    window = webview.create_window(
        title="rxyy mcp",
        url="http://127.0.0.1:%d/index.html" % WEB_PORT,
        js_api=api,
        width=1200,
        height=780,
        min_size=(980, 640),
        background_color="#f4f5f9",
    )
    api.bind(window)
    _start_remote_gateway(api)
    webview.start(debug=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
