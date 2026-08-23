"""Validation for server-owned Living Source parameters.

This module is intentionally separate from the durable row contracts.  It keeps
the contract module focused on identity, consent, request, and receipt shapes;
providers still receive only this validated scalar projection.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Mapping

from common.living_source_primitives import (
    FORBIDDEN_PARAMETER_KEYS,
    SOURCE_KINDS,
    SOURCE_PARAMETER_KEYS,
    LivingSourceContractError,
    _require_time,
    parse_utc,
)


def validate_parameters(source: str, parameters: Mapping[str, Any]) -> dict[str, Any]:
    if source not in SOURCE_KINDS:
        raise LivingSourceContractError("source kind is not registered")
    if not isinstance(parameters, Mapping):
        raise LivingSourceContractError("source parameters must be an object")
    allowed = SOURCE_PARAMETER_KEYS[source]
    selected: dict[str, Any] = {}
    for raw_key, value in parameters.items():
        key = str(raw_key).strip()
        if key in FORBIDDEN_PARAMETER_KEYS:
            raise LivingSourceContractError(f"parameter {key!r} is not permitted")
        if key not in allowed:
            raise LivingSourceContractError(
                f"parameter {key!r} is not allowed for source {source!r}"
            )
        if isinstance(value, (dict, list, tuple, set)):
            raise LivingSourceContractError(f"parameter {key!r} must be scalar")
        if isinstance(value, str) and len(value) > 600:
            raise LivingSourceContractError(f"parameter {key!r} is too long")
        if isinstance(value, bool) or isinstance(value, (int, float)):
            selected[key] = value
        elif value is None:
            selected[key] = None
        else:
            selected[key] = str(value)
    if source == "calendar":
        start = selected.get("window_start")
        end = selected.get("window_end")
        if (start is None) != (end is None):
            raise LivingSourceContractError("calendar window must include start and end")
        if start is not None and end is not None:
            start_value = _require_time(start, "calendar.window_start")
            end_value = _require_time(end, "calendar.window_end")
            if parse_utc(end_value) <= parse_utc(start_value):
                raise LivingSourceContractError("calendar window must be increasing")
            if parse_utc(end_value) - parse_utc(start_value) > timedelta(days=366):
                raise LivingSourceContractError("calendar window is too wide")
            selected["window_start"] = start_value
            selected["window_end"] = end_value
    if source == "weather":
        location = str(selected.get("location") or "").strip()
        if not location or len(location) > 160:
            raise LivingSourceContractError("weather location is required")
        selected["location"] = location
        target_date = selected.get("target_date")
        if target_date in (None, ""):
            selected.pop("target_date", None)
        else:
            # A source binding carries a calendar day, not an arbitrary
            # provider query.  Accept date-like ISO input but canonicalize it
            # once at the server boundary so current/forecast comparisons are
            # stable across retries and time zones.
            raw_date = str(target_date).strip()
            try:
                parsed_date = date.fromisoformat(raw_date[:10])
            except (TypeError, ValueError) as exc:
                raise LivingSourceContractError("weather target_date must be an ISO date") from exc
            if raw_date != parsed_date.isoformat():
                raise LivingSourceContractError("weather target_date must be an ISO date")
            selected["target_date"] = parsed_date.isoformat()
    if source == "public_web":
        query = " ".join(str(selected.get("query") or "").split())
        if not query or len(query) > 300:
            raise LivingSourceContractError("public_web query is required and bounded")
        selected["query"] = query
        max_results = selected.get("max_results", 5)
        if isinstance(max_results, bool) or not isinstance(max_results, int):
            raise LivingSourceContractError("public_web.max_results must be an integer")
        if not 1 <= max_results <= 10:
            raise LivingSourceContractError("public_web.max_results is out of range")
        selected["max_results"] = max_results
    if source == "agent_research":
        topic = " ".join(str(selected.get("topic") or "").split())
        if topic and len(topic) > 300:
            raise LivingSourceContractError("agent_research topic is too long")
        selected["topic"] = topic
    return selected


__all__ = ["validate_parameters"]
