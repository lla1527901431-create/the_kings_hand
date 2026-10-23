"""「历史归档」查询工具。

为什么不把 history 直接拼进提示词（之前的做法）：
    * 固定注入最近 N 条，对"半年前那次体检"这类长尾问题没用；
    * 每一轮都要付 token，哪怕用户只是说"明天三点开会"；
    * 归档是分批处理的，"最近几条"未必真的是最近的。

所以改成：给模型一个无参工具，需要时一次把全部归档摘要取回来。
判断"要不要查"由模型按提示词里的规则做，代码不做任何猜测。
"""

from __future__ import annotations

from langchain_core.tools import tool

from db import memories
from server.logging_config import get_logger

logger = get_logger("history")

# 一次最多列这么多段。正常一年也就几十段（越老合并得越粗），
# 这个上限只是防止极端情况下把上下文塞爆。
MAX_BUCKETS = 80


def format_all_history(phone: str) -> str:
    """把该用户全部归档摘要拼成文本（按时间倒序）。"""
    entries = memories.load_history(phone) or []
    if not entries:
        return "（暂无归档历史：这个用户还没有过期日程被沉淀过）"

    ordered = sorted(
        entries,
        key=lambda e: (e.get("from") or "", e.get("key") or ""),
        reverse=True,
    )
    total = len(ordered)
    shown = ordered[:MAX_BUCKETS]

    lines = [f"过去已归档的安排（按时间倒序，共 {total} 段）："]
    for entry in shown:
        start = entry.get("from") or ""
        end = entry.get("to") or ""
        span = f"{start}~{end}" if end and end != start else start
        summary = "；".join(str(s) for s in (entry.get("summary") or []))
        count = entry.get("count") or 0
        lines.append(f"- {span}（{count} 条）：{summary}")

    if total > len(shown):
        lines.append(f"（只显示最近 {len(shown)} 段，更早的已按月合并）")

    lines.append(
        "注意：以上是历史记录，不是当前待办。不要把它们当成待办列表，"
        "也不要把已经做过的事当成新任务重复添加；除非用户明确说要再做一次。"
    )
    return "\n".join(lines)


def make_search_history(phone: str):
    """按手机号绑定出一个无参工具（工具本身拿不到当前用户）。"""

    @tool
    def search_history() -> str:
        """查看用户过去已归档的日程摘要（无参数，一次返回全部）。

        该调用的情况：
        - 时间模糊、可能已有安排，例如"明天下午再开个会"
        - 常规行程或路线，例如"今晚回珠海""去机场接人"
        - 出现"再 / 又 / 还是 / 照上次 / 老地方 / 跟之前一样"这类回指
        - 用户直接问过去，例如"上次是什么时候""我之前去过吗"
        - 周期性安排，例如"每周三上课"
        - 用户只说事不说时间，需要从历史补全
        - 用户的说法与已知事实/偏好冲突，需要核对

        不必调用的情况：
        - 时间明确的新任务，例如"明天下午 3 点开会"
        - 问天气、问当前日程（系统提示里已经列全）
        - 纯闲聊
        """
        logger.info("模型查询了历史归档：%s", phone)
        return format_all_history(phone)

    return search_history
