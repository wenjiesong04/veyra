"""Command and capacity seams for the Living Context facade.

The public methods remain on ``LivingContextRuntime``.  Keeping these writes
here prevents the coordinator from becoming a second God module while all
operations still use its authoritative Situation/Need writers and admission
ledger.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any, Mapping

from core.world_state import StateRevisionConflictError
from common.living_source_primitives import stable_digest
from interface.event_schema import VeyraEvent
from interface.living_context_contract import CandidateNeed, ReactionPlan, stable_evidence_target_digest
from interface.living_source_contract import SourceReceipt
from interface.living_source_payload import (
    weather_coverage_matches,
    weather_material_digest,
    weather_target,
    weather_target_digest,
)


_SOURCE_RECEIPT_PROJECTION_STATUSES = frozenset(
    {"ok", "empty", "unknown", "timeout", "unavailable", "denied", "expired", "stale", "revoked"}
)
_SOURCE_POLICY_CLASSES = {
    "user_answer": "user",
    "calendar": "calendar",
    "weather": "weather",
    "public_web": "public_web",
    "agent_research": "agent",
}


def _source_material_digest(source: str, facts: Mapping[str, Any]) -> str | None:
    """Return a provider-material digest independent of receipt identity/time."""

    if not isinstance(facts, Mapping) or not facts:
        return None
    if source == "weather":
        return weather_material_digest(facts)
    return stable_digest(
        dict(facts),
        namespace=f"veyra.living_context.{source}.material.v1",
    )


def _calendar_time(value: Any) -> datetime | None:
    """Parse a provider event time without inventing a local timezone."""

    if not isinstance(value, str) or not value.strip():
        return None
    try:
        selected = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if selected.tzinfo is None or selected.utcoffset() is None:
        return None
    return selected.astimezone(timezone.utc)


def _calendar_actionable_observations(events: list[Any]) -> list[dict[str, str]]:
    """Return only concrete schedule conflicts worth interrupting for.

    A normal Calendar ``ok`` receipt is useful evidence but not a user-facing
    change.  We promote only overlapping events or two events in different
    places with at most 90 minutes between them.  The result is bounded and
    contains no provider payload beyond the event labels/times needed for an
    explanation.
    """

    parsed: list[tuple[dict[str, Any], datetime, datetime, str, str]] = []
    for raw in events[:20]:
        if not isinstance(raw, dict):
            continue
        starts = _calendar_time(raw.get("starts_at"))
        ends = _calendar_time(raw.get("ends_at"))
        if starts is None or ends is None or ends <= starts:
            continue
        title = str(raw.get("title") or "calendar event").strip()[:160]
        location = str(raw.get("location") or "").strip()[:160]
        parsed.append((raw, starts, ends, title, location))

    observations: list[dict[str, str]] = []
    for left_index, left in enumerate(parsed):
        for right in parsed[left_index + 1 :]:
            _, left_start, left_end, left_title, left_location = left
            _, right_start, right_end, right_title, right_location = right
            if left_start <= right_start:
                earlier = (left_start, left_end, left_title, left_location)
                later = (right_start, right_end, right_title, right_location)
            else:
                earlier = (right_start, right_end, right_title, right_location)
                later = (left_start, left_end, left_title, left_location)
            overlap = earlier[0] < later[1] and later[0] < earlier[1]
            gap_seconds = max(0.0, (later[0] - earlier[1]).total_seconds())
            different_places = bool(
                earlier[3]
                and later[3]
                and earlier[3].casefold() != later[3].casefold()
            )
            if not overlap and not (different_places and gap_seconds <= 90 * 60):
                continue
            if overlap:
                statement = (
                    f"Calendar conflict: {earlier[2]} ({earlier[0].isoformat()}–{earlier[1].isoformat()}) "
                    f"overlaps {later[2]} ({later[0].isoformat()}–{later[1].isoformat()})."
                )
                kind = "overlap"
            else:
                minutes = max(1, int(round(gap_seconds / 60)))
                statement = (
                    f"Calendar travel risk: {earlier[2]} at {earlier[3]} ends {earlier[1].isoformat()}, "
                    f"then {later[2]} at {later[3]} starts {later[0].isoformat()} "
                    f"with only {minutes} minutes between them."
                )
                kind = "short_location_gap"
            observations.append({"statement": statement[:480], "kind": kind})
            if len(observations) >= 8:
                return observations
    return observations


def _is_calendar_attention_marker(value: Any) -> bool:
    return bool(
        isinstance(value, dict)
        and value.get("source") == "calendar"
        and value.get("attention_trigger") == "material_observation"
    )


def _weather_need_target(need: Mapping[str, Any]) -> tuple[dict[str, str | None] | None, str | None]:
    """Return the typed weather target and its optional authoritative digest."""

    raw_target = need.get("evidence_target")
    if not isinstance(raw_target, Mapping):
        return None, None
    target = weather_target(raw_target.get("location"), raw_target.get("target_date"))
    if target is None:
        return None, None
    explicit_digest = str(
        need.get("provider_target_digest")
        or raw_target.get("target_digest")
        or raw_target.get("digest")
        or ""
    ).strip() or None
    return target, explicit_digest or weather_target_digest(target["location"], target["target_date"])


def _authoritative_source_receipt(
    runtime: Any,
    receipt: Any,
    *,
    owner_id: str,
    session_id: str,
    expected_generation: int,
    event_id: str | None = None,
) -> tuple[SourceReceipt, dict[str, Any]]:
    """Require a receipt re-read from the server-owned source controller.

    ``apply_source_receipt`` is a semantic writer, not a parser for provider
    dictionaries.  The source runtime owns request/receipt reverse references,
    binding digest/currentness, consent redaction and source-policy admission;
    re-reading through that controller keeps this seam from accepting a
    look-alike payload supplied by a caller.
    """

    if not isinstance(receipt, SourceReceipt):
        raise PermissionError("source receipt must be a trusted SourceReceipt")
    if receipt.status not in _SOURCE_RECEIPT_PROJECTION_STATUSES:
        raise StateRevisionConflictError("pending source receipt cannot be projected")
    if receipt.user_id != owner_id or receipt.session_id != session_id:
        raise PermissionError("source receipt scope does not match event scope")

    try:
        current_need = runtime.needs.authoritative_projection(
            receipt.need_id,
            owner_id=owner_id,
            session_id=session_id,
        )
    except Exception as exc:
        raise StateRevisionConflictError("source receipt InformationNeed projection is unavailable") from exc
    if not isinstance(current_need, dict):
        raise StateRevisionConflictError("source receipt InformationNeed is unavailable")
    if int(current_need.get("generation") or 0) != int(expected_generation):
        raise StateRevisionConflictError("source receipt InformationNeed generation is stale")
    if str(current_need.get("status") or "") not in {"open", "asked", "observing", "waiting"}:
        prior = runtime.admission.get(event_id) if event_id else None
        if not (
            isinstance(prior, dict)
            and runtime.admission.phase_rank(prior.get("phase")) >= 1
            and str(current_need.get("status") or "") == "resolved"
            and str(prior.get("situation_id") or "") == str(current_need.get("situation_id") or "")
            and str(current_need.get("need_id") or "") in {
                str(key) for key in (prior.get("answered_need_generations") or {})
            }
        ):
            raise StateRevisionConflictError("source receipt InformationNeed is not active")

    source_runtime = getattr(runtime.state_store, "_living_source_runtime", None)
    from runtime.living_source_runtime import LivingSourceRuntime
    if not isinstance(source_runtime, LivingSourceRuntime):
        raise PermissionError("authoritative Living Source runtime is unavailable")
    try:
        authoritative = source_runtime.get_receipt(
            receipt.receipt_id,
            user_id=owner_id,
            session_id=session_id,
            now=runtime._clock(),
        )
    except Exception as exc:
        # A successful receipt may have already resolved its Need before the
        # final admission marker was written.  The normal source read API
        # intentionally rejects that now-terminal binding; an exact durable
        # admission may instead inspect the typed server-owned snapshot and
        # continue the same event.  No caller payload is trusted here.
        prior = runtime.admission.get(event_id) if event_id else None
        if not (
            isinstance(prior, dict)
            and runtime.admission.phase_rank(prior.get("phase")) >= 1
            and str(prior.get("situation_id") or "") == str(current_need.get("situation_id") or "")
            and str(current_need.get("need_id") or "") in {
                str(key) for key in (prior.get("answered_need_generations") or {})
            }
        ):
            raise StateRevisionConflictError("source receipt is not current authoritative state") from exc
        try:
            snapshot = source_runtime.state_snapshot()
            raw = (snapshot.get("receipts") or {}).get(receipt.receipt_id)
            authoritative = source_runtime._receipt_from_dict(raw) if isinstance(raw, dict) else None
        except Exception as snapshot_exc:
            raise StateRevisionConflictError("source receipt is not current authoritative state") from snapshot_exc
    if not isinstance(authoritative, SourceReceipt):
        raise PermissionError("source receipt is not present in authoritative source state")
    if authoritative.to_dict() != receipt.to_dict():
        raise StateRevisionConflictError("source receipt does not match authoritative receipt")

    try:
        binding = source_runtime.get_binding(
            receipt.need_id,
            receipt.source,
            user_id=owner_id,
            session_id=session_id,
            binding_id=receipt.binding_id,
        )
    except Exception as exc:
        binding = None
        if isinstance(prior if "prior" in locals() else None, dict) and runtime.admission.phase_rank(prior.get("phase")) >= 1:
            try:
                raw_binding = (source_runtime.state_snapshot().get("bindings") or {}).get(receipt.binding_id)
                binding = source_runtime._binding_from_dict(raw_binding) if isinstance(raw_binding, dict) else None
            except Exception:
                binding = None
        if binding is None:
            raise StateRevisionConflictError("source receipt binding is not current") from exc
    if binding is None and isinstance(prior if "prior" in locals() else None, dict) and runtime.admission.phase_rank(prior.get("phase")) >= 1:
        try:
            raw_binding = (source_runtime.state_snapshot().get("bindings") or {}).get(receipt.binding_id)
            binding = source_runtime._binding_from_dict(raw_binding) if isinstance(raw_binding, dict) else None
        except Exception:
            binding = None
    if binding is None:
        raise StateRevisionConflictError("source receipt binding is unavailable")
    binding_digest_matches = binding.record_digest == str(current_need.get("record_digest") or "")
    recovery_binding = (
        isinstance(prior if "prior" in locals() else None, dict)
        and runtime.admission.phase_rank(prior.get("phase")) >= 1
        and str(prior.get("situation_id") or "") == str(current_need.get("situation_id") or "")
        and str(current_need.get("need_id") or "") in {
            str(key) for key in (prior.get("answered_need_generations") or {})
        }
    )
    if (
        binding.need_id != receipt.need_id
        or binding.source != receipt.source
        or binding.scope != (owner_id, session_id)
        or binding.need_revision != int(current_need.get("generation") or 0)
        or (not binding_digest_matches and not recovery_binding)
    ):
        raise StateRevisionConflictError("source receipt binding is stale or mismatched")
    allowed = {str(item) for item in current_need.get("allowed_source_classes") or []}
    policy_class = _SOURCE_POLICY_CLASSES.get(receipt.source)
    if receipt.source not in allowed and policy_class not in allowed:
        raise PermissionError("source receipt source is not allowed by the current InformationNeed")
    return authoritative, current_need


def answer_need(
    runtime: Any,
    event: VeyraEvent,
    need_id: str,
    *,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    """Record a user's answer as reported evidence and resolve its Need."""

    owner_id = str(event.source.user_id or "").strip() or "local-user"
    session_id = str(event.source.session_id or "").strip() or "local-session"
    need = runtime.needs.get(need_id, owner_id=owner_id, session_id=session_id)
    if need is None:
        raise KeyError(f"unknown InformationNeed: {need_id}")
    situation = runtime.situations.get_semantic(
        str(need["situation_id"]),
        user_id=owner_id,
        session_id=session_id,
    )
    if situation is None:
        raise KeyError("InformationNeed points to an unavailable Situation")
    prior_admission = runtime.admission.get(event.event_id)
    if prior_admission is not None and str(prior_admission.get("phase") or "") == "committed":
        answered = prior_admission.get("answered_need_generations")
        if not isinstance(answered, dict) or str(need_id) not in answered:
            raise StateRevisionConflictError("same answer event is bound to a different InformationNeed")
        return {
            "status": "answered",
            "replayed": True,
            "situation": situation,
            "need": need,
        }
    current = copy.deepcopy(situation.get("semantic") or {})
    text = runtime._event_text(event)
    known = list(current.get("known") or [])
    known.append(
        {
            "statement": text,
            "epistemic_status": "reported",
            "source_event_id": event.event_id,
            "recorded_at": event.timestamp,
        }
    )
    blocked = str(need.get("blocked_judgment") or "")
    unknown_binding = str(need.get("unknown_binding") or "").strip() or None
    unknown = [str(item) for item in current.get("unknown") or [] if str(item).strip()]
    if unknown_binding is None:
        # Legacy rows predate the server-owned Need -> unknown binding.  A
        # blocked judgment is not proof of the semantic endpoint: the user
        # answer can resolve the Need while preserving a paraphrased unknown
        # rather than deleting unrelated context.
        binding_status = "unbound"
    elif unknown_binding not in unknown:
        binding_status = "missing"
    else:
        unknown = [item for item in unknown if item != unknown_binding]
        binding_status = "cleared"
    current["unknown"] = unknown
    current["known"] = dedupe_records(known, key="statement", limit=12)
    timeline = list(current.get("timeline") or [])
    timeline.append(
        {
            "statement": text,
            "occurred_at": event.occurred_at,
            "source_event_id": event.event_id,
            "recorded_at": event.timestamp,
            "material": True,
        }
    )
    current["timeline"] = dedupe_records(timeline, key="source_event_id", limit=24)
    current["material_change"] = f"用户补充了与 {blocked} 相关的信息"[:480]
    with runtime.state_store.writer_transaction():
        # Revalidate both authoritative rows after taking the shared writer
        # fence.  Without this preflight, a concurrent Need transition could
        # let the Situation commit first and leave answer/resolve split across
        # files.  A failed preflight is side-effect free; a replay of a
        # prepared admission can then safely repair the same event.
        prior_phase_rank = (
            runtime.admission.phase_rank(prior_admission.get("phase"))
            if isinstance(prior_admission, dict)
            else 0
        )
        fresh_need = runtime.needs.get(need_id, owner_id=owner_id, session_id=session_id)
        if fresh_need is None or int(fresh_need.get("generation") or 0) != int(need.get("generation") or 0):
            raise StateRevisionConflictError("InformationNeed changed before answer admission")
        if str(fresh_need.get("status") or "") not in {"open", "asked", "observing", "waiting"}:
            if not (
                prior_phase_rank >= 1
                and str(fresh_need.get("status") or "") == "resolved"
                and str(fresh_need.get("answered_by_event_id") or "") == str(event.event_id)
            ):
                raise StateRevisionConflictError("InformationNeed changed before answer admission")
        fresh_situation = runtime.situations.get_semantic(
            str(situation["situation_id"]),
            user_id=owner_id,
            session_id=session_id,
        )
        if fresh_situation is None or (
            int(fresh_situation.get("observation_revision") or 0) != int(situation.get("observation_revision") or 0)
            and not (
                prior_phase_rank >= 1
                and str(fresh_situation.get("source_event_id") or "") == str(event.event_id)
            )
        ):
            raise StateRevisionConflictError("Situation changed before answer admission")
        semantic_digest = runtime.admission.digest(current)
        need_plan_digest = runtime.admission.digest([])
        admission = runtime.admission.prepare(
            event_id=event.event_id,
            owner_id=owner_id,
            session_id=session_id,
            situation_id=str(situation["situation_id"]),
            operation="answer_need",
            semantic_digest=semantic_digest,
            need_plan_digest=need_plan_digest,
            answered_need_generations={str(need_id): int(need.get("generation") or 1)},
            event_timestamp=event.timestamp,
            expected_situation_revision=(
                int(prior_admission.get("expected_situation_revision"))
                if isinstance(prior_admission, dict) and prior_admission.get("expected_situation_revision") is not None
                else int(situation.get("observation_revision") or 1)
            ),
            expected_need_state_revision=(
                int(prior_admission.get("expected_need_state_revision"))
                if isinstance(prior_admission, dict) and prior_admission.get("expected_need_state_revision") is not None
                else runtime._need_state_revision()
            ),
        )
        if admission.get("replayed"):
            persisted = runtime.situations.get_semantic(
                str(situation["situation_id"]), user_id=owner_id, session_id=session_id
            )
            resolved = runtime.needs.get(need_id, owner_id=owner_id, session_id=session_id)
            if persisted is None or resolved is None:
                raise StateRevisionConflictError("committed answer admission has no current records")
            return {
                "status": "answered",
                "replayed": True,
                "situation": persisted,
                "need": resolved,
            }
        phase_rank = runtime.admission.phase_rank(admission.get("phase"))
        fresh_situation = runtime.situations.get_semantic(
            str(situation["situation_id"]), user_id=owner_id, session_id=session_id
        )
        semantic_already_applied = runtime._semantic_event_matches(
            fresh_situation,
            event_id=event.event_id,
            semantic_digest=semantic_digest,
            expected_revision=admission.get("result_situation_revision"),
        )
        if phase_rank >= 1 and not semantic_already_applied:
            raise StateRevisionConflictError("answer admission semantic phase does not match current Situation")
        if semantic_already_applied:
            persisted = copy.deepcopy(fresh_situation)
            persisted["semantic_replayed"] = True
        else:
            persisted = runtime.situations.record_semantic(
                event,
                subject_key=str(situation.get("semantic_subject_key") or ""),
                semantic_state=current,
                situation_id=str(situation["situation_id"]),
                operation="update",
                expected_revision=(
                    expected_revision
                    if expected_revision is not None
                    else int(situation.get("observation_revision") or 1)
                ),
                evidence_refs=event.evidence_refs,
            )
            runtime.admission.mark_phase(
                event_id=event.event_id,
                phase="semantic_applied",
                semantic_digest=semantic_digest,
                need_plan_digest=need_plan_digest,
                result_situation_revision=int(persisted.get("observation_revision") or 0),
            )
        if phase_rank < 2:
            resolved = runtime.needs.resolve(
                need_id,
                owner_id=owner_id,
                session_id=session_id,
                answered_by_event_id=event.event_id,
                expected_generation=int(need.get("generation") or 1),
            )
            runtime.admission.mark_phase(
                event_id=event.event_id,
                phase="needs_applied",
                semantic_digest=semantic_digest,
                need_plan_digest=need_plan_digest,
                result_situation_revision=int(persisted.get("observation_revision") or 0),
                result_need_state_revision=runtime._need_state_revision(),
            )
        else:
            resolved = runtime.needs.get(need_id, owner_id=owner_id, session_id=session_id)
            if resolved is None or str(resolved.get("status") or "") != "resolved":
                raise StateRevisionConflictError("answer admission Need phase does not match current Need")
        runtime.admission.mark_committed(
            event_id=event.event_id,
            semantic_digest=semantic_digest,
            need_plan_digest=need_plan_digest,
        )
    return {
        "status": "answered" if binding_status == "cleared" else "degraded",
        "situation": persisted,
        "need": resolved,
        "unknown_binding": {
            "status": binding_status,
            "bound": bool(unknown_binding),
        },
    }


