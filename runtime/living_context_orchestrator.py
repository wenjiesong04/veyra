"""V1 Living Context orchestration across core, reaction, and sources.

This facade is the only integration point used by AwarenessLoop, ActiveLoop,
and ProductExperience.  Core owns Situation/Need truth, Reaction owns timing
and feedback aftereffects, and Source owns consent/request/receipt lifecycle.
None of the paths below grants route, risk, Agent, tool, execution, or delivery
authority.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
from typing import Any, Callable, Iterable, Mapping

from core.world_state import StateRevisionConflictError
from interface.event_schema import EventSource, EventType, VeyraEvent
from interface.living_context_contract import CandidateNeed
from interface.living_reaction_contract import (
    CognitiveSuggestionCandidate,
    FEEDBACK_LABELS,
    LivingReactionValidationError,
    ReactionInput,
    stable_digest as reaction_digest,
)
from interface.living_source_contract import SourceConsent, canonical_utc
from runtime.living_context_source_policy import LivingContextSourcePolicy
from runtime.information_need_runtime import InformationNeedStaleReopen
from runtime.living_source_runtime import SourceStateCorruptError


ACTIVE_SITUATION_STATUSES = frozenset({"emerging", "active", "waiting"})
ACTIVE_NEED_STATUSES = frozenset({"open", "asked", "observing", "waiting"})
READABLE_SOURCES = frozenset({"calendar", "weather", "public_web"})
CONSENT_SOURCES = frozenset({"calendar", "weather", "public_web"})


def _now_utc(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Living Context clock must be timezone-aware")
    return value.astimezone(timezone.utc)


def _authority() -> dict[str, bool]:
    return {
        "route": False,
        "risk": False,
        "tool": False,
        "agent": False,
        "execution": False,
        "delivery": False,
        "permission_expansion": False,
    }


class LivingContextOrchestrator:
    """One bounded facade for user-turn and background Living Context work."""

    def __init__(
        self,
        core_runtime: Any,
        reaction_runtime: Any,
        source_runtime: Any,
        *,
        source_policy: LivingContextSourcePolicy | None = None,
        clock: Callable[[], datetime] | None = None,
        quiet_hours_resolver: Callable[[str, str, datetime], bool] | None = None,
        conversation_runtime: Any | None = None,
    ) -> None:
        self.core = core_runtime
        self.reaction_runtime = reaction_runtime
        self.source_runtime = source_runtime
        self.source_policy = source_policy or LivingContextSourcePolicy()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._quiet_hours_resolver = quiet_hours_resolver
        self.conversation_runtime = conversation_runtime

    @property
    def needs(self) -> Any:
        return self.core.needs

    def model_catalog(self, *, owner_id: str, session_id: str, limit: int = 8) -> list[dict[str, Any]]:
        rows = self.core.model_catalog(owner_id=owner_id, session_id=session_id, limit=limit)
        result: list[dict[str, Any]] = []
        for row in rows:
            projection = deepcopy(row)
            current, reaction_status = self._current_reaction(
                owner_id=str(owner_id),
                session_id=str(session_id),
                situation_id=str(row.get("situation_token") or ""),
                situation_revision=int(row.get("observation_revision") or 1),
            )
            if current is not None:
                projection["reaction"] = current
            if reaction_status is not None:
                projection["reaction_status"] = reaction_status
            result.append(projection)
        return result

    def process_user_turn(
        self,
        event: VeyraEvent,
        understanding: Any,
        *,
        expected_revision: int | None = None,
        catalog: list[dict[str, Any]] | None = None,
        server_command: bool = False,
    ) -> dict[str, Any]:
        """Admit one turn while keeping optional feedback independently durable.

        The model catalog is a turn-start snapshot.  Feedback is validated and
        recorded against that snapshot before candidate admission, so a
        candidate revision cannot invalidate a legitimate feedback token.  A
        failed/stale optional feedback artifact is bounded to the returned
        feedback diagnostic and never aborts a valid Situation update.
        """

        owner = str(event.source.user_id or "local-user")
        session = str(event.source.session_id or "local-session")
        turn_catalog = deepcopy(catalog) if isinstance(catalog, list) else self.model_catalog(
            owner_id=owner,
            session_id=session,
            limit=16,
        )
        feedback = self._feedback_candidate(understanding)
        feedback_target: dict[str, Any] | None = None
        feedback_result: dict[str, Any] | None = None
        if feedback is not None:
            try:
                feedback_target = self._feedback_target(event, feedback, turn_catalog)
                feedback_result = self.record_feedback_from_turn(
                    event,
                    feedback,
                    catalog=turn_catalog,
                    target=feedback_target,
                    apply_situation=False,
                )
            except Exception as exc:
                # Feedback is optional.  Preserve a bounded issue artifact and
                # continue admitting the independent candidate below.
                feedback_result = {
                    "status": "ignored",
                    "reason": "feedback_not_recorded",
                    "error_type": type(exc).__name__,
                    "authority": _authority(),
                }

        try:
            core_result = self.core.process_user_turn(
                event,
                understanding,
                expected_revision=expected_revision,
                catalog=turn_catalog,
                server_command=server_command,
            )
        except Exception as exc:
            if feedback_result is None:
                raise
            # A recorded feedback artifact must survive a candidate admission
            # failure.  AwarenessLoop will render this as a degraded Living
            # Context artifact without losing the ordinary turn response.
            return {
                "status": "degraded",
                "reason": "candidate_admission_failed",
                "error_type": type(exc).__name__,
                "feedback": feedback_result,
                "authority": _authority(),
            }

        result = dict(core_result) if isinstance(core_result, dict) else {"status": "degraded"}
        result.setdefault("authority", _authority())
        situation = result.get("situation") if isinstance(result.get("situation"), dict) else None
        if situation is not None:
            reaction = self._evaluate_situation(
                situation,
                result.get("information_needs") if isinstance(result.get("information_needs"), list) else None,
                owner_id=owner,
                session_id=session,
            )
            result["reaction"] = reaction
            result["current_reaction"] = reaction.get("decision") if isinstance(reaction, dict) else None

        if feedback_result is not None:
            # ``resolved`` affects Situation lifecycle only after the candidate
            # has had a chance to commit.  Thus one turn can legitimately both
            # update a Situation and resolve the prior reaction target.
            if (
                feedback_target is not None
                and str(getattr(feedback, "label", "")) == "resolved"
                and str(feedback_result.get("status") or "") == "recorded"
            ):
                try:
                    final_situation = self._apply_resolved_feedback(
                        event,
                        feedback_target,
                    )
                    if isinstance(final_situation, dict):
                        feedback_result["situation"] = deepcopy(final_situation)
                        candidate_situation = result.get("situation")
                        candidate_id = (
                            str(candidate_situation.get("situation_id") or "")
                            if isinstance(candidate_situation, dict)
                            else ""
                        )
                        feedback_situation_id = str(final_situation.get("situation_id") or "")
                        if not candidate_id or candidate_id == feedback_situation_id:
                            # The candidate admission happened before the
                            # resolve command, so its returned artifact may
                            # still be an active revision.  Replace it with
                            # the final authoritative row and re-evaluate
                            # terminal reaction semantics.  If candidate and
                            # feedback target different Situations, retain
                            # both independent artifacts instead of replacing
                            # the candidate with the feedback target.
                            self._refresh_result_situation(
                                result,
                                final_situation,
                                owner_id=owner,
                                session_id=session,
                            )
                except Exception as exc:
                    feedback_result["situation_status"] = "degraded"
                    feedback_result["situation_error_type"] = type(exc).__name__
            result["feedback"] = feedback_result
        result["authority"] = _authority()
        return result

    def tick(
        self,
        *,
        owner_id: str | None = None,
        session_id: str | None = None,
        limit: int = 20,
        reason: str = "active_loop",
    ) -> dict[str, Any]:
        """Evaluate bounded exact-scope Situations and perform read-only work."""

        bound = max(1, min(int(limit), 20))
        scopes = self._scopes(owner_id=owner_id, session_id=session_id, limit=bound)
        evaluated: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for selected_owner, selected_session in scopes:
            if len(evaluated) >= bound:
                break
            try:
                status = self._source_status(selected_owner, selected_session)
                situations = self.core.list_situations(owner_id=selected_owner, session_id=selected_session, limit=bound)
                for situation in situations:
                    if len(evaluated) >= bound:
                        break
                    if str(situation.get("status") or (situation.get("semantic") or {}).get("lifecycle") or "") not in ACTIVE_SITUATION_STATUSES:
                        continue
                    # A successful source observation resolves its current
                    # Need.  Once its TTL/next observation boundary is due,
                    # reopen that exact Need generation through the durable
                    # InformationNeed writer so the context keeps observing
                    # without creating a new row on every scheduler tick.
                    self._ensure_observation_need(
                        situation,
                        owner_id=selected_owner,
                        session_id=selected_session,
                    )
                    needs = self.core.needs.list(
                        owner_id=selected_owner,
                        session_id=selected_session,
                        situation_id=str(situation.get("situation_id") or ""),
                        statuses=ACTIVE_NEED_STATUSES,
                        limit=32,
                    )
                    action = self._evaluate_situation(
                        situation,
                        needs,
                        owner_id=selected_owner,
                        session_id=selected_session,
                        source_status=status,
                    )
                    item: dict[str, Any] = {"situation_id": situation.get("situation_id"), "reaction": action}
                    decision = action.get("decision") if isinstance(action, dict) else {}
                    disposition = str(decision.get("disposition") or "") if isinstance(decision, dict) else ""
                    if disposition == "ask":
                        item["need"] = self._mark_asked(needs, situation, selected_owner, selected_session)
                    elif disposition == "read":
                        item["source"] = self._read_need(
                            situation,
                            needs,
                            selected_owner,
                            selected_session,
                            source_status=status,
                        )
                        applied = item["source"].get("applied") if isinstance(item["source"], dict) else None
                        if (
                            isinstance(applied, dict)
                            and isinstance(applied.get("situation"), dict)
                            # A source receipt never directly turns weather
                            # novelty into a suggestion. Weather is surfaced
                            # through the cognitive bridge; only an actionable
                            # Calendar material observation gets this direct
                            # reaction re-evaluation.
                            and str(applied.get("attention_trigger") or "") == "material_observation"
                        ):
                            current_needs = self.core.needs.list(
                                owner_id=selected_owner,
                                session_id=selected_session,
                                situation_id=str(situation.get("situation_id") or ""),
                                statuses=ACTIVE_NEED_STATUSES,
                                limit=32,
                            )
                            item["reevaluated"] = self._evaluate_situation(
                                applied["situation"],
                                current_needs,
                                owner_id=selected_owner,
                                session_id=selected_session,
                                source_status=status,
                                attention_trigger=str(applied.get("attention_trigger") or "none"),
                            )
                    conversation_reaction = item.get("reevaluated") or action
                    item["conversation"] = self._record_suggest_conversation(conversation_reaction)
                    evaluated.append(item)
            except Exception as exc:
                errors.append({"owner_id": selected_owner, "session_id": selected_session, "error_type": type(exc).__name__})
        return {
            "status": "degraded" if errors else "success",
            "reason": reason,
            "evaluated_count": len(evaluated),
            "error_count": len(errors),
            "items": evaluated[:bound],
            "errors": errors[:8],
            "authority": _authority(),
        }

    def _ensure_observation_need(
        self,
        situation: Mapping[str, Any],
        *,
        owner_id: str,
        session_id: str,
    ) -> dict[str, Any] | None:
        """Reopen one exact source Need after its observation boundary.

        The decision is server-owned and deliberately narrow: only a resolved
        Need whose latest successful typed receipt is stale (or whose
        Situation explicitly scheduled the next observation) is reopened.
        User-dismissed/answered Needs are never resurrected by the scheduler.
        """

        situation_id = str(situation.get("situation_id") or "")
        if not situation_id:
            return None
        semantic = situation.get("semantic") if isinstance(situation.get("semantic"), Mapping) else {}
        now = _now_utc(self._clock)
        scheduled = self._parse_time(semantic.get("next_observation_at"))
        situation_deadline = self._parse_time(semantic.get("deadline_at"))
        if situation_deadline is not None and situation_deadline <= now:
            return None
        all_needs = self.core.needs.list(
            owner_id=owner_id,
            session_id=session_id,
            situation_id=situation_id,
            limit=self.core.needs.max_needs_per_situation,
        )
        candidates = [
            row for row in all_needs
            if isinstance(row, Mapping)
            and str(row.get("status") or "") == "resolved"
            and str(row.get("evidence_kind") or "") in READABLE_SOURCES
            # A one-shot observation is terminal by design.  Only an
            # explicitly typed watch Need can be reopened by the scheduler;
            # this prevents every successful source read from becoming an
            # endless new blocker.
            and str(row.get("observation_mode") or "once").strip().lower() == "watch"
        ]
        if not candidates:
            return None

        # Use the latest raw receipt in the exact scope.  The public receipt
        # projection intentionally redacts stale payloads, so the scheduler
        # reads only bounded timing metadata from the server-owned snapshot.
        latest_by_need: dict[str, dict[str, Any]] = {}
        snapshot = self.source_runtime.state_snapshot()
        if isinstance(snapshot, Mapping) and snapshot.get("state_corrupt"):
            raise SourceStateCorruptError("living source state is corrupt")
        binding_rows = snapshot.get("bindings") if isinstance(snapshot.get("bindings"), Mapping) else {}
        for raw in (snapshot.get("receipts") or {}).values():
            if not isinstance(raw, Mapping):
                continue
            if str(raw.get("user_id") or "") != owner_id or str(raw.get("session_id") or "") != session_id:
                continue
            if str(raw.get("status") or "") not in {"ok", "empty"}:
                continue
            need_id = str(raw.get("need_id") or "")
            prior = latest_by_need.get(need_id)
            if prior is None or str(raw.get("observed_at") or "") > str(prior.get("observed_at") or ""):
                latest_by_need[need_id] = dict(raw)

        selected: Mapping[str, Any] | None = None
        selected_receipt: Mapping[str, Any] | None = None
        due_at: datetime | None = scheduled
        for need in candidates:
            receipt = latest_by_need.get(str(need.get("need_id") or ""))
            if receipt is None:
                continue
            # A receipt from an older Need generation cannot wake a newly
            # terminal Need.  Require the server-owned binding to match the
            # exact generation read above; otherwise this is a stale/no-op
            # observation boundary, not permission to create another generation.
            binding = binding_rows.get(str(receipt.get("binding_id") or ""))
            if not isinstance(binding, Mapping):
                raise SourceStateCorruptError("observation receipt binding is unavailable")
            if (
                int(binding.get("need_revision") or 0) != int(need.get("generation") or 0)
            ):
                continue
            receipt_due = self._parse_time(receipt.get("fresh_until"))
            if receipt_due is None:
                observed = self._parse_time(receipt.get("observed_at"))
                ttl = receipt.get("ttl_seconds")
                if observed is not None and isinstance(ttl, int) and not isinstance(ttl, bool) and ttl > 0:
                    receipt_due = observed + timedelta(seconds=min(ttl, 604800))
            if receipt_due is None:
                continue
            # Receipt freshness is an evidence-cache bound, not the watch
            # schedule. The typed source policy owns cadence; the orchestrator
            # only passes the source and Need projection through.
            cadence_seconds = self.source_policy.watch_cadence_seconds(
                str(need.get("evidence_kind") or ""),
                need=need,
            )
            if cadence_seconds is not None:
                observed = self._parse_time(receipt.get("observed_at"))
                if observed is not None:
                    receipt_due = max(receipt_due, observed + timedelta(seconds=cadence_seconds))
            receipt_payload = receipt.get("payload") if isinstance(receipt.get("payload"), Mapping) else {}
            receipt_facts = receipt_payload.get("facts") if isinstance(receipt_payload, Mapping) else {}
            next_eligible = self._parse_time(
                receipt_facts.get("next_eligible_at")
                if isinstance(receipt_facts, Mapping)
                else None
            )
            if next_eligible is not None:
                receipt_due = max(receipt_due, next_eligible)
            if scheduled is not None:
                receipt_due = max(receipt_due, scheduled)
            need_deadline = self._parse_time(need.get("expires_at"))
            if need_deadline is not None and receipt_due > need_deadline:
                continue
            if receipt_due <= now:
                selected = need
                selected_receipt = receipt
                due_at = receipt_due
                break
        if selected is None or due_at is None or due_at > now:
            return None

        need_id = str(selected.get("need_id") or "")
        receipt_id = str((selected_receipt or {}).get("receipt_id") or "")
        cycle_event = "observe_" + reaction_digest(
            "veyra.living_context.observation_cycle.v1",
            {
                "situation_id": situation_id,
                "need_id": need_id,
                "receipt_id": receipt_id,
                "boundary": due_at.isoformat(),
            },
        )[:32]
        try:
            record_digest = self.core.needs.record_digest_for_row(selected)
            # Preserve the typed target and observation mode across the
            # generation bump.  The model does not get a chance to recreate a
            # binding from prose during a background reopen.
            candidate_payload = {
                "blocked_judgment": str(selected.get("blocked_judgment") or "")[:480],
                "evidence_kind": str(selected.get("evidence_kind") or "other"),
                "why_now": str(selected.get("why_now") or "A fresh observation boundary has arrived.")[:480],
                "urgency": float(selected.get("urgency") or 0.0),
                "expires_at": selected.get("expires_at"),
                "allowed_source_classes": list(selected.get("allowed_source_classes") or []),
                "fallback_reaction": str(selected.get("fallback_reaction") or "wait"),
                "question": str(selected.get("question") or "")[:360],
            }
            model_fields = getattr(CandidateNeed, "model_fields", {})
            if "observation_mode" in model_fields:
                candidate_payload["observation_mode"] = str(selected.get("observation_mode") or "watch")
            if "evidence_target" in model_fields and isinstance(selected.get("evidence_target"), Mapping):
                candidate_payload["evidence_target"] = deepcopy(selected.get("evidence_target"))
            if "evidence_target_digest" in model_fields:
                digest = str(selected.get("evidence_target_digest") or "").strip()
                if digest:
                    candidate_payload["evidence_target_digest"] = digest
            candidate = CandidateNeed.model_validate(candidate_payload, strict=True)
            reopened = self.core.needs.upsert_for_situation(
                situation_id=situation_id,
                owner_id=owner_id,
                session_id=session_id,
                needs=[candidate],
                source_event_id=cycle_event,
                expected_generation=int(selected.get("generation") or 0),
                expected_status="resolved",
                expected_record_digest=record_digest,
                unknown_bindings={
                    str(selected.get("blocked_judgment") or ""): (
                        str(selected.get("unknown_binding") or "").strip() or None
                    )
                },
                evidence_targets=[
                    deepcopy(selected.get("evidence_target"))
                    if isinstance(selected.get("evidence_target"), Mapping)
                    else None
                ],
            )
            return reopened[0] if reopened else None
        except InformationNeedStaleReopen:
            # A malformed/changed Need must never make the background tick
            # mutate a different row or manufacture a replacement identity.
            return None

    @staticmethod
    def _parse_time(value: Any) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc)

    def record_feedback_from_turn(
        self,
        event: VeyraEvent,
        feedback: Any,
        *,
        catalog: list[dict[str, Any]] | None = None,
        target: dict[str, Any] | None = None,
        apply_situation: bool = True,
    ) -> dict[str, Any]:
        """Validate an exact turn-start reaction before learning."""

        owner = str(event.source.user_id or "local-user")
        session = str(event.source.session_id or "local-session")
        text = self._event_text(event)
        quote = feedback.source_quote
        if quote.end > len(text) or text[quote.start : quote.end] != quote.text:
            raise ValueError("reaction feedback source_quote is not source-bound")
        target = target or self._feedback_target(
            event,
            feedback,
            catalog if isinstance(catalog, list) else self.model_catalog(owner_id=owner, session_id=session, limit=16),
        )
        # The catalog is a turn-start binding aid, not a write lease.  Reuse
        # the same exact-reaction writer used by the Product API so control
        # labels cannot target a stale policy row and historical usefulness
        # remains descriptive only.
        return self.record_feedback_for_reaction(
            owner_id=owner,
            session_id=session,
            reaction_id=str(target["reaction_id"]),
            situation_revision=int(target["situation_revision"]),
            label=str(feedback.label),
            remind_before_seconds=feedback.remind_before_seconds,
            evidence_refs=[event.event_id],
            feedback_id=self._feedback_id(event.event_id, feedback.reaction_token, feedback.label),
            now=_now_utc(self._clock),
            apply_situation=apply_situation,
        )

    def record_feedback_for_reaction(
        self,
        *,
        owner_id: str,
        session_id: str,
        reaction_id: str,
        situation_revision: int,
        label: str,
        remind_before_seconds: int | None = None,
        evidence_refs: Iterable[str] | None = None,
        feedback_id: str | None = None,
        now: datetime | None = None,
        apply_situation: bool = True,
    ) -> dict[str, Any]:
        """Record Product feedback against a freshly re-read reaction.

        Product tokens are a hint, not a write lease.  The current Situation
        and reaction are re-read under the root writer fence, and both owner /
        session and revision must still match.  ``resolved`` then uses the
        existing Situation command path so the feedback and terminal update
        are returned together without rebinding a stale target.
        """

        owner = str(owner_id or "local-user")
        session = str(session_id or "local-session")
        selected_reaction_id = str(reaction_id or "")
        if not selected_reaction_id:
            raise ValueError("reaction_id is required")
        if isinstance(situation_revision, bool) or not isinstance(situation_revision, int) or situation_revision < 1:
            raise ValueError("situation_revision must be a positive integer")
        selected_label = str(label or "").strip().lower()
        if selected_label == "remind_offset":
            selected_label = "remind_before"
        if selected_label not in FEEDBACK_LABELS:
            raise ValueError("feedback label is unsupported")
        selected_refs = [str(item) for item in (evidence_refs or []) if str(item)]
        selected_now = now or _now_utc(self._clock)
        selected_feedback_id = feedback_id or (
            "product_feedback_"
            + reaction_digest(
                "veyra.product_feedback.v1",
                {
                    "owner_id": owner,
                    "session_id": session,
                    "reaction_id": selected_reaction_id,
                    "label": selected_label,
                    "situation_revision": situation_revision,
                },
            )[:32]
        )
        with self._root_writer_transaction():
            exact_getter = getattr(self.reaction_runtime, "get_reaction", None)
            reaction = (
                exact_getter(
                    selected_reaction_id,
                    owner_id=owner,
                    session_id=session,
                )
                if callable(exact_getter)
                else None
            )
            if not isinstance(reaction, Mapping):
                raise StateRevisionConflictError("reaction target is stale or unavailable")
            situation_id = str(reaction.get("situation_id") or "")
            current_situation = self.core.get_situation(
                situation_id,
                owner_id=owner,
                session_id=session,
            )
            if not isinstance(current_situation, Mapping):
                raise StateRevisionConflictError("reaction target Situation is unavailable")
            current_revision = int(current_situation.get("observation_revision") or 0)
            reaction_revision = int(reaction.get("situation_revision") or 0)
            if current_revision < 1 or reaction_revision < 1 or situation_revision != reaction_revision:
                raise StateRevisionConflictError("reaction is stale for the requested Situation revision")

            current_getter = getattr(self.reaction_runtime, "get_current_reaction", None)
            if not callable(current_getter):
                raise RuntimeError("current reaction reader is unavailable")
            current_reaction = current_getter(
                owner_id=owner,
                session_id=session,
                situation_id=situation_id,
                situation_revision=current_revision,
            )
            is_current_reaction = (
                isinstance(current_reaction, Mapping)
                and str(current_reaction.get("reaction_id") or "") == selected_reaction_id
                and reaction_revision == current_revision
            )
            historical_evaluation = not is_current_reaction
            if historical_evaluation and selected_label not in {"useful", "not_useful"}:
                raise StateRevisionConflictError("control feedback requires the current reaction")
            # Product Conversation is only a display mirror.  The durable
            # reaction feedback ledger—not a chat message—fences duplicate
            # labels and remains writable when the optional message is absent
            # or its ledger is incompatible.
            feedback_reader = getattr(self.reaction_runtime, "read_state", None)
            if not callable(feedback_reader):
                raise RuntimeError("reaction feedback reader is unavailable")
            feedback_state = feedback_reader()
            feedback_rows = feedback_state.get("feedback") if isinstance(feedback_state, Mapping) else None
            if not isinstance(feedback_rows, Mapping):
                raise RuntimeError("reaction feedback state is unavailable")
            existing_feedback = next(
                (
                    row
                    for row in feedback_rows.values()
                    if isinstance(row, Mapping)
                    and isinstance(row.get("semantics"), Mapping)
                    and str(row["semantics"].get("owner_id") or "") == owner
                    and str(row["semantics"].get("session_id") or "") == session
                    and str(row["semantics"].get("reaction_id") or "") == selected_reaction_id
                ),
                None,
            )
            if isinstance(existing_feedback, Mapping):
                existing_semantics = existing_feedback.get("semantics") or {}
                prior_label = str(existing_semantics.get("label") or "").strip().lower()
                if prior_label and prior_label != selected_label:
                    raise StateRevisionConflictError("feedback label is already bound to this reaction")
            payload = {
                "feedback_id": selected_feedback_id,
                "owner_id": owner,
                "session_id": session,
                "situation_id": situation_id,
                "reaction_id": selected_reaction_id,
                "label": selected_label,
                # Category is server-owned.  Client category is deliberately
                # not accepted as a learning key.
                "category": str(reaction.get("category") or "general"),
                "remind_before_seconds": remind_before_seconds,
                "evidence_refs": selected_refs,
                "now": selected_now.isoformat(),
            }
            recorded = self.reaction_runtime.record_feedback(
                payload,
                apply_situation_effect=not historical_evaluation,
                historical_evaluation=historical_evaluation,
            )
            result: dict[str, Any] = {
                **(dict(recorded) if isinstance(recorded, Mapping) else {"status": "recorded", "feedback": recorded}),
                "authority": _authority(),
                "historical_evaluation": historical_evaluation,
            }
            conversation_runtime = self.conversation_runtime
            feedback_writer = getattr(conversation_runtime, "update_reaction_feedback", None)
            if callable(feedback_writer):
                try:
                    conversation_result = feedback_writer(
                        selected_reaction_id,
                        owner_id=owner,
                        session_id=session,
                        label=selected_label,
                        feedback_at=selected_now.isoformat(),
                    )
                    if not isinstance(conversation_result, Mapping) or str(conversation_result.get("status") or "") in {"unavailable", "degraded", "error"}:
                        raise RuntimeError("conversation feedback mirror is unavailable")
                    result["conversation"] = conversation_result
                except Exception:
                    result["conversation"] = {
                        "status": "degraded",
                        "degraded": {
                            "status": "degraded",
                            "code": "product_conversation_feedback_mirror_unavailable",
                            "read_only": True,
                        },
                    }
            else:
                result["conversation"] = {
                    "status": "degraded",
                    "degraded": {
                        "status": "degraded",
                        "code": "product_conversation_feedback_mirror_unavailable",
                        "read_only": True,
                    },
                }
            if selected_label == "resolved" and apply_situation:
                command_event = self._event(
                    owner,
                    session,
                    EventType.USER_FEEDBACK,
                    {
                        "reaction_feedback": "resolved",
                        "reaction_id": selected_reaction_id,
                        "source_event_id": selected_feedback_id,
                    },
                    prefix="reaction_resolve",
                )
                result["situation"] = self._apply_resolved_feedback(command_event, dict(reaction))
            # Return a refreshable, server-owned reaction snapshot alongside
            # the feedback and updated Product Conversation message.  A
            # resolved Situation may advance its revision and therefore have
            # no current reaction yet; retain the target as the historical
            # reference in that case.
            refreshed_situation = result.get("situation") if isinstance(result.get("situation"), Mapping) else current_situation
            refreshed_revision = int(refreshed_situation.get("observation_revision") or current_revision) if isinstance(refreshed_situation, Mapping) else current_revision
            refreshed = current_getter(
                owner_id=owner,
                session_id=session,
                situation_id=situation_id,
                situation_revision=refreshed_revision,
            )
            returned_reaction = deepcopy(dict(refreshed if isinstance(refreshed, Mapping) else reaction))
            returned_reaction["feedback_available"] = False
            returned_reaction["feedback_label"] = selected_label
            returned_reaction["feedback_at"] = selected_now.isoformat()
            result["reaction"] = returned_reaction
            result["updated"] = {
                "message": result.get("conversation", {}).get("message") if isinstance(result.get("conversation"), Mapping) else None,
                "reaction": returned_reaction,
                "situation": result.get("situation") if isinstance(result.get("situation"), Mapping) else None,
            }
            return result

    def _feedback_target(
        self,
        event: VeyraEvent,
        feedback: Any,
        catalog: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Resolve a feedback token only in the exact turn-start catalog."""

        text = self._event_text(event)
        quote = feedback.source_quote
        if quote.end > len(text) or text[quote.start : quote.end] != quote.text:
            raise ValueError("reaction feedback source_quote is not source-bound")
        for row in catalog:
            reaction = row.get("reaction") if isinstance(row, dict) else None
            if (
                isinstance(reaction, dict)
                and reaction.get("reaction_token") == feedback.reaction_token
                and str(reaction.get("owner_id") or row.get("owner_id") or "") == str(event.source.user_id or "local-user")
                and str(reaction.get("session_id") or row.get("session_id") or "") == str(event.source.session_id or "local-session")
            ):
                return deepcopy(reaction)
        raise StateRevisionConflictError("reaction token is stale or outside the turn-start catalog")

    def _apply_resolved_feedback(self, event: VeyraEvent, target: dict[str, Any]) -> dict[str, Any] | None:
        owner = str(event.source.user_id or "local-user")
        session = str(event.source.session_id or "local-session")
        # Keep the current read and the CAS command under one root fence.  This
        # makes the returned Situation the exact state produced by this
        # feedback operation, while still allowing the nested command writer to
        # use its normal authority/CAS path.
        with self._root_writer_transaction():
            current = self.core.get_situation(str(target["situation_id"]), owner_id=owner, session_id=session)
            if current is None or str(current.get("status") or "") in {"resolved", "expired", "contradicted", "archived"}:
                return current
            command_event = self._event(
                owner,
                session,
                EventType.USER_FEEDBACK,
                {"reaction_feedback": "resolved", "reaction_id": target["reaction_id"], "source_event_id": event.event_id},
                prefix="reaction_resolve",
            )
            command_result = self.core.command_situation(
                command_event,
                str(target["situation_id"]),
                owner_id=owner,
                session_id=session,
                command="resolve",
                expected_revision=int(current.get("observation_revision") or 1),
                reason="user reaction feedback resolved this Situation",
            )
            # ``command_situation`` returns a command envelope.  The
            # orchestrator's feedback/result artifact exposes the authoritative
            # Situation row itself so callers cannot accidentally retain or
            # compare the pre-command active snapshot.
            if isinstance(command_result, dict) and isinstance(command_result.get("situation"), dict):
                return command_result["situation"]
            return command_result

    def _refresh_result_situation(
        self,
        result: dict[str, Any],
        situation: dict[str, Any],
        *,
        owner_id: str,
        session_id: str,
    ) -> None:
        """Project a post-command Situation into the returned turn artifact."""

        result["situation"] = deepcopy(situation)
        situation_id = str(situation.get("situation_id") or "")
        try:
            current_needs = self.core.needs.list(
                owner_id=owner_id,
                session_id=session_id,
                situation_id=situation_id,
                limit=32,
            )
        except Exception:
            current_needs = result.get("information_needs") if isinstance(result.get("information_needs"), list) else []
        result["information_needs"] = current_needs
        try:
            reaction = self._evaluate_situation(
                situation,
                current_needs,
                owner_id=owner_id,
                session_id=session_id,
            )
        except Exception as exc:
            # The authoritative Situation still wins.  Keep the artifact
            # bounded and explicit if a reaction projection is unavailable.
            result["reaction"] = {
                "status": "degraded",
                "reason": "final_situation_reaction_unavailable",
                "error_type": type(exc).__name__,
                "decision": None,
                "authority": _authority(),
            }
            result["current_reaction"] = None
        else:
            result["reaction"] = reaction
            result["current_reaction"] = reaction.get("decision") if isinstance(reaction, dict) else None

    def _root_writer_transaction(self) -> Any:
        """Return the authoritative state-root writer fence.

        Living Context's Situation, Need, and reaction ledgers all share one
        ``WorldStateStore`` root.  Feedback revision checks must use that root,
        not a per-ledger lock, or a concurrent Situation writer can race the
        check.  Fail closed if a non-composed core is supplied rather than
        silently dropping the fence.
        """

        state_store = getattr(self.core, "state_store", None)
        writer_transaction = getattr(state_store, "writer_transaction", None)
        if not callable(writer_transaction):
            raise RuntimeError("Living Context root writer transaction is unavailable")
        return writer_transaction()

    def source_status(self, *, owner_id: str, session_id: str) -> dict[str, Any]:
        return self._source_status(owner_id, session_id)

    def record_cognitive_suggestion(
        self,
        candidate: CognitiveSuggestionCandidate | Mapping[str, Any],
    ) -> dict[str, Any]:
        """Project one cognitive hypothesis into Living Reaction/Product Chat.

        This path deliberately does not call a Situation writer.  It binds the
        candidate to the current exact semantic revision, evaluates an
        ephemeral Situation-shaped reaction input, and then records only the
        reaction plus its optional Product Conversation message.  A stale
        candidate is a normal scheduler outcome and is never rebound to the
        latest revision.
        """

        try:
            if isinstance(candidate, CognitiveSuggestionCandidate):
                payload: Any = candidate.to_dict()
            elif isinstance(candidate, Mapping):
                payload = candidate
            else:
                model_dump = getattr(candidate, "model_dump", None)
                payload = model_dump(mode="json") if callable(model_dump) else candidate
            selected = CognitiveSuggestionCandidate.from_mapping(payload)
        except LivingReactionValidationError as exc:
            return {
                "status": "rejected",
                "reason": "invalid_cognitive_suggestion_candidate",
                "error_type": type(exc).__name__,
                "authority": _authority(),
                "record_only": True,
                "external_delivery": False,
                "reaction": None,
                "conversation": None,
            }

        owner = selected.owner_id
        session = selected.session_id

        # A cheap first read gives callers an immediate stale/rejected answer,
        # but it is only an admission hint.  The authoritative validation is
        # repeated after taking the root writer fence below.  This matters for
        # a background candidate racing a foreground Situation update.
        initial = self.core.get_situation(
            selected.situation_id,
            owner_id=owner,
            session_id=session,
        )
        initial_failure = self._validate_cognitive_candidate(selected, initial)
        if initial_failure is not None:
            return initial_failure

        # Situation, Living Reaction, and Product Conversation share one
        # state-root writer fence.  The reaction runtime and conversation
        # runtime use nested re-entrant mutations, so a reaction can remain
        # durable if the later chat append fails; the returned degraded result
        # is then safe for the bridge to replay with this exact candidate.
        try:
            with self._root_writer_transaction():
                current = self.core.get_situation(
                    selected.situation_id,
                    owner_id=owner,
                    session_id=session,
                )
                failure = self._validate_cognitive_candidate(selected, current)
                if failure is not None:
                    return failure

                semantic = current.get("semantic") if isinstance(current.get("semantic"), Mapping) else {}
                # This copy is intentionally never passed to a Situation
                # writer.  It is an explicitly prefixed hypothesis, so the
                # reaction explanation cannot render it as a user-reported
                # fact even when the candidate is surfaced as a suggestion.
                ephemeral = deepcopy(dict(current))
                ephemeral_semantic = deepcopy(dict(semantic))
                ephemeral_semantic["material_change"] = {
                    "statement": self._cognitive_hypothesis_statement(selected.statement),
                    "why_now": selected.why_now,
                    "epistemic_status": "hypothesis",
                    "is_fact": False,
                    "evidence_refs": list(selected.evidence_refs),
                    "candidate_id": selected.candidate_id,
                    "confidence": selected.confidence,
                    "revision": 1,
                }
                ephemeral["semantic"] = ephemeral_semantic
                reaction = self._evaluate_situation(
                    ephemeral,
                    [],
                    owner_id=owner,
                    session_id=session,
                    source_status={"capabilities": {}, "consent": {}},
                    attention_trigger="cognitive_hypothesis",
                    attention_candidate_id=selected.candidate_id,
                    next_step_override=selected.suggested_next_step,
                )
                decision = reaction.get("decision") if isinstance(reaction, Mapping) else None
                result = self._cognitive_candidate_result(
                    selected,
                    status=self._cognitive_reaction_status(reaction),
                    reason=str(reaction.get("reason") or "reaction_evaluated"),
                    reaction=reaction,
                )
                if not isinstance(decision, Mapping):
                    result["status"] = "degraded"
                    result["reason"] = "cognitive_reaction_unavailable"
                    return result

                disposition = str(decision.get("disposition") or "")
                if disposition == "suggest":
                    conversation = self._record_suggest_conversation(reaction)
                    result["conversation"] = conversation
                    conversation_status = str(conversation.get("status") or "")
                    if conversation_status not in {"recorded", "duplicate"}:
                        result["status"] = "degraded"
                        result["reason"] = "proactive_conversation_record_failed"
                        result["retryable"] = True
                    else:
                        # Preserve the reaction ledger's outcome.  A replay
                        # that only repairs a missing chat row is still a
                        # duplicate reaction, while append_message remains
                        # idempotent for a chat row that already exists.
                        result["status"] = (
                            "recorded"
                            if str(reaction.get("status") or "") == "recorded"
                            else "duplicate"
                        )
                elif disposition == "silent":
                    result["status"] = self._cognitive_silent_status(decision)
                    result["reason"] = str(decision.get("reason") or "suggestion_suppressed")
                else:
                    result["status"] = "degraded"
                    result["reason"] = "cognitive_candidate_did_not_produce_suggest"
                return result
        except Exception as exc:
            # Keep a durable reaction result visible when Product Conversation
            # fails after Living Reaction has committed.  The bridge/retry
            # caller can invoke this handler again; reaction idempotency then
            # finds the same row and record_reaction fills the missing chat.
            if "result" in locals() and isinstance(result, dict):
                result["status"] = "degraded"
                result["reason"] = "cognitive_delivery_failed"
                result["error_type"] = type(exc).__name__
                result["retryable"] = True
                conversation = result.get("conversation")
                if not isinstance(conversation, dict):
                    result["conversation"] = {
                        "status": "degraded",
                        "reason": "proactive_conversation_record_failed",
                        "error_type": type(exc).__name__,
                        "retryable": True,
                    }
                return result
            return self._cognitive_candidate_result(
                selected,
                status="degraded",
                reason="cognitive_delivery_failed",
            ) | {
                "error_type": type(exc).__name__,
                "retryable": True,
            }

    def _validate_cognitive_candidate(
        self,
        candidate: CognitiveSuggestionCandidate,
        current: Mapping[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Validate exact owner/session/revision/digest/token binding."""

        if not isinstance(current, Mapping):
            return self._cognitive_candidate_result(
                candidate,
                status="rejected",
                reason="situation_not_found",
            )
        if (
            str(current.get("situation_id") or "") != candidate.situation_id
            or str(current.get("user_id") or "") != candidate.owner_id
            or str(current.get("session_id") or "") != candidate.session_id
        ):
            return self._cognitive_candidate_result(
                candidate,
                status="rejected",
                reason="candidate_scope_mismatch",
            )
        raw_revision = current.get("observation_revision")
        if isinstance(raw_revision, bool) or not isinstance(raw_revision, int) or raw_revision < 1:
            return self._cognitive_candidate_result(
                candidate,
                status="rejected",
                reason="situation_revision_invalid",
            )
        current_revision = raw_revision
        semantic = current.get("semantic")
        if not isinstance(semantic, Mapping):
            return self._cognitive_candidate_result(
                candidate,
                status="rejected",
                reason="situation_semantic_invalid",
                current_revision=current_revision,
            )
        current_digest = reaction_digest(
            "veyra.cognitive_living_context.semantic.v1",
            semantic,
        )
        digest_reader = getattr(getattr(self.core, "admission", None), "digest", None)
        compatibility_digest = digest_reader(semantic) if callable(digest_reader) else None
        accepted_digests = {str(current_digest)}
        if compatibility_digest:
            accepted_digests.add(str(compatibility_digest))
        if current_revision != candidate.situation_revision or candidate.semantic_digest not in accepted_digests:
            return self._cognitive_candidate_result(
                candidate,
                status="rejected",
                reason="situation_revision_or_digest_mismatch",
                current_revision=current_revision,
                current_digest=str(current_digest),
            )
        raw_material_revision = semantic.get("material_revision")
        material_revision = (
            int(raw_material_revision)
            if isinstance(raw_material_revision, int)
            and not isinstance(raw_material_revision, bool)
            and raw_material_revision >= 0
            else 0
        )
        raw_material_digest = semantic.get("material_digest")
        material_digest = (
            str(raw_material_digest)
            if isinstance(raw_material_digest, str)
            and len(raw_material_digest) == 64
            else str(current_digest)
        )
        if (
            candidate.material_revision is not None
            and candidate.material_revision != material_revision
        ) or (
            candidate.material_digest is not None
            and candidate.material_digest != material_digest
        ):
            return self._cognitive_candidate_result(
                candidate,
                status="rejected",
                reason="situation_material_checkpoint_mismatch",
                current_revision=current_revision,
                current_digest=str(current_digest),
            )
        expected_tokens = {
            self._cognitive_change_token(candidate, material_revision, material_digest),
        }
        if compatibility_digest:
            expected_tokens.add(self._cognitive_change_token(candidate, material_revision, material_digest))
        if candidate.change_token not in expected_tokens:
            return self._cognitive_candidate_result(
                candidate,
                status="rejected",
                reason="situation_change_token_mismatch",
                current_revision=current_revision,
                current_digest=str(current_digest),
            )
        return None

    @staticmethod
    def _cognitive_change_token(
        candidate: CognitiveSuggestionCandidate,
        material_revision: int,
        material_digest: str,
    ) -> str:
        return "lcchg_" + reaction_digest(
            "veyra.cognitive_living_context.change_token.v1",
            {
                "owner_id": candidate.owner_id,
                "session_id": candidate.session_id,
                "situation_id": candidate.situation_id,
                "material_revision": material_revision,
                "material_digest": material_digest,
            },
        )[:32]

    @staticmethod
    def _cognitive_hypothesis_statement(statement: str) -> str:
        selected = str(statement or "").strip()
        prefix = "可能的变化："
        if selected.startswith(prefix):
            return selected[:1200]
        return f"{prefix}{selected}"[:1200]

    @staticmethod
    def _cognitive_silent_status(decision: Mapping[str, Any]) -> str:
        reason = str(decision.get("reason") or "").strip().lower()
        if reason in {
            "cognitive_hypothesis_cooldown",
            "quiet_hours",
            "feedback_cooldown",
            "feedback_suppression",
            "ignore",
            "resolved",
            "too_frequent",
        }:
            return "suppressed"
        return "silent"

    @classmethod
    def _cognitive_reaction_status(cls, reaction: Mapping[str, Any]) -> str:
        decision = reaction.get("decision") if isinstance(reaction.get("decision"), Mapping) else None
        if isinstance(decision, Mapping) and str(decision.get("disposition") or "") == "silent":
            return cls._cognitive_silent_status(decision)
        status = str(reaction.get("status") or "degraded")
        return status if status in {"recorded", "duplicate", "degraded"} else "degraded"

    @staticmethod
    def _cognitive_candidate_result(
        candidate: CognitiveSuggestionCandidate,
        *,
        status: str,
        reason: str,
        reaction: Mapping[str, Any] | None = None,
        current_revision: int | None = None,
        current_digest: str | None = None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": status,
            "reason": reason,
            "candidate_id": candidate.candidate_id,
            "situation_id": candidate.situation_id,
            "situation_revision": candidate.situation_revision,
            "candidate": candidate.to_dict(),
            "authority": _authority(),
            "record_only": True,
            "external_delivery": False,
            "reaction": None,
            "conversation": None,
        }
        if reaction is not None:
            result["reaction"] = deepcopy(dict(reaction))
        if current_revision is not None:
            result["current_situation_revision"] = current_revision
        if current_digest is not None:
            result["current_situation_digest"] = current_digest
        return result

    def grant_source_consent(
        self,
        source: str,
        *,
        owner_id: str,
        session_id: str,
        purpose: str = "Veyra V1 read-only Living Context",
        expected_generation: int = 0,
        consent_id: str | None = None,
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        selected = str(source or "").strip().lower()
        if selected not in CONSENT_SOURCES:
            raise ValueError("source consent is limited to calendar, weather, or public_web")
        current = self._source_status(owner_id, session_id).get("consent", {}).get(selected, {})
        current_generation = int(current.get("generation") or 0)
        if current_generation != int(expected_generation):
            raise StateRevisionConflictError("source consent generation is stale")
        now = _now_utc(self._clock)
        selected_id = consent_id or "consent_" + reaction_digest(
            "veyra.source.consent.id.v1", {"owner_id": owner_id, "session_id": session_id, "source": selected}
        )[:24]
        expiry = expires_at or canonical_utc(now + timedelta(days=30))
        consent = SourceConsent(
            consent_id=selected_id,
            user_id=str(owner_id),
            workspace_id=self.source_policy.workspace_id,
            session_id=str(session_id),
            source=selected,  # type: ignore[arg-type]
            purpose=str(purpose or "Veyra V1 read-only Living Context")[:600],
            granted_at=canonical_utc(now),
            expires_at=expiry,
            # The source runtime retains revoked/expired grants as audit rows;
            # every CAS-approved renewal therefore advances exactly one
            # generation instead of silently resetting to 1.
            generation=max(1, current_generation + 1),
        )
        persisted = self.source_runtime.grant_consent(consent)
        return {"status": "granted", "source": selected, "consent": persisted.to_dict(), "authority": _authority()}

    def revoke_source_consent(
        self,
        source: str,
        *,
        owner_id: str,
        session_id: str,
        expected_generation: int,
        consent_id: str | None = None,
    ) -> dict[str, Any]:
        selected = str(source or "").strip().lower()
        if selected not in CONSENT_SOURCES:
            raise ValueError("source consent is limited to calendar, weather, or public_web")
        snapshot = self.source_runtime.state_snapshot()
        rows = [
            row for row in (snapshot.get("consents") or {}).values()
            if isinstance(row, dict)
            and str(row.get("user_id") or "") == str(owner_id)
            and str(row.get("session_id") or "") == str(session_id)
            and str(row.get("source") or "") == selected
        ]
        if consent_id:
            rows = [row for row in rows if str(row.get("consent_id") or "") == str(consent_id)]
        current = max(rows, key=lambda row: int(row.get("generation") or 0), default=None)
        if current is None or int(current.get("generation") or 0) != int(expected_generation):
            raise StateRevisionConflictError("source consent generation is stale or unavailable")
        revoked = self.source_runtime.revoke_consent(str(current["consent_id"]), user_id=str(owner_id), session_id=str(session_id))
        return {"status": "revoked" if revoked else "unchanged", "source": selected, "consent_id": current["consent_id"], "authority": _authority()}

    def _record_suggest_conversation(self, reaction: Any) -> dict[str, Any]:
        """Record only a server-owned ``suggest`` reaction in Product Chat.

        Ask/read/wait/silent remain ledger-only reaction outcomes.  Any
        storage failure is returned as a bounded diagnostic so Product Chat
        cannot alter Living Context routing or execution semantics.
        """

        runtime = self.conversation_runtime
        decision = reaction.get("decision") if isinstance(reaction, Mapping) else None
        if runtime is None or not isinstance(decision, Mapping):
            return {
                "status": "degraded",
                "reason": "conversation_runtime_unavailable",
                "retryable": True,
            }
        if str(decision.get("disposition") or "") != "suggest":
            return {
                "status": "ignored",
                "reason": "reaction_disposition_not_suggest",
                "disposition": str(decision.get("disposition") or ""),
            }
        try:
            return runtime.record_reaction(decision)
        except Exception as exc:
            return {
                "status": "degraded",
                "reason": "proactive_conversation_record_failed",
                "error_type": type(exc).__name__,
            }

    def _evaluate_situation(
        self,
        situation: Mapping[str, Any],
        needs: Iterable[Mapping[str, Any]] | None,
        *,
        owner_id: str,
        session_id: str,
        source_status: Mapping[str, Any] | None = None,
        attention_trigger: str = "none",
        attention_candidate_id: str | None = None,
        next_step_override: str | None = None,
    ) -> dict[str, Any]:
        rows = [dict(row) for row in (needs or []) if isinstance(row, Mapping) and str(row.get("status") or "") in ACTIVE_NEED_STATUSES]
        selected_need = max(rows, key=lambda item: (float(item.get("urgency") or 0.0), str(item.get("updated_at") or "")), default=None)
        status = source_status or self._source_status(owner_id, session_id)
        capabilities = status.get("capabilities") if isinstance(status.get("capabilities"), dict) else {}
        consent = status.get("consent") if isinstance(status.get("consent"), dict) else {}
        # Reaction's source_availability input represents a permitted
        # background-read boundary.  A source may be attemptable while its
        # OS permission is still unknown: the first bounded, consented read
        # is how the provider establishes ready/denied.  Product capability
        # status keeps ``available`` reserved for confirmed ready state.
        availability = {
            name: bool(value.get("available") or value.get("can_request"))
            for name, value in capabilities.items()
            if isinstance(value, Mapping)
        }
        consented = {name: bool(value.get("granted")) for name, value in consent.items() if isinstance(value, Mapping)}
        now = _now_utc(self._clock)
        # A permitted source is only a usable read target when the server can
        # also derive a bounded request for this exact Need.  Without this the
        # reaction could report ``read`` for a source whose parameters never
        # resolve, which strands the Need: the read never runs and the user is
        # never asked.  Masking availability lets the existing policy fall back
        # to its declared ask/wait disposition instead.
        if selected_need is not None:
            candidate_source = self.source_policy.choose_source(selected_need)
            if candidate_source and availability.get(candidate_source) and not self.source_policy.can_resolve_parameters(
                candidate_source, situation=situation, need=selected_need, now=now
            ):
                availability[candidate_source] = False
        quiet = bool(self._quiet_hours_resolver(owner_id, session_id, now)) if self._quiet_hours_resolver else False
        reaction_situation = deepcopy(dict(situation))
        if next_step_override:
            raw_next_semantic = reaction_situation.get("semantic")
            if isinstance(raw_next_semantic, dict):
                raw_next_semantic = deepcopy(raw_next_semantic)
                raw_next_semantic["next_step"] = str(next_step_override)[:600]
                reaction_situation["semantic"] = raw_next_semantic
            else:
                reaction_situation["next_step"] = str(next_step_override)[:600]
        raw_semantic = reaction_situation.get("semantic")
        # ``next_observation_at`` is a scheduler boundary, not a user-facing
        # deadline.  The reaction policy accepts it as a deadline fallback for
        # legacy rows, so strip that fallback when no real deadline exists;
        # otherwise an empty successful read would immediately become a
        # spurious in-window suggestion.
        if isinstance(raw_semantic, dict) and not raw_semantic.get("deadline_at"):
            reaction_semantic = deepcopy(raw_semantic)
            reaction_semantic.pop("next_observation_at", None)
            reaction_situation["semantic"] = reaction_semantic
        reaction_situation.pop("next_observation_at", None)
        selected_attention_trigger = str(attention_trigger or "none").strip().lower() or "none"
        if selected_attention_trigger == "none":
            semantic_for_trigger = reaction_situation.get("semantic")
            assumptions = semantic_for_trigger.get("assumptions") if isinstance(semantic_for_trigger, dict) else []
            if isinstance(assumptions, list) and any(
                isinstance(item, Mapping)
                and item.get("source") == "calendar"
                and item.get("attention_trigger") == "material_observation"
                for item in assumptions
            ):
                selected_attention_trigger = "material_observation"
        value = ReactionInput.from_mapping(
            {
                "owner_id": owner_id,
                "session_id": session_id,
                "situation": reaction_situation,
                "information_need": selected_need,
                "now": now,
                "quiet_hours": quiet,
                "consent": consented,
                "source_availability": availability,
                # Only a trusted source-receipt path may set this value.  A
                # model/material_change alone must not outrank an ordinary
                # remaining InformationNeed.
                "attention_trigger": selected_attention_trigger,
                "attention_candidate_id": attention_candidate_id,
            }
        )
        evaluated = self.reaction_runtime.evaluate(value)
        decision = evaluated.get("decision") if isinstance(evaluated, dict) else None
        projected = self._project_reaction(decision) if isinstance(decision, dict) else None
        return {**(evaluated if isinstance(evaluated, dict) else {"status": "degraded"}), "decision": projected, "authority": _authority()}

    def _read_need(
        self,
        situation: Mapping[str, Any],
        needs: list[Mapping[str, Any]],
        owner_id: str,
        session_id: str,
        *,
        source_status: Mapping[str, Any],
    ) -> dict[str, Any]:
        selected = max(needs, key=lambda item: (float(item.get("urgency") or 0.0), str(item.get("updated_at") or "")), default=None)
        if selected is None:
            return {"status": "skipped", "reason": "no_active_need"}
        current_need = self.core.needs.get(str(selected.get("need_id") or ""), owner_id=owner_id, session_id=session_id)
        if current_need is None:
            return {"status": "degraded", "reason": "need_disappeared"}
        projection = self.core.needs.authoritative_projection(str(current_need["need_id"]), owner_id=owner_id, session_id=session_id)
        if projection is None:
            return {"status": "degraded", "reason": "need_projection_unavailable"}
        selected_need = {**current_need, **projection}
        next_eligible = self._next_eligible_at(
            need_id=str(current_need.get("need_id") or ""),
            owner_id=owner_id,
            session_id=session_id,
        )
        now = _now_utc(self._clock)
        if next_eligible is not None and next_eligible > now:
            return {
                "status": "waiting",
                "reason": "next_eligible_at",
                "next_eligible_at": next_eligible.isoformat(),
            }
        binding = self.source_policy.derive_binding(situation=situation, need=selected_need, now=_now_utc(self._clock))
        if binding is None:
            return {"status": "waiting", "reason": "no_safe_source_binding"}
        try:
            self.source_runtime.register_binding(binding)
            request = self.source_runtime.admit(binding.need_id, binding.source, user_id=owner_id, session_id=session_id, now=_now_utc(self._clock), binding_id=binding.binding_id)
            receipt = self.source_runtime.execute(request.request_id, user_id=owner_id, session_id=session_id, now=_now_utc(self._clock))
            current_receipt = self.source_runtime.get_receipt(receipt.receipt_id, user_id=owner_id, session_id=session_id, now=_now_utc(self._clock)) or receipt
            after = self.core.needs.authoritative_projection(binding.need_id, owner_id=owner_id, session_id=session_id)
            if after is None or int(after.get("generation") or 0) != binding.need_revision or str(after.get("record_digest") or "") != binding.record_digest:
                return {"status": "stale", "receipt": current_receipt.to_dict(), "reason": "need_changed_before_receipt_projection"}
            result: dict[str, Any] = {"status": current_receipt.status, "receipt": current_receipt.to_dict()}
            if current_receipt.status in {"ok", "empty", "unknown", "timeout", "unavailable", "denied", "expired", "stale", "revoked"}:
                event = self._event(owner_id, session_id, EventType.OBSERVATION, {"source_receipt_id": current_receipt.receipt_id, "need_id": binding.need_id, "source": binding.source, "status": current_receipt.status}, prefix="source_receipt")
                result["applied"] = self.core.apply_source_receipt(event, current_receipt, expected_generation=binding.need_revision)
            return result
        except Exception as exc:
            return {"status": "degraded", "reason": "source_read_failed", "error_type": type(exc).__name__}

    def _next_eligible_at(
        self,
        *,
        need_id: str,
        owner_id: str,
        session_id: str,
    ) -> datetime | None:
        """Read a typed empty-result retry boundary for one exact Need."""

        snapshot = self.source_runtime.state_snapshot()
        receipts = snapshot.get("receipts") if isinstance(snapshot, Mapping) else None
        latest: Mapping[str, Any] | None = None
        for raw in receipts.values() if isinstance(receipts, Mapping) else []:
            if not isinstance(raw, Mapping):
                continue
            if (
                str(raw.get("need_id") or "") != need_id
                or str(raw.get("user_id") or "") != owner_id
                or str(raw.get("session_id") or "") != session_id
                or str(raw.get("status") or "") != "empty"
            ):
                continue
            if latest is None or str(raw.get("observed_at") or "") > str(latest.get("observed_at") or ""):
                latest = raw
        if latest is None:
            return None
        payload = latest.get("payload") if isinstance(latest.get("payload"), Mapping) else {}
        facts = payload.get("facts") if isinstance(payload, Mapping) else {}
        return self._parse_time(
            facts.get("next_eligible_at") if isinstance(facts, Mapping) else None
        )

    def _mark_asked(self, needs: list[Mapping[str, Any]], situation: Mapping[str, Any], owner: str, session: str) -> dict[str, Any] | None:
        selected = max(needs, key=lambda item: (float(item.get("urgency") or 0.0), str(item.get("updated_at") or "")), default=None)
        if selected is None:
            return None
        event_id = "asked_" + reaction_digest("veyra.living_context.asked.v1", {"situation_id": situation.get("situation_id"), "need_id": selected.get("need_id"), "generation": selected.get("generation")})[:24]
        return self.core.needs.mark_asked(
            str(selected["need_id"]),
            owner_id=owner,
            session_id=session,
            event_id=event_id,
            expected_generation=int(selected.get("generation") or 0),
        )

    def _scopes(self, *, owner_id: str | None, session_id: str | None, limit: int) -> list[tuple[str, str]]:
        if (owner_id is None) != (session_id is None):
            raise ValueError("owner_id and session_id must be supplied together")
        if owner_id is not None and session_id is not None:
            return [(str(owner_id), str(session_id))]
        rows = self.core.situations.list_semantic(limit=limit)
        scopes: list[tuple[str, str]] = []
        for row in rows:
            scope = (str(row.get("user_id") or ""), str(row.get("session_id") or ""))
            if all(scope) and scope not in scopes:
                scopes.append(scope)
        return scopes[:limit]

    def _source_status(self, owner: str, session: str) -> dict[str, Any]:
        try:
            value = self.source_runtime.status(user_id=owner, session_id=session, now=_now_utc(self._clock))
            if not isinstance(value, dict):
                return {"status": "degraded"}
            # ``status`` intentionally omits private receipt payloads, but the
            # opaque server-issued consent id is needed by Product revoke/CAS.
            # Project only the current exact-scope id; never expose a foreign
            # consent row or provider credentials.
            consent_projection = value.get("consent")
            if isinstance(consent_projection, dict):
                try:
                    snapshot = self.source_runtime.state_snapshot()
                    rows = snapshot.get("consents", {}) if isinstance(snapshot, dict) else {}
                    for source, projection in consent_projection.items():
                        if not isinstance(projection, dict):
                            continue
                        matches = [
                            row
                            for row in rows.values()
                            if isinstance(row, Mapping)
                            and str(row.get("user_id") or "") == str(owner)
                            and str(row.get("session_id") or "") == str(session)
                            and str(row.get("source") or "") == str(source)
                            and int(row.get("generation") or 0) == int(projection.get("generation") or 0)
                        ]
                        current = max(matches, key=lambda row: int(row.get("generation") or 0), default=None)
                        if current is not None:
                            projection["consent_id"] = str(current.get("consent_id") or "")
                except Exception:
                    # Status remains useful without an id; revoke will fail
                    # closed rather than guessing across scopes.
                    pass
            return value
        except Exception as exc:
            return {"status": "degraded", "error_type": type(exc).__name__, "capabilities": {}, "consent": {}}

    def _current_reaction(
        self,
        *,
        owner_id: str,
        session_id: str,
        situation_id: str,
        situation_revision: int,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        try:
            exact_reader = getattr(self.reaction_runtime, "get_current_reaction", None)
            if callable(exact_reader):
                current = exact_reader(
                    owner_id=owner_id,
                    session_id=session_id,
                    situation_id=situation_id,
                    situation_revision=situation_revision,
                )
                if current is None:
                    return None, {"status": "empty"}
                return self._project_reaction(current), {"status": "success"}
            # Compatibility for an older injected reaction runtime.  Keep the
            # exact revision predicate and use the largest bounded history
            # page available; never choose a stale row as a substitute.
            rows = self.reaction_runtime.list_reactions(
                owner_id=owner_id,
                session_id=session_id,
                situation_id=situation_id,
                limit=10000,
            )
        except Exception:
            # A malformed reaction ledger is not the same thing as a clean
            # absence of a reaction.  Keep the model catalog usable for
            # Situation admission, but carry a bounded typed diagnostic to
            # the Awareness/Product boundary.
            return None, {
                "status": "degraded",
                "code": "reaction_state_integrity_unavailable",
                "read_only": True,
            }
        matching = [row for row in rows if int(row.get("situation_revision") or 0) == int(situation_revision)]
        if not matching:
            return None, {"status": "empty"}
        matching.sort(
            key=lambda row: (
                int(row.get("policy_revision") or 0),
                str(row.get("created_at") or ""),
                str(row.get("reaction_id") or ""),
            ),
            reverse=True,
        )
        return self._project_reaction(matching[0]), {"status": "success"}

    @staticmethod
    def _project_reaction(row: Mapping[str, Any]) -> dict[str, Any]:
        value = deepcopy(dict(row))
        revision = max(1, int(value.get("policy_revision") or 1))
        token = "rxn_" + reaction_digest(
            "veyra.living_reaction.catalog.v1",
            {"reaction_id": value.get("reaction_id"), "situation_revision": value.get("situation_revision"), "policy_revision": revision},
        )[:32]
        value["reaction_token"] = token
        value["reaction_revision"] = revision
        return value

    @staticmethod
    def _feedback_candidate(understanding: Any) -> Any | None:
        candidate = getattr(understanding, "living_reaction_feedback", None)
        return candidate if candidate is not None and not getattr(understanding, "living_reaction_feedback_issues", []) else None

    @staticmethod
    def _event_text(event: VeyraEvent) -> str:
        payload = event.payload if isinstance(event.payload, dict) else {}
        return str(payload.get("text") or payload.get("message") or "").strip()[:480]

    @staticmethod
    def _feedback_id(event_id: str, token: str, label: str) -> str:
        return "feedback_" + hashlib.sha256(f"{event_id}\0{token}\0{label}".encode()).hexdigest()[:32]

    def _event(self, owner: str, session: str, event_type: EventType, payload: dict[str, Any], *, prefix: str) -> VeyraEvent:
        event_id = prefix + "_" + reaction_digest("veyra.living_context.internal_event.v1", {"owner": owner, "session": session, "payload": payload})[:32]
        now = canonical_utc(_now_utc(self._clock))
        return VeyraEvent(
            type=event_type,
            source=EventSource(channel="veyra_living_context", user_id=owner, session_id=session),
            payload=payload,
            event_id=event_id,
            evidence_refs=[event_id],
            timestamp=now,
            occurred_at=now,
            received_at=now,
        )


__all__ = ["LivingContextOrchestrator"]
