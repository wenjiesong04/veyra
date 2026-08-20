#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.situation_evaluator import (  # noqa: E402
    SituationAccessError,
    SituationEvaluator,
    SituationTraceBackpressureError,
)
from core.situation_state_repository import (  # noqa: E402
    SituationStateCapacityError,
    SituationStateError,
    SituationStateRepository,
)
from core.world_state import WorldStateStore  # noqa: E402
from interface.event_schema import EventSource, EventType, VeyraEvent  # noqa: E402


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 25, 8, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: Any) -> None:
        self.value += timedelta(**kwargs)


class FaultInjectingWorldStateStore(WorldStateStore):
    def __init__(self, root: Path) -> None:
        self.state_mutation_failures = 0
        super().__init__(root)
        self.trace_failure_modes: list[str] = []

    def mutate_json(self, name: str, mutator: Any) -> dict[str, Any]:
        if (
            name == SituationEvaluator.STATE_FILE
            and self.state_mutation_failures > 0
        ):
            self.state_mutation_failures -= 1
            raise OSError("injected situation state mutation failure")
        return super().mutate_json(name, mutator)

    def append_jsonl(self, name: str, payload: dict[str, Any]) -> None:
        mode = (
            self.trace_failure_modes.pop(0)
            if name == SituationEvaluator.TRACE_FILE and self.trace_failure_modes
            else None
        )
        if mode == "before":
            raise OSError("injected trace append failure before write")
        super().append_jsonl(name, payload)
        if mode == "after":
            raise OSError("injected interruption after trace append")


class CountingSituationStateRepository(SituationStateRepository):
    """Expose repository validation so every legacy write seam is observable."""

    def __init__(self, state_store: WorldStateStore, **kwargs: Any) -> None:
        super().__init__(state_store, **kwargs)
        self.mutation_calls = 0
        self.validation_calls = 0

    def mutate(self, callback: Any) -> dict[str, Any]:
        self.mutation_calls += 1
        return super().mutate(callback)

    def validate(self, state: dict[str, Any]) -> dict[str, Any]:
        self.validation_calls += 1
        return super().validate(state)


def event(
    event_id: str,
    *,
    user_id: str,
    session_id: str,
    payload: dict[str, Any] | None = None,
) -> VeyraEvent:
    return VeyraEvent(
        type=EventType.USER_MESSAGE,
        source=EventSource(channel="api", user_id=user_id, session_id=session_id),
        payload=payload or {},
        event_id=event_id,
        timestamp="2026-07-25T08:00:00+00:00",
        correlation_id=(payload or {}).get("correlation_id"),
    )


