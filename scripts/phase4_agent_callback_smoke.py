#!/usr/bin/env python3
"""HTTP smoke for Phase 4 Agent callback, polling, refresh, and stop paths.

The scenarios use temporary state and two fake Agent runtimes. They prove that
Durable Case tasks are recovered through their dispatch-time binding instead
of whichever Agent happens to be selected later. A bounded Agent dialogue is a
proposal only: it must not reach the generic execution verifier or Agent-result
memory writer.
"""
from __future__ import annotations

import copy
import sys
import tempfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.response_synthesizer import ResponseSynthesizer  # noqa: E402
from core.task_packet_builder import TaskPacketBuilder  # noqa: E402
from core.verifier import Verifier  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402
from interface.agent_adapter import AgentAdapter, ExecutionResult  # noqa: E402
from interface.event_schema import (  # noqa: E402
    EventSource,
    EventType,
    VeyraEvent,
    VeyraTaskPacket,
)
from rollback_audit.execution_trace import ExecutionTrace  # noqa: E402
from routers.agent_memory import build_agent_memory_router  # noqa: E402
from runtime.agent_task_tracker import AgentTaskTracker  # noqa: E402
from runtime.bounded_agent_negotiation import (  # noqa: E402
    BoundedNegotiationRuntime,
)
from runtime.durable_case_store import DurableCaseStore  # noqa: E402


WORKSPACE_ID = "workspace-phase4-callback"
USER_ID = "user-phase4-callback"
TARGET_AGENT = "openclaw"
SELECTED_AGENT = "selected-other"
PRIVATE_PROVIDER_DEBUG = "PRIVATE_PHASE4_PROVIDER_DEBUG"
OTHER_USER_GOAL = "TOP SECRET OTHER USER GOAL"
OTHER_USER_SESSION = "private-other-user-agent-session"
OTHER_USER_RUN = "private-other-user-runtime-run"
PASS_COUNT = 0


def expect(condition: bool, label: str, detail: Any = None) -> None:
    global PASS_COUNT
    if not condition:
        suffix = f": {detail!r}" if detail is not None else ""
        raise AssertionError(f"{label}{suffix}")
    PASS_COUNT += 1
    print(f"PASS {label}")


def forbidden_public_paths(value: Any) -> list[str]:
    forbidden = {
        "agent_execution_session_id",
        "binding_digest",
        "operation_id",
        "pending_agent_tasks",
        "raw",
        "run_id",
        "runtime_run_id",
        "runtime_task_id",
        "scope_digest",
        "session_key",
        "task_context",
        "task_packet_id",
    }
    paths: list[str] = []

    def walk(item: Any, path: str) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                next_path = f"{path}.{key}"
                if key in forbidden:
                    paths.append(next_path)
                walk(child, next_path)
        elif isinstance(item, list):
            for index, child in enumerate(item):
                walk(child, f"{path}[{index}]")

    walk(value, "$")
    return paths


class CountingVerifier:
    """Expose accidental fallback to the generic execution verifier."""

    def __init__(self) -> None:
        self.delegate = Verifier()
        self.generic_calls = 0
        self.dialogue_calls = 0

    def verify_execution_result(
        self, execution: ExecutionResult
    ) -> dict[str, Any]:
        self.generic_calls += 1
        return self.delegate.verify_execution_result(execution)

    def verify_agent_dialogue(
        self, agent_response: Any, **kwargs: Any
    ) -> dict[str, Any]:
        self.dialogue_calls += 1
        return self.delegate.verify_agent_dialogue(
            agent_response, **kwargs
        )


