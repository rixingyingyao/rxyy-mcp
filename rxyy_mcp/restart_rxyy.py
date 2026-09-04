"""一键重启 rxyy tools 窗口（配合 hub 的 restart_all）。

流程：关掉现有 rxyy tools 窗口 → 等新 hub 的网关 /api/ping 恢复 → 重开 rxyy tools。
窗口消失又回来，就是用户肉眼可见的「整套已恢复」信号（单独重启 hub 用户看不到）。

独立进程运行：hub 自毁不影响本脚本；rxyy tools 不在跑也没关系（照样拉起）。
"""
from __future__ import annotations

import ctypes
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent          # …/rxyy-mcp


def _rxyy_app() -> Path:
    """console/app.py 在哪。常驻区那份不在 rxyy tools 的目录树里，按 APP_DIR 的
    上一级推会推到 %LOCALAPPDATA%\\rxyy-mcp\\live\\，那儿没有 console\\
    （见 live_runtime.console_root）。"""
    sys.path.insert(0, str(APP_DIR))
    try:
        import live_runtime
        recorded = live_runtime.console_root(APP_DIR)
    except Exception:  # noqa: BLE001  老包里没有这个模块
        recorded = None
    if recorded and (recorded / "console" / "app.py").is_file():
        return recorded / "console" / "app.py"
    return APP_DIR / "console" / "app.py"


RXYY_APP = _rxyy_app()


def _kill_rxyy_window() -> int:
    """按窗口标题精确定位 rxyy tools 进程并结束。只认标题，绝不误伤
    hub/server/watchdog 这些同为 pythonw 的兄弟进程。"""
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    killed = 0
    for _ in range(4):  # 理论上单例，多找几轮兜底
        hwnd = user32.FindWindowW(None, "rxyy tools")
        if not hwnd:
            break
        pid = ctypes.c_ulong(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            break
        h = kernel32.OpenProcess(0x0001, False, pid.value)  # PROCESS_TERMINATE
        if h:
            kernel32.TerminateProcess(h, 0)
            kernel32.CloseHandle(h)
            killed += 1
        time.sleep(0.5)
    return killed


def _ping_ok(port: int) -> bool:
    req = urllib.request.Request(
        "http://127.0.0.1:%d/api/ping" % port, data=b"[]",
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return bool(json.loads(resp.read().decode("utf-8")).get("ok"))
    except Exception:
        return False


def _wait_gateway(port: int) -> bool:
    """等【新】hub 的网关上线。先等旧网关死透（旧 hub 自毁约需 4s，期间 ping
    仍会通，直接等上线会误认垂死的旧实例），再等新实例活过来。
    机器高负载时 python 冷启动实测要 2 分钟，上线等待给足 240s。"""
    down_deadline = time.time() + 20
    while time.time() < down_deadline:
        if not _ping_ok(port):
            break
        time.sleep(0.8)
    up_deadline = time.time() + 240
    while time.time() < up_deadline:
        if _ping_ok(port):
            return True
        time.sleep(1.5)
    return False


def _spawn_rxyy() -> None:
    flags = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    if getattr(sys, "frozen", False):
        # 打包版：rxyy tools 本体就是这个 exe，直接重开（不带 --run = 正常 UI）
        exe = sys.executable
        cmd, cwd = [exe], str(Path(exe).parent)
    else:
        python = Path(sys.executable)
        pythonw = python.with_name("pythonw.exe")
        exe = str(pythonw if pythonw.exists() else python)
        cmd, cwd = [exe, str(RXYY_APP)], str(RXYY_APP.parent.parent)
    subprocess.Popen(
        cmd, cwd=cwd,
        creationflags=flags, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)


def main() -> int:
    port = 38777
    if "--gateway-port" in sys.argv:
        try:
            port = int(sys.argv[sys.argv.index("--gateway-port") + 1])
        except (ValueError, IndexError):
            pass
    _kill_rxyy_window()
    time.sleep(1.0)          # 给旧 hub 自毁/新 hub 接力留一拍，避免探到旧网关
    _wait_gateway(port)      # 失败也照样开窗口：app.py 自己会兜底拉 hub
    if RXYY_APP.is_file() or getattr(sys, "frozen", False):
        _spawn_rxyy()
    return 0


if __name__ == "__main__":
    sys.exit(main())
