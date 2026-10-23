"""日期 / 时间换算工具。

设计原则：日期和星期的换算全部由 Python 完成，模型只负责把用户的话
归一化成 date_str / offset_days 这样的确定性参数，然后**照抄**本模块
返回的 'YYYY-MM-DD HH:MM' 字符串，不要自己拼时间。

纯函数（_resolve_date / _resolve_datetime）支持注入 today，便于用固定
日期做测试；下面的 @tool 包装只是换成系统当前时间。
"""

from datetime import date, datetime, timedelta

from langchain_core.tools import tool

WEEKDAY_NAMES = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")
WEEKDAY_SHORT = ("一", "二", "三", "四", "五", "六", "日")

# 结果距离 today 超过这个天数时给出警告（只提示，不拦截）
MAX_OFFSET_DAYS = 180


def _weekday_name(d: date) -> str:
    return WEEKDAY_NAMES[d.weekday()]


def _week_monday(d: date) -> date:
    """所在周的周一。前端 getMonday() 也是以周一为一周起点。"""
    return d - timedelta(days=d.weekday())


def _fmt_date(d: date) -> str:
    """2026-09-24（星期四）"""
    return f"{d.isoformat()}（{_weekday_name(d)}）"


def _fmt_week(monday: date, label: str) -> str:
    """{label}：2026-09-21(一) ~ 2026-09-27(日)"""
    sunday = monday + timedelta(days=6)
    return (
        f"{label}：{monday.isoformat()}({WEEKDAY_SHORT[monday.weekday()]})"
        f" ~ {sunday.isoformat()}({WEEKDAY_SHORT[sunday.weekday()]})"
    )


def _fmt_weekend(monday: date, label: str) -> str:
    """{label}：2026-09-26(六) / 2026-09-27(日)"""
    saturday = monday + timedelta(days=5)
    sunday = monday + timedelta(days=6)
    return (
        f"{label}：{saturday.isoformat()}(六) / {sunday.isoformat()}(日)"
    )


