"""长期记忆的数据访问。一个手机号一行。

记忆分四块（前三块是 LLM 蒸馏出来的"关于人"的判断，第四块是"过去安排"的归档）：

    facts        用户**明确说过**的稳定事实
    observations 观察台账：每条只记"某次发生了什么"，不下结论
    preferences  稳定偏好，可以由 observations 多次归纳而来
    history      过期日程的沉淀摘要，由 agent/memory_history.py 写入

history 里每条形如：

    {"key": "week:2026-W41", "granularity": "week",
     "from": "2026-10-05", "to": "2026-10-11",
     "count": 9, "summary": ["..."], "updated_at": "2026-10-23 18:00"}

`key` 是幂等键：同一周期重复沉淀只会合并摘要、累加 count，不会重复记账。

⚠️ `clear_memory()` 只清前三块，**不动 history**：那是被删日程的唯一存根，
   清掉就永久找不回来了。要清历史请单独处理。
"""

from __future__ import annotations

import json

from db import database

MAX_OBSERVATIONS = 30


def _empty() -> dict:
    return {"facts": [], "observations": [], "preferences": [],
            "history": [], "last_updated": ""}


def _loads(value: str) -> list:
    try:
        data = json.loads(value or "[]")
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _row_to_memory(row) -> dict:
    return {
        "facts": _loads(row["facts"]),
        "observations": _loads(row["observations"]),
        "preferences": _loads(row["preferences"]),
        "history": _loads(row["history"]) if "history" in row.keys() else [],
        "last_updated": row["updated_at"] or "",
    }


def load_memory(phone: str) -> dict:
    """读记忆。没有记录时返回空结构（不新建行）。"""
    database.init_db()
    row = database.query_one(
        "SELECT facts, observations, preferences, history, updated_at "
        "FROM memories WHERE phone = ?",
        (phone,),
    )
    return _row_to_memory(row) if row else _empty()


def save_memory(phone: str, memory: dict) -> None:
    """整体覆盖式保存前三块（upsert）。

    ⚠️ 刻意不写 history 列：调用方传进来的是"关于人"的记忆，
       不该顺手把历史归档覆盖掉。
    """
    database.init_db()
    observations = memory.get("observations") or []
    if not isinstance(observations, list):
        observations = []
    if len(observations) > MAX_OBSERVATIONS:
        observations = observations[-MAX_OBSERVATIONS:]

    database.execute(
        """
        INSERT INTO memories (phone, facts, observations, preferences, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(phone) DO UPDATE SET
            facts = excluded.facts,
            observations = excluded.observations,
            preferences = excluded.preferences,
            updated_at = excluded.updated_at
        """,
        (
            phone,
            json.dumps(memory.get("facts") or [], ensure_ascii=False),
            json.dumps(observations, ensure_ascii=False),
            json.dumps(memory.get("preferences") or [], ensure_ascii=False),
            database.now_text(),
        ),
    )


def clear_memory(phone: str) -> dict:
    """清空"关于人"的三块记忆，保留 history 归档。"""
    save_memory(phone, {"facts": [], "observations": [], "preferences": []})
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


def merge_history(phone: str, entries: list[dict]) -> int:
    """独立事务版的 upsert_history（给脚本/测试用）。"""
    with database.transaction() as conn:
        return upsert_history(conn, phone, entries)


def load_history(phone: str) -> list[dict]:
    row = database.query_one("SELECT history FROM memories WHERE phone = ?", (phone,))
    return _loads(row["history"]) if row else []


def format_memory_text(memory: dict) -> str:
    """拼成给 LLM 看的文本。

    观察台账只在条数够多时展示 —— 只观察过一两次的行为不该影响判断，
    展示出来反而容易让模型过度解读。

    history 只取最近 5 条：它用来回答"用户过去怎么安排的"，
    不是当前日程的替代品（当前日程另有 schedule_text）。
    """
    facts = memory.get("facts") or []
    prefs = memory.get("preferences") or []
    observations = memory.get("observations") or []
    history = memory.get("history") or []

    lines = []
    if facts:
        lines.append("关于用户的事实：")
        lines += [f"- {f}" for f in facts]
    if prefs:
        lines.append("用户的偏好：")
        lines += [f"- {p}" for p in prefs]
    if len(observations) >= 3:
        lines.append("（以下是多次观察到的行为记录，供参考，不要直接当成结论复述）")
        lines += [f"- {o}" for o in observations[-10:]]
    if history:
        lines.append("（以下是已归档的过去安排，仅作背景，不是待办）")
        for entry in history[-5:]:
            summary = "；".join(entry.get("summary") or [])
            span = entry.get("from") or ""
            if entry.get("to") and entry["to"] != span:
                span += f"~{entry['to']}"
            lines.append(f"- {span}：{summary}")

    return "\n".join(lines) if lines else "（暂无长期记忆）"
