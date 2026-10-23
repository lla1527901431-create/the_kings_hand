"""长期记忆的数据访问。一个手机号一行。

记忆分三块（沿用原项目的设计）：
    facts        用户**明确说过**的稳定事实
    observations 观察台账：每条只记"某次发生了什么"，不下结论
    preferences  稳定偏好，可以由 observations 多次归纳而来

三块都用 JSON 数组存文本，符合"整体读-改-写"的使用方式。
"""

from __future__ import annotations

import json

from db import database

MAX_OBSERVATIONS = 30


def _empty() -> dict:
    return {"facts": [], "observations": [], "preferences": [], "last_updated": ""}


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
        "last_updated": row["updated_at"] or "",
    }


def load_memory(phone: str) -> dict:
    """读记忆。没有记录时返回空结构（不新建行）。"""
    database.init_db()
    row = database.query_one(
        "SELECT facts, observations, preferences, updated_at FROM memories WHERE phone = ?",
        (phone,),
    )
    return _row_to_memory(row) if row else _empty()


def save_memory(phone: str, memory: dict) -> None:
    """整体覆盖式保存（upsert）。"""
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
    save_memory(phone, {"facts": [], "observations": [], "preferences": []})
    return _empty()


def format_memory_text(memory: dict) -> str:
    """拼成给 LLM 看的文本。

    观察台账只在条数够多时展示 —— 只观察过一两次的行为不该影响判断，
    展示出来反而容易让模型过度解读。
    """
    facts = memory.get("facts") or []
    prefs = memory.get("preferences") or []
    observations = memory.get("observations") or []

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

    return "\n".join(lines) if lines else "（暂无长期记忆）"
