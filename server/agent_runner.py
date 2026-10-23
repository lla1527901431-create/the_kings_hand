"""Agent 组装与调度。

多用户改造后这里有两个关键变化：

1. **LLM 客户端按请求构造**。以前 llm / executor 是模块级全局单例，
   现在每个用户用自己的 DeepSeek key，所以必须每次请求现造。
   为了不重复建连接，按 (user_id, key指纹) 做了一层小缓存。

2. **对话历史按用户隔离**。以前 _recent 是一个全局列表，
   多用户下会互相串话（A 说的话出现在 B 的上下文里）。

真正决定"数据属于谁"的仍然是数据库的 RLS，这里的分桶只是为了
对话上下文不串。
"""

import json
import uuid
from datetime import datetime, timedelta

from langchain_classic.agents import AgentExecutor, create_tool_calling_agent
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.tools import tool
from pydantic import BaseModel, Field

import config
from agent.llm import build_llm, build_memory_llm, credentials_source
from agent.memory_store import format_memory_text, load_memory, save_memory
from agent.tools import get_current_time, list_calendar_events
from agent.date_tools import resolve_date, resolve_datetime
from agent.weather_openmeteo import get_weather_forecast
from server.logging_config import get_logger

logger = get_logger("agent")


class TaskItem(BaseModel):
    title: str = Field(description="任务标题，例如 '约会'")
    start: str | None = Field(
        default=None,
        description="开始时间，格式 'YYYY-MM-DD HH:MM'。用户没有提到具体时间时留空。",
    )
    end: str | None = Field(
        default=None,
        description="结束时间，格式 'YYYY-MM-DD HH:MM'。用户没有提到结束时间时留空。",
    )
    location: str = Field(default="", description="地点，例如 '科技楼301'。没有就留空。")
    note: str = Field(default="", description="备注，例如 '陪女朋友吃饭'。没有就留空。")


@tool
def emit_tasks(tasks: list[TaskItem]) -> str:
    """当你从用户消息里识别到一个或多个日程/任务时，调用这个工具，把任务列表结构化地传给系统。
    注意：
    - 这个工具不会真的往日历里写东西，只是把识别结果传给系统，系统会展示在左侧待确认列表里。
    - 如果用户消息里没有任何日程/任务（比如闲聊、查天气、查询已有日程），不要调用这个工具。
    - 时间一律用 'YYYY-MM-DD HH:MM' 格式。模糊时间（如"明天下午"）start/end 留空。
    - 只有开始时间没有结束时间时，end 留空，系统会自动往后补 1 小时。
    - 只有结束时间没有开始时间时，start 留空，系统会自动往前补 1 小时。
    - 时间必须来自 resolve_datetime 的返回值，照抄，不要自己拼。
    """
    return f"已捕获 {len(tasks)} 个任务"


tools = [
    get_current_time,
    resolve_date,
    resolve_datetime,
    list_calendar_events,
    get_weather_forecast,
    emit_tasks,
]