def apply_source_receipt(
    runtime: Any,
    event: VeyraEvent,
    receipt: Any,
    *,
    expected_generation: int,
) -> dict[str, Any]:
    """Project one current, typed source receipt into the same Situation.

    Source payloads are evidence, never a second truth store.  A successful
    receipt resolves the Need under its generation fence; an unsuccessful one
    preserves the unknown and records why observation remains incomplete.
    """

    owner_id = str(event.source.user_id or "").strip() or "local-user"
    session_id = str(event.source.session_id or "").strip() or "local-session"
    authoritative_receipt, current_need = _authoritative_source_receipt(
        runtime,
        receipt,
        owner_id=owner_id,
        session_id=session_id,
        expected_generation=expected_generation,
        event_id=event.event_id,
    )
    receipt_dict = authoritative_receipt.to_dict()
    need_id = authoritative_receipt.need_id
    need = runtime.needs.get(need_id, owner_id=owner_id, session_id=session_id)
    if need is None:
        raise KeyError("source receipt InformationNeed is unavailable")
    if (
        int(need.get("generation") or 0) != int(expected_generation)
        or str(need.get("situation_id") or "") != str(current_need.get("situation_id") or "")
    ):
        raise StateRevisionConflictError("source receipt InformationNeed generation is stale")
    situation_id = str(need.get("situation_id") or "")
    situation = runtime.situations.get_semantic(situation_id, user_id=owner_id, session_id=session_id)
    if situation is None:
        raise KeyError("source receipt Situation is unavailable")
    admission_event = runtime.admission.get(event.event_id)
    receipt_source = str(receipt_dict.get("source") or "other")
    receipt_facts = receipt_dict.get("payload", {}).get("facts", {}) if isinstance(receipt_dict.get("payload"), dict) else {}
    receipt_events = receipt_facts.get("events") if isinstance(receipt_facts, dict) and isinstance(receipt_facts.get("events"), list) else []
    calendar_observations = (
        _calendar_actionable_observations(receipt_events)
        if receipt_source == "calendar" and str(receipt_dict.get("status") or "") == "ok"
        else []
    )
    attention_trigger = "none"
    if admission_event is not None and str(admission_event.get("phase") or "") == "committed":
        if str(admission_event.get("situation_id") or "") != situation_id:
            raise StateRevisionConflictError("source receipt event is bound to another Situation")
        return {
            "status": "replayed",
            "situation": situation,
            "need": need,
            "receipt": receipt_dict,
            "attention_trigger": attention_trigger,
        }

    original_semantic = copy.deepcopy(situation.get("semantic") or {})
    current = copy.deepcopy(original_semantic)
    source = str(receipt_dict.get("source") or "other")
    receipt_status = str(receipt_dict.get("status") or "unknown")
    fresh_until = receipt_dict.get("fresh_until")
    ttl_seconds = receipt_dict.get("ttl_seconds")
    facts = receipt_dict.get("payload", {}).get("facts", {}) if isinstance(receipt_dict.get("payload"), dict) else {}
    facts = copy.deepcopy(facts) if isinstance(facts, dict) else {}
    successful = receipt_status in {"ok", "empty"}
    coverage_ok = True
    coverage_reason = ""
    if source == "weather" and successful:
        weather_target_row, expected_target_digest = _weather_need_target(need)
        if weather_target_row is not None:
            coverage_ok, coverage_reason = weather_coverage_matches(
                facts,
                location=weather_target_row.get("location"),
                target_date=weather_target_row.get("target_date"),
                expected_digest=expected_target_digest,
                observation_requirement=(
                    need.get("observation_requirement")
                    if isinstance(need.get("observation_requirement"), Mapping)
                    else None
                ),
            )
        elif need.get("evidence_target") is not None or need.get("evidence_target_digest"):
            coverage_ok, coverage_reason = False, "weather_target_invalid"
        # Weather ``empty`` means the provider did not produce a forecast
        # fact.  It is an honest receipt but cannot resolve an information
        # target.  Calendar empty (no events) may still resolve its Need.
        if receipt_status == "empty":
            coverage_ok, coverage_reason = False, "weather_observation_empty"
    resolves_need = successful and coverage_ok
    unknown_binding = str(need.get("unknown_binding") or "").strip() or None
    binding_status = "not_applicable"
    unknown = [str(item) for item in current.get("unknown") or [] if str(item).strip()]
    if resolves_need:
        if unknown_binding is None:
            # Legacy rows have no server-owned semantic endpoint. A
            # successful receipt may resolve the Need, but it cannot clear an
            # Unknown until structural migration supplies that binding.
            binding_status = "unbound"
        elif unknown_binding not in unknown:
            # The binding is authoritative, but its endpoint is no longer in
            # the current Situation projection.  Do not remove a different
            # unknown merely because the source returned successfully.
            binding_status = "missing"
        else:
            unknown = [item for item in unknown if item != unknown_binding]
            binding_status = "cleared"
    else:
        # Provider prose is not a semantic unknown.  Keep the original
        # server-owned binding stable; otherwise every retry appends another
        # spelling of the same transient failure and the UI reports a new
        # blocker forever.
        pass
    current["unknown"] = dedupe_text(unknown, limit=12)

    known = list(current.get("known") or [])
    material_digest = (
        _source_material_digest(source, facts)
        if receipt_status == "ok" and resolves_need
        else None
    )
    prior_material_digests = current.get("source_material_digests")
    if not isinstance(prior_material_digests, dict):
        prior_material_digests = {}
    weather_digest = weather_material_digest(facts) if source == "weather" and resolves_need else ""
    prior_weather_digests = current.get("source_observation_digests")
    if not isinstance(prior_weather_digests, dict):
        prior_weather_digests = {}
    weather_target_row, _ = _weather_need_target(need) if source == "weather" else (None, None)
    weather_coverage_row = facts.get("coverage") if isinstance(facts.get("coverage"), Mapping) else {}
    weather_target_key = (
        weather_target_digest(
            weather_target_row.get("location"),
            weather_target_row.get("target_date"),
        )
        if weather_target_row is not None
        else str(weather_coverage_row.get("target_digest") or "")
    )
    material_key = (
        str(need.get("evidence_target_digest") or "").strip()
        or weather_target_key
        or str(need_id)
    )
    repeated_weather_observation = bool(
        weather_digest
        and weather_target_key
        and str(prior_weather_digests.get(weather_target_key) or "") == weather_digest
    )
    observation_key = weather_target_key or str(need.get("evidence_target_digest") or "").strip()
    observation_digest = weather_digest or material_digest
    prior_observation_digest = (
        str(prior_weather_digests.get(observation_key) or "")
        if observation_key
        else ""
    )
    # The first typed observation establishes the server material checkpoint;
    # later identical receipts are audit-only. Exact digests, rather than
    # wording, define the boundary.
    material_observed = bool(observation_digest and observation_key and observation_digest != prior_observation_digest)
    # The first exact observation establishes availability and the durable
    # novelty checkpoint. It is not a change from prior evidence; only a
    # different digest after that checkpoint is material_changed.
    material_changed = bool(material_observed and prior_observation_digest)
    if observation_digest and observation_key:
        prior_weather_digests[observation_key] = observation_digest
        current["source_observation_digests"] = {
            str(key): str(value)
            for key, value in list(prior_weather_digests.items())[-8:]
            if str(key) and str(value)
        }
    if material_digest and material_key:
        prior_material_digests[f"{source}:{material_key}"] = material_digest
        current["source_material_digests"] = {
            str(key): str(value)
            for key, value in list(prior_material_digests.items())[-16:]
            if str(key) and str(value)
        }
        current["material_digest"] = material_digest
        current["material_revision"] = int(current.get("material_revision") or 0) + (1 if material_changed else 0)
    if calendar_observations and material_changed:
        attention_trigger = "material_observation"
    # Remove the previous source-owned trigger before applying the current
    # receipt.  A normal/empty/unavailable read therefore clears stale
    # Calendar attention without touching unrelated user assumptions.
    assumptions = [
        item for item in list(current.get("assumptions") or [])
        if not _is_calendar_attention_marker(item)
    ]
    source_event_id = event.event_id
    observed_at = str(receipt_dict.get("observed_at") or event.timestamp)
    if source == "user_answer":
        answer = str(facts.get("answer") or "").strip()
        if answer:
            known.append({"statement": answer[:480], "epistemic_status": "reported", "source_event_id": source_event_id, "recorded_at": observed_at})
    elif source == "weather":
        location = str(facts.get("location") or "").strip()
        current_weather = facts.get("current") if isinstance(facts.get("current"), dict) else {}
        details = ", ".join(f"{key}={value}" for key, value in list(current_weather.items())[:6])
        if resolves_need and not repeated_weather_observation and (location or details):
            forecast = facts.get("forecast") if isinstance(facts.get("forecast"), dict) else {}
            forecast_details = ", ".join(f"{key}={value}" for key, value in list(forecast.items())[:6])
            selected_details = details or forecast_details
            known.append({"statement": f"Weather observation for {location or 'the reported place'}: {selected_details}"[:480], "epistemic_status": "inferred", "source_event_id": source_event_id, "recorded_at": observed_at})
    elif source == "calendar":
        events = facts.get("events") if isinstance(facts.get("events"), list) else []
        for item in events[:6]:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "calendar event").strip()
            starts = str(item.get("starts_at") or "").strip()
            if title and starts:
                known.append({"statement": f"Calendar shows {title} at {starts}"[:480], "epistemic_status": "inferred", "source_event_id": source_event_id, "recorded_at": observed_at})
        for observation in calendar_observations:
            # The reaction boundary currently accepts legacy assumptions as
            # bounded display strings; keep the durable record concise enough
            # for that projection while the full actionable explanation stays
            # in material_change.
            assumptions.append({
                "statement": observation["statement"][:150],
                "epistemic_status": "inferred",
                "source_event_id": source_event_id,
                "recorded_at": observed_at,
                "source": "calendar",
                "attention_trigger": "material_observation",
            })
    elif source == "public_web":
        results = facts.get("results") if isinstance(facts.get("results"), list) else []
        for item in results[:5]:
            if not isinstance(item, dict):
                continue
            snippet = str(item.get("snippet") or "").strip()
            title = str(item.get("title") or "web result").strip()
            if snippet or title:
                current.setdefault("evidence", []).append({"ref": f"{source_event_id}:{len(current.get('evidence') or []) + 1}", "source": source, "title": title[:240], "snippet": snippet[:700], "epistemic_status": "inferred", "observed_at": observed_at, "fresh_until": fresh_until, "ttl_seconds": ttl_seconds})

    current["known"] = dedupe_records(known, key="statement", limit=12)
    current["assumptions"] = dedupe_records(assumptions, key="statement", limit=8)
    timeline = list(current.get("timeline") or [])
    timeline.append({"statement": f"{source} observation: {receipt_status}"[:480], "occurred_at": observed_at, "source_event_id": source_event_id, "recorded_at": observed_at, "material": bool(calendar_observations) or (source == "weather" and resolves_need and not repeated_weather_observation)})
    current["timeline"] = dedupe_records(timeline, key="source_event_id", limit=24)
    # A valid empty or failed observation resolves/updates the Need lifecycle,
    # but it is not itself a user-worthy change.  Clearing the latest material
    # marker keeps the reaction policy from manufacturing a suggest after a
    # source failure or a truthful empty result.
    # A successful source observation is not automatically a user-worthy
    # change.  Calendar is promoted only when the server-owned parser found a
    # concrete overlap or short cross-location gap; empty/unavailable and
    # ordinary Calendar reads remain quiet.
    current["material_change"] = (
        calendar_observations[0]["statement"]
        if calendar_observations and material_changed
        else (
            f"Weather observation changed for {str(facts.get('location') or 'the reported place')}"
            if source == "weather" and material_changed
            else ""
        )
    )
    if successful and isinstance(fresh_until, str) and fresh_until.strip():
        # The receipt TTL is the server-owned lower bound for the next
        # observation.  Preserve an earlier explicit schedule, but never let
        # a stale/absent schedule cause the same Need to reopen every tick.
        existing_next = current.get("next_observation_at")
        try:
            from datetime import datetime, timezone
            next_boundary = datetime.fromisoformat(fresh_until.replace("Z", "+00:00"))
            if next_boundary.tzinfo is not None and next_boundary.utcoffset() is not None:
                next_boundary = next_boundary.astimezone(timezone.utc)
            observed_boundary = None
            try:
                observed_boundary = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
                if observed_boundary.tzinfo is not None and observed_boundary.utcoffset() is not None:
                    observed_boundary = observed_boundary.astimezone(timezone.utc)
            except (TypeError, ValueError):
                observed_boundary = None
            prior_boundary = None
            if existing_next:
                try:
                    prior_boundary = datetime.fromisoformat(str(existing_next).replace("Z", "+00:00"))
                    if prior_boundary.tzinfo is not None and prior_boundary.utcoffset() is not None:
                        prior_boundary = prior_boundary.astimezone(timezone.utc)
                except (TypeError, ValueError):
                    prior_boundary = None
            if prior_boundary is None or (observed_boundary is not None and prior_boundary <= observed_boundary):
                current["next_observation_at"] = next_boundary.isoformat()
        except (TypeError, ValueError):
            pass
    evidence = list(current.get("evidence") or [])
    evidence.append({"ref": str(receipt_dict.get("receipt_id") or source_event_id), "source": source, "status": receipt_status, "observed_at": observed_at, "fresh_until": fresh_until, "ttl_seconds": ttl_seconds})
    current["evidence"] = dedupe_records(evidence, key="ref", limit=16)

    semantic_needs_write = bool(
        material_observed
        or current.get("unknown") != original_semantic.get("unknown")
    )
    if not semantic_needs_write:
        # Source runtime retains the complete receipt audit.  A duplicate or
        # failed receipt must not advance Situation/material revision or wake
        # cognition merely because its receipt ID/time differs.
        current = original_semantic

    with runtime.state_store.writer_transaction():
        semantic_digest = runtime.admission.digest(current)
        need_plan_digest = runtime.admission.digest([])
        admission = runtime.admission.prepare(
            event_id=event.event_id,
            owner_id=owner_id,
            session_id=session_id,
            situation_id=situation_id,
            operation="source_receipt",
            semantic_digest=semantic_digest,
            need_plan_digest=need_plan_digest,
            answered_need_generations={need_id: int(expected_generation)} if resolves_need else {},
            event_timestamp=event.timestamp,
            expected_situation_revision=(
                int(admission_event.get("expected_situation_revision"))
                if isinstance(admission_event, dict) and admission_event.get("expected_situation_revision") is not None
                else int(situation.get("observation_revision") or 1)
            ),
            expected_need_state_revision=(
                int(admission_event.get("expected_need_state_revision"))
                if isinstance(admission_event, dict) and admission_event.get("expected_need_state_revision") is not None
                else runtime._need_state_revision()
            ),
        )
        if admission.get("replayed"):
            persisted = runtime.situations.get_semantic(situation_id, user_id=owner_id, session_id=session_id)
            resolved = runtime.needs.get(need_id, owner_id=owner_id, session_id=session_id)
            if persisted is None or resolved is None:
                raise StateRevisionConflictError("committed source admission has no current records")
            return {
                "status": "replayed",
                "situation": persisted,
                "need": resolved,
                "receipt": receipt_dict,
                "attention_trigger": attention_trigger,
            }
        phase_rank = runtime.admission.phase_rank(admission.get("phase"))
        fresh_situation = runtime.situations.get_semantic(situation_id, user_id=owner_id, session_id=session_id)
        semantic_already_applied = runtime._semantic_event_matches(
            fresh_situation,
            event_id=event.event_id,
            semantic_digest=semantic_digest,
            expected_revision=admission.get("result_situation_revision"),
        )
        if phase_rank >= 1 and not semantic_already_applied:
            raise StateRevisionConflictError("source admission semantic phase does not match current Situation")
        if semantic_already_applied:
            persisted = copy.deepcopy(fresh_situation)
            persisted["semantic_replayed"] = True
        elif not semantic_needs_write:
            persisted = copy.deepcopy(fresh_situation)
            runtime.admission.mark_phase(
                event_id=event.event_id,
                phase="semantic_applied",
                semantic_digest=semantic_digest,
                need_plan_digest=need_plan_digest,
                result_situation_revision=int(persisted.get("observation_revision") or 0),
            )
        else:
            persisted = runtime.situations.record_semantic(
                event,
                subject_key=str(situation.get("semantic_subject_key") or ""),
                semantic_state=current,
                situation_id=situation_id,
                operation="update",
                expected_revision=int(situation.get("observation_revision") or 1),
                evidence_refs=[str(receipt_dict.get("receipt_id") or event.event_id)],
                server_command=True,
            )
            runtime.admission.mark_phase(
                event_id=event.event_id,
                phase="semantic_applied",
                semantic_digest=semantic_digest,
                need_plan_digest=need_plan_digest,
                result_situation_revision=int(persisted.get("observation_revision") or 0),
            )
        if phase_rank < 2:
            if resolves_need:
                resolved = runtime.needs.resolve(
                    need_id,
                    owner_id=owner_id,
                    session_id=session_id,
                    answered_by_event_id=event.event_id,
                    expected_generation=int(expected_generation),
                )
            else:
                # A failed/empty receipt is source audit, not a semantic
                # lifecycle change. Keep the Need's current status so the
                # failed attempt cannot itself create a cognitive wake-up;
                # the scheduler/source policy may retry it later.
                resolved = runtime.needs.get(
                    need_id,
                    owner_id=owner_id,
                    session_id=session_id,
                )
                if resolved is None:
                    raise StateRevisionConflictError("source receipt Need disappeared before retry")
            runtime.admission.mark_phase(
                event_id=event.event_id,
                phase="needs_applied",
                semantic_digest=semantic_digest,
                need_plan_digest=need_plan_digest,
                result_situation_revision=int(persisted.get("observation_revision") or 0),
                result_need_state_revision=runtime._need_state_revision(),
            )
        else:
            resolved = runtime.needs.get(need_id, owner_id=owner_id, session_id=session_id)
            expected_status = "resolved" if resolves_need else str(need.get("status") or "open")
            if resolved is None or str(resolved.get("status") or "") != expected_status:
                raise StateRevisionConflictError("source admission Need phase does not match current Need")
        runtime.admission.mark_committed(event_id=event.event_id, semantic_digest=semantic_digest, need_plan_digest=need_plan_digest)
    result_status = "recorded" if (resolves_need and binding_status in {"cleared", "unbound", "not_applicable"}) else "degraded"
    return {
        "status": result_status,
        "situation": persisted,
        "need": resolved,
        "receipt": receipt_dict,
        "material_changed": material_changed,
        "observation_status": (
            "changed"
            if material_changed
            else "available"
            if material_observed
            else "unchanged"
        ),
        "coverage": {
            "status": "exact" if coverage_ok else "mismatch",
            "reason": coverage_reason or "not_required",
        },
        "unknown_binding": {
            "status": binding_status,
            "bound": bool(unknown_binding),
        },
        "attention_trigger": attention_trigger,
    }


