"""Pure policy for choosing a bounded Living Context reaction.

This module only evaluates a normalised input.  Persistence, deduplication and
feedback aftereffects belong to :mod:`living_reaction_runtime`; keeping the
policy pure makes virtual-clock and adversarial tests straightforward.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from interface.living_reaction_contract import (
    REACTION_DISPOSITIONS,
    REACTION_SCHEMA,
    InformationNeedSnapshot,
    ReactionDecision,
    ReactionInput,
    SituationSnapshot,
    stable_digest,
    time_iso,
)


DEFAULT_FEEDBACK_POLICY: dict[str, Any] = {
    "cooldown_seconds": 3600,
    "rank_multiplier": 1.0,
    "timing_offset_seconds": 0,
    "remind_before_seconds": 86400,
    "cooldown_until": None,
    "suppression_until": None,
    "suppression_reason": "",
}

_RISK_SCORES = {
    "unknown": 0.25,
    "low": 0.2,
    "medium": 0.5,
    "high": 0.8,
    "critical": 1.0,
}

_TERMINAL_SITUATION_STATUSES = frozenset(
    {"resolved", "completed", "closed", "archived", "expired", "contradicted"}
)
_READABLE_SOURCE_CLASSES = frozenset({"calendar", "weather", "public_web"})
_USER_SOURCE_CLASSES = frozenset({"user", "user_input"})


def _number(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def _risk_score(value: Any) -> float:
    if isinstance(value, str):
        return _RISK_SCORES.get(value.strip().lower(), 0.25)
    return max(0.0, min(1.0, _number(value, 0.25)))


def _parse_optional(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        selected = value
    elif isinstance(value, str):
        try:
            selected = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if selected.tzinfo is None or selected.utcoffset() is None:
        return None
    return selected.astimezone(timezone.utc)


def _active_until(value: Any, now: datetime) -> bool:
    parsed = _parse_optional(value)
    return parsed is not None and parsed > now


def normalise_policy(value: Mapping[str, Any] | None) -> dict[str, Any]:
    selected = dict(DEFAULT_FEEDBACK_POLICY)
    if isinstance(value, Mapping):
        for key in selected:
            if key in value:
                selected[key] = value[key]
    cooldown = selected["cooldown_seconds"]
    if isinstance(cooldown, bool) or not isinstance(cooldown, (int, float)):
        cooldown = DEFAULT_FEEDBACK_POLICY["cooldown_seconds"]
    selected["cooldown_seconds"] = max(0, min(30 * 86400, int(cooldown)))
    multiplier = selected["rank_multiplier"]
    if isinstance(multiplier, bool) or not isinstance(multiplier, (int, float)):
        multiplier = 1.0
    selected["rank_multiplier"] = max(0.1, min(1.25, float(multiplier)))
    offset = selected["timing_offset_seconds"]
    if isinstance(offset, bool) or not isinstance(offset, (int, float)):
        offset = 0
    selected["timing_offset_seconds"] = max(-30 * 86400, min(30 * 86400, int(offset)))
    remind = selected["remind_before_seconds"]
    if isinstance(remind, bool) or not isinstance(remind, (int, float)):
        remind = DEFAULT_FEEDBACK_POLICY["remind_before_seconds"]
    selected["remind_before_seconds"] = max(0, min(30 * 86400, int(remind)))
    selected["suppression_reason"] = str(selected.get("suppression_reason") or "")[:160]
    revision = value.get("policy_revision", 0) if isinstance(value, Mapping) else 0
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        revision = 0
    selected["policy_revision"] = revision
    return selected


def _deadline_urgency(situation: SituationSnapshot, now: datetime) -> float:
    deadline = _parse_optional(situation.deadline_at)
    if deadline is None:
        return 0.0
    seconds = (deadline - now).total_seconds()
    if seconds <= 0:
        return 1.0
    if seconds <= 6 * 3600:
        return 0.96
    if seconds <= 24 * 3600:
        return 0.88
    if seconds <= 3 * 86400:
        return 0.76
    if seconds <= 7 * 86400:
        return 0.62
    return 0.38


def _need_open(need: InformationNeedSnapshot | None) -> bool:
    # These are the four core lifecycle states.  Legacy aliases remain
    # accepted at the reaction boundary so an older producer cannot silently
    # turn an active need into no-op silence.
    return need is not None and need.status.lower() in {
        "open",
        "asked",
        "observing",
        "waiting",
        "pending",
        "unresolved",
        "active",
    }


def _source_ready(source: str, reaction: ReactionInput) -> tuple[bool, bool]:
    if not source:
        return False, False
    available = bool(reaction.source_availability.get(source, False))
    consented = bool(reaction.consent.get(source, False))
    return available, consented


def _select_need_disposition(need: InformationNeedSnapshot, reaction: ReactionInput) -> tuple[str, bool]:
    """Choose a need action, keeping source readiness ahead of fallback policy.

    Calendar/weather/public-web are low-risk read sources when both available
    and consented.  A user source is intrinsically an ask: a user-provided
    channel is not treated as a background-readable connector.
    """

    source = need.source.strip().lower()
    available, consented = _source_ready(source, reaction)
    if source in _USER_SOURCE_CLASSES:
        return "ask", False
    if available and consented and source in _READABLE_SOURCE_CLASSES:
        return "read", True
    preferred = need.preferred_disposition
    if preferred == "read" and available and consented and source not in _USER_SOURCE_CLASSES:
        return "read", True
    if preferred in {"ask", "wait", "silent"}:
        return preferred, False
    if available and not consented:
        return "ask", False
    if need.kind in {"clarification", "question", "missing_context", "confirmation"} or need.question:
        return "ask", False
    return "wait", False


def _material_statement(situation: SituationSnapshot) -> str:
    change = situation.material_change
    return str(change.get("statement") or change.get("summary") or change.get("description") or situation.summary or situation.title).strip()


def _material_why_now(situation: SituationSnapshot, now: datetime) -> str:
    change = situation.material_change
    explicit = str(change.get("why_now") or "").strip()
    if explicit:
        return explicit
    urgency = _deadline_urgency(situation, now)
    if urgency >= 0.88:
        return "The relevant deadline is close enough that the next step may affect the outcome."
    if urgency >= 0.62:
        return "The timing has changed and is worth checking while the signal is still timely."
    if change:
        return "A material change was recorded and the current understanding should be kept aligned."
    return "There is no new material signal that needs an interruption right now."


def _need_why_now(
    need: InformationNeedSnapshot | None,
    situation: SituationSnapshot,
    now: datetime,
    disposition: str,
) -> str:
    """Explain why an open Need is worth surfacing now.

    A Need-driven disposition must never borrow the material-change fallback:
    telling the user that nothing needs an interruption while asking them a
    question contradicts the question itself.
    """

    if need is not None and need.why_now.strip():
        return need.why_now.strip()
    urgency = _deadline_urgency(situation, now)
    if urgency >= 0.88:
        return "The relevant deadline is close enough that this open question now affects the outcome."
    if urgency >= 0.62:
        return "The relevant deadline is approaching while this question is still open."
    if disposition == "read":
        return "An authorised source can answer this open question without interrupting you."
    if disposition == "wait":
        return "This question stays open until a better observation point arrives."
    return "This is the open question currently blocking the next step."


def _facts_and_inferences(situation: SituationSnapshot, need: InformationNeedSnapshot | None) -> dict[str, list[str]]:
    facts: list[str] = []
    for item in situation.known[:6]:
        if isinstance(item, str) and item.strip():
            facts.append(item.strip())
        elif isinstance(item, Mapping):
            statement = str(item.get("statement") or item.get("value") or "").strip()
            if statement:
                facts.append(statement)
    change_statement = str(situation.material_change.get("statement") or "").strip()
    if change_statement and change_statement not in facts:
        facts.append(change_statement)
    for row in situation.evidence[:4]:
        ref = str(row.get("ref") or "").strip()
        if ref:
            facts.append(f"Evidence recorded: {ref}")
    inferences: list[str] = []
    if situation.goal:
        inferences.append(f"This may matter for the goal: {situation.goal}")
    if need and need.question:
        inferences.append(f"The next useful information may be: {need.question}")
    return {"facts": facts[:8], "inferences": inferences[:8]}


def _rank(reaction: ReactionInput, need: InformationNeedSnapshot | None, disposition: str, policy: Mapping[str, Any]) -> float:
    urgency = _deadline_urgency(reaction.situation, reaction.now)
    risk = _risk_score(reaction.situation.risk)
    progress_gap = 1.0 - reaction.situation.progress if reaction.situation.progress is not None else 0.5
    need_priority = need.priority if need else 0.0
    if disposition in {"ask", "read"}:
        base = 0.45 * need_priority + 0.3 * urgency + 0.15 * risk + 0.1 * progress_gap
    elif disposition == "suggest":
        base = 0.5 * urgency + 0.3 * risk + 0.2 * progress_gap
    elif disposition == "wait":
        base = 0.2 * urgency + 0.15 * risk + 0.05 * need_priority
    else:
        base = 0.0
    return round(max(0.0, min(1.0, base * float(policy.get("rank_multiplier", 1.0)))), 4)


def _timing(policy: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    offset = int(policy.get("timing_offset_seconds", 0) or 0)
    return {
        "offset_seconds": offset,
        "remind_before_seconds": int(policy.get("remind_before_seconds", 86400) or 0),
        "effective_at": time_iso(now + timedelta(seconds=offset)),
    }


def temporal_phase(reaction: ReactionInput, policy: Mapping[str, Any]) -> dict[str, Any]:
    """Return a stable server-owned deadline phase and reminder boundary.

    ``now`` is used only to select one of a bounded number of phases.  The
    returned boundary is stable throughout that phase, so repeated scheduler
    ticks remain idempotent while crossing a reminder window produces a new
    reaction record.
    """

    deadline = _parse_optional(reaction.situation.deadline_at)
    if deadline is None:
        return {
            "phase": "no_deadline",
            "deadline_at": None,
            "trigger_at": None,
            "evaluation_boundary": "no_deadline",
        }
    remind_before = max(0, int(policy.get("remind_before_seconds", 86400) or 0))
    offset = int(policy.get("timing_offset_seconds", 0) or 0)
    trigger = deadline - timedelta(seconds=remind_before) + timedelta(seconds=offset)
    if reaction.now < trigger:
        phase = "before_window"
    elif reaction.now < deadline:
        phase = "in_window"
    else:
        phase = "overdue"
    # Do not include the current tick time: this is the stable boundary that
    # belongs in the reaction idempotency key.
    return {
        "phase": phase,
        "deadline_at": time_iso(deadline),
        "trigger_at": time_iso(trigger),
        "evaluation_boundary": f"{phase}:{time_iso(trigger)}",
    }


def decide_reaction(reaction: ReactionInput, *, feedback_policy: Mapping[str, Any] | None = None) -> ReactionDecision:
    """Choose one disposition without changing state or gaining authority."""

    situation = reaction.situation
    need = reaction.information_need
    policy = normalise_policy(feedback_policy or reaction.feedback_policy)
    temporal = temporal_phase(reaction, policy)
    suppression_until = _parse_optional(policy.get("suppression_until"))
    cooldown_until = _parse_optional(policy.get("cooldown_until"))
    suppression_active = suppression_until is not None and suppression_until > reaction.now
    cooldown_active = cooldown_until is not None and cooldown_until > reaction.now
    suppression_reason = str(policy.get("suppression_reason") or "").strip().lower()
    explicit_read_block = suppression_active and suppression_reason in {"ignore", "resolved"}

    terminal = situation.status.strip().lower() in _TERMINAL_SITUATION_STATUSES
    authorized_read = False
    if _need_open(need):
        disposition, authorized_read = _select_need_disposition(need, reaction)
    urgent_attention = (
        reaction.attention_trigger == "material_observation"
        or temporal["phase"] in {"in_window", "overdue"}
        or _risk_score(situation.risk) >= 0.8
    )

    # Read is a background information-gathering disposition. Quiet hours,
    # interruption cooldowns and suppression apply to user-facing reactions,
    # but must not prevent a consented, available read.  A trusted actionable
    # observation is the exception: it is already the user-facing signal and
    # therefore outranks the ordinary remaining Need.
    if terminal:
        disposition = "silent"
        reason = "terminal_situation"
    elif suppression_active and (urgent_attention or not authorized_read or explicit_read_block):
        disposition = "silent"
        reason = str(policy.get("suppression_reason") or "feedback_suppression")
    elif cooldown_active and (urgent_attention or not authorized_read):
        disposition = "silent"
        reason = "feedback_cooldown"
    elif reaction.quiet_hours and (urgent_attention or not authorized_read):
        disposition = "silent"
        reason = "quiet_hours"
    elif urgent_attention:
        # A trusted, actionable observation and explicit urgency outrank an
        # ordinary remaining Need.  The Need is intentionally not dismissed:
        # the next bounded evaluation can still ask/read it after this one
        # user-facing suggestion.
        disposition = "suggest"
        reason = (
            "actionable_material_observation"
            if reaction.attention_trigger == "material_observation"
            else "material_change_or_urgency"
        )
    elif authorized_read and not explicit_read_block:
        disposition = "read"
        reason = "open_information_need"
    elif _need_open(need):
        # Reuse the validated candidate selected above. A non-consented read
        # becomes an ask; an unavailable source remains a wait.
        reason = "open_information_need"
    elif temporal["phase"] == "before_window" and situation.deadline_at:
        disposition = "wait"
        reason = "deadline_outside_reminder_window"
    elif bool(situation.material_change) and not _need_open(need):
        disposition = "suggest"
        reason = "material_change_or_urgency"
    elif situation.status.lower() in {"active", "open", "in_progress"} and situation.unknown:
        disposition = "wait"
        reason = "waiting_for_a_better_observation_point"
    else:
        disposition = "silent"
        reason = "no_actionable_change"

    if disposition == "read" and not need:
        disposition = "wait"
        reason = "missing_information_need"

    statement = _material_statement(situation)
    why_matters = situation.goal or situation.summary or "This is part of an active Situation."
    if disposition in {"ask", "read", "wait"} and _need_open(need):
        why_now = _need_why_now(need, situation, reaction.now, disposition)
    else:
        why_now = _material_why_now(situation, reaction.now)
    if disposition == "ask":
        what_happened = f"I still need one piece of information about {situation.title or 'this Situation'}."
        suggested = need.question if need and need.question else "Please fill the most important unknown when convenient."
    elif disposition == "read":
        what_happened = f"The Situation has a pending information need from {need.source}." if need else statement
        suggested = f"Read the authorised {need.source} source for: {need.question}" if need else "Read the authorised source and update the Situation."
    elif disposition == "wait":
        what_happened = statement or "The Situation is being watched."
        suggested = situation.next_step or "Wait for the next relevant observation."
    elif disposition == "suggest":
        what_happened = statement or "The Situation changed."
        suggested = situation.next_step or (need.question if need else "Review the next small step while the signal is timely.")
    else:
        what_happened = statement if reason not in {"no_actionable_change", "quiet_hours"} else "No interruption is warranted right now."
        suggested = ""

    cooldown_seconds = int(policy.get("cooldown_seconds", 0) or 0)
    if cooldown_until is None and cooldown_seconds > 0 and disposition != "silent":
        cooldown_until = reaction.now + timedelta(seconds=cooldown_seconds)
    authority = {"execution": False, "external_delivery": False, "permission_expansion": False}
    return ReactionDecision(
        reaction_id="react_" + stable_digest("veyra.living_reaction.id.v1", reaction.idempotency_key)[:24],
        idempotency_key=reaction.idempotency_key,
        owner_id=reaction.owner_id,
        session_id=reaction.session_id,
        situation_id=situation.situation_id,
        situation_revision=situation.revision,
        category=situation.category,
        disposition=disposition,  # type: ignore[arg-type]
        reason=reason,
        what_happened=what_happened[:1600],
        why_it_matters=why_matters[:1200],
        why_now=why_now[:1000],
        suggested_next_step=suggested[:1000],
        fact_vs_inference=_facts_and_inferences(situation, need),
        rank=_rank(reaction, need, disposition, policy),
        cooldown={
            "active": cooldown_active,
            "applied": cooldown_active and not (authorized_read and not explicit_read_block) and not terminal,
            "bypassed_for_background_read": authorized_read and not explicit_read_block,
            "until": time_iso(cooldown_until) if cooldown_until else None,
            "seconds": cooldown_seconds,
        },
        suppression={
            "active": suppression_active,
            "applied": suppression_active and not (authorized_read and not explicit_read_block) and not terminal,
            "bypassed_for_background_read": authorized_read and not explicit_read_block,
            "until": time_iso(suppression_until) if suppression_until else None,
            "reason": str(policy.get("suppression_reason") or ""),
        },
        timing={
            **_timing(policy, reaction.now),
            **temporal,
        },
        information_need_id=need.need_id if need else None,
        created_at=time_iso(reaction.now),
        authority=authority,
        external_delivery=False,
        record_only=True,
        schema_version=REACTION_SCHEMA,
    )


def apply_feedback_effect(
    current: Mapping[str, Any] | None,
    label: str,
    *,
    now: datetime,
    remind_before_seconds: int | None = None,
) -> dict[str, Any]:
    """Return a bounded timing/ranking effect for explicit user feedback."""

    policy = normalise_policy(current)
    durations = {
        "ignore": (7 * 86400, 0.35, 0),
        "resolved": (30 * 86400, 0.25, 0),
        "useful": (2 * 3600, 1.1, 0),
        "not_useful": (24 * 3600, 0.6, 0),
        "too_early": (12 * 3600, 0.8, 6 * 3600),
        "too_late": (3600, 0.95, -6 * 3600),
        "too_frequent": (7 * 86400, 0.4, 0),
        # A timing correction should not hide the newly timely boundary behind
        # an unrelated interruption cooldown.
        "remind_before": (0, 1.0, 0),
    }
    if label not in durations:
        raise ValueError("unsupported reaction feedback label")
    cooldown_seconds, multiplier, offset = durations[label]
    if label == "remind_before":
        policy["cooldown_seconds"] = 0
    else:
        policy["cooldown_seconds"] = max(int(policy.get("cooldown_seconds", 0) or 0), cooldown_seconds)
    policy["rank_multiplier"] = max(0.1, min(1.25, float(policy.get("rank_multiplier", 1.0)) * multiplier))
    policy["timing_offset_seconds"] = max(-30 * 86400, min(30 * 86400, int(policy.get("timing_offset_seconds", 0) or 0) + offset))
    if label == "remind_before":
        policy["remind_before_seconds"] = max(0, min(30 * 86400, int(remind_before_seconds if remind_before_seconds is not None else 86400)))
    cooldown_at = now + timedelta(seconds=policy["cooldown_seconds"])
    policy["cooldown_until"] = time_iso(cooldown_at)
    if label in {"ignore", "resolved", "too_frequent"}:
        policy["suppression_until"] = time_iso(now + timedelta(seconds=max(policy["cooldown_seconds"], durations[label][0])))
        policy["suppression_reason"] = label
    else:
        policy.setdefault("suppression_until", None)
        policy.setdefault("suppression_reason", "")
    policy["last_feedback_label"] = label
    policy["updated_at"] = time_iso(now)
    return policy
