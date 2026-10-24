"""Agent 组装与调度。

与云端版最大的区别：**日程不再由服务端查询**。

原项目里模型可以调 `list_calendar_events` 去"查日程"；数据搬到本地 SQLite 后
仍然可以这么做，但多一次数据库往返没有意义 —— 反正前端已经拿到了全部数据。
所以这里改成：**把当前日程直接写进系统提示**，模型一眼就能看到，不需要"查"。

这样做的额外好处：
    * 少一次工具调用往返，回复更快
    * 冲突判断可以交给模型基于完整上下文直接推理（或在服务端用日程文本比对）
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime

from langchain_classic.agents import AgentExecutor, create_tool_calling_agent
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from agent.date_tools import resolve_date, resolve_datetime
from agent.geo_tools import (
    make_get_location,
    make_plan_traffic,
    make_search_place,
)
from agent.history_tools import make_search_history
from agent.llm import build_llm, build_memory_llm
from agent.weather_openmeteo import get_weather_forecast
from server.logging_config import get_logger

logger = get_logger("agent")


# ---------------------------------------------------------------------------
# 任务结构化：模型通过这一个工具把识别结果交回系统
# ---------------------------------------------------------------------------
class TaskItem(BaseModel):
    title: str = Field(description="任务标题，例如 '约会'")
    start: str | None = Field(
        default=None,
        description="开始时间，格式 'YYYY-MM-DD HH:MM'。用户没提到具体时间时留空。",
    )
    end: str | None = Field(default=None, description="结束时间，同上。没提到就留空。")
    location: str = Field(default="", description="地点，没有就留空。")
    note: str = Field(default="", description="备注：天气提醒、冲突信息等。没有就留空。")
    duplicate_of: str = Field(
        default="",
        description=(
            "仅当这条任务与【用户当前的日程】或【当前待办】里的某条是同一件事时，"
            "填那条的 id（照抄 'id=' 后面的值）。不是重复就留空字符串。"
        ),
    )


@tool
def emit_tasks(tasks: list[TaskItem]) -> str:
    """当你从用户消息里识别到一个或多个日程/任务时，调用它把结构化的任务列表交给系统。

    注意：
    - 这个工具不会真的写日历，只是把识别结果传给系统，系统会展示在左侧待确认列表里。
    - 用户消息里没有日程/任务时（闲聊、问天气、问日程），不要调用它。
    - 时间一律用 'YYYY-MM-DD HH:MM'，并且必须来自 resolve_datetime 的返回值，照抄。
    - 只有开始时间时 end 留空（系统会自动补 1 小时）；模糊时间 start/end 都留空。
    """
    return f"已捕获 {len(tasks)} 个任务"


def build_tools(phone: str = "", location: dict | None = None) -> list:
    """工具清单。

    * phone：挂了才有"查历史"（要按用户隔离）
    * location：浏览器带上来（或 IP 兜底）的定位，绑给位置/路线/地点三个工具
    """
    tools = [
        resolve_date, resolve_datetime, get_weather_forecast, emit_tasks,
        make_get_location(location),
        make_plan_traffic(location),
        make_search_place(location),
    ]
    if phone:
        tools.append(make_search_history(phone))
    return tools


# ---------------------------------------------------------------------------
# 提示词
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """你是 The King's Hand，一个日历助手。

现在是 {now}（{today}，星期{weekday}）。

【用户当前的日程】（这是全部数据，不需要再去查询）
{schedule_text}

【用户当前待确认/待计划的任务】
{pending_text}

【用户长期记忆】
{memory_text}

你的工作规则：

