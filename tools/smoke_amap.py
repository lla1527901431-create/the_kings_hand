"""高德客户端的离线冒烟（不联网、不碰真实数据库）。

覆盖：
    1. 没配 Key 时的报错
    2. 地理编码解析（"经度,纬度" 顺序）
    3. 查不到地址时的中文提示
    4. 已经是坐标时不再调 geocode
    5. 驾车结果解析（耗时/距离/分段路况）
    6. 摘要文本与字段
    7. 缓存：同一对起终点只请求一次
    8. 高德错误码翻译（10009 平台不匹配）

用法：
    .venv\\Scripts\\python.exe tools/smoke_amap.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TMP_DIR = tempfile.mkdtemp(prefix="kh_amap_")
os.environ["DB_PATH"] = os.path.join(TMP_DIR, "amap.db")
os.environ["DEBUG"] = "false"
os.environ["LOG_LEVEL"] = "ERROR"

from agent import amap                       # noqa: E402
from db import settings                      # noqa: E402

PASS, FAIL = 0, 0


def check(label, ok, extra=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {label}{('  -> ' + str(extra)) if extra else ''}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}{('  -> ' + str(extra)) if extra else ''}")


GEOCODE = {
    "status": "1", "info": "OK", "infocode": "10000",
    "geocodes": [{"location": "113.576123,22.270456"}],
}
GEOCODE_EMPTY = {"status": "1", "info": "OK", "infocode": "10000", "geocodes": []}
DRIVING = {
    "status": "1", "info": "OK", "infocode": "10000",
    "route": {"paths": [{
        "distance": "12400", "duration": "1680",
        "steps": [
            {"tmcs": [{"status": "2", "distance": "300"}, {"status": "1", "distance": "500"}]},
            {"tmcs": [{"status": "3", "distance": "800"}, {"status": "4", "distance": "200"}]},
        ],
    }]},
}
BAD_KEY = {"status": "0", "info": "USERKEY_PLAT_NOMATCH", "infocode": "10009"}

calls: list[str] = []


def fake_get(path: str, params: dict) -> dict:
    calls.append(path)
    if path.endswith("/geocode/geo"):
        if params.get("address") == "不存在的地方":
            return GEOCODE_EMPTY
        return GEOCODE
    if path.endswith("/direction/driving"):
        return DRIVING
    raise AssertionError(f"没预料到的请求：{path}")


print("\n[1] 没配 Key 时的报错")
try:
    amap.geocode("珠海")
    check("未配置时应抛异常", False)
except amap.AmapNotConfigured as e:
    check("未配置时抛 AmapNotConfigured", "高德 Key" in str(e), str(e))

settings.set_value(settings.AMAP_API_KEY, "a" * 32, updated_by="test")
amap._get_json = fake_get
check("status 里显示已配置", settings.amap_status()["configured"] is True)
check("打码后不泄露全文", settings.amap_status()["masked"].count("*") >= 8,
      settings.amap_status()["masked"])

print("\n[2] 地理编码")
lng, lat = amap.geocode("珠海市香洲区情侣路", city="珠海")
check("经度解析正确", lng == 113.576123, lng)
check("纬度解析正确", lat == 22.270456, lat)

print("\n[3] 查不到地址")
try:
    amap.geocode("不存在的地方")
    check("应抛 AmapError", False)
except amap.AmapError as e:
    check("提示里带地点名", "不存在的地方" in str(e), str(e))

print("\n[4] 已经是坐标时不再调 geocode")
calls.clear()
loc = amap._to_location("113.576,22.270")
check("坐标原样保留并规整", loc == "113.576,22.27", loc)
check("没有触发 geocode 请求", len(calls) == 0, calls)
check("is_location 判断正确", amap.is_location("113.5,22.2") and not amap.is_location("珠海"))

print("\n[5] 驾车解析")
calls.clear()
raw = amap.driving("113.576,22.270", "113.550,22.280")
check("耗时秒数", raw["duration_s"] == 1680, raw["duration_s"])
check("距离米数", raw["distance_m"] == 12400, raw["distance_m"])
check("只发了一次请求", len(calls) == 1, calls)

condition = amap._summarize_steps(raw["steps"])
check("缓行+拥堵总里程 1.3 公里", condition["slow_km"] == 1.3, condition["slow_km"])
check("拥堵里程 1.0 公里", condition["jam_km"] == 1.0, condition["jam_km"])
check("分段明细可读", "拥堵" in condition["detail"] and "缓行" in condition["detail"],
      condition["detail"])

print("\n[6] 摘要与缓存")
calls.clear()
amap.clear_cache()
first = amap.drive_summary("家", "宝安机场")
check("第一次不是缓存", first["cached"] is False)
check("换算成分钟", first["duration_min"] == 28, first["duration_min"])
check("距离换算成公里", first["distance_km"] == 12.4, first["distance_km"])
check("文本包含起终点与耗时",
      "家" in first["text"] and "宝安机场" in first["text"] and "28 分钟" in first["text"],
      first["text"])

before = len(calls)
second = amap.drive_summary("家", "宝安机场")
check("第二次命中缓存", second["cached"] is True)
check("缓存没有再发请求", len(calls) == before, (before, len(calls)))

print("\n[7] 错误码翻译")
msg = amap._friendly(BAD_KEY)
check("10009 翻译成「服务平台不匹配」", "Web服务" in msg, msg)
msg2 = amap._friendly({"status": "0", "info": "DAILY_QUERY_OVER_LIMIT", "infocode": "10003"})
check("10003 翻译成配额用完", "配额" in msg2, msg2)

# ---------------------------------------------------------------------------
shutil.rmtree(TMP_DIR, ignore_errors=True)
print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
raise SystemExit(0 if FAIL == 0 else 1)
