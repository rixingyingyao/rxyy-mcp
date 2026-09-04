"""把任务面板嵌进 Codex 左侧栏（走 Dream Skin 已经打开的 CDP）。

探索结论：Codex 桌面是 Chromium，Dream Skin 注入器会把调试口留在 127.0.0.1。
左侧栏稳定选择器是 ``aside.app-shell-left-panel``（Dream Skin selectors.json 的
left-panel）。能找到就嵌进栏底；找不到就浮在左边，不改 Codex 自己的 React 树。

整页不再自绘四列：iframe 本机 webgw 的 ``?page=board&embed=1``。iframe 挂在
``app://`` 里时 SameSite=Strict cookie 存不住，所以 webgw 对 embed 不 302，
令牌走 URL + ``X-Console-Token``。
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from api.codexcfg_api import DREAMSKIN_STATE, _NO_WINDOW, _dreamskin_runtime, _read_json

SIDEBAR_SEL = "aside.app-shell-left-panel"
DOCK_ID = "rxyy-board-dock"
CDP_SCRIPT = Path(__file__).with_name("codex_sidebar_cdp.mjs")
BOARD_URL = "http://127.0.0.1:39090/"
AUTO_INJECT_SEC = 20

_auto_lock = threading.Lock()
_auto_thread: threading.Thread | None = None
_last_injected_browser = ""

PROBE_JS = """(() => {
  const sel = %s;
  const sidebar = document.querySelector(sel);
  const dock = document.getElementById(%s);
  return {
    href: location.href,
    title: document.title,
    sidebar: Boolean(sidebar),
    sidebarW: sidebar ? Math.round(sidebar.getBoundingClientRect().width) : 0,
    dock: Boolean(dock),
  };
})()""" % (json.dumps(SIDEBAR_SEL), json.dumps(DOCK_ID))

REMOVE_JS = """(() => {
  const el = document.getElementById(%s);
  const layer = document.getElementById(%s);
  if (el) el.remove();
  if (layer) layer.remove();
  return { removed: Boolean(el || layer) };
})()""" % (json.dumps(DOCK_ID), json.dumps(DOCK_ID + "-layer"))


def _node_exe() -> str:
    state = _read_json(DREAMSKIN_STATE / "state.json", {}) or {}
    for raw in (state.get("nodePath"),
                os.path.expandvars(r"%LOCALAPPDATA%\Programs\CodexDreamSkin"
                                   r"\payload\runtime\node\node.exe")):
        path = Path(str(raw or ""))
        if path.is_file():
            return str(path)
    return "node"


def _listening_debug_port() -> int:
    import urllib.error
    import urllib.request
    for port in (9336, 9335, 9337):
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:{}/json/version".format(port), timeout=0.4):
                return port
        except (OSError, ValueError, urllib.error.URLError):
            continue
    return 0


def _cdp_port() -> tuple[int, str]:
    runtime = _dreamskin_runtime()
    if runtime.get("active"):
        return int(runtime["port"]), ""
    state = _read_json(DREAMSKIN_STATE / "state.json", {}) or {}
    try:
        port = int(state.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    reason = str(runtime.get("reason") or "Dream Skin 没在跑")
    if port >= 1024:
        return port, reason
    scanned = _listening_debug_port()
    if scanned:
        return scanned, ""
    return 0, reason


def snapshot_cards(limit: int = 200) -> list[dict[str, str]]:
    from api.board.service import BoardService
    cards = BoardService().list_cards({}).get("cards") or []
    slim = []
    for card in cards:
        if card.get("archived") or card.get("status") == "done":
            continue
        slim.append({
            "id": str(card.get("id") or ""),
            "title": str(card.get("title") or "")[:80],
            "status": str(card.get("status") or ""),
            "priority": str(card.get("priority") or ""),
            "project": str(card.get("project") or "")[:40],
        })
        if len(slim) >= limit:
            break
    return slim


def embed_url() -> str:
    """本机远程门上的任务面板。embed=1 让 webgw 不要 302 掉令牌。"""
    from api.webgw import DEFAULT_PORT, ensure_token
    from api.workflow_db import wfdb
    kv = wfdb()
    token = ensure_token(kv)
    try:
        port = int((kv.kv_get("console_remote_port") or "").strip() or DEFAULT_PORT)
    except (TypeError, ValueError):
        port = DEFAULT_PORT
    return "http://127.0.0.1:%d/?t=%s&page=board&embed=1" % (port, token)


def inject_js(cards: list[dict[str, str]], board_url: str = "") -> str:
    counts: dict[str, int] = {}
    for card in cards:
        status = str(card.get("status") or "")
        counts[status] = counts.get(status, 0) + 1
    payload = json.dumps(
        {"sel": SIDEBAR_SEL, "id": DOCK_ID, "counts": counts,
         "total": len(cards), "url": board_url or embed_url()},
        ensure_ascii=False)
    return """(() => {
  const data = __PAYLOAD__;
  const sidebar = document.querySelector(data.sel);
  const pluginBtn = sidebar && [...sidebar.querySelectorAll('button,a')].find((el) => {
    const own = [...el.childNodes].filter((n) => n.nodeType === 3)
      .map((n) => n.textContent.trim()).join('');
    return own === '插件' || (el.textContent || '').trim() === '插件';
  });
  const column = sidebar && (
    sidebar.querySelector(':scope > .max-w-full') || sidebar.firstElementChild);
  let host = document.getElementById(data.id);
  if (!host) {
    host = document.createElement('div');
    host.id = data.id;
  }
  if (pluginBtn && pluginBtn.parentElement) {
    pluginBtn.parentElement.insertBefore(host, pluginBtn.nextSibling);
  } else if (column && column !== sidebar) {
    column.appendChild(host);
  } else if (sidebar) {
    sidebar.appendChild(host);
  } else {
    document.documentElement.appendChild(host);
  }
  const inRail = Boolean(sidebar && sidebar.contains(host) && host.parentElement !== sidebar);
  const root = host.shadowRoot || host.attachShadow({mode: 'open'});
  const oldLayer = document.getElementById(data.id + '-layer');
  if (oldLayer) oldLayer.remove();
  host.style.cssText = inRail
    ? 'display:flex;flex-direction:column;margin:6px 8px 8px;flex:1 1 auto;min-height:180px;max-height:calc(100% - 120px);align-self:stretch;max-width:100%;'
    : 'position:fixed;left:10px;top:72px;z-index:2147483646;width:280px;height:70vh;';
  const c = data.counts || {};
  const line = [
    c.in_review ? ('待验收 ' + c.in_review) : '',
    c.in_progress ? ('处理中 ' + c.in_progress) : '',
    c.todo ? ('待认领 ' + c.todo) : '',
    c.blocked ? ('受阻 ' + c.blocked) : '',
  ].filter(Boolean).join(' · ') || ('共 ' + (data.total || 0) + ' 张');
  const esc = (s) => String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  root.innerHTML =
    '<style>' +
    ':host{font:12px/1.4 "Segoe UI","Microsoft YaHei UI",sans-serif;color:#1B1D22;' +
    'display:flex;flex-direction:column;min-height:0;}' +
    '.box{padding:8px 9px;border:1px solid rgba(27,29,34,.12);border-radius:10px 10px 0 0;' +
    'background:rgba(255,255,255,.88);cursor:pointer;flex:none;}' +
    'h4{margin:0 0 4px;font-size:12px;} .meta{opacity:.72;font-size:11px;}' +
    'iframe{flex:1;border:0;width:100%;min-height:160px;background:#F6F7F9;' +
    'border:1px solid rgba(27,29,34,.12);border-top:0;border-radius:0 0 10px 10px;}' +
    '</style><div class="box" id="chip"><h4>任务面板</h4>' +
    '<div class="meta">' + line + ' · 栏内展开</div></div>' +
    '<iframe src="' + esc(data.url) + '" title="任务面板"></iframe>';
  return { injected: true, sidebar: inRail, underPlugin: Boolean(pluginBtn),
           count: data.total || 0, embedded: true, overlay: false };
})()""".replace("__PAYLOAD__", payload)


def evaluate(expression: str) -> dict[str, Any]:
    port, reason = _cdp_port()
    if port < 1024:
        return {"ok": False, "code": "NO_CDP", "error": reason or "没有调试端口"}
    if not CDP_SCRIPT.is_file():
        return {"ok": False, "code": "NO_HELPER", "error": "缺少 codex_sidebar_cdp.mjs"}
    try:
        proc = subprocess.run(
            [_node_exe(), str(CDP_SCRIPT), "eval"],
            input=json.dumps({"port": port, "expression": expression},
                             ensure_ascii=False).encode("utf-8"),
            capture_output=True, timeout=12, creationflags=_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "code": "CDP_FAIL", "error": repr(exc)}
    raw = (proc.stdout or b"").decode("utf-8", "replace").strip()
    try:
        reply = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        err = (proc.stderr or b"").decode("utf-8", "replace").strip()
        return {"ok": False, "code": "CDP_FAIL",
                "error": err or raw or "CDP 助手没有返回 JSON"}
    if not reply.get("ok"):
        reply["ok"] = False
        reply.setdefault("code", "CDP_FAIL")
        err = str(reply.get("error") or "").strip()
        if not err:
            err = (proc.stderr or b"").decode("utf-8", "replace").strip() or reason or "CDP 助手没有返回 JSON"
        elif reason:
            err = reason + "；" + err
        reply["error"] = err
        return reply
    return reply


def probe() -> dict[str, Any]:
    reply = evaluate(PROBE_JS)
    if reply.get("ok"):
        value = reply.get("value") or {}
        reply["msg"] = ("找到 Codex 侧栏，宽 {}px".format(value.get("sidebarW") or 0)
                        if value.get("sidebar") else
                        "调试口通了，但页面上没有 aside.app-shell-left-panel（会走浮层）")
    return reply


def inject(board_url: str = "") -> dict[str, Any]:
    reply = evaluate(inject_js(snapshot_cards(), board_url))
    if reply.get("ok"):
        value = reply.get("value") or {}
        where = "已嵌进 Codex 侧栏" if value.get("sidebar") else "侧栏没找到，已用浮层"
        reply["msg"] = where + "，任务面板已内置，{} 张卡".format(value.get("count") or 0)
        reply["embedded"] = True
    return reply


def remove() -> dict[str, Any]:
    reply = evaluate(REMOVE_JS)
    if reply.get("ok"):
        reply["msg"] = "已卸下" if (reply.get("value") or {}).get("removed") else "本来就没有任务条"
    return reply


def _browser_id() -> str:
    state = _read_json(DREAMSKIN_STATE / "state.json", {}) or {}
    return str(state.get("browserId") or "").strip()


def keep_injected() -> dict[str, Any]:
    """Codex 起来或换了一轮调试会话时，把栏内看板补回去。

    侧栏还没有就先不嵌（避免又变成浮层）。已经嵌过且还是同一 browserId 则跳过。
    """
    global _last_injected_browser
    probed = probe()
    if not probed.get("ok"):
        return {"ok": False, "skipped": True,
                "reason": probed.get("error") or probed.get("code") or "probe failed"}
    value = probed.get("value") or {}
    if not value.get("sidebar"):
        return {"ok": False, "skipped": True, "reason": "no sidebar"}
    bid = _browser_id()
    if value.get("dock") and bid and bid == _last_injected_browser:
        return {"ok": True, "skipped": True, "reason": "already"}
    reply = inject()
    if reply.get("ok"):
        _last_injected_browser = bid
        reply["kept"] = True
    return reply


def start_auto_inject(interval_sec: float = AUTO_INJECT_SEC) -> None:
    """控制台起来后在后台盯着 CDP：皮肤好了就嵌，Codex 重启再嵌一次。"""
    global _auto_thread
    wait = max(5.0, float(interval_sec or AUTO_INJECT_SEC))
    with _auto_lock:
        if _auto_thread is not None and _auto_thread.is_alive():
            return

        def loop() -> None:
            while True:
                try:
                    keep_injected()
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(wait)

        _auto_thread = threading.Thread(
            target=loop, name="rxyy-codex-board", daemon=True)
        _auto_thread.start()
