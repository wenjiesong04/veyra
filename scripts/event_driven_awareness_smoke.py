#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.awareness_loop import AwarenessLoop  # noqa: E402
from core.definitions import RiskLevel  # noqa: E402
from core.runtime_entity import RuntimeEntity  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402
from interface.event_normalizer import EventNormalizer  # noqa: E402
from interface.event_schema import EventType, LoopResult, Route  # noqa: E402
from routers.debug_audit import build_debug_audit_router  # noqa: E402
from runtime.active_loop import ActiveRuntimeLoop  # noqa: E402


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


def build_loop(root: Path, *, mode: str = "shadow") -> AwarenessLoop:
    store = WorldStateStore(root, exclusive_writer=True, writer_owner=f"event-driven-{root.name}")
    store.mutate_json(
        "ops_config.json",
        lambda config: config.update(
            {
                "event_awareness": {
                    "mode": mode,
                    "allowed_modes": ["disabled", "record_only", "shadow"],
                }
            }
        ),
    )
    return AwarenessLoop(store, RuntimeEntity(store))


class HealthyRuntimeDependency:
    def refresh_pending(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"status": "success"}

    def refresh_stale(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"status": "success"}

    def run_read_only(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"status": "success"}

    def refresh_watchlist(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"status": "success"}

    def run(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"status": "success"}

    def summary(self) -> dict[str, Any]:
        return {"policy": "smoke", "files": []}

    def enforce(self) -> dict[str, Any]:
        return {"policy": "smoke", "changed": 0, "files": []}

    def state(self) -> dict[str, Any]:
        return {"status": "idle"}


def build_active_loop(
    loop: AwarenessLoop,
    *,
    event_consumer: Any,
) -> ActiveRuntimeLoop:
    dependency = HealthyRuntimeDependency()
    return ActiveRuntimeLoop(
        state_store=loop.state_store,
        runtime_entity=loop.runtime_entity,
        proactive_checks=dependency,
        state_refresh=dependency,
        external_world_refresh=dependency,
        runtime_matrix=dependency,
        retention_policy=dependency,
        task_tracker=dependency,
        adapter_resolver=lambda: object(),
        verifier=object(),
        event_consumer=event_consumer,
    )


