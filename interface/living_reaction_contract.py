"""Strict, provider-neutral contracts for Veyra's living-context reactions.

The reaction layer is intentionally a projection over an already-owned
Situation and InformationNeed.  It does not create facts, grant authority, or
deliver messages.  Model/producer adapters may supply mappings, but the
runtime normalises them here before policy evaluation.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Mapping


ReactionDisposition = Literal["ask", "read", "wait", "silent", "suggest"]
FeedbackLabel = Literal[
    "ignore",
    "resolved",
    "useful",
    "not_useful",
    "too_early",
    "too_late",
    "too_frequent",
    "remind_before",
]

REACTION_DISPOSITIONS = frozenset({"ask", "read", "wait", "silent", "suggest"})
FEEDBACK_LABELS = frozenset(
    {
        "ignore",
        "resolved",
        "useful",
        "not_useful",
        "too_early",
        "too_late",
        "too_frequent",
        "remind_before",
    }
)
# ``remind_offset`` is the product/API spelling used by some callers; the
# durable ledger keeps one canonical semantic label so replay and policy
# revisions remain deterministic.
FEEDBACK_LABEL_ALIASES = {"remind_offset": "remind_before"}
REACTION_SCHEMA = "veyra.living_reaction.v1"
FEEDBACK_SCHEMA = "veyra.living_reaction_feedback.v1"
# This is deliberately a small server-owned seam.  A model-provided
# ``material_change`` remains an ordinary Situation field; only a trusted
# source receipt may promote a bounded observation into an interruptible
# attention trigger.
ATTENTION_TRIGGERS = frozenset({"none", "material_observation"})


class LivingReactionValidationError(ValueError):
    """A reaction input or output violates its bounded contract."""


def stable_digest(namespace: str, value: Any) -> str:
    try:
        encoded = json.dumps(
            {"namespace": namespace, "value": value},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            default=str,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise LivingReactionValidationError("value is not canonical JSON") from exc
    return hashlib.sha256(encoded).hexdigest()


def parse_time(value: Any, *, field_name: str = "time") -> datetime:
    if isinstance(value, datetime):
        selected = value
    elif isinstance(value, str) and value.strip():
        raw = value.strip().replace("Z", "+00:00")
        try:
            selected = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise LivingReactionValidationError(f"{field_name} is invalid") from exc
    else:
        raise LivingReactionValidationError(f"{field_name} is required")
    if selected.tzinfo is None or selected.utcoffset() is None:
        raise LivingReactionValidationError(f"{field_name} must include a timezone")
    return selected.astimezone(timezone.utc)


def time_iso(value: datetime) -> str:
    return parse_time(value).isoformat()


def _text(value: Any, field_name: str, *, required: bool = False, limit: int = 600) -> str:
    selected = str(value or "").strip()
    if required and not selected:
        raise LivingReactionValidationError(f"{field_name} is required")
    if len(selected) > limit:
        raise LivingReactionValidationError(f"{field_name} exceeds {limit} characters")
    return selected


def _identity(value: Any, field_name: str) -> str:
    return _text(value, field_name, required=True, limit=240)


def _revision(value: Any, field_name: str, *, default: int = 1) -> int:
    selected = default if value is None else value
    if isinstance(selected, bool) or not isinstance(selected, int) or selected < 1:
        raise LivingReactionValidationError(f"{field_name} must be a positive integer")
    return selected


def _bounded_list(value: Any, field_name: str, *, limit: int = 12) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise LivingReactionValidationError(f"{field_name} must be a list")
    if len(value) > limit:
        raise LivingReactionValidationError(f"{field_name} exceeds {limit} items")
    return list(value)


def _bounded_mapping(value: Any, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise LivingReactionValidationError(f"{field_name} must be an object")
    if len(value) > 40:
        raise LivingReactionValidationError(f"{field_name} has too many fields")
    return {str(key): item for key, item in value.items()}


def _normalise_evidence(value: Any) -> list[dict[str, Any]]:
    rows = _bounded_list(value, "evidence", limit=16)
    result: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if isinstance(row, str):
            ref = _text(row, f"evidence[{index}]", required=True, limit=240)
            result.append({"ref": ref})
        elif isinstance(row, Mapping):
            ref = _text(row.get("ref") or row.get("evidence_ref"), f"evidence[{index}].ref", required=True, limit=240)
            selected = {str(key): item for key, item in row.items() if str(key) != "ref"}
            selected["ref"] = ref
            result.append(selected)
        else:
            raise LivingReactionValidationError(f"evidence[{index}] must be an object or ref")
    return result


def _normalise_assumptions(value: Any) -> list[str]:
    """Project semantic assumption records to bounded display statements."""

    rows = _bounded_list(value, "situation.assumptions", limit=12)
    result: list[str] = []
    for index, item in enumerate(rows):
        if isinstance(item, Mapping):
            item = item.get("statement") or item.get("summary") or item.get("description")
        statement = _text(item, f"situation.assumptions[{index}]", required=True, limit=300)
        result.append(statement)
    return result


@dataclass(frozen=True, slots=True)
class SituationSnapshot:
    owner_id: str
    session_id: str
    situation_id: str
    revision: int
    category: str
    status: str
    title: str
    goal: str
    summary: str
    known: list[Any] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    material_change: dict[str, Any] = field(default_factory=dict)
    deadline_at: str | None = None
    progress: float | None = None
    risk: str | float | int | None = None
    next_step: str = ""

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        owner_id: str | None = None,
        session_id: str | None = None,
    ) -> "SituationSnapshot":
        if not isinstance(value, Mapping):
            raise LivingReactionValidationError("situation must be an object")
        semantic = value.get("semantic")
        semantic_map = dict(semantic) if isinstance(semantic, Mapping) else {}
        merged = {**semantic_map, **{str(key): item for key, item in value.items() if item is not None}}
        selected_owner = _identity(merged.get("owner_id") or merged.get("user_id") or owner_id, "situation.owner_id")
        selected_session = _identity(merged.get("session_id") or session_id, "situation.session_id")
        if owner_id is not None and selected_owner != _identity(owner_id, "owner_id"):
            raise LivingReactionValidationError("situation owner does not match reaction owner")
        if session_id is not None and selected_session != _identity(session_id, "session_id"):
            raise LivingReactionValidationError("situation session does not match reaction session")
        raw_change = merged.get("material_change")
        if raw_change is None:
            raw_change = merged.get("latest_change")
        if isinstance(raw_change, str):
            material_change = (
                {"statement": raw_change.strip()[:800]}
                if raw_change.strip()
                else {}
            )
        else:
            material_change = _bounded_mapping(raw_change, "situation.material_change")
        if material_change:
            material_change.setdefault("revision", 1)
            material_change["revision"] = _revision(material_change.get("revision"), "material_change.revision")
        progress = merged.get("progress")
        if isinstance(progress, Mapping):
            progress = progress.get("value")
        if progress is not None:
            if isinstance(progress, bool) or not isinstance(progress, (int, float)) or not 0 <= float(progress) <= 1:
                raise LivingReactionValidationError("situation.progress must be between 0 and 1")
            progress = float(progress)
        deadline = merged.get("deadline_at") or merged.get("due_at") or merged.get("next_observation_at")
        if deadline is not None:
            deadline = time_iso(parse_time(deadline, field_name="situation.deadline_at"))
        return cls(
            owner_id=selected_owner,
            session_id=selected_session,
            situation_id=_identity(merged.get("situation_id"), "situation.situation_id"),
            revision=_revision(merged.get("revision") or merged.get("observation_revision"), "situation.revision"),
            category=_text(merged.get("category") or merged.get("kind") or "general", "situation.category", limit=120),
            status=_text(merged.get("status") or merged.get("lifecycle") or "active", "situation.status", limit=80),
            title=_text(merged.get("title") or merged.get("name"), "situation.title", limit=240),
            goal=_text(merged.get("goal") or merged.get("objective"), "situation.goal", limit=600),
            summary=_text(merged.get("summary") or merged.get("description"), "situation.summary", limit=800),
            known=_bounded_list(merged.get("known"), "situation.known"),
            unknown=[_text(item, "situation.unknown", required=True, limit=300) for item in _bounded_list(merged.get("unknown"), "situation.unknown")],
            assumptions=_normalise_assumptions(merged.get("assumptions")),
            evidence=_normalise_evidence(merged.get("evidence") or merged.get("evidence_refs")),
            material_change=material_change,
            deadline_at=deadline,
            progress=progress,
            risk=merged.get("risk"),
            next_step=_text(merged.get("next_step") or merged.get("suggested_next_step"), "situation.next_step", limit=600),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "owner_id": self.owner_id,
            "session_id": self.session_id,
            "situation_id": self.situation_id,
            "revision": self.revision,
            "category": self.category,
            "status": self.status,
            "title": self.title,
            "goal": self.goal,
            "summary": self.summary,
            "known": list(self.known),
            "unknown": list(self.unknown),
            "assumptions": list(self.assumptions),
            "evidence": list(self.evidence),
            "material_change": dict(self.material_change),
            "deadline_at": self.deadline_at,
            "progress": self.progress,
            "risk": self.risk,
            "next_step": self.next_step,
        }

    @property
    def material_key(self) -> str:
        change = self.material_change
        return stable_digest(
            "veyra.living_reaction.material.v1",
            {
                "situation_id": self.situation_id,
                "revision": self.revision,
                "change_id": change.get("id") or change.get("change_id") or "",
                "change_revision": change.get("revision") or 1,
                "statement": change.get("statement") or change.get("summary") or "",
            },
        )


@dataclass(frozen=True, slots=True)
class InformationNeedSnapshot:
    need_id: str
    revision: int
    status: str
    kind: str
    question: str
    source: str
    priority: float
    preferred_disposition: str
    due_at: str | None
    evidence_refs: list[str] = field(default_factory=list)
    category: str = "general"

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "InformationNeedSnapshot | None":
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise LivingReactionValidationError("information_need must be an object")
        raw_priority = value.get("priority", value.get("urgency", value.get("expected_information_gain", 0.5)))
        if isinstance(raw_priority, str):
            raw_priority = {"low": 0.25, "medium": 0.5, "high": 0.8, "critical": 1.0}.get(raw_priority.lower(), 0.5)
        if isinstance(raw_priority, bool) or not isinstance(raw_priority, (int, float)):
            raise LivingReactionValidationError("information_need.priority must be numeric")
        priority = max(0.0, min(1.0, float(raw_priority)))
        due = value.get("due_at") or value.get("deadline_at") or value.get("expires_at")
        if due is not None:
            due = time_iso(parse_time(due, field_name="information_need.due_at"))
        refs = [_text(item, "information_need.evidence_refs", required=True, limit=240) for item in _bounded_list(value.get("evidence_refs"), "information_need.evidence_refs", limit=8)]
        preferred = _text(value.get("preferred_disposition") or value.get("resolution") or value.get("fallback_reaction") or "", "information_need.preferred_disposition", limit=40).lower()
        if preferred and preferred not in REACTION_DISPOSITIONS:
            raise LivingReactionValidationError("information_need.preferred_disposition is unsupported")
        return cls(
            need_id=_identity(value.get("need_id") or value.get("id"), "information_need.need_id"),
            revision=_revision(value.get("revision") or value.get("generation"), "information_need.revision"),
            status=_text(value.get("status") or "open", "information_need.status", limit=40).lower(),
            kind=_text(value.get("kind") or value.get("evidence_kind") or "clarification", "information_need.kind", limit=80).lower(),
            question=_text(value.get("question") or value.get("prompt") or value.get("blocked_judgment") or value.get("description"), "information_need.question", limit=600),
            source=_text(value.get("source") or value.get("source_id") or value.get("evidence_kind"), "information_need.source", limit=120),
            priority=priority,
            preferred_disposition=preferred,
            due_at=due,
            evidence_refs=refs,
            category=_text(value.get("category") or "general", "information_need.category", limit=120),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "need_id": self.need_id,
            "revision": self.revision,
            "status": self.status,
            "kind": self.kind,
            "question": self.question,
            "source": self.source,
            "priority": self.priority,
            "preferred_disposition": self.preferred_disposition,
            "due_at": self.due_at,
            "evidence_refs": list(self.evidence_refs),
            "category": self.category,
        }


@dataclass(frozen=True, slots=True)
class ReactionInput:
    owner_id: str
    session_id: str
    situation: SituationSnapshot
    information_need: InformationNeedSnapshot | None
    now: datetime
    quiet_hours: bool
    consent: dict[str, bool]
    source_availability: dict[str, bool]
    attention_trigger: str = "none"
    feedback_policy: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ReactionInput":
        if not isinstance(value, Mapping):
            raise LivingReactionValidationError("reaction input must be an object")
        owner = _identity(value.get("owner_id"), "owner_id")
        session = _identity(value.get("session_id"), "session_id")
        consent = _normalise_flags(value.get("consent"), "consent")
        availability = _normalise_flags(value.get("source_availability") or value.get("sources"), "source_availability")
        quiet = value.get("quiet_hours", False)
        if not isinstance(quiet, bool):
            raise LivingReactionValidationError("quiet_hours must be boolean")
        attention_trigger = _text(value.get("attention_trigger") or "none", "attention_trigger", limit=64).lower()
        if attention_trigger not in ATTENTION_TRIGGERS:
            raise LivingReactionValidationError("attention_trigger is unsupported")
        return cls(
            owner_id=owner,
            session_id=session,
            situation=SituationSnapshot.from_mapping(value.get("situation"), owner_id=owner, session_id=session),
            information_need=InformationNeedSnapshot.from_mapping(value.get("information_need")),
            now=parse_time(value.get("now"), field_name="now"),
            quiet_hours=quiet,
            consent=consent,
            source_availability=availability,
            attention_trigger=attention_trigger,
            feedback_policy=_bounded_mapping(value.get("feedback_policy"), "feedback_policy"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "owner_id": self.owner_id,
            "session_id": self.session_id,
            "situation": self.situation.to_dict(),
            "information_need": self.information_need.to_dict() if self.information_need else None,
            "now": time_iso(self.now),
            "quiet_hours": self.quiet_hours,
            "consent": dict(self.consent),
            "source_availability": dict(self.source_availability),
            "attention_trigger": self.attention_trigger,
            "feedback_policy": dict(self.feedback_policy),
        }

    @property
    def idempotency_key(self) -> str:
        information_need = self.information_need.to_dict() if self.information_need else None
        if isinstance(information_need, dict):
            # ``open`` -> ``asked`` is a lifecycle acknowledgement performed
            # by the scheduler, not a new user-facing signal.  Keep the Need
            # identity/revision in the key while preventing a repeated tick
            # from recording the same question again.
            information_need = dict(information_need)
            information_need.pop("status", None)
        return stable_digest(
            "veyra.living_reaction.idempotency.v1",
            {
                "owner_id": self.owner_id,
                "session_id": self.session_id,
                "material_key": self.situation.material_key,
                "information_need": information_need,
                # A quiet-hour or source-consent boundary is a new evaluation
                # context, while replaying the same boundary is idempotent.
                "quiet_hours": self.quiet_hours,
                "consent": self.consent,
                "source_availability": self.source_availability,
                "attention_trigger": self.attention_trigger,
                "policy_revision": self.feedback_policy.get("policy_revision", 0),
                # The policy layer supplies a stable phase boundary (for
                # example before_window -> in_window).  Never key on now:
                # repeated ticks in one phase must replay idempotently.
                "evaluation_boundary": self.feedback_policy.get(
                    "evaluation_boundary",
                    self.feedback_policy.get("temporal_phase", ""),
                ),
            },
        )


def _normalise_flags(value: Any, field_name: str) -> dict[str, bool]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise LivingReactionValidationError(f"{field_name} must be an object")
    result: dict[str, bool] = {}
    for key, raw in value.items():
        selected = raw
        if isinstance(raw, Mapping):
            selected = raw.get("consented", raw.get("available", raw.get("enabled", raw.get("status") in {"available", "connected", "ready", "granted", "authorized", "consented", "configured"})))
        if not isinstance(selected, bool):
            raise LivingReactionValidationError(f"{field_name}.{key} must be boolean")
        result[_text(key, f"{field_name}.key", required=True, limit=120)] = selected
    return result


@dataclass(frozen=True, slots=True)
class ReactionDecision:
    reaction_id: str
    idempotency_key: str
    owner_id: str
    session_id: str
    situation_id: str
    situation_revision: int
    category: str
    disposition: ReactionDisposition
    reason: str
    what_happened: str
    why_it_matters: str
    why_now: str
    suggested_next_step: str
    fact_vs_inference: dict[str, list[str]]
    rank: float
    cooldown: dict[str, Any]
    suppression: dict[str, Any]
    timing: dict[str, Any]
    information_need_id: str | None
    created_at: str
    authority: dict[str, bool] = field(default_factory=lambda: {"execution": False, "external_delivery": False, "permission_expansion": False})
    external_delivery: bool = False
    record_only: bool = True
    ledger_status: str = "recorded"
    schema_version: str = REACTION_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "reaction_id": self.reaction_id,
            "idempotency_key": self.idempotency_key,
            "owner_id": self.owner_id,
            "session_id": self.session_id,
            "situation_id": self.situation_id,
            "situation_revision": self.situation_revision,
            "category": self.category,
            "disposition": self.disposition,
            "reason": self.reason,
            "what_happened": self.what_happened,
            "why_it_matters": self.why_it_matters,
            "why_now": self.why_now,
            "suggested_next_step": self.suggested_next_step,
            "fact_vs_inference": {"facts": list(self.fact_vs_inference.get("facts", [])), "inferences": list(self.fact_vs_inference.get("inferences", []))},
            "rank": self.rank,
            "cooldown": dict(self.cooldown),
            "suppression": dict(self.suppression),
            "timing": dict(self.timing),
            "information_need_id": self.information_need_id,
            "created_at": self.created_at,
            "authority": dict(self.authority),
            "external_delivery": self.external_delivery,
            "record_only": self.record_only,
            "ledger_status": self.ledger_status,
        }


@dataclass(frozen=True, slots=True)
class FeedbackCommand:
    feedback_id: str
    owner_id: str
    session_id: str
    situation_id: str
    reaction_id: str
    label: FeedbackLabel
    now: datetime
    category: str = ""
    remind_before_seconds: int | None = None
    evidence_refs: list[str] = field(default_factory=list)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "FeedbackCommand":
        if not isinstance(value, Mapping):
            raise LivingReactionValidationError("feedback must be an object")
        label = _text(value.get("label"), "feedback.label", required=True, limit=40).lower()
        label = FEEDBACK_LABEL_ALIASES.get(label, label)
        if label not in FEEDBACK_LABELS:
            raise LivingReactionValidationError("feedback.label is unsupported")
        remind = value.get("remind_before_seconds")
        if remind is not None:
            if isinstance(remind, bool) or not isinstance(remind, int) or not 0 <= remind <= 30 * 86400:
                raise LivingReactionValidationError("remind_before_seconds is out of bounds")
        refs = [_text(item, "feedback.evidence_refs", required=True, limit=240) for item in _bounded_list(value.get("evidence_refs"), "feedback.evidence_refs", limit=8)]
        return cls(
            feedback_id=_identity(value.get("feedback_id"), "feedback.feedback_id"),
            owner_id=_identity(value.get("owner_id"), "feedback.owner_id"),
            session_id=_identity(value.get("session_id"), "feedback.session_id"),
            situation_id=_identity(value.get("situation_id"), "feedback.situation_id"),
            reaction_id=_identity(value.get("reaction_id"), "feedback.reaction_id"),
            label=label,  # type: ignore[arg-type]
            now=parse_time(value.get("now"), field_name="feedback.now"),
            category=_text(value.get("category"), "feedback.category", limit=120),
            remind_before_seconds=remind,
            evidence_refs=refs,
        )

    def semantics(self) -> dict[str, Any]:
        return {
            "feedback_id": self.feedback_id,
            "owner_id": self.owner_id,
            "session_id": self.session_id,
            "situation_id": self.situation_id,
            "reaction_id": self.reaction_id,
            "label": self.label,
            "category": self.category,
            "remind_before_seconds": self.remind_before_seconds,
        }
