#!/usr/bin/env python3
"""Offline Phase 4 Durable Case + bounded Agent negotiation smoke.

This smoke intentionally uses only temporary state and fake Agent adapters. It
tests lifecycle, exact binding, replay, recovery and cancellation fences. Agent
dialogue is always a proposal: it must never become execution authority or a
verified outcome.
"""
from __future__ import annotations

import copy
import json
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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
from runtime.bounded_agent_negotiation import (  # noqa: E402
    BoundedNegotiationError,
    BoundedNegotiationRuntime,
)
from runtime.durable_case_store import DurableCaseStore  # noqa: E402


WORKSPACE_ID = "workspace-phase4-smoke"
USER_ID = "user-phase4-smoke"
AGENT_NAME = "openclaw"
PASS_COUNT = 0


def expect(condition: bool, label: str, detail: Any = None) -> None:
    global PASS_COUNT
    if not condition:
        suffix = f": {detail!r}" if detail is not None else ""
        raise AssertionError(f"{label}{suffix}")
    PASS_COUNT += 1


def raises(error_type: type[BaseException], call: Any, label: str) -> None:
    try:
        call()
    except error_type:
        expect(True, label)
    else:
        raise AssertionError(label)


class FakeAgentAdapter(AgentAdapter):
    """Exact, scriptable Agent runtime without network or side effects."""

    REQUIRED_FEATURES = {
        "agent_dialogue_v1": True,
        "caller_supplied_run_id": True,
        "idempotent_submit": True,
        "exact_stop": True,
        "tool_proxy_enforced": True,
        "enforced_execution_profile": "phase3_sandbox_proposal",
    }

    def __init__(self, *, features: dict[str, Any] | None = None) -> None:
        self.features = {
            **self.REQUIRED_FEATURES,
            **(features or {}),
        }
        self.connected = True
        self.sent: list[VeyraTaskPacket] = []
        self.status_by_run: dict[str, ExecutionResult] = {}
        self.fetches: list[dict[str, Any]] = []
        self.cancel_receipts: list[dict[str, Any]] = []
        self.cancel_calls: list[dict[str, Any]] = []

    def send_task(self, task_packet: VeyraTaskPacket) -> ExecutionResult:
        self.sent.append(task_packet)
        return ExecutionResult(
            task_id=str(task_packet.runtime_run_id or task_packet.task_id),
            executor=AGENT_NAME,
            status="submitted",
            result="bounded dialogue submitted",
            raw={"exact_run_observed": True},
        )

    def connection_status(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "features": dict(self.features),
        }

    def fetch_task_status(self, task_id: str) -> ExecutionResult:
        return self.status_by_run.get(
            task_id,
            ExecutionResult(
                task_id=task_id,
                executor=AGENT_NAME,
                status="adapter_unconfigured",
                result="no exact observation",
                raw={"exact_run_observed": False},
            ),
        )

    def fetch_bound_task_status(
        self,
        task_id: str,
        *,
        identity: dict[str, Any],
    ) -> ExecutionResult:
        self.fetches.append(
            {"task_id": task_id, "identity": copy.deepcopy(identity)}
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
        if self.cancel_receipts:
            receipt = copy.deepcopy(self.cancel_receipts.pop(0))
        else:
            receipt = cancellation_receipt("cancelled")
        receipt.setdefault("run_id", task_id)
        receipt.setdefault("reason", reason)
        return receipt


class FakeRegistry:
    def __init__(self, adapter: FakeAgentAdapter) -> None:
        self.adapter = adapter

    def names(self) -> list[str]:
        return [AGENT_NAME]

    def get(self, name: str) -> FakeAgentAdapter:
        if name != AGENT_NAME:
            raise KeyError(name)
        return self.adapter


@dataclass
class Fixture:
    store: WorldStateStore
    cases: DurableCaseStore
    adapter: FakeAgentAdapter
    runtime: BoundedNegotiationRuntime


def fixture(root: Path, *, adapter: FakeAgentAdapter | None = None) -> Fixture:
    store = WorldStateStore(root)
    store.patch_json("local_world.json", {"current_project": WORKSPACE_ID})
    selected_adapter = adapter or FakeAgentAdapter()
    cases = DurableCaseStore(store)
    runtime = BoundedNegotiationRuntime(
        state_store=store,
        case_store=cases,
        task_packet_builder=TaskPacketBuilder(store),
        verifier=Verifier(),
        response_synthesizer=ResponseSynthesizer(),
        registry=FakeRegistry(selected_adapter),
    )
    return Fixture(
        store=store,
        cases=cases,
        adapter=selected_adapter,
        runtime=runtime,
    )


def event(event_id: str) -> VeyraEvent:
    return VeyraEvent(
        type=EventType.USER_MESSAGE,
        source=EventSource(
            channel="api",
            user_id=USER_ID,
            session_id=f"session-{event_id}",
        ),
        payload={
            "text": (
                "Compare bounded recovery options. Do not modify anything."
            )
        },
        event_id=event_id,
    )


def prepare(runtime_fixture: Fixture, event_id: str) -> dict[str, Any]:
    prepared = runtime_fixture.runtime.prepare(
        event=event(event_id),
        selected_agent=AGENT_NAME,
        context_patch={
            "user_goal": (
                "Compare bounded recovery options without changing state."
            ),
            "constraints": ["read-only comparison"],
            "evidence_refs": ["evidence:baseline"],
            "attention_focus": ["recovery safety"],
        },
        persona_patch={},
        policy_patch={"risk_level": "R0"},
        required_capabilities=["runtime.status.read"],
        memory_policy="forget",
        agent_execution_session_id=f"agent-session-{event_id}",
        agent_session_policy="ephemeral_per_task",
    )
    expect(prepared["replayed"] is False, "new event prepares one Case")
    expect(prepared["case"]["status"] == "DELIBERATING", "prepare enters DELIBERATING")
    expect(
        prepared["packet"].runtime_run_id
        == prepared["binding"]["runtime_run_id"],
        "caller runtime run identity is frozen in the packet",
    )
    expect(
        prepared["request"]["payload"]["authority"]
        == {
            "mode": "sandbox",
            "side_effects_require_governance": True,
            "capability_expansion_authorized": False,
            "verification_authority": False,
        },
        "TASK_REQUEST freezes the proposal-only authority boundary",
    )
    return prepared


def reply(
    prepared: dict[str, Any],
    message_type: str,
    *,
    suffix: str = "1",
) -> dict[str, Any]:
    request = prepared["request"]
    payloads: dict[str, dict[str, Any]] = {
        "OPTION_SET": {
            "options": [
                {
                    "option_id": "observe",
                    "summary": "Collect a fresh read-only observation.",
                    "assumptions": ["The current observation may be stale."],
                    "expected_outcome": "Refresh the evidence available to Veyra.",
                    "costs": ["One read-only probe."],
                    "risks": ["The decision is delayed."],
                    "evidence_refs": ["evidence:baseline"],
                    "required_capabilities": ["runtime.status.read"],
                },
                {
                    "option_id": "pause",
                    "summary": "Pause until the user supplies more context.",
                    "assumptions": ["No execution authority exists."],
                    "expected_outcome": "Avoid acting on incomplete evidence.",
                    "costs": ["User interruption."],
                    "risks": ["Resolution takes longer."],
                    "evidence_refs": [],
                    "required_capabilities": [],
                },
            ],
            "recommended_option_id": "observe",
        },
        "EVIDENCE_REQUEST": {
            "requested_evidence": [
                {
                    "question": "What is the current runtime health?",
                    "reason": "A fresh observation is needed before comparison.",
                    "claim_ref": "claim:runtime-health",
                    "freshness_required": True,
                }
            ],
            # This is deliberately a requested capability, not a grant.
            "requested_capabilities": ["runtime.restart"],
        },
        "CHALLENGE": {
            "challenged_claim_refs": ["claim:runtime-health"],
            "reason": "The current observation may be stale.",
            "alternative": "Collect a fresh read-only observation first.",
            "evidence_refs": ["evidence:baseline"],
            "requested_capabilities": ["runtime.status.read"],
        },
    }
    return {
        "contract_version": "veyra.agent_dialogue.v1",
        "message_id": f"agent_{message_type.lower()}_{suffix}",
        "message_type": message_type,
        "case_id": request["case_id"],
        "case_revision": request["case_revision"],
        "turn_index": request["turn_index"],
        "task_packet_id": request["task_packet_id"],
        "operation_id": request["operation_id"],
        "scope_digest": request["scope_digest"],
        "sender": "agent",
        "in_reply_to": request["message_id"],
        "payload": payloads[message_type],
    }


def execution(
    prepared: dict[str, Any],
    *,
    status: str,
    dialogue: dict[str, Any] | None = None,
    exact: bool = True,
    task_id: str | None = None,
    executor: str = AGENT_NAME,
) -> ExecutionResult:
    binding = prepared["binding"]
    raw: dict[str, Any] = {
        "exact_run_observed": exact,
        "task_context": {
            "agent_execution_session_id": binding["session_key"],
        },
    }
    if dialogue is not None:
        raw["agent_response"] = {
            "answer_or_plan": "This is only a bounded proposal.",
            "dialogue_message": dialogue,
        }
    return ExecutionResult(
        task_id=task_id or binding["runtime_run_id"],
        executor=executor,
        status=status,
        result="bounded Agent dialogue result",
        raw=raw,
    )


def cancellation_receipt(
    status: str,
    *,
    authority_revoked: bool | None = None,
    plugin_authority_closed: bool | None = None,
    agent_abort_confirmed: bool | None = None,
    executing_reservations: list[str] | None = None,
) -> dict[str, Any]:
    confirmed = status == "cancelled"
    return {
        "status": status,
        "authority_revoked": (
            confirmed if authority_revoked is None else authority_revoked
        ),
        "plugin_authority_closed": (
            confirmed
            if plugin_authority_closed is None
            else plugin_authority_closed
        ),
        "agent_abort_confirmed": (
            confirmed
            if agent_abort_confirmed is None
            else agent_abort_confirmed
        ),
        "executing_reservations": list(executing_reservations or []),
        "revoked_grants": ["grant:phase4"] if confirmed else [],
        "cancelled_reservations": (
            ["reservation:phase4"] if confirmed else []
        ),
    }


def assert_no_promotion(
    runtime_fixture: Fixture,
    prepared: dict[str, Any],
    outcome: dict[str, Any],
) -> None:
    verification = outcome.get("verification")
    expect(
        isinstance(verification, dict)
        and verification.get("status") != "verified_success",
        "Agent dialogue is never a verified success",
        verification,
    )
    evidence = (
        verification.get("evidence")
        if isinstance(verification, dict)
        and isinstance(verification.get("evidence"), dict)
        else {}
    )
    expect(
        evidence.get("proposal_is_evidence") is False,
        "Agent proposal is explicitly not evidence",
        evidence,
    )
    expect(
        evidence.get("proposal_is_authority") is False,
        "Agent proposal is explicitly not authority",
        evidence,
    )
    case = runtime_fixture.cases.get_case(
        case_id=prepared["binding"]["case_id"],
        user_id=USER_ID,
        workspace_id=WORKSPACE_ID,
    )
    expect(
        case["status"] not in {"AUTHORIZED", "EXECUTING", "VERIFIED"},
        "Phase 4 Case has no authority or execution state",
        case["status"],
    )
    agent_records = [
        item
        for item in case.get("dialogue", [])
        if item.get("sender") == "agent"
    ]
    for item in agent_records:
        expect(
            item.get("authority_granted") is False
            and item.get("evidence_verified") is False,
            "persisted Agent dialogue grants no authority or evidence",
            item,
        )
    serialized = json.dumps(outcome, ensure_ascii=False, sort_keys=True)
    expect(
        '"authority_granted": true' not in serialized.lower()
        and '"proposal_is_authority": true' not in serialized.lower()
        and '"proposal_is_verified_outcome": true' not in serialized.lower(),
        "public outcome contains no promotion claim",
        outcome,
    )


def test_capability_gate() -> None:
    adapter = FakeAgentAdapter()
    expect(
        BoundedNegotiationRuntime.supports_adapter(adapter),
        "complete Phase 4 capability set is accepted",
    )
    for feature in (
        "agent_dialogue_v1",
        "caller_supplied_run_id",
        "idempotent_submit",
        "exact_stop",
        "tool_proxy_enforced",
    ):
        candidate = FakeAgentAdapter(features={feature: False})
        expect(
            not BoundedNegotiationRuntime.supports_adapter(candidate),
            f"missing {feature} fails the capability gate",
        )
    expect(
        not BoundedNegotiationRuntime.supports_adapter(
            FakeAgentAdapter(
                features={"enforced_execution_profile": "unrestricted"}
            )
        ),
        "wrong execution profile fails the capability gate",
    )
    disconnected = FakeAgentAdapter()
    disconnected.connected = False
    expect(
        not BoundedNegotiationRuntime.supports_adapter(disconnected),
        "disconnected Agent fails the capability gate",
    )


def test_exact_run_observation() -> None:
    immediate = ExecutionResult(
        task_id="run-immediate",
        executor=AGENT_NAME,
        status="success",
        result="done",
        raw={
            "run_id": "run-immediate",
            "chat_send": {"runId": "run-immediate"},
            "final_event": {"state": "final"},
        },
    )
    expect(
        BoundedNegotiationRuntime._has_exact_run_observation(immediate),
        "exact chat.send and terminal event prove the immediate run",
    )
    for label, raw_patch in (
        (
            "mismatched chat.send run",
            {"chat_send": {"runId": "run-other"}},
        ),
        (
            "mismatched terminal run",
            {"final_event": {"state": "final", "runId": "run-other"}},
        ),
        (
            "non-terminal chat event",
            {"final_event": {"state": "submitted"}},
        ),
        (
            "provider did not start",
            {
                "final_event": {
                    "state": "final",
                    "providerStarted": False,
                }
            },
        ),
    ):
        raw = {
            "run_id": "run-immediate",
            "chat_send": {"runId": "run-immediate"},
            "final_event": {"state": "final"},
            **raw_patch,
        }
        candidate = ExecutionResult(
            task_id="run-immediate",
            executor=AGENT_NAME,
            status="success",
            result="done",
            raw=raw,
        )
        expect(
            not BoundedNegotiationRuntime._has_exact_run_observation(
                candidate
            ),
            f"{label} cannot prove an exact run",
            raw,
        )


def test_success_transitions(root: Path) -> None:
    expected = {
        "OPTION_SET": "PROPOSED",
        "EVIDENCE_REQUEST": "AWAITING_EVIDENCE",
        "CHALLENGE": "PAUSED",
    }
    for index, (message_type, expected_status) in enumerate(expected.items()):
        runtime_fixture = fixture(root / f"success-{index}")
        prepared = prepare(runtime_fixture, f"evt-success-{index}")
        outcome = runtime_fixture.runtime.accept_execution(
            prepared=prepared,
            execution=execution(
                prepared,
                status="success",
                dialogue=reply(prepared, message_type),
            ),
        )
        expect(
            outcome["case"]["status"] == expected_status,
            f"{message_type} advances only to {expected_status}",
            outcome,
        )
        expect(
            outcome["verification"]["status"] == "partially_success",
            f"{message_type} remains a partial proposal",
            outcome["verification"],
        )
        assert_no_promotion(runtime_fixture, prepared, outcome)


def test_failed_and_wrong_bindings(root: Path) -> None:
    failed_fixture = fixture(root / "failed-terminal")
    failed_prepared = prepare(failed_fixture, "evt-failed-terminal")
    failed = failed_fixture.runtime.accept_execution(
        prepared=failed_prepared,
        execution=execution(
            failed_prepared,
            status="failed",
            dialogue=reply(failed_prepared, "OPTION_SET"),
        ),
    )
    expect(
        failed["case"]["status"] == "PAUSED",
        "failed terminal Agent run cannot advance a proposal",
        failed,
    )
    failed_case = failed_fixture.cases.get_case(
        case_id=failed_prepared["binding"]["case_id"],
        user_id=USER_ID,
        workspace_id=WORKSPACE_ID,
    )
    expect(
        len(failed_case["dialogue"]) == 1,
        "proposal from failed execution is not persisted",
        failed_case["dialogue"],
    )
    assert_no_promotion(failed_fixture, failed_prepared, failed)

    for index, field in enumerate(("case_id", "turn_index")):
        runtime_fixture = fixture(root / f"wrong-{field}")
        prepared = prepare(runtime_fixture, f"evt-wrong-{index}")
        wrong = reply(prepared, "OPTION_SET", suffix=field)
        wrong[field] = (
            "case_wrong"
            if field == "case_id"
            else int(wrong["turn_index"]) + 1
        )
        rejected = runtime_fixture.runtime.accept_execution(
            prepared=prepared,
            execution=execution(
                prepared,
                status="success",
                dialogue=wrong,
            ),
        )
        expect(
            rejected["case"]["status"] == "PAUSED"
            and rejected["verification"]["status"] == "needs_more_probe",
            f"wrong dialogue {field} fails closed without inventing verification",
            rejected,
        )
        current = runtime_fixture.cases.get_case(
            case_id=prepared["binding"]["case_id"],
            user_id=USER_ID,
            workspace_id=WORKSPACE_ID,
        )
        expect(
            len(current["dialogue"]) == 1,
            f"wrong dialogue {field} is not persisted",
            current["dialogue"],
        )

    execution_fixture = fixture(root / "wrong-execution")
    execution_prepared = prepare(
        execution_fixture, "evt-wrong-execution"
    )
    raises(
        BoundedNegotiationError,
        lambda: execution_fixture.runtime.accept_execution(
            prepared=execution_prepared,
            execution=execution(
                execution_prepared,
                status="success",
                dialogue=reply(execution_prepared, "OPTION_SET"),
                task_id="wrong-runtime-run",
            ),
        ),
        "wrong runtime execution binding raises before Case mutation",
    )
    unchanged = execution_fixture.cases.get_case(
        case_id=execution_prepared["binding"]["case_id"],
        user_id=USER_ID,
        workspace_id=WORKSPACE_ID,
    )
    expect(
        unchanged["status"] == "DELIBERATING"
        and unchanged["revision"] == 2,
        "wrong runtime execution binding leaves Case evaluable",
        unchanged,
    )

    nonexact_fixture = fixture(root / "nonexact-terminal")
    nonexact_prepared = prepare(
        nonexact_fixture, "evt-nonexact-terminal"
    )
    raises(
        BoundedNegotiationError,
        lambda: nonexact_fixture.runtime.accept_execution(
            prepared=nonexact_prepared,
            execution=execution(
                nonexact_prepared,
                status="success",
                dialogue=reply(
                    nonexact_prepared,
                    "OPTION_SET",
                    suffix="nonexact",
                ),
                exact=False,
            ),
        ),
        "terminal reply without exact run observation is rejected",
    )
    nonexact_case = nonexact_fixture.cases.get_case(
        case_id=nonexact_prepared["binding"]["case_id"],
        user_id=USER_ID,
        workspace_id=WORKSPACE_ID,
    )
    expect(
        nonexact_case["status"] == "DELIBERATING"
        and nonexact_case["revision"] == 2
        and nonexact_fixture.adapter.cancel_calls == [],
        "non-exact terminal reply leaves Case evaluable and authority untouched",
        {
            "case": nonexact_case,
            "cancellations": nonexact_fixture.adapter.cancel_calls,
        },
    )


def test_pending_and_terminal_replay(root: Path) -> None:
    runtime_fixture = fixture(root / "replay")
    prepared = prepare(runtime_fixture, "evt-replay")
    pending_execution = execution(prepared, status="submitted")
    first_pending = runtime_fixture.runtime.accept_execution(
        prepared=prepared,
        execution=pending_execution,
    )
    replayed_pending = runtime_fixture.runtime.accept_execution(
        prepared=prepared,
        execution=pending_execution,
    )
    expect(
        first_pending["case"]["revision"]
        == replayed_pending["case"]["revision"],
        "pending replay does not advance revision twice",
        (first_pending, replayed_pending),
    )
    expect(
        first_pending["case"]["status"] == "DELIBERATING",
        "pending replay keeps the Case deliberating",
    )

    terminal_execution = execution(
        prepared,
        status="success",
        dialogue=reply(prepared, "OPTION_SET", suffix="replay"),
    )
    first_terminal = runtime_fixture.runtime.accept_execution(
        prepared=prepared,
        execution=terminal_execution,
    )
    replayed_terminal = runtime_fixture.runtime.accept_execution(
        prepared=prepared,
        execution=terminal_execution,
    )
    expect(
        first_terminal["case"]["revision"]
        == replayed_terminal["case"]["revision"],
        "terminal replay does not advance revision twice",
        (first_terminal, replayed_terminal),
    )
    case = runtime_fixture.cases.get_case(
        case_id=prepared["binding"]["case_id"],
        user_id=USER_ID,
        workspace_id=WORKSPACE_ID,
    )
    expect(
        len(
            [
                item
                for item in case["dialogue"]
                if item["message_type"] == "OPTION_SET"
            ]
        )
        == 1,
        "terminal replay persists one Agent reply",
        case["dialogue"],
    )
    assert_no_promotion(runtime_fixture, prepared, replayed_terminal)


def registered_context(prepared: dict[str, Any]) -> dict[str, Any]:
    binding = prepared["binding"]
    return {
        "authority": "veyra_registered",
        "case_id": binding["case_id"],
        "case_workspace_id": binding["workspace_id"],
        "user_id": binding["user_id"],
        "case_step_id": binding["step_id"],
        "case_operation_id": binding["operation_id"],
        "case_revision": binding["case_revision"],
        "dialogue_message_id": binding["message_id"],
        "task_packet_id": binding["task_packet_id"],
        "target_agent": binding["target_agent"],
        "runtime_task_id": binding["runtime_run_id"],
    }


def test_registered_callback_binding(root: Path) -> None:
    runtime_fixture = fixture(root / "registered")
    prepared = prepare(runtime_fixture, "evt-registered")
    outcome = runtime_fixture.runtime.accept_registered_execution(
        execution=execution(
            prepared,
            status="success",
            dialogue=reply(prepared, "OPTION_SET", suffix="registered"),
        ),
        task_context=registered_context(prepared),
    )
    expect(
        outcome["case"]["status"] == "PROPOSED",
        "registered exact callback advances the bound Case",
        outcome,
    )
    assert_no_promotion(runtime_fixture, prepared, outcome)

    wrong_fixture = fixture(root / "registered-wrong")
    wrong_prepared = prepare(wrong_fixture, "evt-registered-wrong")
    context = registered_context(wrong_prepared)
    context["case_revision"] = int(context["case_revision"]) + 1
    raises(
        BoundedNegotiationError,
        lambda: wrong_fixture.runtime.accept_registered_execution(
            execution=execution(
                wrong_prepared,
                status="success",
                dialogue=reply(
                    wrong_prepared,
                    "OPTION_SET",
                    suffix="registered-wrong",
                ),
            ),
            task_context=context,
        ),
        "registered callback with wrong Case binding fails closed",
    )
    unchanged = wrong_fixture.cases.get_case(
        case_id=wrong_prepared["binding"]["case_id"],
        user_id=USER_ID,
        workspace_id=WORKSPACE_ID,
    )
    expect(
        unchanged["status"] == "DELIBERATING"
        and unchanged["revision"] == 2,
        "wrong registered callback cannot mutate the Case",
        unchanged,
    )


def test_prepared_crash_recovery(root: Path) -> None:
    exact_fixture = fixture(root / "recovery-exact")
    exact_prepared = prepare(exact_fixture, "evt-recovery-exact")
    run_id = exact_prepared["binding"]["runtime_run_id"]
    exact_fixture.adapter.status_by_run[run_id] = execution(
        exact_prepared,
        status="success",
        dialogue=reply(
            exact_prepared, "OPTION_SET", suffix="recovered"
        ),
        exact=True,
    )
    exact = exact_fixture.runtime.recover_case(
        case_id=exact_prepared["binding"]["case_id"],
        user_id=USER_ID,
        workspace_id=WORKSPACE_ID,
        expected_revision=2,
        operation_id="recover-exact",
        reason="worker restart",
    )
    expect(
        exact["status"] == "partially_success"
        and exact["case"]["status"] == "PROPOSED",
        "exact prepared-run observation reconciles the proposal",
        exact,
    )
    expect(
        exact_fixture.adapter.sent == [],
        "exact crash recovery never redispatches the Agent task",
        exact_fixture.adapter.sent,
    )
    expect(
        exact_fixture.adapter.fetches
        and exact_fixture.adapter.fetches[-1]["task_id"] == run_id,
        "crash recovery polls the frozen runtime run identity",
        exact_fixture.adapter.fetches,
    )

    ambiguous_fixture = fixture(root / "recovery-ambiguous")
    ambiguous_prepared = prepare(
        ambiguous_fixture, "evt-recovery-ambiguous"
    )
    ambiguous_run_id = ambiguous_prepared["binding"]["runtime_run_id"]
    ambiguous_fixture.adapter.status_by_run[ambiguous_run_id] = execution(
        ambiguous_prepared,
        status="adapter_unconfigured",
        exact=False,
    )
    ambiguous_fixture.adapter.cancel_receipts = [
        cancellation_receipt("cancelled")
    ]
    ambiguous = ambiguous_fixture.runtime.recover_case(
        case_id=ambiguous_prepared["binding"]["case_id"],
        user_id=USER_ID,
        workspace_id=WORKSPACE_ID,
        expected_revision=2,
        operation_id="recover-ambiguous",
        reason="worker restart",
    )
    expect(
        ambiguous["recovery_status"]
        == "ambiguous_dispatch_cancelled"
        and ambiguous["case"]["status"] == "CANCELLED",
        "ambiguous prepared dispatch is cancelled instead of redispatched",
        ambiguous,
    )
    expect(
        ambiguous_fixture.adapter.sent == [],
        "ambiguous crash recovery never redispatches the Agent task",
        ambiguous_fixture.adapter.sent,
    )
    expect(
        len(ambiguous_fixture.adapter.cancel_calls) == 1,
        "ambiguous recovery revokes the one frozen run identity",
        ambiguous_fixture.adapter.cancel_calls,
    )


def cancel(
    runtime_fixture: Fixture,
    prepared: dict[str, Any],
    *,
    operation_id: str,
    expected_revision: int = 2,
) -> dict[str, Any]:
    return runtime_fixture.runtime.cancel_case(
        case_id=prepared["binding"]["case_id"],
        user_id=USER_ID,
        workspace_id=WORKSPACE_ID,
        expected_revision=expected_revision,
        operation_id=operation_id,
        reason="user requested cancellation",
    )


def test_cancellation_fences(root: Path) -> None:
    confirmed_fixture = fixture(root / "cancel-confirmed")
    confirmed_prepared = prepare(
        confirmed_fixture, "evt-cancel-confirmed"
    )
    confirmed_fixture.adapter.cancel_receipts = [
        cancellation_receipt("cancelled")
    ]
    confirmed = cancel(
        confirmed_fixture,
        confirmed_prepared,
        operation_id="cancel-confirmed",
    )
    expect(
        confirmed["status"] == "cancelled"
        and confirmed["case"]["status"] == "CANCELLED",
        "complete revocation evidence closes cancellation",
        confirmed,
    )
    first_cancel_call_count = len(
        confirmed_fixture.adapter.cancel_calls
    )
    confirmed_replay = cancel(
        confirmed_fixture,
        confirmed_prepared,
        operation_id="cancel-confirmed",
    )
    expect(
        confirmed_replay["case"]["status"] == "CANCELLED"
        and confirmed_replay["cancellation"]["receipt_replayed"] is True,
        "completed cancel intent replays idempotently",
        confirmed_replay,
    )
    expect(
        len(confirmed_fixture.adapter.cancel_calls)
        == first_cancel_call_count,
        "completed cancel replay does not call the Agent again",
        confirmed_fixture.adapter.cancel_calls,
    )

    retry_fixture = fixture(root / "cancel-retry")
    retry_prepared = prepare(retry_fixture, "evt-cancel-retry")
    retry_fixture.adapter.cancel_receipts = [
        cancellation_receipt(
            "pending",
            authority_revoked=False,
            plugin_authority_closed=False,
            agent_abort_confirmed=False,
        ),
        cancellation_receipt("cancelled"),
    ]
    unconfirmed = cancel(
        retry_fixture,
        retry_prepared,
        operation_id="cancel-retry",
    )
    expect(
        unconfirmed["status"] == "cancellation_unconfirmed"
        and unconfirmed["case"]["status"] == "CANCELLING",
        "unconfirmed revocation keeps Case in CANCELLING",
        unconfirmed,
    )
    retried = cancel(
        retry_fixture,
        retry_prepared,
        operation_id="cancel-retry",
    )
    expect(
        retried["status"] == "cancelled"
        and retried["case"]["status"] == "CANCELLED",
        "same cancel operation may retry only exact authority revocation",
        retried,
    )
    expect(
        len(retry_fixture.adapter.cancel_calls) == 2,
        "unconfirmed cancel retry calls revocation again without Case redispatch",
        retry_fixture.adapter.cancel_calls,
    )

    plugin_fixture = fixture(root / "cancel-plugin-open")
    plugin_prepared = prepare(
        plugin_fixture, "evt-cancel-plugin-open"
    )
    plugin_fixture.adapter.cancel_receipts = [
        cancellation_receipt(
            "cancelled",
            authority_revoked=True,
            plugin_authority_closed=False,
            agent_abort_confirmed=True,
        )
    ]
    plugin_open = cancel(
        plugin_fixture,
        plugin_prepared,
        operation_id="cancel-plugin-open",
    )
    expect(
        plugin_open["status"] == "cancellation_unconfirmed"
        and plugin_open["case"]["status"] == "CANCELLING",
        "Agent abort without plugin authority closure cannot cancel Case",
        plugin_open,
    )

    too_late_fixture = fixture(root / "cancel-too-late")
    too_late_prepared = prepare(
        too_late_fixture, "evt-cancel-too-late"
    )
    too_late_fixture.adapter.cancel_receipts = [
        cancellation_receipt(
            "too_late",
            authority_revoked=False,
            plugin_authority_closed=False,
            agent_abort_confirmed=False,
            executing_reservations=["reservation:already-started"],
        )
    ]
    too_late = cancel(
        too_late_fixture,
        too_late_prepared,
        operation_id="cancel-too-late",
    )
    expect(
        too_late["status"] == "indeterminate"
        and too_late["case"]["status"] == "INDETERMINATE",
        "too-late cancellation becomes INDETERMINATE",
        too_late,
    )


def test_recovery_candidate_fairness(root: Path) -> None:
    """An ineligible older Case must not consume the recovery work limit."""

    runtime_fixture = fixture(root / "recovery-fairness")
    stale = runtime_fixture.cases.admit_event(
        event_id="evt-fairness-ineligible",
        user_id=USER_ID,
        workspace_id=WORKSPACE_ID,
        user_goal="An old Case without a dispatch checkpoint.",
    )
    runtime_fixture.cases.transition(
        case_id=stale["case_id"],
        user_id=USER_ID,
        workspace_id=WORKSPACE_ID,
        operation_id="fairness-ineligible-deliberating",
        expected_revision=1,
        to_status="DELIBERATING",
        reason="legacy deliberation without dispatch",
    )

    recoverable = prepare(
        runtime_fixture, "evt-fairness-recoverable"
    )
    run_id = recoverable["binding"]["runtime_run_id"]
    runtime_fixture.adapter.status_by_run[run_id] = execution(
        recoverable,
        status="submitted",
        exact=True,
    )
    recovered = runtime_fixture.runtime.recover_pending(
        limit=1,
        reason="fairness smoke",
    )
    expect(
        recovered["processed_count"] == 1,
        "recovery limit counts eligible work, not skipped candidates",
        recovered,
    )
    current = runtime_fixture.cases.get_case(
        case_id=recoverable["binding"]["case_id"],
        user_id=USER_ID,
        workspace_id=WORKSPACE_ID,
    )
    expect(
        current["latest_checkpoint"]["phase"] == "agent_dispatched"
        if "latest_checkpoint" in current
        else current["checkpoints"][-1]["phase"] == "agent_dispatched",
        "oldest eligible Case receives its bounded recovery turn",
        current,
    )
    expect(
        runtime_fixture.adapter.sent == [],
        "fair recovery polls and never redispatches",
        runtime_fixture.adapter.sent,
    )


def test_recovery_round_robin_survives_restart(root: Path) -> None:
    """Unconfirmed cancellations cannot permanently starve later work."""

    runtime_fixture = fixture(root / "recovery-round-robin")
    unconfirmed_receipt = cancellation_receipt(
        "cancellation_unconfirmed",
        authority_revoked=True,
        plugin_authority_closed=False,
        agent_abort_confirmed=True,
    )
    for index in range(20):
        stuck = prepare(
            runtime_fixture,
            f"evt-round-robin-stuck-{index:02d}",
        )
        runtime_fixture.adapter.cancel_receipts = [
            copy.deepcopy(unconfirmed_receipt)
        ]
        outcome = cancel(
            runtime_fixture,
            stuck,
            operation_id=f"seed-unconfirmed-{index:02d}",
        )
        expect(
            outcome["status"] == "cancellation_unconfirmed",
            "seeded cancellation remains eligible for recovery",
            outcome,
        )

    recoverable = prepare(
        runtime_fixture,
        "evt-round-robin-recoverable",
    )
    run_id = recoverable["binding"]["runtime_run_id"]
    runtime_fixture.adapter.status_by_run[run_id] = execution(
        recoverable,
        status="submitted",
        exact=True,
    )
    runtime_fixture.adapter.cancel_receipts = [
        copy.deepcopy(unconfirmed_receipt) for _ in range(50)
    ]
    first = runtime_fixture.runtime.recover_pending(
        limit=20,
        reason="round-robin first pass",
    )
    expect(
        first["processed_count"] == 20
        and all(
            fetch.get("task_id") != run_id
            for fetch in runtime_fixture.adapter.fetches
        ),
        "first bounded batch is occupied by older cancelling Cases",
        {
            "result": first,
            "fetches": runtime_fixture.adapter.fetches,
        },
    )

    # Reconstruct the store/runtime around the same durable state. An
    # in-memory-only cursor would start at zero and starve the target again.
    restarted_cases = DurableCaseStore(runtime_fixture.store)
    restarted_runtime = BoundedNegotiationRuntime(
        state_store=runtime_fixture.store,
        case_store=restarted_cases,
        task_packet_builder=TaskPacketBuilder(runtime_fixture.store),
        verifier=Verifier(),
        response_synthesizer=ResponseSynthesizer(),
        registry=FakeRegistry(runtime_fixture.adapter),
    )
    second = restarted_runtime.recover_pending(
        limit=20,
        reason="round-robin second pass after restart",
    )
    current = restarted_cases.get_case(
        case_id=recoverable["binding"]["case_id"],
        user_id=USER_ID,
        workspace_id=WORKSPACE_ID,
    )
    expect(
        second["processed_count"] == 20
        and any(
            fetch.get("task_id") == run_id
            for fetch in runtime_fixture.adapter.fetches
        )
        and current["checkpoints"][-1]["phase"]
        == "agent_dispatched",
        "persisted recovery cursor gives later eligible Case a turn",
        {
            "result": second,
            "fetches": runtime_fixture.adapter.fetches,
            "case": current,
        },
    )


def main() -> None:
    test_capability_gate()
    test_exact_run_observation()
    with tempfile.TemporaryDirectory(
        prefix="veyra-bounded-negotiation-"
    ) as temp_dir:
        root = Path(temp_dir)
        test_success_transitions(root)
        test_failed_and_wrong_bindings(root)
        test_pending_and_terminal_replay(root)
        test_registered_callback_binding(root)
        test_prepared_crash_recovery(root)
        test_cancellation_fences(root)
        test_recovery_candidate_fairness(root)
        test_recovery_round_robin_survives_restart(root)
    print(
        "bounded Agent negotiation smoke passed "
        f"({PASS_COUNT} assertions)"
    )


if __name__ == "__main__":
    main()
