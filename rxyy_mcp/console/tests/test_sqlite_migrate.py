# -*- coding: utf-8 -*-
"""SQLite 迁移器（W3，2026-08-12）：user_version 版本化 + 幂等迁移。

锁住四条路径：全新库建到最新版、存量 v0 老库补列升级、重复打开幂等、
库版本比代码新时绝不动作。workflow.db 与 accounts.db 两个接入方都要过。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from console.api.workflow_db import MIGRATIONS as WF_MIGRATIONS
from console.api.workflow_db import WorkflowDB
from src.db import MIGRATIONS as ACC_MIGRATIONS
from src.db import Database
from src.utils.sqlite_migrate import (add_columns_if_missing, apply_migrations,
                                      get_version)

WF_LATEST = max(v for v, _, _ in WF_MIGRATIONS)
ACC_LATEST = max(v for v, _, _ in ACC_MIGRATIONS)


def _cols(path, table):
    conn = sqlite3.connect(path)
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}
    finally:
        conn.close()


def _user_version(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


# ---------- workflow.db ----------

def test_workflow_fresh_db_reaches_latest(tmp_path):
    p = str(tmp_path / "wf.db")
    WorkflowDB(p)
    assert _user_version(p) == WF_LATEST
    assert {"oa_project_id", "oa_project_text", "biz_name"} <= _cols(p, "projects")
    assert {"id", "slug", "name", "soul"} <= _cols(p, "my_agents")


def test_workflow_legacy_v0_db_upgrades(tmp_path):
    """存量老库：projects 无 oa/biz 列、user_version 从未设过（=0）。"""
    p = str(tmp_path / "wf.db")
    conn = sqlite3.connect(p)
    conn.executescript(
        """CREATE TABLE projects (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               path TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
               branch TEXT DEFAULT '', source TEXT DEFAULT 'scan',
               enabled INTEGER DEFAULT 1, note TEXT DEFAULT '',
               last_commit_at TEXT DEFAULT '',
               created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
           INSERT INTO projects(path, name, created_at, updated_at)
               VALUES('D:/x', 'x', '2026-01-01', '2026-01-01');""")
    conn.commit()
    conn.close()
    assert _user_version(p) == 0

    db = WorkflowDB(p)
    assert _user_version(p) == WF_LATEST
    assert {"oa_project_id", "biz_name"} <= _cols(p, "projects")
    assert {"id", "slug", "name"} <= _cols(p, "my_agents")
    # 老数据无损
    assert [pr.name for pr in db.list_projects()] == ["x"]


def test_workflow_reopen_is_idempotent(tmp_path):
    p = str(tmp_path / "wf.db")
    WorkflowDB(p)
    WorkflowDB(p)  # 二次打开不得报错、版本不变
    assert _user_version(p) == WF_LATEST


# ---------- accounts.db ----------

def test_accounts_fresh_db_reaches_latest(tmp_path):
    p = str(tmp_path / "acc.db")
    Database(p)
    assert _user_version(p) == ACC_LATEST
    assert {"used", "tags", "status_json", "checked_at"} <= _cols(p, "accounts")


def test_accounts_legacy_v0_db_upgrades(tmp_path):
    """08-07 之前的老账号库：无 used/tags/status_json/checked_at 列。"""
    p = str(tmp_path / "acc.db")
    conn = sqlite3.connect(p)
    conn.executescript(
        """CREATE TABLE accounts (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               email TEXT NOT NULL UNIQUE, password TEXT DEFAULT '',
               token TEXT DEFAULT '', membership TEXT DEFAULT 'free',
               refund_used INTEGER DEFAULT 0, note TEXT DEFAULT '',
               order_no TEXT DEFAULT '',
               created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
           INSERT INTO accounts(email, created_at, updated_at)
               VALUES('a@b.c', '2026-01-01', '2026-01-01');""")
    conn.commit()
    conn.close()

    db = Database(p)
    assert _user_version(p) == ACC_LATEST
    assert {"used", "tags", "status_json", "checked_at"} <= _cols(p, "accounts")
    accs = db.list_accounts()
    assert len(accs) == 1 and accs[0].email == "a@b.c" and accs[0].used == 0


# ---------- 迁移器本体 ----------

def test_ahead_db_is_left_untouched(tmp_path):
    """库版本比代码新（拿旧代码开新库）：不动作、报 ahead，绝不降级。"""
    p = str(tmp_path / "x.db")
    conn = sqlite3.connect(p)
    conn.execute("PRAGMA user_version = 99")
    conn.commit()

    called = []
    res = apply_migrations(conn, [(1, "m1", lambda c: called.append(1))])
    conn.close()
    assert res["ahead"] is True and res["applied"] == [] and called == []
    assert _user_version(p) == 99


def test_partial_upgrade_only_runs_missing(tmp_path):
    p = str(tmp_path / "x.db")
    conn = sqlite3.connect(p)
    ran = []
    migs = [(1, "m1", lambda c: ran.append(1)), (2, "m2", lambda c: ran.append(2)),
            (3, "m3", lambda c: ran.append(3))]
    conn.execute("PRAGMA user_version = 2")
    res = apply_migrations(conn, migs)
    conn.commit()
    conn.close()
    assert ran == [3] and res["from"] == 2 and res["to"] == 3


def test_duplicate_version_rejected(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "x.db"))
    with pytest.raises(ValueError):
        apply_migrations(conn, [(1, "a", lambda c: None), (1, "b", lambda c: None)])
    conn.close()


def test_add_columns_if_missing_reports_added(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "x.db"))
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    added = add_columns_if_missing(conn, "t", [("a", "TEXT DEFAULT ''"),
                                               ("b", "INTEGER DEFAULT 0")])
    assert added == ["a", "b"]
    assert add_columns_if_missing(conn, "t", [("a", "TEXT DEFAULT ''")]) == []
    conn.close()


def test_failed_migration_rolls_back_itself_only(tmp_path):
    """迁移级原子：v2 失败只回滚 v2（半张表 + 版本号一起消失），v1 的进度保留。

    背景：Python sqlite3 默认模式下 DDL/PRAGMA 不触发隐式事务（autocommit），
    所以迁移器必须自管 BEGIN IMMEDIATE——本测试锁死这个语义，防止回退成
    「靠调用方 with conn: 兜底」的错误假设。"""
    p = str(tmp_path / "x.db")
    conn = sqlite3.connect(p)

    def boom(c):
        c.execute("CREATE TABLE half (id INTEGER)")
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        apply_migrations(conn, [(1, "ok", lambda c: c.execute(
            "CREATE TABLE ok_t (id INTEGER)")), (2, "boom", boom)])
    conn.close()

    assert _user_version(p) == 1          # v1 已提交，v2 连带版本号回滚
    conn = sqlite3.connect(p)
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "ok_t" in names and "half" not in names
