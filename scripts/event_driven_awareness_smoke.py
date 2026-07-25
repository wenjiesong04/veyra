#!/usr/bin/env python3
from __future__ import annotations

import gzip
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.awareness_loop import AwarenessLoop  # noqa: E402
from core.definitions import RiskLevel  # noqa: E402
from core.runtime_entity import RuntimeEntity  # noqa: E402
from core.situation_evaluator import SituationEvaluator  # noqa: E402
from core.understanding_core import TurnUnderstanding  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402
from interface.agent_adapter import AgentAdapter, ExecutionResult  # noqa: E402
from interface.event_normalizer import EventNormalizer  # noqa: E402
from interface.event_schema import (  # noqa: E402
    Decision,
    EventType,
    LoopResult,
    Route,
    VeyraTaskPacket,
)
from routers.debug_audit import build_debug_audit_router  # noqa: E402
from runtime.active_loop import ActiveRuntimeLoop  # noqa: E402
from runtime.retention_policy import RetentionPolicy  # noqa: E402


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


@dataclass(frozen=True, slots=True)
class OfflineRouteCase:
    case_id: str
    text: str
    route: Route
    risk_level: RiskLevel
    expected_status: str
    response: str | None
    selected_probe: str | None = None


OFFLINE_ROUTE_CASES = (
    OfflineRouteCase(
        case_id="direct",
        text="Explain the offline fixture with sufficient context.",
        route=Route.DIRECT_ANSWER,
        risk_level=RiskLevel.R0,
        expected_status="success",
        response="Offline direct answer.",
    ),
    OfflineRouteCase(
        case_id="probe",
        text="Read the offline fixture status.",
        route=Route.PROBE,
        risk_level=RiskLevel.R1,
        expected_status="verified_success",
        response="Offline probe observation.",
        selected_probe="system",
    ),
    OfflineRouteCase(
        case_id="agent",
        text="Analyze the offline fixture and return a bounded plan.",
        route=Route.AGENT,
        risk_level=RiskLevel.R1,
        expected_status="partially_success",
        response="Offline Agent returned a bounded plan.",
    ),
    OfflineRouteCase(
        case_id="ask_user",
        text="Use the referenced target.",
        route=Route.ASK_USER,
        risk_level=RiskLevel.R0,
        expected_status="needs_user_input",
        response=None,
    ),
    OfflineRouteCase(
        case_id="human_review",
        text="Apply the offline fixture change after review.",
        route=Route.HUMAN_REVIEW,
        risk_level=RiskLevel.R3,
        expected_status="needs_confirmation",
        response=None,
    ),
)


class OfflineModelClient:
    """Fail-fast model boundary for route equivalence and latency diagnostics."""

    def status(self) -> dict[str, Any]:
        return {
            "enabled": False,
            "configured": False,
            "status": "isolated_for_event_awareness_validation",
            "decision_mode": "disabled",
        }

    def complete_json(self, *, system: str, user: str, purpose: str) -> dict[str, Any]:
        del system, user
        return {
            "status": "disabled_for_event_awareness_validation",
            "purpose": purpose,
        }


class OfflineProbe:
    def run(self, text: str = "", **kwargs: Any) -> dict[str, Any]:
        del text, kwargs
        return {
            "probe": "system",
            "source": "offline_fixture",
            "target": "offline-system",
            "observed_at": "2026-01-01T00:00:00+00:00",
            "status": "ok",
            "summary": "Offline probe observation.",
            "confidence": 1.0,
            "ttl_seconds": 60,
            "details": {"fixture": True},
        }


