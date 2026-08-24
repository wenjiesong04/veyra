"""Coordinate semantic user Situations and InformationNeeds.

This module is intentionally a coordinator, not a second state store.  The
authoritative Situation is written only by ``SemanticSituationRuntime`` over
``SituationStateRepository``; needs are written only by
``InformationNeedRuntime``. Model output is admitted only after the strict
contract and exact owner/session checks pass.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import date, datetime, timezone
from typing import Any, Callable, Mapping

from common.reported_time_window import resolve_reported_calendar_date, resolve_reported_window_end
from core.situation_state_repository import SituationStateRepository
from core.world_state import StateRevisionConflictError, WorldStateStore
from core.living_context_need_answer_selector import StandaloneUnknownResolutionBinding
from core.semantic_frame import TurnSemanticFrame
from interface.event_schema import EventType, VeyraEvent
from interface.living_context_contract import (
    CandidateNeed,
    LivingContextCandidate,
    parse_living_context_candidate,
    situation_catalog_projection,
    stable_evidence_target_digest,
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
            projection = situation_catalog_projection(situation, open_needs=open_needs)
            semantic = situation.get("semantic")
            unknown_values = semantic.get("unknown") if isinstance(semantic, dict) else None
            if isinstance(unknown_values, list):
                generation = int(projection.get("observation_revision") or 1)
                endpoints = [
                    {
                        "unknown_token": self._unknown_endpoint_token(
                            owner_id=selected_owner,
                            session_id=selected_session,
                            situation_token=str(projection.get("situation_token") or ""),
                            statement=str(statement),
                            index=index,
                            generation=generation,
                        ),
                        "generation": generation,
                        "statement": str(statement),
                    }
                    for index, statement in enumerate(unknown_values[:12])
                    if isinstance(statement, str) and statement.strip()
                ]
                # Keep the existing bounded ``unknown`` strings for clients
                # that display the catalog.  Endpoint objects remain in a
                # separate server-owned field; TurnContextBuilder carries
                # that field through its narrow model projection.
                projection["unknown_endpoints"] = endpoints
            catalog.append(projection)
        return catalog

    @staticmethod
    def _unknown_endpoint_token(
        *,
        owner_id: str,
        session_id: str,
        situation_token: str,
        statement: str,
        index: int,
        generation: int,
    ) -> str:
        """Derive an opaque, exact endpoint token for one catalog revision."""

        payload = json.dumps(
            {
                "owner_id": owner_id,
                "session_id": session_id,
                "situation_token": situation_token,
                "statement": statement,
                "index": index,
                "generation": generation,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"unk_{hashlib.sha256(payload).hexdigest()[:32]}"

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
        # Candidate Unknown/Need rows have no server-owned endpoint reference.
        # Keep them open here; only a validated selector answer carrying the
        # current Need or Unknown token+generation may authorize deletion.
        unknown_resolved_count = 0
        resolved_need_count = 0
        deduped_need_count = 0
        if candidate.disposition == "create":
            subject_key = stable_subject_digest(
                f"{owner_id}\0{session_id}",
                candidate.create_subject,
            )
            operation = "create"
        else:
            subject_key = str(existing.get("semantic_subject_key") or "")
            operation = candidate.disposition
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
        semantic_state = self._semantic_snapshot(event, candidate, existing)
        evidence_targets = self._derive_evidence_targets(
            candidate=candidate,
            semantic_state=semantic_state,
            semantic_frame=getattr(understanding, "semantic_frame", None),
            situation_id=situation_id,
            owner_id=owner_id,
            session_id=session_id,
            source_text=self._event_text(event),
            current_time=event.timestamp,
        )
        standalone_unknown_resolutions = self._validated_standalone_unknown_resolutions(
            event=event,
            understanding=understanding,
            candidate=candidate,
            catalog_row=catalog_row,
            existing=existing,
        )
        if standalone_unknown_resolutions:
            resolved_unknowns = set(standalone_unknown_resolutions)
            semantic_state["unknown"] = [
                value
                for value in semantic_state.get("unknown", [])
                if str(value) not in resolved_unknowns
            ]
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
        unknown_bindings = self._derive_unknown_bindings(
            candidate=candidate,
            semantic_state=semantic_state,
            situation_id=situation_id,
            owner_id=owner_id,
            session_id=session_id,
            evidence_targets=evidence_targets,
        )
        candidate, evidence_targets, unknown_bindings, deduped_need_count = (
            self._dedupe_typed_need_candidates(
                candidate,
                evidence_targets,
                unknown_bindings,
            )
        )
        validated_needs = self.needs.validate_candidates(candidate.needs)
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
            evidence_targets=evidence_targets,
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
            "standalone_unknown_resolved_count": len(standalone_unknown_resolutions),
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

    def _validated_standalone_unknown_resolutions(
        self,
        *,
        event: VeyraEvent,
        understanding: Any,
        candidate: LivingContextCandidate,
        catalog_row: dict[str, Any] | None,
        existing: dict[str, Any] | None,
    ) -> tuple[str, ...]:
        """Validate selector sidecar bindings against the current catalog row.

        The sidecar is not a candidate field and cannot authorize a write by
        itself.  It is accepted only for an existing Situation, only when its
        opaque token+generation+statement is still the server-issued current
        endpoint, and only with affirmative evidence quoted from this turn.
        """

        raw = getattr(understanding, "living_context_standalone_unknown_resolutions", ())
        if not raw:
            return ()
        if candidate.disposition not in {"update", "correct", "resolve"}:
            raise StateRevisionConflictError(
                "standalone Unknown resolution requires an existing Situation"
            )
        if not isinstance(catalog_row, dict) or not isinstance(existing, dict):
            raise StateRevisionConflictError(
                "standalone Unknown resolution has no current catalog row"
            )
        if candidate.assertion_mode != "direct_user" or candidate.source_quote is None:
            raise StateRevisionConflictError(
                "standalone Unknown resolution requires a direct quoted user assertion"
            )
        raw_endpoints = catalog_row.get("unknown_endpoints")
        if not isinstance(raw_endpoints, list):
            raw_endpoints = catalog_row.get("unknown")
        endpoint_map: dict[tuple[str, int], str] = {}
        for item in raw_endpoints or []:
            if not isinstance(item, dict):
                continue
            token = item.get("unknown_token")
            generation = item.get("generation")
            statement = item.get("statement")
            if (
                not isinstance(token, str)
                or not re.fullmatch(r"unk_[0-9a-f]{32}", token)
                or isinstance(generation, bool)
                or not isinstance(generation, int)
                or generation < 1
                or not isinstance(statement, str)
                or not statement
            ):
                continue
            endpoint_map[(token, generation)] = statement
        previous_unknown = {
            str(value)
            for value in (existing.get("semantic") or {}).get("unknown") or []
            if str(value).strip()
        }
        admission = self.admission.get(event.event_id)
        replay_phase = (
            isinstance(admission, dict)
            and str(admission.get("phase") or "")
            in {"semantic_applied", "needs_applied", "committed"}
        )
        selected: list[str] = []
        seen: set[str] = set()
        source_text = self._event_text(event)
        for item in raw:
            if not isinstance(item, StandaloneUnknownResolutionBinding):
                raise StateRevisionConflictError(
                    "standalone Unknown resolution sidecar is invalid"
                )
            token = item.unknown_token
            generation = item.generation
            statement = item.unknown_text
            supporting_index = item.supporting_known_index
            quote = item.source_quote
            known = candidate.known[supporting_index] if (
                isinstance(supporting_index, int)
                and not isinstance(supporting_index, bool)
                and 0 <= supporting_index < len(candidate.known)
            ) else None
            known_quote = known.source_quote if known is not None else None
            if (
                not isinstance(token, str)
                or not re.fullmatch(r"unk_[0-9a-f]{32}", token)
                or isinstance(generation, bool)
                or not isinstance(generation, int)
                or not isinstance(statement, str)
                or isinstance(supporting_index, bool)
                or not isinstance(supporting_index, int)
                or supporting_index < 0
                or supporting_index >= len(candidate.known)
                or known is None
                or known.epistemic_status != "reported"
                or known_quote is None
                or quote.text != known_quote.text
                or quote.start != known_quote.start
                or quote.end != known_quote.end
                or quote.end > len(source_text)
                or source_text[quote.start : quote.end] != quote.text
                or endpoint_map.get((token, generation)) != statement
                or statement in seen
            ):
                raise StateRevisionConflictError(
                    "standalone Unknown resolution is stale or outside the current catalog"
                )
            if statement not in previous_unknown:
                # A committed admission replay may carry the original
                # turn-start catalog after its exact endpoint was already
                # removed.  It is a no-op, not a new deletion.  Non-replay
                # stale/missing endpoints remain fail-closed below.
                if replay_phase:
                    continue
                raise StateRevisionConflictError(
                    "standalone Unknown resolution is stale or already absent"
                )
            seen.add(statement)
            selected.append(statement)
        return tuple(selected)

    def _derive_evidence_targets(
        self,
        *,
        candidate: LivingContextCandidate,
        semantic_state: Mapping[str, Any],
        semantic_frame: TurnSemanticFrame | None = None,
        situation_id: str | None = None,
        owner_id: str | None = None,
        session_id: str | None = None,
        source_text: str = "",
        current_time: Any = None,
    ) -> list[dict[str, Any] | None]:
        """Derive positional source targets from typed Situation state.

        Model Need prose is intentionally not consulted here. Weather can
        only bind to one eligible structured place entity. A forecast date
        comes only from an explicit typed endpoint, one validated semantic
        frame endpoint, or this candidate's deadline. Ambiguous or missing
        typed
        inputs stay unbound so the orchestrator can ask/wait instead of
        fabricating a provider request.
        """

        semantic_entities = semantic_state.get("entities")
        entities = semantic_entities if isinstance(semantic_entities, list) else []
        places: list[dict[str, Any]] = []
        for raw in entities:
            if not isinstance(raw, Mapping) or str(raw.get("kind") or "").lower() != "place":
                continue
            value = " ".join(str(raw.get("value") or "").split())[:160]
            if not value:
                continue
            provenance = str(raw.get("provenance_scope") or "")
            epistemic = str(raw.get("epistemic_status") or "").lower()
            if provenance == "span":
                quote = raw.get("source_quote")
                if epistemic != "reported" or not isinstance(quote, Mapping):
                    continue
            elif provenance == "model_attributed":
                if epistemic != "inferred" or raw.get("source_quote") is not None:
                    continue
            else:
                continue
            places.append({"value": value, "entity": dict(raw)})

        raw_deadline = semantic_state.get("deadline_at")
        weather_needs = [need for need in candidate.needs if need.evidence_kind == "weather"]

        # Provider dates come only from the direct user event and the server
        # reference clock. A semantic frame may corroborate meaning, but it
        # cannot authorize or invent a forecast day.
        direct_weather_date = (
            resolve_reported_calendar_date(source_text, current_time)
            if len(weather_needs) == 1 and len(places) == 1
            else None
        )

        targets: list[dict[str, Any] | None] = []
        for need in candidate.needs:
            if need.evidence_kind == "weather":
                # Multiple candidate places are not safe to collapse into one
                # provider call.  Keep the Need open for an explicit choice.
                if len(places) != 1:
                    targets.append(None)
                    continue
                requirement = need.observation_requirement
                if requirement is None:
                    # Do not infer required metrics from a question or a
                    # display blocker.  New weather Needs must carry the
                    # structured requirement contract.
                    targets.append(None)
                    continue
                selected_date = None
                if requirement.coverage == "forecast_day":
                    if direct_weather_date is None:
                        targets.append(None)
                        continue
                    if requirement.target_date is not None and requirement.target_date != direct_weather_date:
                        targets.append(None)
                        continue
                    selected_date = direct_weather_date
                if requirement.coverage != "forecast_day":
                    selected_date = None
                target: dict[str, Any] = {"location": places[0]["value"]}
                if selected_date:
                    target["target_date"] = selected_date
                target["observation_requirement"] = requirement.model_dump(mode="json")
                targets.append(target)
                continue
            if need.evidence_kind == "calendar":
                requirement = need.observation_requirement
                targets.append(
                    {
                        **({"window_end": raw_deadline} if raw_deadline else {}),
                        "observation_requirement": requirement.model_dump(mode="json"),
                    }
                    if requirement is not None
                    else None
                )
                continue
            if need.evidence_kind == "public_web":
                # The public-web adapter currently derives its bounded query
                # from the server Situation projection.  Keep a typed
                # requirement sidecar so identity/replay still distinguishes
                # result coverage from unrelated Needs.
                requirement = need.observation_requirement
                targets.append(
                    {"observation_requirement": requirement.model_dump(mode="json")}
                    if requirement is not None
                    else None
                )
                continue
            targets.append(None)

        # A Situation-bound continuation may deliberately omit a repeated
        # place/date. Reuse only one already durable endpoint for one readable
        # Need with the same typed source lifecycle. Presentation wording is
        # never part of this lookup, and any new derived target wins.
        readable_indexes = [
            index
            for index, need in enumerate(candidate.needs)
            if need.evidence_kind in {"weather", "calendar", "public_web"}
        ]
        if (
            situation_id
            and owner_id
            and session_id
            and len(readable_indexes) == 1
        ):
            index = readable_indexes[0]
            candidate_need = candidate.needs[index]
            if targets[index] is None:
                # Reuse is continuation-only. Any current place or date
                # signal must form a fresh endpoint (or remain unbound when
                # it is ambiguous), rather than silently inheriting one.
                candidate_has_place_signal = any(
                    str(item.kind or "").lower() == "place"
                    for item in candidate.entities
                )
                candidate_has_date_signal = bool(
                    direct_weather_date
                    or resolve_reported_window_end(source_text, current_time)
                )
                candidate_requirement = (
                    candidate_need.observation_requirement.model_dump(mode="json")
                    if candidate_need.observation_requirement is not None
                    else None
                )
                existing = self.needs.list(
                    owner_id=owner_id,
                    session_id=session_id,
                    situation_id=str(situation_id),
                    limit=self.needs.max_needs_per_situation,
                )
                matching = [
                    row
                    for row in existing
                    if (
                        str(row.get("status") or "") in {"open", "asked", "observing", "waiting"}
                        or (
                            str(candidate_need.observation_mode) == "watch"
                            and str(row.get("status") or "") == "resolved"
                        )
                    )
                    and str(row.get("evidence_kind") or "") == str(candidate_need.evidence_kind)
                    and str(row.get("observation_mode") or "once") == str(candidate_need.observation_mode)
                    and row.get("observation_requirement") == candidate_requirement
                    and isinstance(row.get("evidence_target"), Mapping)
                    and bool(row.get("evidence_target"))
                ]
                if not candidate_has_place_signal and not candidate_has_date_signal and len(matching) == 1:
                    existing_target = dict(matching[0]["evidence_target"])
                    if candidate_need.evidence_kind == "weather":
                        # Historical rows may have carried the requirement as
                        # a target sidecar. Do not perpetuate that mixed shape:
                        # weather target identity is location + optional day.
                        reused_weather_target: dict[str, Any] = {
                            "location": existing_target.get("location"),
                        }
                        if existing_target.get("target_date"):
                            reused_weather_target["target_date"] = existing_target["target_date"]
                        targets[index] = reused_weather_target
                    else:
                        targets[index] = copy.deepcopy(existing_target)
        return targets

    def _dedupe_typed_need_candidates(
        self,
        candidate: LivingContextCandidate,
        evidence_targets: list[dict[str, Any] | None],
        unknown_bindings: Mapping[str, str | None] | None = None,
    ) -> tuple[LivingContextCandidate, list[dict[str, Any] | None], dict[str, str | None], int]:
        """Keep one Need per canonical typed target in this candidate.

        Identity includes the observation mode, source kind, and complete
        target projection (including observation requirement).  A source-only
        match is never enough to merge two Needs.
        """

        if not candidate.needs:
            return candidate, [], {}, 0
        retained_needs: list[CandidateNeed] = []
        retained_targets: list[dict[str, Any] | None] = []
        retained_bindings: dict[str, str | None] = {}
        identities: set[str] = set()
        dropped = 0
        for index, need in enumerate(candidate.needs):
            target = evidence_targets[index] if index < len(evidence_targets) else None
            target_digest = stable_evidence_target_digest(target)
            unknown_binding = (
                unknown_bindings.get(need.blocked_judgment)
                if isinstance(unknown_bindings, Mapping)
                else None
            )
            typed_endpoint = self.needs._is_typed_endpoint(
                need,
                target,
                unknown_binding,
            )
            need_identity_digest = (
                self.needs.need_identity_digest_for_candidate(need, target)
                if typed_endpoint
                else None
            )
            # Alternate source proposals for one semantic Unknown are a
            # duplicate only when neither proposal carries a distinct typed
            # target/requirement. Metric-specific Needs retain their complete
            # typed identity.
            dedupe_identity_digest = (
                need_identity_digest
                if target_digest or need.observation_requirement is not None
                else None
            )
            identity = self.needs.stable_need_id(
                situation_id="candidate",
                blocked_judgment=need.blocked_judgment,
                evidence_kind=need.evidence_kind,
                evidence_target_digest=target_digest,
                need_identity_digest=dedupe_identity_digest,
                unknown_binding_digest=self.needs._unknown_binding_digest(unknown_binding),
                typed_endpoint=typed_endpoint,
                observation_mode=need.observation_mode,
            )
            if identity in identities:
                dropped += 1
                continue
            identities.add(identity)
            retained_needs.append(need)
            retained_targets.append(copy.deepcopy(target) if isinstance(target, Mapping) else None)
            if isinstance(unknown_bindings, Mapping) and need.blocked_judgment in unknown_bindings:
                retained_bindings[need.blocked_judgment] = unknown_binding
        if dropped == 0 and len(retained_targets) == len(evidence_targets):
            return candidate, evidence_targets, dict(unknown_bindings or {}), 0
        return candidate.model_copy(update={"needs": retained_needs}), retained_targets, retained_bindings, dropped

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
        evidence_targets: list[dict[str, Any] | None],
        planned_situation_id: str,
        admission_recovery: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], bool]:
        """Commit Situation and its needs under one root writer fence.

        The evaluator and need runtime remain the two authoritative writers;
        this fence only makes the cross-file admission window repairable and
        prevents two turns from validating the same revision concurrently.
        """

        with self.state_store.writer_transaction():
            # Bind admission to the exact canonical semantic bytes that the
            # Situation repository persists. Transaction-only metadata such
            # as reopen_reason remains available to record_semantic without
            # making replay depend on a field the repository intentionally
            # omits from semantic state.
            canonical_semantic_state = self.situations.repository.normalize_semantic(
                semantic_state
            )
            semantic_digest = self.admission.digest(canonical_semantic_state)
            need_plan_digest = self.admission.digest(
                [
                    {
                        "need": item.model_dump(mode="json"),
                        "evidence_target": copy.deepcopy(evidence_targets[index]),
                    }
                    for index, item in enumerate(validated_needs)
                ]
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
                    evidence_targets=evidence_targets,
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
                        evidence_targets=evidence_targets,
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
        evidence_targets: list[dict[str, Any] | None] | None = None,
    ) -> dict[str, str | None]:
        """Build a server-owned, one-to-one Need -> semantic unknown map.

        ``blocked_judgment`` is presentation only and is never compared to an
        Unknown. Existing bindings survive only when the typed Need endpoint
        identifies one current row. A new binding is possible only for the
        unambiguous one-Need/one-current-turn-Unknown shape.
        """

        if not candidate.needs:
            return {}
        semantic_unknown = [
            str(item) for item in semantic_state.get("unknown") or [] if str(item).strip()
        ]
        existing_needs: list[dict[str, Any]] = []
        if situation_id:
            existing_needs = self.needs.list(
                owner_id=owner_id,
                session_id=session_id,
                situation_id=str(situation_id),
                limit=self.needs.max_needs_per_situation,
            )

        candidate_unknown_slots = [str(item).strip() for item in candidate.unknown]
        candidate_unknown = [item for item in candidate_unknown_slots if item]
        explicit_bindings: dict[str, str] = {}
        for need in candidate.needs:
            index = need.unknown_index
            if index is None:
                continue
            if index >= len(candidate_unknown_slots) or not candidate_unknown_slots[index]:
                raise ValueError("InformationNeed unknown_index is outside candidate.unknown")
            transport_key = str(need.blocked_judgment)
            selected_unknown = candidate_unknown_slots[index]
            prior = explicit_bindings.get(transport_key)
            if prior is not None and prior != selected_unknown:
                raise ValueError("InformationNeed cannot bind one proposal to two Unknowns")
            # Repeated references to one endpoint are tolerated here so the
            # canonical endpoint deduper can retain the first proposal. The
            # model contract still asks for one proposal per distinct index.
            explicit_bindings[transport_key] = selected_unknown

        bindings: dict[str, str | None] = dict(explicit_bindings)
        used: set[str] = set(explicit_bindings.values())

        selected_targets = list(evidence_targets or [])
        if selected_targets and len(selected_targets) != len(candidate.needs):
            raise ValueError("InformationNeed evidence_targets must align with candidate Needs")
        if not selected_targets:
            selected_targets = [None] * len(candidate.needs)

        for index, need in enumerate(candidate.needs):
            if str(need.blocked_judgment) in bindings:
                continue
            selected_target = selected_targets[index]
            target_digest = stable_evidence_target_digest(selected_target)
            typed_need = bool(selected_target) or need.observation_requirement is not None
            need_identity_digest = (
                self.needs.need_identity_digest_for_candidate(need, selected_target)
                if typed_need
                else None
            )
            same_source_mode = [
                row
                for row in existing_needs
                if str(row.get("evidence_kind") or "") == str(need.evidence_kind)
                and str(row.get("observation_mode") or "once") == str(need.observation_mode)
            ]
            typed_matches = [
                row
                for row in same_source_mode
                if typed_need
                and str(row.get("evidence_target_digest") or "") == str(target_digest or "")
                and str(row.get("need_identity_digest") or "") == str(need_identity_digest or "")
            ]
            # A legacy binding may only survive an exact legacy identity. A
            # newly typed target/requirement never inherits an old unknown
            # merely because source and watch mode happen to match.
            legacy_matches = [
                row for row in same_source_mode
                if not target_digest
                and need.observation_requirement is None
                and not str(row.get("evidence_target_digest") or "").strip()
                and not str(row.get("need_identity_digest") or "").strip()
                and str(row.get("blocked_judgment") or "") == str(need.blocked_judgment)
            ]
            matches = typed_matches
            if not matches and target_digest and len(legacy_matches) == 1:
                matches = legacy_matches
            if len(matches) != 1:
                continue
            prior_binding = str(matches[0].get("unknown_binding") or "").strip()
            if prior_binding and prior_binding in semantic_unknown and prior_binding not in used:
                bindings[str(need.blocked_judgment)] = prior_binding
                used.add(prior_binding)

        # A lone Need and one current-turn unknown form an explicit
        # one-to-one proposal. Multiple candidates remain unbound unless the
        # structural preservation above already admitted their endpoint.
        if len(candidate.needs) == 1 and len(candidate_unknown) == 1:
            need = candidate.needs[0]
            transport_key = str(need.blocked_judgment)
            if transport_key not in bindings and candidate_unknown[0] not in used:
                bindings[transport_key] = candidate_unknown[0]

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
            # A Known statement without an exact user-message span remains a
            # model attribution. The model's epistemic label alone is never
            # enough to promote it to reported fact.
            item_payload = item.model_dump(mode="json")
            quote_is_valid = self._quote_is_source_bound(item.source_quote, text)
            if item.source_quote is not None and not quote_is_valid:
                item_payload["source_quote"] = None
            is_user_report = (
                event.type == EventType.USER_MESSAGE
                and item.epistemic_status == "reported"
            )
            epistemic_status = "reported" if is_user_report and quote_is_valid else "inferred"
            if quote_is_valid:
                item_payload["provenance_scope"] = "span"
            elif is_user_report:
                item_payload["provenance_scope"] = "model_attributed"
            known.append(
                {
                    **item_payload,
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
            ],
            limit=12,
        )
        lifecycle = candidate.lifecycle
        if candidate.disposition == "resolve":
            lifecycle = "resolved"
        entities = list(previous.get("entities") or []) if existing else []
        correction_replaces_places = (
            candidate.disposition == "correct"
            and candidate.assertion_mode == "direct_user"
            and candidate.source_quote is not None
            and len(candidate.entities) == 1
            and candidate.entities[0].kind == "place"
            and self._quote_is_source_bound(
                candidate.entities[0].source_quote,
                text,
                expected_text=candidate.entities[0].value,
            )
        )
        if correction_replaces_places:
            entities = [
                item
                for item in entities
                if not isinstance(item, dict)
                or str(item.get("kind") or "").lower() != "place"
            ]
        for item in candidate.entities:
            # Entity provenance has two honest tiers: an exact source span is
            # reported; without one the value remains inferred/model-attributed
            # and may only be used as a tentative read parameter by a narrowly
            # typed, separately consented weather source.
            item_payload = item.model_dump(mode="json")
            quote_is_valid = self._quote_is_source_bound(
                item.source_quote,
                text,
                expected_text=item.value,
            )
            if item.source_quote is not None and not quote_is_valid:
                item_payload["source_quote"] = None
            if quote_is_valid:
                is_user_report = (
                    event.type == EventType.USER_MESSAGE
                    and item.epistemic_status == "reported"
                )
                epistemic_status = "reported" if is_user_report else "inferred"
                item_payload["provenance_scope"] = "span"
            else:
                epistemic_status = "inferred"
                item_payload["provenance_scope"] = "model_attributed"
            entities.append(
                {
                    **item_payload,
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
        # Keep the raw event text for source-quote validation. Provider/model
        # prompts may use bounded projections, but quote coordinates are always
        # checked against the complete user message.
        return str(payload.get("text") or payload.get("message") or "")

    @staticmethod
    def _quote_is_source_bound(
        quote: Any,
        source_text: str,
        *,
        expected_text: str | None = None,
    ) -> bool:
        """Check one supplied quote without deriving or searching a replacement."""

        return bool(
            quote is not None
            and source_text
            and (expected_text is None or quote.text == expected_text)
            and quote.end <= len(source_text)
            and source_text[quote.start : quote.end] == quote.text
        )

    @staticmethod
    def _validate_quotes(candidate: LivingContextCandidate, text: str) -> None:
        if candidate.source_quote is not None:
            if (
                candidate.source_quote.end > len(text)
                or text[candidate.source_quote.start : candidate.source_quote.end]
                != candidate.source_quote.text
            ):
                raise ValueError("living context candidate source_quote is not source-bound")
        for item in [*candidate.known, *candidate.timeline]:
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
