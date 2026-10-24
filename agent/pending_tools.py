"""修改待办的工具。

只有"还没确认"的任务能被 agent 改：
    pending_tasks  → 可改
    events（已确认）→ 不可改，只能提示用户去日历里手动改

为什么不给"修改日程"的工具：用户点了确认就等于拍板，agent 再改会造成
"我说了算"和"你改了没说"的冲突。这条边界写在提示词里，也写在这里。
"""

from __future__ import annotations

from langchain_core.tools import tool

from db import pendings
from server.logging_config import get_logger

logger = get_logger("pending")


def _describe(task: dict) -> str:
    parts = [f"[id={task.get('id')}] {task.get('title', '')}"]
    if task.get("start"):
        parts.append(f"时间 {task['start']}" + (f"~{task['end']}" if task.get("end") else ""))
    if task.get("location"):
        parts.append(f"地点 {task['location']}")
    if task.get("note"):
        parts.append(f"备注 {str(task['note'])[:80]}")
    return "，".join(parts)


def make_update_pending(phone: str):
    """按手机号绑定出"修改待办"工具。"""

    @tool
    def update_pending_task(task_id: str, title: str = "", start: str = "",
                            end: str = "", location: str = "",
                            note: str = "") -> str:
        """修改「待确认 / 待计划」里的任务。只传要改的字段，留空表示该项不变。

        task_id: 任务 id，来自系统提示里待办列表的 [id=...]。
        title / start / end / location / note: 要设置的新值。

        规则：
        - 用户补充信息（出发地、同行人、备注、改时间）时，**修改已有任务**，
          不要用 emit_tasks 新建一条。
        - 已确认的任务（已经进日历的）不能修改，会返回提示；那种情况请让用户
          在日历里手动改。
        - start / end 必须用 'YYYY-MM-DD HH:MM'，先用 resolve_datetime 算好再填。
        """
        task_id = (task_id or "").strip()
        if not task_id:
            return "缺少 task_id：请从待办列表的 [id=...] 里取。"

        task = pendings.get_pending(phone, task_id)
        if task is None:
            logger.info("拒绝修改不存在的待办：%s", task_id)
            return (f"没有找到 id={task_id} 的待办。它可能已经确认（进了日历）"
                    f"或者被删掉了。**已确认的任务不能由你修改**，"
                    f"请让用户在日历里手动改。")

        fields = {}
        for key, value in (("title", title), ("start", start), ("end", end),
                           ("location", location), ("note", note)):
            if (value or "").strip():
                fields[key] = value.strip()
        if not fields:
            return "没有提供要修改的内容。"

        updated = pendings.update_pending(phone, task_id, fields)
        if updated is None:
            return f"修改 id={task_id} 失败：它可能刚刚被确认或删除。"
        logger.info("更新待办 %s：%s", task_id, list(fields))
        return f"已更新：{_describe(updated)}"

    return update_pending_task
