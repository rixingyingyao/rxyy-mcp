"""环境自检：exe 首启 / 换机时检查依赖是否齐备，缺了给出可执行的修复。

设计红线（务必保持）：
- 所有 `env_selfcheck` 检测**只读**，绝不改机器；
- 会落盘的动作（写/删 mcp.json 接入条目）做成**幂等**且「已正确在位就跳过、不重写」，
  所以在本机（都装好了）点修复也不会重写 mcp.json、不会惊动正在运行的 Cursor；
  只有全新电脑（缺条目）才真正写入。
- 供离线测试：`_path` 参数可指向临时 mcp.json，不碰真文件。
"""
from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path
from typing import Any

MCP_PORT = 39222            # rxyy-mcp MCP 守护进程（Cursor 连这个）
HUB_PORT = 38999            # rxyy-mcp hub 裸 TCP
MCP_ENTRY_KEY = "rxyy-mcp"
MCP_ENTRY_URL = "http://127.0.0.1:%d/mcp" % MCP_PORT

_HOME = Path(os.path.expanduser("~"))
_CURSOR_DIR = _HOME / ".cursor"
_CURSOR_MCP_JSON = _CURSOR_DIR / "mcp.json"
_CURSOR_HOOKS_JSON = _CURSOR_DIR / "hooks.json"
_CURSOR_HOOKS_DIR = _CURSOR_DIR / "hooks"


def _port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.8) -> bool:
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
        return True
    except OSError:
        return False


def _read_json(p: Path) -> Any:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _has_webview2() -> bool:
    """WebView2 Runtime 是否已装（pywebview EdgeChromium 后端渲染 UI 必需）。"""
    if os.name != "nt":
        return False
    try:
        import winreg
    except Exception:  # noqa: BLE001
        return False
    guid = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"  # Evergreen WebView2 Runtime
    for hive, sub in (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\%s" % guid),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\EdgeUpdate\Clients\%s" % guid),
    ):
        try:
            with winreg.OpenKey(hive, sub) as k:
                v, _ = winreg.QueryValueEx(k, "pv")
                if v and v != "0.0.0.0":
                    return True
        except OSError:
            continue
    return False


