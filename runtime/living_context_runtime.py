"""Coordinate semantic user Situations and InformationNeeds.

This module is intentionally a coordinator, not a second state store.  The
authoritative Situation is written only by ``SemanticSituationRuntime`` over
``SituationStateRepository``; needs are written only by
``InformationNeedRuntime``. Model output is admitted only after the strict
contract and exact owner/session checks pass.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any, Callable

from core.situation_state_repository import SituationStateRepository
from core.world_state import StateRevisionConflictError, WorldStateStore
from core.living_context_information_state_policy import has_clearly_resolved_statement
from interface.event_schema import EventType, VeyraEvent
from interface.living_context_contract import (
    CandidateNeed,
    LivingContextCandidate,
    parse_living_context_candidate,
    situation_catalog_projection,
    stable_subject_digest,
)
from runtime.information_need_runtime import InformationNeedRuntime
from runtime.living_context_admission import LivingContextAdmissionLedger
from runtime.living_context_commands import (
    answer_need as answer_need_command,
    apply_source_receipt as apply_source_receipt_command,
    command_situation as command_situation_command,
    dedupe_records,
    dedupe_text,
    preflight_need_scope,
    reaction_hint,
)
from runtime.semantic_situation_runtime import SemanticSituationRuntime


class LivingContextRuntime:
    """First vertical slice for long-lived, user-visible semantic Situations."""

    def __init__(
        self,
        state_store: WorldStateStore,
        *,
        situation_evaluator: Any | None = None,
        information_need_runtime: InformationNeedRuntime | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.state_store = state_store
        # Semantic CRUD is deliberately separate from event-scoped legacy
        # observation projection.  The optional evaluator argument is retained
        # for callers that still pass it, but never becomes a second writer.
        self.situations = SemanticSituationRuntime(
            state_store,
            repository=SituationStateRepository(state_store),
        )
        self.needs = information_need_runtime or InformationNeedRuntime(state_store)
        self.admission = LivingContextAdmissionLedger(state_store)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def model_catalog(
        self,
        *,
        owner_id: str,
        session_id: str,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        """Return exact-owner, server-issued Situation tokens for one turn.

        The catalog is intentionally a projection.  It carries no source
        query, URL, path, command, or authority and is the only place where a
        model may obtain an existing Situation token or InformationNeed
        generation.
        """

        selected_owner = str(owner_id or "").strip() or "local-user"
        selected_session = str(session_id or "").strip() or "local-session"
        rows = self.list_situations(
            owner_id=selected_owner,
            session_id=selected_session,
            limit=max(0, min(int(limit), 16)),
        )
        catalog: list[dict[str, Any]] = []
        for situation in rows:
            open_needs = self.needs.list(
                owner_id=selected_owner,
                session_id=selected_session,
                situation_id=str(situation.get("situation_id") or ""),
                statuses={"open", "asked", "observing", "waiting"},
                limit=8,
            )
            catalog.append(
                situation_catalog_projection(situation, open_needs=open_needs)
            )
        return catalog

    def process_user_turn(
        self,
        event: VeyraEvent,
        understanding: Any,
        *,
        expected_revision: int | None = None,
        catalog: list[dict[str, Any]] | None = None,
        server_command: bool = False,
    ) -> dict[str, Any]:
        """Admit one strict candidate and derive an in-app reaction plan."""

        if event.type != EventType.USER_MESSAGE:
            return {"status": "ignored", "reason": "not_user_message"}
        candidate, issues = self._candidate(understanding)
        if candidate is None:
            return {
                "status": "quiet",
                "reason": "no_valid_living_context_candidate",
                "validation_issues": issues[:4],
            }
        if candidate.disposition == "quiet":
            return {"status": "quiet", "reason": "candidate_quiet"}
        self._validate_quotes(candidate, self._event_text(event))

        owner_id = str(event.source.user_id or "").strip() or "local-user"
        session_id = str(event.source.session_id or "").strip() or "local-session"
        existing: dict[str, Any] | None = None
        catalog_row: dict[str, Any] | None = None
        # Freeze the exact row supplied at turn start for candidate Need
        # deduplication.  Admission may read a fresh row for CAS, but Need
        # identity must not change when live state changes during this event.
        turn_start_catalog_row = self._turn_start_catalog_row(candidate, catalog)
        admission_recovery: dict[str, Any] | None = None
        if candidate.situation_token:
            existing = self.situations.get_semantic(
                candidate.situation_token,
                user_id=owner_id,
                session_id=session_id,
            )
            if existing is None:
                raise KeyError("candidate Situation token is not valid for this owner/session")
            try:
                catalog_row = self._catalog_binding(candidate, catalog, owner_id, session_id)
            except StateRevisionConflictError:
                # Any cross-file admission can be interrupted after the
                # semantic writer advances the Situation.  Its original
                # catalog is then necessarily one revision behind.  Allow
                # only the exact durable admission row to resume; unrelated
                # stale/rebound catalogs remain fail-closed.
                admission_recovery = self._prepared_admission_recovery(
                    event=event,
                    candidate=candidate,
                    catalog=catalog,
                    existing=existing,
                    owner_id=owner_id,
                    session_id=session_id,
                )
                if admission_recovery is None:
                    raise
                catalog_row = admission_recovery.get("catalog_row")
        situation_id = str(existing.get("situation_id") or "") if existing else None
        candidate, unknown_resolved_count = self._suppress_resolved_unknown_candidates(candidate)
        candidate, resolved_need_count = self._suppress_resolved_need_candidates(candidate)
        candidate, deduped_need_count = self._dedupe_active_need_candidates(
            candidate,
            situation_id=situation_id,
            catalog_row=turn_start_catalog_row,
        )
        if candidate.disposition == "create":
            subject_key = stable_subject_digest(
                f"{owner_id}\0{session_id}",
                candidate.create_subject,
            )
            operation = "create"
        else:
            subject_key = str(existing.get("semantic_subject_key") or "")
            operation = candidate.disposition
        validated_needs = self.needs.validate_candidates(candidate.needs)
        answered_needs: list[dict[str, Any]] = []
        for need_token in candidate.answered_need_tokens:
            need = self.needs.get(
                need_token,
                owner_id=owner_id,
                session_id=session_id,
            )
            if need is None or str(need.get("situation_id") or "") != str(situation_id or ""):
                raise KeyError("answered InformationNeed token is outside the Situation scope")
            answered_needs.append(need)
        if candidate.answered_need_tokens:
            self._validate_need_bindings(candidate, catalog_row)
        preflight_need_scope(
            self,
            owner_id=owner_id,
            session_id=session_id,
            situation_id=(
                situation_id
                or self.situations.semantic_situation_id(
                    user_id=owner_id,
                    subject_key=subject_key,
                )
            ),
            needs=validated_needs,
            answered_needs=answered_needs,
        )
        semantic_state = self._semantic_snapshot(event, candidate, existing)
        unknown_bindings = self._derive_unknown_bindings(
            candidate=candidate,
            semantic_state=semantic_state,
            situation_id=situation_id,
            owner_id=owner_id,
            session_id=session_id,
        )
        if answered_needs:
            # An answered Need may only clear the exact semantic endpoint
            # admitted in its server-owned ``unknown_binding``.  The Need's
            # human-readable blocked judgment is not an endpoint: it can be
            # a paraphrase, and legacy/unbound Needs must not guess which of
            # several unknowns a user answer resolved.
            resolved_bindings = {
                str(item.get("unknown_binding") or "").strip()
                for item in answered_needs
                if str(item.get("unknown_binding") or "").strip()
            }
            semantic_state["unknown"] = [
                item
                for item in semantic_state.get("unknown", [])
                if str(item) not in resolved_bindings
            ]
        persisted, current_needs, needs_repaired = self._persist_semantic_and_needs(
            event=event,
            subject_key=subject_key,
            semantic_state=semantic_state,
            situation_id=situation_id,
            operation=operation,
            expected_revision=expected_revision,
            candidate=candidate,
            validated_needs=validated_needs,
            answered_needs=answered_needs,
            owner_id=owner_id,
            session_id=session_id,
            existing=existing,
            server_command=server_command,
            unknown_bindings=unknown_bindings,
            planned_situation_id=(
                situation_id
                or self.situations.semantic_situation_id(
                    user_id=owner_id,
                    subject_key=subject_key,
                )
            ),
            admission_recovery=admission_recovery,
        )
        situation_id = str(persisted["situation_id"])
        reaction = reaction_hint(
            situation=persisted,
            needs=current_needs,
            requested_kind=candidate.requested_reaction,
        )
        result = {
            "status": "replayed" if persisted.get("semantic_replayed") else "recorded",
            "operation": operation,
            "semantic_replayed": bool(persisted.get("semantic_replayed")),
            "needs_repaired": needs_repaired,
            "unknown_candidates_resolved_suppressed": unknown_resolved_count,
            "need_candidates_resolved_suppressed": resolved_need_count,
            "need_candidates_deduped": deduped_need_count,
            "situation": persisted,
            "information_needs": current_needs,
            "reaction_hint": reaction.model_dump(mode="json") if reaction else None,
            "reaction": None,
            "authority": {
                "route": False,
                "risk": False,
                "tool": False,
                "execution": False,
                "delivery": False,
            },
        }
        return result

    @staticmethod
    def _suppress_resolved_unknown_candidates(
        candidate: LivingContextCandidate,
    ) -> tuple[LivingContextCandidate, int]:
        """Drop only candidate unknown statements that are clearly resolved.

        The information-state policy is intentionally text-only and generic.
        Candidate order is retained for every unresolved statement, including
        mixed statements where an unresolved marker takes precedence.  The
        contract bounds ``unknown`` to twelve rows, so the returned count is
        bounded by the candidate contract as well.
        """

        if not candidate.unknown:
            return candidate, 0
        retained = [
            statement
            for statement in candidate.unknown
            if not has_clearly_resolved_statement(statement)
        ]
        suppressed = len(candidate.unknown) - len(retained)
        if suppressed == 0:
            return candidate, 0
        return candidate.model_copy(update={"unknown": retained}), suppressed

    @staticmethod
    def _suppress_resolved_need_candidates(
        candidate: LivingContextCandidate,
    ) -> tuple[LivingContextCandidate, int]:
        """Drop only Need rows whose endpoint is explicitly resolved.

        The policy is generic and text-only.  CandidateNeed rows are copied
        unchanged when retained; only the outer ``needs`` collection is
        projected.  The contract bounds this collection to eight rows, so the
        returned count is inherently bounded by the candidate contract.
        """

        if not candidate.needs:
            return candidate, 0
        retained = [
            need
            for need in candidate.needs
            if not has_clearly_resolved_statement(need.blocked_judgment)
        ]
        suppressed = len(candidate.needs) - len(retained)
        if suppressed == 0:
            return candidate, 0
        return candidate.model_copy(update={"needs": retained}), suppressed

    def _dedupe_active_need_candidates(
        self,
        candidate: LivingContextCandidate,
        *,
        situation_id: str | None,
        catalog_row: dict[str, Any] | None,
    ) -> tuple[LivingContextCandidate, int]:
        """Drop only exact active Need endpoint duplicates.

        ``InformationNeedRuntime`` historically includes ``evidence_kind`` in
        its stable ID.  That can create a second active Need when a model
        re-emits the same server-owned endpoint with a different source class.
        The only durable comparison allowed here is the immutable turn-start
        catalog row's exact active ``blocked_judgment``.  Same-event duplicate
        endpoints are also reduced in order, retaining the first candidate.
        Terminal Needs are absent from ``open_needs`` by design, so the normal
        generation reopen path remains available.
        """

        if not candidate.needs:
            return candidate, 0
        active_endpoint_ids: dict[str, set[str]] = {}
        if isinstance(catalog_row, dict):
            open_needs = catalog_row.get("open_needs")
            if isinstance(open_needs, list):
                for raw_need in open_needs:
                    if not isinstance(raw_need, dict):
                        continue
                    if str(raw_need.get("status") or "open") not in {"open", "asked", "observing", "waiting"}:
                        continue
                    endpoint = str(raw_need.get("blocked_judgment") or "").strip()
                    need_token = str(raw_need.get("need_token") or "").strip()
                    if endpoint:
                        active_endpoint_ids.setdefault(endpoint, set()).add(need_token)

        retained: list[CandidateNeed] = []
        retained_endpoints: set[str] = set()
        dropped = 0
        for need in candidate.needs:
            endpoint = str(need.blocked_judgment or "").strip()
            candidate_need_id = self.needs.stable_need_id(
                situation_id=str(situation_id or ""),
                blocked_judgment=need.blocked_judgment,
                evidence_kind=need.evidence_kind,
            )
            existing_ids = active_endpoint_ids.get(endpoint, set())
            duplicate_active_endpoint = bool(
                endpoint
                and existing_ids
                and candidate_need_id not in existing_ids
            )
            duplicate_same_turn_endpoint = bool(
                endpoint
                and endpoint in retained_endpoints
            )
            if duplicate_active_endpoint or duplicate_same_turn_endpoint:
                dropped += 1
                continue
            retained.append(need)
            if endpoint:
                retained_endpoints.add(endpoint)
        if dropped == 0:
            return candidate, 0
        return candidate.model_copy(update={"needs": retained}), dropped

    @staticmethod
    def _turn_start_catalog_row(
        candidate: LivingContextCandidate,
        catalog: list[dict[str, Any]] | None,
    ) -> dict[str, Any] | None:
        """Return one frozen catalog row matching the candidate's full triple."""

        if candidate.disposition not in {"update", "correct", "resolve"}:
            return None
        matches: list[dict[str, Any]] = []
        for raw_row in catalog or []:
            if not isinstance(raw_row, dict):
                continue
            row_revision = raw_row.get("observation_revision")
            if row_revision is None:
                row_revision = raw_row.get("situation_revision")
            if (
                str(raw_row.get("situation_token") or "") == str(candidate.situation_token or "")
                and type(row_revision) is int
                and int(row_revision) == int(candidate.situation_revision or 0)
                and str(raw_row.get("catalog_token") or "") == str(candidate.catalog_token or "")
            ):
                matches.append(copy.deepcopy(raw_row))
        return matches[0] if len(matches) == 1 else None

    def _persist_semantic_and_needs(
        self,
        *,
        event: VeyraEvent,
        subject_key: str,
        semantic_state: dict[str, Any],
        situation_id: str | None,
        operation: str,
        expected_revision: int | None,
        candidate: LivingContextCandidate,
        validated_needs: list[CandidateNeed],
        answered_needs: list[dict[str, Any]],
        owner_id: str,
        session_id: str,
        existing: dict[str, Any] | None,
        server_command: bool,
        unknown_bindings: dict[str, str | None],
        planned_situation_id: str,
        admission_recovery: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], bool]:
        """Commit Situation and its needs under one root writer fence.

        The evaluator and need runtime remain the two authoritative writers;
        this fence only makes the cross-file admission window repairable and
        prevents two turns from validating the same revision concurrently.
        """

        with self.state_store.writer_transaction():
            semantic_digest = self.admission.digest(semantic_state)
            need_plan_digest = self.admission.digest(
                [item.model_dump(mode="json") for item in validated_needs]
            )
            answered_generations = {
                str(item.get("need_id") or ""): int(item.get("generation") or 1)
                for item in answered_needs
            }
            prior_admission = self.admission.get(event.event_id)
            expected_admission_situation_revision = (
                int(prior_admission.get("expected_situation_revision"))
                if isinstance(prior_admission, dict)
                and prior_admission.get("expected_situation_revision") is not None
                else (
                    int(candidate.situation_revision)
                    if candidate.situation_revision is not None
                    else (
                        int(existing.get("observation_revision") or 1)
                        if isinstance(existing, dict) and operation != "create"
                        else None
                    )
                )
            )
            expected_admission_need_revision = (
                int(prior_admission.get("expected_need_state_revision"))
                if isinstance(prior_admission, dict)
                and prior_admission.get("expected_need_state_revision") is not None
                else self._need_state_revision()
            )
            if admission_recovery is not None:
                # Re-check the recovery preconditions while the root writer
                # fence is held.  The catalog bypass is deliberately narrow;
                # the admission ledger still owns the exact content check
                # below (including semantic/Need digests and generations).
                current_admission = self.admission.get(event.event_id)
                if not self._valid_admission_recovery(
                    event=event,
                    candidate=candidate,
                    current_admission=current_admission,
                    owner_id=owner_id,
                    session_id=session_id,
                    situation_id=planned_situation_id,
                ):
                    raise StateRevisionConflictError(
                        "terminal Situation admission repair is no longer eligible"
                    )
            admission_record = self.admission.prepare(
                event_id=event.event_id,
                owner_id=owner_id,
                session_id=session_id,
                situation_id=planned_situation_id,
                operation=operation,
                semantic_digest=semantic_digest,
                need_plan_digest=need_plan_digest,
                answered_need_generations=answered_generations,
                event_timestamp=event.timestamp,
                expected_situation_revision=expected_admission_situation_revision,
                expected_need_state_revision=expected_admission_need_revision,
            )
            if admission_record.get("replayed"):
                # A committed event is already durable.  Return the current
                # authoritative Situation/Need projection without replaying
                # either writer or touching the admission ledger again.
                replayed = self.situations.get_semantic(
                    planned_situation_id,
                    user_id=owner_id,
                    session_id=session_id,
                )
                if replayed is None:
                    raise StateRevisionConflictError(
                        "committed admission has no current Situation record"
                    )
                replayed = copy.deepcopy(replayed)
                replayed["semantic_replayed"] = True
                current_needs = self.needs.list(
                    owner_id=owner_id,
                    session_id=session_id,
                    situation_id=planned_situation_id,
                    limit=self.needs.max_needs_per_situation,
                )
                return replayed, current_needs, True
            admission_phase = str(admission_record.get("phase") or "prepared")
            phase_rank = self.admission.phase_rank(admission_phase)
            fresh_existing = existing
            if situation_id:
                fresh_existing = self.situations.get_semantic(
                    situation_id,
                    user_id=owner_id,
                    session_id=session_id,
                )
                if fresh_existing is None:
                    raise KeyError("candidate Situation disappeared before CAS")
                semantic_already_applied = self._semantic_event_matches(
                    fresh_existing,
                    event_id=event.event_id,
                    semantic_digest=semantic_digest,
                    expected_revision=admission_record.get("result_situation_revision"),
                )
                if semantic_already_applied and phase_rank < 1:
                    self.admission.mark_phase(
                        event_id=event.event_id,
                        phase="semantic_applied",
                        semantic_digest=semantic_digest,
                        need_plan_digest=need_plan_digest,
                        result_situation_revision=int(fresh_existing.get("observation_revision") or 0),
                    )
                    phase_rank = 1
                if phase_rank >= 1 and not semantic_already_applied:
                    raise StateRevisionConflictError("admission semantic phase does not match current Situation")
                if (
                    not semantic_already_applied
                    and expected_revision is None
                    and existing is not None
                    and int(fresh_existing.get("observation_revision") or 1)
                    != int(existing.get("observation_revision") or 1)
                ):
                    raise StateRevisionConflictError(
                        "Situation changed while the turn was being admitted"
                    )
            else:
                semantic_already_applied = False
                if phase_rank >= 1 or operation == "create":
                    fresh_existing = self.situations.get_semantic(
                        planned_situation_id,
                        user_id=owner_id,
                        session_id=session_id,
                    )
                    semantic_already_applied = self._semantic_event_matches(
                        fresh_existing,
                        event_id=event.event_id,
                        semantic_digest=semantic_digest,
                        expected_revision=admission_record.get("result_situation_revision"),
                    )
                    if not semantic_already_applied:
                        if phase_rank >= 1:
                            raise StateRevisionConflictError("admission semantic phase does not match current Situation")
                    elif phase_rank < 1:
                        self.admission.mark_phase(
                            event_id=event.event_id,
                            phase="semantic_applied",
                            semantic_digest=semantic_digest,
                            need_plan_digest=need_plan_digest,
                            result_situation_revision=int(fresh_existing.get("observation_revision") or 0),
                        )
                        phase_rank = 1
            current_revision = (
                int(fresh_existing.get("observation_revision") or 1)
                if fresh_existing is not None
                else None
            )
            expected_need_revision: int | None = None
            if validated_needs and phase_rank < 2:
                # Capacity and replay binding are checked while the same root
                # writer fence is held, immediately before Situation truth is
                # written.  The returned Need revision becomes the second CAS
                # guard for the subsequent Need commit.
                expected_need_revision = self.needs.preflight_upsert(
                    situation_id=planned_situation_id,
                    owner_id=owner_id,
                    session_id=session_id,
                    needs=validated_needs,
                    source_event_id=event.event_id,
                    unknown_bindings=unknown_bindings,
                )
            if semantic_already_applied:
                persisted = copy.deepcopy(fresh_existing)
                persisted["semantic_replayed"] = True
                selected_situation_id = str(persisted["situation_id"])
            else:
                persisted = self.situations.record_semantic(
                    event,
                    subject_key=subject_key,
                    semantic_state=semantic_state,
                    situation_id=situation_id,
                    operation=operation,
                    expected_revision=(
                        expected_revision
                        if expected_revision is not None
                        else current_revision
                    ),
                    evidence_refs=event.evidence_refs,
                    reopen=candidate.reopen,
                    direct_user_asserted=candidate.assertion_mode == "direct_user",
                    source_quote_valid=(candidate.source_quote is not None),
                    server_command=server_command,
                )
                selected_situation_id = str(persisted["situation_id"])
                self.admission.mark_phase(
                    event_id=event.event_id,
                    phase="semantic_applied",
                    semantic_digest=semantic_digest,
                    need_plan_digest=need_plan_digest,
                    result_situation_revision=int(persisted.get("observation_revision") or 0),
                )
            needs_repaired = False
            if phase_rank < 2:
                if validated_needs:
                    if expected_need_revision is None:  # pragma: no cover - guarded above.
                        raise RuntimeError("Need preflight revision was not captured")
                    current_needs = self.needs.upsert_for_situation(
                        situation_id=selected_situation_id,
                        owner_id=owner_id,
                        session_id=session_id,
                        needs=validated_needs,
                        source_event_id=event.event_id,
                        expected_state_revision=expected_need_revision,
                        unknown_bindings=unknown_bindings,
                    )
                    needs_repaired = bool(persisted.get("semantic_replayed"))
                else:
                    current_needs = self.needs.list(
                        owner_id=owner_id,
                        session_id=session_id,
                        situation_id=selected_situation_id,
                        limit=self.needs.max_needs_per_situation,
                    )
                for need_token in candidate.answered_need_tokens:
                    expected_generation = next(
                        (
                            int(item.get("generation") or 1)
                            for item in [*answered_needs, *current_needs]
                            if str(item.get("need_id") or "") == need_token
                        ),
                        None,
                    )
                    self.needs.resolve(
                        need_token,
                        owner_id=owner_id,
                        session_id=session_id,
                        answered_by_event_id=event.event_id,
                        expected_generation=expected_generation,
                    )
                current_needs = self.needs.list(
                    owner_id=owner_id,
                    session_id=session_id,
                    situation_id=selected_situation_id,
                    limit=self.needs.max_needs_per_situation,
                )
                if str(persisted.get("status") or "") in self.situations.TERMINAL_STATUSES:
                    for need in current_needs:
                        if str(need.get("status") or "") not in {"open", "asked", "observing", "waiting"}:
                            continue
                        self.needs.dismiss(
                            str(need["need_id"]),
                            owner_id=owner_id,
                            session_id=session_id,
                            answered_by_event_id=event.event_id,
                            expected_generation=int(need.get("generation") or 1),
                        )
                    current_needs = self.needs.list(
                        owner_id=owner_id,
                        session_id=session_id,
                        situation_id=selected_situation_id,
                        limit=self.needs.max_needs_per_situation,
                    )
                self.admission.mark_phase(
                    event_id=event.event_id,
                    phase="needs_applied",
                    semantic_digest=semantic_digest,
                    need_plan_digest=need_plan_digest,
                    result_situation_revision=int(persisted.get("observation_revision") or 0),
                    result_need_state_revision=self._need_state_revision(),
                )
            else:
                current_needs = self.needs.list(
                    owner_id=owner_id,
                    session_id=session_id,
                    situation_id=selected_situation_id,
                    limit=self.needs.max_needs_per_situation,
                )
            self.admission.mark_committed(
                event_id=event.event_id,
                semantic_digest=semantic_digest,
                need_plan_digest=need_plan_digest,
            )
        return persisted, current_needs, needs_repaired

    def _derive_unknown_bindings(
        self,
        *,
        candidate: LivingContextCandidate,
        semantic_state: dict[str, Any],
        situation_id: str | None,
        owner_id: str,
        session_id: str,
    ) -> dict[str, str | None]:
        """Build a server-owned, one-to-one Need -> semantic unknown map.

        The model may phrase the missing fact differently in ``unknown`` and
        ``blocked_judgment``.  We bind only an exact match or the uniquely
        attributable single candidate unknown.  Ambiguous rows intentionally
        remain unbound so a later source receipt preserves the unknown and
        reports degradation instead of guessing.
        """

        if not candidate.needs:
            return {}
        semantic_unknown = [
            str(item) for item in semantic_state.get("unknown") or [] if str(item).strip()
        ]
        previous_unknown: set[str] = set()
        existing_needs: list[dict[str, Any]] = []
        if situation_id:
            previous = self.situations.get_semantic(
                str(situation_id), user_id=owner_id, session_id=session_id
            )
            if isinstance(previous, dict):
                previous_unknown = {
                    str(item) for item in (previous.get("semantic") or {}).get("unknown") or []
                }
            existing_needs = self.needs.list(
                owner_id=owner_id,
                session_id=session_id,
                situation_id=str(situation_id),
                limit=self.needs.max_needs_per_situation,
            )

        candidate_unknown = [
            str(item) for item in candidate.unknown if str(item).strip()
        ]
        candidate_blocked = {
            str(need.blocked_judgment) for need in candidate.needs
        }
        bindings: dict[str, str | None] = {}
        used: set[str] = set()
        for item in existing_needs:
            blocked = str(item.get("blocked_judgment") or "")
            prior_binding = str(item.get("unknown_binding") or "").strip()
            if (
                blocked in candidate_blocked
                and prior_binding
                and prior_binding in semantic_unknown
            ):
                bindings.setdefault(blocked, prior_binding)
                used.add(prior_binding)

        for need in candidate.needs:
            blocked = str(need.blocked_judgment)
            if blocked in bindings:
                continue
            # Prefer an exact model-emitted unknown.  The canonical blocked
            # judgment is appended by the server in ``_semantic_snapshot``;
            # treating that synthetic value as the model's paraphrase would
            # leave a second, stale unknown behind after a source receipt.
            explicit_exact = [
                value for value in candidate_unknown
                if value == blocked and value not in used
            ]
            exact = explicit_exact or [
                value for value in semantic_unknown
                if value == blocked and value not in used
            ]
            if len(exact) == 1 and (explicit_exact or not candidate_unknown or blocked in previous_unknown):
                bindings[blocked] = exact[0]
                used.add(exact[0])

        # A single newly emitted unknown and a single Need are a safe one-to-one
        # binding even when their wording differs.  Do not bind an old unknown
        # that merely happens to be present in the Situation history.
        if len(candidate.needs) == 1:
            need = candidate.needs[0]
            blocked = str(need.blocked_judgment)
            if blocked not in bindings:
                fresh = [
                    value for value in candidate_unknown
                    if value not in previous_unknown and value not in used
                ]
                if len(fresh) == 1:
                    bindings[blocked] = fresh[0]
                    used.add(fresh[0])
                    # The canonical blocked judgment is retained on the Need
                    # record, while the semantic projection carries one exact
                    # user-facing unknown for the source receipt to clear.
                    semantic_state["unknown"] = [
                        value for value in semantic_unknown if value != blocked
                    ]

        return bindings

    def answer_need(
        self,
        event: VeyraEvent,
        need_id: str,
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        return answer_need_command(
            self,
            event,
            need_id,
            expected_revision=expected_revision,
        )

    def apply_source_receipt(
        self,
        event: VeyraEvent,
        receipt: Any,
        *,
        expected_generation: int,
    ) -> dict[str, Any]:
        """Project a source receipt through the single semantic writer."""

        return apply_source_receipt_command(
            self,
            event,
            receipt,
            expected_generation=expected_generation,
        )

    def list_situations(
        self,
        *,
        owner_id: str,
        session_id: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return self.situations.list_semantic(
            user_id=owner_id,
            session_id=session_id,
            limit=limit,
        )

    def get_situation(
        self,
        situation_id: str,
        *,
        owner_id: str,
        session_id: str,
    ) -> dict[str, Any] | None:
        return self.situations.get_semantic(
            situation_id,
            user_id=owner_id,
            session_id=session_id,
        )

    def command_situation(
        self,
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
        return command_situation_command(
            self,
            event,
            situation_id,
            owner_id=owner_id,
            session_id=session_id,
            command=command,
            expected_revision=expected_revision,
            patch=patch,
            reason=reason,
        )

    def _need_state_revision(self) -> int:
        """Read the Need writer's CAS revision without creating state."""

        raw = self.state_store.read_json(self.needs.state_file)
        return int(raw.get("_state_revision") or 0) if isinstance(raw, dict) else 0

    def _semantic_event_matches(
        self,
        situation: dict[str, Any] | None,
        *,
        event_id: str,
        semantic_digest: str,
        expected_revision: Any,
    ) -> bool:
        """Prove that a previously written semantic stage is this event."""

        if not isinstance(situation, dict):
            return False
        if str(situation.get("source_event_id") or "") != str(event_id):
            return False
        if expected_revision is not None and int(situation.get("observation_revision") or 0) != int(expected_revision):
            return False
        semantic = situation.get("semantic")
        if not isinstance(semantic, dict):
            return False
        return self.admission.digest(semantic) == str(semantic_digest)

    def _candidate(self, understanding: Any) -> tuple[LivingContextCandidate | None, list[str]]:
        candidate = getattr(understanding, "living_context_candidate", None)
        if isinstance(candidate, LivingContextCandidate):
            return candidate, []
        if isinstance(candidate, dict):
            return parse_living_context_candidate(candidate)
        raw = getattr(understanding, "raw", None)
        if isinstance(raw, dict):
            payload = raw.get("living_context_candidate")
            if payload is None and isinstance(raw.get("situation_assessment"), dict):
                payload = raw["situation_assessment"].get("living_context_candidate")
            return parse_living_context_candidate(payload)
        return None, []

    def _semantic_snapshot(
        self,
        event: VeyraEvent,
        candidate: LivingContextCandidate,
        existing: dict[str, Any] | None,
    ) -> dict[str, Any]:
        previous = copy.deepcopy((existing or {}).get("semantic") or {})
        text = self._event_text(event)
        title = candidate.title.strip() or str(previous.get("title") or "").strip() or candidate.create_subject.strip()
        label = candidate.label.strip() or str(previous.get("label") or "").strip() or title
        summary = candidate.summary.strip() or str(previous.get("summary") or "").strip() or text[:640]
        goal = candidate.goal.strip() or str(previous.get("goal") or "").strip()
        category = candidate.category or str(previous.get("category") or "general")
        deadline_at = candidate.deadline_at or previous.get("deadline_at")
        progress = candidate.progress.model_dump(mode="json")
        if (
            existing
            and progress.get("status") == "unknown"
            and progress.get("value") is None
            and isinstance(previous.get("progress"), dict)
        ):
            progress = copy.deepcopy(previous["progress"])
        known = list(previous.get("known") or []) if existing else []
        for item in candidate.known:
            # A model summary without an exact source slice is an inference.
            # A source-bound user quote may remain reported, while the
            # original event observation is always recorded separately as
            # reported by SemanticSituationRuntime.
            epistemic_status = (
                "reported"
                if item.source_quote is not None and item.epistemic_status == "reported"
                else "inferred"
            )
            known.append(
                {
                    **item.model_dump(mode="json"),
                    "epistemic_status": epistemic_status,
                    "source_event_id": event.event_id,
                    "recorded_at": event.timestamp,
                }
            )
        if not known and text:
            known.append(
                {
                    "statement": text[:480],
                    "epistemic_status": "inferred",
                    "source_event_id": event.event_id,
                    "recorded_at": event.timestamp,
                }
            )
        timeline = list(previous.get("timeline") or []) if existing else []
        for item in candidate.timeline:
            timeline.append(
                {
                    **item.model_dump(mode="json"),
                    "source_event_id": event.event_id,
                    "recorded_at": event.timestamp,
                }
            )
        if text:
            timeline.append(
                {
                    "statement": text[:480],
                    "occurred_at": event.occurred_at,
                    "source_event_id": event.event_id,
                    "recorded_at": event.timestamp,
                    "material": bool(candidate.material_change.strip()),
                }
            )
        unknown = dedupe_text(
            [
                *(list(previous.get("unknown") or []) if existing else []),
                *candidate.unknown,
                *[item.blocked_judgment for item in candidate.needs],
            ],
            limit=12,
        )
        lifecycle = candidate.lifecycle
        if candidate.disposition == "resolve":
            lifecycle = "resolved"
        entities = list(previous.get("entities") or []) if existing else []
        for item in candidate.entities:
            epistemic_status = (
                "reported"
                if item.source_quote is not None and item.epistemic_status == "reported"
                else "inferred"
            )
            entities.append(
                {
                    **item.model_dump(mode="json"),
                    "epistemic_status": epistemic_status,
                    "source_event_id": event.event_id,
                    "recorded_at": event.timestamp,
                }
            )
        assumptions = list(previous.get("assumptions") or []) if existing else []
        for item in candidate.assumptions:
            assumptions.append(item.model_dump(mode="json"))
        evidence = list(previous.get("evidence") or []) if existing else []
        next_observation_at = (
            candidate.next_observation_at
            if candidate.next_observation_at is not None
            else previous.get("next_observation_at") if existing else None
        )
        next_step = candidate.next_step.strip() or str(previous.get("next_step") or "").strip()
        next_step_status = (
            candidate.next_step_epistemic_status
            if candidate.next_step.strip()
            else str(previous.get("next_step_epistemic_status") or "inferred")
        )
        result = {
            "title": title[:240],
            "label": label[:240],
            "summary": summary[:640],
            "goal": goal[:480],
            "category": category,
            "lifecycle": lifecycle,
            "deadline_at": deadline_at,
            "progress": progress,
            "entities": dedupe_records(entities, key="value", limit=8),
            "known": dedupe_records(known, key="statement", limit=12),
            "unknown": unknown,
            "assumptions": dedupe_records(assumptions, key="statement", limit=8),
            "timeline": dedupe_records(timeline, key="source_event_id", limit=24),
            "evidence": dedupe_records(evidence, key="ref", limit=16),
            "material_change": candidate.material_change.strip()[:480],
            "next_observation_at": next_observation_at,
            "next_step": next_step[:480],
            "next_step_epistemic_status": next_step_status,
            "reopen_reason": (
                candidate.reopen_reason.strip()[:240]
                if candidate.reopen and candidate.reopen_reason.strip()
                else (str(previous.get("reopen_reason") or "")[:240] or None)
            ),
        }
        if result.get("reopen_reason") is None:
            result.pop("reopen_reason", None)
        return result

    @staticmethod
    def _event_text(event: VeyraEvent) -> str:
        payload = event.payload if isinstance(event.payload, dict) else {}
        return str(payload.get("text") or payload.get("message") or "").strip()[:480]

    @staticmethod
    def _validate_quotes(candidate: LivingContextCandidate, text: str) -> None:
        if candidate.source_quote is not None:
            if (
                candidate.source_quote.end > len(text)
                or text[candidate.source_quote.start : candidate.source_quote.end]
                != candidate.source_quote.text
            ):
                raise ValueError("living context candidate source_quote is not source-bound")
        for item in [*candidate.known, *candidate.timeline, *candidate.entities]:
            quote = item.source_quote
            if quote is None:
                continue
            if quote.end > len(text) or text[quote.start : quote.end] != quote.text:
                raise ValueError("living context candidate contains a non-source-bound quote")

    def _catalog_binding(
        self,
        candidate: LivingContextCandidate,
        catalog: list[dict[str, Any]] | None,
        owner_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        if not isinstance(catalog, list):
            raise StateRevisionConflictError("existing Situation requires the current catalog snapshot")
        row = next(
            (
                item for item in catalog
                if isinstance(item, dict)
                and str(item.get("situation_token") or "") == str(candidate.situation_token or "")
            ),
            None,
        )
        if row is None:
            raise StateRevisionConflictError("Situation token is outside the current catalog snapshot")
        if str(row.get("owner_id") or "") != owner_id or str(row.get("session_id") or "") != session_id:
            raise PermissionError("catalog Situation scope mismatch")
        if int(row.get("observation_revision") or 0) != int(candidate.situation_revision or 0):
            raise StateRevisionConflictError("catalog Situation revision is stale")
        if str(row.get("catalog_token") or "") != str(candidate.catalog_token or ""):
            raise StateRevisionConflictError("catalog Situation binding token is invalid")
        # The caller must pass the same bounded snapshot that was issued for
        # this turn, not an arbitrary old row copied from durable state.  A
        # fresh server projection is read-only and lets us reject an old
        # revision or a row that fell outside the current catalog limit.
        fresh_catalog = self.model_catalog(
            owner_id=owner_id,
            session_id=session_id,
            limit=16,
        )
        fresh_row = next(
            (
                item
                for item in fresh_catalog
                if isinstance(item, dict)
                and str(item.get("situation_token") or "") == str(candidate.situation_token or "")
            ),
            None,
        )
        if fresh_row is None:
            raise StateRevisionConflictError("Situation token is outside the current server catalog")
        if (
            int(fresh_row.get("observation_revision") or 0) != int(row.get("observation_revision") or 0)
            or str(fresh_row.get("catalog_token") or "") != str(row.get("catalog_token") or "")
        ):
            raise StateRevisionConflictError("caller catalog snapshot is stale")
        return fresh_row

    def _prepared_terminal_admission(
        self,
        *,
        event: VeyraEvent,
        candidate: LivingContextCandidate,
        catalog: list[dict[str, Any]] | None,
        existing: dict[str, Any],
        owner_id: str,
        session_id: str,
    ) -> dict[str, Any] | None:
        """Return an exact interrupted terminal admission, if one exists.

        This is the sole exception to the turn-start catalog revision gate.
        A resolve retry may carry the old row because the first attempt has
        already written the terminal Situation.  The old row still has to be
        the exact candidate row, and the durable admission must prove that the
        same event wrote the next revision.  No missing, committed, foreign,
        or differently shaped row is eligible.
        """

        if candidate.disposition != "resolve" or not isinstance(catalog, list):
            return None
        if candidate.situation_revision is None or not candidate.catalog_token:
            return None
        catalog_row = next(
            (
                item
                for item in catalog
                if isinstance(item, dict)
                and str(item.get("situation_token") or "") == str(candidate.situation_token or "")
            ),
            None,
        )
        if catalog_row is None:
            return None
        if (
            str(catalog_row.get("owner_id") or "") != owner_id
            or str(catalog_row.get("session_id") or "") != session_id
            or str(catalog_row.get("catalog_token") or "") != str(candidate.catalog_token or "")
            or int(catalog_row.get("observation_revision") or 0) != int(candidate.situation_revision)
            or str(catalog_row.get("situation_token") or "") != str(existing.get("situation_id") or "")
        ):
            return None
        admission = self.admission.get(event.event_id)
        if not self._valid_terminal_admission_recovery(
            event=event,
            candidate=candidate,
            current_admission=admission,
            owner_id=owner_id,
            session_id=session_id,
            situation_id=str(existing.get("situation_id") or ""),
            existing=existing,
        ):
            return None
        return {"admission": admission, "catalog_row": catalog_row}

    def _prepared_admission_recovery(
        self,
        *,
        event: VeyraEvent,
        candidate: LivingContextCandidate,
        catalog: list[dict[str, Any]] | None,
        existing: dict[str, Any],
        owner_id: str,
        session_id: str,
    ) -> dict[str, Any] | None:
        """Identify an exact interrupted model admission.

        The catalog is a turn-start binding, so a successful semantic write
        naturally makes that row stale.  We only bridge that revision gap
        when the durable admission row proves the same event, scope,
        operation, timestamp, and semantic result.  A candidate with a
        different event/content or a row merely marked ``prepared`` is not a
        recovery candidate.
        """

        if candidate.disposition not in {"update", "resolve"} or not isinstance(catalog, list):
            return None
        if candidate.situation_revision is None or not candidate.catalog_token:
            return None
        catalog_row = next(
            (
                item
                for item in catalog
                if isinstance(item, dict)
                and str(item.get("situation_token") or "") == str(candidate.situation_token or "")
            ),
            None,
        )
        if catalog_row is None:
            return None
        if (
            str(catalog_row.get("owner_id") or "") != owner_id
            or str(catalog_row.get("session_id") or "") != session_id
            or str(catalog_row.get("catalog_token") or "") != str(candidate.catalog_token or "")
            or int(catalog_row.get("observation_revision") or 0) != int(candidate.situation_revision)
            or str(catalog_row.get("situation_token") or "") != str(existing.get("situation_id") or "")
        ):
            return None
        admission = self.admission.get(event.event_id)
        if not isinstance(admission, dict):
            return None
        phase = str(admission.get("phase") or "")
        if phase not in {"semantic_applied", "needs_applied", "committed"}:
            return None
        if (
            str(admission.get("event_id") or "") != str(event.event_id)
            or str(admission.get("owner_id") or "") != owner_id
            or str(admission.get("session_id") or "") != session_id
            or str(admission.get("event_timestamp") or "") != str(event.timestamp)
            or str(admission.get("situation_id") or "") != str(existing.get("situation_id") or "")
            or str(admission.get("operation") or "") != str(candidate.disposition)
            or int(admission.get("expected_situation_revision") or 0) != int(candidate.situation_revision)
        ):
            return None
        current_revision = int(existing.get("observation_revision") or 0)
        result_revision = int(admission.get("result_situation_revision") or 0)
        if current_revision < 1 or result_revision != current_revision:
            return None
        if str(existing.get("source_event_id") or "") != str(event.event_id):
            return None
        if candidate.disposition == "resolve" and str(existing.get("status") or "") != "resolved":
            return None
        return {"admission": admission, "catalog_row": catalog_row}

    def _valid_admission_recovery(
        self,
        *,
        event: VeyraEvent,
        candidate: LivingContextCandidate,
        current_admission: dict[str, Any] | None,
        owner_id: str,
        session_id: str,
        situation_id: str,
        existing: dict[str, Any] | None = None,
    ) -> bool:
        """Validate an exact prepared/progressed admission retry."""

        if candidate.disposition not in {"update", "resolve"} or candidate.situation_revision is None:
            return False
        if not isinstance(current_admission, dict):
            return False
        if str(current_admission.get("phase") or "") not in {"semantic_applied", "needs_applied", "committed"}:
            return False
        if (
            str(current_admission.get("event_id") or "") != str(event.event_id)
            or str(current_admission.get("owner_id") or "") != owner_id
            or str(current_admission.get("session_id") or "") != session_id
            or str(current_admission.get("event_timestamp") or "") != str(event.timestamp)
            or str(current_admission.get("situation_id") or "") != str(situation_id)
            or str(current_admission.get("operation") or "") != str(candidate.disposition)
            or int(current_admission.get("expected_situation_revision") or 0) != int(candidate.situation_revision)
        ):
            return False
        if existing is None:
            existing = self.situations.get_semantic(
                str(situation_id),
                user_id=owner_id,
                session_id=session_id,
            )
        if not isinstance(existing, dict):
            return False
        if str(existing.get("source_event_id") or "") != str(event.event_id):
            return False
        if int(existing.get("observation_revision") or 0) != int(current_admission.get("result_situation_revision") or 0):
            return False
        if candidate.disposition == "resolve" and str(existing.get("status") or (existing.get("semantic") or {}).get("lifecycle") or "") != "resolved":
            return False
        return True

    def _valid_terminal_admission_recovery(self, **kwargs: Any) -> bool:
        """Compatibility wrapper for older callers/tests."""

        return self._valid_admission_recovery(**kwargs)

    @staticmethod
    def _validate_need_bindings(
        candidate: LivingContextCandidate,
        catalog_row: dict[str, Any] | None,
    ) -> None:
        if catalog_row is None:
            raise StateRevisionConflictError("answered Need requires a current Situation catalog row")
        references = {item.need_token: int(item.generation) for item in candidate.answered_need_bindings}
        if set(references) != set(candidate.answered_need_tokens):
            raise StateRevisionConflictError("answered Need bindings are incomplete")
        available = {
            str(item.get("need_token") or ""): int(item.get("generation") or 0)
            for item in catalog_row.get("open_needs", [])
            if isinstance(item, dict)
        }
        for token in candidate.answered_need_tokens:
            if token not in available or references.get(token) != available[token]:
                raise StateRevisionConflictError("answered Need token/generation is outside the catalog snapshot")
