"""过期日程沉淀的冒烟测试（不联网、不碰真实数据库）。

覆盖：
    1. 边界：本周一之前才沉淀，本周内与未来的不动
    2. 分桶：≤7 天逐条、≤30 天按天、≤180 天按周、更久按月
    3. 事务：先写 history 再删 events，条数对得上
    4. 幂等：重复跑不会重复记账
    5. 失败不删：摘要调用抛异常时，一条日程都不许删
    6. dry-run：只算不写不删
    7. 用户手动删除的事件不进入记忆
    8. 不串用户

用法：
    .venv\\Scripts\\python.exe tools/smoke_memory.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TMP_DIR = tempfile.mkdtemp(prefix="kh_memory_")
os.environ["DB_PATH"] = os.path.join(TMP_DIR, "memory.db")
os.environ["DEBUG"] = "false"
os.environ["LOG_LEVEL"] = "ERROR"

from agent import memory_history                     # noqa: E402
from db import calendars, database, memories         # noqa: E402

PASS, FAIL = 0, 0
NOW = datetime(2026, 10, 22, 14, 0)                   # 周四；本周一 = 2026-10-19 00:00
PHONE = "13800000001"
OTHER = "13800000002"


def check(label, ok, extra=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {label}{('  -> ' + str(extra)) if extra else ''}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}{('  -> ' + str(extra)) if extra else ''}")


def reset_db():
    # 必须先把当前线程的连接关掉：Windows 上打开着的文件删不掉
    conn = getattr(database._local, "conn", None)
    if conn is not None:
        conn.close()
        database._local.conn = None
    database._initialized = False
    for suffix in ("", "-wal", "-shm"):
        path = os.environ["DB_PATH"] + suffix
        if os.path.exists(path):
            os.remove(path)
    database.init_db()


def add(phone, title, start, event_id=None):
    row = calendars.add_event(phone, title, start, None, "", "")
    if event_id:
        database.execute("UPDATE events SET id = ? WHERE id = ?", (event_id, row["id"]))
        row["id"] = event_id
    return row


def stub(granularity_count=None):
    """桩摘要：把标题写进摘要，便于断言"删掉的那条没被记进来"。"""
    def _summarize(_key, bucket):
        titles = "、".join(str(e.get("title")) for e in bucket["events"])
        return [f"{bucket['granularity']}:{len(bucket['events'])}:{titles}"]
    return _summarize


def events_of(phone):
    return calendars.list_events(phone)


print(f"临时库：{os.environ['DB_PATH']}")
print(f"基准时间：{NOW}  ->  本周一 {memory_history.monday_start(NOW)}")

# ---------------------------------------------------------------------------
print("\n[1] 边界与分桶")
reset_db()
add(PHONE, "上周日的事", "2026-10-18 15:00")        # age 4  → event
add(PHONE, "十天前的事", "2026-10-12 09:00")        # age 10 → day
add(PHONE, "二十天前的事", "2026-09-30 09:00")      # age 22 → day
add(PHONE, "两个月前的事", "2026-08-20 09:00")      # age 63 → week
add(PHONE, "半年前的事", "2026-03-01 09:00")        # age 235 → month
add(PHONE, "本周内的事", "2026-10-20 10:00")        # 不早于本周一 → 不动
add(PHONE, "未来的事", "2026-10-25 10:00")          # 不动
add(OTHER, "别人的过期事", "2026-10-01 09:00")      # 不串用户

result = memory_history.distill(PHONE, "sk-test", now=NOW, summarize=stub())
check("处理了 5 条过期日程", result["expired"] == 5, result)
check("分成 5 个桶", result["buckets"] == 5, result)
check("删除了 5 条", result["deleted"] == 5, result)
check("自己的未过期日程还在", len(events_of(PHONE)) == 2, len(events_of(PHONE)))
check("其他用户的数据没动", len(events_of(OTHER)) == 1)

history = memories.load_history(PHONE)
check("history 有 5 条", len(history) == 5, len(history))
granularities = sorted({h["granularity"] for h in history})
check("四种粒度都出现", granularities == ["day", "event", "month", "week"], granularities)
month = [h for h in history if h["granularity"] == "month"][0]
check("月桶的区间是整月", month["from"].startswith("2026-03") and month["to"].startswith("2026-03"),
      (month["from"], month["to"]))
check("count 与事件数一致", sum(h["count"] for h in history) == 5)

# ---------------------------------------------------------------------------
print("\n[2] 幂等：再跑一次什么都不做")
again = memory_history.distill(PHONE, "sk-test", now=NOW, summarize=stub())
check("没有新的过期日程", again["expired"] == 0, again)
check("history 仍是 5 条", len(memories.load_history(PHONE)) == 5)

# ---------------------------------------------------------------------------
print("\n[3] 用户手动删除的事件不进入记忆")
reset_db()
kept = add(PHONE, "留下的", "2026-10-17 10:00")
gone = add(PHONE, "用户删掉的", "2026-10-16 10:00")
calendars.delete_event(PHONE, gone["id"])
result = memory_history.distill(PHONE, "sk-test", now=NOW, summarize=stub())
history = memories.load_history(PHONE)
check("只沉淀了留下的那条", result["deleted"] == 1, result)
check("history 里没有已删除的那条", all("用户删掉的" not in "".join(h["summary"]) for h in history),
      history)
check("history 里有留下的那条", any("留下的" in "".join(h["summary"]) for h in history), history)

# ---------------------------------------------------------------------------
print("\n[4] 摘要失败：一条都不许删")
reset_db()
add(PHONE, "甲", "2026-10-17 10:00")
add(PHONE, "乙", "2026-10-16 10:00")

def boom(_key, _bucket):
    raise RuntimeError("模拟 LLM 失败")

try:
    memory_history.distill(PHONE, "sk-test", now=NOW, summarize=boom)
    raised = False
except RuntimeError:
    raised = True
check("异常向上抛出", raised)
check("事件一条没少", len(events_of(PHONE)) == 2, len(events_of(PHONE)))
check("history 没有被写入", len(memories.load_history(PHONE)) == 0)

# ---------------------------------------------------------------------------
print("\n[5] dry-run 只算不写")
result = memory_history.distill(PHONE, "sk-test", now=NOW, summarize=stub(), dry_run=True)
check("dry-run 标记为 True", result.get("dry_run") is True, result)
check("dry-run 没删事件", len(events_of(PHONE)) == 2)
check("dry-run 没写 history", len(memories.load_history(PHONE)) == 0)
check("dry-run 仍然给出了摘要", len(result.get("entries") or []) == 2, result.get("entries"))

# ---------------------------------------------------------------------------
print("\n[6] max_buckets 限流")
reset_db()
for i in range(6):
    add(PHONE, f"任务{i}", f"2026-10-{10 + i} 10:00")   # 都是 day/event 桶，互不合并
result = memory_history.distill(PHONE, "sk-test", now=NOW, summarize=stub(), max_buckets=3)
check("只处理 3 个桶", result["buckets"] == 3, result)
check("删除了 3 条", result["deleted"] == 3, result)
check("还剩 3 条没处理", result["remaining"] == 3, result)

# ---------------------------------------------------------------------------
print("\n[7] 清空记忆不清历史")
reset_db()
add(PHONE, "要沉淀的", "2026-10-17 10:00")
memory_history.distill(PHONE, "sk-test", now=NOW, summarize=stub())
before = len(memories.load_history(PHONE))
memories.clear_memory(PHONE)
after = len(memories.load_history(PHONE))
check("history 仍在", before == 1 and after == 1, (before, after))

# ---------------------------------------------------------------------------
shutil.rmtree(TMP_DIR, ignore_errors=True)
print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
raise SystemExit(0 if FAIL == 0 else 1)