prompt = ChatPromptTemplate.from_messages([
    (
        "system",
        """你是 The King's Hand，一个日历助手。

今天是 {today}，星期{weekday}。

【用户长期记忆】
{memory_text}

你的工作规则：
1. 当用户消息里包含日程/任务时，按以下流程处理：

   第一步：查重
   调用 list_calendar_events 查看已有日程。
   - 如果发现完全相同的任务（标题、时间都一致），不要重复添加。
     直接告诉用户"这个任务已经在你的日历里了"，并说明它的时间，不要再调 emit_tasks。
   - 如果没有重复，继续下一步。

   第二步：判断时间是否明确
   - "明确时间"指可以落到具体某一天的某个时刻，比如"明天下午3点"、"周五上午10点"、"9月24日晚上7点半"。
   - "模糊时间"或"没有时间"指无法落到具体时刻，比如"明天下午"、"下周找个时间"、"改天"、"有空的时候"。
   - 如果时间模糊或没有时间：
     a) 先查询能查的信息，比如任务涉及出行/户外就查天气。
     b) 在回复里把可查到的信息告诉用户（如天气、已有日程），然后追问具体时间。
     c) 追问时不要调 emit_tasks。
   - 如果用户已经在本轮或前几轮给出了明确时间，跳到第三步。

   第三步：根据情况决定是否还要追问
   - 如果上一轮你追问了时间，而用户这一轮给出了明确时间，进入第四步。
   - 如果用户这一轮仍然只给模糊时间或没有回答时间，则把任务按无时间处理，进入第四步。

   第四步：天气主动提醒（不要等用户问，这一步很重要）
   - 只要任务**可能涉及户外**，就主动调用 get_weather_forecast 查天气，
     并在 note 和回复里给出提醒。判断依据包括但不限于：
     运动（篮球、跑步、骑行、游泳）、出行（跨城、机场、车站、自驾）、
     户外活动（露营、爬山、拍照、户外聚会），以及地点明显在室外的任务。
   - 时间明确就直接查那一天的天气；时间不明确就查用户提到的大致时间范围，
     并说明"具体时间定了可以再确认一次"。
   - 提醒要具体、可行动，不要只说结论。例如：
     "9/26 周六有中雨、降水概率 42%，建议带伞或改到周日（晴，27%）"。
   - 天气不好就主动给备选时间；天气很好简单说一句即可。
   - 反过来，纯室内任务（开会、上课、吃饭、写代码）**不要**查天气，
     也不要往 note 里塞无关的天气信息。
   - 如用户没有给出地点，则追问一轮地点后再查天气。

   第五步：调用 emit_tasks 把任务结构化传给系统
   - 明确时间：start 填具体时间，end 可留空（系统会自动补 1 小时）。
   - 模糊或无时间：start 和 end 都留空。
   - note 字段：把值得关注的信息以简明扼要的形式写进去。
     例如：天气提醒（"下午有雨，建议带伞"）、冲突信息（"与 10:00 开会冲突"）、
     任务性质（"户外活动"）。没有值得关注的信息就留空。

   第六步：用自然语言回复用户
   - 明确时间的任务：告诉用户"已将任务添加到 To be confirmed "。
   - 无明确时间的任务：告诉用户"已将任务添加到 To be planned "。
   - 天气提醒、冲突信息、note 里的内容，都要在回复里一并告诉用户并给出建议。

2. 不要在对话里直接说"已添加"、"已写入日历"，因为你无法直接写日历。
   写日历由用户在左侧点确认按钮完成。

3. 如果用户只是问天气、时间、已有日程，正常调用对应工具回答，不要调 emit_tasks。

4. 如果用户只是闲聊，正常聊天，不调用任何工具。

5. 时间约定：
   早八指 8:15-9:50，早十指 10:10-11:45，晚上的课 19:30-21:05，
   下午第一节 14:30-16:05，下午第二节 16:25-18:00。

6. 参考【用户长期记忆】里的偏好来给建议，但不要在回复里直接复述这些记忆。

7. 日期和时间必须用工具算，严禁自己推算：
   - 任何涉及"星期几""哪天""这周末""下周三"的问题，先调 resolve_date 拿到具体日期，
     再根据返回值告诉用户。返回里已经带了星期和"该周末 / 下周末"的日期，直接照着说。
   - 要往 emit_tasks 里填 start / end 时，先调 resolve_datetime 拿到
     'YYYY-MM-DD HH:MM'，然后**原样照抄**，一个字符都不要自己拼。
   - 没有调用过工具，就不要填具体时间。
   - 用户口语里的"周末"一般指周六；如果一件事横跨周六和周日，拆成两天分别 emit_tasks。

8. 如果【用户长期记忆】里的信息与本轮对话冲突，以本轮对话为准，并提醒用户记忆可能过时了。
""",
    ),
    ("placeholder", "{chat_history}"),
    ("human", "{input}"),
    ("placeholder", "{agent_scratchpad}"),
])


# ---------------------------------------------------------------------------
# 按用户隔离的对话历史
#
# 每个用户一条记录：{"messages": [...], "last_active": datetime}
# 只增不减会导致内存泄漏（每个来过一次的用户永久占一份），所以按空闲时间回收。
# ---------------------------------------------------------------------------
_recent_by_user: dict[str, dict] = {}

# 空闲多久之后回收该用户的对话上下文
IDLE_TIMEOUT_SECONDS = config.RECENT_HISTORY_IDLE_MINUTES * 60


def _now() -> datetime:
    return datetime.now()


def _cleanup_idle_users() -> int:
    """清掉长时间没说话的用户上下文，返回清理了几个。

    放在每次对话时顺手做，不需要额外的定时任务。
    注意：只清"对话上下文"，**不影响**用户的日历、待办和长期记忆。
    """
    cutoff = _now() - timedelta(seconds=IDLE_TIMEOUT_SECONDS)
    stale = [uid for uid, rec in _recent_by_user.items() if rec.get("last_active", cutoff) < cutoff]
    for uid in stale:
        _recent_by_user.pop(uid, None)
    if stale:
        logger.info("回收了 %d 个空闲用户的对话上下文（超过 %d 分钟没活动）",
                    len(stale), config.RECENT_HISTORY_IDLE_MINUTES)
    return len(stale)


def _history_for(user_id: str) -> list:
    record = _recent_by_user.get(user_id)
    if record is None:
        record = {"messages": [], "last_active": _now()}
        _recent_by_user[user_id] = record
    return record["messages"]


