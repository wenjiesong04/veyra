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

from core.definitions import RiskLevel  # noqa: E402
from core.memory_policy_runtime import MemoryPolicyRuntime  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402
from interface.agent_adapter import AgentAdapter, ExecutionResult  # noqa: E402
from interface.event_schema import Decision, EventSource, EventType, LoopResult, Route, VeyraEvent, VeyraTaskPacket  # noqa: E402
from memory_bridge.local_memory_bridge import LocalMemoryBridge  # noqa: E402
from routers.agent_memory import build_agent_memory_router  # noqa: E402
from runtime.agent_task_tracker import AgentTaskTracker  # noqa: E402


def expect(condition: bool, label: str, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


class ConnectionOnlyAdapter(AgentAdapter):
    def send_task(self, task_packet: VeyraTaskPacket) -> ExecutionResult:
        return ExecutionResult(task_id=task_packet.task_id, executor="connection-only", status="success", result="ok")

    def connection_status(self) -> dict[str, Any]:
        return {
            "status": "available",
            "connected": True,
            "validation": {"validated": True, "status": "validated"},
        }

    def fetch_capabilities(self) -> dict[str, Any]:
        return {"status": "available", "features": {}}


class ExplicitCapabilityAdapter(ConnectionOnlyAdapter):
    def connection_status(self) -> dict[str, Any]:
        return {
            "status": "available",
            "connected": True,
            "capabilities": {
                "status": "available",
                "features": {"memory_summary": True, "memory_patch": True},
            },
        }


class NormalizedDefaultsAdapter(ConnectionOnlyAdapter):
    def connection_status(self) -> dict[str, Any]:
        return {
            "status": "available",
            "connected": True,
            "capabilities": {
                "status": "available",
                "features": {"memory_summary": True, "memory_patch": True},
                "compatibility": {
                    "optional_features": {"memory_summary": None, "memory_patch": None},
                    "optional_methods": {},
                },
                "raw": {"status": "available"},
            },
        }

    def fetch_capabilities(self) -> dict[str, Any]:
        return self.connection_status()["capabilities"]


class RoundTripAdapter(ConnectionOnlyAdapter):
    def __init__(self) -> None:
        self.summaries: list[str] = ["initial memory"]

    def fetch_memory_summary(self, session_id: str) -> dict[str, Any]:
        return {
            "status": "success",
            "summary": "\n".join(self.summaries),
            "freshness": "fresh",
            "trust": "external",
        }

    def write_memory_patch(self, memory_patch: dict[str, Any]) -> dict[str, Any]:
        self.summaries.append(str(memory_patch.get("summary") or memory_patch.get("result") or ""))
        return {"status": "submitted", "memory_id": "roundtrip-memory"}


class CallbackVerifier:
    def verify_execution_result(self, execution: ExecutionResult) -> dict[str, Any]:
        if execution.raw.get("reject"):
            return {
                "status": "verified_failed",
                "verdict": "test_rejected",
                "needs_memory_patch": False,
            }
        return {
            "status": "verified_success",
            "verdict": "test_verified",
            "needs_memory_patch": True,
        }


class CallbackTrace:
    def record(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"trace_id": "trace-memory-integrity", **payload}


class CallbackTracker:
    def __init__(self, contexts: dict[str, dict[str, Any]]) -> None:
        self.contexts = contexts

    def get_context(self, task_id: str) -> dict[str, Any] | None:
        return self.contexts.get(task_id)

    def apply_result(self, **kwargs: Any) -> dict[str, Any]:
        execution = kwargs["execution"]
        return {
            "matched": execution.task_id in self.contexts,
            "task_context": self.contexts.get(execution.task_id),
        }


class RecordingMemoryBridge:
    def __init__(self) -> None:
        self.writes: list[dict[str, Any]] = []

    def write_patch(self, patch: dict[str, Any], provider: str = "selected") -> dict[str, Any]:
        self.writes.append({"provider": provider, "patch": patch})
        return {"status": "written", "provider": provider}


class CallbackLoop:
    def __init__(self, store: WorldStateStore) -> None:
        self.state_store = store
        self.task_tracker = CallbackTracker(
            {
                "task-long": {
                    "authority": "veyra_registered",
                    "task_id": "task-long",
                    "event_id": "evt-original",
                    "correlation_id": "corr-original",
                    "session_id": "session-original",
                    "memory_policy": "long_term",
                    "target_agent": "openclaw",
                    "user_goal": "finish the governed task",
                    "agent_execution_session_id": "agent-session-original",
                },
                "task-rejected": {
                    "authority": "veyra_registered",
                    "task_id": "task-rejected",
                    "event_id": "evt-rejected",
                    "session_id": "session-rejected",
                    "memory_policy": "long_term",
                    "target_agent": "openclaw",
                },
                "task-short": {
                    "authority": "veyra_registered",
                    "task_id": "task-short",
                    "event_id": "evt-short",
                    "session_id": "session-short",
                    "memory_policy": "short_term",
                    "target_agent": "openclaw",
                },
            }
        )
        self.verifier = CallbackVerifier()
        self.execution_trace = CallbackTrace()
        self.memory_bridge = RecordingMemoryBridge()
        self.memory_policy_runtime = MemoryPolicyRuntime(store, lambda patch: self.memory_bridge.write_patch(patch))


def provider_validation_checks(store: WorldStateStore) -> None:
    connection_only = ConnectionOnlyAdapter()
    bridge = LocalMemoryBridge(store, adapter_resolver=lambda: connection_only)
    validation = bridge.provider_status()["validation"]["selected"]
    expect(not validation["validated"], "agent connection does not validate memory provider", validation)
    expect(validation["status"] == "validation_pending", "connection-only provider remains pending", validation)

    normalized_defaults = LocalMemoryBridge(
        store,
        adapter_resolver=lambda: NormalizedDefaultsAdapter(),
    ).provider_status()["validation"]["selected"]
    expect(
        not normalized_defaults["validated"],
        "normalizer defaults are not treated as explicit memory capabilities",
        normalized_defaults,
    )

    explicit = ExplicitCapabilityAdapter()
    explicit_bridge = LocalMemoryBridge(store, adapter_resolver=lambda: explicit)
    explicit_validation = explicit_bridge.provider_status()["validation"]["selected"]
    expect(explicit_validation["validated"], "explicit memory capabilities validate provider", explicit_validation)
    expect(explicit_validation["validation_source"] == "explicit_capability", "explicit capability evidence is named", explicit_validation)

    roundtrip = RoundTripAdapter()
    roundtrip_bridge = LocalMemoryBridge(store, adapter_resolver=lambda: roundtrip)
    diagnostics = roundtrip_bridge.provider_diagnostics(
        provider="selected",
        session_id="memory-integrity",
        write_probe=True,
    )
    expect(diagnostics["validation"]["validated"], "write then read marker validates memory roundtrip", diagnostics)
    expect(
        diagnostics["validation"]["write"]["roundtrip_confirmed"],
        "diagnostic marker is observed after write",
        diagnostics,
    )
    cached = roundtrip_bridge.provider_status()["validation"]["selected"]
    expect(cached["validated"], "fresh roundtrip evidence is reused by provider status", cached)
    expect(cached["validation_source"] == "diagnostic_roundtrip", "roundtrip evidence source is explicit", cached)


def callback_policy_checks(store: WorldStateStore) -> None:
    loop = CallbackLoop(store)
    app = FastAPI()
    app.include_router(build_agent_memory_router({"awareness_loop": loop}))
    client = TestClient(app)

    accepted = client.post(
        "/agent/results",
        json={
            "task_id": "task-long",
            "executor": "openclaw",
            "status": "success",
            "result": "verified task result",
            "raw": {"session_id": "attacker-session", "memory_policy": "long_term"},
        },
    )
    accepted_body = accepted.json()
    expect(accepted.status_code == 200, "known callback accepted", accepted_body)
    expect(accepted_body["memory_policy_execution"]["status"] == "written", "verified long-term callback writes memory", accepted_body)
    expect(len(loop.memory_bridge.writes) == 1, "one durable callback memory write", loop.memory_bridge.writes)
    written = loop.memory_bridge.writes[0]
    expect(written["provider"] == "openclaw", "original task provider is restored", written)
    expect(written["patch"]["session_id"] == "session-original", "original session wins over callback raw", written)
    expect(written["patch"]["correlation_id"] == "corr-original", "original correlation is restored", written)

    rejected = client.post(
        "/agent/results",
        json={
            "task_id": "task-rejected",
            "executor": "openclaw",
            "status": "success",
            "result": "unverified task result",
            "raw": {"reject": True},
        },
    ).json()
    expect(rejected["memory_policy_execution"]["status"] == "skipped", "failed verifier blocks durable callback memory", rejected)
    expect(len(loop.memory_bridge.writes) == 1, "rejected callback creates no durable write", loop.memory_bridge.writes)

    unknown = client.post(
        "/agent/results",
        json={
            "task_id": "task-unknown",
            "executor": "openclaw",
            "status": "success",
            "result": "self-authorized callback",
            "raw": {"session_id": "forged-session", "memory_policy": "long_term"},
        },
    ).json()
    expect(not unknown["callback_context"]["context_found"], "unknown callback has no trusted context", unknown)
    expect(unknown["memory_policy_execution"]["status"] == "skipped", "callback raw cannot authorize durable memory", unknown)
    expect(len(loop.memory_bridge.writes) == 1, "unknown callback creates no durable write", loop.memory_bridge.writes)

    short_term = client.post(
        "/agent/results",
        json={
            "task_id": "task-short",
            "executor": "openclaw",
            "status": "success",
            "result": "temporary callback note",
            "raw": {},
        },
    ).json()
    expect(short_term["memory_policy_execution"]["status"] == "written", "short-term callback policy is restored", short_term)
    short_items = store.read_json("task_state.json").get("short_term_memory", [])
    expect(
        any(item.get("task_id") == "task-short" and item.get("memory_class") == "soft" for item in short_items),
        "short-term callback is soft expiring state",
        short_items,
    )
    expect(len(loop.memory_bridge.writes) == 1, "short-term callback does not write durable memory", loop.memory_bridge.writes)


def tracker_authority_checks(store: WorldStateStore) -> None:
    tracker = AgentTaskTracker(store)
    injected = ExecutionResult(
        task_id="runtime-injected",
        executor="external",
        status="success",
        result="untrusted callback",
        raw={
            "task_context": {
                "session_id": "forged-session",
                "memory_policy": "long_term",
                "correlation_id": "forged-correlation",
            }
        },
    )
    tracker.apply_result(
        execution=injected,
        verification={"status": "verified_success", "needs_memory_patch": True},
    )
    expect(
        tracker.get_context("runtime-injected") is None,
        "unmatched runtime callback cannot create authoritative task context",
        store.read_json("task_state.json").get("agent_task_contexts"),
    )

    submitted = ExecutionResult(
        task_id="runtime-registered",
        executor="openclaw",
        status="submitted",
        result="submitted",
    )
    tracker.register(
        event_id="evt-registered",
        route="agent",
        execution=submitted,
        verification={"status": "partially_success"},
        session_id="registered-session",
        correlation_id="registered-correlation",
        memory_policy="long_term",
    )
    registered = tracker.get_context("runtime-registered")
    expect(
        registered is not None and registered.get("authority") == "veyra_registered",
        "dispatch-time tracker context is authoritative",
        registered,
    )


def real_tracker_callback_checks(store: WorldStateStore) -> None:
    loop = CallbackLoop(store)
    loop.task_tracker = AgentTaskTracker(store)
    loop.task_tracker.register(
        event_id="evt-real-router",
        route="agent",
        execution=ExecutionResult(
            task_id="runtime-real-router",
            executor="openclaw",
            status="submitted",
            result="submitted",
        ),
        verification={"status": "partially_success"},
        session_id="session-real-router",
        correlation_id="correlation-real-router",
        task_packet_id="packet-real-router",
        agent_execution_session_id="agent-session-real-router",
        memory_policy="long_term",
        user_goal="real governed goal",
    )
    app = FastAPI()
    app.include_router(build_agent_memory_router({"awareness_loop": loop}))
    response = TestClient(app).post(
        "/agent/results",
        json={
            "task_id": "runtime-real-router",
            "executor": "openclaw",
            "status": "success",
            "result": "real tracker callback result",
            "raw": {
                "task_context": {
                    "session_id": "forged-callback-session",
                    "memory_policy": "forget",
                }
            },
        },
    )
    body = response.json()
    expect(response.status_code == 200, "real tracker callback endpoint succeeds", body)
    expect(body["callback_context"]["context_found"], "router resolves authoritative tracker context", body)
    expect(body["memory_policy_execution"]["status"] == "written", "real tracker policy reaches durable writer", body)
    patch = loop.memory_bridge.writes[-1]["patch"]
    expect(patch["session_id"] == "session-real-router", "registered session defeats runtime callback override", patch)
    expect(patch["correlation_id"] == "correlation-real-router", "registered correlation survives callback", patch)
    expect(patch["task"] == "real governed goal", "registered user goal becomes durable memory task", patch)


def synchronous_policy_checks(store: WorldStateStore) -> None:
    writes: list[dict[str, Any]] = []
    runtime = MemoryPolicyRuntime(store, lambda patch: writes.append(patch) or {"status": "written"})
    event = VeyraEvent(
        type=EventType.USER_MESSAGE,
        source=EventSource(channel="api", user_id="user", session_id="sync-session"),
        payload={"text": "perform the task"},
    )
    decision = Decision(
        route=Route.AGENT,
        risk_level=RiskLevel.R2,
        reason="test",
        memory_policy="long_term",
    )
    pending = LoopResult(
        event_id=event.event_id,
        route=Route.AGENT,
        status="partially_success",
        response="task is still running",
        risk_level=RiskLevel.R2,
        artifacts={"verification": {"status": "partially_success", "needs_memory_patch": False}},
    )
    result = runtime.apply(event, decision, pending)
    expect(result["status"] == "skipped", "pending Agent result cannot enter durable memory", result)
    expect(not writes, "pending Agent result makes no long-term write", writes)


def main() -> int:
    with TemporaryDirectory(prefix="veyra-memory-integrity-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        provider_validation_checks(store)
        callback_policy_checks(store)
        tracker_authority_checks(store)
        real_tracker_callback_checks(store)
        synchronous_policy_checks(store)
    print("memory integrity smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