@dataclass
class OfflineAgentAdapter(AgentAdapter):
    executor: str = "offline-agent"
    sent_packets: list[dict[str, Any]] = field(default_factory=list)

    def send_task(self, task_packet: VeyraTaskPacket) -> ExecutionResult:
        self.sent_packets.append(task_packet.to_dict())
        return ExecutionResult(
            task_id=task_packet.task_id,
            executor=self.executor,
            status="success",
            result="Offline Agent returned a bounded plan.",
            raw={
                "agent_response": {
                    "answer_or_plan": "Offline Agent returned a bounded plan.",
                    "proposed_actions": [],
                },
                "offline_fixture": True,
            },
        )

    def fetch_task_status(self, task_id: str) -> ExecutionResult:
        return ExecutionResult(
            task_id=task_id,
            executor=self.executor,
            status="success",
            result="Offline Agent returned a bounded plan.",
            raw={"offline_fixture": True},
        )

    def fetch_capabilities(self) -> dict[str, Any]:
        return {
            "runtime": self.executor,
            "status": "available",
            "connected": True,
            "offline_fixture": True,
        }

    def connection_status(self) -> dict[str, Any]:
        return self.fetch_capabilities()

    def fetch_memory_summary(self, session_id: str) -> dict[str, Any]:
        return {
            "session_id": session_id,
            "summary": "",
            "freshness": "fresh",
            "trust": "offline_fixture",
        }


def offline_result_signature(result: LoopResult) -> dict[str, str]:
    return {
        "route": result.route.value,
        "status": result.status,
        "response": result.response,
        "risk_level": result.risk_level.value,
    }


def build_offline_route_loop(
    root: Path,
    *,
    mode: str,
    case: OfflineRouteCase,
) -> AwarenessLoop:
    """Build the real turn pipeline with every external boundary replaced locally."""

    loop = build_loop(root, mode=mode)
    loop.core_reasoning.client = OfflineModelClient()  # type: ignore[assignment]
    selected_agent = loop.agent_registry.selected_name()
    loop.agent_registry._adapters[selected_agent] = OfflineAgentAdapter()
    loop.agent_adapter = loop.agent_registry.selected()
    loop.probes["system"] = OfflineProbe()

    def fixed_understanding(**kwargs: Any) -> TurnUnderstanding:
        del kwargs
        return TurnUnderstanding(
            intent="offline_validation",
            task_summary=case.text,
            user_goal=case.text,
            what_user_really_needs="validate event awareness non-interference",
            task_type="offline_validation",
            explicit_request=case.text,
            hidden_need="validate event awareness non-interference",
            suggested_mode=case.route.value,
            confidence=1.0,
            reason="scripted offline fixture",
            source="offline_fixture",
        )

    def fixed_decision(*args: Any, **kwargs: Any) -> Decision:
        del args, kwargs
        semantic_policy = {
            "preferred_route": case.route.value,
            "requires_clarification": case.route == Route.ASK_USER,
            "clarification_reason": (
                "the referenced target is unresolved"
                if case.route == Route.ASK_USER
                else ""
            ),
            "selected_probe": case.selected_probe,
            "allowed_capabilities": (
                ["system_probe"]
                if case.route == Route.PROBE
                else ["selected_agent_runtime"]
                if case.route == Route.AGENT
                else ["native_answer"]
                if case.route == Route.DIRECT_ANSWER
                else ["human_review"]
                if case.route == Route.HUMAN_REVIEW
                else ["ask_user"]
            ),
            "allowed_effects": (
                ["agent.execute"]
                if case.route == Route.AGENT
                else []
            ),
            "denied_effects": [],
        }
        return Decision(
            route=case.route,
            risk_level=case.risk_level,
            reason="scripted offline event awareness validation",
            requires_confirmation=case.route == Route.HUMAN_REVIEW,
            selected_probe=case.selected_probe,
            intent="offline_validation",
            complexity="complex" if case.route == Route.AGENT else "simple",
            capability=(
                "selected_agent_runtime"
                if case.route == Route.AGENT
                else "probe"
                if case.route == Route.PROBE
                else "human_review"
                if case.route == Route.HUMAN_REVIEW
                else "ask_user"
                if case.route == Route.ASK_USER
                else "native_answer"
            ),
            needs_probe=case.route == Route.PROBE,
            needs_agent=case.route == Route.AGENT,
            needs_user_confirmation=case.route == Route.HUMAN_REVIEW,
            memory_policy="forget",
            reasoning_mode=(
                "execution"
                if case.route in {Route.AGENT, Route.HUMAN_REVIEW}
                else "direct"
            ),
            required_capabilities=list(semantic_policy["allowed_capabilities"]),
            model_assist={
                "status": "offline_fixture",
                "draft_response": case.response,
                "semantic_policy": semantic_policy,
            },
        )

    loop.understanding_core.build = fixed_understanding  # type: ignore[method-assign]
    loop.decision_core.decide = fixed_decision  # type: ignore[method-assign]
    loop._direct_answer = (  # type: ignore[method-assign]
        lambda *args, **kwargs: str(case.response or "")
    )
    loop._low_latency_short_response = lambda *args, **kwargs: None  # type: ignore[method-assign]
    loop._conversation_followup_result = lambda *args, **kwargs: None  # type: ignore[method-assign]
    loop._early_awareness_response = lambda *args, **kwargs: {}  # type: ignore[method-assign]
    loop._process_commitment_turn = lambda *args, **kwargs: None  # type: ignore[method-assign]
    loop._sync_user_awareness_from_text = lambda *args, **kwargs: None  # type: ignore[method-assign]
    loop._persist_conversation_slots = lambda *args, **kwargs: None  # type: ignore[method-assign]
    loop._render_final_user_messages = lambda *args, **kwargs: None  # type: ignore[method-assign]
    return loop


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

    def fail_enqueue_and_claim(
        event: Any,
        consumer_id: str,
        lease_seconds: float = 60.0,
        **kwargs: Any,
    ) -> dict[str, Any]:
        del event, consumer_id, lease_seconds, kwargs
        raise OSError("injected inbox failure")

    failing.state_store.append_jsonl = fail_alert_only  # type: ignore[method-assign]
    failing.event_awareness.event_inbox.enqueue_and_claim = fail_enqueue_and_claim  # type: ignore[method-assign]
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


