"""任务查重与时间冲突判定。

⚠️ **已停用（2026-10-23）**：查重已改为「模型在同一轮对话里判定」，
   见 agent/runner.py 的 SYSTEM_PROMPT 与 emit_tasks 的 duplicate_of 字段。
   本模块不再被运行时调用。保留原因：demo 目录没有 git，删除不可回滚；
   规则与阈值仍可作为将来对比或回退的参考，tools/eval_dedup.py 也仍能跑它。

为什么当初放在服务端按规则做，而不是让模型判断：

* **确定性**：同一份输入必须得到同一个结论，否则无法写测试、也无法评估命中率；
* **零成本**：不额外调用 LLM，不消耗 token，也不受模型当天状态影响；
* **可解释**：每条结论都带 score 与 reason，判错了能立刻定位是规则错还是阈值错。

分工：模型只负责"把自然语言拆成结构化任务"（它擅长这件事）；"这个任务是不是
已经有了"由本模块裁决。日程与待办由调用方传入 —— 本模块**不碰数据库**，
因此可以脱机跑评测。

结论写在 task["dedup"]：

    {"status": "duplicate" | "similar" | "conflict" | "recurring" | "new",
     "score": 0.0 ~ 1.0,
     "match": {"id", "title", "start", "source"} | None,
     "reason": "给用户看的一句话"}

五个状态的含义：
    new       全新的任务，正常入列
    duplicate 同一件事已经存在，前端应跳过、不要重复入库
    similar   疑似同一件事（改期、措辞不同、一边没定时间），入库但提示
    conflict  同一时段已有别的安排（不是重复，是撞车）
    recurring 同名日程出现在另一天，多半是周期性安排，提示但不拦截
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta
from difflib import SequenceMatcher

# ---------------------------------------------------------------------------
# 阈值：集中放这里，调参只改这一段（评测脚本会打印每个阈值下的命中率）
# ---------------------------------------------------------------------------
DUP_WINDOW_MIN = 15        # 判定"完全重复"的时间容差（分钟）
DUP_SIM = 0.80             # 判定"完全重复"的相似度下限
SIM_WINDOW_MIN = 120       # 判定"疑似重复"的时间容差
SIM_SIM = 0.65             # 判定"疑似重复"的相似度下限
SAME_DAY_WIDE_MIN = 240    # 同一天内相隔较远但仍提示的上限
RECURRING_SIM = 0.90       # 跨天同名（周期性日程）的相似度下限
CONFLICT_SIM_MAX = 0.65    # 低于这个相似度 + 时间重叠 = 冲突而不是重复
NO_TIME_SIM = 0.90         # 两边都没定时间时的重复阈值
PENDING_SIM = 0.85         # 与"待计划"任务比对的阈值（比日程略严）

DEFAULT_DURATION = timedelta(hours=1)   # 没给 end 时按 1 小时估算区间

NEW, DUPLICATE, SIMILAR, CONFLICT, RECURRING = (
    "new", "duplicate", "similar", "conflict", "recurring",
)

# 命中多个候选时，按这个顺序取最"严重"的一条
_PRIORITY = {DUPLICATE: 4, CONFLICT: 3, SIMILAR: 2, RECURRING: 1, NEW: 0}

# 只收高频、歧义小的日历词。宁缺毋滥：同义词表越长，误杀越多。
_SYNONYMS = (
    ("碰头", "开会"), ("碰个头", "开会"), ("例会", "开会"), ("会议", "开会"),
    ("聚餐", "吃饭"), ("用餐", "吃饭"), ("聚会", "吃饭"),
    ("健身", "运动"), ("锻炼", "运动"), ("跑步", "运动"),
    ("课程", "上课"), ("培训", "上课"),
    ("就医", "看病"), ("看医生", "看病"),
)

_PUNCT = re.compile(
    r"[\s，。、；：！？,.!?;:\"'“”‘’（）()【】\[\]《》<>·~\-—_/\\|+*&^%$#@]+"
)
_TAIL_PARTICLES = "啊吧呢哦啦呀嘛哈"


def normalize_title(title: str) -> str:
    """标题归一化：全角转半角、小写、去标点空白、去尾部语气词、替换高频同义词。"""
    text = unicodedata.normalize("NFKC", title or "").lower()
    text = _PUNCT.sub("", text)
    for src, dst in _SYNONYMS:
        text = text.replace(src, dst)
    while text and text[-1] in _TAIL_PARTICLES:
        text = text[:-1]
    return text


def _char_bigrams(text: str) -> set[str]:
    if len(text) < 2:
        return {text} if text else set()
    return {text[i:i + 2] for i in range(len(text) - 1)}


def title_similarity(a: str, b: str) -> float:
    """中文标题相似度：编辑相似度、字符二元组 Jaccard、包含关系三者取最大。

    单用 SequenceMatcher 对中文短标题偏严（"开会" vs "部门开会" 只有 0.67），
    所以补一条"短标题被完整包含"的规则。
    """
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0

    ratio = SequenceMatcher(None, na, nb).ratio()
    ba, bb = _char_bigrams(na), _char_bigrams(nb)
    jaccard = len(ba & bb) / len(ba | bb) if (ba | bb) else 0.0
    contains = 0.90 if (na in nb or nb in na) and min(len(na), len(nb)) >= 2 else 0.0
    return round(max(ratio, jaccard, contains), 3)


_TIME_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M")


def parse_time(value) -> datetime | None:
    """解析项目内部统一的时间文本（'YYYY-MM-DD HH:MM'），失败返回 None。"""
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _interval(task: dict) -> tuple[datetime, datetime] | None:
    """取出 [start, end)；只有开始时间时按默认时长补 end。"""
    start = parse_time(task.get("start"))
    if start is None:
        return None
    end = parse_time(task.get("end")) or (start + DEFAULT_DURATION)
    if end <= start:                      # end 早于 start 视为非法，按默认时长
        end = start + DEFAULT_DURATION
    return start, end


def _minutes_between(a: datetime, b: datetime) -> float:
    return abs((a - b).total_seconds()) / 60.0


def _verdict(task_iv, cand_iv, sim: float, task: dict, cand: dict):
    """对单个候选给出 (status, score, reason)；不够格返回 None。"""
    source = "待办" if cand.get("source") == "pending" else "日历"

    # ---- 两边都没有时间：只能比标题 ----
    if task_iv is None and cand_iv is None:
        threshold = PENDING_SIM if cand.get("source") == "pending" else NO_TIME_SIM
        if sim >= threshold:
            return DUPLICATE, sim, f"「{source}」里已有同名任务（都还没定时间）"
        return None

    # ---- 一边有时间、一边没有 ----
    if task_iv is None or cand_iv is None:
        if sim >= NO_TIME_SIM:
            return SIMILAR, sim, f"「{source}」里已有同名任务，时间需确认"
        return None

    # ---- 两边都有时间 ----
    start, end = task_iv
    cstart, cend = cand_iv
    delta = _minutes_between(start, cstart)
    same_day = start.date() == cstart.date()
    overlap = start < cend and cstart < end

    if same_day and delta <= DUP_WINDOW_MIN and sim >= DUP_SIM:
        return DUPLICATE, sim, "同一时段已有同名任务"

    # "开始时间落在已有安排的时段内部" = 同一件事换了个说法，可以判重复。
    # 注意只看这一个方向：反过来（旧安排落在新任务区间内）不算 —— 那多半是
    # 新任务把旧安排圈了进去，属于"改期/冲突"，不是重复。
    if sim >= DUP_SIM and cstart <= start < cend:
        return DUPLICATE, sim, "开始的时刻落在已有安排的时段内"

    if same_day and delta <= SIM_WINDOW_MIN and sim >= SIM_SIM:
        return SIMILAR, sim, "同一天已有相似任务，可能是改期"

    if same_day and delta <= SAME_DAY_WIDE_MIN and sim >= DUP_SIM:
        return SIMILAR, sim, "同一天已有同名任务，时间不同"

    if (not same_day) and sim >= RECURRING_SIM:
        day_delta = _minutes_between(
            start.replace(year=2000, month=1, day=1),
            cstart.replace(year=2000, month=1, day=1),
        )
        if day_delta <= 60:
            return RECURRING, sim, "同名日程出现在另一天，可能是周期性安排"

    if overlap and sim < CONFLICT_SIM_MAX:
        return CONFLICT, round(1.0 - sim, 3), "该时段已被其他安排占用"

    return None


def _candidates(events, pending) -> list[dict]:
    """把日历与待办合并成候选列表，source 用于区分来源。"""
    out = []
    for event in events or []:
        if event.get("title"):
            out.append({**event, "source": "event"})
    for bucket in ("confirmed", "planned"):
        for task in (pending or {}).get(bucket) or []:
            if task.get("title"):
                out.append({**task, "source": "pending"})
    return out


def classify(task: dict, events=None, pending=None) -> dict:
    """判定单个任务，返回 dedup 结论字典。"""
    task_iv = _interval(task)
    best = None
    for cand in _candidates(events, pending):
        sim = title_similarity(task.get("title", ""), cand.get("title", ""))
        verdict = _verdict(task_iv, _interval(cand), sim, task, cand)
        if verdict is None:
            continue
        status, score, reason = verdict
        if best is None or (_PRIORITY[status], score) > (_PRIORITY[best[0]], best[1]):
            best = (status, score, reason, cand)

    if best is None:
        return {"status": NEW, "score": 0.0, "match": None, "reason": ""}

    status, score, reason, cand = best
    return {
        "status": status,
        "score": round(score, 3),
        "match": {
            "id": cand.get("id", ""),
            "title": cand.get("title", ""),
            "start": cand.get("start"),
            "source": cand.get("source", ""),
        },
        "reason": reason,
    }


def annotate(tasks: list[dict], events=None, pending=None) -> list[dict]:
    """给每个任务补上 dedup 结论（浅拷贝，不改调用方的对象）。

    同一个批次内部也互相查重：模型一次可能把同一件事拆成两条，第一批判完
    就把它们塞进候选池，后面那条会被判成重复。
    """
    base_pending = pending or {}
    batch: list[dict] = []
    out: list[dict] = []
    for task in tasks or []:
        item = dict(task)
        merged = {
            "confirmed": list(base_pending.get("confirmed") or []) + batch,
            "planned": list(base_pending.get("planned") or []),
        }
        item["dedup"] = classify(item, events, merged)
        if item["dedup"]["status"] != DUPLICATE:
            batch.append({"id": item.get("id"), "title": item.get("title"),
                          "start": item.get("start")})
        out.append(item)
    return out


def is_duplicate(task: dict) -> bool:
    """前端/接口层判断是否应跳过入库。"""
    return (task.get("dedup") or {}).get("status") == DUPLICATE