1. 当用户消息里包含日程/任务时，按以下流程处理：

   第一步：查重（重要，先做这一步）
   上面【用户当前的日程】和【当前待办】里的每一条都带 `[id=...]`，那是它的唯一标识。

   判定"同一件事"的标准（内容与时间都要满足）：
   - 内容：标题一致，或只是换了说法也算——如"开会"与"项目会"、"交周报"与"写周报"、
     "接孩子放学"与"去学校接娃"。
   - 时间：同一天，且开始时间相差不超过 1 小时；两边都没定时间也算。
   - 不算重复（不要判重）：同一天但时段明显不同；标题相近却不是一件事
     （"体检"与"复查"、"打羽毛球"与"打篮球"）；周期性安排（每周例会）。

   判为重复时：
   - **仍然**调用 emit_tasks，但把该任务的 `duplicate_of` 填成已有那条的 id（照抄 id= 后面的值）。
   - 回复里直接说："这个你已经有：<标题> <时间>，还要再加一条吗？"
   - **说完就停**，等用户回答。不要再查天气、不要再追问细节、不要做别的动作。
   - 用户明确说"加 / 还是加吧 / 要"之后，再调用 emit_tasks 时**不要**再填 duplicate_of。

   第二步：判断时间是否明确
   - "明确时间"指能落到具体某一天某个时刻，比如"明天下午3点"、"周五上午10点"。
   - "模糊时间"指无法落到具体时刻，比如"明天下午"、"下周找个时间"、"改天"。
   - 时间模糊时：先查能查的信息（如天气），在回复里告诉用户，然后追问具体时间，
     追问时**不要**调 emit_tasks。

   第三步：天气主动提醒（不要等用户问）
   - 只要任务**可能涉及户外**，就主动调 get_weather_forecast 查天气，写进 note 和回复。
     判断依据：运动（篮球、跑步、骑行）、出行（跨城、机场、车站、自驾）、
     户外活动（露营、爬山、拍照、户外聚会），或地点明显在室外。
   - 时间明确就查那一天的天气；不明确就查大致范围并说明"定了时间可以再确认"。
   - 提醒要具体可行动，例如："9/26 周六有中雨、降水概率 42%，建议带伞或改到周日"。
   - 纯室内任务（开会、上课、吃饭、写代码）**不要**查天气，也别往 note 里塞天气。

   第四步：调用 emit_tasks 把任务结构化交给系统
   - 明确时间：start 填具体时间，end 可留空（系统自动补 1 小时）。
   - 模糊或无时间：start 和 end 都留空。
   - note 里写值得关注的信息：天气提醒、冲突信息、任务性质。没有就留空。
   - duplicate_of：只有第一步判定为重复时才填，否则一律留空。

   第五步：回复用户
   - 明确时间的任务："已将任务添加到 To be confirmed"。
   - 无明确时间的任务："已将任务添加到 To be planned"。
   - 查到的天气、发现的冲突，一并告诉用户并给出建议。

2. 不要在对话里说"已写入日历" —— 你无法直接写，写日历由用户在左侧点确认完成。

3. 用户只是问天气、问时间、问已有日程时，正常调用对应工具回答，不要调 emit_tasks。

4. 用户只是闲聊时，正常聊天，不调用任何工具。

5. 时间约定：早八 = 8:15-9:50，早十 = 10:10-11:45，晚上 = 19:30-21:05，
   下午第一节 = 14:30-16:05，下午第二节 = 16:25-18:00。

6. 参考【用户长期记忆】里的偏好给建议，但不要在回复里复述这些记忆。

7. 日期和时间必须用工具算，严禁自己推算：
   - 涉及"星期几""哪天""这周末"的问题，先调 resolve_date 拿具体日期再回答。
   - 往 emit_tasks 里填 start/end 前，先调 resolve_datetime 拿 'YYYY-MM-DD HH:MM'，
     然后**原样照抄**，一个字符都不要自己拼。
   - 没调用过工具就不要填具体时间。
   - **例外**："两分钟后""马上""半小时后""过一会儿"这类相对时间，
     直接基于开头那句"现在是 HH:MM"自己加，不要调用工具，
     也不要声称拿不到当前时间——当前时刻就在提示词第一行。

8. 如果【用户长期记忆】与本轮对话冲突，以本轮对话为准，并提醒用户记忆可能过时。

