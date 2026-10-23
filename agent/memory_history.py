"""过期日程 → 长期记忆的沉淀（写 memories.history 后物理删除 events）。

规则（2026-10 与用户确认）：

* **用户手动删除的事件不进记忆**：蒸馏只处理库里仍然存在的行，
  用户删掉的行在查询时就看不见了，不需要额外的"已删除"标记。
* **过期** = 开始时间早于「本周一 00:00」，与日历界面的可见范围一致。
* **越老越粗**（"指数减少"的落地形式）：

      | 年龄        | 粒度  | 一条摘要对应 |
      |------------|-------|-------------|
      | ≤ 7 天     | event | 1 个事件     |
      | 8 ~ 30 天  | day   | 1 天的事件   |
      | 31~180 天  | week  | 1 周的事件   |
      | > 180 天   | month | 1 个月的事件 |

  500 条/年的记录会沉淀成 500 → 365 → 52 → 12 条，规模随年龄迅速下降。

* **先写记忆、再删日程**，放在同一个事务里；任何一步失败整批回滚，一条都不删。
  宁可这次没沉淀成功，也不能出现"日程删了、记忆没写进去"。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

import config
from agent.llm import build_memory_llm
from db import calendars, database, memories
from server.logging_config import get_logger

logger = get_logger("memory")

EVENT_MAX_DAYS = 7      # ≤7 天：逐条
DAY_MAX_DAYS = 30       # ≤30 天：按天
WEEK_MAX_DAYS = 180     # ≤180 天：按周；再老按月
MAX_BUCKETS_PER_RUN = 8  # 一次最多蒸馏几个桶，避免登录时突发几十次 LLM 调用


# ---------------------------------------------------------------------------
# 时间分桶
# ---------------------------------------------------------------------------
def monday_start(now: datetime | None = None) -> datetime:
    """本周一 00:00 —— 日历界面能看到的最早边界。"""
    now = now or datetime.now()
    return (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)


def _parse_start(text) -> datetime | None:
    try:
        return datetime.strptime(str(text or "").strip(), "%Y-%m-%d %H:%M")
    except ValueError:
        return None


def bucket_key(event: dict, now: datetime) -> tuple[str, str] | None:
    """返回 (粒度, 幂等键)；时间无法解析时返回 None（跳过，不删）。"""
    start = _parse_start(event.get("start"))
    if start is None:
        return None
    age_days = (now - start).days
    if age_days <= EVENT_MAX_DAYS:
        return "event", f"event:{event.get('id')}"
    if age_days <= DAY_MAX_DAYS:
        return "day", f"day:{start:%Y-%m-%d}"
    if age_days <= WEEK_MAX_DAYS:
        iso_year, iso_week, _ = start.isocalendar()
        return "week", f"week:{iso_year}-W{iso_week:02d}"
    return "month", f"month:{start:%Y-%m}"


def plan_buckets(events: list[dict], now: datetime) -> dict[str, dict]:
    """把过期事件按粒度分桶。返回 {key: {key, granularity, from, to, events}}。"""
    buckets: dict[str, dict] = {}
    for event in events:
        got = bucket_key(event, now)
        if not got:
            logger.warning("日程 %s 的开始时间无法解析，跳过沉淀", event.get("id"))
            continue
        granularity, key = got
        bucket = buckets.setdefault(key, {
            "key": key, "granularity": granularity,
            "from": "", "to": "", "events": [],
        })
        bucket["events"].append(event)

    for bucket in buckets.values():
        dates = sorted(
            str(e.get("start") or "")[:10] for e in bucket["events"] if e.get("start")
        )
        bucket["from"] = dates[0] if dates else ""
        bucket["to"] = dates[-1] if dates else ""
    return buckets


# ---------------------------------------------------------------------------
# 摘要（一次 LLM 调用产出一个桶的摘要）
# ---------------------------------------------------------------------------
SUMMARY_PROMPT = """你是日程归档助手。下面是用户已经过去的一段日程（{label}，共 {count} 条）。
请把它压缩成 1~3 条中文摘要，写进长期记忆。

规则：
- 每条不超过 30 字，写清"做了什么"，带日期或周期
- 同一类反复出现的事合并成一条并注明次数，例如"每周三晚上上课（4 次）"
- 只写日程里确实出现过的内容，不要编造，不要评价用户
- 只输出 JSON，不要解释：{{"summary": ["...", "..."]}}