def test_offline_route_equivalence_matrix(base: Path) -> None:
    normalizer = EventNormalizer()
    modes = ("disabled", "record_only", "shadow")
    matrix: list[dict[str, Any]] = []
    for case in OFFLINE_ROUTE_CASES:
        signatures: dict[str, dict[str, str]] = {}
        loops: dict[str, AwarenessLoop] = {}
        for mode in modes:
            loop = build_offline_route_loop(
                base / case.case_id / mode,
                mode=mode,
                case=case,
            )
            event = normalizer.user_message(
                case.text,
                "offline-matrix",
                "matrix-user",
                f"matrix-{case.case_id}",
                event_id=f"evt_matrix_{case.case_id}_{mode}",
                correlation_id=f"corr-matrix-{case.case_id}",
            )
            result = loop.handle_event(event)
            signatures[mode] = offline_result_signature(result)
            loops[mode] = loop

        baseline = signatures["disabled"]
        expect(
            baseline["route"] == case.route.value
            and baseline["status"] == case.expected_status
            and bool(baseline["response"])
            and (case.response is None or baseline["response"] == case.response)
            and baseline["risk_level"] == case.risk_level.value,
            f"{case.case_id} offline fixture exercises its intended route",
            baseline,
        )
        expect(
            signatures["record_only"] == baseline
            and signatures["shadow"] == baseline,
            f"{case.case_id} disabled, record-only, and shadow outputs are equivalent",
            signatures,
        )
        expect(
            loops["disabled"].event_inbox.stats().get("total") == 0
            and loops["record_only"].event_inbox.stats().get("pending") == 1
            and loops["shadow"].event_inbox.stats().get("completed") == 1,
            f"{case.case_id} mode changes observation state only",
            {
                mode: loop.event_inbox.stats()
                for mode, loop in loops.items()
            },
        )
        matrix.append({"case": case.case_id, "signatures": signatures})

    expect(
        len(matrix) == len(OFFLINE_ROUTE_CASES),
        "offline equivalence matrix covers direct, probe, Agent, ask-user, and review",
        matrix,
    )