def command_situation(
    runtime: Any,
    event: VeyraEvent,
    situation_id: str,
    *,
    owner_id: str,
    session_id: str,
    command: str,
    expected_revision: int,
    patch: dict[str, Any] | None = None,
    reason: str = "",
) -> dict[str, Any]:
    """Public server-command seam for Product, using the same CAS writer."""

    owner = str(owner_id or "").strip()
    session = str(session_id or "").strip()
    current = runtime.get_situation(situation_id, owner_id=owner, session_id=session)
    if current is None:
        raise KeyError("unknown Situation")
    prior_admission = runtime.admission.get(event.event_id)
    if int(current.get("observation_revision") or 0) != int(expected_revision):
        can_resume = (
            isinstance(prior_admission, dict)
            and runtime.admission.phase_rank(prior_admission.get("phase")) >= 1
            and str(current.get("source_event_id") or "") == str(event.event_id)
        )
        if not can_resume:
            raise StateRevisionConflictError("Situation revision does not match expected_revision")
    selected = str(command or "").strip().lower()
    if selected not in {"correct", "resolve", "reopen", "quiet"}:
        raise ValueError("unsupported Situation command")
    allowed = {
        "title", "label", "summary", "goal", "category", "deadline_at", "progress",
        "known", "unknown", "assumptions", "timeline", "material_change",
        "next_observation_at", "next_step", "next_step_epistemic_status",
    }
    raw_patch = copy.deepcopy(patch or {})
    unknown = set(raw_patch) - allowed
    if unknown:
        raise ValueError(f"unsupported Situation patch field: {sorted(unknown)!r}")
    semantic = copy.deepcopy(current.get("semantic") or {})
    semantic.update(raw_patch)
    reopen = selected == "reopen"
    operation = "correct"
    if selected == "resolve":
        operation = "resolve"
        semantic["lifecycle"] = "resolved"
    elif selected == "reopen":
        can_resume_reopen = (
            isinstance(prior_admission, dict)
            and runtime.admission.phase_rank(prior_admission.get("phase")) >= 1
            and str(current.get("source_event_id") or "") == str(event.event_id)
        )
        if str(current.get("status") or "") not in runtime.situations.TERMINAL_STATUSES and not can_resume_reopen:
            raise StateRevisionConflictError("reopen requires a terminal Situation")
        semantic["lifecycle"] = "active"
        semantic["reopen_reason"] = str(reason or "user requested a reopen")[:240]
    elif selected == "quiet":
        if str(current.get("status") or "") in runtime.situations.TERMINAL_STATUSES:
            raise StateRevisionConflictError("quiet cannot revive a terminal Situation")
        semantic["lifecycle"] = "waiting"
    with runtime.state_store.writer_transaction():
        semantic_digest = runtime.admission.digest(semantic)
        admission = runtime.admission.prepare(
            event_id=event.event_id,
            owner_id=owner,
            session_id=session,
            situation_id=str(situation_id),
            operation=operation,
            semantic_digest=semantic_digest,
            need_plan_digest=runtime.admission.digest([]),
            answered_need_generations={},
            event_timestamp=event.timestamp,
            expected_situation_revision=(
                int(prior_admission.get("expected_situation_revision"))
                if isinstance(prior_admission, dict) and prior_admission.get("expected_situation_revision") is not None
                else int(expected_revision)
            ),
            expected_need_state_revision=(
                int(prior_admission.get("expected_need_state_revision"))
                if isinstance(prior_admission, dict) and prior_admission.get("expected_need_state_revision") is not None
                else int(runtime._need_state_revision())
            ),
        )
        if admission.get("replayed"):
            replayed = runtime.get_situation(situation_id, owner_id=owner, session_id=session)
            if replayed is None:
                raise StateRevisionConflictError("committed command admission has no current Situation")
            return {"status": "replayed", "command": selected, "situation": replayed}
        phase_rank = runtime.admission.phase_rank(admission.get("phase"))
        fresh_current = runtime.get_situation(situation_id, owner_id=owner, session_id=session)
        semantic_already_applied = runtime._semantic_event_matches(
            fresh_current,
            event_id=event.event_id,
            semantic_digest=semantic_digest,
            expected_revision=admission.get("result_situation_revision"),
        )
        if phase_rank >= 1 and not semantic_already_applied:
            raise StateRevisionConflictError("command admission semantic phase does not match current Situation")
        if semantic_already_applied:
            persisted = copy.deepcopy(fresh_current)
            persisted["semantic_replayed"] = True
        else:
            persisted = runtime.situations.record_semantic(
                event,
                subject_key=str(current.get("semantic_subject_key") or ""),
                semantic_state=semantic,
                situation_id=str(situation_id),
                operation=operation,
                expected_revision=expected_revision,
                evidence_refs=event.evidence_refs,
                reopen=reopen,
                server_command=True,
            )
            runtime.admission.mark_phase(
                event_id=event.event_id,
                phase="semantic_applied",
                semantic_digest=semantic_digest,
                need_plan_digest=runtime.admission.digest([]),
                result_situation_revision=int(persisted.get("observation_revision") or 0),
            )
        if phase_rank < 2:
            active = runtime.needs.list(
                owner_id=owner,
                session_id=session,
                situation_id=str(situation_id),
                statuses={"open", "asked", "observing", "waiting"},
                limit=runtime.needs.max_needs_per_situation,
            )
            if str(persisted.get("status") or "") in runtime.situations.TERMINAL_STATUSES:
                for need in active:
                    runtime.needs.dismiss(
                        str(need["need_id"]),
                        owner_id=owner,
                        session_id=session,
                        answered_by_event_id=event.event_id,
                        expected_generation=int(need.get("generation") or 1),
                    )
            runtime.admission.mark_phase(
                event_id=event.event_id,
                phase="needs_applied",
                semantic_digest=semantic_digest,
                need_plan_digest=runtime.admission.digest([]),
                result_situation_revision=int(persisted.get("observation_revision") or 0),
                result_need_state_revision=runtime._need_state_revision(),
            )
        runtime.admission.mark_committed(
            event_id=event.event_id,
            semantic_digest=semantic_digest,
            need_plan_digest=runtime.admission.digest([]),
        )
    return {"status": "recorded", "command": selected, "situation": persisted}


