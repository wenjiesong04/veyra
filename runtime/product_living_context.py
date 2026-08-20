"""Typed product commands over the authoritative LivingContext runtimes.

This module is deliberately a seam: it creates server-owned events and calls
runtime APIs.  It never edits a JSON document directly and never grants
execution, delivery, or source-provider authority.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Mapping

from core.world_state import StateRevisionConflictError
from interface.event_schema import EventSource, EventType, VeyraEvent
from interface.product_living_context import (
    ProductSituationCommand,
    QuestionAnswerRequest,
    QuestionTransitionRequest,
    SituationCommandRequest,
)


ALLOWED_PATCH_FIELDS = frozenset(
    {
        "title",
        "label",
        "summary",
        "goal",
        "category",
        "deadline_at",
        "progress",
        "known",
        "unknown",
        "assumptions",
        "timeline",
        "material_change",
        "next_observation_at",
        "next_step",
        "next_step_epistemic_status",
    }
)


def _required(value: Any, field: str) -> str:
    selected = str(value or "").strip()
    if not selected or len(selected) > 240:
        raise ValueError(f"{field} is required")
    return selected


def _event_id(prefix: str, value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(encoded).hexdigest()[:24]}"


def build_product_event(
    *,
    owner_id: str,
    session_id: str,
    payload: dict[str, Any],
    event_id: str | None = None,
    event_type: EventType = EventType.USER_FEEDBACK,
) -> VeyraEvent:
    owner = _required(owner_id, "owner_id")
    session = _required(session_id, "session_id")
    selected_id = event_id or _event_id("product", {"owner_id": owner, "session_id": session, "payload": payload})
    now = datetime.now(timezone.utc).isoformat()
    return VeyraEvent(
        type=event_type,
        source=EventSource(channel="api", user_id=owner, session_id=session),
        payload=copy.deepcopy(payload),
        event_id=selected_id,
        timestamp=now,
        correlation_id=selected_id,
        evidence_refs=[selected_id],
    )


def command_situation(
    living_context_runtime: Any,
    situation_id: str,
    *,
    owner_id: str,
    session_id: str,
    request: SituationCommandRequest,
) -> dict[str, Any]:
    """Apply a typed command through the public Living Context seam."""

    selected_id = _required(situation_id, "situation_id")
    owner = _required(owner_id, "owner_id")
    session = _required(session_id, "session_id")
    current = living_context_runtime.get_situation(selected_id, owner_id=owner, session_id=session)
    if current is None:
        raise KeyError(f"unknown Situation: {selected_id}")
    current_revision = int(current.get("observation_revision") or 0)
    if current_revision != request.expected_revision:
        raise StateRevisionConflictError("Situation revision does not match expected_revision")
    if not isinstance(current.get("semantic"), dict):
        raise ValueError("Situation semantic projection is malformed")
    unknown = set(request.patch) - ALLOWED_PATCH_FIELDS
    if unknown:
        raise ValueError(f"unsupported Situation patch field: {sorted(unknown)!r}")
    command: ProductSituationCommand = request.command
    payload = {
        "product_command": command,
        "situation_id": selected_id,
        "expected_revision": request.expected_revision,
        "patch": copy.deepcopy(request.patch),
        "reason": request.reason,
    }
    event = build_product_event(
        owner_id=owner,
        session_id=session,
        payload=payload,
        event_id=request.event_id or _event_id("product_command", payload),
    )
    # Core owns semantic validation and the single Situation writer.  Product
    # supplies only the server event plus typed command arguments.
    return living_context_runtime.command_situation(
        event,
        selected_id,
        owner_id=owner,
        session_id=session,
        command=command,
        expected_revision=request.expected_revision,
        patch=copy.deepcopy(request.patch),
        reason=request.reason,
    )


def answer_question(
    living_context_runtime: Any,
    need_id: str,
    *,
    owner_id: str,
    session_id: str,
    request: QuestionAnswerRequest,
) -> dict[str, Any]:
    owner = _required(owner_id, "owner_id")
    session = _required(session_id, "session_id")
    need = living_context_runtime.needs.get(need_id, owner_id=owner, session_id=session)
    if need is None:
        raise KeyError(f"unknown InformationNeed: {need_id}")
    if int(need.get("generation") or 0) != request.expected_generation:
        raise StateRevisionConflictError("InformationNeed generation does not match expected_generation")
    payload = {"product_question": "answer", "need_id": need_id, "text": request.answer}
    event = build_product_event(
        owner_id=owner,
        session_id=session,
        payload=payload,
        event_id=request.event_id or _event_id("product_answer", {**payload, "generation": request.expected_generation}),
        event_type=EventType.USER_MESSAGE,
    )
    return living_context_runtime.answer_need(event, need_id, expected_revision=request.expected_revision)


def defer_question(
    living_context_runtime: Any,
    need_id: str,
    *,
    owner_id: str,
    session_id: str,
    request: QuestionTransitionRequest,
) -> dict[str, Any]:
    """Move a need to the core's waiting state through its runtime writer."""

    owner = _required(owner_id, "owner_id")
    session = _required(session_id, "session_id")
    need = living_context_runtime.needs.get(need_id, owner_id=owner, session_id=session)
    if need is None:
        raise KeyError(f"unknown InformationNeed: {need_id}")
    if int(need.get("generation") or 0) != request.expected_generation:
        raise StateRevisionConflictError("InformationNeed generation does not match expected_generation")
    transition = getattr(living_context_runtime.needs, "mark_waiting", None)
    if not callable(transition):
        raise RuntimeError("InformationNeedRuntime mark_waiting seam unavailable")
    return {
        "status": "deferred",
        "need": transition(
            need_id,
            owner_id=owner,
            session_id=session,
            event_id=request.event_id or _event_id("product_defer", {"need_id": need_id, "generation": request.expected_generation}),
            expected_generation=request.expected_generation,
        ),
    }


def dismiss_question(
    living_context_runtime: Any,
    need_id: str,
    *,
    owner_id: str,
    session_id: str,
    request: QuestionTransitionRequest,
) -> dict[str, Any]:
    owner = _required(owner_id, "owner_id")
    session = _required(session_id, "session_id")
    need = living_context_runtime.needs.get(need_id, owner_id=owner, session_id=session)
    if need is None:
        raise KeyError(f"unknown InformationNeed: {need_id}")
    if int(need.get("generation") or 0) != request.expected_generation:
        raise StateRevisionConflictError("InformationNeed generation does not match expected_generation")
    dismiss = getattr(living_context_runtime.needs, "dismiss", None)
    if not callable(dismiss):
        raise RuntimeError("InformationNeedRuntime dismiss seam unavailable")
    try:
        persisted = dismiss(
            need_id,
            owner_id=owner,
            session_id=session,
            answered_by_event_id=request.event_id or _event_id("product_dismiss", {"need_id": need_id, "generation": request.expected_generation}),
            expected_generation=request.expected_generation,
        )
    except TypeError as exc:
        raise RuntimeError("InformationNeedRuntime dismiss expected_generation seam unavailable") from exc
    return {"status": "dismissed", "need": persisted}
