"""高德 Web 服务客户端：地址解析、逆地理、IP 定位、POI、驾车路况。

本项目需要的几件事：

    geocode("珠海市香洲区情侣路")   -> (113.576, 22.270)
    regeo(113.576, 22.270)          -> {"city": "珠海市", "address": "..."}
    ip_location("1.2.3.4")          -> {"city": "珠海市", "location": (lng, lat)}
    poi_search("加油站", "珠海市")   -> [{name, address, location, distance}]
    drive_summary("家", "宝安机场")  -> {duration_min, distance_km, ...}

为什么不用高德的「交通态势」接口：它属于高级服务，要商务申请，而且只覆盖
部分城市、返回的是区域快照。路径规划是基础服务，申请即用，还能直接回答
"从 A 到 B 现在要多久、哪一段堵"。

坐标系：高德全站用 GCJ-02；浏览器 Geolocation 给的是 WGS-84，差几百米，
所以这里提供 wgs84_to_gcj02()（纯本地计算，不额外消耗配额）。

Key 由管理员在「访问管理」里配置，存在 app_settings；没配时抛
AmapNotConfigured，由上层决定怎么提示用户。
"""

from __future__ import annotations

import math
import re
import time

import requests

from db import settings
from server.logging_config import get_logger

logger = get_logger("amap")

BASE = "https://restapi.amap.com"
TIMEOUT = 6
CACHE_TTL = 300          # 同一对起终点 5 分钟内复用，省配额

_LOCATION_RE = re.compile(r"^-?\d{1,3}(\.\d+)?,-?\d{1,2}(\.\d+)?$")
_TMC_NAMES = {0: "未知", 1: "畅通", 2: "缓行", 3: "拥堵", 4: "严重拥堵"}

_cache: dict[tuple[str, str], tuple[float, dict]] = {}


class AmapError(RuntimeError):
    """高德调用失败，message 已经是可以直接给用户看的中文。"""


class AmapNotConfigured(AmapError):
    """管理员还没配高德 Key。"""


def _key() -> str:
    key = settings.amap_key()
    if not key:
        raise AmapNotConfigured("还没有配置高德 Key。请联系管理员在「访问管理」里填写。")
    return key


def _friendly(data: dict) -> str:
    """把高德的 infocode 翻成人话。"""
    code = str(data.get("infocode") or "")
    info = str(data.get("info") or "")
    mapping = {
        "10001": "高德 Key 无效（INVALID_USER_KEY），请管理员重新填写。",
        "10003": "高德接口今日配额已用完，请明天再试。",
        "10004": "高德接口调用过于频繁，请稍后再试。",
        "10009": "高德 Key 的服务平台不匹配：必须创建「Web服务」类型的 Key。",
        "10010": "高德接口 IP 校验失败。",
        "10012": "高德接口今日配额已用完。",
    }
    return mapping.get(code, f"高德接口返回错误：{info or '未知'}（infocode={code or '-'}）")


def _get_json(path: str, params: dict) -> dict:
    """唯一的一层 HTTP 封装；测试里直接替换它就能离线验证解析逻辑。"""
    payload = {**params, "key": _key(), "output": "JSON"}
    try:
        resp = requests.get(f"{BASE}{path}", params=payload, timeout=TIMEOUT)
        data = resp.json()
    except requests.RequestException as e:
        raise AmapError(f"连不上高德接口：{type(e).__name__}") from e
    except ValueError as e:
        raise AmapError("高德接口返回的不是 JSON") from e

    if str(data.get("status")) != "1":
        raise AmapError(_friendly(data))
    return data


def is_location(text: str) -> bool:
    """判断是不是 "lng,lat" 形式的坐标（高德的经纬度顺序是经度在前）。"""
    return bool(_LOCATION_RE.match((text or "").strip()))


def geocode(address: str, city: str = "") -> tuple[float, float]:
    """地址 → (经度, 纬度)。

    高德要求经纬度顺序是 "经度,纬度"，小数不超过 6 位，这里统一四舍五入。
    """
    address = (address or "").strip()
    if not address:
        raise AmapError("地址是空的。")

    params = {"address": address}
    if city:
        params["city"] = city
    data = _get_json("/v3/geocode/geo", params)

    geocodes = data.get("geocodes") or []
    if not geocodes:
        raise AmapError(f"高德没找到这个地点：{address}（试试加上城市或更完整的地址）")

    location = str(geocodes[0].get("location") or "")
    if "," not in location:
        raise AmapError(f"高德返回的坐标格式不对：{location!r}")
    lng, lat = (round(float(x), 6) for x in location.split(",", 1))
    return lng, lat


