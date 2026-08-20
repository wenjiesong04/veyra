#!/usr/bin/env python3
"""Non-gating V1 acceptance against the configured model and a local ICS feed.

This is deliberately separate from the deterministic smoke suite.  It uses the
same natural-language UnderstandingCore -> LivingContextOrchestrator path for
all three examples, writes only to a temporary WorldStateStore, and reports
``NOT_RUN``/``DEGRADED`` when the model or a typed source cannot prove a step.
It never prints a model payload, provider error body, credential, or temporary
state path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.env_loader import load_runtime_env  # noqa: E402
from core.reasoning_core import CoreReasoning  # noqa: E402
from core.understanding_core import UnderstandingCore  # noqa: E402
from core.world_state import StateRevisionConflictError, WorldStateStore  # noqa: E402
from interface.event_schema import EventSource, EventType, VeyraEvent  # noqa: E402
from interface.living_source_contract import canonical_utc  # noqa: E402
from runtime.calendar_source import CalendarSource, IcsCalendarProvider  # noqa: E402
from runtime.living_context_composition import build_living_context_composition  # noqa: E402


OWNER = "v1-real-model-acceptance"
SESSION = "isolated-natural-language"
SCENARIOS: tuple[dict[str, str], ...] = (
    {
        "key": "travel",
        "initial": "下周去上海出差",
        "followup": "补充上海出差进展：我已经开始确认行程，请把这个进展记到原来的事项里。",
        "calendar_followup": "上海出差的具体日程还没有确认，请关注日历里的安排变化。",
        "feedback": "上海出差这个提醒不用管",
    },
    {
        "key": "interview",
        "initial": "最近准备面试",
        "followup": "补充面试准备进展：我已经开始整理简历和常见问题，请把这个进展记到原来的事项里。",
    },
    {
        "key": "move",
        "initial": "月底要搬家",
        "followup": "补充月底搬家进展：我已经开始联系搬家公司，请把这个进展记到原来的事项里。",
        "feedback": "搬家这个提醒以后提前三天告诉我",
    },
)


class AcceptanceUnavailable(RuntimeError):
    """The configured model/source cannot provide this acceptance evidence."""

    def __init__(self, stage: str, reason: str, *, progress: Mapping[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.stage = stage
        self.reason = reason
        self.progress = dict(progress or {})


class Clock:
    def __init__(self) -> None:
        self.value = datetime.now(timezone.utc).replace(microsecond=0)

    def __call__(self) -> datetime:
        return self.value


def _emit(marker: str, payload: Mapping[str, Any]) -> None:
    # Keep the public result intentionally small.  In particular, never pass
    # through exception text or a model/provider response here.
    print(f"{marker} {json.dumps(dict(payload), ensure_ascii=False, sort_keys=True)}")


def _aware_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _event(clock: Clock, text: str, event_id: str) -> VeyraEvent:
    timestamp = canonical_utc(clock())
    return VeyraEvent(
        type=EventType.USER_MESSAGE,
        source=EventSource(channel="api", user_id=OWNER, session_id=SESSION),
        payload={"text": text},
        event_id=event_id,
        timestamp=timestamp,
        occurred_at=timestamp,
        received_at=timestamp,
    )


def _ics_text(clock: Clock) -> str:
    start = clock() + timedelta(hours=1)
    first_end = start + timedelta(minutes=45)
    second_start = first_end + timedelta(minutes=30)
    second_end = second_start + timedelta(minutes=45)

    def ics_time(value: datetime) -> str:
        return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    return "\n".join(
        (
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//Veyra//V1 real acceptance//EN",
            "BEGIN:VEVENT",
            "UID:v1-real-acceptance-a",
            "DTSTART:" + ics_time(start),
            "DTEND:" + ics_time(first_end),
            "SUMMARY:Shanghai business meeting",
            "LOCATION:Shanghai",
            "END:VEVENT",
            "BEGIN:VEVENT",
            "UID:v1-real-acceptance-b",
            "DTSTART:" + ics_time(second_start),
            "DTEND:" + ics_time(second_end),
            "SUMMARY:Follow-up meeting",
            "LOCATION:Hangzhou",
            "END:VEVENT",
            "END:VCALENDAR",
            "",
        )
    )


def _candidate_status(understanding: Any) -> str:
    metrics = getattr(understanding, "living_context_candidate_metrics", {})
    return str(metrics.get("status") or "") if isinstance(metrics, dict) else ""


def _feedback_status(understanding: Any) -> str:
    metrics = getattr(understanding, "living_reaction_feedback_metrics", {})
    return str(metrics.get("status") or "") if isinstance(metrics, dict) else ""


def _boundary_failure_suffix(understanding: Any, *, candidate: bool = True) -> str:
    """Return bounded model-boundary metadata for safe acceptance triage."""

    metrics = (
        getattr(understanding, "living_context_candidate_metrics", {})
        if candidate
        else getattr(understanding, "living_reaction_feedback_metrics", {})
    )
    if not isinstance(metrics, Mapping):
        return "unknown"
    status = str(metrics.get("status") or "unknown")
    attempted = bool(getattr(understanding, "model_boundary_metrics", {}).get("repair_attempted"))
    issue_codes = metrics.get("issue_codes")
    code = str(issue_codes[0]).replace(" ", "_")[:96] if isinstance(issue_codes, list) and issue_codes else "none"
    return f"{status}_{'repair' if attempted else 'initial'}_{code}"


def _feedback_boundary_summary(understanding: Any, *, expected_label: str) -> dict[str, Any]:
    """Return only safe feedback-boundary diagnostics for a non-gating run."""

    metrics = getattr(understanding, "living_reaction_feedback_metrics", {})
    if not isinstance(metrics, Mapping):
        metrics = {}
    feedback = getattr(understanding, "living_reaction_feedback", None)
    observed_label = str(getattr(feedback, "label", "")) if feedback is not None else ""
    allowed_labels = {
        "ignore",
        "resolved",
        "useful",
        "not_useful",
        "too_early",
        "too_late",
        "too_frequent",
        "remind_before",
    }
    summary: dict[str, Any] = {
        "status": str(metrics.get("status") or "absent"),
        "issue_codes": [
            str(item)[:120]
            for item in (metrics.get("issue_codes") or [])[:3]
            if isinstance(item, str)
        ],
        "reason": str(metrics.get("reason") or "")[:80],
        "row_count": int(metrics.get("row_count") or 0) if str(metrics.get("row_count") or "").isdigit() else 0,
        "extractor_attempted": bool(metrics.get("extractor_attempted")),
        "expected_label": expected_label if expected_label in allowed_labels else "unknown",
    }
    if observed_label in allowed_labels:
        summary["observed_label"] = observed_label
    return summary


class FeedbackReceiptError(ValueError):
    """A feedback response did not contain one safe durable receipt row."""

    def __init__(self, code: str) -> None:
        self.code = str(code)[:80]
        super().__init__(self.code)


def _feedback_receipt_situation_id(
    result: Mapping[str, Any],
    *,
    owner_id: str,
    session_id: str,
    expected_label: str,
) -> str:
    """Unwrap and validate the bounded reaction feedback receipt.

    ``LivingContextOrchestrator`` returns an outer turn artifact whose
    ``feedback`` value is the reaction-runtime envelope.  The durable row is
    one level deeper at ``feedback.feedback``.  Keep this helper strict and
    content-free: it never searches a catalog or infers a Situation from the
    user message.
    """

    if not isinstance(result, Mapping):
        raise FeedbackReceiptError("receipt_not_object")
    outer = result.get("feedback")
    if not isinstance(outer, Mapping):
        raise FeedbackReceiptError("receipt_missing")
    outer_status = outer.get("status")
    if outer_status not in {"recorded", "duplicate"}:
        raise FeedbackReceiptError("outer_status_invalid")
    runtime_envelope = outer.get("feedback")
    if not isinstance(runtime_envelope, Mapping):
        raise FeedbackReceiptError("runtime_envelope_missing")
    runtime_status = runtime_envelope.get("status")
    if runtime_status not in {"recorded", "duplicate"}:
        raise FeedbackReceiptError("runtime_status_invalid")
    durable = runtime_envelope.get("feedback")
    if not isinstance(durable, Mapping):
        raise FeedbackReceiptError("durable_row_missing")
    if durable.get("schema_version") != "veyra.living_reaction_feedback.v1":
        raise FeedbackReceiptError("schema_invalid")

    semantics = durable.get("semantics")
    expected_keys = {
        "feedback_id",
        "owner_id",
        "session_id",
        "situation_id",
        "reaction_id",
        "label",
        "category",
        "remind_before_seconds",
    }
    if not isinstance(semantics, Mapping) or set(semantics) != expected_keys:
        raise FeedbackReceiptError("semantics_shape_invalid")
    if durable.get("feedback_id") != semantics.get("feedback_id"):
        raise FeedbackReceiptError("feedback_id_mismatch")

    for field, expected in (("owner_id", owner_id), ("session_id", session_id)):
        value = semantics.get(field)
        if not isinstance(value, str) or not value or len(value) > 240 or value != expected:
            raise FeedbackReceiptError(f"{field}_scope_invalid")
    situation_id = semantics.get("situation_id")
    if not isinstance(situation_id, str) or not situation_id or len(situation_id) > 240:
        raise FeedbackReceiptError("situation_scope_invalid")
    for field in ("feedback_id", "reaction_id", "category"):
        value = semantics.get(field)
        if not isinstance(value, str) or not value or len(value) > 240:
            raise FeedbackReceiptError(f"{field}_invalid")

    allowed_labels = {
        "ignore",
        "resolved",
        "useful",
        "not_useful",
        "too_early",
        "too_late",
        "too_frequent",
        "remind_before",
    }
    label = semantics.get("label")
    if label not in allowed_labels or label != expected_label:
        raise FeedbackReceiptError("label_mismatch")
    remind_before = semantics.get("remind_before_seconds")
    if remind_before is not None and (
        isinstance(remind_before, bool)
        or not isinstance(remind_before, int)
        or not 0 <= remind_before <= 30 * 86400
    ):
        raise FeedbackReceiptError("remind_before_invalid")
    if label == "remind_before" and remind_before is None:
        raise FeedbackReceiptError("remind_before_missing")
    if label != "remind_before" and remind_before is not None:
        raise FeedbackReceiptError("unexpected_remind_before")
    return situation_id


def _preserve_acceptance_progress(
    error: AcceptanceUnavailable,
    acceptance_progress: Mapping[str, Any],
) -> None:
    """Merge global counters while retaining safe feedback-boundary detail."""

    merged = dict(acceptance_progress)
    if isinstance(error.progress, Mapping):
        boundary = error.progress.get("feedback_boundary")
        if isinstance(boundary, Mapping):
            merged["feedback_boundary"] = dict(boundary)
    error.progress = merged


@dataclass(slots=True)
class NaturalTurn:
    understanding: Any
    catalog: list[dict[str, Any]]
    result: dict[str, Any]


class RealModelAcceptance:
    def __init__(self, store: WorldStateStore, composition: Any, clock: Clock) -> None:
        self.store = store
        self.composition = composition
        self.orchestrator = composition.orchestrator
        self.clock = clock
        self.reasoning = CoreReasoning(store)
        self.understanding_core = UnderstandingCore(self.reasoning, store)

    def _understanding(self, text: str, event: VeyraEvent, catalog: list[dict[str, Any]]) -> Any:
        turn_context = self.reasoning.turn_context.build(
            user_message=text,
            attention_focus=[text[:120]],
            event=event,
            rule_decision={},
            living_context_situation_candidates=catalog,
        )
        return self.understanding_core.build(
            text=text,
            attention_focus=[text[:120]],
            event=event,
            turn_context=turn_context,
        )

    def natural_turn(
        self,
        text: str,
        event_id: str,
        *,
        require_candidate: bool = True,
        phase: str = "turn",
    ) -> NaturalTurn:
        event = _event(self.clock, text, event_id)
        catalog = self.orchestrator.model_catalog(owner_id=OWNER, session_id=SESSION, limit=8)
        understanding = self._understanding(text, event, catalog)
        if require_candidate:
            candidate = getattr(understanding, "living_context_candidate", None)
            if candidate is None or candidate.disposition == "quiet":
                # A provider can return an otherwise valid understanding
                # envelope without its optional Situation artifact.  Retry
                # that model boundary once in the isolated acceptance harness;
                # this does not construct a candidate or alter admission
                # authority.  Core-level quiet/continuation repair is already
                # counted, so do not create an unbounded retry chain.
                boundary = getattr(understanding, "model_boundary_metrics", {})
                if not isinstance(boundary, Mapping) or not bool(boundary.get("repair_attempted")):
                    retry = self._understanding(text, event, catalog)
                    if retry is not understanding:
                        understanding = retry
                        candidate = getattr(understanding, "living_context_candidate", None)
                if candidate is None or candidate.disposition == "quiet":
                    raise AcceptanceUnavailable(
                        "model",
                        f"{phase}_candidate_not_admitted_{_boundary_failure_suffix(understanding)}",
                    )
            if getattr(understanding, "source", "") not in {"model", "model_candidate"}:
                raise AcceptanceUnavailable("model", f"{phase}_understanding_not_from_model")
            if _candidate_status(understanding) not in {"accepted", "repaired"}:
                raise AcceptanceUnavailable("model", f"{phase}_candidate_boundary_not_accepted")
        try:
            result = self.orchestrator.process_user_turn(event, understanding, catalog=catalog)
        except StateRevisionConflictError as exc:
            raise AcceptanceUnavailable("admission", "candidate_catalog_conflict") from exc
        except (KeyError, ValueError) as exc:
            raise AcceptanceUnavailable("admission", "candidate_rejected_at_server_boundary") from exc
        if require_candidate:
            if str(result.get("status") or "") not in {"recorded", "replayed"}:
                raise AcceptanceUnavailable("admission", f"{phase}_candidate_not_recorded")
            if not isinstance(result.get("situation"), dict):
                raise AcceptanceUnavailable("admission", f"{phase}_situation_not_returned")
        return NaturalTurn(understanding=understanding, catalog=catalog, result=result)

    def natural_feedback(self, text: str, event_id: str, label: str) -> dict[str, Any]:
        event = _event(self.clock, text, event_id)
        catalog = self.orchestrator.model_catalog(owner_id=OWNER, session_id=SESSION, limit=8)
        understanding = self._understanding(text, event, catalog)
        feedback_boundary = _feedback_boundary_summary(understanding, expected_label=label)
        feedback = getattr(understanding, "living_reaction_feedback", None)
        if feedback is None or str(getattr(feedback, "label", "")) != label:
            raise AcceptanceUnavailable(
                "feedback",
                f"{label}_feedback_not_parsed",
                progress={"feedback_boundary": feedback_boundary},
            )
        if _feedback_status(understanding) not in {"accepted", "repaired"}:
            raise AcceptanceUnavailable(
                "feedback",
                f"{label}_feedback_boundary_not_accepted",
                progress={"feedback_boundary": feedback_boundary},
            )
        try:
            result = self.orchestrator.process_user_turn(event, understanding, catalog=catalog)
        except StateRevisionConflictError as exc:
            raise AcceptanceUnavailable(
                "feedback",
                "feedback_catalog_conflict",
                progress={"feedback_boundary": feedback_boundary},
            ) from exc
        except (KeyError, ValueError) as exc:
            raise AcceptanceUnavailable(
                "feedback",
                "feedback_rejected_at_server_boundary",
                progress={"feedback_boundary": feedback_boundary},
            ) from exc
        feedback_result = result.get("feedback") if isinstance(result.get("feedback"), dict) else {}
        if str(feedback_result.get("status") or "") not in {"recorded", "duplicate"}:
            boundary = dict(feedback_boundary)
            boundary["admission_status"] = str(feedback_result.get("status") or "absent")[:40]
            raise AcceptanceUnavailable(
                "feedback",
                f"{label}_feedback_not_recorded",
                progress={"feedback_boundary": boundary},
            )
        try:
            situation_id = _feedback_receipt_situation_id(
                result,
                owner_id=OWNER,
                session_id=SESSION,
                expected_label=label,
            )
        except FeedbackReceiptError as exc:
            boundary = dict(feedback_boundary)
            boundary["receipt_status"] = "invalid"
            boundary["receipt_issue"] = exc.code
            raise AcceptanceUnavailable(
                "feedback",
                f"{label}_feedback_receipt_{exc.code}",
                progress={"feedback_boundary": boundary},
            ) from exc
        return {
            "result": result,
            "situation_id": situation_id,
            "feedback_boundary": feedback_boundary,
        }


def _semantic(situation: Mapping[str, Any]) -> Mapping[str, Any]:
    value = situation.get("semantic")
    return value if isinstance(value, Mapping) else {}


def _active_needs(composition: Any, situation_id: str) -> list[dict[str, Any]]:
    rows = composition.needs.list(
        owner_id=OWNER,
        session_id=SESSION,
        situation_id=situation_id,
        statuses={"open", "asked", "observing", "waiting"},
        limit=32,
    )
    return [dict(row) for row in rows if isinstance(row, Mapping)]


def _calendar_need(composition: Any, situation_id: str) -> dict[str, Any] | None:
    for need in _active_needs(composition, situation_id):
        allowed = {str(item).strip().lower() for item in need.get("allowed_source_classes", [])}
        if str(need.get("evidence_kind") or "").strip().lower() == "calendar" and "calendar" in allowed:
            return need
    return None


def _reaction_for(composition: Any, situation_id: str) -> dict[str, Any] | None:
    rows = composition.orchestrator.model_catalog(owner_id=OWNER, session_id=SESSION, limit=16)
    for row in rows:
        if str(row.get("situation_token") or "") != situation_id:
            continue
        reaction = row.get("reaction")
        return dict(reaction) if isinstance(reaction, Mapping) else None
    return None


def _scope_effect(reaction_runtime: Any, situation_id: str) -> dict[str, Any] | None:
    state = reaction_runtime.read_state()
    for row in (state.get("situation_effects") or {}).values():
        if isinstance(row, Mapping) and str(row.get("scope") or "").endswith("\x1f" + situation_id):
            return dict(row)
    return None


def _calendar_result(tick: Mapping[str, Any], situation_id: str) -> dict[str, Any] | None:
    for item in tick.get("items") or []:
        if not isinstance(item, Mapping) or str(item.get("situation_id") or "") != situation_id:
            continue
        source = item.get("source")
        if isinstance(source, Mapping):
            return dict(item)
    return None


def _calendar_evidence_reason(composition: Any, item: Mapping[str, Any], situation_id: str) -> str | None:
    """Return a safe, content-free reason for each Calendar seam outcome."""

    source = item.get("source") if isinstance(item.get("source"), Mapping) else {}
    receipt = source.get("receipt") if isinstance(source.get("receipt"), Mapping) else {}
    applied = source.get("applied") if isinstance(source.get("applied"), Mapping) else {}
    applied_situation = applied.get("situation") if isinstance(applied.get("situation"), Mapping) else {}
    if str(source.get("status") or "") != "ok":
        return "calendar_receipt_not_ok"
    if str(receipt.get("source") or "") != "calendar":
        return "calendar_receipt_source_missing"
    if str(applied.get("status") or "") not in {"recorded", "replayed"}:
        return "calendar_apply_not_recorded"
    if str(applied_situation.get("situation_id") or "") != situation_id:
        return "calendar_apply_wrong_situation"
    semantic = _semantic(applied_situation)
    if not semantic.get("known"):
        return "calendar_known_missing"
    if not semantic.get("evidence"):
        return "calendar_evidence_missing"
    if not any(str(row.get("source") or "") == "calendar" for row in semantic.get("evidence") if isinstance(row, Mapping)):
        return "calendar_evidence_provenance_missing"
    if not semantic.get("timeline"):
        return "calendar_timeline_missing"
    if not any("calendar observation" in str(row.get("statement") or "") for row in semantic.get("timeline") if isinstance(row, Mapping)):
        return "calendar_timeline_observation_missing"
    decision = item.get("reevaluated") if isinstance(item.get("reevaluated"), Mapping) else {}
    decision = decision.get("decision") if isinstance(decision.get("decision"), Mapping) else {}
    if str(decision.get("disposition") or "") != "suggest":
        return "calendar_reaction_disposition_not_suggest"
    required = ("what_happened", "why_it_matters", "why_now", "suggested_next_step")
    for key in required:
        if not str(decision.get(key) or "").strip():
            return f"calendar_reaction_explanation_missing_{key}"
    return None


def run_acceptance() -> dict[str, Any]:
    load_runtime_env()
    with TemporaryDirectory(prefix="veyra-v1-real-model-") as temp:
        acceptance_progress: dict[str, Any] = {
            "creates_accepted": 0,
            "updates_accepted": 0,
            "calendar_updates_accepted": 0,
        }
        store = WorldStateStore(Path(temp) / "state")
        reasoning = CoreReasoning(store)
        model_status = reasoning.status()
        if not bool(model_status.get("configured")):
            raise AcceptanceUnavailable("model", "model_not_configured")

        clock = Clock()
        calendar_source = CalendarSource(IcsCalendarProvider(ics_text=_ics_text(clock), ttl_seconds=300))
        composition = build_living_context_composition(store, clock=clock, calendar_source=calendar_source)
        orchestrator = composition.orchestrator
        consent = orchestrator.grant_source_consent("calendar", owner_id=OWNER, session_id=SESSION, expected_generation=0)
        if str(consent.get("status") or "") != "granted":
            raise AcceptanceUnavailable("source", "calendar_consent_not_granted")
        runner = RealModelAcceptance(store, composition, clock)

        def fail(stage: str, reason: str) -> None:
            raise AcceptanceUnavailable(stage, reason, progress=acceptance_progress)

        records: dict[str, dict[str, Any]] = {}
        for index, scenario in enumerate(SCENARIOS, start=1):
            try:
                turn = runner.natural_turn(
                    scenario["initial"],
                    f"real-create-{index}",
                    phase=f"{scenario['key']}_create",
                )
            except AcceptanceUnavailable as exc:
                exc.progress = dict(acceptance_progress)
                raise
            acceptance_progress["creates_accepted"] += 1
            situation = turn.result["situation"]
            situation_id = str(situation.get("situation_id") or "")
            if not situation_id:
                fail("admission", f"{scenario['key']}_situation_id_missing")
            semantic = _semantic(situation)
            goal = str(semantic.get("goal") or "").strip()
            semantic_progress = semantic.get("progress") if isinstance(semantic.get("progress"), Mapping) else {}
            progress_status = str(semantic_progress.get("status") or "unknown")
            if not goal or progress_status == "unknown":
                fail("model", f"{scenario['key']}_goal_or_progress_missing")
            deadline = _aware_time(semantic.get("deadline_at"))
            if scenario["key"] in {"travel", "move"} and deadline is None:
                fail("model", f"{scenario['key']}_deadline_missing_or_unbounded")
            if scenario["key"] == "interview" and deadline is None and not (_active_needs(composition, situation_id) or semantic.get("unknown")):
                fail("model", "interview_deadline_unknown_not_tracked")
            records[scenario["key"]] = {
                "situation_id": situation_id,
                "initial_revision": int(situation.get("observation_revision") or 0),
                "goal_present": bool(goal),
                "progress_status": progress_status,
                "deadline_present": deadline is not None,
                "deadline_timezone_aware": deadline is not None,
            }

            try:
                followup = runner.natural_turn(
                    scenario["followup"],
                    f"real-followup-{index}",
                    phase=f"{scenario['key']}_update",
                )
            except AcceptanceUnavailable as exc:
                exc.progress = dict(acceptance_progress)
                raise
            acceptance_progress["updates_accepted"] += 1
            updated = followup.result.get("situation") if isinstance(followup.result.get("situation"), Mapping) else {}
            if str(updated.get("situation_id") or "") != situation_id or int(updated.get("observation_revision") or 0) <= records[scenario["key"]]["initial_revision"]:
                fail("admission", f"{scenario['key']}_followup_revision_not_updated")
            records[scenario["key"]]["followup_revision"] = int(updated.get("observation_revision") or 0)

        # If the model did not initially express a Calendar InformationNeed,
        # use one more ordinary user update to state the missing observation.
        # No typed candidate or scenario-specific runtime branch is injected.
        travel_id = records["travel"]["situation_id"]
        calendar_need = _calendar_need(composition, travel_id)
        if calendar_need is None:
            try:
                calendar_turn = runner.natural_turn(
                    SCENARIOS[0]["calendar_followup"],
                    "real-calendar-followup",
                    phase="calendar_update",
                )
            except AcceptanceUnavailable as exc:
                exc.progress = dict(acceptance_progress)
                raise
            acceptance_progress["calendar_updates_accepted"] += 1
            updated = calendar_turn.result.get("situation") if isinstance(calendar_turn.result.get("situation"), Mapping) else {}
            if str(updated.get("situation_id") or "") != travel_id:
                fail("admission", "calendar_followup_rebound_to_wrong_situation")
            calendar_need = _calendar_need(composition, travel_id)
        if calendar_need is None:
            fail("source", "calendar_need_not_admitted")

        tick = orchestrator.tick(owner_id=OWNER, session_id=SESSION, limit=20)
        calendar_item = _calendar_result(tick, travel_id)
        if calendar_item is None:
            fail("source", "calendar_item_missing")
        calendar_reason = _calendar_evidence_reason(composition, calendar_item, travel_id)
        if calendar_reason is not None:
            fail("source", calendar_reason)
        applied = calendar_item.get("source", {}).get("applied", {})
        applied_semantic = _semantic(applied.get("situation") if isinstance(applied, Mapping) else {})
        resolved_need = not any(str(row.get("need_id") or "") == str(calendar_need.get("need_id") or "") and str(row.get("status") or "") in {"open", "asked", "observing", "waiting"} for row in composition.needs.list(owner_id=OWNER, session_id=SESSION, situation_id=travel_id, limit=32) if isinstance(row, Mapping))

        # Use model-parsed natural feedback for the same catalog seam.  The
        # first feedback proves suppression survives a fresh composition.
        try:
            ignore = runner.natural_feedback(SCENARIOS[0]["feedback"], "real-feedback-ignore", "ignore")
        except AcceptanceUnavailable as exc:
            _preserve_acceptance_progress(exc, acceptance_progress)
            raise
        ignore_id = ignore["situation_id"]
        if isinstance(ignore.get("feedback_boundary"), Mapping):
            acceptance_progress["feedback_boundary"] = dict(ignore["feedback_boundary"])
        if not ignore_id:
            fail("feedback", "ignore_target_missing")
        restarted = build_living_context_composition(store, clock=clock, calendar_source=calendar_source)
        ignore_effect = _scope_effect(restarted.reaction, ignore_id)
        if not isinstance(ignore_effect, dict) or str(ignore_effect.get("suppression_reason") or "") != "ignore":
            fail("restart", "ignore_effect_not_durable")
        ignore_tick = restarted.orchestrator.tick(owner_id=OWNER, session_id=SESSION, limit=20)
        ignore_row = next((row for row in ignore_tick.get("items") or [] if isinstance(row, Mapping) and str(row.get("situation_id") or "") == ignore_id), {})
        ignore_decision = ignore_row.get("reaction", {}).get("decision", {}) if isinstance(ignore_row.get("reaction"), Mapping) else {}
        ignore_changed = str(ignore_decision.get("disposition") or "") == "silent" or bool(ignore_decision.get("suppression", {}).get("active"))
        if not ignore_changed:
            fail("restart", "ignore_did_not_change_reaction")

        # Select a separate deadline-bearing Situation so ignore and timing
        # calibration remain independent effects.  A model-generated reaction
        # is required; this never constructs a typed feedback artifact.
        timing_key = "move"
        timing_id = records[timing_key]["situation_id"]
        timing_semantic = _semantic(composition.core.get_situation(timing_id, owner_id=OWNER, session_id=SESSION) or {})
        timing_deadline = _aware_time(timing_semantic.get("deadline_at"))
        if timing_deadline is None:
            fail("feedback", "remind_before_deadline_missing")
        timing_reaction = _reaction_for(composition, timing_id)
        if timing_reaction is None:
            fail("feedback", "remind_before_target_missing")
        try:
            remind = runner.natural_feedback(SCENARIOS[2]["feedback"], "real-feedback-remind", "remind_before")
        except AcceptanceUnavailable as exc:
            _preserve_acceptance_progress(exc, acceptance_progress)
            raise
        if isinstance(remind.get("feedback_boundary"), Mapping):
            acceptance_progress["feedback_boundary"] = dict(remind["feedback_boundary"])
        if remind["situation_id"] != timing_id:
            fail("feedback", "remind_before_target_wrong_situation")
        clock.value = timing_deadline - timedelta(hours=48)
        restarted_timing = build_living_context_composition(store, clock=clock, calendar_source=calendar_source)
        remind_effect = _scope_effect(restarted_timing.reaction, timing_id)
        if not isinstance(remind_effect, dict) or int(remind_effect.get("remind_before_seconds") or 0) != 3 * 86400:
            fail("restart", "remind_before_effect_not_durable")
        timing_tick = restarted_timing.orchestrator.tick(owner_id=OWNER, session_id=SESSION, limit=20)
        timing_row = next((row for row in timing_tick.get("items") or [] if isinstance(row, Mapping) and str(row.get("situation_id") or "") == timing_id), {})
        timing_decision = timing_row.get("reaction", {}).get("decision", {}) if isinstance(timing_row.get("reaction"), Mapping) else {}
        timing_changed = int(timing_decision.get("timing", {}).get("remind_before_seconds") or 0) == 3 * 86400
        if not timing_changed:
            fail("restart", "remind_before_did_not_change_timing")
        before_count = int(restarted_timing.reaction.status().get("reaction_count") or 0)
        repeat_tick = restarted_timing.orchestrator.tick(owner_id=OWNER, session_id=SESSION, limit=20)
        after_count = int(restarted_timing.reaction.status().get("reaction_count") or 0)
        repeat_idempotent = after_count == before_count and all(
            str((row.get("reaction") or {}).get("status") or "") in {"duplicate", "recorded", ""}
            for row in repeat_tick.get("items") or []
            if isinstance(row, Mapping)
        )
        if not repeat_idempotent:
            fail("restart", "repeat_tick_created_new_reaction")

        return {
            "status": "passed",
            "scenario_count": len(records),
            "followups_updated": sum("followup_revision" in row for row in records.values()),
            "scenarios": records,
            "calendar": {
                "read": True,
                "need_resolved": resolved_need,
                "known_evidence_timeline_updated": True,
                "unknown_binding_cleared": str((applied.get("unknown_binding") or {}).get("status") or "") == "cleared" if isinstance(applied, Mapping) else False,
                "explainable_suggest": True,
            },
            "feedback": {
                "ignore": True,
                "remind_before": True,
                "restart": True,
                "repeat_tick_idempotent": True,
            },
            "progress": acceptance_progress,
        }


def main() -> int:
    try:
        result = run_acceptance()
    except AcceptanceUnavailable as exc:
        marker = "V1_REAL_MODEL_ACCEPTANCE_NOT_RUN" if exc.stage == "model" and exc.reason == "model_not_configured" else "V1_REAL_MODEL_ACCEPTANCE_DEGRADED"
        payload = {
            "status": "not_run" if marker.endswith("NOT_RUN") else "degraded",
            "stage": exc.stage,
            "reason": exc.reason,
        }
        if exc.progress:
            payload["progress"] = dict(exc.progress)
        _emit(marker, payload)
        return 0
    except Exception as exc:
        # Exception type is useful for triage and cannot contain provider
        # payloads or secrets.  The script is non-gating by design.
        _emit("V1_REAL_MODEL_ACCEPTANCE_DEGRADED", {"status": "degraded", "stage": "runtime", "reason": type(exc).__name__})
        return 0
    _emit("V1_REAL_MODEL_ACCEPTANCE_OK", result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
