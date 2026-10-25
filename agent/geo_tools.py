"""位置 / 交通 / 地点 三个工具。

它们都依赖"当前用户在哪"，而这个信息只能由浏览器带上来（服务端不知道用户
在哪）。所以每个工具都是按请求绑定的闭包：把请求里的 location 作为默认值，
模型只负责说"要去哪"，不需要重复传坐标。

location 字典的形状（前端或 IP 兜底提供）：
    {"lat": 22.27, "lng": 113.57, "accuracy": 35, "source": "browser"}
    {"ip": "1.2.3.4"}          # 浏览器拒绝授权时，用客户端 IP 兜底

坐标约定：外面（浏览器）给 WGS-84，高德用 GCJ-02，resolve_current() 里统一转。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from langchain_core.tools import tool

from agent import amap
from server.logging_config import get_logger

logger = get_logger("geo")

# 用户说"现在"的几种说法；命中这些就不加"当前路况仅供参考"的提示
_NOW_WORDS = ("", "现在", "当前", "此刻", "马上", "立即", "现在出发", "今天现在")


def _coord(lng: float, lat: float) -> str:
    return f"{lng:.6f},{lat:.6f}"


def resolve_current(location: dict | None) -> dict:
    """把请求里的定位信息变成高德可用的坐标 + 城市。

    返回：{"ok", "precise", "source", "lng", "lat", "city", "address", "reason"}
    """
    location = location or {}
    lat, lng = location.get("lat"), location.get("lng")

    if lat is not None and lng is not None:
        try:
            lat, lng = float(lat), float(lng)
        except (TypeError, ValueError):
            lat = lng = None
        else:
            # 浏览器给的默认是 WGS-84；前端若声明 gcj02 就原样用
            if str(location.get("source") or "").lower() != "gcj02":
                lng, lat = amap.wgs84_to_gcj02(lat, lng)
            info = amap.regeo(lng, lat)
            return {"ok": True, "precise": True, "source": "浏览器定位",
                    "lng": lng, "lat": lat,
                    "city": info.get("city") or "",
                    "address": info.get("address") or "", "reason": ""}

    # ---- 兜底：IP 定位（城市级）----
    try:
        info = amap.ip_location(location.get("ip") or "")
    except amap.AmapNotConfigured:
        raise                      # 没配 Key 让上层给出配置提示，不要伪装成"定位失败"
    except amap.AmapError as e:
        return {"ok": False, "precise": False, "source": "", "lng": None, "lat": None,
                "city": "", "address": "", "reason": f"定位失败：{e}"}

    city = info.get("city") or ""
    lng, lat = info.get("lng"), info.get("lat")

    # 高德 IP 定位在 IPv6 / 内网 IP 下常常只给城市、不给 rectangle。
    # 这时不能直接判失败 —— 拿城市名反查一次坐标，仍然可用（城市级）。
    if lng is None and city:
        try:
            lng, lat = amap.geocode(city)
            logger.info("IP 定位没给坐标，用城市反查：%s -> %s,%s", city, lng, lat)
        except amap.AmapError as e:
            logger.warning("城市反查坐标失败：%s (%s)", city, e)

    if lng is None:
        return {"ok": False, "precise": False, "source": "", "lng": None, "lat": None,
                "city": city, "address": "",
                "reason": (f"定位失败：浏览器没给坐标，IP 也只识别到「{city or '未知地区'}」，"
                           f"拿不到可用坐标。")}

    detail = amap.regeo(lng, lat)
    return {"ok": True, "precise": False, "source": "网络定位（城市级）",
            "lng": lng, "lat": lat,
            "city": city or detail.get("city") or "",
            "address": detail.get("address") or "", "reason": ""}


def make_get_location(location: dict | None):
    """获取当前位置。天气、路线、找地点之前都可以先调它。"""

    @tool
    def get_current_location() -> str:
        """获取用户当前所在的位置（坐标 + 城市 + 地址）。

        在需要"用户在哪"的时候先调它，例如：
        - 查天气但用户没说城市
        - 规划路线但用户没说从哪出发
        - 找附近的地点（加油站、餐厅）

        返回里带高德坐标（经度,纬度），可以直接传给 plan_traffic 或天气工具。
        """
        try:
            info = resolve_current(location)
        except amap.AmapNotConfigured as e:
            return str(e)
        except amap.AmapError as e:
            return f"定位失败：{e}"

        if not info["ok"]:
            return f"定位失败：{info['reason']}请直接告诉我你在哪个城市，或允许浏览器定位。"

        coord = _coord(info["lng"], info["lat"])
        where = info["address"] or info["city"] or "未知位置"
        if info["precise"]:
            return (f"当前位置：{where}（坐标 {coord}，来源：{info['source']}，"
                    f"精确到街道）。可以把这对坐标直接传给天气或路线工具。")
        return (f"当前位置：{info['city'] or where}（坐标 {coord}，来源：{info['source']}，"
                f"精度约 5 公里，只能当城市用）。如果要精确到街道，请让用户允许浏览器定位。")

    return get_current_location


def make_plan_traffic(location: dict | None, brief: dict | None = None):
    """路线 + 实时路况。没说起点时默认用当前位置。"""

    @tool
    def plan_traffic(destination: str, origin: str = "", when: str = "",
                     arrive_by: str = "") -> str:
        """查询从起点到目的地的驾车耗时和实时路况。

        destination: 目的地，地址或地点名都可以，例如 "宝安机场"、"珠海站"。
        origin: 起点。留空表示用用户当前位置。
        when: 用户说的时间，例如 "明天下午3点"。留空表示"现在"。
        arrive_by: 需要在某个时刻"到达"时传它，格式 'YYYY-MM-DD HH:MM'。
            工具会反推建议出发时间（当前耗时 + 15 分钟缓冲）。

        ⚠️ 高德只能查到**当前时刻**的路况，没有未来预测。所以 when / arrive_by
        填了未来时间时，返回的仍然是现在的情况，必须原样把"仅供参考"的提醒
        转告用户，不要假装是那时的路况。
        """
        destination = (destination or "").strip()
        if not destination:
            return "请告诉我要去哪里。"

        origin = (origin or "").strip()
        if not origin:
            try:
                info = resolve_current(location)
            except amap.AmapNotConfigured as e:
                return str(e)
            if not info["ok"]:
                return f"不知道你从哪出发：{info['reason']}请补充起点。"
            origin = _coord(info["lng"], info["lat"])
            origin_label = f"当前位置（{info['city'] or '未知'}）"
        else:
            origin_label = origin

        try:
            result = amap.drive_summary(origin, destination)
        except amap.AmapNotConfigured as e:
            return str(e)
        except amap.AmapError as e:
            return f"查路况失败：{e}"

        arrive_dt = _parse_dt(arrive_by) if arrive_by else None
        lines = [result["text"]]

        leave_at = None
        if arrive_dt:
            leave_at = arrive_dt - timedelta(minutes=result["duration_min"] + 15)
            lines.append(
                f"要 {arrive_dt.strftime('%H:%M')} 前到达，建议 {leave_at.strftime('%H:%M')} "
                f"前出发（按当前耗时 {result['duration_min']} 分钟 + 15 分钟缓冲）。")

        # 给前端左下角小字用的结构化数据（模型不需要在正文里复述这些字段）
        if brief is not None:
            brief["traffic"] = {
                "destination": destination,
                "distance_km": result["distance_km"],
                "duration_min": result["duration_min"],
                "leave_by": leave_at.strftime("%H:%M") if leave_at else "",
                "cached": result.get("cached", False),
            }

        future = bool(arrive_dt) or ((when or "").strip() not in _NOW_WORDS)
        if future:
            label = arrive_dt.strftime("%m-%d %H:%M") if arrive_dt else when.strip()
            lines.insert(0, f"⚠️ 注意：下面是**当前时刻**（{_coord_clock()}）的路况，"
                            f"不能代表「{label}」的实际拥堵情况，仅供参考。")

        lines.append(f"（起点：{origin_label}；缓存 5 分钟内有效）")
        return "\n".join(lines)

    return plan_traffic


def _parse_dt(text: str):
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime((text or "").strip(), fmt)
        except ValueError:
            continue
    return None


def _coord_clock() -> str:
    return datetime.now().strftime("%H:%M")


def make_search_place(location: dict | None):
    """模糊地点搜索：加油站、麦当劳这种。"""

    @tool
    def search_place(keyword: str, city: str = "") -> str:
        """按关键词搜索地点（POI），适合"附近的加油站""最近的麦当劳"这类需求。

        keyword: 搜索词，例如 "加油站"、"麦当劳"、"人民医院"。
        city: 限定城市，例如 "珠海"。留空则用用户当前所在城市。

        返回结果里每个地点都带名称、地址、距离和坐标；要查路线就把地点名
        交给 plan_traffic。
        """
        keyword = (keyword or "").strip()
        if not keyword:
            return "请告诉我要找什么地点。"

        coord, resolved_city = "", (city or "").strip()
        try:
            info = resolve_current(location)
            if info["ok"]:
                coord = _coord(info["lng"], info["lat"])
                resolved_city = resolved_city or info["city"]
        except amap.AmapError:
            pass          # 定位失败不影响搜索，只是没有距离排序

        try:
            pois = amap.poi_search(keyword, city=resolved_city, location=coord)
        except amap.AmapNotConfigured as e:
            return str(e)
        except amap.AmapError as e:
            return f"搜索地点失败：{e}"

        if not pois:
            return f"没搜到「{keyword}」" + (f"（{resolved_city}）" if resolved_city else "") + "，换个说法试试。"

        lines = [f"找到 {len(pois)} 个「{keyword}」"
                 + (f"（{resolved_city}）" if resolved_city else "") + "，按距离排序："]
        for i, poi in enumerate(pois, 1):
            distance = f"，距你约 {poi['distance_m']} 米" if poi.get("distance_m") else ""
            lines.append(f"{i}. {poi['name']}（{poi['address'] or '地址未标注'}{distance}）"
                         f" 坐标 {poi['location']}")
        lines.append("需要路线的话，把地点名传给 plan_traffic。")
        return "\n".join(lines)

    return search_place