def preflight_need_scope(
    runtime: Any,
    *,
    owner_id: str,
    session_id: str,
    situation_id: str | None,
    needs: list[CandidateNeed],
    answered_needs: list[dict[str, Any]],
    unknown_bindings: Mapping[str, str | None] | None = None,
    evidence_targets: list[Mapping[str, Any] | None] | None = None,
) -> None:
    """Reject predictable Need capacity/scope failures before Situation write."""

    if situation_id is None:
        # A create receives its server token only after the Situation CAS;
        # candidates are still schema-validated before that write.
        return
    current = runtime.needs.list(
        owner_id=owner_id,
        session_id=session_id,
        situation_id=situation_id,
        limit=runtime.needs.max_needs_per_situation,
    )
    selected_bindings = unknown_bindings or {}
    selected_targets = evidence_targets or [None] * len(needs)
    if len(selected_targets) != len(needs):
        raise ValueError("InformationNeed evidence_targets must align with candidate Needs")
    existing_ids = {
        runtime.needs.stable_need_id(
            situation_id=situation_id,
            blocked_judgment=str(item.blocked_judgment),
            evidence_kind=str(item.evidence_kind),
            evidence_target_digest=stable_evidence_target_digest(selected_targets[index]),
            unknown_binding_digest=runtime.needs._unknown_binding_digest(
                selected_bindings.get(item.blocked_judgment)
            ),
            typed_endpoint=True,
            observation_mode=str(item.observation_mode),
        )
        for index, item in enumerate(needs)
    }
    projected = len({str(item.get("need_id") or "") for item in current} | existing_ids)
    if projected > runtime.needs.max_needs_per_situation:
        terminal = sum(
            1
            for item in current
            if str(item.get("status") or "") in {"resolved", "expired", "dismissed"}
        )
        if projected - terminal > runtime.needs.max_needs_per_situation:
            raise StateRevisionConflictError(
                "InformationNeed capacity cannot be admitted before Situation write"
            )
    for item in answered_needs:
        if str(item.get("owner_id") or "") != owner_id or str(item.get("session_id") or "") != session_id:
            raise PermissionError("InformationNeed owner/session scope mismatch")