def test_evidence_linked_lifecycle(root: Path) -> None:
    clock = MutableClock()
    store = WorldStateStore(root)
    evaluator = SituationEvaluator(store, clock=clock)
    observed = evaluator.observe(
        event(
            "evt_a1",
            user_id="user-a",
            session_id="session-a",
            payload={
                "text": "无论这句话写成什么形式，都不应据此授权或路由。",
                "correlation_id": "corr-a",
                "goal_refs": ["goal-a"],
                "commitment_id": "commitment-a",
                "evidence_refs": [
                    {
                        "ref_id": "probe-1",
                        "source": "runtime_probe",
                        "epistemic_status": "verified",
                        "is_fact": True,
                    },
                    {
                        "ref_id": "model-claim-1",
                        "source": "core_model",
                        "epistemic_status": "verified",
                        "is_fact": True,
                    },
                ],
            },
        ),
        salience_components={
            "goal_relevance": {"score": 0.9, "weight": 2},
            "urgency": 0.6,
            "impact": 0.8,
        },
        observation={"event_seen": True},
        observation_source="event",
        inference={"possible_relation": "goal-a may be affected", "is_fact": True},
        prediction={"expected_change": "state may become stale", "is_fact": True},
    )
    expect(observed["correlation_id"] == "corr-a", "correlation is linked")
    expect(observed["goal_refs"][0]["ref_id"] == "goal-a", "goal reference is linked")
    expect(observed["commitment_refs"][0]["ref_id"] == "commitment-a", "commitment reference is linked")
    expect(
        observed["source_event"]["event_observed"]
        and not observed["source_event"]["payload_claims_verified"],
        "event occurrence does not certify payload claims",
        observed["source_event"],
    )
    expect(
        observed["inferences"][0]["epistemic_status"] == "inference"
        and observed["inferences"][0]["is_fact"] is False,
        "model inference is never persisted as fact",
        observed["inferences"],
    )
    expect(
        observed["prediction"]["epistemic_status"] == "prediction"
        and observed["prediction"]["is_fact"] is False,
        "model prediction is never persisted as fact",
        observed["prediction"],
    )
    expect(
        observed["prediction"]["value"]["is_fact"] is False
        and observed["inferences"][0]["value"]["is_fact"] is False,
        "embedded model factuality claims are neutralized",
        {"prediction": observed["prediction"], "inference": observed["inferences"]},
    )
    model_evidence = next(
        item for item in observed["evidence_refs"] if item.get("ref_id") == "model-claim-1"
    )
    probe_evidence = next(
        item for item in observed["evidence_refs"] if item.get("ref_id") == "probe-1"
    )
    expect(
        probe_evidence["epistemic_status"] == "reference"
        and probe_evidence["is_fact"] is False,
        "an inbound evidence reference cannot certify itself as fact",
        probe_evidence,
    )
    expect(
        model_evidence["epistemic_status"] == "inference" and model_evidence["is_fact"] is False,
        "model evidence reference cannot self-promote to fact",
        model_evidence,
    )
    expect(
        observed["observations"][0]["is_fact"] is False,
        "an unresolved evidence reference cannot promote its enclosing observation",
        observed["observations"][0],
    )
    expect(
        "route" not in observed and "authorization" not in observed and "text" not in observed,
        "evaluator neither routes nor authorizes from natural-language text",
        observed,
    )

    decision = evaluator.record_decision(
        observed["situation_id"],
        {"recommendation": "collect another observation"},
        user_id="user-a",
        session_id="session-a",
        evidence_refs=[{"ref_id": "policy-trace-1", "source": "guardian"}],
        prediction={"next_state": "more evidence"},
        source="core_model",
        status="monitoring",
        due_at=clock.value + timedelta(minutes=10),
    )
    expect(
        decision["decision"]["epistemic_status"] == "inference"
        and decision["decision"]["is_fact"] is False,
        "model decision is explicitly an inference",
        decision["decision"],
    )
    expect(
        decision["prediction"]["epistemic_status"] == "prediction"
        and decision["status"] == "monitoring",
        "decision and prediction are persisted",
        decision,
    )
    expect(
        evaluator.due_candidates(user_id="user-a", now=clock.value) == [],
        "future situation is not due",
    )
    clock.advance(minutes=11)
    due = evaluator.due_candidates(user_id="user-a", now=clock.value)
    expect(
        len(due) == 1 and due[0]["situation_id"] == observed["situation_id"],
        "due candidates are time and tenant scoped",
        due,
    )

    resolved = evaluator.record_outcome(
        observed["situation_id"],
        {"state": "fresh"},
        user_id="user-a",
        session_id="session-a",
        evidence_refs=[{"ref_id": "probe-2", "source": "runtime_probe"}],
        source="verifier",
        evidence_verified=True,
    )
    expect(
        resolved["status"] == "resolved"
        and resolved["outcome"]["is_fact"] is True
        and resolved["outcome"]["prediction_ref"],
        "verified evidence records a resolved outcome linked to its prediction",
        resolved,
    )
    expect(
        evaluator.due_candidates(user_id="user-a", now=clock.value) == [],
        "terminal situations are not returned as due",
    )

    state = store.read_json("situation_state.json")
    trace = store.read_jsonl("situation_trace.jsonl", limit=100)
    expect(
        state.get("schema_version") == SituationEvaluator.SCHEMA_VERSION
        and state.get("count") == 1,
        "bounded state document is persisted",
        state,
    )
    expect(
        [row.get("trace_type") for row in trace]
        == [
            "situation_observed",
            "situation_decision_recorded",
            "situation_outcome_recorded",
        ],
        "lifecycle trace is append-only and ordered",
        trace,
    )


def test_tenant_isolation_idempotence_and_bounds(root: Path) -> None:
    clock = MutableClock()
    store = WorldStateStore(root)
    evaluator = SituationEvaluator(
        store,
        clock=clock,
        max_situations=4,
        max_situations_per_user=2,
    )
    first = evaluator.observe(
        event("evt-a-1", user_id="user-a", session_id="session-a"),
        salience_components={"impact": 0.2},
    )
    replay = evaluator.observe(
        event("evt-a-1", user_id="user-a", session_id="session-a"),
        salience_components={"urgency": 0.8},
    )
    expect(
        replay["situation_id"] == first["situation_id"]
        and len(evaluator.list(user_id="user-a")) == 1,
        "same source event is idempotently upserted",
        evaluator.list(user_id="user-a"),
    )
    expect(
        replay["salience_components"] == {"impact": 0.2, "urgency": 0.8},
        "re-observation merges structured salience",
        replay,
    )

    other = evaluator.observe(
        event("evt-b-1", user_id="user-b", session_id="session-b"),
        salience_components={"impact": 0.7},
    )
    expect(
        evaluator.get(other["situation_id"], user_id="user-a") is None
        and [item["situation_id"] for item in evaluator.list(user_id="user-a")] == [first["situation_id"]],
        "reads are isolated by user",
    )
    try:
        evaluator.record_decision(
            other["situation_id"],
            {"recommendation": "must not write"},
            user_id="user-a",
        )
        raise AssertionError("cross-user mutation unexpectedly succeeded")
    except SituationAccessError:
        pass
    expect(
        evaluator.get(other["situation_id"], user_id="user-b")["decision"] is None,
        "cross-user mutation is rejected",
    )

    for index in range(2, 5):
        clock.advance(seconds=1)
        evaluator.observe(
            event(f"evt-a-{index}", user_id="user-a", session_id="session-a"),
            salience_components={"impact": index / 10},
        )
    user_a = evaluator.list(user_id="user-a", newest_first=False)
    user_b = evaluator.list(user_id="user-b")
    expect(
        len(user_a) == 2 and {item["source_event_id"] for item in user_a} == {"evt-a-3", "evt-a-4"},
        "per-user state is bounded to the newest situations",
        user_a,
    )
    expect(
        len(user_b) == 1 and user_b[0]["situation_id"] == other["situation_id"],
        "one noisy user does not erase another user within the global bound",
        user_b,
    )
    trace = store.read_jsonl("situation_trace.jsonl", limit=100)
    expect(
        len(trace) == 6
        and any(row.get("trace_type") == "situation_observation_replayed" for row in trace),
        "state eviction and replay never rewrite append-only trace history",
        trace,
    )

    state_path = store.path_for("situation_state.json")
    trace_path = store.path_for("situation_trace.jsonl")
    expect(
        state_path.exists()
        and trace_path.exists()
        and isinstance(json.loads(state_path.read_text(encoding="utf-8")), dict),
        "caller-selected logical state files are usable before layout mapping",
        {"state": state_path, "trace": trace_path},
    )


