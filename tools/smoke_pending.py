"""待办修改 + 建议出发时间的离线冒烟（不联网）。

覆盖：
    1. update_pending_task 能改标题/备注/时间，并返回可读结果
    2. 待办被确认（从 pending 里删掉）之后，工具拒绝修改
    3. 不存在的 id 给出"已确认/已删除"的提示
    4. plan_traffic 的 arrive_by 能反推建议出发时间（含 15 分钟缓冲）
    5. 待办文本里会带上备注与地点，模型才知道要不要改

用法：
    .venv\\Scripts\\python.exe tools/smoke_pending.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TMP_DIR = tempfile.mkdtemp(prefix="kh_pending_")
os.environ["DB_PATH"] = os.path.join(TMP_DIR, "pending.db")
os.environ["DEBUG"] = "false"
os.environ["LOG_LEVEL"] = "ERROR"

from agent import amap, geo_tools, pending_tools, runner   # noqa: E402
from db import database, pendings, settings                 # noqa: E402

PASS, FAIL = 0, 0
PHONE = "13800000001"


def check(label, ok, extra=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {label}{('  -> ' + str(extra)) if extra else ''}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}{('  -> ' + str(extra)) if extra else ''}")


database.init_db()
settings.set_value(settings.AMAP_API_KEY, "a" * 32, updated_by="test")


def add_task(**kw):
    task = {"id": kw.get("id", "task-1"), "title": kw.get("title", "珠海清华科技园开会"),
            "start": kw.get("start", "2026-10-25 14:00"), "end": kw.get("end"),
            "location": kw.get("location", "清华科技园4栋1楼"),
            "note": kw.get("note", "")}
    pendings.add_pending(PHONE, task)
    return task["id"]


print("\n[1] 修改未确认的待办")
tid = add_task()
tool = pending_tools.make_update_pending(PHONE)
out = tool.invoke({"task_id": tid,
                   "note": "驾车约 28 分钟；建议 13:15 前出发；当天多云 22~28°C。",
                   "location": "珠海市清华科技园4栋1楼众创空间会议室"})
check("返回更新成功", "已更新" in out, out)
saved = pendings.get_pending(PHONE, tid)
check("备注已写入", "建议 13:15" in saved["note"], saved["note"])
check("地点已更新", "众创空间" in saved["location"], saved["location"])

print("\n[2] 确认之后不能再改")
pendings.pop_pending(PHONE, tid)          # 模拟用户点「确认」：进日历、从待办消失
out = tool.invoke({"task_id": tid, "note": "想偷偷改备注"})
check("拒绝修改", "不能" in out and "确认" in out, out)
check("没有偷偷写回", pendings.get_pending(PHONE, tid) is None)

print("\n[3] 不存在的 id")
out = tool.invoke({"task_id": "task-does-not-exist", "title": "x"})
check("提示不存在", "没有找到" in out, out)

print("\n[4] 待办文本带备注与地点")
tid2 = add_task(id="task-2", note="带伞", location="3 楼")
text = runner._format_pending({"confirmed": [pendings.get_pending(PHONE, tid2)], "planned": []})
check("带地点", "3 楼" in text, text)
check("带备注", "带伞" in text, text)
check("带 id", "task-2" in text, text)

print("\n[5] arrive_by 反推建议出发时间")


def fake_get(path, params):
    if path.endswith("/geocode/geo"):
        return {"status": "1", "geocodes": [{"location": "113.810,22.640"}]}
    if path.endswith("/regeo"):
        return {"status": "1", "regeocode": {"formatted_address": "珠海市香洲区",
                "addressComponent": {"city": "珠海市"}}}
    if path.endswith("/direction/driving"):
        return {"status": "1", "route": {"paths": [{"distance": "12400", "duration": "1680",
                "steps": [{"tmcs": [{"status": "1", "distance": "12400"}]}]}]}}
    raise AssertionError(path)


amap._get_json = fake_get
amap.clear_cache()
out = geo_tools.make_plan_traffic({"lat": 22.27, "lng": 113.57}).invoke(
    {"destination": "珠海清华科技园", "arrive_by": "2026-10-25 14:00"})
check("给出建议出发时间", "建议 13:17 前出发" in out, out)
check("说明缓冲", "15 分钟缓冲" in out, out)
check("未来时间带提醒", "仅供参考" in out, out)

# ---------------------------------------------------------------------------
shutil.rmtree(TMP_DIR, ignore_errors=True)
print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
raise SystemExit(0 if FAIL == 0 else 1)