def test_situation_trace_retention(base: Path) -> None:
    store = WorldStateStore(
        base / "situation-trace-retention",
        exclusive_writer=True,
        writer_owner="situation-trace-retention",
    )
    seeded = [
        {
            "trace_type": "situation_observed",
            "schema_version": "veyra.situation_trace.v1",
            "situation_id": f"sit_retention_{index}",
            "sequence": index,
        }
        for index in range(8)
    ]
    for row in seeded:
        store.append_jsonl("situation_trace.jsonl", row)

    policy = RetentionPolicy(store, limits={"situation_trace.jsonl": 3})
    summary_row = next(
        item
        for item in policy.summary()["files"]
        if item["file"] == "situation_trace.jsonl"
    )
    before = store.path_for("situation_trace.jsonl").read_bytes()
    preview = policy.enforce(dry_run=True)
    preview_row = next(
        item
        for item in preview["files"]
        if item["file"] == "situation_trace.jsonl"
    )
    expect(
        "situation_trace.jsonl" in RetentionPolicy.DEFAULT_LIMITS
        and summary_row["target_limit"] == 3
        and summary_row["status"] == "over_limit"
        and preview_row["status"] == "would_rotate"
        and store.path_for("situation_trace.jsonl").read_bytes() == before
        and not (store.root / preview_row["archive_path"]).exists(),
        "situation trace retention preview is explicit and non-mutating",
        {"summary": summary_row, "preview": preview_row},
    )

    enforced = policy.enforce()
    enforced_row = next(
        item
        for item in enforced["files"]
        if item["file"] == "situation_trace.jsonl"
    )
    retained = store.read_jsonl("situation_trace.jsonl", limit=20)
    archive_path = store.root / enforced_row["archive_path"]
    with gzip.open(archive_path, "rt", encoding="utf-8") as handle:
        archived = [
            json.loads(line)
            for line in handle.read().splitlines()
            if line.strip()
        ]
    expect(
        enforced_row["status"] == "rotated"
        and enforced_row["archive_compression"] == "gzip"
        and enforced_row["pruned_entries"] == 5
        and [row["sequence"] for row in retained] == [5, 6, 7]
        and [row["sequence"] for row in archived] == [0, 1, 2, 3, 4]
        and [row["sequence"] for row in archived + retained] == list(range(8))
        and all(
            row["schema_version"] == "veyra.situation_trace.v1"
            for row in archived + retained
        ),
        "situation trace archives before truncation and preserves ordered lifecycle history",
        {
            "rotation": enforced_row,
            "archived": archived,
            "retained": retained,
        },
    )


