from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import Any

from interface.agent_contract import AGENT_CONTRACT_VERSION


PROVIDER_CERTIFICATION_SCHEMA = "veyra.provider_certification.v1"
DEFAULT_REQUIRED_FEATURES = (
    "structured_task_packet",
    "rendered_prompt_fallback",
    "task_status",
    "stop_task",
)
CONNECTED_STATUSES = frozenset({"available", "ok", "success"})
UNCONFIGURED_STATUSES = frozenset(
    {"adapter_unconfigured", "not_configured", "unconfigured"}
)


def certify_agent_provider(
    *,
    runtime: str,
    capabilities: Any,
    observed_at: str | datetime | None,
    now: datetime | None = None,
    ttl_seconds: int = 300,
    required_features: Iterable[str] = DEFAULT_REQUIRED_FEATURES,
    trusted_native_adapter: bool = False,
) -> dict[str, Any]:
    """Evaluate a fresh provider observation without selecting the provider.

    The helper deliberately does not call an adapter and cannot mutate Agent
    configuration.  It consumes one caller-supplied observation, rejects the
    optimistic defaults added by ``normalize_capabilities`` for generic HTTP
    adapters, and returns only read-only eligibility.  A positive result is
    therefore evidence for a later governed decision, never an automatic
    provider switch or execution authorization.
    """

    selected_runtime = _required_runtime(runtime)
    selected_ttl = _ttl(ttl_seconds)
    selected_now = _aware_now(now)
    selected_features = _feature_names(required_features)
    document = capabilities if isinstance(capabilities, dict) else {}
    raw = (
        document.get("raw")
        if isinstance(document.get("raw"), dict)
        else document
    )
    compatibility = (
        document.get("compatibility")
        if isinstance(document.get("compatibility"), dict)
        else (
            raw.get("compatibility")
            if isinstance(raw.get("compatibility"), dict)
            else {}
        )
    )
    # A remote capability document is not allowed to promote itself to a
    # trusted native adapter. This bit must come from the locally constructed
    # adapter instance at the call site.
    native_adapter_claimed = compatibility.get("native_adapter") is True
    native_adapter = (
        trusted_native_adapter is True
        and selected_runtime == "openclaw"
    )

    parsed_observed = _parse_aware(observed_at)
    age_seconds: int | None = None
    freshness = "unknown"
    freshness_issue: str | None = None
    expires_at: str | None = None
    if parsed_observed is None:
        freshness_issue = "missing_or_invalid_observed_at"
    else:
        expires = parsed_observed + timedelta(seconds=selected_ttl)
        expires_at = expires.isoformat()
        delta = (selected_now - parsed_observed).total_seconds()
        if delta < -30:
            freshness_issue = "observed_at_in_future"
        else:
            age_seconds = max(0, int(delta))
            if selected_now <= expires:
                freshness = "fresh"
            else:
                freshness = "stale"
                freshness_issue = "observation_stale"

    outer_runtime = str(document.get("runtime") or "").strip()
    raw_runtime = str(raw.get("runtime") or "").strip()
    runtime_identity_match = (
        outer_runtime == selected_runtime
        and raw_runtime == selected_runtime
    )
    status = str(
        raw.get("status") or document.get("status") or "unknown"
    ).strip()
    connected = (
        document.get("connected") is True
        and status in CONNECTED_STATUSES
    )
    compatibility_status = str(
        compatibility.get("status") or "unknown"
    ).strip()

    reported_contract = str(
        (
            document.get("contract_version")
            if native_adapter and selected_runtime == "openclaw"
            else raw.get("contract_version")
            or raw.get("adapter_contract_version")
        )
        or ""
    ).strip()
    contract_match = reported_contract == AGENT_CONTRACT_VERSION

    raw_features = (
        raw.get("features")
        if isinstance(raw.get("features"), dict)
        else {}
    )
    feature_status = {
        feature: (
            True
            if raw_features.get(feature) is True
            else False
            if feature in raw_features
            else None
        )
        for feature in selected_features
    }

    incompatible: list[str] = []
    unverified: list[str] = []
    if not document or not isinstance(capabilities, dict):
        unverified.append("capability_observation_missing")
    if freshness_issue:
        unverified.append(freshness_issue)
    if not outer_runtime or not raw_runtime:
        unverified.append("runtime_identity_not_advertised")
    elif not runtime_identity_match:
        incompatible.append("runtime_identity_mismatch")
    if status in UNCONFIGURED_STATUSES:
        unverified.append("provider_not_configured")
    elif status not in CONNECTED_STATUSES or not connected:
        unverified.append("provider_not_connected")
    if compatibility_status == "incompatible":
        incompatible.append("compatibility_incompatible")
    elif compatibility_status != "compatible":
        unverified.append("compatibility_not_verified")
    if reported_contract and not contract_match:
        incompatible.append("contract_version_mismatch")
    elif not reported_contract:
        unverified.append("contract_version_not_advertised")
    for feature, enabled in feature_status.items():
        if enabled is False:
            incompatible.append(f"required_feature_disabled:{feature}")
        elif enabled is None:
            unverified.append(
                f"required_feature_not_advertised:{feature}"
            )

    issues = sorted(set([*incompatible, *unverified]))
    validated = (
        not issues
        and freshness == "fresh"
        and runtime_identity_match
        and connected
        and compatibility_status == "compatible"
        and contract_match
        and all(value is True for value in feature_status.values())
    )
    if validated:
        certification_status = "validated"
    elif freshness == "stale":
        certification_status = "stale"
    elif status in UNCONFIGURED_STATUSES:
        certification_status = "not_configured"
    elif incompatible:
        certification_status = "incompatible"
    else:
        certification_status = "unverified"

    return {
        "schema_version": PROVIDER_CERTIFICATION_SCHEMA,
        "runtime": selected_runtime,
        "certification_status": certification_status,
        "validated": validated,
        "observed_at": (
            parsed_observed.isoformat()
            if parsed_observed is not None
            else None
        ),
        "ttl_seconds": selected_ttl,
        "expires_at": expires_at,
        "freshness": {
            "status": freshness,
            "age_seconds": age_seconds,
        },
        "evidence": {
            "runtime_identity_match": runtime_identity_match,
            "connected": connected,
            "runtime_status": status,
            "compatibility_status": compatibility_status,
            "native_adapter": native_adapter,
            "native_adapter_claimed": native_adapter_claimed,
            "contract": {
                "expected": AGENT_CONTRACT_VERSION,
                "reported": reported_contract or None,
                "exact_match": contract_match,
            },
            "required_features": feature_status,
        },
        "issues": issues,
        "diagnostic_reads_eligible": validated,
        "read_only_dispatch_allowed": False,
        "automatic_selection_allowed": False,
        "provider_switch_allowed": False,
        "side_effect_dispatch_allowed": False,
        "dispatch_authority": "none",
        "policy_effect": "none",
    }


