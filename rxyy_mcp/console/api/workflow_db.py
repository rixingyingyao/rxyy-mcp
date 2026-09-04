"""工作台数据库：项目管理 + 日报生产。

单独一个 sqlite（data/workflow.db），与账号库 accounts.db 解耦。
- projects：纳管的本地 git 项目（自动扫描 + 手动添加）
- daily_reports：按日期存的日报（内置模型生成 或 agent 会话总结写入）
- report_notes：按「项目+日期」存的日报素材（对话里 agent 总结落库，
  生成日报时优先于该项目的 git 提交）
"""
from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from src.config import WORKFLOW_DB_PATH
from src.utils.sqlite_migrate import add_columns_if_missing, apply_migrations

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    path         TEXT NOT NULL UNIQUE,
    name         TEXT NOT NULL,
    branch       TEXT DEFAULT '',
    source       TEXT DEFAULT 'scan',   -- scan | manual
    enabled      INTEGER DEFAULT 1,     -- 是否纳入日报统计
    note         TEXT DEFAULT '',
    last_commit_at TEXT DEFAULT '',
    -- OA 工作汇报「项目选择」归属：本地项目 → 某个 OA 项目（多对一）
    oa_project_id   TEXT DEFAULT '',    -- OA 项目 run_id（DATA_4_1 值）
    oa_project_text TEXT DEFAULT '',    -- OA 项目别名（DATA_4_1_TEXT 显示）
    -- 业务名：日报正文用的中文业务称呼（如 playthread-go → 智慧云广播录播播出端），
    -- 避免仓库英文名/代号漏进领导看的日报
    biz_name     TEXT DEFAULT '',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_reports (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    report_date  TEXT NOT NULL UNIQUE,  -- YYYY-MM-DD
    content      TEXT DEFAULT '',
    source       TEXT DEFAULT 'model',  -- model（内置生成）| agent（会话总结写入）
    model        TEXT DEFAULT '',
    commit_count INTEGER DEFAULT 0,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

-- 键值配置（OA 账号 / token 等；一行一个 key）
CREATE TABLE IF NOT EXISTS kv_config (
    k            TEXT PRIMARY KEY,
    v            TEXT DEFAULT '',
    updated_at   TEXT NOT NULL
);

-- 日报素材：对话里 agent 按「项目+日期」总结落库，生成日报时优先采用
CREATE TABLE IF NOT EXISTS report_notes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    note_date    TEXT NOT NULL,         -- YYYY-MM-DD
    project      TEXT NOT NULL,         -- 项目名（与 projects.name 对齐）
    content      TEXT DEFAULT '',       -- 素材行（一行一条“做了什么”）
    source       TEXT DEFAULT 'agent',  -- agent（对话总结）| manual
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    UNIQUE(note_date, project)
);
"""


@dataclass
class Project:
    path: str
    name: str
    branch: str = ""
    source: str = "scan"
    enabled: int = 1
    note: str = ""
    last_commit_at: str = ""
    oa_project_id: str = ""
    oa_project_text: str = ""
    biz_name: str = ""
    id: int | None = None
    created_at: str = ""
    updated_at: str = ""


def _m1_baseline(conn: sqlite3.Connection) -> None:
    """v1：全套表（幂等 CREATE IF NOT EXISTS，存量库跑过等于无操作）。"""
    conn.executescript(_SCHEMA)


def _m2_project_oa_biz_cols(conn: sqlite3.Connection) -> None:
    """v2：projects 补 OA 归属 / 业务名列（旧库没有；新库 v1 已含，幂等跳过）。"""
    add_columns_if_missing(conn, "projects", [
        ("oa_project_id", "TEXT DEFAULT ''"),
        ("oa_project_text", "TEXT DEFAULT ''"),
        ("biz_name", "TEXT DEFAULT ''"),
    ])


def _m3_my_agents(conn: sqlite3.Connection) -> None:
    """v3：个人 Agent 名册（控制台「我的 Agent」页）。"""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS my_agents (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            slug             TEXT NOT NULL UNIQUE,
            name             TEXT NOT NULL,
            emoji            TEXT DEFAULT '',
            role             TEXT DEFAULT '',
            soul             TEXT DEFAULT '',
            runtime          TEXT DEFAULT 'cursor',
            model            TEXT DEFAULT '',
            project_path     TEXT DEFAULT '',
            tools            TEXT DEFAULT '[]',
            memory_notes     TEXT DEFAULT '',
            governance       TEXT DEFAULT 'ask',
            builtin          INTEGER DEFAULT 0,
            pinned           INTEGER DEFAULT 0,
            sort_order       INTEGER DEFAULT 0,
            last_launched_at TEXT DEFAULT '',
            created_at       TEXT NOT NULL,
            updated_at       TEXT NOT NULL
        )"""
    )


