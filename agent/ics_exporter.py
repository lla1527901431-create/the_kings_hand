"""日历导出：事件列表 -> .ics 文本（RFC 5545）。

只做"生成文本、由浏览器下载"这一件事。
之前还有一个"把 ics 作为附件发邮件"的变体，但界面上从来没有入口，
已随死代码一起删除 —— 需要发邮件的话，让用户下载后自己发即可。
"""

from datetime import datetime

from icalendar import Calendar, Event


def _parse_dt(s: str) -> datetime | None:
    """把 'YYYY-MM-DD HH:MM' 转成 datetime。解析失败返回 None。"""
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M")
    except ValueError:
        return None


def build_ics(events: list) -> str:
    """把事件列表转成 ics 文本。"""
    cal = Calendar()
    cal.add("prodid", "-//The King's Hand//CN")
    cal.add("version", "2.0")

    for evt in events:
        start = _parse_dt(evt.get("start", ""))
        end = _parse_dt(evt.get("end", ""))
        if start is None:
            continue
        if end is None:
            end = start

        e = Event()
        e.add("uid", str(evt.get("id", "")) + "@the-kings-hand")
        e.add("summary", evt.get("title", ""))
        e.add("dtstart", start)
        e.add("dtend", end)

        if evt.get("location"):
            e.add("location", evt["location"])
        if evt.get("note"):
            e.add("description", evt["note"])

        cal.add_component(e)

    return cal.to_ical().decode("utf-8")