def test_terminal_replay_is_monotonic(root: Path) -> None:
    clock = MutableClock()
    store = WorldStateStore(root)
    evaluator = SituationEvaluator(store, clock=clock)
    situations: dict[str, dict[str, Any]] = {}

    monitoring_event = event(
        "evt-monitoring-replay",
        user_id="user-terminal",
        session_id="session-terminal",
        payload={"correlation_id": "corr-monitoring-replay"},
    )
    monitoring_observed = evaluator.observe(
        monitoring_event,
        salience_components={"impact": 0.2},
    )
    monitoring = evaluator.record_decision(
        monitoring_observed["situation_id"],
        {"recommendation": "continue monitoring"},
        user_id="user-terminal",
        session_id="session-terminal",
        status="monitoring",
        due_at=clock.value + timedelta(hours=3),
    )
    monitoring_replay = evaluator.observe(
        monitoring_event,
        status="observed",
        salience_components={"impact": 0.8},
        next_evaluation_at=clock.value + timedelta(minutes=5),
    )
    expect(
        monitoring_replay["status"] == "monitoring"
        and monitoring_replay["next_evaluation_at"] == monitoring["next_evaluation_at"],
        "same-event replay cannot roll back a non-terminal lifecycle or schedule",
        {
            "before": monitoring,
            "after": monitoring_replay,
        },
    )

    for index, terminal_status in enumerate(sorted(SituationEvaluator._TERMINAL_STATUSES)):
        source_event = event(
            f"evt-terminal-{index}",
            user_id="user-terminal",
            session_id="session-terminal",
            payload={"correlation_id": f"corr-terminal-{index}"},
        )
        scheduled_at = clock.value + timedelta(hours=2)
        observed = evaluator.observe(
            source_event,
            salience_components={"impact": 0.1},
            next_evaluation_at=scheduled_at,
        )
        terminal = evaluator.record_outcome(
            observed["situation_id"],
            {"terminal_status": terminal_status},
            user_id="user-terminal",
            session_id="session-terminal",
            status=terminal_status,
        )
        original_schedule = terminal["next_evaluation_at"]
        clock.advance(seconds=1)
        replayed = evaluator.observe(
            source_event,
            status="observed",
            salience_components={"impact": 0.9},
            next_evaluation_at=clock.value + timedelta(days=1),
        )
        expect(
            replayed["status"] == terminal_status
            and replayed["next_evaluation_at"] == original_schedule,
            f"same-event replay preserves {terminal_status} lifecycle and schedule",
            {
                "before": terminal,
                "after": replayed,
            },
        )
        situations[terminal_status] = replayed

    partial = situations["partial"]
    refined = evaluator.record_outcome(
        partial["situation_id"],
        {"verification": "success"},
        user_id="user-terminal",
        session_id="session-terminal",
        status="verified_success",
    )
    expect(
        refined["status"] == "verified_success",
        "authoritative outcome may refine one terminal status into another",
        refined,
    )


