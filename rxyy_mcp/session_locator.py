# -*- coding: utf-8 -*-
"""Cursor 会话 transcript 精确定位、标题读取与复制提示词。"""
import json
import os
import re
import sqlite3
import time
from pathlib import Path


def cursor_project_slug(cwd):
    normalized = (cwd or "").strip().replace("\\", "/").strip("/")
    return re.sub(r"[^A-Za-z0-9]+", "-", normalized).strip("-").lower()


def _contains_conversation_id(value, conversation_id):
    if isinstance(value, dict):
        if value.get("conversation_id") == conversation_id:
            return True
        return any(_contains_conversation_id(v, conversation_id) for v in value.values())
    if isinstance(value, list):
        return any(_contains_conversation_id(v, conversation_id) for v in value)
    return False


def find_cursor_transcript(cwd, conversation_id, projects_root=None):
    cid = (conversation_id or "").strip()
    if not cid or cid == "__default__":
        return None
    root = Path(projects_root or (Path.home() / ".cursor" / "projects"))
    preferred = root / cursor_project_slug(cwd) / "agent-transcripts"

    def sort_existing(paths):
        dated = []
        for path in paths:
            try:
                dated.append((path.stat().st_mtime, path))
            except OSError:
                continue
        return [path for _, path in sorted(dated, reverse=True)]

    preferred_paths = (
        sort_existing(preferred.glob("*/*.jsonl"))
        if preferred.is_dir()
        else []
    )
    seen = set(preferred_paths)
    fallback_paths = sort_existing(
        path
        for path in root.glob("*/agent-transcripts/*/*.jsonl")
        if path not in seen
    )
    candidates = preferred_paths + fallback_paths
    for path in candidates:
        try:
            with path.open("r", encoding="utf-8") as stream:
                for line in stream:
                    if cid not in line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if _contains_conversation_id(data, cid):
                        return path
        except OSError:
            continue
    return None


def _main_transcripts(folder):
    """列出目录下的主会话 jsonl（<UUID>/<UUID>.jsonl），按 mtime 新→旧"""
    entries = []
    for p in folder.glob("*/*.jsonl"):
        if p.parent.name != p.stem:
            continue  # 跳过 subagents
        try:
            st = p.stat()
        except OSError:
            continue
        entries.append((st.st_mtime, st.st_ctime, p))
    entries.sort(reverse=True)
    return entries