# 加新迁移：append 一条 (版本+1, 说明, fn)，fn 必须幂等（见 sqlite_migrate 模块 docstring）
MIGRATIONS = [
    (1, "baseline schema", _m1_baseline),
    (2, "projects add oa/biz cols", _m2_project_oa_biz_cols),
    (3, "my_agents roster", _m3_my_agents),
]


class WorkflowDB:
    _lock = threading.RLock()

    def __init__(self, path: str | None = None) -> None:
        self._path = str(path or WORKFLOW_DB_PATH)
        with self._lock, self._connect() as conn:
            apply_migrations(conn, MIGRATIONS)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """同 src.db.Database._connect：`with sqlite3_conn:` 不关连接，必须自己关，
        否则 workflow.db 的句柄会一直挂着，目录改不动也删不掉。"""
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # ---------- projects ----------
    def upsert_project(self, p: Project) -> Project:
        now = datetime.now().isoformat(timespec="seconds")
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT id, created_at, enabled, note, source FROM projects WHERE path=?",
                               (p.path,)).fetchone()
            if row:
                p.id = row["id"]
                p.created_at = row["created_at"]
                # 手动改过的 enabled/note 不被扫描覆盖
                if p.source == "scan":
                    p.enabled = row["enabled"]
                    if not p.note:
                        p.note = row["note"]
                    p.source = row["source"]
                p.updated_at = now
                conn.execute(
                    """UPDATE projects SET name=?, branch=?, source=?, enabled=?, note=?,
                       last_commit_at=?, updated_at=? WHERE id=?""",
                    (p.name, p.branch, p.source, p.enabled, p.note, p.last_commit_at, p.updated_at, p.id),
                )
            else:
                p.created_at = now
                p.updated_at = now
                cur = conn.execute(
                    """INSERT INTO projects(path, name, branch, source, enabled, note,
                       last_commit_at, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (p.path, p.name, p.branch, p.source, p.enabled, p.note,
                     p.last_commit_at, p.created_at, p.updated_at),
                )
                p.id = cur.lastrowid
        return p

    def list_projects(self) -> list[Project]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT * FROM projects ORDER BY last_commit_at DESC, name ASC").fetchall()
        return [Project(**{k: r[k] for k in r.keys()}) for r in rows]

    def get_project(self, project_id: int) -> Project | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        return Project(**{k: row[k] for k in row.keys()}) if row else None

    def set_project_fields(self, project_id: int, fields: dict[str, Any]) -> None:
        allowed = {"enabled", "note", "name", "oa_project_id", "oa_project_text", "biz_name"}
        sets, vals = [], []
        for k, v in (fields or {}).items():
            if k in allowed:
                sets.append(f"{k}=?")
                vals.append(v)
        if not sets:
            return
        vals.append(datetime.now().isoformat(timespec="seconds"))
        vals.append(project_id)
        with self._lock, self._connect() as conn:
            conn.execute(f"UPDATE projects SET {', '.join(sets)}, updated_at=? WHERE id=?", vals)

    def update_project_git_meta(self, project_id: int, branch: str, last_commit_at: str) -> None:
        """刷新扫描快照：分支和最近提交时间。不碰 enabled/note/OA 归属。"""
        now = datetime.now().isoformat(timespec="seconds")
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE projects SET branch=?, last_commit_at=?, updated_at=? WHERE id=?",
                (branch or "", last_commit_at or "", now, project_id),
            )

    def delete_project(self, project_id: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM projects WHERE id=?", (project_id,))

    # ---------- daily reports ----------
    def upsert_report(self, report_date: str, content: str, source: str = "model",
                      model: str = "", commit_count: int = 0) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT id, created_at FROM daily_reports WHERE report_date=?",
                               (report_date,)).fetchone()
            if row:
                conn.execute(
                    """UPDATE daily_reports SET content=?, source=?, model=?, commit_count=?,
                       updated_at=? WHERE id=?""",
                    (content, source, model, commit_count, now, row["id"]),
                )
            else:
                conn.execute(
                    """INSERT INTO daily_reports(report_date, content, source, model,
                       commit_count, created_at, updated_at) VALUES(?,?,?,?,?,?,?)""",
                    (report_date, content, source, model, commit_count, now, now),
                )

    def get_report(self, report_date: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM daily_reports WHERE report_date=?", (report_date,)).fetchone()
        return {k: row[k] for k in row.keys()} if row else None

    def list_reports(self, limit: int = 30) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT * FROM daily_reports ORDER BY report_date DESC LIMIT ?",
                               (limit,)).fetchall()
        return [{k: r[k] for k in r.keys()} for r in rows]

    # ---------- report notes（项目+日期 素材） ----------
    def upsert_note(self, note_date: str, project: str, content: str,
                    source: str = "agent", mode: str = "append") -> dict[str, Any]:
        """写入某项目某天的素材。mode=append 在已有内容后追加新行（去重）；replace 覆盖。

        与该项目「别的日期」已有素材逐字相同的行一律丢掉：8-06 库里多了一条与
        7-23 完全一样的 cursor工作流 素材（测素材库功能时留下的），当天日报第一条
        照它写成了三周前的活，还一路写进了 OA 草稿。返回值里的 skipped 是被丢的行数。
        """
        now = datetime.now().isoformat(timespec="seconds")
        content = (content or "").strip()
        with self._lock, self._connect() as conn:
            seen: set[str] = set()
            for r in conn.execute(
                    "SELECT content FROM report_notes WHERE project=? AND note_date<>?",
                    (project, note_date)):
                seen.update(x.strip() for x in (r["content"] or "").splitlines() if x.strip())
            kept = [x for x in content.splitlines() if x.strip() and x.strip() not in seen]
            skipped = len([x for x in content.splitlines() if x.strip()]) - len(kept)
            content = "\n".join(kept)
            row = conn.execute(
                "SELECT id, content FROM report_notes WHERE note_date=? AND project=?",
                (note_date, project)).fetchone()
            if not content:
                # 整条都是别处搬来的重复行：既不新建空条目，也不许 replace 拿空的把原有的抹掉
                return {"id": row["id"] if row else 0, "merged": False, "skipped": skipped}
            if row:
                if mode == "append" and (row["content"] or "").strip():
                    old_lines = [x.strip() for x in row["content"].splitlines() if x.strip()]
                    new_lines = [x.strip() for x in content.splitlines()
                                 if x.strip() and x.strip() not in old_lines]
                    content = "\n".join(old_lines + new_lines)
                conn.execute(
                    "UPDATE report_notes SET content=?, source=?, updated_at=? WHERE id=?",
                    (content, source, now, row["id"]))
                return {"id": row["id"], "merged": mode == "append", "skipped": skipped}
            cur = conn.execute(
                """INSERT INTO report_notes(note_date, project, content, source,
                   created_at, updated_at) VALUES(?,?,?,?,?,?)""",
                (note_date, project, content, source, now, now))
            return {"id": cur.lastrowid, "merged": False, "skipped": skipped}

    def list_notes(self, date_from: str, date_to: str = "",
                   project: str = "") -> list[dict[str, Any]]:
        date_to = date_to or date_from
        sql = ("SELECT * FROM report_notes WHERE note_date>=? AND note_date<=?")
        args: list[Any] = [date_from, date_to]
        if project:
            sql += " AND project=?"
            args.append(project)
        sql += " ORDER BY note_date ASC, project ASC"
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [{k: r[k] for k in r.keys()} for r in rows]

    def delete_note(self, note_id: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM report_notes WHERE id=?", (note_id,))

    # ---------- kv_config ----------
    def kv_get(self, key: str, default: str = "") -> str:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT v FROM kv_config WHERE k=?", (key,)).fetchone()
        return row["v"] if row else default

    def kv_set(self, key: str, value: str) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO kv_config(k, v, updated_at) VALUES(?,?,?)
                   ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_at=excluded.updated_at""",
                (key, value, now),
            )

    def kv_get_json(self, key: str, default: Any = None) -> Any:
        import json as _json
        raw = self.kv_get(key, "")
        if not raw:
            return default
        try:
            return _json.loads(raw)
        except Exception:
            return default

    def kv_set_json(self, key: str, value: Any) -> None:
        import json as _json
        self.kv_set(key, _json.dumps(value, ensure_ascii=False))

    # ---------- my_agents（个人 Agent 名册） ----------
    def list_my_agents(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM my_agents ORDER BY pinned DESC, sort_order ASC, id ASC"
            ).fetchall()
        return [{k: r[k] for k in r.keys()} for r in rows]

    def get_my_agent(self, agent_id: int) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM my_agents WHERE id=?", (agent_id,)).fetchone()
        return {k: row[k] for k in row.keys()} if row else None

    def get_my_agent_by_slug(self, slug: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM my_agents WHERE slug=?", (slug,)).fetchone()
        return {k: row[k] for k in row.keys()} if row else None

    def upsert_my_agent(self, row: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now().isoformat(timespec="seconds")
        fields = (
            "slug", "name", "emoji", "role", "soul", "runtime", "model",
            "project_path", "tools", "memory_notes", "governance",
            "builtin", "pinned", "sort_order", "last_launched_at",
        )
        values = {k: row.get(k, "" if k != "builtin" and k != "pinned" and k != "sort_order" else 0)
                  for k in fields}
        if values["tools"] is None:
            values["tools"] = "[]"
        agent_id = row.get("id")
        with self._lock, self._connect() as conn:
            existing = None
            if agent_id:
                existing = conn.execute("SELECT * FROM my_agents WHERE id=?", (agent_id,)).fetchone()
            if existing is None and values["slug"]:
                existing = conn.execute(
                    "SELECT * FROM my_agents WHERE slug=?", (values["slug"],)
                ).fetchone()
            if existing:
                conn.execute(
                    """UPDATE my_agents SET slug=?, name=?, emoji=?, role=?, soul=?,
                       runtime=?, model=?, project_path=?, tools=?, memory_notes=?,
                       governance=?, builtin=?, pinned=?, sort_order=?,
                       last_launched_at=?, updated_at=? WHERE id=?""",
                    (values["slug"], values["name"], values["emoji"], values["role"],
                     values["soul"], values["runtime"], values["model"],
                     values["project_path"], values["tools"], values["memory_notes"],
                     values["governance"], int(values["builtin"] or 0),
                     int(values["pinned"] or 0), int(values["sort_order"] or 0),
                     values["last_launched_at"] or existing["last_launched_at"] or "",
                     now, existing["id"]),
                )
                agent_id = existing["id"]
            else:
                cur = conn.execute(
                    """INSERT INTO my_agents(
                       slug, name, emoji, role, soul, runtime, model, project_path,
                       tools, memory_notes, governance, builtin, pinned, sort_order,
                       last_launched_at, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (values["slug"], values["name"], values["emoji"], values["role"],
                     values["soul"], values["runtime"], values["model"],
                     values["project_path"], values["tools"], values["memory_notes"],
                     values["governance"], int(values["builtin"] or 0),
                     int(values["pinned"] or 0), int(values["sort_order"] or 0),
                     values["last_launched_at"] or "", now, now),
                )
                agent_id = cur.lastrowid
        saved = self.get_my_agent(int(agent_id))
        if not saved:
            raise RuntimeError("my_agents upsert 后读不到行")
        return saved

    def delete_my_agent(self, agent_id: int) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM my_agents WHERE id=?", (agent_id,))
            return cur.rowcount > 0

    def touch_my_agent_launch(self, agent_id: int) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE my_agents SET last_launched_at=?, updated_at=? WHERE id=?",
                (now, now, agent_id),
            )


_wf_singleton: WorkflowDB | None = None


def wfdb() -> WorkflowDB:
    global _wf_singleton
    if _wf_singleton is None:
        _wf_singleton = WorkflowDB()
    return _wf_singleton