def _to_location(value: str, city: str = "") -> str:
    """输入可以是地址，也可以直接是坐标。"""
    text = (value or "").strip()
    if not text:
        raise AmapError("起点或目的地是空的。")
    if is_location(text):
        lng, lat = (round(float(x), 6) for x in text.split(",", 1))
        return f"{lng},{lat}"
    lng, lat = geocode(text, city=city)
    return f"{lng},{lat}"


def driving(origin: str, destination: str, city: str = "") -> dict:
    """驾车路径规划（含实时路况）。返回原始 route.paths[0] 的瘦身版。"""
    data = _get_json("/v3/direction/driving", {
        "origin": _to_location(origin, city),
        "destination": _to_location(destination, city),
        "strategy": 0,            # 速度优先
        "extensions": "all",      # 必须 all，否则没有 tmcs 分段路况
        "city": city,
    })

    paths = ((data.get("route") or {}).get("paths") or [])
    if not paths:
        raise AmapError(f"高德没有规划出路线：{origin} → {destination}")

    path = paths[0]
    return {
        "distance_m": int(path.get("distance") or 0),
        "duration_s": int(path.get("duration") or 0),
        "steps": path.get("steps") or [],
    }


def _summarize_steps(steps: list[dict]) -> dict:
    """统计分段路况：每个 tmcs 项是 {status, distance}。"""
    counts = {0: 0, 1: 0, 2: 0, 3: 0, 4: 0}
    meters = {0: 0, 1: 0, 2: 0, 3: 0, 4: 0}
    for step in steps:
        for tmc in step.get("tmcs") or []:
            try:
                status = int(tmc.get("status", 1))
                distance = int(tmc.get("distance") or 0)
            except (TypeError, ValueError):
                continue
            if status not in counts:
                status = 0
            counts[status] += 1
            meters[status] += distance
    slow_km = (meters[2] + meters[3] + meters[4]) / 1000
    jam_km = (meters[3] + meters[4]) / 1000
    parts = []
    for status in (3, 4, 2):
        if meters[status]:
            parts.append(f"{_TMC_NAMES[status]} {meters[status] / 1000:.1f} 公里")
    return {"counts": counts, "meters": meters,
            "slow_km": round(slow_km, 1), "jam_km": round(jam_km, 1),
            "detail": "、".join(parts) or "全程畅通"}


def drive_summary(origin: str, destination: str, city: str = "") -> dict:
    """给模型/用户看的一句话路况摘要。同一对起终点 5 分钟内走缓存。"""
    cache_key = (origin.strip(), destination.strip())
    now = time.time()
    cached = _cache.get(cache_key)
    if cached and now - cached[0] < CACHE_TTL:
        return {**cached[1], "cached": True}

    raw = driving(origin, destination, city=city)
    condition = _summarize_steps(raw["steps"])
    minutes = max(1, round(raw["duration_s"] / 60))
    km = raw["distance_m"] / 1000

    text = (f"从「{origin}」到「{destination}」现在开车约 {minutes} 分钟"
            f"（{km:.1f} 公里），{condition['detail']}。")

    result = {
        "origin": origin,
        "destination": destination,
        "duration_min": minutes,
        "distance_km": round(km, 1),
        "slow_km": condition["slow_km"],
        "jam_km": condition["jam_km"],
        "detail": condition["detail"],
        "text": text,
        "cached": False,
    }
    _cache[cache_key] = (now, result)
    logger.info("高德路况：%s → %s，%s 分钟，%s", origin, destination, minutes, condition["detail"])
    return result


def clear_cache() -> None:
    """测试或手动刷新用。"""
    _cache.clear()


# ---------------------------------------------------------------------------
# 坐标系转换（WGS-84 → GCJ-02）
# ---------------------------------------------------------------------------
# 浏览器 Geolocation 给的是 GPS 原始坐标（WGS-84），高德用的是国测局加密坐标
# （GCJ-02），两者在国内相差几百米。这里用公开的偏移算法本地换算，
# 不额外调接口、不占配额；境外坐标原样返回。
_A = 6378245.0
_EE = 0.00669342162296594323


