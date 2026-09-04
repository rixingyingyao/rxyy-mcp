# -*- coding: utf-8 -*-
"""hub 拆分第二刀验收冒烟：隔离实例 --daemon 起完整 hub，验证 39222 并入进程内。

手动跑：``python tests/_smoke_inproc_daemon.py``（在 rxyy-mcp 目录下、用 .venv 解释器）。
pytest 不收集（下划线开头）。全程用临时 RXYY_MCP_DATA_DIR + 错开端口，绝不碰生产：
- RXYY_MCP_LIVE_DIR 指到空目录，挡 live_runtime.hand_over（别把冒烟让给常驻区）；
- instance_owner 的 .instance-owner.json 也在隔离 DATA_DIR 里，不抢正主；
- 开始前/结束后都按端口清一遍隔离实例的残留（hub 会拉起自己的 watchdog，
  只杀 hub 的话 watchdog 会把 hub/server.py 全家复活回来——首轮冒烟实测）。

验收点（任一不过整体 FAIL）：
1. hub --daemon 起来后 38999'/38777'/39222' 三端口全通；
2. 39222' 的监听 pid == hub 进程 pid（「并入」的硬证据，不再有独立 server.py）；
3. initialize / tools/list 正常应答；
4. zhi 报到 → 控制台 send_reply → SSE 返回用户回复（提问/绿灯/回复全链路）；
5. zt 上报走进程内管道，get_state 里 agent_status 变 testing；
6. ji 黑板写入落盘到隔离 DATA_DIR 的 board.json（管道直达 Hub 落盘的硬证据）；
7. zhi __timeout_probe__ 短探走 SSE 流全链路，拿到 PROBE_OK。
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]

PORT_HUB = 48999
PORT_GW = 48777
PORT_MCP = 49222
PORT_SHARE = 49080
PORT_WD = 48996
ISOLATED_PORTS = (PORT_WD, PORT_MCP, PORT_HUB, PORT_GW, PORT_SHARE)


def port_pid(port):
    # netstat 表头是本地代码页（GBK）输出，数据行全 ASCII——replace 兜住表头即可
    out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                         timeout=15, creationflags=0x08000000
                         ).stdout.decode("ascii", "replace")
    for line in out.splitlines():
        parts = line.split()
        if (len(parts) >= 5 and parts[0] == "TCP" and parts[3] == "LISTENING"
                and parts[1].endswith(":%d" % port)):
            return int(parts[4])
    return None


def taskkill(pid):
    subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                   capture_output=True, timeout=15, creationflags=0x08000000)


def cleanup_isolated():
    """把占着隔离端口的进程全清掉（watchdog 优先——不先杀它，它会复活别人）。"""
    killed = []
    for port in ISOLATED_PORTS:
        pid = port_pid(port)
        if pid:
            taskkill(pid)
            killed.append("%d(pid=%d)" % (port, pid))
    if killed:
        print("[cleanup] 清掉隔离端口残留：%s" % "、".join(killed))
        time.sleep(1)


def wait_port(port, secs=90):
    import socket
    deadline = time.time() + secs
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return True
        except OSError:
            time.sleep(0.5)
    return False


def mcp_call(payload, timeout=30):
    req = urllib.request.Request(
        "http://127.0.0.1:%d/mcp" % PORT_MCP,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def gw_call(method, args, timeout=15):
    req = urllib.request.Request(
        "http://127.0.0.1:%d/api/%s" % (PORT_GW, method),
        data=json.dumps(args, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def main():
    cleanup_isolated()
    tmp = Path(tempfile.mkdtemp(prefix="rxyy_mcp-smoke-"))
    (tmp / "no-live").mkdir()
    (tmp / "config.json").write_text(json.dumps({
        "port": PORT_HUB, "gateway_port": PORT_GW, "mcp_http_port": PORT_MCP,
        "share_port": PORT_SHARE, "watchdog_port": PORT_WD,
        "share_enabled": False, "push_enabled": False, "audio_enabled": False,
        "history_dir": str(tmp / "history"),
    }, ensure_ascii=False), encoding="utf-8")
    env = dict(os.environ, RXYY_MCP_DATA_DIR=str(tmp),
               RXYY_MCP_LIVE_DIR=str(tmp / "no-live"), PYTHONIOENCODING="utf-8")
    logf = open(tmp / "smoke-hub.log", "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", str(APP_DIR / "hub.py"), "--daemon"],
        cwd=str(APP_DIR), env=env, stdin=subprocess.DEVNULL,
        stdout=logf, stderr=subprocess.STDOUT, creationflags=0x08000000)
    fails = []

    def check(ok, ok_msg, fail_msg):
        if ok:
            print("[ok] %s" % ok_msg)
        else:
            fails.append(fail_msg)
            print("[FAIL] %s" % fail_msg)
        return ok

    try:
        for name, port in (("hub 38999'", PORT_HUB), ("gateway 38777'", PORT_GW),
                           ("mcp 39222'", PORT_MCP)):
            check(wait_port(port), "%s (%d) 已就绪" % (name, port),
                  "%s (%d) 未就绪" % (name, port))
        if fails:
            return 1

        # 「并入」的本质断言：MCP 端点与 hub 会话路由是同一个进程在听。
        # 不比 proc.pid——.venv 的 python.exe 是 launcher 壳，真解释器是它的子进程
        mcp_pid, hub_pid = port_pid(PORT_MCP), port_pid(PORT_HUB)
        check(mcp_pid is not None and mcp_pid == hub_pid,
              "39222'(pid=%s) 与 38999'(pid=%s) 同进程（并入，无独立 server.py）" % (mcp_pid, hub_pid),
              "39222' 监听 pid=%s != 38999' 监听 pid=%s（有独立 server.py 在顶班？）" % (mcp_pid, hub_pid))

        init = mcp_call({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                         "params": {"protocolVersion": "2024-11-05",
                                    "capabilities": {}, "clientInfo": {"name": "smoke"}}})
        check('"serverInfo"' in init and "rxyy-mcp" in init,
              "initialize 应答正常", "initialize 应答异常: %s" % init[:200])

        tl = mcp_call({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        check(all(('"%s"' % t) in tl for t in ("zhi", "zt", "ji")),
              "tools/list 含 zhi/zt/ji", "tools/list 缺工具: %s" % tl[:200])

        # --- zhi 报到 → 控制台回复 → SSE 返回（提问/绿灯/回复全链路）---
        zhi_box = {}

        def do_zhi():
            try:
                zhi_box["raw"] = mcp_call(
                    {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                     "params": {"name": "zhi", "_meta": {"progressToken": 3},
                                "arguments": {"message": "冒烟报到",
                                              "predefined_options": ["开始任务"],
                                              "conversation_id": "smoke001",
                                              "task_name": "冒烟·二刀",
                                              "project_path": str(tmp)}}},
                    timeout=90)
            except Exception as e:
                zhi_box["raw"] = "EXC:%r" % (e,)

        zt = threading.Thread(target=do_zhi, daemon=True)
        zt.start()
        sid = None
        deadline = time.time() + 30
        while time.time() < deadline and sid is None:
            time.sleep(0.5)
            try:
                st = gw_call("get_state", [])
            except Exception:
                continue
            for row in st.get("sessions") or []:
                if row.get("conv_key") == "smoke001" and row.get("pending"):
                    sid = row.get("id")
                    break
        if check(sid is not None, "zhi 提问已到控制台（tab 绿灯）", "30s 内控制台未见 smoke001 的提问"):
            r = gw_call("send_reply", [sid, "开始任务·冒烟回复", [], [], False])
            check(bool(r.get("ok", True)), "控制台 send_reply 已接受",
                  "send_reply 被拒: %s" % r)
            zt.join(30)
            raw = zhi_box.get("raw") or ""
            check("开始任务·冒烟回复" in raw,
                  "zhi SSE 收到用户回复（全链路闭环）",
                  "zhi 未收到回复: %s" % raw[:300])

        # --- zt 上报 → hub 侧状态可见 ---
        ztr = mcp_call({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                        "params": {"name": "zt", "arguments": {
                            "status": "testing", "activity": "第二刀冒烟",
                            "conversation_id": "smoke001",
                            "task_name": "冒烟·二刀"}}})
        seen = False
        if "状态已更新" in ztr:
            deadline = time.time() + 10
            while time.time() < deadline and not seen:
                time.sleep(0.5)
                try:
                    st = gw_call("get_state", [])
                except Exception:
                    continue
                seen = any(r.get("conv_key") == "smoke001"
                           and r.get("agent_status") == "testing"
                           for r in st.get("sessions") or [])
        check(seen, "zt 状态经管道送达 hub（get_state 见 testing）",
              "zt 状态未在 get_state 出现: %s" % ztr[:200])

        # --- ji 黑板 → 落盘 ---
        mcp_call({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                  "params": {"name": "ji", "arguments": {
                      "action": "黑板", "category": "部署",
                      "content": "第二刀冒烟写入",
                      "conversation_id": "smoke001",
                      "task_name": "冒烟·二刀",
                      "project_path": str(tmp)}}})
        board = ""
        deadline = time.time() + 10
        while time.time() < deadline and "第二刀冒烟写入" not in board:
            time.sleep(0.5)
            try:
                board = (tmp / "board.json").read_text(encoding="utf-8")
            except OSError:
                pass
        check("第二刀冒烟写入" in board,
              "ji 黑板经管道直达 Hub 并落盘 board.json", "board.json 未见冒烟条目")

        # --- SSE 超时探测短版 ---
        probe = mcp_call({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                          "params": {"name": "zhi", "_meta": {"progressToken": 7},
                                     "arguments": {"message": "6",
                                                   "task_name": "__timeout_probe__"}}},
                         timeout=60)
        check("PROBE_OK" in probe,
              "zhi SSE 流全链路（__timeout_probe__ 短探 6s）PROBE_OK",
              "SSE 探测未返回 PROBE_OK: %s" % probe[:200])
    finally:
        taskkill(proc.pid)
        logf.close()
        cleanup_isolated()
    print()
    if fails:
        print("SMOKE FAIL（%d 项）：%s" % (len(fails), "；".join(fails)))
        print("隔离实例日志与机器态保留在 %s 供排查" % tmp)
        return 1
    print("SMOKE PASS —— 全部验收点通过。隔离目录：%s" % tmp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