def test_trace_outbox_recovers_append_failures(root: Path) -> None:
    clock = MutableClock()
    store = FaultInjectingWorldStateStore(root)
    evaluator = SituationEvaluator(store, clock=clock)
    before_event = event(
        "evt-trace-before",
        user_id="user-trace",
        session_id="session-trace",
        payload={"correlation_id": "corr-trace-before"},
    )

    store.trace_failure_modes = ["before"]
    committed_before = evaluator.observe(
        before_event,
        salience_components={"impact": 0.4},
    )
    expect(
        committed_before["source_event_id"] == "evt-trace-before",
        "trace failure does not misreport the durable observation as failed",
        committed_before,
    )

    pending_before = store.read_json("situation_state.json")
    pending_before_entries = pending_before.get("trace_outbox")
    pending_before_id = (
        pending_before_entries[0].get("transition_id")
        if isinstance(pending_before_entries, list)
        and pending_before_entries
        and isinstance(pending_before_entries[0], dict)
        else None
    )
    expect(
        pending_before.get("count") == 1
        and pending_before.get("trace_outbox_count") == 1
        and bool(pending_before_id)
        and store.read_jsonl("situation_trace.jsonl", limit=100) == [],
        "state transition retains a durable outbox after append failure",
        pending_before,
    )

    clock.advance(minutes=5)
    recovered_before = evaluator.observe(
        before_event,
        salience_components={"impact": 0.4},
    )
    trace_after_before_retry = store.read_jsonl("situation_trace.jsonl", limit=100)
    state_after_before_retry = store.read_json("situation_state.json")
    revision_after_recovery = state_after_before_retry.get("_state_revision")
    expect(
        recovered_before["source_event_id"] == "evt-trace-before"
        and state_after_before_retry.get("trace_outbox_count") == 0
        and len(trace_after_before_retry) == 1
        and trace_after_before_retry[0].get("transition_id") == pending_before_id,
        "retry repairs a pre-append failure without duplicating the observation",
        {
            "state": state_after_before_retry,
            "trace": trace_after_before_retry,
        },
    )

    clock.advance(minutes=5)
    evaluator.observe(
        before_event,
        salience_components={"impact": 0.4},
    )
    exact_replay_trace = store.read_jsonl("situation_trace.jsonl", limit=100)
    exact_replay_state = store.read_json("situation_state.json")
    expect(
        len(exact_replay_trace) == 1
        and exact_replay_state.get("_state_revision") == revision_after_recovery,
        "exact same-event replay is a state and trace no-op",
        {
            "state_revision": exact_replay_state.get("_state_revision"),
            "trace": exact_replay_trace,
        },
    )

    after_event = event(
        "evt-trace-after",
        user_id="user-trace",
        session_id="session-trace",
        payload={"correlation_id": "corr-trace-after"},
    )
    store.trace_failure_modes = ["after"]
    committed_after = evaluator.observe(
        after_event,
        salience_components={"impact": 0.7},
    )
    expect(
        committed_after["source_event_id"] == "evt-trace-after",
        "post-append interruption does not misreport the durable observation",
        committed_after,
    )

    pending_after = store.read_json("situation_state.json")
    trace_after_interruption = store.read_jsonl("situation_trace.jsonl", limit=100)
    pending_after_entries = pending_after.get("trace_outbox")
    pending_after_id = (
        pending_after_entries[0].get("transition_id")
        if isinstance(pending_after_entries, list)
        and pending_after_entries
        and isinstance(pending_after_entries[0], dict)
        else None
    )
    expect(
        pending_after.get("trace_outbox_count") == 1
        and len(trace_after_interruption) == 2
        and bool(pending_after_id)
        and trace_after_interruption[-1].get("transition_id") == pending_after_id,
        "append success before acknowledgement leaves a recoverable outbox",
        {
            "state": pending_after,
            "trace": trace_after_interruption,
        },
    )

    evaluator.observe(
        after_event,
        salience_components={"impact": 0.7},
    )
    recovered_after = store.read_json("situation_state.json")
    final_trace = store.read_jsonl("situation_trace.jsonl", limit=100)
    transition_ids = [row.get("transition_id") for row in final_trace]
    expect(
        recovered_after.get("trace_outbox_count") == 0
        and len(final_trace) == 2
        and len(set(transition_ids)) == 2,
        "retry deduplicates an already-appended transition before acknowledging it",
        {
            "state": recovered_after,
            "trace": final_trace,
        },
    )
    expect(
        [row.get("source_event_id") for row in final_trace]
        == ["evt-trace-before", "evt-trace-after"],
        "outbox recovery preserves trace order",
        final_trace,
    )

    store.trace_failure_modes = ["before", "before", "before", "before"]
    decision = evaluator.record_decision(
        committed_after["situation_id"],
        {"recommendation": "continue lifecycle"},
        user_id="user-trace",
        session_id="session-trace",
        status="monitoring",
    )
    expect(
        decision["status"] == "monitoring",
        "decision remains committed while trace delivery is unavailable",
        decision,
    )
    try:
        evaluator.flush_trace_outbox()
        raise AssertionError("explicit trace repair unexpectedly hid its delivery failure")
    except OSError as exc:
        expect(
            "before write" in str(exc),
            "explicit trace repair surfaces delivery failure to operators",
            str(exc),
        )

    outcome = evaluator.record_outcome(
        committed_after["situation_id"],
        {"state": "completed"},
        user_id="user-trace",
        session_id="session-trace",
        status="resolved",
    )
    lifecycle_pending = store.read_json("situation_state.json")
    expect(
        outcome["status"] == "resolved"
        and lifecycle_pending.get("trace_outbox_count") == 2
        and len(store.read_jsonl("situation_trace.jsonl", limit=100)) == 2,
        "outcome still commits after decision trace delivery fails",
        {
            "outcome": outcome,
            "state": lifecycle_pending,
        },
    )

    repair = evaluator.flush_trace_outbox()
    lifecycle_trace = store.read_jsonl("situation_trace.jsonl", limit=100)
    expect(
        repair == {
            "pending": 0,
            "appended": 2,
            "deduplicated": 0,
            "acknowledged": 2,
        }
        and [row.get("trace_type") for row in lifecycle_trace[-2:]]
        == ["situation_decision_recorded", "situation_outcome_recorded"],
        "explicit repair restores the complete lifecycle in transition order",
        {
            "repair": repair,
            "trace": lifecycle_trace,
        },
    )


