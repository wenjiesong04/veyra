"""Strict JSON and receipt-payload helpers for Living Source contracts."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from typing import Any, Mapping

from common.living_source_primitives import LivingSourceContractError, canonical_utc, stable_digest


WEATHER_TARGET_DIGEST_NAMESPACE = "veyra.weather.evidence_target.v1"

# Semantic requirement names map only to fixed typed provider fields. They
# are neither text aliases nor source-query vocabulary.
_WEATHER_SEMANTIC_METRIC_GROUPS = {
    "temperature": {
        "current": frozenset({"temperature_2m"}),
        "forecast_day": frozenset({"temperature_2m_max", "temperature_2m_min"}),
    },
    "precipitation": {
        "current": frozenset({"precipitation"}),
        "forecast_day": frozenset({"precipitation_probability_max"}),
    },
    "conditions": {
        "current": frozenset({"weather_code", "weather_description"}),
        "forecast_day": frozenset({"weather_code", "weather_description"}),
    },
}


def _weather_text(value: Any, *, limit: int = 160) -> str:
    return " ".join(str(value or "").split())[:limit]


def _weather_date(value: Any) -> str | None:
    if value in (None, ""):
        return None
    raw = str(value).strip()
    try:
        # The binding validator stores canonical YYYY-MM-DD.  Keep this
        # helper aligned with that typed boundary for old Need projections.
        from datetime import date

        return date.fromisoformat(raw).isoformat()
    except (TypeError, ValueError):
        return None


def weather_target(location: Any, target_date: Any = None) -> dict[str, str | None] | None:
    selected_location = _weather_text(location)
    selected_date = _weather_date(target_date)
    if not selected_location:
        return None
    if target_date not in (None, "") and selected_date is None:
        return None
    return {"location": selected_location, "target_date": selected_date}


def weather_target_digest(location: Any, target_date: Any = None) -> str | None:
    target = weather_target(location, target_date)
    if target is None:
        return None
    # Keep this compatible with the core InformationNeed target digest:
    # canonical JSON of the typed target, SHA-256, no presentation wording or
    # provider timestamp.  The optional namespace constant is retained for
    # older callers that only use it as a diagnostic label.
    digest_target = {"location": target["location"]}
    if target.get("target_date"):
        digest_target["target_date"] = target["target_date"]
    payload = json.dumps(
        digest_target,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def weather_coverage(
    location: Any,
    target_date: Any = None,
    *,
    kind: str | None = None,
    target_digest: str | None = None,
    resolved_place: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    target = weather_target(location, target_date)
    if target is None:
        return None
    selected_kind = str(kind or ("forecast_day" if target["target_date"] else "current")).strip().lower()
    if selected_kind not in {"current", "forecast_day"}:
        return None
    coverage: dict[str, Any] = {
        "kind": selected_kind,
        "location": target["location"],
        "target_date": target["target_date"],
        "target_digest": str(target_digest or weather_target_digest(target["location"], target["target_date"])),
    }
    if isinstance(resolved_place, Mapping):
        selected_place: dict[str, Any] = {}
        for key in (
            "name",
            "provider_id",
            "feature_code",
            "population",
            "latitude",
            "longitude",
            "timezone",
            "country",
            "admin1",
        ):
            value = resolved_place.get(key)
            if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                selected_place[key] = value
        if selected_place:
            coverage["resolved_place"] = selected_place
    return coverage


def inject_weather_coverage(
    payload: Mapping[str, Any],
    *,
    location: Any,
    target_date: Any = None,
    target_digest: str | None = None,
) -> dict[str, Any]:
    """Add server-derived coverage only when a provider omitted it.

    A provider-supplied coverage claim is never repaired here.  Keeping a
    wrong claim visible lets the receipt projector reject it as a typed
    coverage mismatch instead of silently turning a wrong forecast into a
    successful observation.
    """

    result = json.loads(json.dumps(dict(payload), ensure_ascii=False, default=str))
    facts = result.get("facts")
    expected = weather_coverage(location, target_date)
    shape_matches_target = bool(
        isinstance(facts, dict)
        and (
            isinstance(facts.get("forecast"), Mapping)
            if target_date not in (None, "")
            else isinstance(facts.get("current"), Mapping)
        )
    )
    if shape_matches_target and expected is not None and not isinstance(facts.get("coverage"), Mapping):
        if target_digest:
            expected["target_digest"] = str(target_digest)
        facts["coverage"] = expected
    return result


def weather_coverage_matches(
    facts: Mapping[str, Any],
    *,
    location: Any,
    target_date: Any = None,
    expected_digest: str | None = None,
    observation_requirement: Mapping[str, Any] | None = None,
) -> tuple[bool, str]:
    """Check a receipt's server-relevant weather coverage without prose.

    The comparison is field-based and typed.  No text matching or provider
    reason is used to decide whether a Need can be resolved.
    """

    expected = weather_coverage(location, target_date)
    actual = facts.get("coverage") if isinstance(facts, Mapping) else None
    if expected is None:
        return False, "weather_target_missing"
    if not isinstance(actual, Mapping):
        return False, "weather_coverage_missing"
    for key in ("kind", "location", "target_date"):
        if actual.get(key) != expected.get(key):
            return False, "weather_coverage_mismatch"
    # Coverage is a server-derived binding claim, not the observation itself.
    # A successful receipt must carry the matching typed fact shape as well.
    # In particular, a forecast label may not turn current conditions (or a
    # different forecast day) into evidence for this target.
    observed = (
        facts.get("forecast")
        if expected["kind"] == "forecast_day"
        else facts.get("current")
    )
    if not isinstance(observed, Mapping):
        return False, "weather_observation_facts_missing"
    if expected["kind"] == "forecast_day" and observed.get("date") != expected["target_date"]:
        return False, "weather_forecast_date_mismatch"
    requirement = observation_requirement if isinstance(observation_requirement, Mapping) else {}
    required_coverage = str(requirement.get("coverage") or "any").strip().lower()
    if required_coverage not in {"", "any"} and required_coverage != str(expected.get("kind") or ""):
        return False, "weather_requirement_coverage_mismatch"
    required_metrics = requirement.get("metrics")
    if required_metrics is not None:
        if not isinstance(required_metrics, (list, tuple)):
            return False, "weather_requirement_metrics_invalid"
        for metric in required_metrics:
            metric_name = str(metric or "").strip()
            group = _WEATHER_SEMANTIC_METRIC_GROUPS.get(metric_name)
            if group is not None:
                if any(
                    field not in observed or observed.get(field) in (None, "")
                    for field in group[expected["kind"]]
                ):
                    return False, "weather_requirement_metric_missing"
                continue
            if not metric_name or metric_name not in observed or observed.get(metric_name) in (None, ""):
                return False, "weather_requirement_metric_missing"
    resolved_place = actual.get("resolved_place")
    if isinstance(resolved_place, Mapping):
        provider_id = resolved_place.get("provider_id")
        feature_code = str(resolved_place.get("feature_code") or "").strip()
        if provider_id in (None, "") or not feature_code:
            return False, "weather_resolved_place_identity_missing"
        latitude = resolved_place.get("latitude")
        longitude = resolved_place.get("longitude")
        if not isinstance(latitude, (int, float)) or isinstance(latitude, bool) or not isinstance(longitude, (int, float)) or isinstance(longitude, bool):
            return False, "weather_resolved_place_invalid"
        if not -90 <= float(latitude) <= 90 or not -180 <= float(longitude) <= 180:
            return False, "weather_resolved_place_invalid"
    actual_digest = str(actual.get("target_digest") or actual.get("evidence_target_digest") or "")
    expected_target_digest = str(expected_digest or expected["target_digest"])
    if actual_digest != expected_target_digest:
        return False, "weather_coverage_digest_mismatch"
    return True, "weather_coverage_exact"


def weather_material_digest(facts: Mapping[str, Any]) -> str:
    """Digest weather facts while ignoring observation-clock churn.

    A repeated watch receipt is still retained as evidence, but its provider
    observation timestamp must not by itself become a new cognitive delta.
    """

    def scrub(value: Any, *, key: str = "") -> Any:
        if isinstance(value, Mapping):
            return {
                str(name): scrub(item, key=str(name))
                for name, item in sorted(value.items(), key=lambda pair: str(pair[0]))
                if str(name).lower() not in {"observed_at", "observation_time", "timestamp", "time"}
            }
        if isinstance(value, list):
            return [scrub(item) for item in value]
        return value

    return stable_digest(scrub(dict(facts)), namespace="veyra.weather.material.v1")


_RECEIPT_TOP_LEVEL_KEYS = frozenset({"facts", "provider", "source", "summary"})
_FORBIDDEN_PAYLOAD_KEYS = frozenset(
    "path paths file filename command cmd shell tool tools tool_args args arguments env headers "
    "credential credentials token recipient recipients external_target raw debug traceback".split()
)


def validate_receipt_payload(source: str, status: str, payload: Mapping[str, Any]) -> None:
    unknown = set(str(key) for key in payload) - _RECEIPT_TOP_LEVEL_KEYS
    if unknown:
        raise LivingSourceContractError(f"receipt payload contains unsupported keys: {sorted(unknown)!r}")
    if status == "ok":
        facts = payload.get("facts")
        if not isinstance(facts, Mapping) or not facts:
            raise LivingSourceContractError("successful receipt must contain typed facts")
    if status == "revoked" and payload:
        raise LivingSourceContractError("revoked receipt must not retain provider facts")

    def visit(value: Any, path: tuple[str, ...] = ()) -> None:
        if isinstance(value, Mapping):
            for raw_key, item in value.items():
                key = str(raw_key)
                if key.lower() in _FORBIDDEN_PAYLOAD_KEYS:
                    raise LivingSourceContractError(f"receipt payload key {key!r} is forbidden")
                if key == "url" and (source != "public_web" or len(path) < 2 or path[-2] != "results"):
                    raise LivingSourceContractError("receipt URL is only allowed in public_web results")
                visit(item, path + (key,))
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                visit(item, path + (str(index),))
        elif isinstance(value, (str, bool, int)) or value is None:
            return
        elif isinstance(value, float):
            if value != value or value in (float("inf"), float("-inf")):
                raise LivingSourceContractError("receipt payload must contain finite JSON values")
        else:
            raise LivingSourceContractError("receipt payload contains a non-JSON value")

    visit(payload)


def _strict_json_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 8:
        raise LivingSourceContractError("provider payload nesting is too deep")
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            if not key or len(key) > 120:
                raise LivingSourceContractError("provider payload key is invalid")
            result[key] = _strict_json_value(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        if len(value) > 500:
            raise LivingSourceContractError("provider payload list is too large")
        return [_strict_json_value(item, depth=depth + 1) for item in value]
    if isinstance(value, bool) or value is None or isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise LivingSourceContractError("provider payload must contain finite JSON values")
        return value
    if isinstance(value, datetime):
        return canonical_utc(value)
    if isinstance(value, str):
        if len(value) > 4000:
            raise LivingSourceContractError("provider payload string is too long")
        return value
    raise LivingSourceContractError("provider payload contains a non-JSON value")


def normalize_provider_payload(value: Any, *, max_bytes: int = 32768) -> dict[str, Any]:
    """Keep provider output JSON-shaped and bounded before projection."""

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise LivingSourceContractError("provider payload must be an object")
    selected = _strict_json_value(dict(value))
    encoded = json.dumps(selected, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > max_bytes:
        raise LivingSourceContractError("provider payload exceeds receipt budget")
    return dict(selected)


__all__ = [
    "WEATHER_TARGET_DIGEST_NAMESPACE",
    "inject_weather_coverage",
    "normalize_provider_payload",
    "validate_receipt_payload",
    "weather_coverage",
    "weather_coverage_matches",
    "weather_material_digest",
    "weather_target",
    "weather_target_digest",
]
