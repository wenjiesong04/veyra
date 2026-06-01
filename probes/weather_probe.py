from __future__ import annotations

import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote

from probes.http_utils import fetch_json
from probes.schema import probe_payload


class WeatherProbe:
    """Read-only current weather via Open-Meteo (no API key)."""

    def run(self, text: str = "") -> dict:
        location = self._extract_location(text)
        if not location:
            return probe_payload(
                probe="weather_probe",
                target="weather",
                status="missing_target",
                summary="Weather probe needs a city or place name in the question.",
                confidence=0.5,
                ttl_seconds=900,
                details={"configured": True},
            )
        geo = self._geocode(location)
        if geo.get("status") != "ok":
            return probe_payload(
                probe="weather_probe",
                target=location,
                status="unavailable",
                summary=str(geo.get("summary") or f"Could not geocode {location}."),
                confidence=0.4,
                ttl_seconds=300,
                details={"location_query": location, **geo},
            )
        forecast = self._current_weather(geo["latitude"], geo["longitude"], geo.get("timezone") or "UTC")
        if forecast.get("status") != "ok":
            return probe_payload(
                probe="weather_probe",
                target=location,
                status="unavailable",
                summary=str(forecast.get("summary") or "Weather service unavailable."),
                confidence=0.4,
                ttl_seconds=300,
                details={"location": geo, **forecast},
            )
        current = forecast.get("current") if isinstance(forecast.get("current"), dict) else {}
        description = str(current.get("weather_description") or "unknown")
        temp = current.get("temperature_2m")
        summary = f"{geo.get('name') or location}: {description}, {temp}°C" if temp is not None else f"{geo.get('name') or location}: {description}"
        return probe_payload(
            probe="weather_probe",
            target=location,
            status="ok",
            summary=summary,
            confidence=0.88,
            ttl_seconds=900,
            details={
                "location": geo.get("name") or location,
                "latitude": geo.get("latitude"),
                "longitude": geo.get("longitude"),
                "timezone": geo.get("timezone"),
                "weather_description": description,
                "current": current,
            },
            claims=[
                {
                    "key": f"weather:{location}:current",
                    "claim": summary,
                    "confidence": 0.88,
                    "source": "weather_probe",
                    "ttl_seconds": 900,
                }
            ],
        )

    def _extract_location(self, text: str) -> str:
        cleaned = (text or "").strip()
        if not cleaned:
            return ""
        patterns = [
            r"(?:在|于)\s*([^\s，,。.?！!]{2,24}?)(?:的)?(?:天气|气温)",
            r"([^\s，,。.?！!]{2,24}?)(?:今天|现在|当前|明天|今日)?(?:的)?(?:天气|气温)",
            r"(?:weather in|weather for)\s+([A-Za-z][A-Za-z\s.-]{1,40})",
        ]
        for pattern in patterns:
            match = re.search(pattern, cleaned, flags=re.IGNORECASE)
            if match:
                location = self._clean_location(match.group(1))
                if location:
                    return location
        for token in ("北京", "上海", "广州", "深圳", "杭州", "成都", "武汉", "西安", "南京", "重庆", "天津", "苏州"):
            if token in cleaned:
                return token
        return ""

    def _clean_location(self, value: str) -> str:
        location = (value or "").strip(" 的？?！!，,。")
        for prefix in ("今天", "现在", "当前", "明天", "今日"):
            if location.startswith(prefix) and len(location) > len(prefix):
                location = location[len(prefix) :]
        for suffix in ("今天", "现在", "当前", "明天", "今日", "的"):
            if location.endswith(suffix) and len(location) > len(suffix):
                location = location[: -len(suffix)]
        return location.strip(" 的？?！!，,。")

    def _geocode(self, location: str) -> dict:
        url = f"https://geocoding-api.open-meteo.com/v1/search?name={quote(location)}&count=1&language=zh"
        try:
            body = fetch_json(url, timeout=5)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            return {"status": "error", "summary": f"Geocoding failed: {exc}"}
        results = body.get("results") if isinstance(body.get("results"), list) else []
        if not results:
            return {"status": "error", "summary": f"No geocoding result for {location}."}
        item = results[0] if isinstance(results[0], dict) else {}
        return {
            "status": "ok",
            "name": item.get("name") or location,
            "latitude": item.get("latitude"),
            "longitude": item.get("longitude"),
            "timezone": item.get("timezone"),
        }

    def _current_weather(self, latitude: float, longitude: float, timezone: str) -> dict:
        url = (
            "https://api.open-meteo.com/v1/forecast?"
            f"latitude={latitude}&longitude={longitude}&current=temperature_2m,weather_code&timezone={quote(timezone)}"
        )
        try:
            body = fetch_json(url, timeout=5)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            return {"status": "error", "summary": f"Forecast failed: {exc}"}
        current = body.get("current") if isinstance(body.get("current"), dict) else {}
        code = current.get("weather_code")
        return {
            "status": "ok",
            "current": {
                **current,
                "weather_description": self._weather_code_label(code),
            },
        }

    def _weather_code_label(self, code: Any) -> str:
        mapping = {
            0: "晴",
            1: "大部晴朗",
            2: "多云",
            3: "阴",
            45: "雾",
            48: "雾凇",
            51: "小毛毛雨",
            53: "毛毛雨",
            55: "大毛毛雨",
            61: "小雨",
            63: "中雨",
            65: "大雨",
            71: "小雪",
            73: "中雪",
            75: "大雪",
            80: "阵雨",
            95: "雷暴",
        }
        try:
            return mapping.get(int(code), f"天气码 {code}")
        except (TypeError, ValueError):
            return "未知"
