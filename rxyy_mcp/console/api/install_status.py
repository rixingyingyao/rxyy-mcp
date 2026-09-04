"""装没装一览：把散落各处的「往 Cursor / Codex / 本机装东西」统一体检成一张表。

为什么单开一个模块：这些安装点原本各自为政——无感补丁在账号页、mcp 接入在环境
自检页、开车在rxyy-mcp 顶栏、Codex 子代理压根没有界面。用户的原话是「我都不知道
装了还是没装」，症结不是没检测，而是**六处已有的检测结果从没汇到一个地方**。

三态而不是两态（这条是重点）：
- installed 已装
- missing   未装
- broken    **装了但失效** —— Cursor 一升级就把无感补丁和开车 stub 冲掉，文件还在、
            标记没了，两态模型会把它报成「已装」，用户点半天没反应还以为自己手潮。
- unknown   查不了（没装 Cursor、非 Windows、检测器自己塌了）

设计红线：
- 全部**只读**，任何一项都不许改机器；
- 任何一项塌了只让它自己变 unknown，绝不连累整张表（历史教训：一个字段坏掉
  让整份快照静默不写）；
- 面板会轮询，所以慢活（子进程、扫 Cursor 安装目录）一律走缓存或短超时。
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

_HOME = Path(os.path.expanduser("~"))
_CURSOR_DIR = _HOME / ".cursor"
_CURSOR_MCP_JSON = _CURSOR_DIR / "mcp.json"
_CURSOR_HOOKS_JSON = _CURSOR_DIR / "hooks.json"
_CURSOR_HOOKS_DIR = _CURSOR_DIR / "hooks"
_CODEX_DIR = _HOME / ".codex"
_CODEX_CONFIG = _CODEX_DIR / "config.toml"
_SALAK_HOOK = _HOME / ".salak" / "hook.js"

MCP_ENTRY_KEY = "rxyy-mcp"
GATEWAY_PORT = 38777          # rxyy-mcp 网关，开车状态从这儿取
CODEX_GUARD_TASK = "rxyy-codex-agents-guard"

INSTALLED, MISSING, BROKEN, UNKNOWN = "installed", "missing", "broken", "unknown"

STATE_LABELS = {
    INSTALLED: "已装",
    MISSING: "未装",
    BROKEN: "装了但失效",
    UNKNOWN: "查不了",
}

# 装进 Cursor 本体的最该让人心里有数：它一升级就可能把这些冲掉
GROUP_CURSOR, GROUP_CODEX, GROUP_LOCAL = "Cursor", "Codex", "本机"


def _read_json(p: Path) -> Any:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


# 子进程/扫盘这类慢活的缓存。面板轮询时不能每拍都去 schtasks 开一个进程。
_CACHE: dict[str, tuple[float, Any]] = {}


def _cached(key: str, ttl: float, fn: Callable[[], Any]) -> Any:
    hit = _CACHE.get(key)
    now = time.time()
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = fn()
    _CACHE[key] = (now, val)
    return val


def _item(key, group, label, state, detail="", where="", fix="", why="") -> dict[str, Any]:
    """why = 「这东西是干嘛的」，面板上鼠标移上去就能看懂，不用回来翻代码。"""
    return {"key": key, "group": group, "label": label, "state": state,
            "state_label": STATE_LABELS.get(state, state),
            "detail": detail, "where": where, "fix": fix, "why": why}


# ---------------------------------------------------------------- 各项检测
def check_cursor_root() -> dict[str, Any]:
    def probe():
        try:
            from src.cursor import seamless
            r = seamless.cursor_root()
            if not r:
                return {"root": ""}
            # 版本值得报出来：Cursor 每升一次级就把无感补丁和开车 stub 冲掉一次，
            # 「上次看还好好的，今天怎么全失效了」十有八九答案就在这个号里。
            ver = ""
            pkg = _read_json(Path(r) / "resources" / "app" / "package.json")
            if isinstance(pkg, dict):
                ver = str(pkg.get("version") or "")
            return {"root": str(r), "ver": ver}
        except Exception:  # noqa: BLE001
            return {"root": ""}
    st = _cached("cursor_root", 60.0, probe)
    root = st.get("root") or ""
    ver = st.get("ver") or ""
    return _item("cursor", GROUP_CURSOR, "Cursor 本体",
                 INSTALLED if root else UNKNOWN,
                 (("%s（%s）" % (root, ver)) if ver else root)
                 or "没找到 Cursor 安装目录，下面几项也就无从查起",
                 root, why="下面凡是「装进 Cursor」的项都以它为前提；它一升级就冲掉补丁和 stub")


def check_mcp_entry() -> dict[str, Any]:
    mj = _read_json(_CURSOR_MCP_JSON)
    if not isinstance(mj, dict):
        return _item("mcp_entry", GROUP_CURSOR, "MCP 接入条目", MISSING,
                     "还没有 mcp.json", str(_CURSOR_MCP_JSON), "env_install_mcp",
                     why="Cursor 靠它连上控制台，没有它 zhi/zt/ji 都用不了")
    entry = (mj.get("mcpServers") or {}).get(MCP_ENTRY_KEY)
    if not isinstance(entry, dict):
        return _item("mcp_entry", GROUP_CURSOR, "MCP 接入条目", MISSING,
                     "mcp.json 里没有「%s」这一条" % MCP_ENTRY_KEY,
                     str(_CURSOR_MCP_JSON), "env_install_mcp",
                     why="Cursor 靠它连上控制台，没有它 zhi/zt/ji 都用不了")
    if entry.get("disabled"):
        return _item("mcp_entry", GROUP_CURSOR, "MCP 接入条目", BROKEN,
                     "条目在，但被标了 disabled，Cursor 不会连",
                     str(_CURSOR_MCP_JSON), "env_install_mcp",
                     why="Cursor 靠它连上控制台，没有它 zhi/zt/ji 都用不了")
    if not entry.get("url"):
        return _item("mcp_entry", GROUP_CURSOR, "MCP 接入条目", BROKEN,
                     "条目在，但没有 url，连不上", str(_CURSOR_MCP_JSON),
                     "env_install_mcp",
                     why="Cursor 靠它连上控制台，没有它 zhi/zt/ji 都用不了")
    return _item("mcp_entry", GROUP_CURSOR, "MCP 接入条目", INSTALLED,
                 str(entry.get("url")), str(_CURSOR_MCP_JSON),
                 why="Cursor 靠它连上控制台，没有它 zhi/zt/ji 都用不了")


def check_seamless_patch() -> dict[str, Any]:
    why = "切号免重启。补丁改的是 Cursor 自己的 workbench.js，Cursor 一升级就没了"
    def probe():
        try:
            from src.cursor import seamless
            st = seamless.patch_status()
            return {"active": bool(st.active), "reason": st.reason or "",
                    "marker": st.marker or "",
                    "js": str(st.workbench_js) if st.workbench_js else ""}
        except Exception as exc:  # noqa: BLE001
            return {"err": repr(exc)}
    st = _cached("seamless", 20.0, probe)
    if "err" in st:
        return _item("seamless_patch", GROUP_CURSOR, "无感补丁", UNKNOWN,
                     "检测不了：%s" % st["err"], why=why)
    if st["active"]:
        return _item("seamless_patch", GROUP_CURSOR, "无感补丁", INSTALLED,
                     "补丁标记 %s 在位" % st["marker"], st["js"],
                     why=why)
    if not st["js"]:
        return _item("seamless_patch", GROUP_CURSOR, "无感补丁", UNKNOWN,
                     st["reason"] or "找不到 workbench.js", why=why)
    return _item("seamless_patch", GROUP_CURSOR, "无感补丁", MISSING,
                 st["reason"] or "workbench 里没有补丁标记", st["js"],
                 "acc_install_patch", why=why)


def check_parkgate() -> dict[str, Any]:
    """开车：hook.js 在盘上只是一半，Cursor 扩展宿主入口那行 stub 才是另一半。

    状态问rxyy-mcp 网关拿（它那边有 30s 缓存，且是唯一权威）；网关不通就退回
    只看 hook.js，并老实说「只查到一半」，绝不假装已装。
    """
    why = "把 Cursor 里发出的消息先扣住、攒一批再统一放行"
    def probe():
        try:
            import urllib.request
            req = urllib.request.Request(
                "http://127.0.0.1:%d/api/park_status" % GATEWAY_PORT,
                data=b"[]", headers={"Content-Type": "application/json"},
                method="POST")
            with urllib.request.urlopen(req, timeout=3) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            return None
    st = _cached("parkgate", 15.0, probe)
    if not isinstance(st, dict) or not st.get("ok"):
        on_disk = _SALAK_HOOK.is_file()
        return _item("parkgate", GROUP_CURSOR, "开车 hook",
                     UNKNOWN,
                     ("hook.js 在盘上，但控制台没应答，查不到 Cursor 入口那半截"
                      if on_disk else "控制台没应答，且 hook.js 不在盘上"),
                     str(_SALAK_HOOK), why=why)
    note = str(st.get("wiring_note") or "")
    if st.get("wired"):
        return _item("parkgate", GROUP_CURSOR, "开车 hook", INSTALLED,
                     note or "已接通", str(_SALAK_HOOK), why=why)
    if st.get("hook_installed"):
        # hook.js 在、stub 不在 = 典型的「Cursor 升级后被冲掉」，按了没反应
        return _item("parkgate", GROUP_CURSOR, "开车 hook", BROKEN,
                     note or "hook.js 在，但 Cursor 入口没注入 stub，开车按了不生效",
                     str(_SALAK_HOOK), "park_install_hook", why=why)
    return _item("parkgate", GROUP_CURSOR, "开车 hook", MISSING,
                 note or "还没装", str(_SALAK_HOOK), "park_install_hook", why=why)


def check_cursor_hooks() -> dict[str, Any]:
    why = "文件占用看板、停车这些要靠 Cursor 钩子回调"
    hj = _read_json(_CURSOR_HOOKS_JSON)
    if isinstance(hj, dict) and hj.get("hooks"):
        return _item("cursor_hooks", GROUP_CURSOR, "Cursor 钩子", INSTALLED,
                     "hooks.json 已登记", str(_CURSOR_HOOKS_JSON), why=why)
    scripts = []
    if _CURSOR_HOOKS_DIR.is_dir():
        scripts = sorted(p.name for p in _CURSOR_HOOKS_DIR.glob("*.cjs"))
    if scripts:
        # 脚本在、hooks.json 没登记 = Cursor 根本不会去调它们
        return _item("cursor_hooks", GROUP_CURSOR, "Cursor 钩子", BROKEN,
                     "钩子脚本在（%s），但 hooks.json 没登记，Cursor 不会调用"
                     % "、".join(scripts[:3]), str(_CURSOR_HOOKS_DIR), why=why)
    return _item("cursor_hooks", GROUP_CURSOR, "Cursor 钩子", MISSING,
                 "没装（非必需）", str(_CURSOR_HOOKS_JSON), why=why)


def check_codex_agents() -> dict[str, Any]:
    """~/.codex/config.toml 的 [agents]。

    这一项特别值得盯：Codex 桌面端刷新市场 / 改设置时会整份重写 config.toml，
    把它不认识的 [agents] 整块丢掉（08-07 实测，相隔 41 秒），无声无息。
    """
    why = "Codex 子代理的默认模型与并发。桌面端改设置时会把这块整段丢掉"
    if not _CODEX_CONFIG.is_file():
        return _item("codex_agents", GROUP_CODEX, "Codex 子代理配置", MISSING,
                     "还没有 %s" % _CODEX_CONFIG, str(_CODEX_CONFIG), why=why)
    try:
        text = _CODEX_CONFIG.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return _item("codex_agents", GROUP_CODEX, "Codex 子代理配置", UNKNOWN,
                     "读不了：%r" % exc, str(_CODEX_CONFIG), why=why)
    has = any(ln.strip().startswith("[agents") for ln in text.splitlines())
    if has:
        return _item("codex_agents", GROUP_CODEX, "Codex 子代理配置", INSTALLED,
                     "[agents] 在位", str(_CODEX_CONFIG), why=why)
    return _item("codex_agents", GROUP_CODEX, "Codex 子代理配置", MISSING,
                 "config.toml 在，但 [agents] 整块不见了"
                 "（Codex 桌面端刷新市场时会干这事）", str(_CODEX_CONFIG), why=why)


def check_codex_guard() -> dict[str, Any]:
    """每 5 分钟把被冲掉的 [agents] 补回去的计划任务。"""
    why = "定时把被 Codex 桌面端冲掉的 [agents] 自动补回来"
    def probe():
        if os.name != "nt":
            return None
        try:
            r = subprocess.run(
                ["schtasks", "/Query", "/TN", CODEX_GUARD_TASK],
                capture_output=True, text=True, timeout=8,
                encoding="utf-8", errors="replace", creationflags=0x08000000)
            return {"rc": r.returncode, "out": (r.stdout or "")[-400:]}
        except Exception:  # noqa: BLE001
            return None
    r = _cached("codex_guard", 120.0, probe)
    if r is None:
        return _item("codex_guard", GROUP_CODEX, "子代理自愈守护", UNKNOWN,
                     "查不了计划任务", CODEX_GUARD_TASK, why=why)
    if r["rc"] != 0:
        return _item("codex_guard", GROUP_CODEX, "子代理自愈守护", MISSING,
                     "没有计划任务 %s，[agents] 被冲掉后没人补" % CODEX_GUARD_TASK,
                     CODEX_GUARD_TASK, why=why)
    disabled = "已禁用" in r["out"] or "Disabled" in r["out"]
    if disabled:
        return _item("codex_guard", GROUP_CODEX, "子代理自愈守护", BROKEN,
                     "计划任务在，但被禁用了，不会跑", CODEX_GUARD_TASK, why=why)
    return _item("codex_guard", GROUP_CODEX, "子代理自愈守护", INSTALLED,
                 "计划任务在跑", CODEX_GUARD_TASK, why=why)


def check_codebrain() -> dict[str, Any]:
    why = "项目记忆的三层检索：规则注入 / 文件记忆 / 知识图谱"
    mj = _read_json(_CURSOR_MCP_JSON)
    servers = (mj.get("mcpServers") or {}) if isinstance(mj, dict) else {}
    fs = servers.get("codebrain-fs") or {}
    gr = servers.get("codebrain-graphiti") or {}
    on = [n for n, e in (("文件记忆", fs), ("知识图谱", gr))
          if isinstance(e, dict) and e and not e.get("disabled")]
    off = [n for n, e in (("文件记忆", fs), ("知识图谱", gr))
           if isinstance(e, dict) and e and e.get("disabled")]
    if len(on) == 2:
        return _item("codebrain", GROUP_CURSOR, "codebrain 记忆", INSTALLED,
                     "文件记忆与知识图谱都接上了", str(_CURSOR_MCP_JSON), why=why)
    if len(off) == 2:
        # 两条都在、都被停用 = 装好了没在用。这是人主动关的（MCP 注册即付费，
        # 停用比移除好，见 cb_mcp_toggle），报成「失效」是在催他改回一个刻意的决定
        return _item("codebrain", GROUP_CURSOR, "codebrain 记忆", INSTALLED,
                     "两条都接好了，但都停用着——现在 agent 用不到它们（省 token）",
                     str(_CURSOR_MCP_JSON), why=why)
    if on or off:
        return _item("codebrain", GROUP_CURSOR, "codebrain 记忆", BROKEN,
                     "只接了一半：在用 %s；%s" % (
                         "、".join(on) or "无",
                         ("被停用 " + "、".join(off)) if off else "另一个没接"),
                     str(_CURSOR_MCP_JSON), "cb_setup_link_mcp", why=why)
    return _item("codebrain", GROUP_CURSOR, "codebrain 记忆", MISSING,
                 "两个 MCP 都没接", str(_CURSOR_MCP_JSON), "cb_setup_link_mcp",
                 why=why)


def check_codex_mcp() -> dict[str, Any]:
    """Cursor 那边的 MCP 有没有同步进 codex。

    这一项跟「MCP 接入条目」不是一回事：那条查的是 Cursor 连不连得上控制台，
    这条查的是同一批 server 有没有搬到 codex 去。codex 用的是 config.toml 里的
    mcp_servers，Cursor 改了不会自己跟过去——**两边不一致时 codex 侧的 agent
    会少几件工具，而它自己不会喊**，只表现为「codex 怎么不会用这个」。
    """
    why = "Cursor 的 MCP server 同步一份给 codex，两边 agent 拿到同样的工具"
    def probe():
        try:
            from api import codebrain_sync as cbs
            d = cbs.diff_cursor_to_codex()
            rows = d.get("rows") or []
            return {"total": len(rows),
                    "synced": [r["name"] for r in rows if r["status"] == "synced"],
                    "missing": [r["name"] for r in rows if r["status"] == "missing"],
                    "differs": [r["name"] for r in rows if r["status"] == "differs"],
                    "off": [r["name"] for r in rows if r.get("off_differs")]}
        except Exception as exc:  # noqa: BLE001
            return {"err": repr(exc)}
    r = _cached("codex_mcp", 30.0, probe)
    if "err" in r:
        return _item("codex_mcp", GROUP_CODEX, "MCP 同步到 codex", UNKNOWN,
                     "检测不了：%s" % r["err"], str(_CODEX_CONFIG), why=why)
    if not r["total"]:
        return _item("codex_mcp", GROUP_CODEX, "MCP 同步到 codex", UNKNOWN,
                     "Cursor 侧一个 MCP server 都没有，无从比对",
                     str(_CODEX_CONFIG), why=why)
    if r["differs"]:
        # 同名、却指向不同的东西。这才是真出错：codex 那边连过去是另一个服务
        return _item("codex_mcp", GROUP_CODEX, "MCP 同步到 codex", BROKEN,
                     "%s 两边指向的不是同一个东西"
                     % "、".join(r["differs"][:3]),
                     str(_CODEX_CONFIG), "cb_mcp_sync", why=why)
    if not r["synced"]:
        return _item("codex_mcp", GROUP_CODEX, "MCP 同步到 codex", MISSING,
                     "%d 个 server 一个都没同步过去" % r["total"],
                     str(_CODEX_CONFIG), "cb_mcp_sync", why=why)
    # codex 少几个不算故障：多半是有意不给它（浏览器、数据库这些 Cursor 专用的
    # 搬过去也没用）。说清楚就够了，不报红、不催人去「修」——催错了比不催更烦。
    part = ["%d 个已同步" % len(r["synced"])]
    if r["missing"]:
        part.append("codex 另缺 %d 个（%s%s；可能是你有意不给它）"
                    % (len(r["missing"]), "、".join(r["missing"][:3]),
                       " 等" if len(r["missing"]) > 3 else ""))
    if r["off"]:
        part.append("%d 个两边开关不一样" % len(r["off"]))
    return _item("codex_mcp", GROUP_CODEX, "MCP 同步到 codex", INSTALLED,
                 "；".join(part), str(_CODEX_CONFIG), why=why)


def check_codex_github_plugin() -> dict[str, Any]:
    """官方 github@openai-curated 装没装。只看本地缓存目录，不跑 CLI（设置页会轮询）。"""
    why = "Codex 官方 GitHub 插件：PR 评审/扫仓库。在「Codex 插件」页装卸"
    root = _CODEX_DIR / "plugins" / "cache" / "openai-curated" / "github"
    if not root.is_dir():
        return _item("codex_github_plugin", GROUP_CODEX, "Codex GitHub 插件", MISSING,
                     "还没装 github@openai-curated", str(root), why=why)
    versions = sorted(p.name for p in root.iterdir() if p.is_dir())
    if not versions:
        return _item("codex_github_plugin", GROUP_CODEX, "Codex GitHub 插件", BROKEN,
                     "插件目录在，但没有版本缓存", str(root), why=why)
    return _item("codex_github_plugin", GROUP_CODEX, "Codex GitHub 插件", INSTALLED,
                 "已装 %s" % versions[-1], str(root), why=why)


def check_codex_rules() -> dict[str, Any]:
    """codex 全局 AGENTS.md 里的 codebrain 托管块。

    规则是「开局即在上下文」那一层，装没装的差别是 codex 那边的 agent 认不认
    rxyy 的工作协议。托管块用两个 HTML 注释夹着，块外内容不动——所以「文件在」
    远不等于「规则装了」，得看那对标记在不在。
    """
    why = "让 codex 侧的 agent 开局就认 rxyy 的工作协议（托管块内联，或自己写指针）"
    def probe():
        try:
            from api import codebrain_sync as cbs
            st = cbs.read_codex_agents()
            txt = ""
            try:
                txt = Path(st.get("path") or "").read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass
            names = [r["name"] for r in cbs.list_codebrain_rules()
                     if r.get("kind") == "v19"]
            return {"path": st.get("path", ""), "exists": bool(st.get("exists")),
                    "block": bool(st.get("has_block")), "bytes": int(st.get("bytes") or 0),
                    # 指针式：不内联，只写一句「先去读那份协议」。这台机器上就是这么配的
                    "points_at": any(n and n in txt for n in names) or "AICodebrain" in txt}
        except Exception as exc:  # noqa: BLE001
            return {"err": repr(exc)}
    r = _cached("codex_rules", 30.0, probe)
    if "err" in r:
        return _item("codex_rules", GROUP_CODEX, "codex 规则", UNKNOWN,
                     "检测不了：%s" % r["err"], why=why)
    where = r["path"]
    if r["block"]:
        return _item("codex_rules", GROUP_CODEX, "codex 规则", INSTALLED,
                     "托管块在位（AGENTS.md 共 %d 字节）" % r["bytes"], where, why=why)
    if r["points_at"]:
        # 没有托管块，但 AGENTS.md 自己指向了协议原文。这是**更省的一种装法**：
        # 协议一万多字，内联进全局 AGENTS.md 等于每个 codex 任务都先付一遍这笔
        # token。指着让它去读，规则照样到位。判成失效就会催人把它内联回来。
        return _item("codex_rules", GROUP_CODEX, "codex 规则", INSTALLED,
                     "AGENTS.md（%d 字节）用指针指向协议原文，没内联——比托管块省 token"
                     % r["bytes"], where, why=why)
    if r["exists"] and r["bytes"]:
        # 文件在、既没托管块也没提协议：多半是手工编辑时把那段整个删了
        return _item("codex_rules", GROUP_CODEX, "codex 规则", BROKEN,
                     "AGENTS.md 在（%d 字节）却既没有托管块、也没指向协议原文，"
                     "codex 那边等于没规则" % r["bytes"], where, "cb_rules_sync", why=why)
    return _item("codex_rules", GROUP_CODEX, "codex 规则", MISSING,
                 "还没给 codex 全局 AGENTS.md 配规则", where, "cb_rules_sync", why=why)


def check_skills() -> dict[str, Any]:
    why = "Cursor 与 codex 的全局技能，开局就注入上下文"
    found, where = [], []
    for base, tag in ((_CURSOR_DIR / "skills-cursor", "Cursor"),
                      (_HOME / ".agents" / "skills", "codex")):
        if not base.is_dir():
            continue
        n = sum(1 for d in base.iterdir() if (d / "SKILL.md").is_file())
        if n:
            found.append("%s %d 个" % (tag, n))
            where.append(str(base))
    if found:
        return _item("skills", GROUP_LOCAL, "全局技能", INSTALLED,
                     "、".join(found), " | ".join(where), why=why)
    return _item("skills", GROUP_LOCAL, "全局技能", MISSING,
                 "两个技能目录都是空的", str(_CURSOR_DIR / "skills-cursor"), why=why)


def check_autostart() -> dict[str, Any]:
    """开机自启：主程序与看门狗两条 Run 键，缺一条就是半残。"""
    why = "开机自动拉起控制台与看门狗，不用每次手动开"
    if os.name != "nt":
        return _item("autostart", GROUP_LOCAL, "开机自启", UNKNOWN, "非 Windows", why=why)
    try:
        import winreg
    except Exception:  # noqa: BLE001
        return _item("autostart", GROUP_LOCAL, "开机自启", UNKNOWN, "读不了注册表", why=why)
    key = r"Software\Microsoft\Windows\CurrentVersion\Run"
    got = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            for name in ("RxyyPlus", "RxyyPlusWatchdog"):
                try:
                    got[name] = winreg.QueryValueEx(k, name)[0]
                except OSError:
                    pass
    except OSError as exc:
        return _item("autostart", GROUP_LOCAL, "开机自启", UNKNOWN,
                     "读不了 Run 键：%r" % exc, key, why=why)
    if len(got) == 2:
        return _item("autostart", GROUP_LOCAL, "开机自启", INSTALLED,
                     "主程序与看门狗都已登记", key, why=why)
    if got:
        return _item("autostart", GROUP_LOCAL, "开机自启", BROKEN,
                     "只登记了 %s，另一条缺失（看门狗缺了就没人复活崩掉的控制台）"
                     % "、".join(got), key, why=why)
    return _item("autostart", GROUP_LOCAL, "开机自启", MISSING, "没登记", key, why=why)


def _rustdesk_exe() -> str:
    """找 rustdesk.exe。

    rustdesk_api 里那个 `_find_exe` 是拿 powershell 查 WMI 的，一次最长 15 秒；
    这张表要被面板轮询，不能为一项等它。改读服务的 ImagePath（注册表，普通权限
    可读、瞬间返回），再退回同一批候选路径——判据与它一致，只是路子快。
    """
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SYSTEM\CurrentControlSet\Services\RustDesk") as k:
            img = str(winreg.QueryValueEx(k, "ImagePath")[0]).replace('"', "")
        for tail in (" --service", " --server"):
            img = img.split(tail)[0]
        img = img.strip()
        if img and os.path.isfile(img):
            return img
    except Exception:  # noqa: BLE001
        pass
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    for c in (os.path.join(pf, "RustDesk", "RustDesk.exe"),
              os.path.join(os.environ.get("ProgramW6432", pf), "RustDesk", "RustDesk.exe"),
              r"D:\RustDesk\RustDesk.exe",
              os.path.join(os.environ.get("LOCALAPPDATA", ""), "rustdesk", "rustdesk.exe")):
        if c and os.path.isfile(c):
            return c
    return ""


def check_rustdesk() -> dict[str, Any]:
    """远程桌面：装了客户端 ≠ 接进了自建中继。

    这一项有两层，只看客户端在不在会把「装着、但连的还是官方服务器」报成已装——
    而那恰恰是人在外面连不上时最摸不着头脑的一种。
    """
    why = "自建中继的远程桌面。人在外面时靠它连回这台机器"

    def probe():
        try:
            from api import rustdesk_api as rd
            exe = _rustdesk_exe()
            st = rd._local_config_state() if exe else {}
            return {"exe": exe,
                    "cfg_host": (rd.wfdb().kv_get(rd._K_HOST, "") or "").strip(),
                    "server": st.get("server", ""),
                    "pwd": bool(st.get("password_set"))}
        except Exception as exc:  # noqa: BLE001
            return {"err": repr(exc)}

    r = _cached("rustdesk", 120.0, probe)
    if "err" in r:
        return _item("rustdesk", GROUP_LOCAL, "远程桌面接入", UNKNOWN,
                     "检测不了：%s" % r["err"], why=why)
    if not r["exe"]:
        return _item("rustdesk", GROUP_LOCAL, "远程桌面接入", MISSING,
                     "本机没装 RustDesk 客户端", "", "rd_join", why=why)
    if not r["cfg_host"]:
        return _item("rustdesk", GROUP_LOCAL, "远程桌面接入", MISSING,
                     "客户端装了，但设置里还没填自建服务器地址与公钥",
                     r["exe"], "rd_join", why=why)
    if r["server"] != r["cfg_host"]:
        # 客户端在、却没指向自建中继：界面上一切正常，人在外面就是连不上
        return _item("rustdesk", GROUP_LOCAL, "远程桌面接入", BROKEN,
                     "客户端连的是 %s，不是配置的 %s"
                     % (r["server"] or "官方服务器", r["cfg_host"]),
                     r["exe"], "rd_join", why=why)
    return _item("rustdesk", GROUP_LOCAL, "远程桌面接入", INSTALLED,
                 "已接入 %s%s" % (r["cfg_host"],
                                  "，已设永久密码" if r["pwd"] else "，未设永久密码"),
                 r["exe"], why=why)


_CHECKS: tuple[tuple[str, Callable[[], dict[str, Any]]], ...] = (
    ("cursor", check_cursor_root),
    ("mcp_entry", check_mcp_entry),
    ("seamless_patch", check_seamless_patch),
    ("parkgate", check_parkgate),
    ("cursor_hooks", check_cursor_hooks),
    ("codebrain", check_codebrain),
    ("codex_agents", check_codex_agents),
    ("codex_guard", check_codex_guard),
    ("codex_mcp", check_codex_mcp),
    ("codex_github_plugin", check_codex_github_plugin),
    ("codex_rules", check_codex_rules),
    ("skills", check_skills),
    ("autostart", check_autostart),
    ("rustdesk", check_rustdesk),
)


# 「去装 / 去修」按下去跳哪儿：页面的 data-page + 该页上真正干这件事的控件 id。
# 写在后端是因为「这一项归哪个页面管」跟检测本身是同一件知识，拆两处早晚走岔。
# 空 anchor = 那页没有一键安装的按钮（只读页，或要人自己填几个字段）。
_FIX_TARGETS: dict[str, tuple[str, str]] = {
    "mcp_entry": ("settings", "envFixMcp"),
    "seamless_patch": ("accounts", "accPatchInstall"),
    "parkgate": ("mcp", ""),          # 开车在rxyy-mcp 顶栏，控制台里嵌在「持久 MCP」页
    "cursor_hooks": ("cbhooks", ""),
    "codebrain": ("cbsetup", "cbsetupLink"),
    "codex_agents": ("cbagents", "cbaSave"),
    "codex_guard": ("cbagents", ""),
    "codex_mcp": ("cbmcp", ""),
    "codex_github_plugin": ("cbplugins", ""),
    "codex_rules": ("cbrules", ""),
    "skills": ("cbskills", ""),
    "rustdesk": ("rustdesk", "rdJoin"),
}


class InstallStatusApi:
    """挂到 bridge.Api：前端 window.pywebview.api.install_* 调用。"""

    def install_status(self) -> dict[str, Any]:
        """一次性体检所有安装点。只读，任何一项塌了都不影响其它项。"""
        items: list[dict[str, Any]] = []
        for key, fn in _CHECKS:
            try:
                items.append(fn())
            except Exception as exc:  # noqa: BLE001
                items.append(_item(key, GROUP_LOCAL, key, UNKNOWN,
                                   "检测器自己出错：%r" % exc))
        for it in items:
            page, anchor = _FIX_TARGETS.get(it["key"], ("", ""))
            it["goto_page"], it["goto_anchor"] = page, anchor
        counts = {s: sum(1 for i in items if i["state"] == s)
                  for s in (INSTALLED, MISSING, BROKEN, UNKNOWN)}
        # 失效比未装更该先看见：未装多半是没用上，失效是「你以为在用其实没有」
        attention = [i for i in items if i["state"] in (BROKEN, MISSING)]
        return {
            "ok": True,
            "items": items,
            "counts": counts,
            "need_attention": [i["key"] for i in attention],
            "all_ok": not attention,
            "checked_at": time.strftime("%H:%M:%S"),
        }

    def install_status_refresh(self) -> dict[str, Any]:
        """点「重新检测」：把慢活缓存清掉再查一遍，拿到的一定是此刻的实况。"""
        _CACHE.clear()
        return self.install_status()
