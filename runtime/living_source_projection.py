"""Typed, privacy-minimized projections for Living Source provider output."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import urlparse

from interface.living_source_contract import (
    LivingSourceContractError,
    canonical_utc,
    normalize_provider_payload,
    parse_utc,
)


class SourceProjectionError(LivingSourceContractError):
    """Provider output cannot be admitted as typed Living Context evidence."""


def _text(value: Any, *, limit: int, required: bool = False) -> str:
    selected = " ".join(str(value or "").split())
    if len(selected) > limit:
        selected = selected[:limit]
    if required and not selected:
        raise SourceProjectionError("typed source fact text is required")
    return selected


def _status(value: Any) -> str:
    selected = str(value or "unknown").strip().lower()
    return {
        "available": "ok",
        "success": "ok",
        "validated": "ok",
        "not_configured": "unavailable",
        "missing_target": "unavailable",
        "error": "unknown",
        "failed": "unknown",
    }.get(selected, selected if selected in {"ok", "empty", "pending", "unknown", "timeout", "unavailable"} else "unknown")


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _canonical_event_time(value: Any) -> str:
    try:
        parsed = parse_utc(str(value))
    except Exception as exc:
        raise SourceProjectionError("calendar event time is invalid") from exc
    return canonical_utc(parsed)


def _calendar_facts(raw: Mapping[str, Any]) -> dict[str, Any]:
    source_facts = _mapping(raw.get("facts"))
    rows = source_facts.get("events", raw.get("events"))
    if rows is None:
        return {"events": []}
    if not isinstance(rows, list):
        raise SourceProjectionError("calendar events must be a list")
    events: list[dict[str, Any]] = []
    for item in rows[:201]:
        if not isinstance(item, Mapping):
            continue
        event_id = _text(item.get("event_id", item.get("id")), limit=240, required=True)
        title = _text(item.get("title", item.get("summary")), limit=600, required=True)
        starts_at = _canonical_event_time(item.get("starts_at", item.get("start")))
        ends_at = _canonical_event_time(item.get("ends_at", item.get("end")))
        if parse_utc(ends_at) <= parse_utc(starts_at):
            raise SourceProjectionError("calendar event end must be after start")
        location = _text(item.get("location"), limit=600)
        all_day = item.get("all_day", False)
        if not isinstance(all_day, bool):
            raise SourceProjectionError("calendar all_day must be boolean")
        events.append(
            {
                "event_id": event_id,
                "title": title,
                "starts_at": starts_at,
                "ends_at": ends_at,
                "location": location,
                "all_day": all_day,
            }
        )
        if len(events) >= 200:
            break
    events.sort(key=lambda row: (row["starts_at"], row["event_id"]))
    facts: dict[str, Any] = {"events": events}
    for key in ("window_start", "window_end"):
        if key in source_facts or key in raw:
            try:
                facts[key] = _canonical_event_time(source_facts.get(key, raw.get(key)))
            except SourceProjectionError:
                pass
    return facts


_WEATHER_CURRENT_KEYS = frozenset(
    {
        "time",
        "temperature_2m",
        "apparent_temperature",
        "relative_humidity_2m",
        "precipitation",
        "rain",
        "showers",
        "snowfall",
        "weather_code",
        "weather_description",
        "wind_speed_10m",
    }
)


def _weather_facts(raw: Mapping[str, Any]) -> dict[str, Any]:
    source_facts = _mapping(raw.get("facts"))
    details = _mapping(raw.get("details"))
    location = _text(source_facts.get("location", raw.get("location", details.get("location"))), limit=160, required=True)
    coverage_present = (
        "coverage" in source_facts
        or "coverage" in raw
        or "coverage" in details
    )
    coverage = source_facts.get("coverage", raw.get("coverage", details.get("coverage")))
    selected_coverage: dict[str, Any] | None = None
    if isinstance(coverage, Mapping):
        kind = str(coverage.get("kind") or "").strip().lower()
        coverage_location = _text(coverage.get("location"), limit=160)
        target_date = coverage.get("target_date")
        target_date = str(target_date).strip() if target_date not in (None, "") else None
        target_digest = str(
            coverage.get("target_digest")
            or coverage.get("evidence_target_digest")
            or ""
        ).strip()
        # Preserve malformed provider coverage as a bounded typed object so
        # the core apply seam can reject it.  Do not silently drop it and let
        # the runtime inject the binding target as if the provider omitted
        # coverage altogether.
        selected_coverage = {
            "kind": kind,
            "location": coverage_location,
            "target_date": target_date,
            "target_digest": target_digest,
        }
        resolved_place = coverage.get("resolved_place")
        if isinstance(resolved_place, Mapping):
            selected_coverage["resolved_place"] = {
                str(key): value
                for key, value in resolved_place.items()
                if str(key) in {
                    "name",
                    "provider_id",
                    "feature_code",
                    "population",
                    "latitude",
                    "longitude",
                    "timezone",
                    "country",
                    "admin1",
                }
                and isinstance(value, (str, int, float))
                and not isinstance(value, bool)
            }
    elif coverage_present:
        # Keep a malformed scalar/list claim visible; the receipt contract is
        # JSON-shaped, while ``weather_coverage_matches`` will reject it.
        selected_coverage = {"invalid": coverage}
    current = source_facts.get("current", raw.get("current", details.get("current")))
    current_map = _mapping(current)
    selected_current: dict[str, Any] = {}
    for key in _WEATHER_CURRENT_KEYS:
        value = current_map.get(key)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            selected_current[key] = value
    forecast = source_facts.get("forecast", raw.get("forecast", details.get("forecast")))
    selected_forecast: dict[str, Any] | None = None
    if isinstance(forecast, Mapping):
        selected_forecast = {}
        for key in (
            "date",
            "weather_code",
            "weather_description",
            "temperature_2m_max",
            "temperature_2m_min",
            "precipitation_probability_max",
        ):
            value = forecast.get(key)
            if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                selected_forecast[key] = value
        if not selected_forecast:
            selected_forecast = None
    if not selected_current and selected_forecast is None and selected_coverage is None:
        raise SourceProjectionError("weather typed facts are required")
    facts: dict[str, Any] = {"location": location}
    if selected_current:
        facts["current"] = selected_current
    if selected_forecast is not None:
        facts["forecast"] = selected_forecast
    if selected_coverage is not None:
        facts["coverage"] = selected_coverage
    next_eligible_at = source_facts.get(
        "next_eligible_at",
        raw.get("next_eligible_at", details.get("next_eligible_at")),
    )
    if isinstance(next_eligible_at, str) and next_eligible_at.strip():
        facts["next_eligible_at"] = next_eligible_at.strip()[:80]
    return facts


def _web_facts(raw: Mapping[str, Any]) -> dict[str, Any]:
    source_facts = _mapping(raw.get("facts"))
    details = _mapping(raw.get("details"))
    rows = source_facts.get("results", raw.get("results", details.get("results")))
    if rows is None:
        return {"results": []}
    if not isinstance(rows, list):
        raise SourceProjectionError("public_web results must be a list")
    results: list[dict[str, Any]] = []
    for item in rows[:10]:
        if not isinstance(item, Mapping):
            continue
        title = _text(item.get("title", item.get("name")), limit=240, required=True)
        url = _text(item.get("url", item.get("link", item.get("href"))), limit=1000, required=True)
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            continue
        snippet = _text(item.get("snippet", item.get("summary", item.get("content", item.get("text")))), limit=700)
        results.append({"rank": len(results) + 1, "title": title, "url": url, "snippet": snippet})
        if len(results) >= 5:
            break
    return {"results": results}


def project_provider_result(
    source: str,
    raw: Any,
    *,
    invocation_status: str,
    provider_id: str,
    default_ttl_seconds: int,
) -> tuple[str, dict[str, Any], str, int]:
    """Return ``status, receipt_payload, reason, ttl`` for one provider call."""

    if invocation_status == "timeout":
        return "timeout", {}, "source_provider_timeout", 0
    if invocation_status == "unavailable":
        return "unavailable", {}, "source_provider_unavailable", 0
    try:
        normalized = normalize_provider_payload(raw)
        source_status = _status(normalized.get("status"))
        raw_ttl = normalized.get("ttl_seconds", default_ttl_seconds)
        ttl = max(0, min(int(raw_ttl), int(default_ttl_seconds)))
        if source == "calendar":
            facts = _calendar_facts(normalized)
        elif source == "weather":
            facts = _weather_facts(normalized)
        elif source == "public_web":
            facts = _web_facts(normalized)
        else:
            raise SourceProjectionError("provider projection is not available for this source")
        if source_status == "ok" and not facts:
            raise SourceProjectionError("successful source result has no typed facts")
        if source == "calendar" and source_status == "ok" and not facts.get("events"):
            source_status = "empty"
        if source == "public_web" and source_status == "ok" and not facts.get("results"):
            source_status = "empty"
        if source_status == "ok" and source in {"weather", "public_web", "calendar"} and not facts:
            raise SourceProjectionError("successful source result has no typed facts")
        summary = _text(normalized.get("summary", normalized.get("reason")), limit=600)
        payload: dict[str, Any] = {"facts": facts, "provider": provider_id, "source": source}
        if summary:
            payload["summary"] = summary
        return source_status, payload, summary or "typed_source_observation", ttl
    except Exception as exc:
        return "unknown", {}, f"provider_payload_invalid:{type(exc).__name__}", 0


__all__ = ["SourceProjectionError", "project_provider_result"]