def dedupe_records(values: list[dict[str, Any]], *, key: str, limit: int) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, dict):
            continue
        identity = str(value.get(key) or value.get("statement") or "")
        if identity and identity in seen:
            continue
        if identity:
            seen.add(identity)
        result.append(copy.deepcopy(value))
    return result[-limit:]


def dedupe_text(values: list[Any], *, limit: int) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        selected = str(value or "").strip()[:480]
        if selected and selected not in seen:
            seen.add(selected)
            result.append(selected)
    return result[-limit:]


def reaction_hint(
    *,
    situation: dict[str, Any],
    needs: list[dict[str, Any]],
    requested_kind: str,
) -> ReactionPlan | None:
    """Derive a non-authoritative hint for later LivingReactionPolicy."""

    open_needs = [
        item
        for item in needs
        if str(item.get("status") or "") in {"open", "asked", "observing", "waiting"}
    ]
    if not open_needs:
        return None
    selected = max(
        open_needs,
        key=lambda item: (
            float(item.get("urgency") or 0.0),
            str(item.get("updated_at") or ""),
        ),
    )
    fallback = str(selected.get("fallback_reaction") or "wait")
    kind = requested_kind if requested_kind in {"ask", "read", "wait", "silent"} else fallback
    if kind == "ask" and not str(selected.get("question") or "").strip():
        kind = "wait"
    semantic = situation.get("semantic") if isinstance(situation.get("semantic"), dict) else {}
    material = str(semantic.get("material_change") or "")
    related = str(semantic.get("summary") or "")
    why_now = str(selected.get("why_now") or "")
    if material and not why_now:
        why_now = material
    return ReactionPlan(
        situation_id=str(situation.get("situation_id") or ""),
        need_id=str(selected.get("need_id") or ""),
        kind=kind,  # type: ignore[arg-type]
        related_to=related[:480] or "当前 Situation",
        material_change=material[:480],
        why_now=why_now[:480],
        rank=min(1.0, float(selected.get("urgency") or 0.0) + (0.2 if material else 0.0)),
        cooldown_until=None,
        suppression_reason="" if kind != "silent" else "candidate_or_policy_requested_silence",
    )
