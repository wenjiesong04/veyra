from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

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
        "evidence": evidence or {},
    }
    if next_action:
        payload["next_action"] = next_action
    return payload


def refresh_claim_status(claim: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    updated = dict(claim)
    if updated.get("status") == "conflict":
        return updated
    expires_at = _parse_iso(str(updated.get("expires_at") or ""))
    if expires_at is None:
        observed_at = str(updated.get("updated_at") or updated.get("observed_at") or utc_now_iso())
        ttl_seconds = int(updated.get("ttl_seconds") or 0)
        expires_at = _parse_iso(_expires_at(observed_at, ttl_seconds))
        updated["expires_at"] = expires_at.isoformat() if expires_at else None
    if expires_at and (now or datetime.now(timezone.utc)) > expires_at:
        updated["status"] = "stale"
        updated["next_action"] = "refresh_probe"
    else:
        updated.setdefault("status", "fresh")
    return updated


def detect_conflicts(claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest_by_key: dict[str, dict[str, Any]] = {}
    for claim in claims:
        key = str(claim.get("key") or claim.get("claim") or "")
        if not key:
            continue
        current = latest_by_key.get(key)
        if current is None or str(claim.get("updated_at", "")) >= str(current.get("updated_at", "")):
            latest_by_key[key] = claim

    result: list[dict[str, Any]] = []
    for claim in claims:
        key = str(claim.get("key") or claim.get("claim") or "")
        latest = latest_by_key.get(key)
        updated = dict(claim)
        if latest and _claim_value(updated) != _claim_value(latest) and updated.get("status") != "stale":
            updated["status"] = "conflict"
            updated["next_action"] = "refresh_probe"
            updated["conflicts_with"] = latest.get("updated_at")
        result.append(updated)
    return result


def _claim_value(claim: dict[str, Any]) -> Any:
    evidence = claim.get("evidence")
    if isinstance(evidence, dict):
        for field in ("status", "value", "dirty", "exists"):
            if field in evidence:
                return evidence[field]
    return claim.get("claim")


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
