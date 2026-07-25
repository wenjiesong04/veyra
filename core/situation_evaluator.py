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


class SituationAccessError(PermissionError):
    """Raised when a caller tries to mutate another user's situation."""


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

    def __init__(
        self,
        state_store: WorldStateStore,
        *,
        state_file: str = STATE_FILE,
        trace_file: str = TRACE_FILE,
        max_situations: int = 500,
        max_situations_per_user: int = 200,
        max_history_per_situation: int = 24,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if max_situations < 1:
            raise ValueError("max_situations must be at least 1")
        if max_situations_per_user < 1:
            raise ValueError("max_situations_per_user must be at least 1")
        if max_history_per_situation < 1:
            raise ValueError("max_history_per_situation must be at least 1")
        self.state_store = state_store
        self.state_file = str(state_file)
        self.trace_file = str(trace_file)
        self.max_situations = int(max_situations)
        self.max_situations_per_user = min(int(max_situations_per_user), self.max_situations)
        self.max_history_per_situation = int(max_history_per_situation)
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
    ) -> dict[str, Any]:
        """Observe one event and create or update its evidence-linked situation.

        Only explicit structured fields are consumed from ``event.payload``.
        The evaluator never scans message text for keywords and never produces an
        authorization or routing decision.
        """

        source = self._event_source(event)
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
        persisted: dict[str, Any] = {}
        evicted_ids: list[str] = []
        replayed = False

        def upsert(state: dict[str, Any]) -> dict[str, Any]:
            nonlocal persisted, evicted_ids, replayed
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
                replayed = source["event_id"] == str(existing.get("source_event_id") or "")
                merged = self._merge_observation(existing, incoming)
                situations[existing_index] = merged
                persisted = copy.deepcopy(merged)
            else:
                situations.append(incoming)
                persisted = copy.deepcopy(incoming)
            situations, evicted_ids = self._bound_state(situations)
            state["schema_version"] = self.SCHEMA_VERSION
            state["situations"] = situations
            state["count"] = len(situations)
            state["updated_at"] = now
            return state

        self.state_store.mutate_json(self.state_file, upsert)
        self._append_trace(
            trace_type="situation_observation_replayed" if replayed else "situation_observed",
            situation=persisted,
            detail={
                "source_event": source,
                "goal_refs": normalized_goals,
                "commitment_refs": normalized_commitments,
                "evidence_refs": normalized_evidence,
                "salience_components": normalized_salience["components"],
                "salience_score": normalized_salience["score"],
                "inference": inference_record,
                "prediction": prediction_record,
                "decision": decision_record,
                "outcome": outcome_record,
                "evicted_situation_ids": evicted_ids,
            },
        )
        return copy.deepcopy(persisted)

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
        )
        self._append_trace(
            trace_type="situation_decision_recorded",
            situation=persisted,
            detail={
                "decision": decision_record,
                "prediction": prediction_record,
                "evidence_refs": normalized_evidence,
            },
        )
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
        )
        self._append_trace(
            trace_type="situation_outcome_recorded",
            situation=persisted,
            detail={
                "outcome": outcome_record,
                "prediction_ref": outcome_record.get("prediction_ref"),
                "evidence_refs": normalized_evidence,
            },
        )
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
            return state

        self.state_store.mutate_json(self.state_file, update)
        if persisted is None:  # pragma: no cover - guarded by the mutation above.
            raise KeyError(f"unknown situation: {selected_id}")
        return persisted

    def _merge_observation(self, existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
        merged = copy.deepcopy(existing)
        merged["correlation_id"] = incoming["correlation_id"]
        merged["channel"] = incoming["channel"]
        merged["source_event"] = incoming["source_event"]
        merged["source_event_id"] = incoming["source_event_id"]
        merged["source_event_type"] = incoming["source_event_type"]
        for key in ("goal_refs", "commitment_refs", "evidence_refs"):
            merged[key] = self._merge_refs(
                self._normalize_refs(merged.get(key), evidence=key == "evidence_refs"),
                incoming[key],
            )
        merged["salience_components"] = {
            **(merged.get("salience_components") if isinstance(merged.get("salience_components"), dict) else {}),
            **incoming["salience_components"],
        }
        merged["salience_score"] = self._normalize_salience(merged["salience_components"])["score"]
        merged["status"] = incoming["status"]
        merged["next_evaluation_at"] = incoming["next_evaluation_at"]
        for singular, history in (
            ("decision", "decision_history"),
            ("prediction", "prediction_history"),
            ("outcome", "outcome_history"),
        ):
            if incoming.get(singular) is not None:
                merged[singular] = incoming[singular]
                merged[history] = self._append_history(merged.get(history), incoming[singular])
        merged["observations"] = self._extend_history(merged.get("observations"), incoming.get("observations"))
        merged["inferences"] = self._extend_history(merged.get("inferences"), incoming.get("inferences"))
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

    def _append_trace(
        self,
        *,
        trace_type: str,
        situation: dict[str, Any],
        detail: dict[str, Any],
    ) -> None:
        self.state_store.append_jsonl(
            self.trace_file,
            {
                "schema_version": self.TRACE_SCHEMA_VERSION,
                "trace_id": f"strace_{uuid4().hex[:16]}",
                "trace_type": trace_type,
                "situation_id": situation.get("situation_id"),
                "correlation_id": situation.get("correlation_id"),
                "user_id": situation.get("user_id"),
                "session_id": situation.get("session_id"),
                "source_event_id": situation.get("source_event_id"),
                "source_event_type": situation.get("source_event_type"),
                "status": situation.get("status"),
                "timestamp": self._now_iso(),
                "detail": self._bounded_value(detail),
            },
        )

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
