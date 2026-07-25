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

from core.situation_evaluator import SituationAccessError, SituationEvaluator  # noqa: E402
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


def main() -> int:
    with TemporaryDirectory(prefix="veyra-situation-evaluator-") as temp:
        root = Path(temp) / "state"
        test_evidence_linked_lifecycle(root / "lifecycle")
        test_tenant_isolation_idempotence_and_bounds(root / "isolation")
    print("situation evaluator smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
