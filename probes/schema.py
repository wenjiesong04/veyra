from __future__ import annotations

from typing import Any

from interface.event_schema import utc_now_iso


def probe_payload(
    *,
    probe: str,
    status: str,
    summary: str,
    target: str | None = None,
    confidence: float = 0.85,
    ttl_seconds: int = 60,
    details: dict[str, Any] | None = None,
    claims: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    observed_at = utc_now_iso()
    payload: dict[str, Any] = {
        "probe": probe,
        "source": probe,
        "target": target,
        "status": status,
        "confidence": max(0.0, min(1.0, confidence)),
        "ttl_seconds": ttl_seconds,
        "timestamp": observed_at,
        "observed_at": observed_at,
        "summary": summary,
        "details": details or {},
        "claims": claims or [],
    }
    if details:
        payload.update(details)
    return payload