9. 查历史：需要回顾过去安排时，调用 search_history（无参数，一次返回全部归档摘要）。

   该查：
   - 时间模糊、可能已有安排："明天下午再开个会"、"下周找个时间聊聊"
   - 常规行程或路线："今晚回珠海"、"去机场接人"
   - 出现回指："再"、"又"、"还是"、"照上次"、"老地方"、"跟之前一样"
   - 用户直接问过去："上次是什么时候"、"我之前去过吗"、"我一般几点出门"
   - 周期性安排："每周三上课"（查历史确认周期，别造重复条目）
   - 用户只说事不说时间，需要从历史补全
   - 用户的说法与事实/偏好冲突，需要核对

   不必查：
   - 时间明确的新任务："明天下午 3 点开会"
   - 问天气、问当前日程（上面的日程与待办已经列全）
   - 纯闲聊
   - 已经查过一次且返回为空，不要反复调用

   查到历史之后：它是"过去"，不是待办。不要把它当成当前任务列表，
   也不要把已经做过的事当成新任务重复添加，除非用户明确说要再做一次。

10. 位置相关工具（只在需要"用户在哪"时才用）：
   - 查天气但用户没说城市 → 先调 get_current_location，把返回的坐标传给 get_weather_forecast。
   - 规划路线但用户没说起点 → plan_traffic 会自动用当前位置，不用传 origin。
   - "附近的加油站""最近的麦当劳" → 先 search_place；要路线再把地点名传给 plan_traffic。
   - 用户已经说了城市或起点 → 直接用他说的，不要再调定位，浪费一次往返。
   - 路况只有"当前时刻"：用户说"明天下午三点"时，把 when 填进去，
     并把工具返回的"仅供参考"原样转告用户，绝不能把它说成那时的路况。
   - 定位失败（用户拒绝授权、没有 IP 结果）时，直接问用户所在城市，不要猜。
"""


# ---------------------------------------------------------------------------
# 记忆蒸馏
# ---------------------------------------------------------------------------
MEMORY_PROMPT = """你是记忆分析助手。请分析下面这轮对话，更新用户的长期事实。

【现有事实】
{old_facts}

【本轮对话】
用户：{user_msg}
助手：{assistant_msg}

规则：
1. 只记录**用户自己说过**的稳定信息：身份、学校、单位、项目、经历等。
2. 禁止把工具返回的数据当成用户的事实：
   - 日程、天气、当前时间都只是数据；**不得**从日程反推规律
     （例如日程里 9-22 和 9-24 都有"信息论"，不能写成"这门课在周二和周四"）。
3. 一次性的安排不写进 facts（那是日程，不是长期事实）。
4. **不要记录偏好**——偏好由用户自己在界面上维护，你只负责事实。
5. 与旧事实冲突时用新信息更新，输出合并后的完整列表。
6. 无关闲聊（打招呼、问时间）不产生任何事实。

严格输出 JSON，不要有多余文字，不要用 markdown 代码块：

{{"facts": ["..."]}}

