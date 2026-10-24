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


@tool
def get_weather_forecast(city: str = "", days: int = 3,
                         latitude: float | None = None,
                         longitude: float | None = None) -> str:
    """查询指定位置的天气预报，包含当前天气和未来几天的预报。

    city: 城市名，中英文均可，例如 '深圳'、'Shenzhen'。用户没说城市时留空。
    days: 查询未来几天的预报，默认 3，范围 1-7。
    latitude / longitude: 精确坐标（WGS-84 或 GCJ-02 都行，城市级天气差异可忽略）。
        从 get_current_location 拿到坐标后直接传进来，比城市名更准。

    当用户询问天气、气温、是否下雨、出行建议时使用此工具。
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
            return "需要城市名或坐标：请先调用 get_current_location，或告诉我城市。"
        geo = _geocode(city)
        if geo is None:
            return f"找不到城市 '{city}'，请换一个更具体的城市名。"
        lat, lon, resolved_name = geo

    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,weather_code,wind_speed_10m,relative_humidity_2m",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
        "timezone": "auto",
        "forecast_days": days,
    }

    try:
        resp = requests.get(url, params=params, timeout=10)
        data = resp.json()
    except Exception as e:
        return f"调用天气 API 失败：{e}"

    if "error" in data:
        return f"天气 API 返回错误：{data.get('reason', '未知错误')}"

    current = data.get("current", {})
    cur_code = current.get("weather_code")
    cur_desc = WEATHER_CODE_MAP.get(cur_code, "未知")
    cur_temp = current.get("temperature_2m")
    cur_hum = current.get("relative_humidity_2m")
    cur_wind = current.get("wind_speed_10m")

    lines = [
        f"{resolved_name}当前天气：{cur_desc}，气温 {cur_temp}°C，"
        f"湿度 {cur_hum}%，风速 {cur_wind} km/h"
    ]

    daily = data.get("daily", {})
    dates = daily.get("time", [])
    tmax = daily.get("temperature_2m_max", [])
    tmin = daily.get("temperature_2m_min", [])
    precip = daily.get("precipitation_probability_max", [])
    codes = daily.get("weather_code", [])

    for i in range(len(dates)):
        desc = WEATHER_CODE_MAP.get(codes[i] if i < len(codes) else None, "未知")
        lines.append(
            f"{dates[i]}：{desc}，{tmin[i]}~{tmax[i]}°C，降水概率 {precip[i]}%"
        )

    return "\n".join(lines)