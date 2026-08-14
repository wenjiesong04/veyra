"""Pure field projections used by the local Product Preview read model.

This module intentionally contains no state-store or runtime adapter access;
it keeps the service composition boundary small and makes status/readiness
formatting straightforward to test in isolation.
"""

from __future__ import annotations

from typing import Any


def build_projection(value: dict[str, Any]) -> dict[str, Any]:
    raw_status = str(value.get("status") or "")
    return {
        "status": raw_status if raw_status in {"available", "unavailable", "unknown"} else "available" if value and not raw_status else "unknown",
        "revision": value.get("revision") or value.get("runtime_build") or value.get("build_revision") or None,
        "startup_dirty": (
            value.get("startup_dirty")
            if isinstance(value.get("startup_dirty"), bool)
            else value.get("dirty_flag")
            if isinstance(value.get("dirty_flag"), bool)
            else None
        ),
    }


def selected_runtime_projection(value: dict[str, Any]) -> dict[str, Any]:
    runtimes = value.get("runtimes")
    if not isinstance(runtimes, list):
        return value
    selected_runtime = str(value.get("selected_runtime") or "")
    rows = [item for item in runtimes if isinstance(item, dict)]
    selected = next((item for item in rows if item.get("operator_selected") is True), None)
    if selected is None and selected_runtime:
        selected = next((item for item in rows if str(item.get("runtime") or "") == selected_runtime), None)
    return selected or {"status": "unknown", "connected": False}


def readiness_projection(value: dict[str, Any]) -> dict[str, Any]:
    value = selected_runtime_projection(value)
    raw = str(value.get("status") or "unknown")
    explicit_configured = value.get("configured")
    if raw in {"not_configured", "unknown", "unavailable", "cached_unavailable"}:
        configured = False
    elif isinstance(explicit_configured, bool):
        configured = explicit_configured
    else:
        configured = bool(value.get("base_url")) or raw in {"configured", "ready", "available", "connected", "success"}
    return {
        "status": raw,
        "configured": configured,
        "connected": bool(value.get("connected")),
        "evidence_level": "live" if bool(value.get("connected")) else "configured" if configured else "pending",
    }


def cognition_projection(value: dict[str, Any]) -> dict[str, Any]:
    raw = str(value.get("status") or "unknown")
    return {
        "status": raw,
        "evidence_level": "automated" if raw in {"success", "observed", "validated"} else "validation_pending",
        "record_only": True,
    }


def configuration_label(agent: dict[str, Any], integration: dict[str, Any]) -> str:
    if any(str(value.get("status") or "") in {"configured", "connected", "success", "validated"} for value in (agent, integration)):
        return "configured"
    return "configuration_pending"


def section_status(source_status: Any, items: Any) -> str:
    """Keep source failures visible even when their item list is empty."""

    status = str(source_status or "").strip().lower()
    if status in {"fail_closed", "degraded", "unavailable", "error", "ambiguous", "needs_session_link", "unsupported"}:
        return status
    values = items if isinstance(items, list) else []
    return "success" if values else "empty"


def matters_status(today_status: Any, statuses: Any) -> str:
    top = str(today_status or "fail_closed")
    if top in {"ambiguous", "needs_session_link", "fail_closed"}:
        return top
    values = statuses.values() if isinstance(statuses, dict) else []
    if any(str(value) in {"fail_closed", "degraded", "unavailable", "error"} for value in values):
        return "degraded"
    return top


def section_projection(items: Any, *, status: str | None = None) -> dict[str, Any]:
    """Return the stable product section envelope without leaking raw state."""

    values = items if isinstance(items, list) else []
    return {
        "status": status or ("success" if values else "empty"),
        "count": len(values),
        "items": values,
    }


def blocked_today_projection(
    scope: dict[str, str],
    *,
    status: str,
    reason: str,
    freshness: dict[str, str],
    authority: dict[str, bool],
) -> dict[str, Any]:
    """Build a fail-closed Today envelope for an unavailable exact scope."""

    return {
        "schema_version": "veyra.product_today.v1",
        "status": status,
        "reason": reason,
        "scope": scope,
        "goal": None,
        "situations": [],
        "attention": [],
        "suggestions": [],
        "questions": {"status": "unsupported", "items": []},
        "waiting": [],
        "section_statuses": {
            "situations": status,
            "attention": status,
            "suggestions": status,
            "questions": "unsupported",
            "waiting": status,
        },
        "freshness": freshness,
        "authority": authority,
    }