def test_trace_outbox_backpressure_is_bounded(root: Path) -> None:
    store = FaultInjectingWorldStateStore(root)
    invalid_capacity_rejected = False
    try:
        SituationEvaluator(store, max_trace_outbox=1)
    except ValueError:
        invalid_capacity_rejected = True
    evaluator = SituationEvaluator(store, max_trace_outbox=2)
    store.trace_failure_modes = ["before"] * 20
    for index in range(2):
        evaluator.observe(
            event(
                f"evt-trace-cap-{index}",
                user_id="user-trace-cap",
                session_id="session-trace-cap",
                payload={"correlation_id": f"corr-trace-cap-{index}"},
            ),
            salience_components={"impact": 0.2 + index / 10},
        )

    rejected = False
    try:
        evaluator.observe(
            event(
                "evt-trace-cap-rejected",
                user_id="user-trace-cap",
                session_id="session-trace-cap",
                payload={"correlation_id": "corr-trace-cap-rejected"},
            ),
            salience_components={"impact": 0.9},
        )
    except RuntimeError as exc:
        rejected = "outbox capacity is exhausted" in str(exc)
    state = store.read_json("situation_state.json")
    expect(
        invalid_capacity_rejected
        and rejected
        and state.get("trace_outbox_count") == 2
        and state.get("count") == 2
        and all(
            item.get("source_event_id") != "evt-trace-cap-rejected"
            for item in state.get("situations", [])
            if isinstance(item, dict)
        ),
        "trace outage applies bounded backpressure before an unaudited transition commits",
        state,
    )

    byte_store = FaultInjectingWorldStateStore(root / "byte-cap")
    byte_evaluator = SituationEvaluator(
        byte_store,
        max_trace_outbox=10,
    )
    byte_store.trace_failure_modes = ["before"] * 10
    byte_evaluator.observe(
        event(
            "evt-trace-byte-cap-0",
            user_id="user-trace-byte-cap",
            session_id="session-trace-byte-cap",
        )
    )
    pending_bytes = int(
        byte_store.read_json("situation_state.json").get(
            "trace_outbox_bytes"
        )
        or 0
    )
    byte_evaluator.max_trace_outbox_bytes = pending_bytes
    total_bytes_rejected = False
    try:
        byte_evaluator.observe(
            event(
                "evt-trace-byte-cap-1",
                user_id="user-trace-byte-cap",
                session_id="session-trace-byte-cap",
            )
        )
    except SituationTraceBackpressureError:
        total_bytes_rejected = True

    entry_store = FaultInjectingWorldStateStore(root / "entry-cap")
    entry_evaluator = SituationEvaluator(
        entry_store,
        max_trace_entry_bytes=128,
        max_trace_outbox_bytes=1024,
    )
    entry_bytes_rejected = False
    try:
        entry_evaluator.observe(
            event(
                "evt-trace-entry-cap",
                user_id="user-trace-entry-cap",
                session_id="session-trace-entry-cap",
            )
        )
    except SituationTraceBackpressureError:
        entry_bytes_rejected = True
    expect(
        pending_bytes > 0
        and total_bytes_rejected
        and entry_bytes_rejected
        and byte_store.read_json("situation_state.json").get("count") == 1
        and entry_store.read_json("situation_state.json").get("count") == 0,
        "trace outbox enforces per-entry and total serialized byte bounds",
        {
            "pending_bytes": pending_bytes,
            "byte_state": byte_store.read_json("situation_state.json"),
            "entry_state": entry_store.read_json("situation_state.json"),
        },
    )


