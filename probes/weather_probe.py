from __future__ import annotations

from datetime import date, timedelta
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote

from probes.http_utils import fetch_json
from probes.schema import probe_payload
from interface.living_source_payload import weather_coverage


class WeatherProbe:
    """Read-only current or one-day forecast weather via Open-Meteo.

    The Living Source path supplies ``location`` and optional ``target_date``
    as typed binding parameters.  ``text`` remains API-compatible for older
    callers but is not interpreted as a location source.
    """

    CURRENT_TTL_SECONDS = 900
    FORECAST_TTL_SECONDS = 21600
    FORECAST_DAYS = 16

    def run(
        self,
        text: str = "",
        *,
        location: str | None = None,
        target_date: str | None = None,
    ) -> dict:
        location = (location or "").strip()
        if not location:
            return probe_payload(
                probe="weather_probe",
                target="weather",
                status="missing_target",
                summary="Weather probe needs a typed city or place parameter.",
                confidence=0.5,
                ttl_seconds=900,
                details={"configured": True},
            )
        canonical_target_date = self._canonical_target_date(target_date)
        if target_date not in (None, "") and canonical_target_date is None:
            return probe_payload(
                probe="weather_probe",
                target=location,
                status="unavailable",
                summary="Weather target_date must be an ISO calendar date.",
                confidence=0.4,
                ttl_seconds=300,
                details={"location_query": location, "target_date": target_date},
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
        timezone_name = geo.get("timezone") or "UTC"
        forecast = (
            self._forecast_weather(
                geo["latitude"],
                geo["longitude"],
                timezone_name,
                canonical_target_date,
            )
            if canonical_target_date
            else self._current_weather(geo["latitude"], geo["longitude"], timezone_name)
        )
        coverage = weather_coverage(
            location,
            canonical_target_date,
            resolved_place=geo,
        )
        if forecast.get("status") != "ok":
            # A valid forecast target outside Open-Meteo's bounded horizon is
            # an honest empty observation, never a current-weather success.
            empty_status = str(forecast.get("status") or "").lower() == "empty"
            return probe_payload(
                probe="weather_probe",
                target=location,
                status="empty" if empty_status else "unavailable",
                summary=str(forecast.get("summary") or "Weather service unavailable."),
                confidence=0.4,
                ttl_seconds=300,
                details={"location": geo, "coverage": coverage, **forecast},
            )
        if canonical_target_date:
            day = forecast.get("forecast") if isinstance(forecast.get("forecast"), dict) else {}
            description = str(day.get("weather_description") or "unknown")
            high = day.get("temperature_2m_max")
            low = day.get("temperature_2m_min")
            range_text = ""
            if high is not None or low is not None:
                range_text = f", {low}–{high}°C" if low is not None and high is not None else f", {high if high is not None else low}°C"
            summary = f"{geo.get('name') or location} {canonical_target_date}: {description}{range_text}"
            details = {
                "location": geo.get("name") or location,
                "latitude": geo.get("latitude"),
                "longitude": geo.get("longitude"),
                "timezone": timezone_name,
                "coverage": coverage,
                "forecast": day,
            }
            claim_key = f"weather:{location}:forecast:{canonical_target_date}"
            ttl_seconds = self.FORECAST_TTL_SECONDS
        else:
            current = forecast.get("current") if isinstance(forecast.get("current"), dict) else {}
            description = str(current.get("weather_description") or "unknown")
            temp = current.get("temperature_2m")
            summary = f"{geo.get('name') or location}: {description}, {temp}°C" if temp is not None else f"{geo.get('name') or location}: {description}"
            details = {
                "location": geo.get("name") or location,
                "latitude": geo.get("latitude"),
                "longitude": geo.get("longitude"),
                "timezone": timezone_name,
                "coverage": coverage,
                "weather_description": description,
                "current": current,
            }
            claim_key = f"weather:{location}:current"
            ttl_seconds = self.CURRENT_TTL_SECONDS
        return probe_payload(
            probe="weather_probe",
            target=location,
            status="ok",
            summary=summary,
            confidence=0.88,
            ttl_seconds=ttl_seconds,
            details=details,
            claims=[
                {
                    "key": claim_key,
                    "claim": summary,
                    "confidence": 0.88,
                    "source": "weather_probe",
                    "ttl_seconds": ttl_seconds,
                }
            ],
        )

    @staticmethod
    def _canonical_target_date(value: Any) -> str | None:
        if value in (None, ""):
            return None
        raw = str(value).strip()
        try:
            parsed = date.fromisoformat(raw)
            return parsed.isoformat()
        except (TypeError, ValueError):
            return None

    def _geocode(self, location: str) -> dict:
        last_error: dict[str, Any] = {"status": "error", "summary": f"No geocoding result for {location}."}
        for candidate in self._location_candidates(location):
            result = self._geocode_one(candidate)
            if result.get("status") == "ambiguous":
                return result
            if result.get("status") == "ok":
                return result
            last_error = result
        return last_error

    def _geocode_one(self, location: str) -> dict:
        url = f"https://geocoding-api.open-meteo.com/v1/search?name={quote(location)}&count=3&language=zh"
        try:
            body = fetch_json(url, timeout=5)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            return {"status": "error", "summary": f"Geocoding failed: {exc}"}
        results = body.get("results") if isinstance(body.get("results"), list) else []
        if not results:
            return {"status": "error", "summary": f"No geocoding result for {location}."}
        candidates = [item for item in results if isinstance(item, dict)]
        selected = self._select_geocode_candidate(candidates)
        if selected is None:
            return {
                "status": "ambiguous",
                "summary": f"Provider returned comparable places for {location}; choose a more specific place.",
                "candidates": [
                    {
                        "name": item.get("name"),
                        "admin1": item.get("admin1"),
                        "country": item.get("country"),
                        "feature_code": item.get("feature_code"),
                        "population": item.get("population"),
                    }
                    for item in candidates[:3]
                ],
            }
        item = selected
        return {
            "status": "ok",
            "name": item.get("name") or location,
            "provider_id": item.get("id"),
            "feature_code": item.get("feature_code"),
            "population": item.get("population"),
            "latitude": item.get("latitude"),
            "longitude": item.get("longitude"),
            "timezone": item.get("timezone"),
            "admin1": item.get("admin1"),
            "country": item.get("country"),
        }

    @staticmethod
    def _select_geocode_candidate(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Select only a structurally dominant provider place.

        Administrative feature rank wins over an ordinary populated place;
        population can break a same-rank tie only when it is clearly
        dominant. Comparable same-rank candidates stay ambiguous.
        """

        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        feature_rank = {
            "PPLC": 5,
            "PPLA": 4,
            "PPLA2": 3,
            "PPLA3": 2,
            "PPLA4": 1,
            "PPL": 0,
        }

        def rank(item: dict[str, Any]) -> tuple[int, int, float]:
            feature = str(item.get("feature_code") or "").upper()
            administrative = 1 if feature.startswith("PPLA") else 0
            population = item.get("population")
            numeric_population = (
                float(population)
                if isinstance(population, (int, float)) and not isinstance(population, bool)
                else 0.0
            )
            return (feature_rank.get(feature, 0), administrative, numeric_population)

        ordered = sorted(candidates, key=rank, reverse=True)
        best = rank(ordered[0])
        second = rank(ordered[1])
        if best[:2] > second[:2]:
            return ordered[0]
        best_population = best[2]
        second_population = second[2]
        if best_population > 0 and second_population > 0 and best_population >= second_population * 5:
            return ordered[0]
        return None

    def _location_candidates(self, location: str) -> list[str]:
        loc = (location or "").strip()
        if not loc:
            return []
        candidates: list[str] = [loc]
        without_province = re.sub(r"^[\u4e00-\u9fff]{2,8}(?:省|自治区|特别行政区)", "", loc)
        if without_province and without_province != loc:
            candidates.append(without_province)
        city_district = re.search(r"([\u4e00-\u9fff]{2,8}市)([\u4e00-\u9fff]{2,8}[区县])", without_province or loc)
        if city_district:
            city = city_district.group(1)
            district = city_district.group(2)
            candidates.extend([f"{city}{district}", district, city, city.rstrip("市")])
        if "市" in loc:
            city, rest = loc.split("市", 1)
            rest = rest.strip(" 的")
            if rest:
                candidates.extend([rest, f"{city}市", city])
        # 贵阳花溪区 → 花溪区, 贵阳, 贵阳市花溪区
        for suffix in ("区", "县"):
            if loc.endswith(suffix) and len(loc) >= 5:
                district_len = 3 if loc.endswith("区") else 2
                district = loc[-district_len:]
                prefix = loc[:-district_len].strip(" 市")
                if district.endswith(suffix) and len(prefix) >= 2:
                    candidates.extend([district, prefix, f"{prefix}市{district}"])
        if re.search(r"[\u4e00-\u9fff]", loc):
            candidates.append(f"{loc}, China")
        deduped: list[str] = []
        seen: set[str] = set()
        for item in candidates:
            key = item.strip()
            if key and key not in seen:
                seen.add(key)
                deduped.append(key)
        return deduped

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

    def _forecast_weather(
        self,
        latitude: float,
        longitude: float,
        timezone: str,
        target_date: str,
    ) -> dict:
        url = (
            "https://api.open-meteo.com/v1/forecast?"
            f"latitude={latitude}&longitude={longitude}"
            "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
            f"&forecast_days={self.FORECAST_DAYS}&timezone={quote(timezone)}"
        )
        try:
            body = fetch_json(url, timeout=5)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            return {"status": "error", "summary": f"Forecast failed: {exc}"}
        daily = body.get("daily") if isinstance(body.get("daily"), dict) else {}
        dates = daily.get("time") if isinstance(daily.get("time"), list) else []
        try:
            index = dates.index(target_date)
        except ValueError:
            return {
                "status": "empty",
                "summary": f"No forecast is available for {target_date} within the provider horizon.",
                "next_eligible_at": (
                    date.fromisoformat(target_date) - timedelta(days=self.FORECAST_DAYS - 1)
                ).isoformat() + "T00:00:00Z",
            }

        def item(key: str) -> Any:
            values = daily.get(key)
            return values[index] if isinstance(values, list) and index < len(values) else None

        code = item("weather_code")
        forecast = {
            "date": target_date,
            "weather_code": code,
            "weather_description": self._weather_code_label(code),
            "temperature_2m_max": item("temperature_2m_max"),
            "temperature_2m_min": item("temperature_2m_min"),
            "precipitation_probability_max": item("precipitation_probability_max"),
        }
        if all(value is None for key, value in forecast.items() if key not in {"date", "weather_description"}):
            return {
                "status": "empty",
                "summary": f"No forecast facts are available for {target_date}.",
                "next_eligible_at": target_date + "T00:00:00Z",
            }
        return {"status": "ok", "forecast": forecast}

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
            80: "阵雨",
            81: "中阵雨",
            82: "强阵雨",
            85: "小阵雪",
            86: "大阵雪",
            95: "雷暴",
            96: "雷暴伴冰雹",
            99: "强雷暴伴冰雹",
        }
        try:
            return mapping.get(int(code), f"天气码 {code}")
        except (TypeError, ValueError):
            return "未知"