class FakeAgentAdapter(AgentAdapter):
    REQUIRED_FEATURES = {
        "agent_dialogue_v1": True,
        "caller_supplied_run_id": True,
        "idempotent_submit": True,
        "exact_stop": True,
        "tool_proxy_enforced": True,
        "enforced_execution_profile": "phase3_sandbox_proposal",
    }

    def __init__(self, name: str) -> None:
        self.name = name
        self.fetch_results: dict[str, ExecutionResult] = {}
        self.fetch_calls: list[str] = []
        self.bound_fetch_calls: list[dict[str, Any]] = []
        self.cancel_calls: list[dict[str, Any]] = []
        self.cancel_receipts: list[dict[str, Any]] = []

    def send_task(self, task_packet: VeyraTaskPacket) -> ExecutionResult:
        return ExecutionResult(
            task_id=str(
                task_packet.runtime_run_id or task_packet.task_id
            ),
            executor=self.name,
            status="submitted",
            result="bounded dialogue submitted",
        )

    def connection_status(self) -> dict[str, Any]:
        return {
            "connected": True,
            "features": dict(self.REQUIRED_FEATURES),
        }

    def fetch_task_status(self, task_id: str) -> ExecutionResult:
        self.fetch_calls.append(task_id)
        return copy.deepcopy(
            self.fetch_results.get(
                task_id,
                ExecutionResult(
                    task_id=task_id,
                    executor=self.name,
                    status="submitted",
                    result="still pending",
                    raw={"exact_run_observed": True},
                ),
            )
        )

    def fetch_bound_task_status(
        self,
        task_id: str,
        *,
        identity: dict[str, Any],
    ) -> ExecutionResult:
        self.bound_fetch_calls.append(
            {
                "task_id": task_id,
                "identity": copy.deepcopy(identity),
            }
        )
        return self.fetch_task_status(task_id)

    def cancel_task_authority(
        self,
        task_id: str,
        *,
        reason: str = "user_requested_stop",
        identity: dict[str, Any] | None = None,
        abort_agent: bool = True,
    ) -> dict[str, Any]:
        self.cancel_calls.append(
            {
                "task_id": task_id,
                "reason": reason,
                "identity": copy.deepcopy(identity),
                "abort_agent": abort_agent,
            }
        )
        receipt = (
            copy.deepcopy(self.cancel_receipts.pop(0))
            if self.cancel_receipts
            else {
                "status": "cancelled",
                "authority_revoked": True,
                "plugin_authority_closed": True,
                "agent_abort_confirmed": True,
                "executing_reservations": [],
            }
        )
        receipt.setdefault("run_id", task_id)
        receipt.setdefault("reason", reason)
        return receipt


class FakeRegistry:
    def __init__(
        self,
        *,
        target: FakeAgentAdapter,
        selected: FakeAgentAdapter,
    ) -> None:
        self.adapters = {
            target.name: target,
            selected.name: selected,
        }
        self._selected_name = selected.name

    def names(self) -> list[str]:
        return list(self.adapters)

    def get(self, name: str) -> FakeAgentAdapter:
        return self.adapters[name]

    def selected(self) -> FakeAgentAdapter:
        return self.adapters[self._selected_name]

    def selected_name(self) -> str:
        return self._selected_name


class RecordingMemoryBridge:
    def __init__(self) -> None:
        self.writes: list[dict[str, Any]] = []

    def write_patch(
        self,
        patch: dict[str, Any],
        provider: str = "selected",
    ) -> dict[str, Any]:
        self.writes.append(
            {"patch": copy.deepcopy(patch), "provider": provider}
        )
        return {"status": "written"}