def _content_match(entries, cid, exclude, max_files):
    """内容精确匹配：同一行同时出现 conversation_id 字样和该 ID（避免撞 hash/base64）"""
    for _, _, p in entries[:max_files]:
        if p.stem in exclude:
            continue
        try:
            if p.stat().st_size > 32 * 1024 * 1024:
                continue
        except OSError:
            continue
        try:
            with p.open("r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if cid in line and "conversation_id" in line:
                        return p
        except OSError:
            continue
    return None


def locate_cursor_session(cwd, conversation_id, created_ts=None, exclude=(),
                          projects_root=None, max_files=25):
    """轻量定位会话（供 tab 标题同步的后台线程周期调用）。

    与 find_cursor_transcript 的区别：优先只扫当前项目目录、按新旧排序限额，
    并提供「时间窗唯一候选」兜底——transcript 内容常常滞后落盘，
    但 <UUID> 目录在对话开始时就已创建。
    项目目录没命中再全局内容匹配兜底（MCP 上报的 cwd 可能是家目录，slug 对不上）。
    返回 (UUID, transcript路径str) 或 None。
    """
    cid = (conversation_id or "").strip()
    if not cid or cid == "__default__":
        return None
    root = Path(projects_root or (Path.home() / ".cursor" / "projects"))
    d = root / cursor_project_slug(cwd) / "agent-transcripts"
    entries = _main_transcripts(d) if d.is_dir() else []
    hit = _content_match(entries, cid, exclude, max_files)
    if hit:
        return hit.stem, str(hit)
    # 时间窗唯一候选（仅限本项目目录）：会话 tab 创建前 10 分钟内新建、
    # 且未被其他 tab 认领的 transcript 恰好只有一个时认为就是它
    if entries and created_ts:
        cands = [p for _, ct, p in entries
                 if p.stem not in exclude and -30 < created_ts - ct < 600]
        if len({p.stem for p in cands}) == 1:
            return cands[0].stem, str(cands[0])
    # 全局内容匹配兜底：cwd 上报错误（如家目录）时跨项目找
    if root.is_dir():
        seen = {p for _, _, p in entries}
        global_entries = [e for e in _main_transcripts_root(root) if e[2] not in seen]
        hit = _content_match(global_entries, cid, exclude, max_files=40)
        if hit:
            return hit.stem, str(hit)
    return None


def _main_transcripts_root(root):
    entries = []
    for p in root.glob("*/agent-transcripts/*/*.jsonl"):
        if p.parent.name != p.stem:
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        entries.append((st.st_mtime, st.st_ctime, p))
    entries.sort(reverse=True)
    return entries


_PLACEHOLDER_TITLES = {"new chat", "untitled", "新对话", "新聊天"}
_COMPOSER_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_COMPOSER_HEX32_RE = re.compile(r"^[0-9a-f]{32}$", re.I)


def _composer_id_forms(session_uuid):
    raw = (session_uuid or "").strip()
    if not raw:
        return []
    forms = [raw]
    compact = raw.replace("-", "")
    if compact != raw:
        forms.append(compact)
    if _COMPOSER_HEX32_RE.match(compact):
        dashed = "%s-%s-%s-%s-%s" % (
            compact[:8], compact[8:12], compact[12:16], compact[16:20], compact[20:])
        if dashed not in forms:
            forms.append(dashed)
    return forms


def _looks_like_composer_id(session_uuid):
    raw = (session_uuid or "").strip()
    return bool(_COMPOSER_UUID_RE.match(raw) or _COMPOSER_HEX32_RE.match(raw.replace("-", "")))


def read_cursor_title(session_uuid, appdata=None):
    """从 Cursor 全局库读会话标题。

    读法见 _read_composer_json：mode=ro 优先（看得到 WAL 里刚写的名字），打不开再退
    immutable 快照。0902 起「写了名字又被 Cursor 盖回去」这个判断喂给侧栏自动改名，
    读到慢一个 checkpoint 的旧标题就会白抢一次键盘，所以这里也不能只读主文件。
    """
    if not session_uuid:
        return None
    try:
        data = _read_composer_json(session_uuid, appdata)
        if not data:
            return None
        name = (data.get("name") or "").strip()
        if not name or name.lower() in _PLACEHOLDER_TITLES:
            return None
        return name
    except Exception:
        return None


def write_cursor_title(session_uuid, title, appdata=None):
    """把 rxyy-mcp 的 task_name 写回 Cursor 会话落盘标题（重开窗口后能对上）。

    同时改 cursorDiskKV.composerData 和 composerHeaders。假 uuid / 库被锁 /
    没有该键 → False，不抛。这只改磁盘：正在运行的 Cursor 侧栏读的是内存里的
    composerDataService，**不会**因为写库就实时变；而且对话还开着时 Cursor 会把
    内存那份刷回磁盘，连名字一起盖回去（08-27 用户截图里只有两个闲置对话留住了
    真名，其余全被盖成 Persistent plus zhi report）。所以这个函数只保证「下次
    重开窗口时对得上」，别指望它治侧栏。

    要让侧栏当场变，两条路都改内存：
    - cursor-app-control.rename_chat：Cursor 内置工具，走 composerService。但它
      只认调用方自己的 conversationId，且只有 IDE 主对话的工具表里有它，子代理
      调不到（见 MCP 说明第 7 条）。
    - cursor_live_rename：走 Cursor 自己的 composer.renameChat 命令，能指名道姓
      改任何 composerId，谁都不用求。代价是抢一两秒键盘焦点，只做成手动按钮。
    禁止把 Reload Window 当刷新手段（会断 MCP）。
    """
    name = (title or "").strip()[:40]
    if not name or not _looks_like_composer_id(session_uuid):
        return False
    db = _global_db(appdata)
    if db is None:
        return False
    now_ms = int(time.time() * 1000)
    wrote = False
    try:
        con = sqlite3.connect(str(db), timeout=3)
        try:
            for uid in _composer_id_forms(session_uuid):
                key = "composerData:" + uid
                row = con.execute(
                    "SELECT value FROM cursorDiskKV WHERE key=?", (key,)).fetchone()
                if row:
                    try:
                        data = json.loads(row[0])
                    except Exception:
                        data = None
                    if isinstance(data, dict) and (data.get("name") or "").strip() != name:
                        data["name"] = name
                        con.execute(
                            "UPDATE cursorDiskKV SET value=? WHERE key=?",
                            (json.dumps(data, ensure_ascii=False,
                                        separators=(",", ":")), key))
                        wrote = True
                    elif isinstance(data, dict):
                        wrote = True
                head = con.execute(
                    "SELECT value FROM composerHeaders WHERE composerId=?",
                    (uid,)).fetchone()
                if head:
                    try:
                        hdr = json.loads(head[0])
                    except Exception:
                        hdr = None
                    if isinstance(hdr, dict) and (hdr.get("name") or "").strip() != name:
                        hdr["name"] = name
                        con.execute(
                            "UPDATE composerHeaders SET value=?, lastUpdatedAt=? "
                            "WHERE composerId=?",
                            (json.dumps(hdr, ensure_ascii=False,
                                        separators=(",", ":")), now_ms, uid))
                        wrote = True
                    elif isinstance(hdr, dict):
                        wrote = True
            if wrote:
                con.commit()
            return wrote
        finally:
            con.close()
    except Exception:
        return False


def _global_db(appdata=None):
    roaming = Path(appdata or os.environ.get("APPDATA")
                   or Path.home() / "AppData" / "Roaming")
    db = roaming / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    return db if db.is_file() else None


def cursor_conversation_exists(session_uuid, appdata=None):
    """这个对话在 Cursor 的库里还存不存在。返回 True/False；库不可用时返回 None。

    会话可能被用户删掉、或被 Cursor 政策直接回收——composerData 键整个消失，
    一条报错都不留（07-31 用户实测：对话没了，控制台还蒙在鼓里）。
    这种死法只能靠「以前验过身、现在键没了」来判，别的证据全是空白。
    """
    if not session_uuid:
        return None
    db = _global_db(appdata)
    if db is None:
        return None
    try:
        con = sqlite3.connect(
            "file:{}?immutable=1".format(str(db).replace("\\", "/")), uri=True, timeout=2)
        try:
            row = con.execute("SELECT 1 FROM cursorDiskKV WHERE key=?",
                              ("composerData:" + session_uuid,)).fetchone()
        finally:
            con.close()
    except Exception:
        return None
    return bool(row)


# ---------------- 转世对话定位（云端/后台 agent 判活断层的根治） ----------------
# 08-28 实案：Cursor 窗口 16:35 重载后，正在跑的云端 agent 对话「转世」——同一个
# 对话换了新 composer uuid 继续干活，旧 uuid 的 composerData 被整个回收。hub 只在
# zhi/zt 到达时校准身份，而长跑 agent 可能几小时不碰 MCP：音视频编辑·env审计·30d6
# 的 agent 一直在写文件，控制台却顶着「对话已从 Cursor 里消失」在待续组躺了 5 小时。
# 好在转世不改户口，本机还留着两样一手证据：
#   ① transcript 存根：新对话的 jsonl 开头仍是「原始第一条用户消息」，里面有
#      rxyy-mcp 的 conversation_id（报到词/接手词必带「xxxxxxxx」）；
#   ② AI 改档流水 ~/.cursor/ai-tracking/ai-code-tracking.db：按新 uuid 记着它
#      此刻还在改哪个文件（composerData 冻结/回收后它照记不误）。
# 两样凑齐 = 不需要 agent 配合就能把会话换绑到转世后的对话上。

_STUB_CONV_RE = (
    re.compile(r"「([0-9a-f]{8})」"),
    re.compile(r"conversation_id[=:：\s「']*([0-9a-f]{8})"),
    re.compile(r"对话\s*([0-9a-f]{8})"),
)
_STUB_SCAN_CACHE = {"ts": 0.0, "map": {}}
_STUB_SCAN_TTL = 60.0
_STUB_HEAD_BYTES = 8192       # 户口在首条用户消息里，读个头就够
_STUB_MAX_AGE_DAYS = 14


def stub_conv_ids(text):
    """从 transcript 存根头部文本里抠出所有rxyy-mcp conversation_id（8 位 hex）。"""
    ids = set()
    for rx in _STUB_CONV_RE:
        ids.update(m.lower() for m in rx.findall(text or ""))
    return ids


def scan_agent_stub_convs(projects_root=None, now=None):
    """扫 ~/.cursor/projects/*/agent-transcripts 里每个对话存根的开头，提取其中的
    rxyy-mcp conversation_id。返回 {uuid: {"convs": set, "path": str, "mtime": float}}。
    默认根目录下带 60s 缓存；单轮几十个文件、每个只读头 8KB，开销可忽略。"""
    now = now or time.time()
    if projects_root is None and now - _STUB_SCAN_CACHE["ts"] < _STUB_SCAN_TTL:
        return _STUB_SCAN_CACHE["map"]
    root = Path(projects_root or (Path.home() / ".cursor" / "projects"))
    out = {}
    cutoff = now - _STUB_MAX_AGE_DAYS * 86400
    try:
        paths = list(root.glob("*/agent-transcripts/*/*.jsonl"))
    except OSError:
        paths = []
    for path in paths:
        try:
            st = path.stat()
        except OSError:
            continue
        if st.st_mtime < cutoff:
            continue
        try:
            with path.open("rb") as fh:
                head = fh.read(_STUB_HEAD_BYTES).decode("utf-8", "replace")
        except OSError:
            continue
        convs = stub_conv_ids(head)
        if not convs:
            continue
        uuid = path.stem
        cur = out.get(uuid)
        if cur is None or st.st_mtime > cur["mtime"]:
            out[uuid] = {"convs": convs, "path": str(path), "mtime": st.st_mtime}
    if projects_root is None:
        _STUB_SCAN_CACHE["ts"] = now
        _STUB_SCAN_CACHE["map"] = out
    return out


def _tracking_db(home=None):
    base = Path(home) if home else Path.home()
    db = base / ".cursor" / "ai-tracking" / "ai-code-tracking.db"
    return db if db.is_file() else None


_EDIT_TS_CACHE = {"ts": 0.0, "map": {}}
_EDIT_TS_TTL = 20.0
_EDIT_TS_WINDOW_H = 48


def agent_edit_ts_map(home=None, now=None):
    """AI 改档流水里近 48h 每个对话最近一次写文件的时刻 {uuid: epoch 秒}。

    这本流水是转世/云端对话在本机唯一持续更新的活证：composerData 被回收、
    transcript 存根冻结后，agent 只要还在改文件这里就有记录。conversationId
    列没索引，全表扫一次实测 0.33s——所以默认带 20s 缓存，判活/判死路径
    只查缓存好的字典。"""
    now = now or time.time()
    if home is None and now - _EDIT_TS_CACHE["ts"] < _EDIT_TS_TTL:
        return _EDIT_TS_CACHE["map"]
    db = _tracking_db(home)
    out = {}
    if db is not None:
        cut = int((now - _EDIT_TS_WINDOW_H * 3600) * 1000)
        # mode=ro 尊重日志/锁；写入间隙偶发 locked 时退 immutable 快照读
        for uri in ("?mode=ro", "?immutable=1"):
            try:
                con = sqlite3.connect(
                    "file:{}{}".format(str(db).replace("\\", "/"), uri),
                    uri=True, timeout=2)
                try:
                    rows = con.execute(
                        "SELECT conversationId, MAX(timestamp) FROM ai_code_hashes "
                        "WHERE timestamp > ? GROUP BY conversationId",
                        (cut,)).fetchall()
                finally:
                    con.close()
                out = {str(cid): float(ts) / 1000.0
                       for cid, ts in rows if cid and ts}
                break
            except Exception:
                continue
    if home is None:
        _EDIT_TS_CACHE["ts"] = now
        _EDIT_TS_CACHE["map"] = out
    return out


def agent_last_edit_ts(session_uuid, home=None):
    """这个对话最近一次 AI 写文件的时刻（epoch 秒）；近 48h 无记录返回 0。"""
    if not session_uuid:
        return 0.0
    try:
        return float(agent_edit_ts_map(home).get(str(session_uuid), 0.0) or 0.0)
    except Exception:
        return 0.0


# 模型名在侧栏里只有几十像素可用。厂商前缀对区分毫无帮助（同屏全是 claude-），
# 砍掉才看得见真正区分度所在的那一半。其余厂商（composer/grok/gpt/gemini）本来
# 就短，原样显示。
_MODEL_PREFIXES = ("claude-", "anthropic/", "openai/", "google/", "xai/")
# Cursor 里选「Auto」时 modelConfig.modelName 落的是这个字面量，不是真实模型名
_MODEL_AUTO = "default"


def model_label(model_name, max_mode=False):
    """`claude-opus-5` + maxMode → `opus-5 max`；Auto 档如实说是 Auto。"""
    raw = str(model_name or "").strip()
    if not raw:
        return ""
    if raw.lower() == _MODEL_AUTO:
        short = "Auto"
    else:
        short = raw
        for p in _MODEL_PREFIXES:
            if short.lower().startswith(p):
                short = short[len(p):]
                break
    return short + (" max" if max_mode else "")


def model_info_from_name(model_name, effort=""):
    """把任意模型字符串收成面板用的 {model, label, max, effort}。"""
    name = str(model_name or "").strip()
    if not name:
        return None
    return {"model": name, "label": model_label(name, False),
            "max": False, "effort": str(effort or "")}


_CODEX_MODEL_CACHE = {"ts": 0.0, "info": None, "path": ""}
_CODEX_MODEL_TTL = 5.0
_TOML_STR = re.compile(r'^([A-Za-z0-9_]+)\s*=\s*"([^"]*)"')


def read_codex_model(config_path=None):
    """Codex 没有 Cursor 那种 composerData。默认模型写在 ~/.codex/config.toml 顶层。

    08-26 用户：Codex 里挂rxyy-mcp 的 tab 模型牌永远空白。tick_model_probe 以前
    在 cursor_uuid 为空时直接 return，这块永远不会亮。顶层 `model = "..."` 是
    主任务当前档；[agents.*] 里的是子代理，不能拿来冒充本 tab。
    """
    path = Path(config_path) if config_path else (Path.home() / ".codex" / "config.toml")
    now = time.time()
    cache_ok = (config_path is None
                and _CODEX_MODEL_CACHE["path"] == str(path)
                and now - _CODEX_MODEL_CACHE["ts"] < _CODEX_MODEL_TTL)
    if cache_ok:
        return _CODEX_MODEL_CACHE["info"]
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        info = None
    else:
        name, effort = "", ""
        for ln in text.splitlines():
            s = ln.strip()
            if not s or s.startswith("#"):
                continue
            if s.startswith("["):
                break
            m = _TOML_STR.match(s)
            if not m:
                continue
            if m.group(1) == "model":
                name = m.group(2).strip()
            elif m.group(1) == "model_reasoning_effort":
                effort = m.group(2).strip()
        info = model_info_from_name(name, effort)
    if config_path is None:
        _CODEX_MODEL_CACHE.update(ts=now, info=info, path=str(path))
    return info


def _read_composer_json(session_uuid, appdata=None):
    """单键读 composerData:<uuid>，返回解析后的 dict；没有 / 读不到 / 不是 JSON → None。

    先 `mode=ro`，打不开或 locked 再退 `immutable=1`。顺序不能反：immutable 只读
    主文件、不看 `-wal`，Cursor 刚写的那几 MB 帧要等它下一次 checkpoint 才落主文件。
    0902 实测同一键两种模式并读，气泡数 99 vs 101 / 176 vs 181——immutable 就是慢
    一个 checkpoint。模型牌吃这口数据最疼：新建对话时继承选择器上的旧档、用户随手
    切成新档，切档那笔停在 WAL 里，控制台就挂着旧档一两分钟（rxyy 09-02 截图：五个
    待命壳全显示 grok-4.6，其中 6f9d 实为 claude-fable-5-1）。08-25/08-26 两次
    「牌更新慢」把探测间隔从 60s 压到 8s 也没治好，因为源头就是滞后的。
    `mode=ro` 走正常锁与 WAL 索引，单键查询实测 2~14ms，与 immutable 同量级。
    """
    db = _global_db(appdata)
    if db is None:
        return None
    key = "composerData:" + session_uuid
    path = str(db).replace("\\", "/")
    for uri in ("?mode=ro", "?immutable=1"):
        try:
            con = sqlite3.connect("file:{}{}".format(path, uri), uri=True, timeout=2)
            try:
                row = con.execute("SELECT value FROM cursorDiskKV WHERE key=?",
                                  (key,)).fetchone()
            finally:
                con.close()
        except Exception:
            continue
        if not row:
            return None
        try:
            data = json.loads(row[0])
        except Exception:
            return None
        return data if isinstance(data, dict) else None
    return None


def read_cursor_model(session_uuid, appdata=None):
    """这个对话此刻用的是哪个模型（Cursor 自己记在 composerData.modelConfig 里）。

    用户 08-24：一排 tab 只看名字分不出谁是 opus、谁是 composer，出了问题不知道
    该换谁。模型没有任何一条通道会主动告诉控制台——MCP 那头是独立进程、hook 的
    payload 里 model 字段实测恒为空——只有 Cursor 自己的库里有。

    单键查询（约 1ms），**绝不能全表扫**：Cursor 正在写 WAL 时 immutable=1 全表
    扫会直接抛 "database disk image is malformed"（08-24 实测），单键则稳。
    读法见 _read_composer_json：mode=ro 优先，否则刚切的档要等 checkpoint 才看得见。

    返回 {"model": 原始名, "label": 短名, "max": bool, "effort": ...} 或 None。
    """
    if not session_uuid:
        return None
    data = _read_composer_json(session_uuid, appdata)
    if data is None:
        return None
    cfg = data.get("modelConfig") or {}
    if not isinstance(cfg, dict):
        return None
    name = str(cfg.get("modelName") or "").strip()
    if not name:
        return None
    max_mode = bool(cfg.get("maxMode"))
    effort = ""
    for m in cfg.get("selectedModels") or []:
        if not isinstance(m, dict) or str(m.get("modelId") or "") != name:
            continue
        for p in m.get("parameters") or []:
            if isinstance(p, dict) and p.get("id") == "effort":
                effort = str(p.get("value") or "")
    return {"model": name, "label": model_label(name, max_mode),
            "max": max_mode, "effort": effort}


def read_cursor_activity(session_uuid, appdata=None):
    """从 Cursor 自己的库里读某个对话此刻的活动状态——比猜 transcript 文件时间准。

    composerData 里 `generatingBubbleIds` 非空 = Cursor 正在生成（铁证）；
    时间戳取 lastUpdatedAt 与 conversationCheckpointLastUpdatedAt 的较新者
    （实测 lastUpdatedAt 常滞后半小时，checkpoint 才跟得上实时）。
    单键查询在 6.6GB 的库上实测 0.5~0.9ms，可以按需高频调用。
    """
    if not session_uuid:
        return None
    db = _global_db(appdata)
    if db is None:
        return None
    try:
        con = sqlite3.connect(
            "file:{}?immutable=1".format(str(db).replace("\\", "/")), uri=True, timeout=2)
        try:
            row = con.execute("SELECT value FROM cursorDiskKV WHERE key=?",
                              ("composerData:" + session_uuid,)).fetchone()
        finally:
            con.close()
    except Exception:
        return None
    if not row:
        return None
    try:
        d = json.loads(row[0])
    except Exception:
        return None
    updated = max(float(d.get("lastUpdatedAt") or 0),
                  float(d.get("conversationCheckpointLastUpdatedAt") or 0))
    return {
        "uuid": session_uuid,
        "name": (d.get("name") or "").strip(),
        "subtitle": (d.get("subtitle") or "").strip(),
        "status": (d.get("status") or "").strip(),
        "generating": len(d.get("generatingBubbleIds") or []),
        "queued": len(d.get("queueItems") or []),
        "unread": bool(d.get("hasUnreadMessages")),
        "updated_ts": updated / 1000.0 if updated else 0.0,
    }


_TURN_STATE_CACHE = {}
_MENTION_CACHE = {}


_TOOL_ARG_CACHE = {}


def _json_has_mcp_conv(obj, cid):
    """结构化认领：只算 zhi/zt/ji / CallMcpTool 的 conversation_id 参数。

    不用正则扫全文——agent 的 Shell 命令、探针脚本、接手提示词正文里都会
    出现 `"conversation_id": "xxxx"` 字样，扫全文会把隔壁窗口误绑过来。
    """
    if isinstance(obj, dict):
        if obj.get("name") == "CallMcpTool":
            inp = obj.get("input") if isinstance(obj.get("input"), dict) else {}
            args = inp.get("arguments") if isinstance(inp.get("arguments"), dict) else {}
            if args.get("conversation_id") == cid:
                return True
        name = str(obj.get("name") or obj.get("toolName") or "")
        if name in ("zhi", "zt", "ji") and obj.get("conversation_id") == cid:
            return True
        inp = obj.get("input") if isinstance(obj.get("input"), dict) else {}
        if str(inp.get("toolName") or "") in ("zhi", "zt", "ji") \
                and (inp.get("conversation_id") == cid
                     or (isinstance(inp.get("arguments"), dict)
                         and inp["arguments"].get("conversation_id") == cid)):
            return True
        return any(_json_has_mcp_conv(v, cid) for v in obj.values())
    if isinstance(obj, list):
        return any(_json_has_mcp_conv(v, cid) for v in obj)
    return False


def transcript_has_tool_arg(transcript_path, conversation_id, tail_bytes=524288):
    """jsonl 里是否出现过「用这个 conversation_id 调了 zhi/zt/ji」。

    composerData 气泡通常不含 MCP 工具 JSON（08-15 实测 a3fffc60：流水有
    CallMcpTool arguments，composer 气泡没有），收壳/重定位必须查 jsonl。
    """
    cid = str(conversation_id or "").strip()
    if not transcript_path or not cid or cid == "__default__":
        return False
    try:
        p = Path(transcript_path)
        st = p.stat()
    except OSError:
        return False
    key = (str(p), st.st_mtime_ns, st.st_size, cid)
    hit = _TOOL_ARG_CACHE.get(key)
    if hit is not None:
        return hit
    found = False
    try:
        with open(p, "rb") as f:
            if st.st_size > tail_bytes:
                f.seek(-tail_bytes, 2)
            blob = f.read().decode("utf-8", "ignore")
        for line in blob.splitlines():
            line = line.strip()
            if not line or cid not in line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if _json_has_mcp_conv(data, cid):
                found = True
                break
    except OSError:
        found = False
    if len(_TOOL_ARG_CACHE) > 512:
        _TOOL_ARG_CACHE.clear()
    _TOOL_ARG_CACHE[key] = found
    return found


def jsonl_path_for_uuid(cwd, session_uuid, projects_root=None):
    uid = str(session_uuid or "").strip()
    if not uid:
        return None
    root = Path(projects_root or (Path.home() / ".cursor" / "projects"))
    p = root / cursor_project_slug(cwd) / "agent-transcripts" / uid / (uid + ".jsonl")
    return str(p) if p.is_file() else None


def jsonl_tool_arg_session_for_conv(cwd, conversation_id, exclude=(),
                                    within_secs=1800, projects_root=None,
                                    appdata=None):
    """在本工作区 jsonl 里找「用这个 ID 调过 zhi/zt」且最近还在写的对话。

    只认 CallMcpTool 工具参数，不认接手提示词正文。jsonl 要等 turn 结束才
    刷盘（08-15 实测长 turn 里 mtime 停在 40 分钟前，composer checkpoint
    仍是十几秒前）——mtime 超窗时改看 composer 活性，昨天已 aborted 的前任
    （9bca27fc）两边都凉，仍排除。
    返回 (uuid, path, {"updated_ts": recency}) 或 None。
    """
    cid = (conversation_id or "").strip()
    if not cid or cid == "__default__":
        return None
    root = Path(projects_root or (Path.home() / ".cursor" / "projects"))
    d = root / cursor_project_slug(cwd) / "agent-transcripts"
    if not d.is_dir():
        return None
    now = time.time()
    best = None
    for mtime, _, path in _main_transcripts(d):
        uid = path.stem
        if uid in exclude:
            continue
        recency = mtime
        if now - mtime > within_secs:
            act = read_cursor_activity(uid, appdata)
            recency = float((act or {}).get("updated_ts") or 0)
            if now - recency > within_secs:
                continue
        if not transcript_has_tool_arg(str(path), cid):
            continue
        if best is None or recency > best[2]["updated_ts"]:
            best = (uid, str(path), {"updated_ts": recency})
    return best


def transcript_mentions(transcript_path, needle):
    """这份流水里有没有出现过 needle（如本对话的 conversation_id）。

    用途：验明流水正身。transcript 定位有一条「就近猜」的兜底（freshest_active_session），
    猜错时把别人的收工文件当成这个会话的，会产生一脸自信的误判（07-31 实测：
    cursor工作流1 被隔壁死会话的 turn_ended 判成「已中断」）。agent 调 zhi 时
    conversation_id 必然进工具参数、随流水落盘——文件里找得到这个 ID 才算它的。
    全文件扫一次，按 (路径, mtime, size) 缓存；收工的文件不再变，只扫一遍。"""
    needle = str(needle or "").strip()
    if not transcript_path or not needle or needle == "__default__":
        return False
    try:
        p = Path(transcript_path)
        st = p.stat()
    except OSError:
        return False
    key = (str(p), st.st_mtime_ns, st.st_size, needle)
    hit = _MENTION_CACHE.get(key)
    if hit is not None:
        return hit
    found = False
    try:
        with open(p, "rb") as f:
            blob = f.read()
        found = needle.encode("utf-8") in blob
    except OSError:
        found = False
    if len(_MENTION_CACHE) > 512:
        _MENTION_CACHE.clear()
    _MENTION_CACHE[key] = found
    return found


def read_turn_state(transcript_path, tail_bytes=16384):
    """Cursor 对话流水（jsonl）尾部的最后一个事件是不是 turn_ended。

    这是「agent 本轮是否已结束」的直接证据，不用问 agent 也不用猜时间：
    - 最后一行是 {"type":"turn_ended",...} → 这轮真收工了，它不会再自己说话
      （新一轮开始时 Cursor 会立刻往后追加新行，turn_ended 就不再是最后一行）
    - 返回 (state, status)：state 为 "ended"/"running"/None（判断不了），
      status 为 turn_ended 里的 status（completed/error/aborted…）
    """
    if not transcript_path:
        return None, ""
    try:
        p = Path(transcript_path)
        st = p.stat()
    except OSError:
        return None, ""
    key = (str(p), st.st_mtime_ns, st.st_size)
    hit = _TURN_STATE_CACHE.get(key)
    if hit is not None:
        return hit
    try:
        with open(p, "rb") as f:
            f.seek(max(0, st.st_size - tail_bytes))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return None, ""
    result = (None, "")
    for line in tail.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue  # 尾块可能把第一行截断，跳过残行
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("type") == "turn_ended":
            result = ("ended", str(d.get("status") or ""))
        else:
            result = ("running", "")
    if len(_TURN_STATE_CACHE) > 256:
        _TURN_STATE_CACHE.clear()
    _TURN_STATE_CACHE[key] = result
    return result


# Cursor 报错码 → 人话死因。会话因这些原因死掉时界面上一点动静都没有，
# agent 也不会再调 zhi——只能由控制台主动去 Cursor 库里查。
# （码值取自本机历史实测：50 欠费 22 次、51 额度到顶 4 次、13 被 Anthropic 拒 4 次…）
_ERROR_REASONS = {
    50: ("账号欠费", "Cursor 账号有未支付账单，去 dashboard 付清才能继续发请求"),
    51: ("额度到顶", "这个账号的用量已到上限，得换号或等额度重置"),
    13: ("被模型方拒绝", "请求被 Anthropic 拦了（策略/风控），换个模型或稍后重试"),
    64: ("模型不可用", "当前地区不支持这个模型供应商，换模型"),
    38: ("登录失效", "请求未授权，多半是 Cursor 登录态掉了，重新登录"),
}
_ERROR_REASONS_BY_NAME = {
    "ERROR_EXTENSION_HOST_TIMEOUT": ("扩展宿主超时", "Cursor 扩展宿主没响应，重载窗口即可"),
}

# 平台并发闸（08-27 11:42-12:10 事故，hub 日志实测三种原话）：5+ tab 同时
# 派活/接手时 Cursor 拒开新会话——「会话建立失败（错误码：1001）」「会话超过
# 最大限制 4/4」「每分钟请求上限」。这不是哪个 agent 坏了，是门口排队：空位
# 一出平台自动重试就进得去。此前它们被当成普通死因走判死（退卡+推「挂了」+
# 工作流重派），重派又开新会话，越派越堵——接手 c0cfa23a 的 agent 连败 57 次、
# 26 分钟后 tab 判死，用户看到的就是「消息发出去了没成功」。
_GATE_MARKS = (
    "会话建立失败", "超过最大限制", "每分钟请求", "请求上限", "请求过于频繁",
    "concurrent session", "maximum number of", "session limit",
    "rate limit", "too many requests",
)

# AI 网关（bajie 会员网关 curvsix，Cursor 的 api2 流量经它中转）的并发闸长得不一样：
# 不是 errorDetails，而是一条**正文气泡**「并发窗口已满（2/2）：该口令同时打开的窗口
# 已达上限，请关闭其他窗口后重试」，对话就此 completed、**不会自动重试**。09-03 17:25
# 无头开待命对话实测撞上（本会话 + 另一个 agent 正占着两个名额），hub 只看 errorDetails
# 于是壳 tab 一直「重连中」、没人知道它根本没跑起来。
_GATEWAY_TEXT_MARKS = (
    "并发窗口已满", "同时打开的窗口已达上限", "窗口已达上限", "请关闭其他窗口",
)
# 网关拒稿的正文很短；agent 正常回复里引用这些字样时篇幅远超此数，不会误判
_GATEWAY_TEXT_MAX_CHARS = 200
# 只对这么小的最后一条气泡多解一次 JSON（正常回复动辄几十 KB，不白解）
_GATEWAY_BUBBLE_MAX_BYTES = 8192


def _looks_like_gate(*chunks):
    blob = " ".join(str(c or "") for c in chunks).lower()
    return any(m in blob for m in _GATE_MARKS)


def gateway_text_rejection(bubble):
    """这条 AI 气泡是不是「AI 网关并发满」的拒稿正文；是则返回死因 dict，否则 None。

    只认：AI 侧（type 2）、纯正文（无工具调用、无思考）、很短、命中网关原话。
    """
    if not isinstance(bubble, dict) or bubble.get("type") != 2:
        return None
    if bubble.get("toolFormerData") or bubble.get("thinking") or bubble.get("errorDetails"):
        return None
    text = str(bubble.get("text") or "").strip()
    if not text or len(text) > _GATEWAY_TEXT_MAX_CHARS:
        return None
    if not any(m in text for m in _GATEWAY_TEXT_MARKS):
        return None
    return {"code": "GATEWAY_CONCURRENCY", "reason": "AI 网关并发满（口令名额用完，不会自动重试）",
            "advice": "", "title": "网关拒稿", "detail": text,
            "retryable": True, "gate": True, "gate_kind": "gateway_text"}


def _classify_error(err):
    """把 bubble 里的 errorDetails.error 归成 (死因, 建议, 码)。"""
    if isinstance(err, dict):
        code = err.get("error")
        details = err.get("details") or {}
        title = str(details.get("title") or "").strip()
        detail = str(details.get("detail") or "").strip()
        retryable = bool(details.get("isRetryable"))
    else:
        code, title, detail, retryable = err, "", "", False
    if _looks_like_gate(title, detail, code):
        # gate=True 是下游三处的分岔依据：不退卡（_is_transient_death）、
        # 不推骷髅/不给一键派单（alert_session_death）、面板显示排队而非已挂
        # （agent_liveness / get_state）。原始 title/detail 保留作证据。
        return {"code": code, "reason": "Cursor 并发满/限频（排队等空位）",
                "advice": "", "title": title, "detail": detail,
                "retryable": True, "gate": True}
    known = None
    if isinstance(code, int):
        known = _ERROR_REASONS.get(code)
    elif isinstance(code, str):
        known = _ERROR_REASONS_BY_NAME.get(code)
    reason, advice = known if known else (title or "报错中断", detail)
    return {"code": code, "reason": reason, "advice": advice or detail,
            "title": title, "detail": detail, "retryable": retryable}


def read_cursor_error(session_uuid, appdata=None, tail=3):
    """查这个对话最后是不是「死在一条报错上」。

    Cursor 把每次生成的失败原因原样写进 bubble 的 errorDetails（欠费/额度到顶/
    被 Anthropic 拒/区域不支持…），而且这条报错气泡就是对话的最后一条——
    实测本机历史 39 个中断会话全部如此。扫尾部 3 条 bubble，单次约 1ms。

    返回 {reason, advice, code, title, detail, at_ts, is_last, retryable} 或 None。
    """
    if not session_uuid:
        return None
    db = _global_db(appdata)
    if db is None:
        return None
    try:
        con = sqlite3.connect(
            "file:{}?immutable=1".format(str(db).replace("\\", "/")), uri=True, timeout=2)
        try:
            row = con.execute("SELECT value FROM cursorDiskKV WHERE key=?",
                              ("composerData:" + session_uuid,)).fetchone()
            if not row:
                return None
            heads = (json.loads(row[0]).get("fullConversationHeadersOnly") or [])
            if not heads:
                return None
            last_id = heads[-1].get("bubbleId")
            for h in reversed(heads[-max(1, int(tail)):]):
                bid = h.get("bubbleId")
                brow = con.execute(
                    "SELECT value FROM cursorDiskKV WHERE key=?",
                    ("bubbleId:{}:{}".format(session_uuid, bid),)).fetchone()
                if not brow:
                    continue
                if '"errorDetails"' not in brow[0]:
                    # 网关拒稿没有 errorDetails，是最后一条很短的正文。只在「最后一条
                    # 且这条气泡本身很小」时才解一次 JSON（不能按原始串找关键词：
                    # 中文在库里可能是 \uXXXX 转义形态，逐字节匹配会漏）
                    if bid == last_id and len(brow[0]) <= _GATEWAY_BUBBLE_MAX_BYTES:
                        bubble = json.loads(brow[0])
                        info = gateway_text_rejection(bubble)
                        if info:
                            return {**info, "bubble_id": bid, "is_last": True,
                                    "at_ts": _iso_to_ts(bubble.get("createdAt")),
                                    "request_id": ""}
                    continue
                bubble = json.loads(brow[0])
                ed = bubble.get("errorDetails")
                if not ed:
                    continue
                info = _classify_error(ed.get("error"))
                return {**info, "bubble_id": bid, "is_last": bid == last_id,
                        "at_ts": _iso_to_ts(bubble.get("createdAt")),
                        "request_id": ed.get("requestId", "")}
        finally:
            con.close()
    except Exception:
        return None
    return None


def _iso_to_ts(text):
    """'2026-07-30T08:15:41.349Z' → epoch 秒；解析不了给 0。"""
    if not text:
        return 0.0
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0


def freshest_active_session(cwd, exclude=(), within_secs=300, appdata=None,
                            projects_root=None):
    """本项目里「Cursor 侧刚刚还在动」的对话，用于纠正 tab 与对话的错配。

    按 conversation_id 搜正文经常搜不到（agent 只把 ID 写在工具参数里，不落进
    transcript 正文），于是接手过的 tab 会一直指着上一任的旧文件。这里换个思路：
    在本项目的候选里挑 Cursor 自己记录的「最近有动作」的那个。
    返回 (uuid, transcript路径str, 活动信息) 或 None。
    """
    root = Path(projects_root or (Path.home() / ".cursor" / "projects"))
    d = root / cursor_project_slug(cwd) / "agent-transcripts"
    if not d.is_dir():
        return None
    best = None
    now = time.time()
    for _, _, path in _main_transcripts(d):
        uid = path.stem
        if uid in exclude:
            continue
        act = read_cursor_activity(uid, appdata)
        if not act or not act["updated_ts"]:
            continue
        age = now - act["updated_ts"]
        if age > within_secs:
            continue
        if best is None or act["updated_ts"] > best[2]["updated_ts"]:
            best = (uid, str(path), act)
    return best


def generating_now_session(cwd, exclude=(), appdata=None, projects_root=None):
    """此刻本工作区里「正在生成」的 Cursor 会话；恰好一个时返回 (uuid, path, act)。

    用途：身份自校准。agent 调 zhi/zt 的那一瞬，它的 Cursor 会话必然处于生成中
    （generatingBubbleIds 非空是 Cursor 库里的硬事实）——工作区里未被其他 tab
    认领、且正在生成的会话恰好只有一个时，叫话的就是它。比按 conversation_id
    搜正文可靠（agent 常不把 ID 落进正文），也比「谁最新算谁」的猜测硬。
    同时有多个在生成时返回 None（分不清，宁可不动）。"""
    root = Path(projects_root or (Path.home() / ".cursor" / "projects"))
    d = root / cursor_project_slug(cwd) / "agent-transcripts"
    if not d.is_dir():
        return None
    hits = []
    for _, _, path in _main_transcripts(d):
        uid = path.stem
        if uid in exclude:
            continue
        act = read_cursor_activity(uid, appdata)
        if act and act.get("generating"):
            hits.append((uid, str(path), act))
            if len(hits) > 1:
                return None
    return hits[0] if len(hits) == 1 else None


def generating_session_for_conv(cwd, conversation_id, exclude=(), appdata=None,
                                projects_root=None):
    """多个对话同时在生成、generating_now_session 因「不唯一」放弃时，用
    conversation_id 正文精确认领本会话的对话文件。

    接手 / 多 agent 并发同工作区的场景：好几个对话都在生成，「唯一在生成」不成立；
    但每个 agent 调 zhi/zt 时把自己的 conversation_id 写进工具参数、随流水落盘，
    该 ID 只出现在它自己那个对话文件里，据此把叫话的会话从一堆在生成的对话里摘出。
    候选按 mtime 新→旧，命中第一个即返回（同一 ID 若新旧文件都在生成，取活跃的新文件）。
    返回 (uuid, path, act) 或 None。"""
    cid = (conversation_id or "").strip()
    if not cid or cid == "__default__":
        return None
    root = Path(projects_root or (Path.home() / ".cursor" / "projects"))
    d = root / cursor_project_slug(cwd) / "agent-transcripts"
    if not d.is_dir():
        return None
    for _, _, path in _main_transcripts(d):
        uid = path.stem
        if uid in exclude:
            continue
        act = read_cursor_activity(uid, appdata)
        if not (act and act.get("generating")):
            continue
        if transcript_mentions(str(path), cid):
            return uid, str(path), act
    return None


def _cursor_db_conn(appdata=None):
    db = _global_db(appdata)
    if db is None:
        return None
    try:
        return sqlite3.connect(
            "file:{}?immutable=1".format(str(db).replace("\\", "/")), uri=True, timeout=2)
    except Exception:
        return None


def _conv_matcher(cid, param_context):
    """返回「这段 composer 文本算不算提到了 cid」的判定函数。

    param_context=False：裸包含（在已确认是本会话的对话里找空壳报到 ID 用，够准）。
    param_context=True：要求 cid 紧跟在 conversation_id 字样后 ≤24 字符内——
    即 zhi/zt 工具参数（"conversation_id":"xxx"，含 JSON 转义）、报到词
    （conversation_id=「xxx」）或接手提示词（conversation_id 必须改用「xxx」）
    的形态。跨对话认领必须用这一档：队友通知/zt 动态里满天飞的裸 ID（08-03
    实测「待命2：接手27322a27…」的 zt 动态随团队快照进了别人的对话）会把
    裸包含污染成错绑。
    param_context="tool_arg"：只认 JSON 工具参数 `"conversation_id": "cid"`。
    收壳反查必须用这一档——接手提示词正文里本来就有 conversation_id=「原ID」，
    用 True 会把「贴提示词当上下文、明确不用接手」的 tab 误收掉（08-14 2cf0）。

    连接段一律不许出现叙述性汉字（白名单短语除外）：08-12 实测，排查串台的
    agent 对话里写了一句「OA对接 的 conversation_id是1d07989b」，就这一个
    「是」字让它被认成 OA对接 的接手方，两个活 tab 身份互抢半小时。活性闸
    （hub._verify_identity_by_generating）挡住了活人被抢，这里把污染源也堵上
    ——安静超窗的原主仍可能被叙述句错绑。白名单（必须改用/固定用/沿用/改用）
    是报到与接手提示词的既有措辞，不能误杀。"""
    if not param_context:
        return lambda text: cid in (text or "")
    if param_context == "tool_arg":
        rx = re.compile(
            r"""["']conversation_id["']\s*:\s*["']""" + re.escape(cid) + r"""["']""")
        return lambda text: bool(rx.search(text or ""))
    rx = re.compile(
        "conversation_id(?:\\s*(?:必须改用|固定用|沿用|改用))?"
        "[^\\u4e00-\\u9fff]{0,24}?" + re.escape(cid), re.DOTALL)
    return lambda text: bool(rx.search(text or ""))


def composer_mentions_conv(uuid, conversation_id, con=None, appdata=None, tail=16,
                           param_context=False):
    """Cursor 实时库(composerData)里这个对话是否已含该 conversation_id。

    比 transcript jsonl 早：用户贴的接手提示词、agent 刚调的 zhi 参数会立即成为
    composerData 的 bubble，而 agent-transcript jsonl 要等整个 turn 结束才落盘
    （接手 agent 首个长 turn 里 jsonl 根本不含新 ID）。查 composerData 顶层
    （name/headers/queue）+ 尾部 tail 个 bubble 正文。
    param_context 语义见 _conv_matcher。"""
    cid = (conversation_id or "").strip()
    if not uuid or not cid or cid == "__default__":
        return False
    match = _conv_matcher(cid, param_context)
    own = con is None
    if own:
        con = _cursor_db_conn(appdata)
        if con is None:
            return False
    try:
        row = con.execute("SELECT value FROM cursorDiskKV WHERE key=?",
                          ("composerData:" + uuid,)).fetchone()
        if not row:
            return False
        if match(row[0] or ""):
            return True
        heads = json.loads(row[0]).get("fullConversationHeadersOnly") or []
        for h in heads[-tail:]:
            bid = h.get("bubbleId")
            br = con.execute("SELECT value FROM cursorDiskKV WHERE key=?",
                            ("bubbleId:{}:{}".format(uuid, bid),)).fetchone()
            if br and match(br[0] or ""):
                return True
        return False
    except Exception:
        return False
    finally:
        if own:
            try:
                con.close()
            except Exception:
                pass


def cursor_db_session_for_conv(cwd, conversation_id, exclude=(), within_secs=1800,
                               appdata=None, projects_root=None, param_context=True):
    """用 Cursor 实时库(composerData)把 conversation_id 认到「接手者当前对话」。

    这是接手落地最可靠的信号：composerData 是 Cursor 边写边存的实时库，用户刚贴的
    接手提示词、agent 刚调的 zhi 参数立即可查；而 transcript jsonl 要等 turn 结束才
    落盘，且被接手的原 ID 会同时出现在一堆历史对话文件里（按 mtime 常命中前任旧对话）。
    这里只认「最近 within_secs 内还活跃」的对话——前任死对话虽也含这个 ID，但早不动了，
    天然被时间窗排除。多个活跃命中取 updated_ts 最新的（接手者当前对话）。
    匹配用 param_context 档（见 _conv_matcher）：跨对话认领只信 conversation_id
    参数/接手提示词形态的出现，不吃队友通知里裸 ID 的污染（08-03 错绑根因之一）。
    返回 (uuid, path, act) 或 None。"""
    cid = (conversation_id or "").strip()
    if not cid or cid == "__default__":
        return None
    root = Path(projects_root or (Path.home() / ".cursor" / "projects"))
    d = root / cursor_project_slug(cwd) / "agent-transcripts"
    if not d.is_dir():
        return None
    con = _cursor_db_conn(appdata)
    if con is None:
        return None
    now = time.time()
    best = None
    try:
        for _, _, path in _main_transcripts(d):
            uid = path.stem
            if uid in exclude:
                continue
            act = read_cursor_activity(uid, appdata)
            if not act or not act["updated_ts"] or now - act["updated_ts"] > within_secs:
                continue
            if not composer_mentions_conv(uid, cid, con=con,
                                          param_context=param_context):
                continue
            if best is None or act["updated_ts"] > best[2]["updated_ts"]:
                best = (uid, str(path), act)
    finally:
        try:
            con.close()
        except Exception:
            pass
    return best


def build_locator_prompt(
    cwd,
    tab_name,
    conversation_id,
    user_home=None,
    appdata=None,
):
    home = Path(user_home or Path.home())
    roaming = Path(
        appdata
        or os.environ.get("APPDATA")
        or home / "AppData" / "Roaming"
    )
    transcript_root = (
        home
        / ".cursor"
        / "projects"
        / cursor_project_slug(cwd)
        / "agent-transcripts"
    )
    workspace_storage = roaming / "Cursor" / "User" / "workspaceStorage"
    return (
        "请在本机精确定位下面这个 Cursor 会话的 transcript 文件：\n\n"
        f"- 项目路径：{cwd or '未知'}\n"
        f"- 界面会话标题：{tab_name or '未知'}\n"
        f"- rxyy-mcp conversation_id：{conversation_id or '无'}\n\n"
        f"优先在 `{transcript_root}` 下搜索完整 conversation_id。"
        "目标主会话文件格式为 `<UUID>\\<UUID>.jsonl`，不要选择 subagents 目录。"
        "命中后读取文件开头和末尾，确认任务内容及中断点。\n\n"
        f"如果需要用界面标题辅助映射，到 `{workspace_storage}` 下先通过 "
        "workspace.json 找到项目哈希，再只读查询 state.vscdb。"
        "标题通常不在 jsonl 正文里。不要仅按最新修改时间猜测。"
    )
