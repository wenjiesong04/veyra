"""Pure field projections used by the local Product Preview read model.

This module intentionally contains no state-store or runtime adapter access;
it keeps the service composition boundary small and makes status/readiness
formatting straightforward to test in isolation.
"""

from __future__ import annotations

import hashlib
from typing import Any


def _text(value: Any, fallback: str = "", *, limit: int = 640) -> str:
    selected = value.strip() if isinstance(value, str) else fallback
    return selected[:limit]


def bounded_strings(value: Any, *, limit: int = 12, length: int = 480) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            result.append(item.strip()[:length])
        if len(result) >= limit:
            break
    return result


def safe_records(value: Any, *, limit: int, fields: tuple[str, ...] = ("statement", "epistemic_status")) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for item in value[:limit]:
        if not isinstance(item, dict):
            continue
        row: dict[str, Any] = {}
        for field in fields:
            raw = item.get(field)
            if isinstance(raw, str) and raw.strip():
                row[field] = raw.strip()[:640]
            elif field == "epistemic_status" and raw in {"reported", "inferred"}:
                row[field] = raw
            elif field == "material" and isinstance(raw, bool):
                row[field] = raw
        if row:
            result.append(row)
    return result


def evidence_records(value: Any, *, limit: int = 16) -> list[dict[str, Any]]:
    """Project bounded source evidence without URLs or provider internals."""

    if not isinstance(value, list):
        return []
    allowed = (
        "ref",
        "source",
        "status",
        "observed_at",
        "fresh_until",
        "ttl_seconds",
        "freshness",
        "title",
        "snippet",
        "epistemic_status",
    )
    result: list[dict[str, Any]] = []
    for item in value[:limit]:
        if not isinstance(item, dict):
            continue
        row: dict[str, Any] = {}
        for field in allowed:
            raw = item.get(field)
            if field == "ref":
                projected = _safe_reference(raw)
                if projected:
                    row[field] = projected
            elif field in {"ttl_seconds"}:
                if isinstance(raw, int) and not isinstance(raw, bool) and 0 <= raw <= 604800:
                    row[field] = raw
            elif field == "epistemic_status":
                if raw in {"reported", "inferred"}:
                    row[field] = raw
            elif isinstance(raw, str) and raw.strip():
                row[field] = raw.strip()[:700 if field == "snippet" else 240]
        if row:
            result.append(row)
    return result


def _safe_reference(value: Any) -> str | None:
    """Project an evidence handle without exposing paths, secrets, or tokens."""

    if isinstance(value, dict):
        value = value.get("ref_id") or value.get("event_id") or value.get("kind")
    if not isinstance(value, str) or not value.strip():
        return None
    selected = value.strip()
    lowered = selected.lower()
    if any(marker in lowered for marker in ("/", "\\", "path", "token", "secret", "credential", "password")):
        return f"ref_{hashlib.sha256(selected.encode('utf-8')).hexdigest()[:20]}"
    return selected[:240]


def situation_projection(item: dict[str, Any], *, detail: bool) -> dict[str, Any]:
    semantic = item.get("semantic") if isinstance(item.get("semantic"), dict) else {}
    status = _text(item.get("status") or semantic.get("lifecycle"), "unknown", limit=48).lower()
    progress = semantic.get("progress") if isinstance(semantic.get("progress"), dict) else {"status": "unknown", "value": None}
    refs = item.get("evidence_refs")
    evidence_refs = []
    if isinstance(refs, list):
        for ref in refs[:24]:
            projected_ref = _safe_reference(ref)
            if projected_ref and projected_ref not in evidence_refs:
                evidence_refs.append(projected_ref)
            if len(evidence_refs) >= 12:
                break
    projected: dict[str, Any] = {
        "situation_id": str(item.get("situation_id")),
        "revision": int(item.get("observation_revision") or 1),
        "title": _text(semantic.get("title") or semantic.get("label"), "Untitled Situation", limit=240),
        "label": _text(semantic.get("label"), limit=240),
        "summary": _text(semantic.get("summary"), limit=640),
        "goal": _text(semantic.get("goal"), limit=480),
        "category": _text(semantic.get("category"), "general", limit=48),
        "status": status,
        "progress": {"status": _text(progress.get("status"), "unknown", limit=48), "value": progress.get("value") if isinstance(progress.get("value"), (int, float)) and not isinstance(progress.get("value"), bool) else None},
        "deadline_at": _text(semantic.get("deadline_at")) or None,
        "changed_at": _text(item.get("updated_at") or item.get("created_at")) or None,
        "material_change": _text(semantic.get("material_change"), limit=480),
        "unknown": bounded_strings(semantic.get("unknown"), limit=12),
        "next_observation_at": _text(semantic.get("next_observation_at")) or None,
        "next_step": _text(semantic.get("next_step"), limit=480),
        "evidence_refs": evidence_refs,
    }
    if detail:
        projected.update({
            "known": safe_records(semantic.get("known"), limit=12),
            "assumptions": safe_records(semantic.get("assumptions"), limit=8),
            "timeline": safe_records(semantic.get("timeline"), limit=24, fields=("statement", "occurred_at", "material")),
            "entities": safe_records(semantic.get("entities"), limit=8, fields=("kind", "value", "epistemic_status")),
            "evidence": evidence_records(semantic.get("evidence"), limit=16),
            "epistemic": {"next_step": _text(semantic.get("next_step_epistemic_status"), "inferred", limit=32)},
        })
    return projected


def question_projection(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "need_id": str(item.get("need_id")),
        "situation_id": str(item.get("situation_id")),
        "generation": int(item.get("generation") or 1),
        "status": _text(item.get("status"), "open", limit=32),
        "question": _text(item.get("question"), _text(item.get("blocked_judgment"), limit=480), limit=480),
        "blocked_judgment": _text(item.get("blocked_judgment"), limit=480),
        "why_now": _text(item.get("why_now"), limit=480),
        "urgency": item.get("urgency") if isinstance(item.get("urgency"), (int, float)) and not isinstance(item.get("urgency"), bool) else 0.0,
        "source": _text(item.get("evidence_kind"), "other", limit=48),
        "expires_at": _text(item.get("expires_at")) or None,
        "fallback_reaction": _text(item.get("fallback_reaction"), "wait", limit=24),
    }


def reaction_projection(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "reaction_id": str(item.get("reaction_id")),
        "situation_id": str(item.get("situation_id")),
        "need_id": item.get("information_need_id") or item.get("need_id"),
        "situation_revision": int(item.get("situation_revision") or 1),
        "category": _text(item.get("category"), "general", limit=120),
        "disposition": _text(item.get("disposition"), "silent", limit=24),
        "what_changed": _text(item.get("what_happened"), limit=1200),
        "why_relevant": _text(item.get("why_it_matters"), limit=1200),
        "why_now": _text(item.get("why_now"), limit=1000),
        "recommendation": _text(item.get("suggested_next_step"), limit=1000),
        "rank": item.get("rank") if isinstance(item.get("rank"), (int, float)) and not isinstance(item.get("rank"), bool) else 0.0,
        "reason": _text(item.get("reason"), limit=480),
        "timing": dict(item.get("timing")) if isinstance(item.get("timing"), dict) else {},
        "feedback": {"available": True},
        "authority": {"execution_allowed": False, "tool_allowed": False, "agent_allowed": False, "capability_grant_allowed": False, "route_change_allowed": False, "external_delivery_allowed": False},
    }


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
