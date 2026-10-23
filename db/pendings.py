"""待确认 / 待计划任务的数据访问。

分桶规则（沿用原项目）：有明确开始时间 -> confirmed；没有 -> planned。
对外返回结构保持 {"confirmed": [...], "planned": [...]}，前端不用改。
"""

from __future__ import annotations

from db import database

COLUMNS = "id, title, start_at, end_at, location, note, bucket"


def _empty() -> dict:
    return {"confirmed": [], "planned": []}


def _bucket_of(start: str | None) -> str:
    return "confirmed" if start else "planned"


def _row_to_task(row) -> dict:
    return {
        "id": row["id"],
        "title": row["title"] or "",
        "start": row["start_at"] or None,
        "end": row["end_at"] or None,
        "location": row["location"] or "",
        "note": row["note"] or "",
    }


def load_pending(phone: str) -> dict:
    """读出两个桶。"""
    database.init_db()
    rows = database.query(
        f"SELECT {COLUMNS} FROM pending_tasks WHERE phone = ? ORDER BY created_at ASC",
        (phone,),
    )
    data = _empty()
    for row in rows:
        task = _row_to_task(row)
        bucket = row["bucket"] if row["bucket"] in ("confirmed", "planned") else _bucket_of(task["start"])
        data[bucket].append(task)
    return data


def add_pending(phone: str, task: dict) -> dict:
    """新增一条。task 需要 id 与 title。

    幂等兜底：同一号码下"标题 + 开始时间"完全相同的行不再重复插入，直接返回
    已有那条并带 `duplicate: True`。前端的 dedup 判定是第一道闸，这里是第二道
    —— 手工添加、请求重放、两个标签页同时操作都会绕过第一道。
    """
    database.init_db()
    title = (task.get("title") or "").strip()
    start = task.get("start") or None
    if title:
        existing = database.query_one(
            f"SELECT {COLUMNS} FROM pending_tasks "
            "WHERE phone = ? AND title = ? AND IFNULL(start_at, '') = ?",
            (phone, title, start or ""),
        )
        if existing:
            item = _row_to_task(existing)
            item["duplicate"] = True
            return item

    database.execute(
        "INSERT INTO pending_tasks (id, phone, title, start_at, end_at, location, note, bucket) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            task["id"], phone, task.get("title", ""), start,
            task.get("end") or None, task.get("location") or "",
            task.get("note") or "", _bucket_of(start),
        ),
    )
    row = database.query_one(f"SELECT {COLUMNS} FROM pending_tasks WHERE id = ?", (task["id"],))
    return _row_to_task(row) if row else {}


def update_pending(phone: str, task_id: str, fields: dict) -> dict | None:
    """更新。start 从无到有会自动换桶，反之亦然。"""
    database.init_db()
    mapping = {"title": "title", "start": "start_at", "end": "end_at",
               "location": "location", "note": "note"}
    sets, params = [], []
    for key, column in mapping.items():
        if key not in fields:
            continue
        value = fields[key]
        if key in ("start", "end"):
            value = value or None
        else:
            value = value or ""
        sets.append(f"{column} = ?")
        params.append(value)

    if not sets:
        return None

    # start 参与修改时才重算桶，避免无端改变用户已有的分桶
    if "start" in fields:
        sets.append("bucket = ?")
        params.append(_bucket_of(fields.get("start")))

    sets.append("updated_at = ?")
    params.append(database.now_text())
    params.extend([task_id, phone])

    n = database.execute(
        f"UPDATE pending_tasks SET {', '.join(sets)} WHERE id = ? AND phone = ?", tuple(params)
    )
    if n == 0:
        return None
    row = database.query_one(f"SELECT {COLUMNS} FROM pending_tasks WHERE id = ?", (task_id,))
    return _row_to_task(row) if row else None


def get_pending(phone: str, task_id: str) -> dict | None:
    database.init_db()
    row = database.query_one(
        f"SELECT {COLUMNS} FROM pending_tasks WHERE id = ? AND phone = ?", (task_id, phone)
    )
    return _row_to_task(row) if row else None


def pop_pending(phone: str, task_id: str) -> dict | None:
    """取出并删除（用于"确认"：先拿走再写进日历）。"""
    task = get_pending(phone, task_id)
    if task is None:
        return None
    database.execute("DELETE FROM pending_tasks WHERE id = ? AND phone = ?", (task_id, phone))
    return task


def delete_pending(phone: str, task_id: str) -> bool:
    database.init_db()
    return database.execute(
        "DELETE FROM pending_tasks WHERE id = ? AND phone = ?", (task_id, phone)
    ) > 0


def count(phone: str) -> int:
    row = database.query_one("SELECT COUNT(*) AS n FROM pending_tasks WHERE phone = ?", (phone,))
    return row["n"] if row else 0
