"""日程事件的数据访问。

对外字段名保持和原项目一致（start / end 而不是 start_at / end_at），
这样前端 static/app.js 的渲染逻辑不用改。
时间统一用 'YYYY-MM-DD HH:MM' 文本存储，SQLite 里可以直接按字符串排序
（这个格式的字典序恰好等于时间序）。
"""

from __future__ import annotations

import uuid

from db import database

COLUMNS = "id, title, start_at, end_at, location, note"


def _new_id() -> str:
    return uuid.uuid4().hex[:8]


def _row_to_event(row) -> dict:
    return {
        "id": row["id"],
        "title": row["title"] or "",
        "start": row["start_at"] or "",
        "end": row["end_at"] or "",
        "location": row["location"] or "",
        "note": row["note"] or "",
    }


def list_events(phone: str) -> list[dict]:
    database.init_db()
    rows = database.query(
        f"SELECT {COLUMNS} FROM events WHERE phone = ? ORDER BY start_at ASC",
        (phone,),
    )
    return [_row_to_event(r) for r in rows]


def add_event(phone: str, title: str, start: str, end: str | None,
              location: str = "", note: str = "") -> dict:
    database.init_db()
    event_id = _new_id()
    database.execute(
        "INSERT INTO events (id, phone, title, start_at, end_at, location, note) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (event_id, phone, title, start, end or None, location or "", note or ""),
    )
    row = database.query_one(f"SELECT {COLUMNS} FROM events WHERE id = ?", (event_id,))
    return _row_to_event(row) if row else {}


def update_event(phone: str, event_id: str, fields: dict) -> dict | None:
    """按 id 修改。只更新 fields 里出现的键。找不到（或不属于本用户）返回 None。"""
    database.init_db()
    mapping = {"title": "title", "start": "start_at", "end": "end_at",
               "location": "location", "note": "note"}
    sets, params = [], []
    for key, column in mapping.items():
        if key not in fields:
            continue
        value = fields[key]
        if key == "end":
            # 结束时间允许为空（表示未知时长），空值要存成 NULL 而不是空串
            value = value or None
        else:
            value = value or ""
        sets.append(f"{column} = ?")
        params.append(value)
    if not sets:
        return None

    sets.append("updated_at = ?")
    params.append(database.now_text())
    params.extend([event_id, phone])

    n = database.execute(
        f"UPDATE events SET {', '.join(sets)} WHERE id = ? AND phone = ?", tuple(params)
    )
    if n == 0:
        return None
    row = database.query_one(f"SELECT {COLUMNS} FROM events WHERE id = ?", (event_id,))
    return _row_to_event(row) if row else None


def delete_event(phone: str, event_id: str) -> bool:
    database.init_db()
    return database.execute(
        "DELETE FROM events WHERE id = ? AND phone = ?", (event_id, phone)
    ) > 0


def count(phone: str) -> int:
    row = database.query_one("SELECT COUNT(*) AS n FROM events WHERE phone = ?", (phone,))
    return row["n"] if row else 0