class RecordingMemoryPolicy:
    def __init__(self) -> None:
        self.calls = 0

    def apply_agent_result(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        self.calls += 1
        return {"status": "written", "write": {"unexpected": True}}


class LoopFixture:
    def __init__(self, root: Path) -> None:
        self.state_store = WorldStateStore(root)
        self.state_store.patch_json(
            "local_world.json",
            {"current_project": WORKSPACE_ID},
        )
        self.durable_case_store = DurableCaseStore(self.state_store)
        self.verifier = CountingVerifier()
        self.execution_trace = ExecutionTrace(self.state_store)
        self.task_tracker = AgentTaskTracker(
            self.state_store,
            self.execution_trace,
        )
        self.target_adapter = FakeAgentAdapter(TARGET_AGENT)
        self.selected_adapter = FakeAgentAdapter(SELECTED_AGENT)
        self.agent_registry = FakeRegistry(
            target=self.target_adapter,
            selected=self.selected_adapter,
        )
        self.agent_adapter = self.selected_adapter
        self.memory_bridge = RecordingMemoryBridge()
        self.memory_policy_runtime = RecordingMemoryPolicy()
        self.bounded_negotiation = BoundedNegotiationRuntime(
            state_store=self.state_store,
            case_store=self.durable_case_store,
            task_packet_builder=TaskPacketBuilder(self.state_store),
            verifier=self.verifier,
            response_synthesizer=ResponseSynthesizer(),
            registry=self.agent_registry,
            task_tracker=self.task_tracker,
        )
        app = FastAPI()
        app.include_router(
            build_agent_memory_router(
                {
                    "awareness_loop": self,
                    "state_store": self.state_store,
                }
            )
        )
        self.client = TestClient(app)

    def prepare_registered_case(
        self,
        event_id: str,
        *,
        registered_revision_delta: int = 0,
        record_submitted: bool = True,
    ) -> dict[str, Any]:
        prepared = self.bounded_negotiation.prepare(
            event=VeyraEvent(
                type=EventType.USER_MESSAGE,
                source=EventSource(
                    channel="api",
                    user_id=USER_ID,
                    session_id=f"session-{event_id}",
                ),
                payload={
                    "text": (
                        "Compare two read-only recovery options. "
                        "Do not change anything."
                    )
                },
                event_id=event_id,
            ),
            selected_agent=TARGET_AGENT,
            context_patch={
                "user_goal": (
                    "Compare two read-only recovery options without "
                    "changing state."
                ),
                "constraints": ["read-only comparison"],
            },
            persona_patch={},
            policy_patch={"risk_level": "R0"},
            required_capabilities=["runtime.status.read"],
            memory_policy="long_term",
            agent_execution_session_id=f"agent-session-{event_id}",
            agent_session_policy="ephemeral_per_task",
        )
        binding = prepared["binding"]
        execution = ExecutionResult(
            task_id=binding["runtime_run_id"],
            executor=TARGET_AGENT,
            status="submitted",
            result="bounded dialogue submitted",
            raw={
                "exact_run_observed": True,
                "task_context": {
                    "agent_execution_session_id": binding["session_key"],
                },
            },
        )
        pending = (
            self.bounded_negotiation.accept_execution(
                prepared=prepared,
                execution=execution,
            )
            if record_submitted
            else {
                "verification": (
                    self.bounded_negotiation._pending_verification(
                        "submitted"
                    )
                )
            }
        )
        self.task_tracker.register(
            event_id=event_id,
            route="agent",
            execution=execution,
            verification=pending["verification"],
            session_id=f"session-{event_id}",
            channel="api",
            user_id=USER_ID,
            correlation_id=event_id,
            task_packet_id=binding["task_packet_id"],
            agent_execution_session_id=binding["session_key"],
            agent_session_policy="ephemeral_per_task",
            memory_policy="long_term",
            user_goal="Compare two read-only recovery options.",
            case_id=binding["case_id"],
            case_workspace_id=binding["workspace_id"],
            case_step_id=binding["step_id"],
            case_operation_id=binding["operation_id"],
            case_revision=(
                int(binding["case_revision"])
                + registered_revision_delta
            ),
            dialogue_message_id=binding["message_id"],
            target_agent=TARGET_AGENT,
        )
        return prepared


def option_set(prepared: dict[str, Any], suffix: str) -> dict[str, Any]:
    request = prepared["request"]
    return {
        "contract_version": "veyra.agent_dialogue.v1",
        "message_id": f"agent_options_{suffix}",
        "message_type": "OPTION_SET",
        "case_id": request["case_id"],
        "case_revision": request["case_revision"],
        "turn_index": request["turn_index"],
        "task_packet_id": request["task_packet_id"],
        "operation_id": request["operation_id"],
        "scope_digest": request["scope_digest"],
        "sender": "agent",
        "in_reply_to": request["message_id"],
        "payload": {
            "options": [
                {
                    "option_id": "observe",
                    "summary": "Collect one fresh read-only observation.",
                    "assumptions": ["The existing observation may be stale."],
                    "expected_outcome": "Refresh evidence for Veyra.",
                    "costs": ["One read-only probe."],
                    "risks": ["The decision is delayed."],
                    "evidence_refs": [],
                    "required_capabilities": ["runtime.status.read"],
                },
                {
                    "option_id": "pause",
                    "summary": "Pause until the user provides context.",
                    "assumptions": ["No execution authority exists."],
                    "expected_outcome": "Avoid acting on weak evidence.",
                    "costs": ["One user interaction."],
                    "risks": ["Resolution takes longer."],
                    "evidence_refs": [],
                    "required_capabilities": [],
                },
            ],
            "recommended_option_id": "observe",
        },
    }


def result_payload(
    prepared: dict[str, Any],
    *,
    task_id: str | None = None,
    executor: str = TARGET_AGENT,
    suffix: str,
) -> dict[str, Any]:
    binding = prepared["binding"]
    return {
        "task_id": task_id or binding["runtime_run_id"],
        "executor": executor,
        "status": "success",
        "result": "bounded option proposal",
        "raw": {
            "exact_run_observed": True,
            "agent_response": {
                "answer_or_plan": "These are proposals only.",
                "dialogue_message": option_set(prepared, suffix),
            },
        },
    }


def terminal_execution(
    prepared: dict[str, Any],
    suffix: str,
) -> ExecutionResult:
    payload = result_payload(prepared, suffix=suffix)
    return ExecutionResult(
        task_id=payload["task_id"],
        executor=payload["executor"],
        status=payload["status"],
        result=payload["result"],
        raw={
            **payload["raw"],
            "provider_debug": PRIVATE_PROVIDER_DEBUG,
        },
    )


def callback_contract_checks(root: Path) -> None:
    loop = LoopFixture(root / "callback")
    prepared = loop.prepare_registered_case("event-callback")
    run_id = prepared["binding"]["runtime_run_id"]
    loop.task_tracker.register(
        event_id="other-user-event",
        route="agent",
        execution=ExecutionResult(
            task_id=OTHER_USER_RUN,
            executor=TARGET_AGENT,
            status="submitted",
            result="private other user task",
        ),
        verification={
            "status": "partially_success",
            "next_action": "poll_runtime_or_probe_result",
        },
        session_id="other-user-dialogue",
        channel="api",
        user_id="other-user",
        task_packet_id="private-other-user-task-packet",
        agent_execution_session_id=OTHER_USER_SESSION,
        user_goal=OTHER_USER_GOAL,
    )
    loop.target_adapter.fetch_results[run_id] = terminal_execution(
        prepared, "authoritative_callback_fetch"
    )
    response = loop.client.post(
        "/agent/results",
        json={
            **result_payload(prepared, suffix="forged_callback"),
            "status": "success",
            "result": "caller-forged result must be ignored",
            "raw": {
                "agent_response": {
                    "dialogue_message": {
                        "message_type": "FORGED_CALLBACK"
                    }
                },
                "governance_cleanup": {
                    "status": "cancelled",
                    "authority_revoked": True,
                    "plugin_authority_closed": True,
                    "agent_abort_confirmed": True,
                },
            },
        },
    )
    body = response.json()
    expect(response.status_code == 200, "registered callback is accepted", body)
    expect(
        body["status"] == "partially_success",
        "OPTION_SET remains a partial proposal",
        body,
    )
    expect(
        body["durable_case"]["status"] == "PROPOSED",
        "registered OPTION_SET advances Case to PROPOSED",
        body,
    )
    expect(
        len(loop.target_adapter.bound_fetch_calls) == 1
        and body["agent_dialogue"]["message_id"]
        == "agent_options_authoritative_callback_fetch",
        "callback is only a wake-up hint and uses exact bound re-fetch",
        {
            "fetches": loop.target_adapter.bound_fetch_calls,
            "dialogue": body.get("agent_dialogue"),
        },
    )
    expect(
        len(loop.target_adapter.cancel_calls) == 1
        and loop.target_adapter.cancel_calls[0]["abort_agent"] is False
        and loop.target_adapter.cancel_calls[0]["identity"]
        == {"run_id": run_id},
        "terminal callback closes broker and plugin authority before Case advance",
        loop.target_adapter.cancel_calls,
    )
    expect(
        PRIVATE_PROVIDER_DEBUG not in response.text
        and OTHER_USER_GOAL not in response.text
        and OTHER_USER_SESSION not in response.text
        and OTHER_USER_RUN not in response.text
        and run_id not in response.text
        and not forbidden_public_paths(body),
        "Case callback response recursively redacts provider and cross-user private state",
        {
            "forbidden": forbidden_public_paths(body),
            "body": body,
        },
    )
    expect(
        body["verification"]["evidence"]["proposal_is_authority"]
        is False,
        "callback proposal grants no authority",
        body,
    )
    expect(
        body["memory_policy_execution"]["status"] == "skipped"
        and body["memory_write"] is None,
        "Case callback cannot write Agent-result memory",
        body,
    )
    expect(
        loop.verifier.generic_calls == 0,
        "Case callback bypasses generic execution verification",
        loop.verifier.generic_calls,
    )
    expect(
        loop.memory_policy_runtime.calls == 0
        and loop.memory_bridge.writes == [],
        "Case callback bypasses all memory writers",
        {
            "policy_calls": loop.memory_policy_runtime.calls,
            "writes": loop.memory_bridge.writes,
        },
    )

    unconfirmed = LoopFixture(root / "callback-unconfirmed-closure")
    unconfirmed_prepared = unconfirmed.prepare_registered_case(
        "event-callback-unconfirmed-closure"
    )
    unconfirmed_run = unconfirmed_prepared["binding"][
        "runtime_run_id"
    ]
    unconfirmed.target_adapter.fetch_results[unconfirmed_run] = (
        terminal_execution(unconfirmed_prepared, "unconfirmed_closure")
    )
    unconfirmed.target_adapter.cancel_receipts = [
        {
            "status": "cancellation_unconfirmed",
            "authority_revoked": True,
            "plugin_authority_closed": False,
            "agent_abort_confirmed": True,
            "executing_reservations": [],
        }
    ]
    blocked = unconfirmed.client.post(
        "/agent/results",
        json=result_payload(
            unconfirmed_prepared,
            suffix="untrusted_cleanup_claim",
        ),
    )
    blocked_case = unconfirmed.durable_case_store.get_case(
        case_id=unconfirmed_prepared["binding"]["case_id"],
        user_id=USER_ID,
        workspace_id=WORKSPACE_ID,
    )
    pending = unconfirmed.state_store.read_json(
        "task_state.json"
    ).get("pending_agent_tasks", [])
    expect(
        blocked.status_code == 409
        and blocked_case["status"] == "DELIBERATING"
        and any(
            isinstance(item, dict)
            and item.get("task_id") == unconfirmed_run
            for item in pending
        ),
        "unconfirmed terminal authority closure keeps Case supervised",
        {
            "response": blocked.text,
            "case": blocked_case,
            "pending": pending,
        },
    )


def wrong_binding_checks(root: Path) -> None:
    wrong_run = LoopFixture(root / "wrong-run")
    prepared_run = wrong_run.prepare_registered_case("event-wrong-run")
    response = wrong_run.client.post(
        "/agent/results",
        json=result_payload(
            prepared_run,
            task_id=prepared_run["binding"]["task_packet_id"],
            suffix="wrong_run",
        ),
    )
    expect(
        response.status_code == 409
        and "runtime_task_id" in response.text,
        "callback with wrong registered run identity is rejected",
        response.text,
    )

    wrong_runtime = LoopFixture(root / "wrong-runtime")
    prepared_runtime = wrong_runtime.prepare_registered_case(
        "event-wrong-runtime"
    )
    response = wrong_runtime.client.post(
        "/agent/results",
        json=result_payload(
            prepared_runtime,
            executor=SELECTED_AGENT,
            suffix="wrong_runtime",
        ),
    )
    expect(
        response.status_code == 409
        and "target_agent" in response.text,
        "callback from wrong Agent runtime is rejected",
        response.text,
    )

    wrong_revision = LoopFixture(root / "wrong-revision")
    prepared_revision = wrong_revision.prepare_registered_case(
        "event-wrong-revision",
        registered_revision_delta=1,
    )
    response = wrong_revision.client.post(
        "/agent/results",
        json=result_payload(
            prepared_revision,
            suffix="wrong_revision",
        ),
    )
    expect(
        response.status_code == 409
        and "case_revision" in response.text,
        "callback with wrong registered Case revision is rejected",
        response.text,
    )
    for fixture, prepared in (
        (wrong_run, prepared_run),
        (wrong_runtime, prepared_runtime),
        (wrong_revision, prepared_revision),
    ):
        case = fixture.durable_case_store.get_case(
            case_id=prepared["binding"]["case_id"],
            user_id=USER_ID,
            workspace_id=WORKSPACE_ID,
        )
        expect(
            case["status"] == "DELIBERATING",
            "rejected callback leaves Case evaluable",
            case,
        )


def poll_binding_checks(root: Path) -> None:
    loop = LoopFixture(root / "poll")
    prepared = loop.prepare_registered_case("event-poll")
    run_id = prepared["binding"]["runtime_run_id"]
    loop.target_adapter.fetch_results[run_id] = terminal_execution(
        prepared, "poll"
    )
    response = loop.client.get(f"/agent/tasks/{run_id}")
    body = response.json()
    expect(response.status_code == 200, "Case task poll succeeds", body)
    expect(
        body["durable_case"]["status"] == "PROPOSED",
        "task poll accepts the bound OPTION_SET",
        body,
    )
    expect(
        loop.target_adapter.fetch_calls == [run_id],
        "task poll uses dispatch-time target Agent",
        loop.target_adapter.fetch_calls,
    )
    expect(
        loop.selected_adapter.fetch_calls == [],
        "task poll ignores the currently selected Agent",
        loop.selected_adapter.fetch_calls,
    )
    expect(
        loop.verifier.generic_calls == 0,
        "Case task poll bypasses generic execution verifier",
        loop.verifier.generic_calls,
    )
    expect(
        run_id not in response.text
        and not forbidden_public_paths(body),
        "Case task poll exposes no provider binding or raw trace",
        {
            "forbidden": forbidden_public_paths(body),
            "body": body,
        },
    )

    queue_timeout = LoopFixture(root / "poll-queue-timeout")
    queue_prepared = queue_timeout.prepare_registered_case(
        "event-poll-queue-timeout",
        record_submitted=False,
    )
    queue_run = queue_prepared["binding"]["runtime_run_id"]
    queue_timeout.target_adapter.fetch_results[queue_run] = (
        ExecutionResult(
            task_id=queue_run,
            executor=TARGET_AGENT,
            status="submitted",
            result="provider never started this queued run",
            raw={
                "agent_wait": {
                    "runId": queue_run,
                    "status": "timeout",
                    "timeoutPhase": "queue",
                    "providerStarted": False,
                }
            },
        )
    )
    rejected = queue_timeout.client.get(
        f"/agent/tasks/{queue_run}"
    )
    queue_case = queue_timeout.durable_case_store.get_case(
        case_id=queue_prepared["binding"]["case_id"],
        user_id=USER_ID,
        workspace_id=WORKSPACE_ID,
    )
    expect(
        rejected.status_code == 409
        and queue_case["revision"] == 2
        and queue_case["checkpoints"][-1]["phase"]
        == "agent_dispatch_prepared",
        "queue timeout without provider start cannot become dispatched",
        {
            "response": rejected.text,
            "case": queue_case,
        },
    )


def refresh_binding_checks(root: Path) -> None:
    loop = LoopFixture(root / "refresh")
    prepared = loop.prepare_registered_case("event-refresh")
    run_id = prepared["binding"]["runtime_run_id"]
    loop.task_tracker.register(
        event_id="refresh-other-user-event",
        route="agent",
        execution=ExecutionResult(
            task_id=OTHER_USER_RUN,
            executor=TARGET_AGENT,
            status="submitted",
            result="private pending task",
        ),
        verification={
            "status": "partially_success",
            "next_action": "poll_runtime_or_probe_result",
        },
        session_id="refresh-other-user-dialogue",
        channel="api",
        user_id="refresh-other-user",
        task_packet_id="private-refresh-task-packet",
        agent_execution_session_id=OTHER_USER_SESSION,
        user_goal=OTHER_USER_GOAL,
        case_id="case-private-refresh-other-user",
        case_workspace_id="workspace-private-refresh-other-user",
        case_step_id="step-private-refresh-other-user",
        case_operation_id="operation-private-refresh-other-user",
        case_revision=1,
        dialogue_message_id="message-private-refresh-other-user",
        target_agent=TARGET_AGENT,
    )
    loop.target_adapter.fetch_results[run_id] = terminal_execution(
        prepared, "refresh"
    )
    response = loop.client.post("/agent/tasks/refresh")
    body = response.json()
    expect(response.status_code == 200, "Case task refresh succeeds", body)
    expect(
        body["durable_cases"]["processed_count"] == 1,
        "refresh reconciles the one pending Case",
        body,
    )
    expect(
        loop.verifier.dialogue_calls == 1
        and loop.verifier.generic_calls == 0,
        "refresh verifies Case dialogue once and never generically",
        {
            "dialogue": loop.verifier.dialogue_calls,
            "generic": loop.verifier.generic_calls,
        },
    )
    expect(
        len(loop.target_adapter.bound_fetch_calls) == 1
        and loop.target_adapter.bound_fetch_calls[0]["task_id"] == run_id,
        "refresh uses bound recovery on the stored target Agent",
        loop.target_adapter.bound_fetch_calls,
    )
    expect(
        loop.selected_adapter.fetch_calls == []
        and loop.selected_adapter.bound_fetch_calls == [],
        "legacy refresh does not poll Case task through selected Agent",
        {
            "fetch": loop.selected_adapter.fetch_calls,
            "bound": loop.selected_adapter.bound_fetch_calls,
        },
    )
    expect(
        run_id not in response.text
        and OTHER_USER_RUN not in response.text
        and OTHER_USER_SESSION not in response.text
        and OTHER_USER_GOAL not in response.text
        and not forbidden_public_paths(body),
        "task refresh returns counts and public Case summaries only",
        {
            "forbidden": forbidden_public_paths(body),
            "body": body,
        },
    )


def stop_binding_checks(root: Path) -> None:
    loop = LoopFixture(root / "stop")
    prepared = loop.prepare_registered_case("event-stop")
    run_id = prepared["binding"]["runtime_run_id"]
    response = loop.client.post(f"/agent/tasks/{run_id}/stop")
    body = response.json()
    expect(response.status_code == 200, "Case task stop succeeds", body)
    expect(
        body["stopped"] is True
        and body["result"]["case"]["status"] == "CANCELLED",
        "stop completes only after Case cancellation receipt",
        body,
    )
    expect(
        len(loop.target_adapter.cancel_calls) == 1
        and loop.target_adapter.cancel_calls[0]["task_id"] == run_id
        and loop.target_adapter.cancel_calls[0]["abort_agent"] is True,
        "stop routes exact run cancellation through stored target Agent",
        loop.target_adapter.cancel_calls,
    )
    expect(
        loop.selected_adapter.cancel_calls == [],
        "stop never cancels through the currently selected Agent",
        loop.selected_adapter.cancel_calls,
    )
    expect(
        run_id not in response.text
        and body.get("task_id") is None
        and body.get("case_id")
        == prepared["binding"]["case_id"]
        and not forbidden_public_paths(body),
        "Case stop returns public Case identity without provider run authority",
        {
            "forbidden": forbidden_public_paths(body),
            "body": body,
        },
    )
    case = loop.durable_case_store.get_case(
        case_id=prepared["binding"]["case_id"],
        user_id=USER_ID,
        workspace_id=WORKSPACE_ID,
    )
    expect(
        case["status"] == "CANCELLED"
        and case["checkpoints"][-1]["effect_state"] == "observed",
        "Case persists observed cancellation evidence",
        case,
    )


def main() -> int:
    with tempfile.TemporaryDirectory(
        prefix="veyra-phase4-callback-"
    ) as temp_dir:
        root = Path(temp_dir)
        callback_contract_checks(root)
        wrong_binding_checks(root)
        poll_binding_checks(root)
        refresh_binding_checks(root)
        stop_binding_checks(root)
    print(f"phase4 agent callback smoke passed ({PASS_COUNT} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