def _chromium_present() -> tuple[bool, str]:
    """买号 / 开 Pro 的内嵌浏览器要用 Playwright chromium：先看打包内置，再看本机缓存。"""
    try:
        if getattr(sys, "frozen", False):
            mei = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
            d = mei / "ms-playwright"
            if d.is_dir() and any(d.glob("chromium-*")):
                return True, str(d)
    except Exception:  # noqa: BLE001
        pass
    home = Path(os.getenv("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "ms-playwright"
    if home.is_dir() and any(home.glob("chromium-*")):
        return True, str(home)
    return False, ""


def _hooks_installed() -> tuple[bool, str]:
    hj = _read_json(_CURSOR_HOOKS_JSON)
    if isinstance(hj, dict) and hj.get("hooks"):
        return True, str(_CURSOR_HOOKS_JSON)
    if _CURSOR_HOOKS_DIR.is_dir() and any(_CURSOR_HOOKS_DIR.glob("*.cjs")):
        return True, str(_CURSOR_HOOKS_DIR)
    return False, ""


class EnvCheckApi:
    """挂到 bridge.Api：前端 window.pywebview.api.env_* 调用。"""

    def env_selfcheck(self) -> dict[str, Any]:
        """只读体检：逐项返回 {key,label,ok,detail,fix}。fix 非空=前端可给「修复」按钮。"""
        checks: list[dict[str, Any]] = []

        def add(key: str, label: str, ok: bool, detail: str = "", fix: str = "") -> None:
            checks.append({"key": key, "label": label, "ok": bool(ok),
                           "detail": detail, "fix": fix})

        # 1) Cursor 安装位置
        cursor_root = None
        try:
            from src.cursor import seamless
            r = seamless.cursor_root()
            cursor_root = str(r) if r else None
        except Exception:  # noqa: BLE001
            cursor_root = None
        add("cursor", "Cursor 已安装", bool(cursor_root),
            cursor_root or "未找到 Cursor 安装目录（先装 Cursor 再启动）")

        # 2) mcp.json 接入条目
        mj = _read_json(_CURSOR_MCP_JSON)
        entry = (mj.get("mcpServers") or {}).get(MCP_ENTRY_KEY) if isinstance(mj, dict) else None
        entry_ok = bool(isinstance(entry, dict) and entry.get("url"))
        add("mcp_entry", "已接入rxyy-mcp（mcp.json）", entry_ok,
            (entry.get("url") if entry_ok else "缺接入条目：%s" % _CURSOR_MCP_JSON),
            "" if entry_ok else "env_install_mcp")

        # 3) rxyy-mcp 网关在跑
        up = _port_open(MCP_PORT) or _port_open(HUB_PORT)
        add("hub", "rxyy-mcp 正在运行", up,
            "MCP %d / hub %d %s" % (MCP_PORT, HUB_PORT, "可达" if up else "均不可达（启动本 exe 会自动拉起）"))

        # 4) Cursor 钩子（agentboard 文件占用面板 / 停车）
        hk_ok, hk_det = _hooks_installed()
        add("hooks", "Cursor 钩子已装", hk_ok, hk_det or "未装（agentboard/停车用；非必需）")

        # 5) Playwright Chromium
        c_ok, c_det = _chromium_present()
        add("chromium", "Playwright Chromium", c_ok, c_det or "缺（买号 / 开 Pro 的内嵌浏览器要用）")

        # 6) WebView2 Runtime
        wv = _has_webview2()
        add("webview2", "WebView2 Runtime", wv, "已装" if wv else "缺（rxyy tools 界面渲染必需）")

        # 7) 百炼模型 Key
        try:
            from api.bailian import runtime_cfg
            has_key = bool((runtime_cfg() or {}).get("api_key"))
        except Exception:  # noqa: BLE001
            has_key = False
        add("bailian", "百炼模型 Key", has_key,
            "已配置" if has_key else "未配置（生成日报/周报要用；设置页填或用内置默认）")

        # 8) OA 账号
        try:
            from api.workflow_db import wfdb
            w = wfdb()
            has_oa = bool(w.kv_get("oa_user_account", "")) and bool(w.kv_get("oa_password", ""))
        except Exception:  # noqa: BLE001
            has_oa = False
        add("oa", "OA 账号", has_oa, "已配置" if has_oa else "未配置（写日报/加班到 OA 要用）")

        # 9) OA 网络出口（只读缓存，不在体检里发包拖慢界面）
        try:
            from src.config import OA_API_BASE
            from src.oa import egress
            seen = egress.peek(OA_API_BASE)
        except Exception:  # noqa: BLE001
            seen = None
        if seen is None:
            add("oa_net", "OA 网络出口", True, "未探测（开着加速器打不开 OA 时点「网络自检」）")
        elif seen.get("mode") == "bound":
            add("oa_net", "OA 网络出口", True,
                "默认出口被加速器劫持，已自动改走物理网卡 %s" % seen.get("source", ""))
        elif seen.get("mode") == "direct":
            add("oa_net", "OA 网络出口", True, "默认出口直连正常")
        else:
            add("oa_net", "OA 网络出口", False, seen.get("detail", "不可达"), "oa_netcheck")

        missing = [c["key"] for c in checks if not c["ok"]]
        return {"ok": True, "checks": checks, "all_ok": not missing, "missing": missing}

    def env_install_mcp(self, _path: str = "") -> dict[str, Any]:
        """幂等接入：往 mcp.json 写rxyy-mcp 条目；已正确在位就**跳过不重写**
        （所以本机点它不会惊动正在运行的 Cursor），全新电脑才真正落盘。"""
        p = Path(_path) if _path else _CURSOR_MCP_JSON
        data = _read_json(p)
        if not isinstance(data, dict):
            data = {}
        servers = data.get("mcpServers")
        if not isinstance(servers, dict):
            servers = {}
            data["mcpServers"] = servers
        cur = servers.get(MCP_ENTRY_KEY)
        if isinstance(cur, dict) and cur.get("url") == MCP_ENTRY_URL:
            return {"ok": True, "changed": False, "message": "已接入，跳过（未改动 mcp.json）",
                    "path": str(p)}
        servers[MCP_ENTRY_KEY] = {"url": MCP_ENTRY_URL}
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, p)
        except OSError as exc:
            return {"ok": False, "error": "写 mcp.json 失败: %r" % exc, "path": str(p)}
        return {"ok": True, "changed": True, "message": "已写入rxyy-mcp 接入条目（重启 Cursor 生效）",
                "path": str(p)}

    def env_uninstall_mcp(self, _path: str = "") -> dict[str, Any]:
        """卸载：从 mcp.json 移除rxyy-mcp 条目（其它 server 不动），干净无残留。"""
        p = Path(_path) if _path else _CURSOR_MCP_JSON
        data = _read_json(p)
        if not isinstance(data, dict):
            return {"ok": True, "changed": False, "message": "无 mcp.json，无需卸载"}
        servers = data.get("mcpServers")
        if not isinstance(servers, dict) or MCP_ENTRY_KEY not in servers:
            return {"ok": True, "changed": False, "message": "无rxyy-mcp 条目，无需卸载"}
        servers.pop(MCP_ENTRY_KEY, None)
        try:
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, p)
        except OSError as exc:
            return {"ok": False, "error": "写 mcp.json 失败: %r" % exc, "path": str(p)}
        return {"ok": True, "changed": True, "message": "已移除rxyy-mcp 接入条目", "path": str(p)}
