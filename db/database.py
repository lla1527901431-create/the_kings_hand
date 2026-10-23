"""SQLite 连接与通用查询封装。

设计要点：

* **每个线程一个连接**（threading.local）
  FastAPI 的同步端点跑在线程池里，会被多个线程并发调用；
  sqlite3 的连接默认不允许跨线程使用。每线程一个连接最省事且安全。

* **WAL 模式 + busy_timeout**
  WAL 让"读"和"写"不互相阻塞；busy_timeout 让并发写等待而不是直接报
  "database is locked"。这两项对 Web 场景几乎是必须的。

* **建表在启动时执行一次**（db/schema.sql），用 IF NOT EXISTS 保证可重复。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta

import config

_local = threading.local()
_init_lock = threading.Lock()
_initialized = False


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")       # 读写不互相阻塞
    conn.execute("PRAGMA foreign_keys=ON")        # 让 ON DELETE CASCADE 生效
    conn.execute("PRAGMA busy_timeout=5000")      # 遇到锁等待 5 秒而不是立刻报错
    conn.execute("PRAGMA synchronous=NORMAL")     # WAL 下的推荐值，兼顾安全与速度


def get_conn() -> sqlite3.Connection:
    """取当前线程的连接（懒创建）。新连接会确保表已建好。"""
    conn = getattr(_local, "conn", None)
    if conn is None:
        os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
        conn = sqlite3.connect(config.DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        _apply_pragmas(conn)
        _local.conn = conn
        init_db()          # 注意：init_db 用的是自己的临时连接，不会递归回来
    return conn


def init_db() -> None:
    """建表（执行 db/schema.sql，里面全是 IF NOT EXISTS，可重复执行）。

    ⚠️ 这里刻意**不复用** get_conn()：
        如果用 get_conn()，而 get_conn() 又调用 init_db()，就会无限递归。
        建表用一个用完即关的独立连接，语义更清楚，也避免和线程连接纠缠。
    """
    global _initialized
    with _init_lock:
        if _initialized:
            return
        schema_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")
        with open(schema_path, encoding="utf-8") as f:
            schema = f.read()

        os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
        conn = sqlite3.connect(config.DB_PATH, timeout=10)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(schema)
            conn.commit()
            _apply_column_migrations(conn)
        finally:
            conn.close()
        _initialized = True


# 已经发布过的表要加列，只能 ALTER —— CREATE TABLE IF NOT EXISTS 对已存在的表
# 什么都不做。每次启动检查一遍缺哪个列就补哪个，成本可以忽略。
_COLUMN_MIGRATIONS = {
    "whitelist": [
        ("password_hash", "TEXT NOT NULL DEFAULT ''"),
        ("password_set_at", "TEXT"),
    ],
    # 2026-10：过期日程沉淀到 memories.history
    "memories": [
        ("history", "TEXT NOT NULL DEFAULT '[]'"),
    ],
}


def _apply_column_migrations(conn: sqlite3.Connection) -> None:
    """给旧库补齐后来才加的列（幂等）。"""
    for table, columns in _COLUMN_MIGRATIONS.items():
        # 注意：这个连接是 init_db 里临时开的，没设 row_factory，所以按序号取值
        # （PRAGMA table_info 的列是 cid, name, type, notnull, dflt_value, pk）
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            continue        # 表还不存在，说明是全新库，schema.sql 已经带上了新列
        for name, ddl in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
    conn.commit()


# ---------------------------------------------------------------------------
# 通用查询
# ---------------------------------------------------------------------------
def query(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    """查询多行。"""
    conn = get_conn()
    cur = conn.execute(sql, params)
    return cur.fetchall()


def query_one(sql: str, params: tuple = ()) -> sqlite3.Row | None:
    """查询一行，没有则返回 None。"""
    conn = get_conn()
    cur = conn.execute(sql, params)
    return cur.fetchone()


def execute(sql: str, params: tuple = ()) -> int:
    """执行写操作，返回受影响行数。"""
    conn = get_conn()
    cur = conn.execute(sql, params)
    conn.commit()
    return cur.rowcount


def now_text() -> str:
    """当前本地时间，格式 'YYYY-MM-DD HH:MM'。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def now_date() -> str:
    """当前本地日期，格式 'YYYY-MM-DD'。"""
    return datetime.now().strftime("%Y-%m-%d")


@contextmanager
def transaction():
    """把多条写操作包成一个事务：全部成功才提交，任一失败整批回滚。

    ⚠️ 事务内部只能用 `conn.execute(...)`，**不要**调用本模块的
       execute()/query() —— 它们各自会 commit，事务会被切成两半。

    典型用法（过期日程沉淀：写记忆 + 删日程必须同生共死）：

        with database.transaction() as conn:
            memories.upsert_history(conn, phone, entries)
            conn.executemany("DELETE FROM events WHERE phone = ? AND id = ?", ids)
    """
    conn = get_conn()
    if conn.in_transaction:      # 防御：上一条语句若留了隐式事务，先收尾
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ---------------------------------------------------------------------------
# 维护
# ---------------------------------------------------------------------------
def cleanup_expired_sessions() -> int:
    """删掉已过期的会话，返回删除条数。

    在每次登录时顺手调用，不需要定时任务。
    """
    return execute("DELETE FROM sessions WHERE expires_at < ?", (now_text(),))


def new_expires_at(days: int | None = None) -> str:
    days = config.SESSION_DAYS if days is None else days
    return (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M")


def stats() -> dict:
    """给 /health 和启动日志用：各表行数。"""
    out = {}
    for table in ("whitelist", "sessions", "events", "pending_tasks", "memories", "histories"):
        row = query_one(f"SELECT COUNT(*) AS n FROM {table}")
        out[table] = row["n"] if row else 0
    return out