def _out_of_china(lng: float, lat: float) -> bool:
    return not (73.66 < lng < 135.05 and 3.86 < lat < 53.55)


def _transform_lat(x: float, y: float) -> float:
    ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * math.pi) + 320 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lng(x: float, y: float) -> float:
    ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return ret


def wgs84_to_gcj02(lat: float, lng: float) -> tuple[float, float]:
    """WGS-84 → GCJ-02。返回 (经度, 纬度)，和高德一致。"""
    lat, lng = float(lat), float(lng)
    if _out_of_china(lng, lat):
        return round(lng, 6), round(lat, 6)

    dlat = _transform_lat(lng - 105.0, lat - 35.0)
    dlng = _transform_lng(lng - 105.0, lat - 35.0)
    radlat = lat / 180.0 * math.pi
    magic = math.sin(radlat)
    magic = 1 - _EE * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_A * (1 - _EE)) / (magic * sqrtmagic) * math.pi)
    dlng = (dlng * 180.0) / (_A / sqrtmagic * math.cos(radlat) * math.pi)
    return round(lng + dlng, 6), round(lat + dlat, 6)


# ---------------------------------------------------------------------------
# 逆地理编码 / IP 定位 / POI 搜索
# ---------------------------------------------------------------------------
def regeo(lng: float, lat: float) -> dict:
    """坐标 → 地址信息。失败时返回空字段（不抛异常，定位场景可降级）。"""
    try:
        data = _get_json("/v3/regeo", {"location": f"{lng:.6f},{lat:.6f}",
                                       "extensions": "base"})
    except AmapNotConfigured:
        raise                      # 没配 Key 是配置问题，不能被"降级"吞掉
    except AmapError as e:
        logger.warning("逆地理编码失败：%s", e)
        return {"address": "", "city": "", "district": "", "adcode": ""}

    regeocode = data.get("regeocode") or {}
    comp = regeocode.get("addressComponent") or {}
    city = comp.get("city") or comp.get("province") or ""
    if isinstance(city, list):          # 高德直辖市有时给空数组
        city = comp.get("province") or ""
    return {
        "address": regeocode.get("formatted_address") or "",
        "city": city or "",
        "district": comp.get("district") or "",
        "adcode": comp.get("adcode") or "",
    }


def ip_location(ip: str = "") -> dict:
    """按 IP 定位到城市。不传 ip 时高德用请求方 IP（即服务器），所以一定要传。"""
    params = {}
    if ip:
        params["ip"] = ip
    data = _get_json("/v3/ip", params)

    city = data.get("city") or data.get("province") or ""
    if isinstance(city, list):
        city = data.get("province") or ""
    rectangle = str(data.get("rectangle") or "")
    lng = lat = None
    if ";" in rectangle:
        left, right = rectangle.split(";", 1)
        try:
            lng = round((float(left.split(",")[0]) + float(right.split(",")[0])) / 2, 6)
            lat = round((float(left.split(",")[1]) + float(right.split(",")[1])) / 2, 6)
        except (ValueError, IndexError):
            lng = lat = None
    return {"city": city, "adcode": str(data.get("adcode") or ""),
            "lng": lng, "lat": lat}


def poi_search(keyword: str, city: str = "", location: str = "",
               limit: int = 5) -> list[dict]:
    """关键词搜地点（加油站、麦当劳这类模糊需求）。

    location 传 "lng,lat" 时，返回结果会带距离，并按距离排序。
    """
    keyword = (keyword or "").strip()
    if not keyword:
        raise AmapError("搜索关键词是空的。")

    params = {"keywords": keyword, "offset": max(1, min(limit, 10)), "page": 1,
              "extensions": "base"}
    if city:
        params["city"] = city
        params["citylimit"] = "true"      # 有 city 时默认限制在该城市
    if location:
        params["location"] = location

    data = _get_json("/v3/place/text", params)
    out = []
    for poi in (data.get("pois") or [])[:limit]:
        loc = str(poi.get("location") or "")
        out.append({
            "name": poi.get("name") or "",
            "address": poi.get("address") or "",
            "location": loc,
            "distance_m": int(poi.get("distance") or 0) if str(poi.get("distance") or "").isdigit() else None,
            "type": poi.get("type") or "",
        })
    return out