def test_retention_defers_pending_trace_outbox(base: Path) -> None:
    store = WorldStateStore(
        base / "pending-outbox-retention",
        exclusive_writer=True,
        writer_owner="pending-outbox-retention",
    )
    evaluator = SituationEvaluator(store)
    event = EventNormalizer().normalize(
        EventType.OBSERVATION,
        {"component": "retention-recovery-fixture"},
        channel="runtime",
        user_id="retention-user",
        session_id="retention-session",
        event_id="evt_retention_pending_outbox",
        correlation_id="corr-retention-pending-outbox",
    )
    original_append = store.append_jsonl
    interrupted = False

    def interrupt_after_trace_append(
        name: str,
        payload: dict[str, Any],
    ) -> None:
        nonlocal interrupted
        original_append(name, payload)
        if name == "situation_trace.jsonl" and not interrupted:
            interrupted = True
            raise OSError("injected interruption before trace outbox ack")

    store.append_jsonl = interrupt_after_trace_append  # type: ignore[method-assign]
    try:
        evaluator.observe(event)
    finally:
        store.append_jsonl = original_append  # type: ignore[method-assign]
    pending_state = store.read_json("situation_state.json")
    pending_entry = pending_state.get("trace_outbox", [])[0]
    pending_transition_id = str(pending_entry.get("transition_id") or "")
    for index in range(7):
        store.append_jsonl(
            "situation_trace.jsonl",
            {
                "schema_version": "veyra.situation_trace.v1",
                "transition_id": f"sitxn_retention_fixture_{index}",
                "trace_type": "situation_observed",
                "sequence": index,
            },
        )

    policy = RetentionPolicy(store, limits={"situation_trace.jsonl": 3})
    before = store.path_for("situation_trace.jsonl").read_bytes()
    deferred = policy.enforce()
    deferred_row = next(
        item
        for item in deferred["files"]
        if item["file"] == "situation_trace.jsonl"
    )
    expect(
        interrupted
        and pending_state.get("trace_outbox_count") == 1
        and deferred_row.get("status") == "deferred_pending_outbox"
        and deferred_row.get("pending_trace_outbox") == 1
        and store.path_for("situation_trace.jsonl").read_bytes() == before,
        "retention cannot rotate a trace that still awaits outbox acknowledgement",
        {
            "state": pending_state,
            "retention": deferred_row,
        },
    )

    repair = evaluator.flush_trace_outbox()
    enforced = policy.enforce()
    enforced_row = next(
        item
        for item in enforced["files"]
        if item["file"] == "situation_trace.jsonl"
    )
    live = store.read_jsonl("situation_trace.jsonl", limit=20)
    archive_path = store.root / enforced_row["archive_path"]
    with gzip.open(archive_path, "rt", encoding="utf-8") as handle:
        archived = [
            json.loads(line)
            for line in handle.read().splitlines()
            if line.strip()
        ]
    all_rows = archived + live
    expect(
        repair.get("deduplicated") == 1
        and repair.get("pending") == 0
        and enforced_row.get("status") == "rotated"
        and sum(
            row.get("transition_id") == pending_transition_id
            for row in all_rows
        )
        == 1,
        "outbox acknowledgement precedes retention and preserves one transition",
        {
            "repair": repair,
            "retention": enforced_row,
            "transition_id": pending_transition_id,
            "rows": all_rows,
        },
    )


