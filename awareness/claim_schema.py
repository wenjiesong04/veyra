from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from core.context_scope import (
    OPERATOR_GLOBAL_SCOPE,
    TENANT_SCOPE,
    is_operator_global_probe,
    owner_scope,
    scope_kind,
)
from awareness.refresh_spec import validate_refresh_spec
from interface.event_schema import utc_now_iso


def make_claim(
    *,
    key: str,
    claim: str,
    source: str,
    confidence: float,
    ttl_seconds: int,
    evidence: dict[str, Any] | None = None,
    observed_at: str | None = None,
    status: str = "fresh",
    next_action: str | None = None,
    source_trust: float | None = None,
    claim_kind: str = "observed",
    derived_from: str | None = None,
    refresh_spec: dict[str, Any] | None = None,
) -> dict[str, Any]:
    timestamp = observed_at or utc_now_iso()
    payload: dict[str, Any] = {
        "key": key,
        "claim": claim,
        "confidence": max(0.0, min(1.0, confidence)),
        "source": source,
        "observed_at": timestamp,
        "updated_at": timestamp,
        "ttl_seconds": ttl_seconds,
        "expires_at": _expires_at(timestamp, ttl_seconds),
        "status": status,
        "claim_kind": claim_kind if claim_kind in {"observed", "derived"} else "observed",
        "source_trust": _source_trust(source) if source_trust is None else max(0.0, min(1.0, source_trust)),
        "evidence": evidence or {},
    }
    if derived_from:
        payload["derived_from"] = derived_from
    if refresh_spec is not None:
        payload["refresh_spec"] = validate_refresh_spec(
            refresh_spec,
            source=source,
        )
    if next_action:
        payload["next_action"] = next_action
    return payload


def refresh_claim_status(claim: dict[str, Any], now: datetime | None = None, *, expire_after_seconds: int | None = None) -> dict[str, Any]:
    updated = dict(claim)
    current_time = now or datetime.now(timezone.utc)
    expires_at = _parse_iso(str(updated.get("expires_at") or ""))
    if expires_at is None:
        observed_at = str(updated.get("updated_at") or updated.get("observed_at") or utc_now_iso())
        ttl_seconds = int(updated.get("ttl_seconds") or 0)
        expires_at = _parse_iso(_expires_at(observed_at, ttl_seconds))
        updated["expires_at"] = expires_at.isoformat() if expires_at else None
    observed = _parse_iso(str(updated.get("observed_at") or updated.get("updated_at") or "")) or current_time
    updated["age_seconds"] = max(0, int((current_time - observed).total_seconds()))
    if expires_at:
        updated["ttl_remaining_seconds"] = int((expires_at - current_time).total_seconds())
    updated.setdefault("source_trust", _source_trust(str(updated.get("source") or "")))
    if updated.get("status") == "conflict":
        updated.setdefault("next_action", "refresh_probe")
        return updated
    if expires_at and current_time > expires_at:
        updated["status"] = "stale"
        updated["next_action"] = "refresh_probe"
        updated.setdefault("stale_since", expires_at.isoformat())
        if expire_after_seconds is not None and (current_time - expires_at).total_seconds() > expire_after_seconds:
            updated["status"] = "expired"
            updated["next_action"] = "refresh_probe"
    else:
        updated.setdefault("status", "fresh")
    return updated


def detect_conflicts(claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    for claim in claims:
        updated = dict(claim)
        if "conflicts_with" in updated:
            updated.pop("conflicts_with", None)
            if updated.get("status") == "conflict":
                updated["status"] = "fresh"
                if updated.get("next_action") == "refresh_probe":
                    updated.pop("next_action", None)
                updated = refresh_claim_status(updated)
        prepared.append(updated)

    latest_by_key: dict[tuple[str, ...], dict[str, Any]] = {}
    for claim in prepared:
        identity = claim_identity_key(claim)
        if not identity:
            continue
        current = latest_by_key.get(identity)
        if current is None or str(claim.get("updated_at", "")) >= str(current.get("updated_at", "")):
            latest_by_key[identity] = claim

    result: list[dict[str, Any]] = []
    for claim in prepared:
        identity = claim_identity_key(claim)
        latest = latest_by_key.get(identity)
        updated = dict(claim)
        if latest and _claim_value(updated) != _claim_value(latest) and updated.get("status") != "stale":
            updated["status"] = "conflict"
            updated["next_action"] = "refresh_probe"
            updated["conflicts_with"] = latest.get("updated_at")
        result.append(updated)
    return result


def claim_identity_key(
    claim: dict[str, Any],
) -> tuple[str, ...] | None:
    """Bind Belief replacement and conflict detection to visibility scope."""

    key = str(claim.get("key") or claim.get("claim") or "").strip()
    if not key:
        return None
    owner_state, user_id, session_id = owner_scope(claim)
    kind = scope_kind(claim)
    if owner_state == "exact" and kind not in {
        "invalid",
        OPERATOR_GLOBAL_SCOPE,
    }:
        return ("tenant", user_id, session_id, key)
    if (
        owner_state == "ownerless"
        and kind != TENANT_SCOPE
        and (
            kind == OPERATOR_GLOBAL_SCOPE
            or is_operator_global_probe(claim)
        )
    ):
        return ("operator_global", key)
    if (
        owner_state == "invalid"
        or kind == "invalid"
        or kind == TENANT_SCOPE
        or bool(claim.get("tenant_derived"))
    ):
        return None
    return ("legacy_ownerless", key)


def _claim_value(claim: dict[str, Any]) -> Any:
    evidence = claim.get("evidence")
    if isinstance(evidence, dict):
        for field in ("status", "value", "dirty", "exists"):
            if field in evidence:
                return evidence[field]
    return claim.get("claim")


def _source_trust(source: str) -> float:
    lowered = source.lower()
    if lowered.startswith("core_model"):
        return 0.55
    if lowered in {"event", "user"} or lowered.startswith("channel"):
        return 0.7
    if "probe" in lowered:
        return 0.82
    if "agent" in lowered or "openclaw" in lowered or "hermes" in lowered:
        return 0.75
    return 0.6


def _expires_at(timestamp: str, ttl_seconds: int) -> str:
    parsed = _parse_iso(timestamp) or datetime.now(timezone.utc)
    return (parsed + timedelta(seconds=ttl_seconds)).isoformat()


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed
