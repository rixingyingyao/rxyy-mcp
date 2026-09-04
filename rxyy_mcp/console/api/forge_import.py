"""从 Forge tools 的本地缓存里提取 Cursor 账号列表。

Forge（cockpit-tools，Tauri + WebView2）把账号数组存在 WebView2 的 Local Storage
（leveldb）里，每条记录是形如 {"id":"cursor_...","email":...,"workos_token":...} 的
JSON 对象。这里直接把 leveldb 的 .ldb/.log 复制出来做字符串扫描 + 括号配对提取，
不依赖 leveldb 库，也不需要 Forge 关闭（.ldb 只读、复制不受写锁影响）。

对外只暴露 extract_accounts()，返回规范化后的账号列表，供 accounts_api 导入。
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import tempfile

# Forge 数据目录候选（新版 cockpit-tools 优先，旧版 cursorforge.lite 兜底）
_APP_DIRS = ("com.jlcodes.cockpit-tools", "com.cursorforge.lite")
_LS_SUBPATH = os.path.join("EBWebView", "Default", "Local Storage", "leveldb")

_OBJ_START = re.compile(r'\{"id":"cursor_')


def _candidate_ls_dirs() -> list[str]:
    local = os.environ.get("LOCALAPPDATA", "")
    dirs = []
    for app in _APP_DIRS:
        p = os.path.join(local, app, _LS_SUBPATH)
        if os.path.isdir(p):
            dirs.append(p)
    return dirs


def _read_leveldb_blob(ls_dir: str) -> str:
    """复制 leveldb 的 .ldb/.log 到临时目录后拼接读入（规避运行时写锁）。"""
    tmp = tempfile.mkdtemp(prefix="forge_ls_")
    blob = b""
    try:
        for src in glob.glob(os.path.join(ls_dir, "*")):
            name = os.path.basename(src)
            if not (name.endswith(".ldb") or name.endswith(".log")):
                continue
            dst = os.path.join(tmp, name)
            try:
                shutil.copy2(src, dst)
                with open(dst, "rb") as fh:
                    blob += fh.read()
            except Exception:
                # 单个文件复制/读取失败不致命，跳过
                continue
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return blob.decode("latin-1")


def _match_obj(text: str, start: int, limit: int = 16000) -> str | None:
    depth = 0
    end = min(len(text), start + limit)
    for i in range(start, end):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _parse_blob(text: str) -> dict[str, dict]:
    """扫描 blob，返回 {account_id: raw_obj}，同 id 保留『有 token 且 last_used 最新』的快照。"""
    found: dict[str, dict] = {}

    def score(o: dict) -> tuple:
        return (1 if o.get("workos_token") else 0, o.get("last_used", 0) or 0)

    for m in _OBJ_START.finditer(text):
        raw = _match_obj(text, m.start())
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        acc_id = obj.get("id")
        if not acc_id:
            continue
        prev = found.get(acc_id)
        if prev is None or score(obj) >= score(prev):
            found[acc_id] = obj
    return found


def _norm_membership(forge_type: str, has_token: bool) -> str:
    t = (forge_type or "").lower()
    if not has_token:
        return "token_invalid"
    if "invalid" in t:
        return "token_invalid"
    if "pro" in t or "team" in t or "business" in t:
        return "pro"
    return "free"


def extract_accounts() -> dict:
    """返回 {ok, source, accounts:[{email,token,membership,note,...}], stats}。

    accounts 里的 token 已把 URL 编码的 %3A%3A 还原成 ::，与 accounts.db 现有格式一致。
    """
    ls_dirs = _candidate_ls_dirs()
    if not ls_dirs:
        return {"ok": False, "error": "未找到 Forge 数据目录（com.jlcodes.cockpit-tools）", "accounts": []}

    merged: dict[str, dict] = {}
    used_dir = ""
    for ls_dir in ls_dirs:
        blob = _read_leveldb_blob(ls_dir)
        objs = _parse_blob(blob)
        if objs:
            used_dir = ls_dir
            # 跨目录合并，同 id 取更优快照
            for k, v in objs.items():
                prev = merged.get(k)
                sc = (1 if v.get("workos_token") else 0, v.get("last_used", 0) or 0)
                psc = (1 if prev and prev.get("workos_token") else 0, prev.get("last_used", 0) or 0) if prev else (-1, -1)
                if sc >= psc:
                    merged[k] = v

    accounts = []
    with_token = 0
    for obj in merged.values():
        email = (obj.get("email") or "").strip()
        if not email:
            continue
        tok = (obj.get("workos_token") or "").replace("%3A%3A", "::")
        if tok:
            with_token += 1
        raw = obj.get("cursor_auth_raw") or {}
        accounts.append({
            "email": email,
            "token": tok,
            "membership": _norm_membership(obj.get("membership_type", ""), bool(tok)),
            "note": "Forge导入",
            "forge_type": obj.get("membership_type", ""),
            "stripe_status": raw.get("stripeSubscriptionStatus", ""),
            "last_used": obj.get("last_used"),
        })

    accounts.sort(key=lambda a: a["email"])
    return {
        "ok": True,
        "source": used_dir,
        "accounts": accounts,
        "stats": {
            "total": len(accounts),
            "with_token": with_token,
            "without_token": len(accounts) - with_token,
        },
    }


if __name__ == "__main__":
    import sys
    r = extract_accounts()
    if not r["ok"]:
        print("FAIL:", r.get("error"))
        sys.exit(1)
    print("source:", r["source"])
    print("stats:", r["stats"])
    for a in r["accounts"]:
        print(f"  {a['email']:45s} | {a['membership']:14s} | tok:{'Y' if a['token'] else 'N'} | {a['forge_type']}")