def test_retention_rotation_is_atomic_with_trace_delivery(base: Path) -> None:
    store = WorldStateStore(
        base / "atomic-retention",
        exclusive_writer=True,
        writer_owner="atomic-retention",
    )
    for index in range(8):
        store.append_jsonl(
            "situation_trace.jsonl",
            {
                "schema_version": "veyra.situation_trace.v1",
                "transition_id": f"sitxn_atomic_retention_seed_{index}",
                "trace_type": "situation_observed",
                "sequence": index,
            },
        )
    evaluator = SituationEvaluator(store)
    event = EventNormalizer().normalize(
        EventType.OBSERVATION,
        {"component": "atomic-retention-fixture"},
        channel="runtime",
        user_id="atomic-retention-user",
        session_id="atomic-retention-session",
        event_id="evt_atomic_retention",
        correlation_id="corr-atomic-retention",
    )
    original_append = store.append_jsonl
    original_rotate = store.rotate_jsonl
    worker_started = Event()
    worker_finished = Event()
    worker_threads: list[Thread] = []
    blocked_while_locked = False
    interrupted = False

    def interrupt_target_after_append(
        name: str,
        payload: dict[str, Any],
    ) -> None:
        nonlocal interrupted
        original_append(name, payload)
        if (
            name == "situation_trace.jsonl"
            and payload.get("source_event_id") == event.event_id
            and not interrupted
        ):
            interrupted = True
            raise OSError("injected concurrent interruption before ack")

    def observe_during_rotation() -> None:
        worker_started.set()
        evaluator.observe(event)
        worker_finished.set()

    def rotate_with_concurrent_delivery(
        name: str,
        *,
        limit: int,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        nonlocal blocked_while_locked
        if name == "situation_trace.jsonl":
            worker = Thread(
                target=observe_during_rotation,
                name="atomic-retention-observer",
                daemon=True,
            )
            worker_threads.append(worker)
            worker.start()
            worker_started.wait(timeout=2)
            blocked_while_locked = not worker_finished.wait(timeout=0.05)
        return original_rotate(name, limit=limit, dry_run=dry_run)

    store.append_jsonl = interrupt_target_after_append  # type: ignore[method-assign]
    store.rotate_jsonl = rotate_with_concurrent_delivery  # type: ignore[method-assign]
    try:
        policy = RetentionPolicy(
            store,
            limits={"situation_trace.jsonl": 3},
        )
        enforced = policy.enforce()
    finally:
        store.rotate_jsonl = original_rotate  # type: ignore[method-assign]
        for worker in worker_threads:
            worker.join(timeout=5)
        store.append_jsonl = original_append  # type: ignore[method-assign]

    enforced_row = next(
        item
        for item in enforced["files"]
        if item["file"] == "situation_trace.jsonl"
    )
    pending_state = store.read_json("situation_state.json")
    pending_entry = pending_state.get("trace_outbox", [])[0]
    transition_id = str(pending_entry.get("transition_id") or "")
    repair = evaluator.flush_trace_outbox()
    live = store.read_jsonl("situation_trace.jsonl", limit=20)
    archive_path = store.root / enforced_row["archive_path"]
    with gzip.open(archive_path, "rt", encoding="utf-8") as handle:
        archived = [
            json.loads(line)
            for line in handle.read().splitlines()
            if line.strip()
        ]
    expect(
        blocked_while_locked
        and worker_finished.is_set()
        and interrupted
        and enforced_row.get("status") == "rotated"
        and pending_state.get("trace_outbox_count") == 1
        and repair.get("deduplicated") == 1
        and sum(
            row.get("transition_id") == transition_id
            for row in archived + live
        )
        == 1,
        "retention check and rotation serialize against concurrent trace delivery",
        {
            "retention": enforced_row,
            "pending_state": pending_state,
            "repair": repair,
            "transition_id": transition_id,
        },
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


def test_shadow_foreground_atomic_claim(base: Path) -> None:
    loop = build_loop(base / "atomic-foreground", mode="shadow")
    event = EventNormalizer().user_message(
        "foreground atomic claim fixture",
        "offline-race",
        "atomic-user",
        "atomic-session",
        event_id="evt_atomic_foreground",
    )
    original_atomic = loop.event_inbox.enqueue_and_claim
    atomic_calls: list[dict[str, Any]] = []
    background_claims: list[dict[str, Any] | None] = []

    def atomic_with_background_probe(
        incoming: Any,
        consumer_id: str,
        lease_seconds: float = 60.0,
        **kwargs: Any,
    ) -> dict[str, Any]:
        admission = original_atomic(
            incoming,
            consumer_id,
            lease_seconds,
            **kwargs,
        )
        atomic_calls.append(admission)
        # The atomic mutation has returned, but foreground still has not
        # observed or completed the event. A background consumer must not be
        # able to acquire the already-leased record in this window.
        background_claims.append(
            loop.event_inbox.claim(
                "awareness-shadow-background-race",
                lease_seconds=60,
            )
        )
        return admission

    def legacy_split_call_forbidden(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise AssertionError("shadow foreground used legacy split enqueue/claim")

    loop.event_inbox.enqueue_and_claim = atomic_with_background_probe  # type: ignore[method-assign]
    loop.event_inbox.enqueue = legacy_split_call_forbidden  # type: ignore[method-assign]
    loop.event_inbox.claim_by_id = legacy_split_call_forbidden  # type: ignore[method-assign]
    observed = loop.event_awareness.begin(event)
    record = loop.event_inbox.get_record(event.event_id)
    expect(
        len(atomic_calls) == 1
        and atomic_calls[0].get("claimed") is True
        and background_claims == [None]
        and observed.get("status") == "observed"
        and isinstance(record, dict)
        and record.get("status") == "completed"
        and record.get("attempts") == 1,
        "shadow foreground atomically admits and claims before background can race",
        {
            "admission": atomic_calls,
            "background_claims": background_claims,
            "observed": observed,
            "record": record,
        },
    )


def test_duplicate_foreground_skips_finalize(base: Path) -> None:
    loop = build_loop(base / "duplicate-foreground", mode="shadow")
    event = EventNormalizer().user_message(
        "你好",
        "api",
        "duplicate-user",
        "duplicate-session",
        event_id="evt_duplicate_foreground",
        correlation_id="corr-duplicate-foreground",
    )
    first = loop.handle_event(event)
    first_situation = loop.situation_evaluator.list(
        user_id="duplicate-user",
        session_id="duplicate-session",
        correlation_id="corr-duplicate-foreground",
    )[0]
    second = loop.handle_event(event)
    second_situation = loop.situation_evaluator.list(
        user_id="duplicate-user",
        session_id="duplicate-session",
        correlation_id="corr-duplicate-foreground",
    )[0]
    record = loop.event_inbox.get_record(event.event_id)
    expect(
        offline_result_signature(first) == offline_result_signature(second)
        and isinstance(record, dict)
        and record.get("status") == "completed"
        and record.get("delivery_count") == 2
        and len(first_situation.get("decision_history") or []) == 1
        and len(first_situation.get("outcome_history") or []) == 1
        and len(second_situation.get("decision_history") or []) == 1
        and len(second_situation.get("outcome_history") or []) == 1,
        "duplicate foreground delivery does not finalize the same outcome twice",
        {
            "first": first.to_dict(),
            "second": second.to_dict(),
            "record": record,
            "situation": second_situation,
        },
    )


def test_duplicate_foreground_repairs_failed_resolution(base: Path) -> None:
    loop = build_loop(base / "duplicate-resolution-repair", mode="shadow")
    event = EventNormalizer().user_message(
        "你好",
        "api",
        "resolution-repair-user",
        "resolution-repair-session",
        event_id="evt_resolution_repair",
        correlation_id="corr-resolution-repair",
    )
    original_resolution = loop.situation_evaluator.record_resolution
    attempts = 0

    def fail_first_finalize_attempts(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise OSError("injected resolution persistence failure")
        return original_resolution(*args, **kwargs)

    loop.situation_evaluator.record_resolution = fail_first_finalize_attempts  # type: ignore[method-assign]
    first = loop.handle_event(event)
    incomplete = loop.situation_evaluator.list(
        user_id="resolution-repair-user",
        session_id="resolution-repair-session",
        correlation_id="corr-resolution-repair",
    )[0]
    second = loop.handle_event(event)
    repaired = loop.situation_evaluator.list(
        user_id="resolution-repair-user",
        session_id="resolution-repair-session",
        correlation_id="corr-resolution-repair",
    )[0]
    expect(
        offline_result_signature(first) == offline_result_signature(second)
        and attempts == 3
        and len(incomplete.get("decision_history") or []) == 0
        and len(incomplete.get("outcome_history") or []) == 0
        and len(repaired.get("decision_history") or []) == 1
        and len(repaired.get("outcome_history") or []) == 1,
        "duplicate foreground delivery repairs an atomic resolution that previously failed",
        {
            "attempts": attempts,
            "incomplete": incomplete,
            "repaired": repaired,
        },
    )


def test_background_trace_failure_recovery(base: Path) -> None:
    loop = build_loop(base / "trace-recovery", mode="record_only")
    event = EventNormalizer().normalize(
        EventType.COMPONENT_DEGRADED,
        {
            "component": "offline-trace-fixture",
            "observed_status": "degraded",
            "salience_components": {"impact": 0.7},
        },
        channel="runtime",
        user_id="trace-recovery-user",
        session_id="trace-recovery-session",
        event_id="evt_trace_recovery",
        correlation_id="corr-trace-recovery",
    )
    admitted = loop.publish_event(event)
    original_append = loop.state_store.append_jsonl
    injected_failures = 0

    def fail_first_situation_trace(name: str, payload: dict[str, Any]) -> None:
        nonlocal injected_failures
        if name == "situation_trace.jsonl" and injected_failures == 0:
            injected_failures += 1
            raise OSError("injected transient situation trace append failure")
        original_append(name, payload)

    loop.state_store.append_jsonl = fail_first_situation_trace  # type: ignore[method-assign]
    try:
        first = loop.process_event_inbox(limit=1)
    finally:
        loop.state_store.append_jsonl = original_append  # type: ignore[method-assign]
    first_record = loop.event_inbox.get_record(event.event_id)
    pending_state = loop.state_store.read_json("situation_state.json")
    pending_trace = loop.state_store.read_jsonl("situation_trace.jsonl", limit=20)
    expect(
        admitted.get("status") == "enqueued"
        and injected_failures == 1
        and first.get("processed_count") == 1
        and first.get("failed_count") == 0
        and isinstance(first_record, dict)
        and first_record.get("status") == "completed"
        and pending_state.get("trace_outbox_count") == 1
        and pending_trace == [],
        "trace append failure completes durable state while retaining a trace outbox",
        {
            "first": first,
            "record": first_record,
            "state": pending_state,
            "trace": pending_trace,
        },
    )

    # A later background tick performs a strict outbox flush before looking for
    # queue work. Recovery therefore does not depend on illegally retrying an
    # already-completed event.
    second = loop.process_event_inbox(limit=1)
    completed_record = loop.event_inbox.get_record(event.event_id)
    recovered_state = loop.state_store.read_json("situation_state.json")
    recovered_trace = loop.state_store.read_jsonl("situation_trace.jsonl", limit=20)
    situations = loop.situation_evaluator.list(
        user_id=event.source.user_id,
        session_id=event.source.session_id,
        correlation_id=event.correlation_id,
    )
    expect(
        second.get("processed_count") == 0
        and second.get("failed_count") == 0
        and isinstance(completed_record, dict)
        and completed_record.get("status") == "completed"
        and recovered_state.get("trace_outbox_count") == 0
        and len(recovered_trace) == 1
        and recovered_trace[0].get("source_event_id") == event.event_id
        and recovered_trace[0].get("trace_type") == "situation_observed"
        and len(situations) == 1,
        "the next background tick repairs trace delivery for the completed event",
        {
            "second": second,
            "record": completed_record,
            "state": recovered_state,
            "trace": recovered_trace,
            "situations": situations,
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
        test_offline_route_equivalence_matrix(Path(tmp))
    with TemporaryDirectory() as tmp:
        test_situation_trace_retention(Path(tmp))
    with TemporaryDirectory() as tmp:
        test_retention_defers_pending_trace_outbox(Path(tmp))
    with TemporaryDirectory() as tmp:
        test_retention_rotation_is_atomic_with_trace_delivery(Path(tmp))
    with TemporaryDirectory() as tmp:
        test_default_record_only_contract(Path(tmp))
    with TemporaryDirectory() as tmp:
        test_shadow_foreground_atomic_claim(Path(tmp))
    with TemporaryDirectory() as tmp:
        test_duplicate_foreground_skips_finalize(Path(tmp))
    with TemporaryDirectory() as tmp:
        test_duplicate_foreground_repairs_failed_resolution(Path(tmp))
    with TemporaryDirectory() as tmp:
        test_background_trace_failure_recovery(Path(tmp))
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