def test_atomic_resolution_is_retryable_and_idempotent(root: Path) -> None:
    store = FaultInjectingWorldStateStore(root)
    evaluator = SituationEvaluator(store)
    observed = evaluator.observe(
        event(
            "evt-resolution",
            user_id="user-resolution",
            session_id="session-resolution",
            payload={"correlation_id": "corr-resolution"},
        )
    )
    kwargs = {
        "idempotency_key": "resolution-key-1",
        "expected_source_event_id": "evt-resolution",
        "expected_correlation_id": "corr-resolution",
        "decision": {
            "route": "direct_answer",
            "status": "success",
            "risk_level": "R0",
        },
        "outcome": {
            "route": "direct_answer",
            "status": "success",
            "verification": {},
        },
        "user_id": "user-resolution",
        "session_id": "session-resolution",
        "decision_evidence_refs": [{"ref_id": "trace:resolution"}],
        "outcome_evidence_refs": [{"ref_id": "trace:resolution"}],
        "decision_source": "veyra_policy",
        "outcome_source": "runtime_result",
        "decision_status": "decided",
        "outcome_status": "resolved",
    }
    wrong_binding_rejected = False
    try:
        evaluator.record_resolution(
            observed["situation_id"],
            **{
                **kwargs,
                "idempotency_key": "resolution-key-wrong-event",
                "expected_source_event_id": "evt-other",
            },
        )
    except ValueError:
        wrong_binding_rejected = True
    after_rejection = evaluator.get(
        observed["situation_id"],
        user_id="user-resolution",
        session_id="session-resolution",
    )
    expect(
        wrong_binding_rejected
        and isinstance(after_rejection, dict)
        and after_rejection.get("decision") is None
        and after_rejection.get("outcome") is None,
        "atomic resolution rejects a mismatched source event before mutation",
        after_rejection,
    )
    store.state_mutation_failures = 1
    failed = False
    try:
        evaluator.record_resolution(observed["situation_id"], **kwargs)
    except OSError:
        failed = True
    unchanged = evaluator.get(
        observed["situation_id"],
        user_id="user-resolution",
        session_id="session-resolution",
    )
    resolved = evaluator.record_resolution(observed["situation_id"], **kwargs)
    replayed = evaluator.record_resolution(observed["situation_id"], **kwargs)
    trace = store.read_jsonl("situation_trace.jsonl", limit=20)
    expect(
        failed
        and isinstance(unchanged, dict)
        and unchanged.get("decision") is None
        and unchanged.get("outcome") is None
        and len(resolved.get("decision_history") or []) == 1
        and len(resolved.get("outcome_history") or []) == 1
        and replayed.get("decision", {}).get("decision_id")
        == resolved.get("decision", {}).get("decision_id")
        and replayed.get("outcome", {}).get("outcome_id")
        == resolved.get("outcome", {}).get("outcome_id")
        and [row.get("trace_type") for row in trace]
        == [
            "situation_observed",
            "situation_decision_recorded",
            "situation_outcome_recorded",
        ],
        "resolution commits decision and outcome atomically and replays by stable key",
        {
            "unchanged": unchanged,
            "resolved": resolved,
            "replayed": replayed,
            "trace": trace,
        },
    )


