"""位置 / 交通 / 地点 三个工具的离线冒烟（不联网、不碰真实数据库）。

覆盖：
    1. 没有高德 Key 时，三个工具都给出可读的提示
    2. 浏览器坐标 → WGS-84 转 GCJ-02 → 逆地理编码 → 位置文本
    3. 没有浏览器坐标时，用客户端 IP 兜底（城市级）
    4. plan_traffic 默认起点 = 当前位置；传了 when 会加"仅供参考"
    5. search_place 带城市与距离；空结果给出可读提示
    6. 天气工具能用坐标，不再走城市地理编码

用法：
    .venv\\Scripts\\python.exe tools/smoke_geo.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TMP_DIR = tempfile.mkdtemp(prefix="kh_geo_")
os.environ["DB_PATH"] = os.path.join(TMP_DIR, "geo.db")
os.environ["DEBUG"] = "false"
os.environ["LOG_LEVEL"] = "ERROR"

from agent import amap, geo_tools              # noqa: E402
from db import settings                        # noqa: E402

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


R = {"status": "1", "info": "OK", "infocode": "10000"}
RE_GEO = {**R, "regeocode": {"formatted_address": "广东省珠海市香洲区情侣路",
                             "addressComponent": {"city": "珠海市", "district": "香洲区",
                                                  "adcode": "440402"}}}
IP = {**R, "city": "珠海市", "adcode": "440400", "rectangle": "113.45,22.15;113.65,22.35"}
POI = {**R, "pois": [
    {"name": "中国石化加油站(情侣路站)", "address": "香洲区情侣中路 12 号",
     "location": "113.580,22.275", "distance": "320", "type": "汽车服务"},
    {"name": "中石油加油站", "address": "香洲区海滨南路", "location": "113.590,22.280",
     "distance": "980", "type": "汽车服务"},
    {"name": "壳牌加油站", "address": "香洲区吉大路", "location": "113.600,22.290",
     "distance": "2100", "type": "汽车服务"},
]}
POI_EMPTY = {**R, "pois": []}
DRIVING = {**R, "route": {"paths": [{
    "distance": "12400", "duration": "1680",
    "steps": [{"tmcs": [{"status": "2", "distance": "500"}, {"status": "3", "distance": "700"}]}],
}]}}

calls: list[str] = []


def fake_get(path: str, params: dict) -> dict:
    calls.append(path)
    if path.endswith("/regeo"):
        return RE_GEO
    if path.endswith("/ip"):
        return IP
    if path.endswith("/geocode/geo"):
        return {**R, "geocodes": [{"location": "113.810,22.640"}]}
    if path.endswith("/place/text"):
        return POI_EMPTY if params.get("keywords") == "不存在的地方" else POI
    if path.endswith("/direction/driving"):
        return DRIVING
    raise AssertionError(f"没预料到的请求：{path} {params}")


BROWSER = {"lat": 22.27, "lng": 113.57, "accuracy": 30, "source": "browser",
           "ip": "1.2.3.4"}

print("\n[1] 没配高德 Key")
text = geo_tools.make_get_location(BROWSER).invoke({})
check("未配置 Key 时给出可读提示（不抛异常）", "高德 Key" in text, text)

settings.set_value(settings.AMAP_API_KEY, "a" * 32, updated_by="test")
amap._get_json = fake_get

print("\n[2] 浏览器定位 → 逆地理")
text = geo_tools.make_get_location(BROWSER).invoke({})
check("包含地址", "香洲区情侣路" in text, text)
check("包含坐标", "113.5" in text and "22.2" in text, text)
check("标注来源为浏览器定位", "浏览器定位" in text, text)

converted = amap.wgs84_to_gcj02(22.27, 113.57)
check("WGS-84 转 GCJ-02 有偏移但不大",
      0.0001 < abs(converted[0] - 113.57) < 0.01, converted)

print("\n[3] 只有 IP（浏览器拒绝授权）")
text = geo_tools.make_get_location({"ip": "1.2.3.4"}).invoke({})
check("标注网络定位", "网络定位" in text, text)
check("标注城市级精度", "城市级" in text or "城市" in text, text)
check("包含城市名", "珠海" in text, text)

print("\n[4] 路线：默认起点 + 未来时间提醒")
text = geo_tools.make_plan_traffic(BROWSER).invoke({"destination": "宝安机场"})
check("包含耗时", "28 分钟" in text, text)
check("包含拥堵信息", "缓行" in text or "拥堵" in text, text)
check("起点标注为当前位置", "当前位置" in text, text)
check("没传 when 时不带提醒", "仅供参考" not in text, text)

text2 = geo_tools.make_plan_traffic(BROWSER).invoke(
    {"destination": "宝安机场", "when": "明天下午三点"})
check("未来时间带「当前时刻」提醒", "当前时刻" in text2 and "仅供参考" in text2, text2)

print("\n[5] 地点搜索")
text = geo_tools.make_search_place(BROWSER).invoke({"keyword": "加油站"})
check("列出地点", "中国石化" in text, text)
check("带距离", "320" in text, text)
check("带坐标", "113.58" in text, text)
text = geo_tools.make_search_place(BROWSER).invoke({"keyword": "不存在的地方"})
check("空结果有可读提示", "没搜到" in text, text)

print("\n[6] 天气工具接受坐标")
from agent import weather_openmeteo          # noqa: E402
import agent.weather_openmeteo as w          # noqa: E402

geo_calls = []
w._geocode = lambda city: (geo_calls.append(city) or (22.27, 113.57, city))
w.requests.get = lambda *a, **k: type("R", (), {
    "json": lambda self: {"current": {"weather_code": 1, "temperature_2m": 26,
                                      "relative_humidity_2m": 60, "wind_speed_10m": 5},
                          "daily": {"time": [], "temperature_2m_max": [],
                                    "temperature_2m_min": [], "precipitation_probability_max": [],
                                    "weather_code": []}}})()
out = weather_openmeteo.get_weather_forecast.invoke(
    {"latitude": 22.27, "longitude": 113.57, "city": "珠海"})
check("用坐标时不再做城市地理编码", len(geo_calls) == 0, geo_calls)
check("天气结果正常返回", "当前天气" in out, out[:60])

# ---------------------------------------------------------------------------
shutil.rmtree(TMP_DIR, ignore_errors=True)
print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
raise SystemExit(0 if FAIL == 0 else 1)