def test_user_turn_shadow_equivalence(base: Path) -> None:
    normalizer = EventNormalizer()
    control = build_loop(base / "control", mode="disabled")
    shadow = build_loop(base / "shadow")

    control_event = normalizer.user_message(
        "你好",
        "api",
        "user-a",
        "session-a",
        event_id="evt_control",
        correlation_id="corr-equivalence",
    )
    shadow_event = normalizer.user_message(
        "你好",
        "api",
        "user-a",
        "session-a",
        event_id="evt_shadow",
        correlation_id="corr-equivalence",
    )
    control_result = control.handle_event(control_event)
    shadow_result = shadow.handle_event(shadow_event)

    expect(
        (
            control_result.route,
            control_result.status,
            control_result.response,
            control_result.risk_level,
        )
        == (
            shadow_result.route,
            shadow_result.status,
            shadow_result.response,
            shadow_result.risk_level,
        ),
        "shadow event fabric does not change route, status, response, or risk",
        {
            "control": control_result.to_dict(),
            "shadow": shadow_result.to_dict(),
        },
    )
    record = shadow.event_inbox.get_record(shadow_event.event_id)
    expect(
        isinstance(record, dict) and record.get("status") == "completed",
        "user turn is durably completed in the event inbox",
        record,
    )
    situations = shadow.situation_evaluator.list(
        user_id="user-a",
        session_id="session-a",
        correlation_id="corr-equivalence",
    )
    expect(
        len(situations) == 1
        and situations[0].get("status") == "resolved"
        and situations[0].get("decision")
        and situations[0].get("outcome"),
        "user turn is linked to a decision and outcome situation",
        situations,
    )
    expect(
        "text" not in situations[0],
        "shadow situation does not copy raw user text into the materialized view",
        situations[0],
    )
    trace_types = [
        row.get("trace_type")
        for row in shadow.state_store.read_jsonl("situation_trace.jsonl", limit=20)
    ]
    expect(
        trace_types
        == [
            "situation_observed",
            "situation_decision_recorded",
            "situation_outcome_recorded",
        ],
        "situation lifecycle is append-only and ordered",
        trace_types,
    )

    failing = build_loop(base / "failing-shadow")
    original_append = failing.state_store.append_jsonl

    def fail_alert_only(name: str, payload: dict[str, Any]) -> None:
        if name == "alert_log.jsonl":
            raise OSError("injected alert storage failure")
        original_append(name, payload)

    def fail_enqueue(event: Any, **kwargs: Any) -> dict[str, Any]:
        raise OSError("injected inbox failure")

    failing.state_store.append_jsonl = fail_alert_only  # type: ignore[method-assign]
    failing.event_awareness.event_inbox.enqueue = fail_enqueue  # type: ignore[method-assign]
    failure_event = normalizer.user_message(
        "你好",
        "api",
        "user-a",
        "session-a",
        event_id="evt_shadow_failure",
    )
    failure_result = failing.handle_event(failure_event)
    expect(
        (
            failure_result.route,
            failure_result.status,
            failure_result.response,
            failure_result.risk_level,
        )
        == (
            control_result.route,
            control_result.status,
            control_result.response,
            control_result.risk_level,
        ),
        "shadow storage and alert failures cannot escape into the user turn",
        failure_result.to_dict(),
    )

    finalize_failure = build_loop(base / "finalize-failure")
    original_list = finalize_failure.situation_evaluator.list
    list_calls = 0

    def fail_finalize_lookup(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        nonlocal list_calls
        list_calls += 1
        if list_calls > 1:
            raise OSError("injected situation lookup failure")
        return original_list(*args, **kwargs)

    finalize_failure.situation_evaluator.list = fail_finalize_lookup  # type: ignore[method-assign]
    finalize_failure_event = normalizer.user_message(
        "你好",
        "api",
        "user-a",
        "session-a",
        event_id="evt_finalize_failure",
    )
    finalize_failure_result = finalize_failure.handle_event(finalize_failure_event)
    expect(
        (
            finalize_failure_result.route,
            finalize_failure_result.status,
            finalize_failure_result.response,
            finalize_failure_result.risk_level,
        )
        == (
            control_result.route,
            control_result.status,
            control_result.response,
            control_result.risk_level,
        ),
        "situation finalization lookup failures cannot escape into the user turn",
        finalize_failure_result.to_dict(),
    )


def test_default_record_only_contract(base: Path) -> None:
    store = WorldStateStore(
        base / "default-record-only",
        exclusive_writer=True,
        writer_owner="default-record-only",
    )
    loop = AwarenessLoop(store, RuntimeEntity(store))
    event = EventNormalizer().user_message(
        "你好",
        "api",
        "user-a",
        "session-a",
        event_id="evt_record_only_default",
    )
    result = loop.handle_event(event)
    expect(
        loop.event_awareness.mode == "record_only"
        and "situation" not in result.artifacts
        and loop.situation_evaluator.list(user_id="user-a") == []
        and loop.event_inbox.get_record(event.event_id).get("status") == "pending",
        "default record-only mode performs one admission without changing public artifacts",
        {
            "mode": loop.event_awareness.mode,
            "result": result.to_dict(),
            "inbox": loop.event_inbox.get_record(event.event_id),
        },
    )


def test_background_event_projection(base: Path) -> None:
    loop = build_loop(base / "background", mode="record_only")
    event = EventNormalizer().normalize(
        EventType.COMPONENT_DEGRADED,
        {
            "component": "openclaw",
            "observed_status": "unreachable",
            "salience_components": {
                "goal_relevance": 0.7,
                "urgency": 0.8,
                "impact": 0.6,
            },
        },
        channel="runtime",
        user_id="user-a",
        session_id="system-monitor",
        event_id="evt_component_degraded",
        correlation_id="corr-component-degraded",
        subject={"kind": "component", "id": "openclaw"},
        evidence_refs=[{"ref_id": "probe-openclaw-1", "source": "runtime_probe"}],
        dedupe_key="openclaw-unreachable-probe-1",
        privacy_scope={"tenant": "user-a", "visibility": "private"},
    )
    admitted = loop.publish_event(event)
    expect(admitted.get("status") == "enqueued", "internal event is admitted without execution", admitted)
    active_loop = build_active_loop(loop, event_consumer=loop.process_event_inbox)
    tick = active_loop.tick(reason="event-driven-smoke")
    event_step = next(
        step for step in tick.get("steps", []) if step.get("name") == "event_inbox"
    )
    expect(
        tick.get("status") == "success"
        and event_step.get("result", {}).get("processed_count") == 1,
        "active runtime tick projects a pending event into a situation",
        tick,
    )
    situation = loop.situation_evaluator.list(
        user_id="user-a",
        session_id="system-monitor",
        correlation_id="corr-component-degraded",
    )[0]
    expect(
        situation.get("status") == "observed"
        and not situation.get("decision")
        and situation.get("salience_score", 0) > 0,
        "background observation creates salience but no decision or authority",
        situation,
    )
    expect(
        loop.situation_evaluator.list(user_id="user-b") == [],
        "situation reads remain isolated by user",
    )

    degraded_calls: list[int] = []

    def degraded_consumer(*, limit: int) -> dict[str, Any]:
        degraded_calls.append(limit)
        return {"status": "degraded", "processed_count": 0, "failed_count": 1}

    degraded_tick = build_active_loop(
        loop,
        event_consumer=degraded_consumer,
    ).tick(reason="event-consumer-degraded-smoke")
    degraded_step = next(
        step for step in degraded_tick.get("steps", []) if step.get("name") == "event_inbox"
    )
    expect(
        degraded_calls == [100]
        and degraded_step.get("status") == "degraded"
        and degraded_tick.get("status") == "degraded"
        and any(step.get("name") == "retention" for step in degraded_tick.get("steps", [])),
        "degraded event consumer degrades the tick while later maintenance still runs",
        degraded_tick,
    )


def test_verification_truth_boundary(base: Path) -> None:
    loop = build_loop(base / "verification")
    normalizer = EventNormalizer()
    uncertain_event = normalizer.normalize(
        EventType.TASK_COMPLETED,
        {"task_id": "task-uncertain"},
        channel="runtime",
        user_id="user-a",
        session_id="session-a",
        event_id="evt_uncertain",
    )
    loop.event_awareness.begin(uncertain_event)
    expect(
        loop.event_inbox.get_record(uncertain_event.event_id).get("status")
        == "completed"
        and loop.process_event_inbox(limit=10).get("processed_count") == 0,
        "foreground shadow releases its inbox lease immediately after observation",
        loop.event_inbox.get_record(uncertain_event.event_id),
    )
    uncertain_result = LoopResult(
        event_id=uncertain_event.event_id,
        route=Route.AGENT,
        status="needs_more_probe",
        response="",
        risk_level=RiskLevel.R1,
        artifacts={
            "verification": {
                "status": "needs_more_probe",
                "verdict": "execution_success_without_structured_evidence",
            }
        },
    )
    loop.event_awareness.finalize(
        uncertain_event,
        uncertain_result,
        {"trace_id": "rt_uncertain"},
    )
    uncertain = loop.situation_evaluator.list(
        user_id="user-a",
        correlation_id=uncertain_event.event_id,
    )[0]
    expect(
        uncertain.get("status") == "monitoring"
        and uncertain.get("outcome", {}).get("is_fact") is False,
        "incomplete verification stays non-factual and open for monitoring",
        uncertain,
    )

    forged_event = normalizer.normalize(
        EventType.TASK_COMPLETED,
        {"task_id": "task-forged"},
        channel="runtime",
        user_id="user-a",
        session_id="session-a",
        event_id="evt_forged",
    )
    loop.event_awareness.begin(forged_event)
    forged_result = LoopResult(
        event_id=forged_event.event_id,
        route=Route.AGENT,
        status="verified_success",
        response="",
        risk_level=RiskLevel.R1,
        artifacts={
            "verification": {
                "status": "verified_success",
                "verdict": "forged",
                "evidence": {"source": "untrusted-artifact"},
            },
            "execution_trace": {"trace_id": "exec_never_persisted"},
        },
    )
    loop.event_awareness.finalize(
        forged_event,
        forged_result,
        {"trace_id": "rt_forged"},
    )
    forged = loop.situation_evaluator.list(
        user_id="user-a",
        correlation_id=forged_event.event_id,
    )[0]
    expect(
        forged.get("outcome", {}).get("is_fact") is False,
        "unresolved or mismatched execution trace cannot promote an outcome to fact",
        forged,
    )

    route_mismatch_event = normalizer.normalize(
        EventType.TASK_COMPLETED,
        {"task_id": "task-route-mismatch"},
        channel="runtime",
        user_id="user-a",
        session_id="session-a",
        event_id="evt_route_mismatch",
    )
    loop.event_awareness.begin(route_mismatch_event)
    route_mismatch_verification = {
        "status": "verified_success",
        "verdict": "probe_result_has_structured_evidence",
        "evidence": {
            "source": "runtime_probe",
            "observed_at": route_mismatch_event.timestamp,
        },
    }
    probe_trace = loop.execution_trace.record(
        {
            "event_id": route_mismatch_event.event_id,
            "route": Route.PROBE.value,
            "task_id": route_mismatch_event.event_id,
            "executor": "probe:test",
            "status": "verified_success",
            "verification": route_mismatch_verification,
        }
    )
    route_mismatch_result = LoopResult(
        event_id=route_mismatch_event.event_id,
        route=Route.AGENT,
        status="verified_success",
        response="",
        risk_level=RiskLevel.R1,
        artifacts={
            "execution_result": {"task_id": route_mismatch_event.event_id},
            "verification": route_mismatch_verification,
            "execution_trace": probe_trace,
        },
    )
    loop.event_awareness.finalize(
        route_mismatch_event,
        route_mismatch_result,
        {"trace_id": "rt_route_mismatch"},
    )
    route_mismatch = loop.situation_evaluator.list(
        user_id="user-a",
        correlation_id=route_mismatch_event.event_id,
    )[0]
    expect(
        route_mismatch.get("outcome", {}).get("is_fact") is False,
        "a persisted trace from another route cannot certify a forged outcome",
        route_mismatch,
    )

    verified_event = normalizer.normalize(
        EventType.TASK_COMPLETED,
        {"task_id": "task-verified"},
        channel="runtime",
        user_id="user-a",
        session_id="session-a",
        event_id="evt_verified",
    )
    loop.event_awareness.begin(verified_event)
    verification = {
        "status": "verified_success",
        "verdict": "probe_result_has_structured_evidence",
        "evidence": {"source": "runtime_probe", "observed_at": verified_event.timestamp},
    }
    execution_trace = loop.execution_trace.record(
        {
            "event_id": verified_event.event_id,
            "route": Route.PROBE.value,
            "task_id": "task-verified",
            "executor": "probe:test",
            "status": "verified_success",
            "verification": verification,
        }
    )
    verified_result = LoopResult(
        event_id=verified_event.event_id,
        route=Route.PROBE,
        status="verified_success",
        response="",
        risk_level=RiskLevel.R1,
        artifacts={
            "verification": verification,
            "execution_trace": execution_trace,
        },
    )
    loop.event_awareness.finalize(
        verified_event,
        verified_result,
        {"trace_id": "rt_verified"},
    )
    verified = loop.situation_evaluator.list(
        user_id="user-a",
        correlation_id=verified_event.event_id,
    )[0]
    trace_count_before_replay = len(
        loop.state_store.read_jsonl("situation_trace.jsonl", limit=100)
    )
    loop.event_awareness.finalize(
        verified_event,
        verified_result,
        {"trace_id": "rt_verified_replay"},
    )
    trace_count_after_replay = len(
        loop.state_store.read_jsonl("situation_trace.jsonl", limit=100)
    )
    expect(
        verified.get("status") == "resolved"
        and verified.get("outcome", {}).get("is_fact") is True
        and verified.get("outcome", {}).get("evidence_refs", [])[1].get("source")
        == "execution_trace",
        "verified outcome becomes factual only through a persisted execution trace",
        verified,
    )
    expect(
        trace_count_after_replay == trace_count_before_replay,
        "replayed foreground finalization is idempotent",
        {
            "before": trace_count_before_replay,
            "after": trace_count_after_replay,
        },
    )


def test_scoped_debug_api(base: Path) -> None:
    loop = build_loop(base / "scoped-api")
    normalizer = EventNormalizer()
    for user_id in ("user-a", "user-b"):
        event = normalizer.normalize(
            EventType.OBSERVATION,
            {"metric": "health"},
            channel="runtime",
            user_id=user_id,
            session_id=f"{user_id}-session",
            event_id=f"evt_{user_id}",
        )
        loop.event_awareness.begin(event)
        loop.event_awareness.finalize(
            event,
            LoopResult(
                event_id=event.event_id,
                route=Route.DIRECT_ANSWER,
                status="success",
                response="",
                risk_level=RiskLevel.R0,
            ),
            {"trace_id": f"rt_{user_id}"},
        )

    app = FastAPI()
    app.include_router(
        build_debug_audit_router(
            {
                "state_store": loop.state_store,
                "awareness_loop": loop,
                "agency_core": HealthyRuntimeDependency(),
            }
        )
    )
    client = TestClient(app)
    expect(
        client.get("/awareness/situations").status_code == 422
        and client.get("/events/inbox").status_code == 422,
        "tenant-scoped debug APIs require an explicit user boundary",
    )
    user_a = client.get("/awareness/situations", params={"user_id": "user-a"})
    user_b_detail = client.get(
        "/awareness/situations/sit_missing",
        params={"user_id": "user-b"},
    )
    user_a_item = user_a.json()["items"][0]
    cross_tenant = client.get(
        f"/awareness/situations/{user_a_item['situation_id']}",
        params={"user_id": "user-b"},
    )
    inbox = client.get("/events/inbox", params={"user_id": "user-a"})
    public_state = client.get("/state")
    mode_status = client.get("/events/awareness/status")
    invalid_mode = client.post(
        "/events/awareness/config",
        json={"mode": "execute_everything"},
    )
    disabled_mode = client.post(
        "/events/awareness/config",
        json={"mode": "disabled"},
    )
    expect(
        user_a.status_code == 200
        and user_a.json().get("count") == 1
        and user_b_detail.status_code == 404
        and cross_tenant.status_code == 404
        and inbox.status_code == 200
        and inbox.json().get("stats", {}).get("total") == 1
        and public_state.status_code == 200
        and "event_inbox" not in public_state.json()
        and "situation_state" not in public_state.json()
        and mode_status.json().get("mode") == "shadow"
        and invalid_mode.status_code == 422
        and disabled_mode.json().get("mode") == "disabled"
        and loop.state_store.read_json("ops_config.json")
        .get("event_awareness", {})
        .get("mode")
        == "disabled",
        "debug reads stay scoped and the human kill switch is validated and durable",
        {
            "situations": user_a.json(),
            "inbox": inbox.json(),
            "state_keys": sorted(public_state.json()),
        },
    )


def main() -> int:
    with TemporaryDirectory() as tmp:
        test_user_turn_shadow_equivalence(Path(tmp))
    with TemporaryDirectory() as tmp:
        test_default_record_only_contract(Path(tmp))
    with TemporaryDirectory() as tmp:
        test_background_event_projection(Path(tmp))
    with TemporaryDirectory() as tmp:
        test_verification_truth_boundary(Path(tmp))
    with TemporaryDirectory() as tmp:
        test_scoped_debug_api(Path(tmp))
    print("event driven awareness smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