def _semantic_row(
    situation_id: str,
    *,
    user_id: str,
    session_id: str,
    subject: str,
    lifecycle: str = "active",
    timestamp: str = "2026-07-25T08:00:00+00:00",
) -> dict[str, Any]:
    return {
        "record_kind": "semantic_situation",
        "situation_id": situation_id,
        "semantic_subject_key": subject,
        "user_id": user_id,
        "session_id": session_id,
        "status": lifecycle,
        "observation_revision": 1,
        "semantic": {
            "title": subject,
            "label": subject,
            "summary": subject,
            "goal": f"Understand {subject}",
            "category": "general",
            "lifecycle": lifecycle,
            "deadline_at": None,
            "progress": {"status": "unknown", "value": None},
            "entities": [],
            "known": [],
            "unknown": [],
            "assumptions": [],
            "timeline": [],
            "evidence": [],
            "material_change": "",
            "next_observation_at": None,
            "next_step": "",
            "next_step_epistemic_status": "inferred",
        },
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def test_shared_retention_protects_semantic_context(root: Path) -> None:
    """Mixed legacy/semantic writes use one bounded, fail-closed policy."""

    store = WorldStateStore(root)
    repository = SituationStateRepository(
        store,
        max_situations=4,
        max_situations_per_user=3,
    )
    repository.mutate(
        lambda state: {
            **state,
            "situations": [
                _semantic_row(
                    "sem-a-active",
                    user_id="user-a",
                    session_id="session-a",
                    subject="active-a",
                ),
                _semantic_row(
                    "sem-b-active",
                    user_id="user-b",
                    session_id="session-b",
                    subject="active-b",
                    timestamp="2026-07-25T08:00:01+00:00",
                ),
                _semantic_row(
                    "sem-a-terminal",
                    user_id="user-a",
                    session_id="session-a",
                    subject="terminal-a",
                    lifecycle="resolved",
                    timestamp="2026-07-25T08:00:02+00:00",
                ),
                {
                    "record_kind": "situation_candidate",
                    "situation_id": "legacy-a-old",
                    "user_id": "user-a",
                    "session_id": "session-a",
                    "status": "observed",
                    "created_at": "2026-07-25T08:00:03+00:00",
                    "updated_at": "2026-07-25T08:00:03+00:00",
                },
            ],
            "count": 4,
        }
    )
    evaluator = SituationEvaluator(
        store,
        max_situations=4,
        max_situations_per_user=3,
    )
    evaluator.observe(
        event(
            "legacy-a-new",
            user_id="user-a",
            session_id="session-a",
        )
    )
    rows = store.read_json("situation_state.json")["situations"]
    ids = {str(row["situation_id"]) for row in rows}
    expect(
        {"sem-a-active", "sem-b-active"}.issubset(ids)
        and any(str(row.get("source_event_id")) == "legacy-a-new" for row in rows)
        and "sem-a-terminal" not in ids
        and len(rows) == 4,
        "legacy writer preserves active semantic rows and evicts terminal semantic context first",
        rows,
    )

    # If all capacity is protected semantic context, a new legacy observation
    # fails before the state file changes and remains safely replayable.
    overflow_root = root / "overflow"
    overflow_store = WorldStateStore(overflow_root)
    overflow_repository = SituationStateRepository(
        overflow_store,
        max_situations=2,
        max_situations_per_user=2,
    )
    overflow_repository.mutate(
        lambda state: {
            **state,
            "situations": [
                _semantic_row(
                    "overflow-a",
                    user_id="overflow-user-a",
                    session_id="overflow-session-a",
                    subject="overflow-a",
                ),
                _semantic_row(
                    "overflow-b",
                    user_id="overflow-user-b",
                    session_id="overflow-session-b",
                    subject="overflow-b",
                ),
            ],
            "count": 2,
        }
    )
    overflow_evaluator = SituationEvaluator(
        overflow_store,
        max_situations=2,
        max_situations_per_user=2,
    )
    before = overflow_store.path_for("situation_state.json").read_bytes()
    rejected = False
    try:
        overflow_evaluator.observe(
            event(
                "overflow-legacy",
                user_id="overflow-user-a",
                session_id="overflow-session-a",
            )
        )
    except SituationStateCapacityError:
        rejected = True
    after_rejection = overflow_store.path_for("situation_state.json").read_bytes()
    replay_rejected = False
    try:
        overflow_evaluator.observe(
            event(
                "overflow-legacy",
                user_id="overflow-user-a",
                session_id="overflow-session-a",
            )
        )
    except SituationStateCapacityError:
        replay_rejected = True
    expect(
        rejected and replay_rejected and before == after_rejection
        and after_rejection == overflow_store.path_for("situation_state.json").read_bytes(),
        "protected-capacity overflow is fail-closed and byte-pure across replay",
    )

    duplicate_store = WorldStateStore(root / "duplicate")
    duplicate_repository = SituationStateRepository(duplicate_store)
    try:
        duplicate_repository.retain_rows(
            [{"situation_id": "duplicate"}, {"situation_id": "duplicate"}]
        )
    except SituationStateError:
        duplicate_rejected = True
    else:  # pragma: no cover - smoke assertion
        duplicate_rejected = False
    expect(duplicate_rejected, "duplicate Situation identities fail closed")


def test_legacy_write_entrypoints_use_repository(root: Path) -> None:
    """All five pre-repository state writers validate through one repository API."""

    store = FaultInjectingWorldStateStore(root)
    evaluator = SituationEvaluator(store)
    repository = CountingSituationStateRepository(store)
    evaluator._situation_repository = repository
    # Keep lifecycle traces pending so each public legacy write contributes one
    # repository mutation; the final flush exercises the fifth original writer
    # (trace-outbox acknowledgement) independently.
    store.trace_failure_modes = ["before"] * 100

    source_event = event(
        "evt-repository-entrypoints",
        user_id="repository-user",
        session_id="repository-session",
        payload={
            "correlation_id": "corr-repository-entrypoints",
            "goal_id": "goal-repository-entrypoints",
        },
    )
    observed = evaluator.observe(source_event)
    expect(
        repository.mutation_calls == 1 and repository.validation_calls >= 2,
        "observe uses repository mutation and validation",
        {
            "mutations": repository.mutation_calls,
            "validations": repository.validation_calls,
        },
    )

    binding_digest = "b" * 64
    binding = evaluator.record_context_binding(
        observed["situation_id"],
        expected_source_event_id=source_event.event_id,
        expected_observation_revision=1,
        operation_id="context-operation-1",
        binding_digest=binding_digest,
        anchors=[{"kind": "goal", "ref_id": "goal-repository-entrypoints"}],
        provenance=[
            {
                "kind": "goal",
                "ref_id": "goal-repository-entrypoints",
                "source_event_id": source_event.event_id,
                "epistemic_status": "context_hypothesis",
                "is_fact": False,
                "causality_asserted": False,
                "authority": False,
                "semantic_score": 0.8,
                "source_quote": {"start": 0, "end": 1, "digest": "a" * 64},
            }
        ],
        user_id="repository-user",
        session_id="repository-session",
    )
    expect(
        binding["observation_revision"] == 2
        and repository.mutation_calls == 2
        and repository.validation_calls >= 4,
        "context binding uses repository mutation and validation",
        {
            "binding": binding,
            "mutations": repository.mutation_calls,
            "validations": repository.validation_calls,
        },
    )

    decision = evaluator.record_decision(
        observed["situation_id"],
        {"recommendation": "keep observing"},
        user_id="repository-user",
        session_id="repository-session",
        status="monitoring",
    )
    expect(
        decision["status"] == "monitoring"
        and repository.mutation_calls == 3
        and repository.validation_calls >= 6,
        "shared decision/outcome writer uses repository mutation and validation",
        {
            "decision": decision,
            "mutations": repository.mutation_calls,
            "validations": repository.validation_calls,
        },
    )

    resolved = evaluator.record_resolution(
        observed["situation_id"],
        idempotency_key="resolution-repository-entrypoint",
        expected_source_event_id=source_event.event_id,
        expected_correlation_id="corr-repository-entrypoints",
        decision={"recommendation": "close observation"},
        outcome={"state": "closed"},
        user_id="repository-user",
        session_id="repository-session",
    )
    expect(
        resolved["status"] == "resolved"
        and repository.mutation_calls == 4
        and repository.validation_calls >= 8,
        "atomic resolution uses repository mutation and validation",
        {
            "resolution": resolved,
            "mutations": repository.mutation_calls,
            "validations": repository.validation_calls,
        },
    )

    store.trace_failure_modes = []
    repair = evaluator.flush_trace_outbox()
    expect(
        repair["pending"] == 0
        and repair["acknowledged"] > 0
        and repository.mutation_calls == 5
        and repository.validation_calls >= 10,
        "trace outbox acknowledgement uses repository mutation and validation",
        {
            "repair": repair,
            "mutations": repository.mutation_calls,
            "validations": repository.validation_calls,
        },
    )


def test_repository_noop_preserves_existing_valid_state(root: Path) -> None:
    """Repository validation must not rewrite an already-valid document."""

    store = WorldStateStore(root)
    repository = SituationStateRepository(store)
    repository.mutate(
        lambda state: {
            **state,
            "situations": [
                {
                    "record_kind": "situation_candidate",
                    "situation_id": "legacy-valid-row",
                    "user_id": "valid-user",
                    "session_id": "valid-session",
                    "status": "observed",
                    "created_at": "2026-07-25T08:00:00+00:00",
                    "updated_at": "2026-07-25T08:00:00+00:00",
                }
            ],
            "count": 1,
        }
    )
    path = store.path_for("situation_state.json")
    before = path.read_bytes()
    result = repository.mutate(lambda state: state)
    after = path.read_bytes()
    expect(
        result["situations"][0]["situation_id"] == "legacy-valid-row"
        and before == after,
        "valid existing state is unchanged by repository validation",
        {"before_bytes": len(before), "after_bytes": len(after)},
    )


def test_repository_rejects_semantic_lifecycle_drift(root: Path) -> None:
    """One semantic row cannot claim active and resolved at the same time."""

    repository = SituationStateRepository(WorldStateStore(root))
    state = repository.empty_state()
    state["situations"] = [
        {
            "record_kind": "semantic_situation",
            "situation_id": "semantic-lifecycle-drift",
            "semantic_subject_key": "subject-lifecycle-drift",
            "user_id": "semantic-user",
            "session_id": "semantic-session",
            "status": "active",
            "observation_revision": 1,
            "semantic": {
                "lifecycle": "resolved",
                "category": "general",
                "progress": {"status": "completed", "value": 1.0},
            },
        }
    ]
    state["count"] = 1
    try:
        repository.validate(state)
    except SituationStateError:
        rejected = True
    else:
        rejected = False
    expect(
        rejected,
        "semantic status and lifecycle cannot drift",
    )


def main() -> int:
    with TemporaryDirectory(prefix="veyra-situation-evaluator-") as temp:
        root = Path(temp) / "state"
        test_evidence_linked_lifecycle(root / "lifecycle")
        test_tenant_isolation_idempotence_and_bounds(root / "isolation")
        test_terminal_replay_is_monotonic(root / "terminal-replay")
        test_trace_outbox_recovers_append_failures(root / "trace-outbox")
        test_trace_outbox_backpressure_is_bounded(root / "trace-outbox-cap")
        test_atomic_resolution_is_retryable_and_idempotent(root / "resolution")
        test_shared_retention_protects_semantic_context(root / "shared-retention")
        test_legacy_write_entrypoints_use_repository(root / "repository-entrypoints")
        test_repository_noop_preserves_existing_valid_state(root / "repository-noop")
        test_repository_rejects_semantic_lifecycle_drift(root / "repository-lifecycle")
    print("situation evaluator smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
