"""长期记忆的数据访问。一个手机号一行。

记忆分三块，职责刻意分开：

    facts        用户**明确说过**的稳定事实（身份、学校、单位……），LLM 自动蒸馏
    preferences  稳定偏好，**只由用户手动维护**（LLM 只读不写，也不再自动归纳）
    history      过期日程的沉淀摘要（见 agent/memory_history.py），按需通过
                 search_history 工具查询，**不再每轮注入提示词**

为什么取消 observations：
    它存在的唯一理由是"攒证据给 LLM 归纳偏好"。偏好改成用户自己维护后，
    这层台账就没有消费者了，而且实测它记的全是一次性日程（"明天去机场"），
    与 history 重复。

为什么 history 不预加载：
    固定注入最近几条在长尾上没用（问半年前的事查不到），而且每轮都付
    token。改成工具后由模型判断要不要查。

history 里每条形如：

    {"key": "week:2026-W41", "granularity": "week",
     "from": "2026-10-05", "to": "2026-10-11",
     "count": 9, "summary": ["..."], "updated_at": "2026-10-23 18:00"}

`key` 是幂等键：同一周期重复沉淀只会合并摘要、累加 count。

⚠️ `clear_memory()` 只清 facts/preferences，**不动 history**：那是被删日程的
   唯一存根，清掉就永久找不回来了。
"""

from __future__ import annotations

import json

from db import database


def _empty() -> dict:
    return {"facts": [], "preferences": [], "history": [], "last_updated": ""}


def _loads(value: str) -> list:
    try:
        data = json.loads(value or "[]")
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _row_to_memory(row) -> dict:
    return {
        "facts": _loads(row["facts"]),
        "preferences": _loads(row["preferences"]),
        "history": _loads(row["history"]) if "history" in row.keys() else [],
        "last_updated": row["updated_at"] or "",
    }


def load_memory(phone: str) -> dict:
    """读记忆。没有记录时返回空结构（不新建行）。"""
    database.init_db()
    row = database.query_one(
        "SELECT facts, preferences, history, updated_at FROM memories WHERE phone = ?",
        (phone,),
    )
    return _row_to_memory(row) if row else _empty()


def save_memory(phone: str, memory: dict) -> None:
    """整体覆盖式保存 facts / preferences（upsert）。

    ⚠️ 刻意不写 history 列：调用方传进来的是"关于人"的记忆，
       不该顺手把历史归档覆盖掉。
    """
    database.init_db()
    database.execute(
        """
        INSERT INTO memories (phone, facts, preferences, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(phone) DO UPDATE SET
            facts = excluded.facts,
            preferences = excluded.preferences,
            updated_at = excluded.updated_at
        """,
        (
            phone,
            json.dumps(memory.get("facts") or [], ensure_ascii=False),
            json.dumps(memory.get("preferences") or [], ensure_ascii=False),
            database.now_text(),
        ),
    )


def clear_memory(phone: str) -> dict:
    """清空"关于人"的记忆（facts + preferences），保留 history 归档。"""
    save_memory(phone, {"facts": [], "preferences": []})
    return _empty()


# ---------------------------------------------------------------------------
# history（过期日程的沉淀）
# ---------------------------------------------------------------------------
def _merge_entries(current: list[dict], incoming: list[dict]) -> list[dict]:
    """按 key 合并：已有则拼接 summary、累加 count；没有则插入。"""
    by_key = {e.get("key"): e for e in current if isinstance(e, dict) and e.get("key")}
    for entry in incoming:
        key = entry.get("key")
        if not key:
            continue
        old = by_key.get(key)
        if old is None:
            by_key[key] = dict(entry)
            continue
        merged_summary = list(dict.fromkeys((old.get("summary") or []) + (entry.get("summary") or [])))
        by_key[key] = {
            **old,
            **entry,
            "summary": merged_summary,
            "count": int(old.get("count") or 0) + int(entry.get("count") or 0),
        }
    return sorted(by_key.values(), key=lambda e: (e.get("from") or "", e.get("key") or ""))


def upsert_history(conn, phone: str, entries: list[dict]) -> int:
    """在**已开启的事务连接**上合并 history，返回合并后的总条数。

    为什么要传 conn 而不是自己开事务：调用方要把"写记忆 + 删日程"放进
    同一个事务里，这里再 commit 一次就会把事务切成两半。
    """
    row = conn.execute("SELECT history FROM memories WHERE phone = ?", (phone,)).fetchone()
    current = _loads(row["history"]) if row else []
    merged = _merge_entries(current, entries)
    payload = json.dumps(merged, ensure_ascii=False)
    conn.execute(
        """
        INSERT INTO memories (phone, history, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(phone) DO UPDATE SET
            history = excluded.history,
            updated_at = excluded.updated_at
        """,
        (phone, payload, database.now_text()),
    )
    return len(merged)


def load_history(phone: str) -> list[dict]:
    row = database.query_one("SELECT history FROM memories WHERE phone = ?", (phone,))
    return _loads(row["history"]) if row else []


def format_memory_text(memory: dict) -> str:
    """拼成给 LLM 看的"关于人"的记忆。

    只放 facts 与 preferences：
      * history 交给 search_history 工具按需查（固定注入在长尾上没用）；
      * observations 已取消。
    """
    facts = memory.get("facts") or []
    prefs = memory.get("preferences") or []

    lines = []
    if facts:
        lines.append("关于用户的事实：")
        lines += [f"- {f}" for f in facts]
    if prefs:
        lines.append("用户自己写的偏好：")
        lines += [f"- {p}" for p in prefs]

    return "\n".join(lines) if lines else "（暂无长期记忆）"