def _parse_date(text: str) -> date | None:
    """解析 'YYYY-MM-DD'。格式不对或日期不存在（如 2026-02-30）返回 None。"""
    try:
        return datetime.strptime((text or "").strip(), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _describe_day(d: date, today: date) -> str:
    """相对今天的描述：今天 / 明天 / 昨天 / N天后 / N天前"""
    delta = (d - today).days
    if delta == 0:
        return "今天"
    if delta == 1:
        return "明天"
    if delta == -1:
        return "昨天"
    if delta > 0:
        return f"{delta}天后"
    return f"{-delta}天前"


def _relative_week(d: date, today: date) -> str:
    """本周 / 下周 / 上周 / 三周后 / 两周前。

    直接用两个周一的差值算周差，避免跨周边界时把"下周一"算成本周。
    """
    week_diff = (_week_monday(d) - _week_monday(today)).days // 7
    if week_diff == 0:
        return "本周"
    if week_diff == 1:
        return "下周"
    if week_diff == -1:
        return "上周"
    if week_diff > 1:
        return f"{week_diff}周后"
    return f"{-week_diff}周前"


def _resolve_date(
    date_str: str = "",
    offset_days: int = 0,
    anchor: str = "",
    today: date | None = None,
) -> str:
    today = today or date.today()

    if anchor.strip():
        base = _parse_date(anchor)
        if base is None:
            return f"无法解析基准日期 '{anchor}'。请提供 YYYY-MM-DD 格式的日期，例如 2026-09-24。"
    else:
        base = today

    if date_str.strip():
        target = _parse_date(date_str)
        if target is None:
            return (
                f"无法解析日期 '{date_str}'。"
                f"请提供 YYYY-MM-DD 格式的日期，例如 2026-09-24。"
            )
    else:
        target = base

    target = target + timedelta(days=offset_days)

    delta = (target - today).days
    monday = _week_monday(target)

    lines = [
        f"{_fmt_date(target)}，{_describe_day(target, today)}，{_relative_week(target, today)}"
    ]
    lines.append(_fmt_week(monday, "该周"))
    lines.append(_fmt_weekend(monday, "该周末"))

    # 只看未来两周时，顺带列出下一周，方便模型区分"这周末"和"下周末"
    if 0 <= delta <= 14:
        nxt = monday + timedelta(days=7)
        lines.append(_fmt_week(nxt, "下一周"))
        lines.append(_fmt_weekend(nxt, "下周末"))

    if abs(delta) > MAX_OFFSET_DAYS:
        lines.append(f"⚠️ 该日期距离今天 {abs(delta)} 天，请确认年份有没有算错。")

    return "\n".join(lines)


def _parse_time(text: str) -> tuple[int, int] | None:
    """解析 'HH:MM' / 'HH:MM:SS' / 'H:MM'。格式不对返回 None。"""
    parts = (text or "").strip().split(":")
    if len(parts) not in (2, 3):
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def _fmt_time(dt: datetime, base: date) -> str:
    """同一天只写 HH:MM，跨天时带上完整日期。"""
    if dt.date() == base:
        return dt.strftime("%H:%M")
    return dt.strftime("%Y-%m-%d %H:%M")


def _resolve_datetime(
    date_str: str = "",
    time_str: str = "",
    duration_minutes: int = 0,
    today: date | None = None,
) -> str:
    # 这里用 now() 而不是 date.today()，为了在 time_str 留空时能带上当前时刻
    now = datetime.now()
    today = today or now.date()

    if date_str.strip():
        target = _parse_date(date_str)
        if target is None:
            return (
                f"无法解析日期 '{date_str}'。"
                f"请提供 YYYY-MM-DD 格式的日期，例如 2026-09-24。"
            )
    else:
        target = today

    if time_str.strip():
        parsed = _parse_time(time_str)
        if parsed is None:
            return (
                f"无法解析时间 '{time_str}'。"
                f"请提供 HH:MM 格式的时间，例如 15:30。"
            )
        hour, minute = parsed
    else:
        if not date_str.strip():
            # 两个参数都空：直接告诉模型"现在几点"，别让它去猜或声称拿不到
            return (
                f"当前时刻是 {now.strftime('%Y-%m-%d %H:%M')}（{_weekday_name(today)}）。"
                "如果只是要'现在'，直接用这个值；要算具体时间请提供 time_str"
                "（如 '19:16'）或 date_str（如 '2026-10-24'）。"
            )
        if target == today:
            hour, minute = now.hour, now.minute
        else:
            return (
                f"只拿到日期 {target.isoformat()}（{_weekday_name(target)}），没有具体时间。"
                f"不要自己编时间：要么让用户补充几点，要么把 start 留空放进待计划。"
            )

    start = datetime(target.year, target.month, target.day, hour, minute)
    text = f"start：{start.strftime('%Y-%m-%d %H:%M')}"

    if duration_minutes and duration_minutes > 0:
        end = start + timedelta(minutes=duration_minutes)
        text += f"，end：{end.strftime('%Y-%m-%d %H:%M')}"
        text += f"，时长 {duration_minutes} 分钟"
        if end.date() != start.date():
            text += "（跨天）"

    text += f"（{_weekday_name(target)}，{_describe_day(target, today)}）"

    delta = (target - today).days
    if abs(delta) > MAX_OFFSET_DAYS:
        text += f" ⚠️ 该日期距离今天 {abs(delta)} 天，请确认年份有没有算错。"

    return text


@tool
def resolve_date(date_str: str = "", offset_days: int = 0, anchor: str = "") -> str:
    """把日期换算成具体的年月日和星期。任何涉及"星期几""哪天""周末"的问题都必须先用它算，不要自己推算。

    date_str: 明确的日期，格式 'YYYY-MM-DD'。用户提到具体日期时填写，
        例如"9月26日"填 '2026-09-26'，"周六"先算出那个周六的日期再填。
    offset_days: 相对基准日期偏移的天数，正数往后、负数往前。
        例如"三天后"填 3，"下周三"在 date_str 填下周一的日期后填 2。
    anchor: 计算基准日期，格式 'YYYY-MM-DD'，留空表示以今天为基准。

    返回值里已经带了星期几、相对今天的天数（如"2天后"）、所在周和周末的
    具体日期（如"该周末：2026-09-26(六) / 2026-09-27(日)"），
    请直接从返回值里取日期，不要自己再算一遍。
    """
    return _resolve_date(date_str, offset_days, anchor)


@tool
def resolve_datetime(
    date_str: str = "",
    time_str: str = "",
    duration_minutes: int = 0,
) -> str:
    """把日期 + 时间换算成能直接写进日历的 'YYYY-MM-DD HH:MM' 字符串。

    排日程、要给出发时间或结束时间时，必须先调用它，然后把返回的 start / end
    原样照抄进 emit_tasks，不要自己拼时间字符串。

    date_str: 日期，格式 'YYYY-MM-DD'。留空表示今天。
    time_str: 时间，格式 'HH:MM'。用户没说具体几点时留空。
    duration_minutes: 已知时长时填写（如"开一小时的会"填 60），
        工具会顺手把 end 一起算出来。不确定就填 0。
    """
    return _resolve_datetime(date_str, time_str, duration_minutes)
