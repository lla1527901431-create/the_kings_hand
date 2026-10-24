import logging

import requests
from langchain_core.tools import tool

logger = logging.getLogger("agent.weather")

# WMO 天气代码 -> 中文描述
WEATHER_CODE_MAP = {
    0: "晴",
    1: "晴间多云",
    2: "多云",
    3: "阴",
    45: "雾",
    48: "雾凇",
    51: "小毛毛雨",
    53: "中毛毛雨",
    55: "大毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    80: "小阵雨",
    81: "中阵雨",
    82: "大阵雨",
    95: "雷暴",
    96: "雷暴伴小冰雹",
    99: "雷暴伴大冰雹",
}


def _geocode(city: str) -> tuple[float, float, str] | None:
    """把城市名转成经纬度。返回 (纬度, 经度, 解析后的城市名)，失败返回 None。"""
    url = "https://geocoding-api.open-meteo.com/v1/search"
    params = {
        "name": city,
        "count": 1,
        "language": "zh",
        "format": "json",
    }
    try:
        resp = requests.get(url, params=params, timeout=10)
        data = resp.json()
    except Exception as e:
        logger.warning("城市地理编码请求失败（city=%s）：%s", city, type(e).__name__)
        return None

    results = data.get("results") or []
    if not results:
        return None

    r = results[0]
    return r["latitude"], r["longitude"], r.get("name", city)


def _forecast(city: str = "", days: int = 3,
              latitude: float | None = None, longitude: float | None = None,
              date: str = "") -> tuple[str, dict | None]:
    """返回 (给模型看的文本, 给前端小字的天气结构)。

    date 传 'YYYY-MM-DD' 时只取那一天，并且**一定会**产出 info，
    这样前端左下角的小字就不用猜模型指的是哪一天。
    """
    if days < 1:
        days = 1
    if days > 7:
        days = 7

    if latitude is not None and longitude is not None:
        lat, lon = float(latitude), float(longitude)
        resolved_name = (city or "").strip() or "当前定位位置"
    else:
        city = (city or "").strip()
        if not city:
            return "需要城市名或坐标：请先调用 get_current_location，或告诉我城市。", None
        geo = _geocode(city)
        if geo is None:
            return f"找不到城市 '{city}'，请换一个更具体的城市名。", None
        lat, lon, resolved_name = geo

    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,weather_code,wind_speed_10m,relative_humidity_2m",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
        "timezone": "auto",
        "forecast_days": max(days, 1),
    }

    try:
        resp = requests.get(url, params=params, timeout=10)
        data = resp.json()
    except Exception as e:
        return f"调用天气 API 失败：{e}", None

    if "error" in data:
        return f"天气 API 返回错误：{data.get('reason', '未知错误')}", None

    current = data.get("current", {})
    cur_code = current.get("weather_code")
    cur_desc = WEATHER_CODE_MAP.get(cur_code, "未知")
    cur_temp = current.get("temperature_2m")
    cur_hum = current.get("relative_humidity_2m")
    cur_wind = current.get("wind_speed_10m")

    daily = data.get("daily", {})
    dates = daily.get("time", [])
    tmax = daily.get("temperature_2m_max", [])
    tmin = daily.get("temperature_2m_min", [])
    precip = daily.get("precipitation_probability_max", [])
    codes = daily.get("weather_code", [])

    target = (date or "").strip()
    if target:
        if target not in dates:
            return (f"预报里没有 {target} 的数据（只能查未来 {days} 天）。"
                    f"可用日期：{', '.join(dates)}"), None
        idx = dates.index(target)
        desc = WEATHER_CODE_MAP.get(codes[idx] if idx < len(codes) else None, "未知")
        info = {
            "date": f"{target[5:7]}.{target[8:10]}",
            "city": resolved_name,
            "desc": desc,
            "temp": f"{tmin[idx]}~{tmax[idx]}°C",
            "precip": int(precip[idx]) if idx < len(precip) and precip[idx] is not None else None,
        }
        text = (f"{resolved_name} {target}：{desc}，{tmin[idx]}~{tmax[idx]}°C，"
                f"降水概率 {info['precip']}%")
        return text, info

    lines = [
        f"{resolved_name}当前天气：{cur_desc}，气温 {cur_temp}°C，"
        f"湿度 {cur_hum}%，风速 {cur_wind} km/h"
    ]
    for i in range(len(dates)):
        desc = WEATHER_CODE_MAP.get(codes[i] if i < len(codes) else None, "未知")
        lines.append(f"{dates[i]}：{desc}，{tmin[i]}~{tmax[i]}°C，降水概率 {precip[i]}%")

    return "\n".join(lines), None


@tool
def get_weather_forecast(city: str = "", days: int = 3,
                         latitude: float | None = None,
                         longitude: float | None = None,
                         date: str = "") -> str:
    """查询指定位置的天气预报。

    city: 城市名，中英文均可，例如 '深圳'、'Shenzhen'。用户没说城市时留空。
    days: 查询未来几天的预报，默认 3，范围 1-7。
    latitude / longitude: 精确坐标（城市级天气差异可忽略）。
        从 get_current_location 拿到坐标后直接传进来，比城市名更准。
    date: 只查某一天，格式 'YYYY-MM-DD'。排日程要用它查"会议当天"，
        不要用 days 然后自己在多天里挑。

    当用户询问天气、气温、是否下雨、出行建议时使用此工具。
    """
    text, _ = _forecast(city, days, latitude, longitude, date)
    return text


def make_weather_forecast(brief: dict | None):
    """按请求绑定"把结构化天气塞进小字"的工具。

    工具仍然返回文本给模型；同时把当天那条结构化数据写进 brief，
    由 done 事件带给前端，渲染成消息左下角的小字（模型不需要复述这些字段）。
    """

    @tool
    def get_weather_forecast(city: str = "", days: int = 3,
                             latitude: float | None = None,
                             longitude: float | None = None,
                             date: str = "") -> str:
        """查询指定位置的天气预报。

        city: 城市名，中英文均可。用户没说城市时留空。
        days: 查询未来几天，默认 3，范围 1-7。
        latitude / longitude: 精确坐标，从 get_current_location 拿到后直接传。
        date: 只查某一天，格式 'YYYY-MM-DD'。**排日程时用它查会议当天**。

        当用户询问天气、气温、是否下雨、出行建议时使用此工具。
        """
        text, info = _forecast(city, days, latitude, longitude, date)
        if brief is not None and info:
            brief["weather"] = info
            logger.info("天气小字：%s", info)
        return text

    return get_weather_forecast