"""对话历史。

原项目把对话上下文放在进程内存里，有两个毛病：
    * 内存泄漏（需要额外写空闲回收）
    * 多进程/重启就丢
这里落库，两个问题一起解决。

保留策略：每个用户只留最近 N 条消息（N = RECENT_HISTORY_MAX），
写入时顺手删掉更早的，不需要定时任务。
"""

from __future__ import annotations

import config
from db import database


def recent(phone: str, limit: int | None = None) -> list[dict]:
    """取最近的消息，按时间正序返回。

    limit 默认取 config.RECENT_HISTORY_MAX（即最多这么多条消息，
    一问一答算 2 条）。
    """
    database.init_db()
    limit = limit or config.RECENT_HISTORY_MAX
    rows = database.query(
        "SELECT role, content FROM histories WHERE phone = ? ORDER BY id DESC LIMIT ?",
        (phone, limit),
    )
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def append(phone: str, user_msg: str, reply: str) -> None:
    """追加一轮对话（用户 + 助手），并把超出保留量的旧记录删掉。"""
    database.init_db()
    database.execute(
        "INSERT INTO histories (phone, role, content) VALUES (?, 'user', ?)", (phone, user_msg)
    )
    database.execute(
        "INSERT INTO histories (phone, role, content) VALUES (?, 'assistant', ?)", (phone, reply)
    )
    _trim(phone)


def _trim(phone: str) -> None:
    """只保留最近 N 条。用"删掉 id 小于阈值的行"实现。"""
    keep = config.RECENT_HISTORY_MAX
    row = database.query_one(
        "SELECT id FROM histories WHERE phone = ? ORDER BY id DESC LIMIT 1 OFFSET ?",
        (phone, keep - 1),
    )
    if row is None:
        return
    database.execute("DELETE FROM histories WHERE phone = ? AND id < ?", (phone, row["id"]))


def clear(phone: str) -> int:
    """清空某人的对话上下文（不影响日历和记忆）。"""
    database.init_db()
    return database.execute("DELETE FROM histories WHERE phone = ?", (phone,))