日程：
{events}"""

_LABELS = {"event": "单条日程", "day": "一天", "week": "一周", "month": "一个月"}


def _format_events(events: list[dict]) -> str:
    lines = []
    for event in events:
        title = event.get("title") or ""
        line = f"- {event.get('start')} {title}"
        if event.get("location"):
            line += f"（{event['location']}）"
        lines.append(line)
    return "\n".join(lines)


def _extract_json(text: str) -> dict:
    """从模型输出里抠出 JSON（容忍 ```json 围栏和前后废话）。"""
    cleaned = (text or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"摘要没有返回 JSON：{cleaned[:120]!r}")
    return json.loads(cleaned[start:end + 1])


def summarize_with_llm(deepseek_key: str, bucket: dict) -> list[str]:
    """调用 LLM 生成一个桶的摘要；失败抛异常（调用方因此不会删除任何行）。"""
    prompt = SUMMARY_PROMPT.format(
        label=_LABELS.get(bucket["granularity"], "一段日程"),
        count=len(bucket["events"]),
        events=_format_events(bucket["events"]),
    )
    message = build_memory_llm(deepseek_key).invoke(prompt)
    data = _extract_json(getattr(message, "content", "") or "")

    summary = data.get("summary")
    if not isinstance(summary, list) or not summary:
        raise ValueError("摘要的 summary 字段为空或格式不对")
    lines = [str(s).strip() for s in summary if str(s).strip()]
    if not lines:
        raise ValueError("摘要的 summary 全是空字符串")
    return lines[:3]


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def distill(phone: str, deepseek_key: str, *, now: datetime | None = None,
            max_buckets: int = MAX_BUCKETS_PER_RUN, summarize=None,
            dry_run: bool = False) -> dict:
    """沉淀该用户的过期日程。

    summarize 可注入（测试用桩，不联网）；dry_run=True 只算不写不删。
    """
    now = now or datetime.now()
    cutoff = monday_start(now)

    expired = [
        event for event in calendars.list_events(phone)
        if (start := _parse_start(event.get("start"))) and start < cutoff
    ]
    if not expired:
        return {"cutoff": cutoff.strftime("%Y-%m-%d %H:%M"), "expired": 0,
                "buckets": 0, "deleted": 0, "history": memories.load_history(phone).__len__()}

    buckets = plan_buckets(expired, now)
    # 从最老的开始处理：越老合并得越粗，先清掉大头
    keys = sorted(buckets, key=lambda k: (buckets[k]["from"], buckets[k]["key"]))[:max_buckets]
    summarize = summarize or summarize_with_llm

    entries: list[dict] = []
    for key in keys:
        bucket = buckets[key]
        summary = summarize(deepseek_key, bucket)     # 抛异常 → 直接向上冒，不进入事务
        entries.append({
            "key": key,
            "granularity": bucket["granularity"],
            "from": bucket["from"],
            "to": bucket["to"],
            "count": len(bucket["events"]),
            "summary": summary,
            "updated_at": now.strftime("%Y-%m-%d %H:%M"),
        })

    pending_ids = [(phone, e["id"]) for key in keys for e in buckets[key]["events"]]

    if dry_run:
        return {"cutoff": cutoff.strftime("%Y-%m-%d %H:%M"), "expired": len(expired),
                "buckets": len(keys), "deleted": 0, "dry_run": True, "entries": entries}

    with database.transaction() as conn:
        total = memories.upsert_history(conn, phone, entries)
        conn.executemany("DELETE FROM events WHERE phone = ? AND id = ?", pending_ids)

    logger.info("过期日程沉淀：%s 个桶 / %s 条日程 → history %s 条（剩余 %s 条未处理）",
                len(keys), len(pending_ids), total, len(buckets) - len(keys))
    return {"cutoff": cutoff.strftime("%Y-%m-%d %H:%M"), "expired": len(expired),
            "buckets": len(keys), "deleted": len(pending_ids),
            "history": total, "remaining": len(buckets) - len(keys)}


if __name__ == "__main__":        # 方便手工试跑：python -m agent.memory_history
    print("请用 tools/distill_memory.py --phone <手机号> 执行")
