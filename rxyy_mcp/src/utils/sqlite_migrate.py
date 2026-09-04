"""SQLite 轻量迁移器：PRAGMA user_version + 有序迁移清单（W3，2026-08-12）。

为什么不用 alembic：单机工具、两个小库（workflow.db / accounts.db），拖 ORM 级
依赖不值。此前两库各自在 __init__ 里裸跑「PRAGMA table_info + ALTER TABLE 补列」，
每次启动全量探测、迁移逻辑散落且无版本概念；本模块把它收编成：

- 版本存 SQLite 内置 ``PRAGMA user_version``（零额外表）
- 迁移 = 有序清单 [(版本号, 说明, fn(conn)), ...]，只补跑比当前版本新的
- **迁移级原子（尽力）**：每个迁移在自己的 ``BEGIN IMMEDIATE``…``COMMIT`` 里执行
  （fn + 版本号写同生共死）；某个迁移失败只回滚它自己，之前已完成的保留进度。
  为什么不依赖调用方事务：Python sqlite3 默认 isolation_level 下 **DDL/PRAGMA
  不触发隐式事务**（只有 DML 会），CREATE TABLE 会直接 autocommit——
  「with conn: 里跑迁移失败整体回滚」是个不成立的假设（本模块测试锁死了该语义）
- ⚠️ fn 里用 ``conn.executescript()`` 的迁移是例外：executescript 是 sqlite3 里
  唯一会**先 COMMIT 挂起事务**的 API，该迁移退化为非原子、靠幂等性兜底
  （崩在半路重跑即可）——这也是「迁移必须幂等」是硬要求而非建议的原因
- 兼容存量库：user_version 从未设过的旧库读出来是 0，等同全新库——因此
  **每个迁移函数必须幂等**（CREATE IF NOT EXISTS / 先查 PRAGMA table_info 再 ALTER），
  从 0 顺序补跑对「全新库」「存量老库」两种情况都安全
- 库版本比代码新（拿旧代码开新库）：不动作、返回告警，绝不降级/删列

用法（见 console/api/workflow_db.py 与 src/db.py）::

    MIGRATIONS = [
        (1, "baseline", _m1_baseline),
        (2, "projects add oa/biz cols", _m2_project_cols),
    ]
    with self._connect() as conn:
        apply_migrations(conn, MIGRATIONS)
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence

Migration = tuple[int, str, Callable[[sqlite3.Connection], None]]


def get_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def apply_migrations(conn: sqlite3.Connection,
                     migrations: Sequence[Migration]) -> dict:
    """按序补跑缺失迁移。返回 {"from": 起始版本, "to": 结束版本,
    "applied": [说明...], "ahead": bool（库比代码新，未动作）}。
    """
    current = get_version(conn)
    ordered = sorted(migrations, key=lambda m: m[0])
    if ordered:
        vers = [m[0] for m in ordered]
        if len(set(vers)) != len(vers):
            raise ValueError("duplicate migration version: %r" % (vers,))
    latest = ordered[-1][0] if ordered else 0

    if current > latest:
        # 旧代码打开新版库：宁可少做不做错，只读现状、绝不回退 schema
        return {"from": current, "to": current, "applied": [], "ahead": True}

    applied: list[str] = []
    # 调用方已有活动事务（先跑过 DML）时不抢事务管理，在其事务内直跑
    own_txn = not conn.in_transaction
    for ver, desc, fn in ordered:
        if ver <= current:
            continue
        if own_txn:
            conn.execute("BEGIN IMMEDIATE")
        try:
            fn(conn)
            # PRAGMA 不支持参数绑定；ver 已由类型约束为 int，再强转一道防手滑。
            # user_version 写参与当前事务（若还有的话），与 fn 的 schema 变更同生共死
            conn.execute("PRAGMA user_version = %d" % int(ver))
            # fn 用 executescript 时事务已被它 COMMIT 掉（见模块 docstring），
            # 此时各语句已 autocommit 落盘，无需也无法再 COMMIT
            if own_txn and conn.in_transaction:
                conn.execute("COMMIT")
        except BaseException:
            if own_txn and conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        applied.append("v%d %s" % (ver, desc))

    return {"from": current, "to": get_version(conn), "applied": applied,
            "ahead": False}


def add_columns_if_missing(conn: sqlite3.Connection, table: str,
                           columns: Sequence[tuple[str, str]]) -> list[str]:
    """幂等补列助手：columns = [(列名, DDL 片段如 "TEXT DEFAULT ''"), ...]。
    返回实际新增的列名列表。表名/列名来自代码内字面量（非用户输入）。"""
    existing = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table).fetchall()}
    added: list[str] = []
    for name, ddl in columns:
        if name not in existing:
            conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, ddl))
            added.append(name)
    return added