没有任何变化时，原样返回旧事实。"""


def _clean_json_text(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        lines = [ln for ln in text.splitlines() if not ln.strip().startswith("```")]
        text = "\n".join(lines).strip()
    if text[:4].lower() == "json":
        text = text[4:].strip()
    return text


def distill_memory(user_msg: str, assistant_msg: str, memory: dict, deepseek_key: str) -> dict | None:
    """把一轮对话蒸馏进记忆，返回更新后的 memory（失败返回 None）。

    记忆由调用方（server/app.py）负责读写，这里只算 —— 这样 agent 层
    不依赖具体存储。
    """
    old_facts = "\n".join(f"- {f}" for f in memory.get("facts") or []) or "（无）"

    prompt = MEMORY_PROMPT.format(
        old_facts=old_facts, user_msg=user_msg, assistant_msg=assistant_msg,
    )

    try:
        raw = build_memory_llm(deepseek_key).invoke(prompt).content
    except Exception as e:
        logger.warning("记忆蒸馏调用失败：%s: %s", type(e).__name__, str(e)[:160])
        return None

    text = _clean_json_text(raw)
    try:
        data = json.loads(text)
    except Exception as e:
        logger.warning("记忆蒸馏返回的 JSON 解析失败：%s / 原文=%r", e, text[:160])
        return None
    if not isinstance(data, dict):
        logger.warning("记忆蒸馏返回的不是对象，跳过")
        return None

    updated = dict(memory)
    updated["facts"] = data.get("facts", memory.get("facts") or [])
    # 偏好归用户所有：蒸馏只写 facts，绝不覆盖 preferences
    updated["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    return updated


# ---------------------------------------------------------------------------
# 任务抽取
# ---------------------------------------------------------------------------
def _normalize_task_items(raw_tasks) -> list[dict]:
    """把 emit_tasks 的入参整理成带 id 的任务字典列表（非流式与流式共用）。"""
    tasks = []
    for t in raw_tasks or []:
        if isinstance(t, BaseModel):
            t = t.model_dump()
        if not isinstance(t, dict) or not t.get("title"):
            continue
        item = dict(t)
        item["id"] = "task-" + uuid.uuid4().hex[:8]
        item.setdefault("start", None)
        item.setdefault("end", None)
        item.setdefault("location", "")
        item.setdefault("note", "")
        item.setdefault("duplicate_of", "")
        tasks.append(item)
    return tasks


def tasks_from_emit_input(payload) -> list[dict]:
    """从 emit_tasks 的工具入参（dict）里抽出任务。"""
    if isinstance(payload, dict):
        return _normalize_task_items(payload.get("tasks", []))
    return []


def _extract_tasks(steps_raw: list) -> tuple[list, list]:
    """从 intermediate_steps 里分出 emit_tasks 的任务和要展示的步骤。"""
    tasks, display = [], []
    for action, observation in steps_raw:
        if action.tool == "emit_tasks":
            tasks.extend(tasks_from_emit_input(action.tool_input or {}))
        else:
            display.append({
                "tool": action.tool,
                "args": action.tool_input,
                "result": str(observation),
            })
    return tasks, display


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def _format_schedule(events: list[dict]) -> str:
    if not events:
        return "（暂无日程）"
    lines = []
    for e in events:
        time_range = e.get("start", "")
        if e.get("end"):
            time_range += f" ~ {e['end']}"
        extra = []
        if e.get("location"):
            extra.append(f"地点 {e['location']}")
        if e.get("note"):
            extra.append(f"备注 {e['note']}")
        suffix = ("（" + "，".join(extra) + "）") if extra else ""
        # id 必须给到模型，否则 emit_tasks 的 duplicate_of 无从填起
        lines.append(f"- [id={e.get('id', '')}] {time_range}：{e.get('title', '')}{suffix}")
    return "\n".join(lines)


def _format_pending(pending: dict) -> str:
    confirmed = pending.get("confirmed") or []
    planned = pending.get("planned") or []
    lines = []
    if confirmed:
        lines.append("待确认：")
        for t in confirmed:
            rng = t.get("start") or ""
            if t.get("end"):
                rng += f" ~ {t['end']}"
            lines.append(f"- [id={t.get('id', '')}] {t.get('title', '')}（{rng}）")
    if planned:
        lines.append("待计划：")
        for t in planned:
            lines.append(f"- [id={t.get('id', '')}] {t.get('title', '')}")
    return "\n".join(lines) if lines else "（暂无）"


def _known_ids(events, pending) -> set[str]:
    """现有日程与待办的全部 id，用于校验模型填的 duplicate_of 是否真实存在。"""
    ids: set[str] = set()
    for event in events or []:
        if event.get("id"):
            ids.add(str(event["id"]))
    for bucket in ("confirmed", "planned"):
        for task in (pending or {}).get(bucket) or []:
            if task.get("id"):
                ids.add(str(task["id"]))
    return ids


def _split_duplicates(tasks: list[dict], events, pending) -> tuple[list[dict], list[dict]]:
    """按模型给的 duplicate_of 把任务分成（新任务, 重复任务）。

    ⚠️ 不信任模型的 id：duplicate_of 指向不存在的 id 时，当作没有填 —— 宁可多一条
    待确认，也不能因为一个幻觉 id 把用户真实的任务吞掉。
    """
    known = _known_ids(events, pending)
    fresh, duplicated = [], []
    for task in tasks:
        ref = str(task.get("duplicate_of") or "").strip()
        if ref and ref in known:
            duplicated.append(task)
        else:
            if ref:
                logger.warning("emit_tasks 给了不存在的 duplicate_of=%s，按新任务处理", ref)
                task = {**task, "duplicate_of": ""}
            fresh.append(task)
    return fresh, duplicated


def _build_executor(deepseek_key: str, phone: str = "",
                    location: dict | None = None) -> AgentExecutor:
    """构造 agent。非流式与流式共用同一套提示词与工具，避免两边行为漂移。"""
    prompt = ChatPromptTemplate.from_messages([
        ("system", SYSTEM_PROMPT),
        ("placeholder", "{chat_history}"),
        ("human", "{input}"),
        ("placeholder", "{agent_scratchpad}"),
    ])
    llm = build_llm(deepseek_key)
    tools = build_tools(phone, location)   # 每次调用都会新绑 phone / location 闭包
    agent = create_tool_calling_agent(llm, tools, prompt)
    return AgentExecutor(
        agent=agent, tools=tools,
        verbose=False, return_intermediate_steps=True, max_iterations=8,
    )


def _build_inputs(user_input: str, *, events, pending, memory_text, history) -> dict:
    """构造 agent 入参。"""
    from langchain_core.messages import AIMessage, HumanMessage

    now = datetime.now()
    weekday_names = ["一", "二", "三", "四", "五", "六", "日"]

    chat_history = []
    for m in history or []:
        if m.get("role") == "user":
            chat_history.append(HumanMessage(content=m.get("content", "")))
        else:
            chat_history.append(AIMessage(content=m.get("content", "")))

    return {
        "input": user_input,
        "chat_history": chat_history,
        "now": now.strftime("%H:%M"),
        "today": now.strftime("%Y-%m-%d"),
        "weekday": weekday_names[now.weekday()],
        "schedule_text": _format_schedule(events or []),
        "pending_text": _format_pending(pending or {}),
        "memory_text": memory_text,
    }


def chat(
    user_input: str,
    *,
    deepseek_key: str,
    phone: str = "",
    location: dict | None = None,
    events: list[dict] | None = None,
    pending: dict | None = None,
    memory_text: str = "（暂无长期记忆）",
    history: list[dict] | None = None,
) -> dict:
    """处理一轮对话（**非流式**，一次返回完整结果）。

    日程/待办/记忆由调用方传入 —— agent 层不碰存储；phone 与 location 只用来
    绑定"查历史"和位置类工具。
    """
    executor = _build_executor(deepseek_key, phone, location)
    result = executor.invoke(_build_inputs(
        user_input, events=events, pending=pending,
        memory_text=memory_text, history=history,
    ))

    tasks, steps = _extract_tasks(result.get("intermediate_steps", []))
    # 查重由模型在 emit_tasks 里用 duplicate_of 标注；这里只做过滤与 id 校验
    tasks, _ = _split_duplicates(tasks, events, pending)
    return {"output": result.get("output", ""), "steps": steps, "tasks": tasks}


# ---------------------------------------------------------------------------
# 流式对话
# ---------------------------------------------------------------------------
def _chunk_text(chunk) -> str:
    """从 AIMessageChunk 里取出纯文本。

    有些兼容 OpenAI 的服务商会把 content 给成 list（[{'type':'text','text':...}]），
    这里两种形态都兜住。
    """
    content = getattr(chunk, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return "".join(parts)
    return ""


async def chat_stream(
    user_input: str,
    *,
    deepseek_key: str,
    phone: str = "",
    location: dict | None = None,
    events: list[dict] | None = None,
    pending: dict | None = None,
    memory_text: str = "（暂无长期记忆）",
    history: list[dict] | None = None,
):
    """流式处理一轮对话，逐步 yield 事件字典。

    事件类型：
        {"type": "segment"}                      新的模型轮次开始
        {"type": "token",   "text": "..."}        一个文本增量
        {"type": "step",    "tool": ..., "args": ...}   开始调用某个工具
        {"type": "tasks",   "tasks": [...]}       识别到任务，立刻下发
        {"type": "done",    "output": "...", "steps": [...]}
        {"type": "error",   "message": "..."}

    ⚠️ 关于「思考过程」和「最终回答」的区分（这是流式改造里最容易出错的地方）：
    agent 每一轮都会让模型输出一次，调工具的那几轮也可能先吐一小段文字，
    那段文字**不是**给用户的回答。区分办法是**按 run_id 切段**：
    一次模型调用 = 一段；最后一段（agent 结束后不再调工具的那次）才是回答。
    所以前端要在收到 segment 事件时，把上一段文字挪进「推理过程」区。
    """
    executor = _build_executor(deepseek_key, phone, location)
    inputs = _build_inputs(
        user_input, events=events, pending=pending,
        memory_text=memory_text, history=history,
    )

    steps: list[dict] = []
    segments: list[list[str]] = []
    current_run = None
    final_output = ""

    async for ev in executor.astream_events(inputs, version="v2"):
        kind = ev.get("event") or ""

        if kind == "on_chat_model_stream":
            run_id = ev.get("run_id")
            if run_id != current_run:
                # 上一次模型调用的文字已经收完了；接下来的是新的一段
                if current_run is not None:
                    yield {"type": "segment"}
                current_run = run_id
                segments.append([])
            text = _chunk_text(ev["data"].get("chunk"))
            if text:
                segments[-1].append(text)
                yield {"type": "token", "text": text}

        elif kind == "on_tool_start":
            name = ev.get("name") or ""
            args = ev["data"].get("input") or {}
            if name == "emit_tasks":
                # 伪工具：它不查任何东西，只是把识别结果交回系统，不进推理过程
                found = tasks_from_emit_input(args)
                if found:
                    fresh, duplicated = _split_duplicates(found, events, pending)
                    if duplicated:
                        # 被判为重复的不下发；模型已经在回复里问用户"还要再加吗"
                        logger.info("查重命中，%d 条未下发：%s", len(duplicated),
                                    [t.get("title") for t in duplicated])
                    if fresh:
                        yield {"type": "tasks", "tasks": fresh}
                continue
            steps.append({"tool": name, "args": args, "result": ""})
            yield {"type": "step", "tool": name, "args": args}

        elif kind == "on_tool_end":
            name = ev.get("name") or ""
            if name == "emit_tasks":
                continue
            observation = str(ev["data"].get("output") or "")[:500]
            for step in reversed(steps):
                if step["tool"] == name and not step["result"]:
                    step["result"] = observation
                    break

        elif kind == "on_chain_end":
            out = ev["data"].get("output")
            # AgentExecutor 结束时给出的完整结果，用它作为最终回答的权威来源
            if isinstance(out, dict) and isinstance(out.get("output"), str):
                if out.get("output"):
                    final_output = out["output"]
                if out.get("intermediate_steps"):
                    _, display = _extract_tasks(out["intermediate_steps"])
                    if display:
                        steps = display

    if not final_output:
        # 兜底：拿不到权威输出时（比如达到 max_iterations 被截断），取最后一段
        final_output = "".join(segments[-1]) if segments else ""

    yield {"type": "done", "output": final_output, "steps": steps}