def _remember(user_id: str, user_msg: str, reply: str) -> None:
    # 顺手回收空闲用户，避免字典无限增长
    _cleanup_idle_users()

    history = _history_for(user_id)
    history.append(HumanMessage(content=user_msg))
    history.append(AIMessage(content=reply))
    while len(history) > config.RECENT_HISTORY_MAX:
        history.pop(0)

    _recent_by_user[user_id]["last_active"] = _now()


def reset_history(user_id: str | None = None) -> None:
    """清空某个用户的对话历史；user_id 为空时清空全部（仅用于测试）。"""
    if user_id is None:
        _recent_by_user.clear()
    else:
        _recent_by_user.pop(user_id, None)


def history_size(user_id: str) -> int:
    """给测试/调试用：看某个用户上下文里有多少条消息。"""
    return len(_recent_by_user.get(user_id, {}).get("messages", []))


def tracked_user_count() -> int:
    """给测试/调试用：当前在内存里跟踪了多少个用户。"""
    return len(_recent_by_user)


# ---------------------------------------------------------------------------
# 记忆蒸馏
# ---------------------------------------------------------------------------
_MEMORY_PROMPT_TEMPLATE = """你是记忆分析助手。请分析下面这轮对话，更新用户的长期记忆。

【现有事实】
{old_facts}

【现有观察台账】（每条只记录"某次发生了什么"，不代表结论）
{old_observations}

【现有偏好】
{old_prefs}

【本轮对话】
用户：{user_msg}
助手：{assistant_msg}

记忆分三块，请分别更新：

━━ facts（用户明确说过的事实）━━
1. facts 只记录**用户自己说过**的稳定信息：身份、学校、单位、项目、经历、技术栈等。
2. 禁止把工具返回的数据当成用户的事实：
   - 禁止从日程列表反推规律。例如日历里 9-22 和 9-24 都有"信息论"，**不得**写成
     "这门课在周二和周四"。日历条目只是记录，用户从没说过它是周期性的。
   - 禁止从天气结果、当前时间推任何事实。
3. 一次性的安排不写进 facts。"用户计划 10-01 打篮球"属于日程，不是长期事实。
4. 与旧事实冲突时用新信息更新，输出合并后的完整列表。

━━ observations（观察台账：攒证据的地方）━━
5. 把本轮**用户的行为或说法**记成一条观察，格式："YYYY-MM-DD：简述用户做了什么/说了什么"。
   - 只写这一轮实际发生的，不要写你的推断。
   - 例如用户说"这个放到下午吧，我上午要睡觉" → 记
     "2026-09-28：要求把事情安排到下午，提到上午要睡觉"。
6. **去重与合并**：如果本轮的情况和台账里已有的观察是同一类现象，
   **不要新加一条**，而是把那条改写成累计形式（或更新它的日期），例如：
     "2026-09-20：想把事情放下午，提到上午要睡觉"
   → "2026-09-20、2026-09-28：多次想把事情安排到下午，理由是想上午睡觉"
7. 台账最多 30 条，超出时丢掉最旧的。
8. 与用户说的无关的闲聊（打招呼、问时间）不必记录。

━━ preferences（偏好：从重复观察归纳而来，这块鼓励推断）━━
9. **重点规则：同一个现象被观察到多次（至少 2~3 次），就归纳成一条偏好。**
   例：台账里多次出现"想安排到下午，上午要睡觉" → 归纳出
   "用户上午喜欢睡觉，倾向于把事情安排在下午"。
   这类偏好非常有用，请主动归纳，不要只当被动记录员。
10. 归纳出的偏好请在后面标注来源，例如：
    "用户上午喜欢睡觉，倾向于把事情安排在下午（据多次观察归纳）"。
    用户自己明确说过的偏好则不用标注。
11. **只有一次的行为不要写成偏好**，先留在 observations 里等下次。
12. 不要从单次行为推断偏好。例如用户这次想找不下雨的日期，
    **不等于**"用户偏好降水概率最低的日期"。
13. 如果用户明确否认了某条偏好，直接从 preferences 里删掉。
14. 拿不准的宁可先记进 observations，也不要急着当成偏好。

━━ 输出 ━━
严格输出 JSON，不要有多余文字，不要用 markdown 代码块：

{{"facts": ["..."], "observations": ["..."], "preferences": ["..."]}}

没有任何一块需要变化时，原样返回该块的内容。"""


def _clean_json_text(text: str) -> str:
    """清洗 LLM 返回的文本（去掉 markdown 代码块围栏）。"""
    text = (text or "").strip()
    if text.startswith("```"):
        lines = [ln for ln in text.splitlines() if not ln.strip().startswith("```")]
        text = "\n".join(lines).strip()
    # 模型偶尔会加一句 "json" 前缀
    if text[:4].lower() == "json":
        text = text[4:].strip()
    return text