def _required_runtime(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("runtime must be a string")
    selected = value.strip()
    if (
        not selected
        or len(selected) > 120
        or any(ord(character) < 32 for character in selected)
    ):
        raise ValueError("runtime must be a bounded identity")
    return selected


def _feature_names(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError(
            "required_features must be an iterable of feature names"
        )
    selected: list[str] = []
    for value in values:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 120
            or value in selected
        ):
            raise ValueError(
                "required_features must be unique bounded names"
            )
        selected.append(value)
    if not selected:
        raise ValueError("required_features cannot be empty")
    return tuple(selected)


def _ttl(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("ttl_seconds must be an integer")
    try:
        selected = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("ttl_seconds must be an integer") from exc
    if not 1 <= selected <= 86_400:
        raise ValueError("ttl_seconds must be between 1 and 86400")
    return selected


def _aware_now(value: datetime | None) -> datetime:
    selected = value or datetime.now(timezone.utc)
    if not isinstance(selected, datetime) or selected.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return selected.astimezone(timezone.utc)


def _parse_aware(value: str | datetime | None) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(
                value.replace("Z", "+00:00")
            )
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


__all__ = [
    "DEFAULT_REQUIRED_FEATURES",
    "PROVIDER_CERTIFICATION_SCHEMA",
    "certify_agent_provider",
]
