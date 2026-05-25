from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from probes.schema import probe_payload


class WeatherProbe:
    """Fetch current weather from Open-Meteo without requiring an API key."""

    def run(self, text: str = "") -> dict[str, Any]:
        location = self._extract_location(text)
        if not location:
            return probe_payload(
                probe="weather_probe",
                target="weather",
                status="missing_target",
                summary="Weather probe needs a city or place name.",
                confidence=0.55,
                ttl_seconds=120,
                details={"configured": True, "model_assist": False},
            )
        try:
            place = self._geocode(location)
            weather = self._current_weather(place)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
            return probe_payload(
                probe="weather_probe",
                target=location,
                status="unavailable",
                summary=f"无法获取 {location} 的实时天气：{exc}",
                confidence=0.35,
                ttl_seconds=120,
                details={"location": location, "error": str(exc), "provider": "open-meteo", "model_assist": False},
            )

        current = weather.get("current") if isinstance(weather.get("current"), dict) else {}
        units = weather.get("current_units") if isinstance(weather.get("current_units"), dict) else {}
        code = int(float(current.get("weather_code") or 0))
        description = WEATHER_CODES.get(code, f"WMO {code}")
        name = self._place_name(place)
        temperature = current.get("temperature_2m")
        apparent = current.get("apparent_temperature")
        humidity = current.get("relative_humidity_2m")
        precipitation = current.get("precipitation")
        wind_speed = current.get("wind_speed_10m")
        observed_time = str(current.get("time") or "")
        timezone = str(weather.get("timezone") or place.get("timezone") or "")
        temp_unit = str(units.get("temperature_2m") or "°C")
        wind_unit = str(units.get("wind_speed_10m") or "km/h")
        rain_unit = str(units.get("precipitation") or "mm")
        summary = (
            f"{name}当前天气：{description}，{temperature}{temp_unit}，体感{apparent}{temp_unit}，"
            f"湿度{humidity}%，降水{precipitation}{rain_unit}，风速{wind_speed}{wind_unit}。"
            f"观测时间：{observed_time}（{timezone}）。"
        )
        return probe_payload(
            probe="weather_probe",
            target=name,
            status="ok",
            summary=summary,
            confidence=0.9,
            ttl_seconds=900,
            details={
                "location": name,
                "provider": "open-meteo",
                "latitude": place.get("latitude"),
                "longitude": place.get("longitude"),
                "timezone": timezone,
                "current": current,
                "current_units": units,
                "weather_description": description,
                "model_assist": False,
            },
            claims=[
                {
                    "key": f"weather:{name}:current",
                    "claim": summary,
                    "confidence": 0.9,
                    "ttl_seconds": 900,
                    "evidence": {"provider": "open-meteo", "observed_time": observed_time},
                }
            ],
        )

    def _geocode(self, location: str) -> dict[str, Any]:
        url = (
            "https://geocoding-api.open-meteo.com/v1/search?"
            f"name={quote(location)}&count=1&language=zh&format=json"
        )
        data = self._get_json(url)
        results = data.get("results") if isinstance(data.get("results"), list) else []
        if not results:
            raise ValueError(f"Open-Meteo could not geocode location: {location}")
        first = results[0]
        if not isinstance(first, dict):
            raise ValueError(f"Open-Meteo returned an invalid geocoding result for: {location}")
        return first

    def _current_weather(self, place: dict[str, Any]) -> dict[str, Any]:
        query = urlencode(
            {
                "latitude": place.get("latitude"),
                "longitude": place.get("longitude"),
                "current": "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m,wind_direction_10m",
                "timezone": place.get("timezone") or "auto",
                "forecast_days": 1,
            }
        )
        return self._get_json(f"https://api.open-meteo.com/v1/forecast?{query}")

    def _get_json(self, url: str) -> dict[str, Any]:
        request = Request(url, method="GET", headers={"User-Agent": "Veyra-WeatherProbe/0.1"})
        with urlopen(request, timeout=8) as response:
            return json.loads(response.read().decode("utf-8"))

    def _extract_location(self, text: str) -> str:
        normalized = re.sub(r"\s+", " ", text).strip()
        patterns = [
            r"(?:weather|temperature)\s+(?:in|for)?\s*([A-Za-z .'-]{2,60})",
            r"([\u4e00-\u9fffA-Za-z .'-]{2,60}?)(?:的)?(?:天气|气温|温度)",
        ]
        for pattern in patterns:
            match = re.search(pattern, normalized, flags=re.IGNORECASE)
            if match:
                return self._clean_location(match.group(1))
        return ""

    def _clean_location(self, value: str) -> str:
        text = value.strip(" ，,。?？")
        for prefix in ["现在的", "现在", "当前的", "当前", "今天的", "今天", "查一下", "查询", "看一下"]:
            if text.startswith(prefix):
                text = text[len(prefix) :].strip(" 的")
        return text.strip(" 的，,。?？")

    def _place_name(self, place: dict[str, Any]) -> str:
        parts = [place.get("admin1"), place.get("name"), place.get("country")]
        return " ".join(str(part) for part in parts if part)


WEATHER_CODES = {
    0: "晴",
    1: "大部晴朗",
    2: "局部多云",
    3: "阴",
    45: "雾",
    48: "雾凇",
    51: "小毛毛雨",
    53: "中等毛毛雨",
    55: "强毛毛雨",
    56: "冻毛毛雨",
    57: "强冻毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "冻雨",
    67: "强冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "雪粒",
    80: "小阵雨",
    81: "中等阵雨",
    82: "强阵雨",
    85: "小阵雪",
    86: "强阵雪",
    95: "雷暴",
    96: "雷暴伴小冰雹",
    99: "雷暴伴大冰雹",
}