def _distill_memory(user_msg: str, assistant_msg: str, credentials: dict | None = None) -> None:
    """把这轮对话蒸馏进长期记忆。

    注意：这里读写的记忆属于**当前请求的用户**（身份在 db 层由 JWT 决定）。
    """
    memory = load_memory()
    old_facts = "\n".join(f"- {f}" for f in memory.get("facts", [])) or "（无）"
    old_prefs = "\n".join(f"- {p}" for p in memory.get("preferences", [])) or "（无）"
    old_obs = memory.get("observations", [])
    old_observations = "\n".join(f"- {o}" for o in old_obs) or "（暂无）"

    prompt_text = _MEMORY_PROMPT_TEMPLATE.format(
        old_facts=old_facts,
        old_observations=old_observations,
        old_prefs=old_prefs,
        user_msg=user_msg,
        assistant_msg=assistant_msg,
    )

    try:
        client = build_memory_llm(**(credentials or {}))
        response = client.invoke(prompt_text)
        raw = response.content
    except Exception as e:
        # 记忆蒸馏失败不应该影响主流程
        logger.warning("记忆蒸馏调用失败：%s: %s", type(e).__name__, str(e)[:200])
        return

    text = _clean_json_text(raw)
    try:
        data = json.loads(text)
    except Exception as e:
        logger.warning("记忆蒸馏返回的 JSON 解析失败：%s / 原始输出=%r", e, text[:200])
        return

    if not isinstance(data, dict):
        logger.warning("记忆蒸馏返回的不是对象，跳过")
        return

    memory["facts"] = data.get("facts", memory.get("facts", []))
    memory["observations"] = data.get("observations", old_obs)
    memory["preferences"] = data.get("preferences", memory.get("preferences", []))
    memory["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    try:
        save_memory(memory)
    except Exception as e:
        logger.warning("记忆保存失败：%s: %s", type(e).__name__, str(e)[:200])


def _extract_tasks_from_steps(steps_raw: list) -> tuple[list, list]:
    """从 intermediate_steps 里分出 emit_tasks 的任务和要展示给用户的 steps。"""
    tasks = []
    display_steps = []
    for action, observation in steps_raw:
        if action.tool == "emit_tasks":
            tool_input = action.tool_input or {}
            raw_tasks = tool_input.get("tasks", []) if isinstance(tool_input, dict) else []
            for t in raw_tasks:
                if isinstance(t, BaseModel):
                    t = t.model_dump()
                if not isinstance(t, dict) or not t.get("title"):
                    continue
                t["id"] = "task-" + uuid.uuid4().hex[:8]
                t.setdefault("start", None)
                t.setdefault("end", None)
                t.setdefault("location", "")
                t.setdefault("note", "")
                tasks.append(t)
        else:
            display_steps.append({
                "tool": action.tool,
                "args": action.tool_input,
                "result": str(observation),
            })
    return tasks, display_steps


def _build_executor(credentials: dict) -> AgentExecutor:
    """按用户凭据构造一次性的 executor。"""
    llm = build_llm(**credentials)
    agent = create_tool_calling_agent(llm, tools, prompt)
    return AgentExecutor(
        agent=agent,
        tools=tools,
        verbose=False,
        return_intermediate_steps=True,
        max_iterations=8,
    )


def chat(
    user_input: str,
    user_id: str,
    credentials: dict | None = None,
    distill: bool = True,
) -> dict:
    """处理一轮对话。

    user_id 只用于隔离对话历史；数据归属由数据库 RLS 按 JWT 判定。
    credentials: {"deepseek_key": "...", "model": "..."} 缺省则用服务器兜底 key。
    """
    credentials = credentials or {}
    now = datetime.now()
    weekday_names = ["一", "二", "三", "四", "五", "六", "日"]

    memory = load_memory()
    memory_text = format_memory_text(memory)

    executor = _build_executor(credentials)
    result = executor.invoke({
        "input": user_input,
        "chat_history": _history_for(user_id),
        "today": now.strftime("%Y-%m-%d"),
        "weekday": weekday_names[now.weekday()],
        "memory_text": memory_text,
    })

    reply = result["output"]
    _remember(user_id, user_input, reply)

    tasks, steps = _extract_tasks_from_steps(result.get("intermediate_steps", []))

    if distill:
        _distill_memory(user_input, reply, credentials)

    return {
        "output": reply,
        "steps": steps,
        "tasks": tasks,
        "key_source": credentials_source(credentials.get("deepseek_key")),
    }
