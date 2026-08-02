from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Iterable
from uuid import uuid4

from core.world_state import WorldStateStore
from interface.event_schema import VeyraEvent
from interface.general_situation_contract import (
    ANCHOR_KINDS,
    StructuredAnchor,
)


class SituationAccessError(PermissionError):
    """Raised when a caller tries to mutate another user's situation."""


class SituationTraceBackpressureError(RuntimeError):
    """Raised before an unaudited transition can exceed trace-outbox bounds."""


class SituationEvaluator:
    """Persist evidence-linked situations without granting authority.

    This evaluator is deliberately a shadow-state component. It records what was
    observed, what was inferred or predicted, what decision another component
    made, and what was later observed as an outcome. It does not authorize an
    action, select a route, or derive capabilities from natural-language text.

    Model-produced material belongs in ``inference`` or ``prediction`` (or must
    be passed with ``source="model"``). Such records are always persisted with
    ``is_fact=False``. A source event proves that an event occurred; it does not
    prove that every claim in the event payload is true.
    """

    STATE_FILE = "situation_state.json"
    TRACE_FILE = "situation_trace.jsonl"
    SCHEMA_VERSION = "veyra.situation_state.v1"
    TRACE_SCHEMA_VERSION = "veyra.situation_trace.v1"

    _TERMINAL_STATUSES = {
        "cancelled",
        "closed",
        "dismissed",
        "expired",
        "failed",
        "indeterminate",
        "partial",
        "resolved",
        "verified_failed",
        "verified_success",
    }
    _MODEL_SOURCES = {
        "ai",
        "agent_model",
        "core_model",
        "language_model",
        "llm",
        "model",
        "model_assist",
    }
    _STRUCTURED_ANCHOR_DIRECT_FIELDS = {
        "goal_id": "goal",
        "commitment_id": "commitment",
        "case_id": "case",
        "task_id": "task",
        "trace_id": "trace",
        "entity_id": "entity",
        "workspace_id": "workspace",
    }
    _STRUCTURED_ANCHOR_COLLECTION_FIELDS = {
        "goal_refs": "goal",
        "commitment_refs": "commitment",
        "case_refs": "case",
        "task_refs": "task",
        "trace_refs": "trace",
        "entity_refs": "entity",
        "workspace_refs": "workspace",
    }

    def __init__(
        self,
        state_store: WorldStateStore,
        *,
        state_file: str = STATE_FILE,
        trace_file: str = TRACE_FILE,
        max_situations: int = 500,
        max_situations_per_user: int = 200,
        max_history_per_situation: int = 24,
        max_trace_outbox: int = 2000,
        max_trace_entry_bytes: int = 256 * 1024,
        max_trace_outbox_bytes: int = 32 * 1024 * 1024,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if max_situations < 1:
            raise ValueError("max_situations must be at least 1")
        if max_situations_per_user < 1:
            raise ValueError("max_situations_per_user must be at least 1")
        if max_history_per_situation < 1:
            raise ValueError("max_history_per_situation must be at least 1")
        if max_trace_outbox < 2:
            raise ValueError(
                "max_trace_outbox must be at least 2 so one atomic "
                "decision/outcome resolution can be admitted"
            )
        if max_trace_entry_bytes < 1:
            raise ValueError("max_trace_entry_bytes must be positive")
        if max_trace_outbox_bytes < 1:
            raise ValueError("max_trace_outbox_bytes must be positive")
        self.state_store = state_store
        self.state_file = str(state_file)
        self.trace_file = str(trace_file)
        self.max_situations = int(max_situations)
        self.max_situations_per_user = min(int(max_situations_per_user), self.max_situations)
        self.max_history_per_situation = int(max_history_per_situation)
        self.max_trace_outbox = int(max_trace_outbox)
        self.max_trace_entry_bytes = int(max_trace_entry_bytes)
        self.max_trace_outbox_bytes = int(max_trace_outbox_bytes)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def observe(
        self,
        event: VeyraEvent,
        *,
        situation_id: str | None = None,
        correlation_id: str | None = None,
        goal_refs: Iterable[Any] | Any | None = None,
        commitment_refs: Iterable[Any] | Any | None = None,
        evidence_refs: Iterable[Any] | Any | None = None,
        salience_components: dict[str, Any] | None = None,
        status: str = "observed",
        decision: Any | None = None,
        inference: Any | None = None,
        prediction: Any | None = None,
        outcome: Any | None = None,
        next_evaluation_at: str | datetime | None = None,
        due_at: str | datetime | None = None,
        observation: Any | None = None,
        observation_source: str = "event",
        observation_evidence_verified: bool = False,
        observation_sequence: int | None = None,
        observation_id: str | None = None,
        allow_terminal_reopen: bool = False,
    ) -> dict[str, Any]:
        """Observe one event and create or update its evidence-linked situation.

        Only explicit structured fields are consumed from ``event.payload``.
        The evaluator never scans message text for keywords and never produces an
        authorization or routing decision. Callers that aggregate multiple
        observations into one stable Situation may provide a durable sequence and
        semantic observation identity. Older sequences remain in ordered history
        but cannot replace the current head.
        """

        source = self._event_source(event)
        # Recover an earlier trace when possible, but never report the durable
        # situation mutation as failed merely because JSONL delivery is down.
        # The full trace entry remains in the state document's outbox.
        self._try_flush_trace_outbox()
        user_id = source["user_id"]
        session_id = source["session_id"]
        payload = event.payload if isinstance(event.payload, dict) else {}
        correlation = self._text(
            correlation_id
            or getattr(event, "correlation_id", None)
            or payload.get("correlation_id")
            or payload.get("trace_id")
            or payload.get("request_id")
            or event.event_id,
            max_length=240,
        )
        selected_id = self._text(situation_id, max_length=240) or self._stable_situation_id(
            user_id=user_id,
            session_id=session_id,
            event_id=source["event_id"],
            correlation_id=correlation,
        )
        selected_observation_sequence = self._normalize_observation_sequence(
            observation_sequence
        )
        selected_observation_id = (
            self._text(observation_id, max_length=240)
            or (
                source["event_id"]
                if selected_observation_sequence is not None
                else ""
            )
        )
        now = self._now_iso()
        normalized_goals = self._merge_refs(
            self._subject_refs(getattr(event, "subject", None), kind="goal"),
            self._refs_from_payload(payload, plural="goal_refs", singular=("goal_ref", "goal_id")),
        )
        normalized_goals = self._merge_refs(
            normalized_goals,
            self._normalize_refs(goal_refs),
        )
        normalized_commitments = self._merge_refs(
            self._subject_refs(getattr(event, "subject", None), kind="commitment"),
            self._refs_from_payload(
                payload,
                plural="commitment_refs",
                singular=("commitment_ref", "commitment_id"),
            ),
        )
        normalized_commitments = self._merge_refs(
            normalized_commitments,
            self._normalize_refs(commitment_refs),
        )
        structured_anchor_refs = self._structured_anchor_refs(
            event=event,
            payload=payload,
            correlation_id=correlation,
            source_event_id=source["event_id"],
            goal_refs=normalized_goals,
            commitment_refs=normalized_commitments,
        )
        normalized_evidence = self._merge_refs(
            self._normalize_refs(getattr(event, "evidence_refs", None), evidence=True),
            self._refs_from_payload(
                payload,
                plural="evidence_refs",
                singular=("evidence_ref", "evidence_id"),
                evidence=True,
            ),
        )
        normalized_evidence = self._merge_refs(
            normalized_evidence,
            self._normalize_refs(evidence_refs, evidence=True),
        )
        normalized_salience = self._normalize_salience(salience_components or {})
        evaluation_at = self._iso_time(
            next_evaluation_at
            or due_at
            or payload.get("next_evaluation_at")
            or payload.get("due_at")
            or now,
            fallback=now,
        )
        source_observation = self._epistemic_record(
            observation,
            source=observation_source,
            epistemic_status="observation",
            evidence_refs=normalized_evidence,
            evidence_verified=observation_evidence_verified,
        ) if observation is not None else None
        if source_observation is not None and (
            selected_observation_sequence is not None
            or selected_observation_id
        ):
            source_observation["source_event_id"] = source["event_id"]
            if selected_observation_sequence is not None:
                source_observation[
                    "observation_sequence"
                ] = selected_observation_sequence
            if selected_observation_id:
                source_observation["observation_id"] = selected_observation_id
        inference_record = self._epistemic_record(
            inference,
            source="model",
            epistemic_status="inference",
            evidence_refs=normalized_evidence,
        ) if inference is not None else None
        prediction_record = self._prediction_record(
            prediction,
            source="model",
            evidence_refs=normalized_evidence,
        ) if prediction is not None else None
        decision_record = self._decision_record(
            decision,
            source="observer",
            evidence_refs=normalized_evidence,
        ) if decision is not None else None
        outcome_record = self._outcome_record(
            outcome,
            source="observer",
            evidence_refs=normalized_evidence,
            prediction_id=prediction_record.get("prediction_id") if prediction_record else None,
        ) if outcome is not None else None

        incoming = {
            "record_kind": "situation_candidate",
            "situation_id": selected_id,
            "correlation_id": correlation,
            "user_id": user_id,
            "session_id": session_id,
            "channel": source["channel"],
            "source_event": source,
            "source_event_id": source["event_id"],
            "source_event_type": source["type"],
            "goal_refs": normalized_goals,
            "commitment_refs": normalized_commitments,
            "structured_anchor_refs": structured_anchor_refs,
            "evidence_refs": normalized_evidence,
            "salience_components": normalized_salience["components"],
            "salience_score": normalized_salience["score"],
            "status": self._status(status, default="observed"),
            "decision": decision_record,
            "prediction": prediction_record,
            "outcome": outcome_record,
            "observations": [source_observation] if source_observation else [],
            "inferences": [inference_record] if inference_record else [],
            "decision_history": [decision_record] if decision_record else [],
            "prediction_history": [prediction_record] if prediction_record else [],
            "outcome_history": [outcome_record] if outcome_record else [],
            "next_evaluation_at": evaluation_at,
            "created_at": now,
            "updated_at": now,
        }
        if selected_observation_sequence is not None:
            incoming["observation_sequence"] = selected_observation_sequence
        if selected_observation_id:
            incoming["observation_id"] = selected_observation_id
        incoming["source_observation_fingerprint"] = self._observation_fingerprint(incoming)
        if source_observation is not None and selected_observation_id:
            source_observation["observation_fingerprint"] = incoming[
                "source_observation_fingerprint"
            ]
        incoming["observation_revision"] = 1
        persisted: dict[str, Any] = {}
        evicted_ids: list[str] = []
        replayed = False
        transition_created = False

        def upsert(state: dict[str, Any]) -> dict[str, Any]:
            nonlocal persisted, evicted_ids, replayed, transition_created
            situations = self._state_situations(state)
            existing_index = next(
                (
                    index
                    for index, item in enumerate(situations)
                    if str(item.get("situation_id") or "") == selected_id
                ),
                None,
            )
            if existing_index is not None:
                existing = situations[existing_index]
                self._assert_owner(existing, user_id=user_id, session_id=session_id)
                replayed = source["event_id"] == str(
                    existing.get("source_event_id") or ""
                )
                exact_replay = False
                if selected_observation_id:
                    identity_fingerprint = self._observation_identity_fingerprint(
                        existing,
                        selected_observation_id,
                    )
                    if identity_fingerprint is not None:
                        if (
                            identity_fingerprint
                            != incoming["source_observation_fingerprint"]
                        ):
                            raise ValueError(
                                "observation_id is already bound to different content"
                            )
                        exact_replay = True
                        replayed = True
                if not exact_replay:
                    exact_replay = (
                        replayed
                        and str(
                            existing.get("source_observation_fingerprint") or ""
                        )
                        == incoming["source_observation_fingerprint"]
                    )
                if exact_replay:
                    persisted = copy.deepcopy(existing)
                else:
                    sequence_aware = self._sequence_aware_observation(
                        existing,
                        incoming,
                    )
                    replace_head = self._incoming_observation_is_head(
                        existing,
                        incoming,
                    )
                    same_source_event = bool(
                        replayed
                        and not (
                            sequence_aware
                            and (
                                selected_observation_sequence is not None
                                or selected_observation_id
                            )
                        )
                    )
                    merged = self._merge_observation(
                        existing,
                        incoming,
                        same_source_event=same_source_event,
                        replace_head=replace_head,
                        sequence_aware=sequence_aware,
                        allow_terminal_reopen=bool(allow_terminal_reopen),
                    )
                    if merged == existing:
                        persisted = copy.deepcopy(existing)
                    else:
                        merged["observation_revision"] = (
                            int(existing.get("observation_revision") or 0) + 1
                        )
                        situations[existing_index] = merged
                        persisted = copy.deepcopy(merged)
                        transition_created = True
            else:
                situations.append(incoming)
                persisted = copy.deepcopy(incoming)
                transition_created = True
            if transition_created:
                situations, evicted_ids = self._bound_state(situations)
            state["schema_version"] = self.SCHEMA_VERSION
            state["situations"] = situations
            state["count"] = len(situations)
            state["updated_at"] = now
            if transition_created:
                trace_type = (
                    "situation_observation_replayed"
                    if replayed
                    else "situation_observed"
                )
                trace_detail = {
                    "source_event": source,
                    "goal_refs": normalized_goals,
                    "commitment_refs": normalized_commitments,
                    "structured_anchor_refs": structured_anchor_refs,
                    "evidence_refs": normalized_evidence,
                    "salience_components": normalized_salience["components"],
                    "salience_score": normalized_salience["score"],
                    "inference": inference_record,
                    "prediction": prediction_record,
                    "decision": decision_record,
                    "outcome": outcome_record,
                    "observation_revision": persisted.get(
                        "observation_revision"
                    ),
                    "evicted_situation_ids": evicted_ids,
                }
                if selected_observation_sequence is not None:
                    trace_detail[
                        "observation_sequence"
                    ] = selected_observation_sequence
                if selected_observation_id:
                    trace_detail["observation_id"] = selected_observation_id
                self._enqueue_trace(
                    state,
                    trace_type=trace_type,
                    situation=persisted,
                    detail=trace_detail,
                    timestamp=now,
                )
            return state

        self.state_store.mutate_json(self.state_file, upsert)
        self._try_flush_trace_outbox()
        return copy.deepcopy(persisted)

    def record_context_binding(
        self,
        situation_id: str,
        *,
        expected_source_event_id: str,
        expected_observation_revision: int,
        operation_id: str,
        binding_digest: str,
        anchors: list[dict[str, Any]],
        provenance: list[dict[str, Any]],
        user_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        """CAS-enrich one event-scoped Situation with context associations.

        This is intentionally a separate observation revision rather than a
        rewrite of the immutable source Event.  The association is always
        recorded as model inference with ``is_fact=False`` and cannot carry a
        route, risk, capability, execution, or delivery authority.
        """

        selected_id = self._text(situation_id, max_length=240)
        selected_event = self._text(expected_source_event_id, max_length=240)
        selected_operation = self._text(operation_id, max_length=300)
        selected_digest = self._text(binding_digest, max_length=128)
        if not selected_id or not selected_event or not selected_operation:
            raise ValueError("context binding identity is incomplete")
        if (
            len(selected_digest) != 64
            or any(character not in "0123456789abcdef" for character in selected_digest)
        ):
            raise ValueError("context binding digest is invalid")
        if (
            isinstance(expected_observation_revision, bool)
            or not isinstance(expected_observation_revision, int)
            or expected_observation_revision < 1
        ):
            raise ValueError("expected observation revision is invalid")
        normalized_anchors = self._normalized_structured_anchor_refs(anchors)
        if not normalized_anchors:
            raise ValueError("context binding requires at least one structured anchor")
        if len(normalized_anchors) > 8:
            raise ValueError("context binding anchor capacity exceeded")
        normalized_provenance = self._context_binding_provenance(
            provenance,
            expected_source_event_id=selected_event,
            anchors=normalized_anchors,
        )
        now = self._now_iso()
        persisted: dict[str, Any] | None = None
        replayed = False

        def update(state: dict[str, Any]) -> None:
            nonlocal persisted, replayed
            situations = self._state_situations(state)
            target = next(
                (
                    item
                    for item in situations
                    if str(item.get("situation_id") or "") == selected_id
                ),
                None,
            )
            if target is None:
                raise KeyError(f"unknown situation: {selected_id}")
            self._assert_owner(target, user_id=user_id, session_id=session_id)
            if str(target.get("source_event_id") or "") != selected_event:
                raise ValueError("context binding source event does not match Situation")
            history = (
                target.get("context_bindings")
                if isinstance(target.get("context_bindings"), list)
                else []
            )
            existing = next(
                (
                    item
                    for item in history
                    if isinstance(item, dict)
                    and str(item.get("operation_id") or "") == selected_operation
                ),
                None,
            )
            if isinstance(existing, dict):
                if str(existing.get("binding_digest") or "") != selected_digest:
                    raise ValueError(
                        "context binding operation is already bound to different semantics"
                    )
                replayed = True
                persisted = copy.deepcopy(target)
                return
            current_revision = int(target.get("observation_revision") or 0)
            if current_revision != expected_observation_revision:
                raise ValueError("context binding observation revision CAS conflict")

            projection = {
                "operation_id": selected_operation,
                "binding_digest": selected_digest,
                "source_event_id": selected_event,
                "anchors": copy.deepcopy(normalized_anchors),
                "provenance": copy.deepcopy(normalized_provenance),
                "epistemic_status": "context_hypothesis",
                "is_fact": False,
                "causality_asserted": False,
                "route_change_allowed": False,
                "risk_change_allowed": False,
                "authority": False,
                "recorded_at": now,
            }
            target["structured_anchor_refs"] = self._merge_structured_anchor_refs(
                target.get("structured_anchor_refs"),
                normalized_anchors,
                replace=False,
            )
            target["context_bindings"] = [*history, projection][
                -self.max_history_per_situation :
            ]
            inference = self._epistemic_record(
                {
                    "kind": "context_binding",
                    "binding_digest": selected_digest,
                    "anchors": copy.deepcopy(normalized_anchors),
                    "provenance": copy.deepcopy(normalized_provenance),
                    "causality_asserted": False,
                    "authority": False,
                },
                source="context_model",
                epistemic_status="inference",
                evidence_refs=[],
                evidence_verified=False,
            )
            target["inferences"] = self._extend_history(
                target.get("inferences"),
                [inference],
            )
            target["observation_revision"] = current_revision + 1
            target["updated_at"] = now
            target["context_projection_fingerprint"] = (
                self._context_projection_fingerprint(target)
            )
            persisted = copy.deepcopy(target)
            state["schema_version"] = self.SCHEMA_VERSION
            state["situations"] = situations
            state["count"] = len(situations)
            state["updated_at"] = now
            self._enqueue_trace(
                state,
                trace_type="situation_context_bound",
                situation=persisted,
                detail={
                    "operation_id": selected_operation,
                    "binding_digest": selected_digest,
                    "source_event_id": selected_event,
                    "anchors": copy.deepcopy(normalized_anchors),
                    "epistemic_status": "context_hypothesis",
                    "is_fact": False,
                    "causality_asserted": False,
                    "observation_revision": target["observation_revision"],
                },
                timestamp=now,
            )

        self.state_store.mutate_json(self.state_file, update)
        self._try_flush_trace_outbox()
        if persisted is None:  # pragma: no cover - guarded by the mutation.
            raise KeyError(f"unknown situation: {selected_id}")
        return {**copy.deepcopy(persisted), "context_binding_replayed": replayed}

    def _context_projection_fingerprint(
        self,
        situation: dict[str, Any],
    ) -> str:
        semantic = self._stable_transition_value(
            {
                "situation_id": situation.get("situation_id"),
                "source_event_id": situation.get("source_event_id"),
                "structured_anchor_refs": situation.get(
                    "structured_anchor_refs"
                ),
                "context_bindings": situation.get("context_bindings"),
            }
        )
        encoded = json.dumps(
            semantic,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return f"scbp_{hashlib.sha256(encoded).hexdigest()[:32]}"

    def record_decision(
        self,
        situation_id: str,
        decision: Any,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
        evidence_refs: Iterable[Any] | Any | None = None,
        prediction: Any | None = None,
        source: str = "policy",
        status: str = "decided",
        next_evaluation_at: str | datetime | None = None,
        due_at: str | datetime | None = None,
    ) -> dict[str, Any]:
        """Record a decision made elsewhere; this method does not authorize it."""

        self._try_flush_trace_outbox()
        normalized_evidence = self._normalize_refs(evidence_refs, evidence=True)
        decision_record = self._decision_record(
            decision,
            source=source,
            evidence_refs=normalized_evidence,
        )
        prediction_record = self._prediction_record(
            prediction,
            source=source,
            evidence_refs=normalized_evidence,
        ) if prediction is not None else None
        now = self._now_iso()

        def apply(item: dict[str, Any]) -> None:
            item["decision"] = decision_record
            item["decision_history"] = self._append_history(item.get("decision_history"), decision_record)
            if prediction_record is not None:
                item["prediction"] = prediction_record
                item["prediction_history"] = self._append_history(item.get("prediction_history"), prediction_record)
            item["evidence_refs"] = self._merge_refs(
                self._normalize_refs(item.get("evidence_refs"), evidence=True),
                normalized_evidence,
            )
            item["status"] = self._status(status, default="decided")
            if next_evaluation_at is not None or due_at is not None:
                item["next_evaluation_at"] = self._iso_time(
                    next_evaluation_at or due_at,
                    fallback=now,
                )
            item["updated_at"] = now

        persisted = self._mutate_situation(
            situation_id,
            apply,
            user_id=user_id,
            session_id=session_id,
            trace_type="situation_decision_recorded",
            trace_detail={
                "decision": decision_record,
                "prediction": prediction_record,
                "evidence_refs": normalized_evidence,
            },
            trace_timestamp=now,
        )
        self._try_flush_trace_outbox()
        return persisted

    def record_outcome(
        self,
        situation_id: str,
        outcome: Any,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
        evidence_refs: Iterable[Any] | Any | None = None,
        source: str = "observed",
        evidence_verified: bool = False,
        status: str = "resolved",
        next_evaluation_at: str | datetime | None = None,
        due_at: str | datetime | None = None,
    ) -> dict[str, Any]:
        """Record an outcome while preserving its provenance and evidence."""

        self._try_flush_trace_outbox()
        normalized_evidence = self._normalize_refs(evidence_refs, evidence=True)
        now = self._now_iso()
        existing = self.get(situation_id, user_id=user_id, session_id=session_id)
        if existing is None:
            raise KeyError(f"unknown situation: {situation_id}")
        prediction = existing.get("prediction") if isinstance(existing.get("prediction"), dict) else {}
        outcome_record = self._outcome_record(
            outcome,
            source=source,
            evidence_refs=normalized_evidence,
            prediction_id=str(prediction.get("prediction_id") or "") or None,
            evidence_verified=bool(evidence_verified),
        )

        def apply(item: dict[str, Any]) -> None:
            item["outcome"] = outcome_record
            item["outcome_history"] = self._append_history(item.get("outcome_history"), outcome_record)
            item["evidence_refs"] = self._merge_refs(
                self._normalize_refs(item.get("evidence_refs"), evidence=True),
                normalized_evidence,
            )
            item["status"] = self._status(status, default="resolved")
            if next_evaluation_at is not None or due_at is not None:
                item["next_evaluation_at"] = self._iso_time(
                    next_evaluation_at or due_at,
                    fallback=now,
                )
            item["updated_at"] = now

        persisted = self._mutate_situation(
            situation_id,
            apply,
            user_id=user_id,
            session_id=session_id,
            trace_type="situation_outcome_recorded",
            trace_detail={
                "outcome": outcome_record,
                "prediction_ref": outcome_record.get("prediction_ref"),
                "evidence_refs": normalized_evidence,
            },
            trace_timestamp=now,
        )
        self._try_flush_trace_outbox()
        return persisted

    def record_resolution(
        self,
        situation_id: str,
        *,
        idempotency_key: str,
        expected_source_event_id: str,
        expected_correlation_id: str,
        decision: Any,
        outcome: Any,
        user_id: str | None = None,
        session_id: str | None = None,
        decision_evidence_refs: Iterable[Any] | Any | None = None,
        outcome_evidence_refs: Iterable[Any] | Any | None = None,
        decision_source: str = "policy",
        outcome_source: str = "observed",
        outcome_evidence_verified: bool = False,
        decision_status: str = "decided",
        outcome_status: str = "resolved",
    ) -> dict[str, Any]:
        """Atomically persist one idempotent decision/outcome resolution.

        Foreground result projection must not leave a decision without its
        corresponding outcome. Both materialized records and both trace-outbox
        entries therefore commit in one state mutation. Re-delivery with the
        same bounded key is a no-op, including under concurrent callers.
        """

        selected_id = str(situation_id or "").strip()
        selected_key = self._text(idempotency_key, max_length=240).strip()
        selected_source_event_id = self._text(
            expected_source_event_id,
            max_length=240,
        ).strip()
        selected_correlation_id = self._text(
            expected_correlation_id,
            max_length=240,
        ).strip()
        if not selected_id:
            raise ValueError("situation_id must be non-empty")
        if not selected_key:
            raise ValueError("idempotency_key must be non-empty")
        if not selected_source_event_id:
            raise ValueError("expected_source_event_id must be non-empty")
        if not selected_correlation_id:
            raise ValueError("expected_correlation_id must be non-empty")
        self._try_flush_trace_outbox()
        decision_evidence = self._normalize_refs(
            decision_evidence_refs,
            evidence=True,
        )
        outcome_evidence = self._normalize_refs(
            outcome_evidence_refs,
            evidence=True,
        )
        decision_record = self._decision_record(
            decision,
            source=decision_source,
            evidence_refs=decision_evidence,
        )
        existing = self.get(
            selected_id,
            user_id=user_id,
            session_id=session_id,
        )
        if existing is None:
            raise KeyError(f"unknown situation: {selected_id}")
        self._assert_event_binding(
            existing,
            source_event_id=selected_source_event_id,
            correlation_id=selected_correlation_id,
        )
        prediction = (
            existing.get("prediction")
            if isinstance(existing.get("prediction"), dict)
            else {}
        )
        outcome_record = self._outcome_record(
            outcome,
            source=outcome_source,
            evidence_refs=outcome_evidence,
            prediction_id=str(prediction.get("prediction_id") or "") or None,
            evidence_verified=bool(outcome_evidence_verified),
        )
        now = self._now_iso()
        persisted: dict[str, Any] | None = None

        def resolve(state: dict[str, Any]) -> dict[str, Any]:
            nonlocal persisted
            situations = self._state_situations(state)
            target = next(
                (
                    item
                    for item in situations
                    if str(item.get("situation_id") or "") == selected_id
                ),
                None,
            )
            if target is None:
                raise KeyError(f"unknown situation: {selected_id}")
            self._assert_owner(
                target,
                user_id=user_id,
                session_id=session_id,
            )
            self._assert_event_binding(
                target,
                source_event_id=selected_source_event_id,
                correlation_id=selected_correlation_id,
            )
            resolution_keys = [
                str(item)
                for item in (
                    target.get("resolution_keys")
                    if isinstance(target.get("resolution_keys"), list)
                    else []
                )
                if str(item)
            ]
            if selected_key in resolution_keys:
                persisted = copy.deepcopy(target)
                return state
            current_outcome = (
                target.get("outcome")
                if isinstance(target.get("outcome"), dict)
                else {}
            )
            if (
                current_outcome.get("is_fact") is True
                and outcome_record.get("is_fact") is not True
            ):
                # A transient ledger read failure or replay without evidence
                # cannot downgrade an already persisted verified fact.
                persisted = copy.deepcopy(target)
                return state

            target["decision"] = decision_record
            target["decision_history"] = self._append_history(
                target.get("decision_history"),
                decision_record,
            )
            target["evidence_refs"] = self._merge_refs(
                self._normalize_refs(
                    target.get("evidence_refs"),
                    evidence=True,
                ),
                decision_evidence,
            )
            target["status"] = self._status(
                decision_status,
                default="decided",
            )
            target["updated_at"] = now
            decision_snapshot = copy.deepcopy(target)

            target["outcome"] = outcome_record
            target["outcome_history"] = self._append_history(
                target.get("outcome_history"),
                outcome_record,
            )
            target["evidence_refs"] = self._merge_refs(
                self._normalize_refs(
                    target.get("evidence_refs"),
                    evidence=True,
                ),
                outcome_evidence,
            )
            target["status"] = self._status(
                outcome_status,
                default="resolved",
            )
            target["updated_at"] = now
            target["resolution_keys"] = (
                resolution_keys + [selected_key]
            )[-self.max_history_per_situation :]
            persisted = copy.deepcopy(target)

            state["schema_version"] = self.SCHEMA_VERSION
            state["situations"] = situations
            state["count"] = len(situations)
            state["updated_at"] = now
            self._enqueue_trace(
                state,
                trace_type="situation_decision_recorded",
                situation=decision_snapshot,
                detail={
                    "decision": decision_record,
                    "prediction": None,
                    "evidence_refs": decision_evidence,
                    "resolution_key": selected_key,
                },
                timestamp=now,
            )
            self._enqueue_trace(
                state,
                trace_type="situation_outcome_recorded",
                situation=persisted,
                detail={
                    "outcome": outcome_record,
                    "prediction_ref": outcome_record.get("prediction_ref"),
                    "evidence_refs": outcome_evidence,
                    "resolution_key": selected_key,
                },
                timestamp=now,
            )
            return state

        self.state_store.mutate_json(self.state_file, resolve)
        self._try_flush_trace_outbox()
        if persisted is None:  # pragma: no cover - guarded by the mutation.
            raise KeyError(f"unknown situation: {selected_id}")
        return persisted

    def list(
        self,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
        status: str | Iterable[str] | None = None,
        correlation_id: str | None = None,
        limit: int = 100,
        newest_first: bool = True,
    ) -> list[dict[str, Any]]:
        """List bounded situation state, optionally isolated to one user/session."""

        statuses = self._status_filter(status)
        situations = self._state_situations(self.state_store.read_json(self.state_file))
        filtered: list[dict[str, Any]] = []
        for item in situations:
            if user_id is not None and str(item.get("user_id") or "") != str(user_id):
                continue
            if session_id is not None and str(item.get("session_id") or "") != str(session_id):
                continue
            if statuses and str(item.get("status") or "") not in statuses:
                continue
            if correlation_id is not None and str(item.get("correlation_id") or "") != str(correlation_id):
                continue
            filtered.append(copy.deepcopy(item))
        filtered.sort(
            key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""),
            reverse=newest_first,
        )
        return filtered[: max(0, int(limit))]

    def get(
        self,
        situation_id: str,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Return a situation only when the supplied tenant scope matches."""

        target = str(situation_id or "")
        for item in self._state_situations(self.state_store.read_json(self.state_file)):
            if str(item.get("situation_id") or "") != target:
                continue
            if user_id is not None and str(item.get("user_id") or "") != str(user_id):
                return None
            if session_id is not None and str(item.get("session_id") or "") != str(session_id):
                return None
            return copy.deepcopy(item)
        return None

    def due_candidates(
        self,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
        now: datetime | str | None = None,
        min_salience: float = 0.0,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Return open situations due for evaluation, never for auto-execution."""

        current = self._datetime(now) if now is not None else self._clock()
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        threshold = self._score(min_salience)
        candidates: list[dict[str, Any]] = []
        for item in self.list(
            user_id=user_id,
            session_id=session_id,
            limit=self.max_situations,
            newest_first=False,
        ):
            if str(item.get("status") or "") in self._TERMINAL_STATUSES:
                continue
            if self._score(item.get("salience_score")) < threshold:
                continue
            due_at = self._datetime_or_none(item.get("next_evaluation_at"))
            if due_at is not None and due_at > current:
                continue
            candidates.append(item)
        candidates.sort(
            key=lambda item: (
                -self._score(item.get("salience_score")),
                str(item.get("next_evaluation_at") or ""),
                str(item.get("created_at") or ""),
            )
        )
        return candidates[: max(0, int(limit))]

    # Explicit aliases make call sites readable without changing the requested
    # list/get surface.
    list_situations = list
    get_situation = get

    def _mutate_situation(
        self,
        situation_id: str,
        mutator: Callable[[dict[str, Any]], None],
        *,
        user_id: str | None,
        session_id: str | None,
        trace_type: str,
        trace_detail: dict[str, Any],
        trace_timestamp: str,
    ) -> dict[str, Any]:
        selected_id = str(situation_id or "")
        persisted: dict[str, Any] | None = None

        def update(state: dict[str, Any]) -> dict[str, Any]:
            nonlocal persisted
            situations = self._state_situations(state)
            target = next(
                (item for item in situations if str(item.get("situation_id") or "") == selected_id),
                None,
            )
            if target is None:
                raise KeyError(f"unknown situation: {selected_id}")
            self._assert_owner(target, user_id=user_id, session_id=session_id)
            mutator(target)
            persisted = copy.deepcopy(target)
            state["schema_version"] = self.SCHEMA_VERSION
            state["situations"] = situations
            state["count"] = len(situations)
            state["updated_at"] = self._now_iso()
            self._enqueue_trace(
                state,
                trace_type=trace_type,
                situation=persisted,
                detail=trace_detail,
                timestamp=trace_timestamp,
            )
            return state

        self.state_store.mutate_json(self.state_file, update)
        if persisted is None:  # pragma: no cover - guarded by the mutation above.
            raise KeyError(f"unknown situation: {selected_id}")
        return persisted

    def _merge_observation(
        self,
        existing: dict[str, Any],
        incoming: dict[str, Any],
        *,
        same_source_event: bool,
        replace_head: bool,
        sequence_aware: bool,
        allow_terminal_reopen: bool,
    ) -> dict[str, Any]:
        merged = copy.deepcopy(existing)
        if not replace_head:
            history = self._merge_observation_history(
                merged.get("observations"),
                incoming.get("observations"),
            )
            if history != merged.get("observations"):
                merged["observations"] = history
                merged["updated_at"] = incoming["updated_at"]
            return merged

        merged["correlation_id"] = incoming["correlation_id"]
        merged["channel"] = incoming["channel"]
        merged["source_event"] = incoming["source_event"]
        merged["source_event_id"] = incoming["source_event_id"]
        merged["source_event_type"] = incoming["source_event_type"]
        if "observation_sequence" in incoming:
            merged["observation_sequence"] = incoming["observation_sequence"]
        if "observation_id" in incoming:
            merged["observation_id"] = incoming["observation_id"]
        merged["structured_anchor_refs"] = self._merge_structured_anchor_refs(
            merged.get("structured_anchor_refs"),
            incoming["structured_anchor_refs"],
            replace=sequence_aware,
        )
        for key in ("goal_refs", "commitment_refs", "evidence_refs"):
            merged[key] = (
                copy.deepcopy(incoming[key])
                if sequence_aware
                else self._merge_refs(
                    self._normalize_refs(
                        merged.get(key),
                        evidence=key == "evidence_refs",
                    ),
                    incoming[key],
                )
            )
        merged["salience_components"] = (
            copy.deepcopy(incoming["salience_components"])
            if sequence_aware
            else {
                **(
                    merged.get("salience_components")
                    if isinstance(merged.get("salience_components"), dict)
                    else {}
                ),
                **incoming["salience_components"],
            }
        )
        merged["salience_score"] = self._normalize_salience(merged["salience_components"])["score"]
        terminal_reopened = bool(
            allow_terminal_reopen
            and self._status(existing.get("status"), default="observed")
            == "closed"
            and self._status(incoming.get("status"), default="observed")
            == "observed"
        )
        if same_source_event:
            merged["status"] = self._status(existing.get("status"), default="observed")
        elif terminal_reopened:
            merged["status"] = "observed"
        else:
            merged["status"] = self._monotonic_status(
                merged.get("status"),
                incoming["status"],
            )
        # Re-delivery of one source event can enrich its evidence/salience, but
        # it is not a new lifecycle transition and therefore cannot reschedule
        # the situation. A terminal lifecycle is also absorbing with respect to
        # a later non-terminal observation.
        if (
            not same_source_event
            and (
                not self._is_terminal(existing.get("status"))
                or terminal_reopened
            )
        ):
            merged["next_evaluation_at"] = incoming["next_evaluation_at"]
        for singular, history in (
            ("decision", "decision_history"),
            ("prediction", "prediction_history"),
            ("outcome", "outcome_history"),
        ):
            if incoming.get(singular) is not None:
                merged[singular] = incoming[singular]
                merged[history] = self._append_history(merged.get(history), incoming[singular])
        merged["observations"] = (
            self._merge_observation_history(
                merged.get("observations"),
                incoming.get("observations"),
            )
            if sequence_aware
            else self._extend_history(
                merged.get("observations"),
                incoming.get("observations"),
            )
        )
        merged["inferences"] = self._extend_history(merged.get("inferences"), incoming.get("inferences"))
        merged["source_observation_fingerprint"] = incoming["source_observation_fingerprint"]
        merged.setdefault("created_at", incoming["created_at"])
        merged["updated_at"] = incoming["updated_at"]
        return merged

    def _bound_state(self, situations: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
        ordered = sorted(
            situations,
            key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""),
        )
        per_user: dict[str, list[dict[str, Any]]] = {}
        for item in ordered:
            per_user.setdefault(str(item.get("user_id") or ""), []).append(item)
        retained: list[dict[str, Any]] = []
        for items in per_user.values():
            retained.extend(items[-self.max_situations_per_user :])
        retained.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""))
        retained = retained[-self.max_situations :]
        retained_ids = {str(item.get("situation_id") or "") for item in retained}
        evicted = [
            str(item.get("situation_id") or "")
            for item in situations
            if str(item.get("situation_id") or "") not in retained_ids
        ]
        return retained, evicted

    def _try_flush_trace_outbox(self) -> dict[str, Any]:
        """Best-effort delivery for lifecycle writes that are already durable."""

        try:
            return self.flush_trace_outbox()
        except Exception as exc:
            state = self.state_store.read_json(self.state_file)
            return {
                "pending": len(self._trace_outbox(state)),
                "appended": 0,
                "deduplicated": 0,
                "acknowledged": 0,
                "error_type": type(exc).__name__,
            }

    def flush_trace_outbox(self) -> dict[str, int]:
        """Recover pending situation traces without claiming a cross-file transaction.

        Situation state and its full trace entry are committed atomically to the
        JSON state document. This method then appends pending entries to JSONL
        in order and acknowledges them in a later JSON mutation. A deterministic
        ``transition_id`` lets a retry recognize an append that succeeded before
        a crash or acknowledgement failure.
        """

        appended = 0
        deduplicated = 0
        acknowledged = 0
        remaining_after_ack = 0
        # Holding the store's writer transaction serializes scan/append/ack
        # within the one-writer state boundary. Durability still comes from the
        # outbox and transition-id recovery, not from pretending the two files
        # share one atomic transaction.
        with self.state_store.writer_transaction():
            state = self.state_store.read_json(self.state_file)
            pending = self._trace_outbox(state)
            if not pending:
                return {
                    "pending": 0,
                    "appended": 0,
                    "deduplicated": 0,
                    "acknowledged": 0,
                }

            existing_rows = self.state_store.read_jsonl(
                self.trace_file,
                limit=(1 << 63) - 1,
            )
            persisted_ids = {
                str(row.get("transition_id") or "")
                for row in existing_rows
                if isinstance(row, dict) and row.get("transition_id")
            }
            acknowledged_ids: set[str] = set()
            for entry in pending:
                transition_id = str(entry.get("transition_id") or "")
                if not transition_id:
                    raise ValueError("situation trace outbox entry is missing transition_id")
                if transition_id in persisted_ids:
                    deduplicated += 1
                else:
                    self.state_store.append_jsonl(self.trace_file, copy.deepcopy(entry))
                    persisted_ids.add(transition_id)
                    appended += 1
                acknowledged_ids.add(transition_id)

            if acknowledged_ids:

                def acknowledge(current: dict[str, Any]) -> dict[str, Any]:
                    nonlocal acknowledged, remaining_after_ack
                    current_outbox = self._trace_outbox(current)
                    remaining = [
                        entry
                        for entry in current_outbox
                        if str(entry.get("transition_id") or "") not in acknowledged_ids
                    ]
                    acknowledged = len(current_outbox) - len(remaining)
                    remaining_after_ack = len(remaining)
                    current["trace_outbox"] = remaining
                    current["trace_outbox_count"] = len(remaining)
                    current["trace_outbox_bytes"] = self._trace_outbox_size(
                        remaining
                    )
                    current["updated_at"] = self._now_iso()
                    return current

                self.state_store.mutate_json(self.state_file, acknowledge)

        return {
            "pending": remaining_after_ack,
            "appended": appended,
            "deduplicated": deduplicated,
            "acknowledged": acknowledged,
        }

    def _enqueue_trace(
        self,
        state: dict[str, Any],
        *,
        trace_type: str,
        situation: dict[str, Any],
        detail: dict[str, Any],
        timestamp: str,
    ) -> None:
        sequence = int(state.get("trace_sequence") or 0) + 1
        identity = {
            "state_file": self.state_file,
            "sequence": sequence,
            "trace_type": trace_type,
            "situation_id": situation.get("situation_id"),
        }
        encoded = json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        transition_id = f"sitxn_{digest[:32]}"
        entry = {
            "schema_version": self.TRACE_SCHEMA_VERSION,
            "trace_id": f"strace_{digest[:16]}",
            "transition_id": transition_id,
            "transition_sequence": sequence,
            "trace_type": trace_type,
            "situation_id": situation.get("situation_id"),
            "correlation_id": situation.get("correlation_id"),
            "user_id": situation.get("user_id"),
            "session_id": situation.get("session_id"),
            "source_event_id": situation.get("source_event_id"),
            "source_event_type": situation.get("source_event_type"),
            "status": situation.get("status"),
            "timestamp": timestamp,
            "detail": self._bounded_value(detail),
        }
        entry_bytes = self._trace_entry_size(entry)
        if entry_bytes > self.max_trace_entry_bytes:
            raise SituationTraceBackpressureError(
                "situation trace entry exceeds the serialized byte limit"
            )
        outbox = self._trace_outbox(state)
        if not any(
            str(item.get("transition_id") or "") == transition_id
            for item in outbox
        ):
            if len(outbox) >= self.max_trace_outbox:
                raise SituationTraceBackpressureError(
                    "situation trace outbox capacity is exhausted; "
                    "repair trace delivery before accepting more transitions"
                )
            outbox_bytes = self._trace_outbox_size(outbox)
            if outbox_bytes + entry_bytes > self.max_trace_outbox_bytes:
                raise SituationTraceBackpressureError(
                    "situation trace outbox serialized byte capacity is "
                    "exhausted; repair trace delivery before accepting more "
                    "transitions"
                )
            outbox.append(entry)
        state["trace_sequence"] = sequence
        state["trace_outbox"] = outbox
        state["trace_outbox_count"] = len(outbox)
        state["trace_outbox_bytes"] = self._trace_outbox_size(outbox)

    def _trace_outbox(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        raw = state.get("trace_outbox") if isinstance(state, dict) else []
        if not isinstance(raw, list):
            return []
        return [copy.deepcopy(item) for item in raw if isinstance(item, dict)]

    @staticmethod
    def _trace_entry_size(entry: dict[str, Any]) -> int:
        return len(
            json.dumps(
                entry,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        )

    @classmethod
    def _trace_outbox_size(cls, entries: list[dict[str, Any]]) -> int:
        return sum(cls._trace_entry_size(entry) for entry in entries)

    def _event_source(self, event: VeyraEvent) -> dict[str, Any]:
        if event is None or not hasattr(event, "source"):
            raise TypeError("observe requires a VeyraEvent")
        source = event.source
        event_type = event.type.value if isinstance(event.type, Enum) else str(event.type)
        return {
            "event_id": self._text(event.event_id, max_length=240),
            "type": self._text(event_type, max_length=120),
            "timestamp": self._iso_time(event.timestamp, fallback=self._now_iso()),
            "occurred_at": self._iso_time(
                getattr(event, "occurred_at", None) or event.timestamp,
                fallback=self._now_iso(),
            ),
            "received_at": self._iso_time(
                getattr(event, "received_at", None) or event.timestamp,
                fallback=self._now_iso(),
            ),
            "correlation_id": self._text(
                getattr(event, "correlation_id", None) or event.event_id,
                max_length=240,
            ),
            "causation_id": self._text(
                getattr(event, "causation_id", None),
                max_length=240,
            ) or None,
            "channel": self._text(getattr(source, "channel", ""), max_length=120) or "unknown",
            "user_id": self._text(getattr(source, "user_id", ""), max_length=240) or "local-user",
            "session_id": self._text(getattr(source, "session_id", ""), max_length=240) or "local-session",
            "subject": self._bounded_value(getattr(event, "subject", None)),
            "privacy_scope": self._bounded_value(getattr(event, "privacy_scope", "user")),
            "event_observed": True,
            "payload_claims_verified": False,
        }

    def _stable_situation_id(
        self,
        *,
        user_id: str,
        session_id: str,
        event_id: str,
        correlation_id: str,
    ) -> str:
        identity = "\0".join((user_id, session_id, event_id, correlation_id))
        return f"sit_{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:16]}"

    def _assert_owner(
        self,
        item: dict[str, Any],
        *,
        user_id: str | None,
        session_id: str | None,
    ) -> None:
        if user_id is not None and str(item.get("user_id") or "") != str(user_id):
            raise SituationAccessError("situation belongs to a different user")
        if session_id is not None and str(item.get("session_id") or "") != str(session_id):
            raise SituationAccessError("situation belongs to a different session")

    @staticmethod
    def _assert_event_binding(
        item: dict[str, Any],
        *,
        source_event_id: str,
        correlation_id: str,
    ) -> None:
        if str(item.get("source_event_id") or "") != str(source_event_id):
            raise ValueError("situation belongs to a different source event")
        if str(item.get("correlation_id") or "") != str(correlation_id):
            raise ValueError("situation belongs to a different correlation")

    def _normalize_salience(self, components: dict[str, Any]) -> dict[str, Any]:
        normalized: dict[str, float] = {}
        weighted_total = 0.0
        total_weight = 0.0
        for raw_name, raw_value in list(components.items())[:32]:
            name = self._text(raw_name, max_length=120)
            if not name:
                continue
            if isinstance(raw_value, dict):
                score = self._score(raw_value.get("score"))
                weight = max(0.0, self._number(raw_value.get("weight"), default=1.0))
            else:
                score = self._score(raw_value)
                weight = 1.0
            normalized[name] = score
            weighted_total += score * weight
            total_weight += weight
        return {
            "components": normalized,
            "score": round(weighted_total / total_weight, 6) if total_weight else 0.0,
        }

    def _decision_record(
        self,
        value: Any,
        *,
        source: str,
        evidence_refs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        model_source = self._is_model_source(source)
        return {
            "decision_id": f"sdec_{uuid4().hex[:16]}",
            "value": (
                self._nonfactual_value(value, epistemic_status="inference")
                if model_source
                else self._bounded_value(value)
            ),
            "source": self._text(source, max_length=120) or "unknown",
            "epistemic_status": "inference" if model_source else "decision",
            "is_fact": False,
            "evidence_refs": copy.deepcopy(evidence_refs),
            "recorded_at": self._now_iso(),
        }

    def _prediction_record(
        self,
        value: Any,
        *,
        source: str,
        evidence_refs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "prediction_id": f"spred_{uuid4().hex[:16]}",
            "value": self._nonfactual_value(value, epistemic_status="prediction"),
            "source": self._text(source, max_length=120) or "model",
            "epistemic_status": "prediction",
            "is_fact": False,
            "evidence_refs": copy.deepcopy(evidence_refs),
            "recorded_at": self._now_iso(),
        }

    def _outcome_record(
        self,
        value: Any,
        *,
        source: str,
        evidence_refs: list[dict[str, Any]],
        prediction_id: str | None,
        evidence_verified: bool = False,
    ) -> dict[str, Any]:
        model_source = self._is_model_source(source)
        verified_source = str(source or "").strip().lower() in {
            "observed",
            "probe",
            "tool_proxy",
            "verified",
            "verifier",
        }
        return {
            "outcome_id": f"sout_{uuid4().hex[:16]}",
            "value": (
                self._nonfactual_value(value, epistemic_status="inference")
                if model_source
                else self._bounded_value(value)
            ),
            "source": self._text(source, max_length=120) or "unknown",
            "epistemic_status": "inference" if model_source else "observation",
            "is_fact": bool(
                verified_source
                and evidence_verified
                and evidence_refs
                and not model_source
            ),
            "evidence_refs": copy.deepcopy(evidence_refs),
            "prediction_ref": prediction_id,
            "recorded_at": self._now_iso(),
        }

    def _epistemic_record(
        self,
        value: Any,
        *,
        source: str,
        epistemic_status: str,
        evidence_refs: list[dict[str, Any]],
        evidence_verified: bool = False,
    ) -> dict[str, Any]:
        model_source = self._is_model_source(source)
        selected_status = "inference" if model_source else epistemic_status
        return {
            "record_id": f"sepi_{uuid4().hex[:16]}",
            "value": (
                self._nonfactual_value(value, epistemic_status="inference")
                if model_source
                else self._bounded_value(value)
            ),
            "source": self._text(source, max_length=120) or "unknown",
            "epistemic_status": selected_status,
            "is_fact": bool(
                not model_source
                and selected_status in {"observation", "verified"}
                and evidence_verified
                and evidence_refs
            ),
            "evidence_refs": copy.deepcopy(evidence_refs),
            "recorded_at": self._now_iso(),
        }

    def _structured_anchor_refs(
        self,
        *,
        event: VeyraEvent,
        payload: dict[str, Any],
        correlation_id: str,
        source_event_id: str,
        goal_refs: list[dict[str, Any]],
        commitment_refs: list[dict[str, Any]],
    ) -> list[dict[str, str]]:
        """Persist only explicit typed identifiers used for deterministic grouping."""

        anchors: dict[str, StructuredAnchor] = {}

        def add(kind: str, value: Any) -> None:
            ref_id: Any = value
            if isinstance(value, dict):
                ref_id = (
                    value.get("ref_id")
                    or value.get("id")
                    or value.get(f"{kind}_id")
                    or value.get("value")
                )
            anchor = StructuredAnchor(kind, str(ref_id or ""))
            anchors[anchor.key] = anchor

        for item in goal_refs:
            add("goal", item)
        for item in commitment_refs:
            add("commitment", item)

        subjects = (
            event.subject
            if isinstance(event.subject, list)
            else [event.subject]
        )
        for item in subjects:
            if not isinstance(item, dict):
                continue
            kind = str(
                item.get("kind")
                or item.get("type")
                or item.get("subject_type")
                or ""
            ).strip().lower().removesuffix("_ref").removesuffix(
                "_reference"
            )
            if kind not in ANCHOR_KINDS:
                continue
            add(kind, item)

        for field, kind in self._STRUCTURED_ANCHOR_DIRECT_FIELDS.items():
            if field in payload and payload.get(field) is not None:
                add(kind, payload.get(field))
        for field, kind in self._STRUCTURED_ANCHOR_COLLECTION_FIELDS.items():
            if field not in payload:
                continue
            raw = payload.get(field)
            values = raw if isinstance(raw, list) else [raw]
            for item in values:
                if item is not None:
                    add(kind, item)

        if correlation_id and correlation_id != source_event_id:
            add("trace", correlation_id)

        if sum(anchor.kind == "workspace" for anchor in anchors.values()) > 1:
            raise ValueError("one event cannot bind multiple workspaces")
        return [anchors[key].to_dict() for key in sorted(anchors)]

    @staticmethod
    def _normalized_structured_anchor_refs(
        value: Any,
    ) -> list[dict[str, str]]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("structured_anchor_refs must be a list")
        anchors: dict[str, StructuredAnchor] = {}
        for item in value:
            anchor = StructuredAnchor.from_dict(item)
            anchors[anchor.key] = anchor
        if sum(anchor.kind == "workspace" for anchor in anchors.values()) > 1:
            raise ValueError("one situation cannot bind multiple workspaces")
        return [anchors[key].to_dict() for key in sorted(anchors)]

    def _merge_structured_anchor_refs(
        self,
        existing: Any,
        incoming: Any,
        *,
        replace: bool,
    ) -> list[dict[str, str]]:
        current = self._normalized_structured_anchor_refs(existing)
        selected = self._normalized_structured_anchor_refs(incoming)
        current_workspace = next(
            (
                item["ref_id"]
                for item in current
                if item["kind"] == "workspace"
            ),
            None,
        )
        incoming_workspace = next(
            (
                item["ref_id"]
                for item in selected
                if item["kind"] == "workspace"
            ),
            None,
        )
        if current_workspace is not None and (
            incoming_workspace != current_workspace
        ):
            raise SituationAccessError(
                "situation belongs to a different workspace"
            )
        if replace:
            return copy.deepcopy(selected)
        return self._normalized_structured_anchor_refs([*current, *selected])

    def _context_binding_provenance(
        self,
        value: Any,
        *,
        expected_source_event_id: str,
        anchors: list[dict[str, str]],
    ) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            raise ValueError("context binding provenance must be a list")
        anchor_keys = {
            StructuredAnchor.from_dict(item).key
            for item in anchors
        }
        selected: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                raise ValueError("context binding provenance entry is invalid")
            anchor = StructuredAnchor(
                str(item.get("kind") or ""),
                str(item.get("ref_id") or ""),
            )
            if anchor.key not in anchor_keys:
                raise ValueError("context binding provenance references an unknown anchor")
            if (
                str(item.get("source_event_id") or "")
                != expected_source_event_id
                or item.get("is_fact") is not False
                or item.get("causality_asserted") is not False
                or item.get("authority") is not False
                or str(item.get("epistemic_status") or "")
                != "context_hypothesis"
            ):
                raise ValueError("context binding provenance weakens its epistemic boundary")
            raw_score = item.get("semantic_score")
            if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
                raise ValueError("context binding semantic score is invalid")
            score = float(raw_score)
            if not 0.0 <= score <= 1.0:
                raise ValueError("context binding semantic score is out of range")
            quote = item.get("source_quote") if isinstance(item.get("source_quote"), dict) else {}
            start = quote.get("start")
            end = quote.get("end")
            digest = str(quote.get("digest") or "")
            if (
                isinstance(start, bool)
                or not isinstance(start, int)
                or isinstance(end, bool)
                or not isinstance(end, int)
                or start < 0
                or end <= start
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise ValueError("context binding source quote proof is invalid")
            candidate_source = (
                item.get("candidate_source")
                if isinstance(item.get("candidate_source"), dict)
                else None
            )
            candidate_revision = 0
            if candidate_source is not None:
                raw_revision = candidate_source.get("revision")
                if (
                    isinstance(raw_revision, bool)
                    or not isinstance(raw_revision, int)
                    or raw_revision < 0
                ):
                    raise ValueError("context binding candidate revision is invalid")
                candidate_revision = raw_revision
            selected.append(
                {
                    "kind": anchor.kind,
                    "ref_id": anchor.ref_id,
                    "act_id": self._text(item.get("act_id"), max_length=80),
                    "relation": self._text(item.get("relation"), max_length=80)
                    or "about",
                    "source": self._text(item.get("source"), max_length=120),
                    "semantic_score": round(score, 6),
                    "source_quote": {
                        "start": start,
                        "end": end,
                        "digest": digest,
                    },
                    "candidate_snapshot_digest": self._text(
                        item.get("candidate_snapshot_digest"),
                        max_length=128,
                    ),
                    "candidate_source": (
                        {
                            "file": self._text(candidate_source.get("file"), max_length=120),
                            "revision": candidate_revision,
                            "candidate_token": self._text(
                                candidate_source.get("candidate_token"),
                                max_length=120,
                            ),
                        }
                        if candidate_source is not None
                        else None
                    ),
                    "epistemic_status": "context_hypothesis",
                    "is_fact": False,
                    "causality_asserted": False,
                    "authority": False,
                    "source_event_id": expected_source_event_id,
                }
            )
        if not selected:
            raise ValueError("context binding provenance cannot be empty")
        return selected[:8]

    def _normalize_refs(
        self,
        refs: Iterable[Any] | Any | None,
        *,
        evidence: bool = False,
    ) -> list[dict[str, Any]]:
        if refs is None:
            return []
        if isinstance(refs, (str, bytes, dict)) or is_dataclass(refs):
            values = [refs]
        else:
            try:
                values = list(refs)
            except TypeError:
                values = [refs]
        normalized: list[dict[str, Any]] = []
        for raw in values[:64]:
            if is_dataclass(raw):
                raw = asdict(raw)
            if isinstance(raw, dict):
                item = self._bounded_value(raw)
                if not isinstance(item, dict):
                    continue
            else:
                value = self._text(raw, max_length=1000)
                if not value:
                    continue
                item = {"ref_id": value}
            if not item.get("ref_id"):
                for key in (
                    "evidence_id",
                    "claim_id",
                    "goal_id",
                    "commitment_id",
                    "event_id",
                    "id",
                    "uri",
                ):
                    if item.get(key):
                        item["ref_id"] = self._text(item[key], max_length=1000)
                        break
            source = str(item.get("source_type") or item.get("source") or "")
            epistemic = str(item.get("epistemic_status") or item.get("kind") or "").lower()
            if self._is_model_source(source) or epistemic in {"inference", "prediction"}:
                item["epistemic_status"] = "prediction" if epistemic == "prediction" else "inference"
                item["is_fact"] = False
            elif evidence:
                # Incoming references are pointers, not proof. Only a trusted
                # resolver may promote the enclosing outcome after resolving
                # the reference against an authoritative ledger.
                item["epistemic_status"] = "reference"
                item["is_fact"] = False
            normalized.append(item)
        return self._dedupe_refs(normalized)

    def _refs_from_payload(
        self,
        payload: dict[str, Any],
        *,
        plural: str,
        singular: tuple[str, ...],
        evidence: bool = False,
    ) -> list[dict[str, Any]]:
        values: list[Any] = []
        if plural in payload:
            raw = payload.get(plural)
            if isinstance(raw, list):
                values.extend(raw)
            elif raw is not None:
                values.append(raw)
        for key in singular:
            if payload.get(key) is not None:
                values.append(payload[key])
        return self._normalize_refs(values, evidence=evidence)

    def _subject_refs(self, subject: Any, *, kind: str) -> list[dict[str, Any]]:
        """Extract only explicit typed references from an event subject."""

        subjects = subject if isinstance(subject, list) else [subject]
        selected: list[Any] = []
        for item in subjects:
            if not isinstance(item, dict):
                continue
            subject_kind = str(
                item.get("subject_type")
                or item.get("type")
                or item.get("kind")
                or ""
            ).strip().lower()
            if subject_kind not in {kind, f"{kind}_ref", f"{kind}_reference"}:
                continue
            selected.append(item)
        return self._normalize_refs(selected)

    def _merge_refs(
        self,
        first: list[dict[str, Any]],
        second: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return self._dedupe_refs([*first, *second])[:64]

    def _dedupe_refs(self, values: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in values:
            key = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        return deduped

    def _append_history(self, existing: Any, item: dict[str, Any]) -> list[dict[str, Any]]:
        history = [entry for entry in existing if isinstance(entry, dict)] if isinstance(existing, list) else []
        history.append(copy.deepcopy(item))
        return history[-self.max_history_per_situation :]

    def _extend_history(self, existing: Any, additions: Any) -> list[dict[str, Any]]:
        history = [entry for entry in existing if isinstance(entry, dict)] if isinstance(existing, list) else []
        if isinstance(additions, list):
            history.extend(copy.deepcopy(entry) for entry in additions if isinstance(entry, dict))
        return history[-self.max_history_per_situation :]

    def _merge_observation_history(
        self,
        existing: Any,
        additions: Any,
    ) -> list[dict[str, Any]]:
        history = (
            [copy.deepcopy(entry) for entry in existing if isinstance(entry, dict)]
            if isinstance(existing, list)
            else []
        )
        if isinstance(additions, list):
            history.extend(
                copy.deepcopy(entry)
                for entry in additions
                if isinstance(entry, dict)
            )

        seen_ids: set[str] = set()
        unsequenced: list[dict[str, Any]] = []
        sequenced: list[dict[str, Any]] = []
        for entry in history:
            identity = self._text(entry.get("observation_id"), max_length=240)
            if identity:
                if identity in seen_ids:
                    continue
                seen_ids.add(identity)
            sequence = self._stored_observation_sequence(
                entry.get("observation_sequence")
            )
            if sequence is None:
                unsequenced.append(entry)
            else:
                sequenced.append(entry)
        sequenced.sort(
            key=lambda entry: (
                self._stored_observation_sequence(
                    entry.get("observation_sequence")
                )
                or 0,
                str(entry.get("observation_id") or ""),
                str(entry.get("source_event_id") or ""),
            )
        )
        return [*unsequenced, *sequenced][-self.max_history_per_situation :]

    def _observation_identity_fingerprint(
        self,
        situation: dict[str, Any],
        observation_id: str,
    ) -> str | None:
        selected_id = self._text(observation_id, max_length=240)
        if not selected_id:
            return None
        if (
            self._text(situation.get("observation_id"), max_length=240)
            == selected_id
        ):
            fingerprint = self._text(
                situation.get("source_observation_fingerprint"),
                max_length=240,
            )
            if fingerprint:
                return fingerprint
        observations = (
            situation.get("observations")
            if isinstance(situation.get("observations"), list)
            else []
        )
        for entry in observations:
            if not isinstance(entry, dict):
                continue
            if (
                self._text(entry.get("observation_id"), max_length=240)
                != selected_id
            ):
                continue
            return self._text(
                entry.get("observation_fingerprint"),
                max_length=240,
            )
        return None

    def _sequence_aware_observation(
        self,
        existing: dict[str, Any],
        incoming: dict[str, Any],
    ) -> bool:
        return any(
            key in item
            for item in (existing, incoming)
            for key in ("observation_sequence", "observation_id")
        )

    def _incoming_observation_is_head(
        self,
        existing: dict[str, Any],
        incoming: dict[str, Any],
    ) -> bool:
        existing_sequence = self._stored_observation_sequence(
            existing.get("observation_sequence")
        )
        incoming_sequence = self._stored_observation_sequence(
            incoming.get("observation_sequence")
        )
        if existing_sequence is None and incoming_sequence is None:
            return True
        if incoming_sequence is None:
            return False
        if existing_sequence is None:
            return True
        existing_id = self._text(
            existing.get("observation_id")
            or existing.get("source_event_id"),
            max_length=240,
        )
        incoming_id = self._text(
            incoming.get("observation_id")
            or incoming.get("source_event_id"),
            max_length=240,
        )
        return (incoming_sequence, incoming_id) > (
            existing_sequence,
            existing_id,
        )

    @staticmethod
    def _normalize_observation_sequence(value: Any) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValueError("observation_sequence must be a non-negative integer")
        if isinstance(value, float) and not value.is_integer():
            raise ValueError("observation_sequence must be a non-negative integer")
        try:
            selected = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "observation_sequence must be a non-negative integer"
            ) from exc
        if selected < 0:
            raise ValueError("observation_sequence must be a non-negative integer")
        return selected

    @staticmethod
    def _stored_observation_sequence(value: Any) -> int | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            selected = int(value)
        except (TypeError, ValueError):
            return None
        return selected if selected >= 0 else None

    def _state_situations(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        raw = state.get("situations") if isinstance(state, dict) else []
        if isinstance(raw, dict):
            raw = list(raw.values())
        if not isinstance(raw, list):
            return []
        return [copy.deepcopy(item) for item in raw if isinstance(item, dict)]

    def _status_filter(self, status: str | Iterable[str] | None) -> set[str]:
        if status is None:
            return set()
        if isinstance(status, str):
            return {status}
        return {str(item) for item in status}

    def _status(self, value: Any, *, default: str) -> str:
        selected = self._text(value, max_length=120).strip().lower().replace(" ", "_")
        return selected or default

    def _is_terminal(self, status: Any) -> bool:
        return self._status(status, default="") in self._TERMINAL_STATUSES

    def _monotonic_status(self, existing: Any, incoming: Any) -> str:
        current = self._status(existing, default="observed")
        selected = self._status(incoming, default=current)
        if current in self._TERMINAL_STATUSES and selected not in self._TERMINAL_STATUSES:
            return current
        return selected

    def _observation_fingerprint(self, incoming: dict[str, Any]) -> str:
        """Identify an observation call without UUIDs, clocks, or due-time resets."""

        semantic = {
            key: incoming.get(key)
            for key in (
                "situation_id",
                "correlation_id",
                "user_id",
                "session_id",
                "channel",
                "source_event",
                "source_event_id",
                "source_event_type",
                "goal_refs",
                "commitment_refs",
                "structured_anchor_refs",
                "context_bindings",
                "evidence_refs",
                "salience_components",
                "salience_score",
                "status",
                "decision",
                "prediction",
                "outcome",
                "observations",
                "inferences",
            )
        }
        for key in ("observation_sequence", "observation_id"):
            if key in incoming:
                semantic[key] = incoming[key]
        stable = self._stable_transition_value(semantic)
        encoded = json.dumps(
            stable,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return f"sobs_{hashlib.sha256(encoded).hexdigest()[:32]}"

    def _stable_transition_value(self, value: Any) -> Any:
        """Remove locally generated identity/time fields from replay matching."""

        volatile_keys = {
            "created_at",
            "decision_id",
            "outcome_id",
            "prediction_id",
            "prediction_ref",
            "record_id",
            "recorded_at",
            "trace_id",
            "transition_id",
            "updated_at",
        }
        if isinstance(value, list):
            return [self._stable_transition_value(item) for item in value]
        if not isinstance(value, dict):
            return value
        return {
            key: self._stable_transition_value(item)
            for key, item in value.items()
            if key not in volatile_keys
        }

    def _is_model_source(self, source: Any) -> bool:
        selected = str(source or "").strip().lower()
        return selected in self._MODEL_SOURCES or selected.startswith("model:") or selected.endswith("_model")

    def _bounded_value(
        self,
        value: Any,
        *,
        depth: int = 0,
        max_depth: int = 6,
    ) -> Any:
        if depth >= max_depth:
            return self._text(value, max_length=500)
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, Enum):
            return self._bounded_value(value.value, depth=depth + 1, max_depth=max_depth)
        if is_dataclass(value):
            return self._bounded_value(asdict(value), depth=depth + 1, max_depth=max_depth)
        if isinstance(value, str):
            return self._text(value, max_length=4000)
        if isinstance(value, bytes):
            return self._text(value.decode("utf-8", errors="replace"), max_length=4000)
        if isinstance(value, dict):
            bounded: dict[str, Any] = {}
            for raw_key, raw_value in list(value.items())[:48]:
                key = self._text(raw_key, max_length=200)
                if key:
                    bounded[key] = self._bounded_value(raw_value, depth=depth + 1, max_depth=max_depth)
            return bounded
        if isinstance(value, (list, tuple, set, frozenset)):
            return [
                self._bounded_value(item, depth=depth + 1, max_depth=max_depth)
                for item in list(value)[:48]
            ]
        return self._text(value, max_length=1000)

    def _nonfactual_value(self, value: Any, *, epistemic_status: str) -> Any:
        """Prevent a model payload from smuggling a factuality label."""

        bounded = self._bounded_value(value)

        def rewrite(item: Any) -> Any:
            if isinstance(item, list):
                return [rewrite(value) for value in item]
            if not isinstance(item, dict):
                return item
            rewritten = {key: rewrite(value) for key, value in item.items()}
            for key in list(rewritten):
                normalized = key.strip().lower()
                if normalized in {"is_fact", "verified_as_fact", "factual"}:
                    rewritten[key] = False
                elif normalized == "epistemic_status":
                    rewritten[key] = epistemic_status
            return rewritten

        return rewrite(bounded)

    def _text(self, value: Any, *, max_length: int) -> str:
        if value is None:
            return ""
        selected = str(value)
        return selected[:max_length]

    def _score(self, value: Any) -> float:
        return max(0.0, min(self._number(value, default=0.0), 1.0))

    def _number(self, value: Any, *, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _now_iso(self) -> str:
        current = self._clock()
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return current.astimezone(timezone.utc).isoformat()

    def _iso_time(self, value: Any, *, fallback: str) -> str:
        parsed = self._datetime_or_none(value)
        if parsed is None:
            return fallback
        return parsed.astimezone(timezone.utc).isoformat()

    def _datetime(self, value: datetime | str) -> datetime:
        parsed = self._datetime_or_none(value)
        if parsed is None:
            raise ValueError(f"invalid datetime: {value!r}")
        return parsed

    def _datetime_or_none(self, value: Any) -> datetime | None:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str) and value.strip():
            try:
                parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            except ValueError:
                return None
        else:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
