"""Typed Belief refresh-economy metadata.

The economy is a refresh-priority hint, not a truth score or an authority
signal.  Every factor is supplied by a registered source; missing or invalid
factors remain unknown and therefore cannot be guessed into a priority.
"""

from __future__ import annotations

from datetime import datetime, timezone
import math
import re
from typing import Any


BELIEF_ECONOMY_SCHEMA_VERSION = "veyra.belief.economy.v1"
ECONOMY_STATUS_UNKNOWN = "unknown"
ECONOMY_STATUS_COMPLETE = "complete"
_SOURCE = re.compile(r"^[a-z][a-z0-9_.:/-]{1,119}$")
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,239}$")
_FACTOR_KEYS = ("importance", "change_probability", "decision_impact")


class BeliefEconomyError(ValueError):
    """Raised when economy metadata cannot be admitted safely."""


def make_economy(
    *,
    importance: float | None,
    importance_source: str,
    change_probability: float | None,
    change_probability_source: str,
    decision_impact: float | None,
    decision_impact_source_refs: list[str] | None = None,
    next_refresh_at: str | None = None,
    max_staleness_seconds: int | None = None,
) -> dict[str, Any]:
    """Build canonical economy metadata without inferring any factor."""

    values = (importance, change_probability, decision_impact)
    complete = all(item is not None for item in values)
    return validate_economy(
        {
            "schema_version": BELIEF_ECONOMY_SCHEMA_VERSION,
            "importance": {
                "value": importance,
                "source": importance_source,
            },
            "change_probability": {
                "value": change_probability,
                "source": change_probability_source,
            },
            "decision_impact": {
                "value": decision_impact,
                "source_refs": list(decision_impact_source_refs or []),
            },
            "belief_value": (
                round(math.prod(float(item) for item in values), 6)
                if complete
                else None
            ),
            "evaluation_status": (
                ECONOMY_STATUS_COMPLETE if complete else ECONOMY_STATUS_UNKNOWN
            ),
            "next_refresh_at": next_refresh_at,
            "max_staleness_seconds": max_staleness_seconds,
        }
    )


def unknown_economy() -> dict[str, Any]:
    """Return the explicit unknown projection used for missing metadata."""

    return make_economy(
        importance=None,
        importance_source="registered_policy",
        change_probability=None,
        change_probability_source="producer_policy",
        decision_impact=None,
    )


def validate_economy(value: Any) -> dict[str, Any]:
    """Validate and normalize the versioned economy contract.

    Unknown is a first-class result.  No default numeric factor is accepted,
    and arbitrary prose cannot be used as a factor source or source reference.
    """

    if not isinstance(value, dict):
        raise BeliefEconomyError("belief economy must be an object")
    expected = {
        "schema_version",
        "importance",
        "change_probability",
        "decision_impact",
        "belief_value",
        "evaluation_status",
        "next_refresh_at",
        "max_staleness_seconds",
    }
    if set(value) != expected:
        raise BeliefEconomyError("belief economy fields are not canonical")
    if value.get("schema_version") != BELIEF_ECONOMY_SCHEMA_VERSION:
        raise BeliefEconomyError("belief economy schema is unsupported")
    factors: dict[str, dict[str, Any]] = {}
    for key in _FACTOR_KEYS:
        raw = value.get(key)
        if not isinstance(raw, dict):
            raise BeliefEconomyError(f"{key} factor must be an object")
        if key == "decision_impact":
            if set(raw) != {"value", "source_refs"}:
                raise BeliefEconomyError("decision impact fields are not canonical")
            source = None
            refs = raw.get("source_refs")
            if not isinstance(refs, list) or len(refs) > 16:
                raise BeliefEconomyError("decision impact source_refs are invalid")
            normalized_refs: list[str] = []
            for ref in refs:
                if not isinstance(ref, str) or not _REF.fullmatch(ref):
                    raise BeliefEconomyError("decision impact source_ref is invalid")
                normalized_refs.append(ref)
            raw_value = raw.get("value")
            factors[key] = {
                "value": _factor_value(raw_value, key),
                "source_refs": sorted(set(normalized_refs)),
            }
            continue
        if set(raw) != {"value", "source"}:
            raise BeliefEconomyError(f"{key} fields are not canonical")
        source = raw.get("source")
        if not isinstance(source, str) or not _SOURCE.fullmatch(source):
            raise BeliefEconomyError(f"{key} source is invalid")
        factors[key] = {
            "value": _factor_value(raw.get("value"), key),
            "source": source,
        }

    values = [factors[key]["value"] for key in _FACTOR_KEYS]
    complete = all(item is not None for item in values)
    expected_value = (
        round(math.prod(float(item) for item in values), 6)
        if complete
        else None
    )
    if value.get("belief_value") != expected_value:
        raise BeliefEconomyError("belief_value does not match typed factors")
    status = value.get("evaluation_status")
    if status not in {ECONOMY_STATUS_UNKNOWN, ECONOMY_STATUS_COMPLETE}:
        raise BeliefEconomyError("belief economy evaluation status is invalid")
    if (status == ECONOMY_STATUS_COMPLETE) != complete:
        raise BeliefEconomyError("belief economy status does not match factors")
    next_refresh_at = value.get("next_refresh_at")
    if next_refresh_at is not None:
        if not isinstance(next_refresh_at, str):
            raise BeliefEconomyError("next_refresh_at is invalid")
        parsed = _parse_time(next_refresh_at)
        if parsed is None or _canonical_time(parsed) != next_refresh_at:
            raise BeliefEconomyError("next_refresh_at must be canonical UTC")
        next_refresh_at = _canonical_time(parsed)
    max_staleness = value.get("max_staleness_seconds")
    if max_staleness is not None and (
        not isinstance(max_staleness, int)
        or isinstance(max_staleness, bool)
        or not 1 <= max_staleness <= 31_536_000
    ):
        raise BeliefEconomyError("max_staleness_seconds is invalid")
    return {
        "schema_version": BELIEF_ECONOMY_SCHEMA_VERSION,
        "importance": factors["importance"],
        "change_probability": factors["change_probability"],
        "decision_impact": factors["decision_impact"],
        "belief_value": expected_value,
        "evaluation_status": (
            ECONOMY_STATUS_COMPLETE if complete else ECONOMY_STATUS_UNKNOWN
        ),
        "next_refresh_at": next_refresh_at,
        "max_staleness_seconds": max_staleness,
    }


def economy_value(value: Any) -> float | None:
    """Read a valid numeric refresh priority, otherwise fail closed."""

    try:
        normalized = validate_economy(value)
    except BeliefEconomyError:
        return None
    if normalized["evaluation_status"] != ECONOMY_STATUS_COMPLETE:
        return None
    selected = normalized.get("belief_value")
    return float(selected) if isinstance(selected, (int, float)) else None


def _factor_value(value: Any, key: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BeliefEconomyError(f"{key} value is invalid")
    selected = float(value)
    if not math.isfinite(selected) or not 0.0 <= selected <= 1.0:
        raise BeliefEconomyError(f"{key} value is outside [0, 1]")
    return round(selected, 6)


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _canonical_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


__all__ = [
    "BELIEF_ECONOMY_SCHEMA_VERSION",
    "BeliefEconomyError",
    "ECONOMY_STATUS_COMPLETE",
    "ECONOMY_STATUS_UNKNOWN",
    "economy_value",
    "make_economy",
    "unknown_economy",
    "validate_economy",
]
