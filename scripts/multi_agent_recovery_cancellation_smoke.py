#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.response_synthesizer import ResponseSynthesizer  # noqa: E402
from core.task_packet_builder import TaskPacketBuilder  # noqa: E402
from core.verifier import Verifier  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402
from interface.agent_adapter import ExecutionResult  # noqa: E402
from interface.agent_dialogue_contract import (  # noqa: E402
    expected_agent_reply_message_id,
)
from interface.event_schema import VeyraTaskPacket  # noqa: E402
from runtime.agent_capability_directory import (  # noqa: E402
    AgentCapabilityDirectory,
    AgentCapabilitySelectionError,
)
from runtime.agent_task_tracker import AgentTaskTracker  # noqa: E402
from runtime.bounded_agent_negotiation import (  # noqa: E402
    BoundedNegotiationRuntime,
)
from runtime.durable_case_store import DurableCaseStore  # noqa: E402
from runtime.read_only_agent_collaboration import (  # noqa: E402
    CollaborationStorageError,
    ReadOnlyAgentCollaborationRuntime,
)
from scripts.bounded_multi_agent_collaboration_smoke import (  # noqa: E402
    Registry,
    ScriptedOpenClaw,
    USER,
    WORKSPACE,
    event,
    options,
)


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"PASS {label}")


class RecoverableOpenClaw(ScriptedOpenClaw):
    def __init__(self) -> None:
        super().__init__()
        self.terminal: dict[str, ExecutionResult] = {}
        self.cancel_confirmed = True
        self.cancel_too_late = False

    def send_task(self, packet: VeyraTaskPacket) -> ExecutionResult:
        self.sent.append(packet)
        request = dict(packet.dialogue_message or {})
        binding = dict(request["collaboration_binding"])
        role = str(binding["role"])
        if role == "primary_analyst":
            reply = {
                "contract_version": request["contract_version"],
                "message_id": expected_agent_reply_message_id(
                    request
                ),
                "message_type": "OPTION_SET",
                "case_id": request["case_id"],
                "case_revision": request["case_revision"],
                "turn_index": request["turn_index"],
                "task_packet_id": request["task_packet_id"],
                "operation_id": request["operation_id"],
                "scope_digest": request["scope_digest"],
                "collaboration_binding": binding,
                "sender": "agent",
                "in_reply_to": request["message_id"],
                "payload": options("recovered_primary"),
            }
            self.terminal[packet.runtime_run_id] = ExecutionResult(
                task_id=packet.runtime_run_id,
                executor="openclaw",
                status="success",
                result="recovered bounded proposal",
                raw={
                    "exact_run_observed": True,
                    "governed_tool_evidence": {
                        "status": "resolved",
                        "authority_source": "test_private_tool_ledger",
                        "observed_call_count": 0,
                        "effect_count": 0,
                    },
                    "agent_response": {
                        "dialogue_message": reply,
                    },
                },
            )
        return ExecutionResult(
            task_id=packet.runtime_run_id,
            executor="openclaw",
            status="submitted",
            result="submitted",
            raw={"exact_run_observed": True},
        )

    def fetch_bound_task_status(
        self,
        task_id: str,
        *,
        identity: dict[str, Any],
    ) -> ExecutionResult:
        del identity
        return self.terminal.get(
            task_id,
            ExecutionResult(
                task_id=task_id,
                executor="openclaw",
                status="submitted",
                result="still pending",
                raw={"exact_run_observed": True},
            ),
        )

    def cancel_task_authority(
        self,
        task_id: str,
        *,
        reason: str = "user_requested_stop",
        identity: dict[str, Any] | None = None,
        abort_agent: bool = True,
    ) -> dict[str, Any]:
        if self.cancel_too_late:
            self.closed.append(task_id)
            return {
                "status": "too_late",
                "run_id": task_id,
                "reason": reason,
                "authority_revoked": False,
                "plugin_authority_closed": False,
                "agent_abort_confirmed": False,
                "executing_reservations": [
                    "reservation:already-started"
                ],
                "revoked_grants": [],
                "cancelled_reservations": [],
                "identity": {
                    "binding_digest": "a" * 64,
                },
            }
        if self.cancel_confirmed:
            return super().cancel_task_authority(
                task_id,
                reason=reason,
                identity=identity,
                abort_agent=abort_agent,
            )
        self.closed.append(task_id)
        return {
            "status": "unavailable",
            "run_id": task_id,
            "reason": reason,
            "authority_revoked": False,
            "plugin_authority_closed": False,
            "agent_abort_confirmed": False,
            "executing_reservations": [],
        }


class AsyncContextPatchOpenClaw(ScriptedOpenClaw):
    def __init__(self) -> None:
        super().__init__()
        self.terminal: dict[str, ExecutionResult] = {}

    @staticmethod
    def _reply(
        request: dict[str, Any],
        *,
        message_type: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "contract_version": request["contract_version"],
            "message_id": expected_agent_reply_message_id(request),
            "message_type": message_type,
            "case_id": request["case_id"],
            "case_revision": request["case_revision"],
            "turn_index": request["turn_index"],
            "task_packet_id": request["task_packet_id"],
            "operation_id": request["operation_id"],
            "scope_digest": request["scope_digest"],
            "collaboration_binding": dict(
                request["collaboration_binding"]
            ),
            "sender": "agent",
            "in_reply_to": request["message_id"],
            "payload": payload,
        }

    @staticmethod
    def _terminal_result(
        packet: VeyraTaskPacket,
        reply: dict[str, Any],
    ) -> ExecutionResult:
        return ExecutionResult(
            task_id=packet.runtime_run_id,
            executor="openclaw",
            status="success",
            result="async bounded proposal",
            tool_calls=[],
            changed_files=[],
            raw={
                "exact_run_observed": True,
                "governed_tool_evidence": {
                    "status": "resolved",
                    "authority_source": "test_private_tool_ledger",
                    "observed_call_count": 0,
                    "effect_count": 0,
                },
                "agent_response": {
                    "dialogue_message": reply,
                },
            },
        )

    def send_task(self, packet: VeyraTaskPacket) -> ExecutionResult:
        self.sent.append(packet)
        request = dict(packet.dialogue_message or {})
        role = str(request["collaboration_binding"]["role"])
        message_type = str(request["message_type"])
        if (
            message_type == "TASK_REQUEST"
            and role == "primary_analyst"
        ):
            reply = self._reply(
                request,
                message_type="EVIDENCE_REQUEST",
                payload={
                    "requested_evidence": [
                        {
                            "request_id": "async_runtime_health",
                            "question": (
                                "Is a fresh runtime observation available?"
                            ),
                            "reason": (
                                "The primary must preserve this unknown."
                            ),
                            "freshness_required": True,
                        }
                    ],
                    "requested_capabilities": [],
                },
            )
            return self._terminal_result(packet, reply)
        if message_type == "CONTEXT_PATCH":
            reply = self._reply(
                request,
                message_type="OPTION_SET",
                payload=options("async_primary"),
            )
            self.terminal[packet.runtime_run_id] = (
                self._terminal_result(packet, reply)
            )
        elif message_type != "TASK_REQUEST" or role != "critic":
            raise AssertionError(
                f"unexpected async Phase 6 turn: {message_type}/{role}"
            )
        return ExecutionResult(
            task_id=packet.runtime_run_id,
            executor="openclaw",
            status="submitted",
            result="submitted",
            raw={"exact_run_observed": True},
        )

    def fetch_bound_task_status(
        self,
        task_id: str,
        *,
        identity: dict[str, Any],
    ) -> ExecutionResult:
        del identity
        return self.terminal.get(
            task_id,
            ExecutionResult(
                task_id=task_id,
                executor="openclaw",
                status="submitted",
                result="still pending",
                raw={"exact_run_observed": True},
            ),
        )


def runtime_for(
    store: WorldStateStore,
    adapter: ScriptedOpenClaw,
    registry: Registry | None = None,
) -> ReadOnlyAgentCollaborationRuntime:
    selected_registry = registry or Registry(adapter)
    cases = DurableCaseStore(store)
    tracker = AgentTaskTracker(store)
    bounded = BoundedNegotiationRuntime(
        state_store=store,
        case_store=cases,
        task_packet_builder=TaskPacketBuilder(store),
        verifier=Verifier(),
        response_synthesizer=ResponseSynthesizer(),
        registry=selected_registry,
        task_tracker=tracker,
    )
    return ReadOnlyAgentCollaborationRuntime(
        state_store=store,
        case_store=cases,
        task_packet_builder=TaskPacketBuilder(store),
        bounded_negotiation=bounded,
        capability_directory=AgentCapabilityDirectory(
            state_store=store,
            registry=selected_registry,
        ),
        task_tracker=tracker,
    )


def main() -> int:
    with TemporaryDirectory(
        prefix="veyra-phase6-corrupt-startup-"
    ) as raw:
        store = WorldStateStore(Path(raw) / "state")
        store.path_for("phase6_collaboration_state.json").write_text(
            "{broken",
            encoding="utf-8",
        )
        adapter = RecoverableOpenClaw()
        runtime = runtime_for(store, adapter)
        status = runtime.status()
        try:
            runtime.list(
                user_id=USER,
                workspace_id=WORKSPACE,
            )
        except CollaborationStorageError:
            operation_fails_closed = True
        else:
            operation_fails_closed = False
        expect(
            status["status"] == "degraded"
            and status["storage"]["status"] == "degraded"
            and status["storage"]["issue"]
            == "collaboration_state_invalid"
            and operation_fails_closed
            and not adapter.sent,
            (
                "corrupt Phase 6 state degrades its own control plane "
                "without blocking runtime construction or dispatching"
            ),
            status,
        )

    with TemporaryDirectory(
        prefix="veyra-phase6-recovery-"
    ) as raw:
        store = WorldStateStore(Path(raw) / "state")
        store.patch_json(
            "local_world.json", {"current_project": WORKSPACE}
        )
        adapter = RecoverableOpenClaw()
        first_runtime = runtime_for(store, adapter)
        started = first_runtime.start(
            event=event(),
            workspace_id=WORKSPACE,
            runtime="openclaw",
            operation_id="phase6-recovery-start",
            context_summary="recovery fixture",
            evidence_refs=[],
        )
        expect(
            started["status"] == "DELIBERATING"
            and len(adapter.sent) == 1
            and started["budgets"]["agent_calls"]["claimed"] == 1,
            "non-terminal primary persists before simulated restart",
            started,
        )

        restarted = runtime_for(store, adapter)
        advanced = restarted.advance(
            case_id=started["case_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
            operation_id="phase6-recover-primary",
        )
        expect(
            advanced["status"] == "DELIBERATING"
            and len(adapter.sent) == 2
            and advanced["budgets"]["participants"]["used"] == 2
            and advanced["participants"][0]["status"] == "responded"
            and advanced["participants"][1]["status"] == "pending",
            "restart recovers exact primary once then dispatches one critic",
            advanced,
        )
        expect(
            advanced["budgets"]["agent_calls"]["claimed"] == 2
            and advanced["budgets"]["handoffs"]["claimed"] == 1,
            "recovery does not duplicate a claimed call or handoff",
            advanced["budgets"],
        )

        cancelled = restarted.cancel(
            case_id=started["case_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
            expected_revision=int(advanced["case_revision"]),
            operation_id="phase6-cancel-critic",
            reason="stop the pending critic",
        )
        expect(
            cancelled["status"] == "CANCELLED"
            and len(adapter.sent) == 2
            and cancelled["effect_status"] == "not_started",
            "confirmed cancellation closes the only live sequential child",
            cancelled,
        )

    with TemporaryDirectory(
        prefix="veyra-phase6-cancel-too-late-"
    ) as raw:
        store = WorldStateStore(Path(raw) / "state")
        store.patch_json(
            "local_world.json", {"current_project": WORKSPACE}
        )
        adapter = RecoverableOpenClaw()
        runtime = runtime_for(store, adapter)
        pending = runtime.start(
            event=event(),
            workspace_id=WORKSPACE,
            runtime="openclaw",
            operation_id="phase6-cancel-too-late-start",
            context_summary="too-late cancellation fixture",
            evidence_refs=[],
        )
        adapter.cancel_too_late = True
        cancelled = runtime.cancel(
            case_id=pending["case_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
            expected_revision=int(pending["case_revision"]),
            operation_id="phase6-cancel-too-late",
            reason="cancel the raced collaboration",
        )
        restarted = runtime_for(store, adapter)
        projected = restarted.get(
            case_id=pending["case_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
        )
        expect(
            cancelled["status"] == "INDETERMINATE"
            and cancelled["effect_status"] == "unknown"
            and cancelled["verification_status"] == "indeterminate"
            and cancelled["execution_authorized"] is False
            and projected["status"] == "INDETERMINATE"
            and projected["effect_status"] == "unknown"
            and projected["verification_status"] == "indeterminate"
            and len(adapter.sent) == 1,
            (
                "too-late cancellation projects durable unknown effects "
                "before and after restart"
            ),
            {
                "cancelled": cancelled,
                "projected": projected,
            },
        )

    with TemporaryDirectory(
        prefix="veyra-phase6-async-context-patch-"
    ) as raw:
        store = WorldStateStore(Path(raw) / "state")
        store.patch_json(
            "local_world.json", {"current_project": WORKSPACE}
        )
        adapter = AsyncContextPatchOpenClaw()
        runtime = runtime_for(store, adapter)
        pending = runtime.start(
            event=event(),
            workspace_id=WORKSPACE,
            runtime="openclaw",
            operation_id="phase6-async-context-patch-start",
            context_summary="async context-patch recovery fixture",
            evidence_refs=[],
        )
        expect(
            pending["status"] == "DELIBERATING"
            and len(adapter.sent) == 2
            and [
                packet.dialogue_message["message_type"]
                for packet in adapter.sent
            ]
            == ["TASK_REQUEST", "CONTEXT_PATCH"],
            "async context patch persists as the only live exact turn",
            pending,
        )
        restarted = runtime_for(store, adapter)
        advanced = restarted.advance(
            case_id=pending["case_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
            operation_id="phase6-async-context-patch-recover",
        )
        polled = restarted.advance(
            case_id=pending["case_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
            operation_id="phase6-async-critic-poll",
        )
        message_types = [
            packet.dialogue_message["message_type"]
            for packet in adapter.sent
        ]
        expect(
            advanced["status"] == "DELIBERATING"
            and polled["status"] == "DELIBERATING"
            and len(adapter.sent) == 3
            and message_types.count("CONTEXT_PATCH") == 1
            and advanced["participants"][0]["status"] == "responded"
            and advanced["participants"][0]["last_reply_type"]
            == "OPTION_SET"
            and advanced["participants"][1]["status"] == "pending"
            and advanced["issue"] is None,
            (
                "restart accepts the exact async context-patch result once "
                "and dispatches one critic without duplicating a turn"
            ),
            {
                "advanced": advanced,
                "polled": polled,
                "message_types": message_types,
            },
        )

    with TemporaryDirectory(
        prefix="veyra-phase6-provider-drift-"
    ) as raw:
        store = WorldStateStore(Path(raw) / "state")
        store.patch_json(
            "local_world.json", {"current_project": WORKSPACE}
        )
        adapter = RecoverableOpenClaw()
        registry = Registry(adapter)
        runtime = runtime_for(store, adapter, registry)
        pending = runtime.start(
            event=event(),
            workspace_id=WORKSPACE,
            runtime="openclaw",
            operation_id="phase6-provider-drift-start",
            context_summary="provider identity drift fixture",
            evidence_refs=[],
        )
        registry.current_config = {
            **registry.current_config,
            "agents": {
                **registry.current_config["agents"],
                "openclaw": {
                    **registry.current_config["agents"]["openclaw"],
                    "model": "changed-model",
                },
            },
        }
        try:
            runtime.advance(
                case_id=pending["case_id"],
                user_id=USER,
                workspace_id=WORKSPACE,
                operation_id="phase6-provider-drift-advance",
            )
        except AgentCapabilitySelectionError as exc:
            provider_drift_reason = exc.reason
        else:
            provider_drift_reason = ""
        unchanged = runtime.get(
            case_id=pending["case_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
        )
        expect(
            provider_drift_reason
            == "collaboration_provider_binding_changed"
            and len(adapter.sent) == 1
            and unchanged["budgets"]["agent_calls"]["claimed"] == 1
            and unchanged["budgets"]["participants"]["used"] == 1,
            (
                "provider/model drift blocks recovery without a second "
                "dispatch or budget claim"
            ),
            {
                "reason": provider_drift_reason,
                "sent": len(adapter.sent),
                "collaboration": unchanged,
            },
        )

    with TemporaryDirectory(
        prefix="veyra-phase6-effect-callback-"
    ) as raw:
        store = WorldStateStore(Path(raw) / "state")
        store.patch_json(
            "local_world.json", {"current_project": WORKSPACE}
        )
        adapter = RecoverableOpenClaw()
        runtime = runtime_for(store, adapter)
        pending = runtime.start(
            event=event(),
            workspace_id=WORKSPACE,
            runtime="openclaw",
            operation_id="phase6-effect-callback-start",
            context_summary="effect callback fence fixture",
            evidence_refs=[],
        )
        run_id = str(adapter.sent[0].runtime_run_id)
        task_context = runtime.task_tracker.get_context(run_id)
        expect(
            isinstance(task_context, dict),
            "pending Phase 6 callback has an exact durable binding",
            task_context,
        )
        adapter.cancel_confirmed = False
        terminal = adapter.terminal[run_id]
        effect_execution = ExecutionResult(
            task_id=terminal.task_id,
            executor=terminal.executor,
            status=terminal.status,
            result=terminal.result,
            tool_calls=["forbidden_phase6_tool"],
            changed_files=[],
            raw=terminal.raw,
        )
        outcome = runtime.bounded_negotiation.accept_registered_execution(
            execution=effect_execution,
            task_context=task_context or {},
        )
        restarted = runtime_for(store, adapter)
        projected = restarted.get(
            case_id=pending["case_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
        )
        raw_case = runtime.case_store.get_case(
            case_id=pending["case_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
        )
        expect(
            outcome["verification"]["status"] == "indeterminate"
            and outcome["dialogue_message"] is None
            and raw_case["status"] == "INDETERMINATE"
            and projected["status"] == "INDETERMINATE"
            and projected["effect_status"] == "unknown"
            and projected["verification_status"] == "indeterminate"
            and len(adapter.sent) == 1,
            (
                "known effects stay fenced despite unconfirmed authority "
                "closure, and restart self-heals the public projection"
            ),
            {
                "outcome": outcome,
                "case": raw_case,
                "collaboration": projected,
            },
        )

    with TemporaryDirectory(
        prefix="veyra-phase6-effect-evidence-unknown-"
    ) as raw:
        store = WorldStateStore(Path(raw) / "state")
        store.patch_json(
            "local_world.json", {"current_project": WORKSPACE}
        )
        adapter = RecoverableOpenClaw()
        runtime = runtime_for(store, adapter)
        pending = runtime.start(
            event=event(),
            workspace_id=WORKSPACE,
            runtime="openclaw",
            operation_id="phase6-effect-evidence-unknown-start",
            context_summary="authoritative effect evidence fixture",
            evidence_refs=[],
        )
        run_id = str(adapter.sent[0].runtime_run_id)
        task_context = runtime.task_tracker.get_context(run_id)
        terminal = adapter.terminal[run_id]
        unresolved_execution = ExecutionResult(
            task_id=terminal.task_id,
            executor=terminal.executor,
            status=terminal.status,
            result=terminal.result,
            tool_calls=[],
            changed_files=[],
            raw={
                **terminal.raw,
                "governed_tool_evidence": {
                    "status": "resolution_failed",
                    "authority_source": "test_private_tool_ledger",
                    "observed_call_count": 0,
                    "effect_count": 0,
                    "reason": "ledger unavailable",
                },
            },
        )
        outcome = runtime.bounded_negotiation.accept_registered_execution(
            execution=unresolved_execution,
            task_context=task_context or {},
        )
        projected = runtime.reconcile_case_projection(
            case_id=pending["case_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
        )
        expect(
            outcome["verification"]["status"] == "indeterminate"
            and outcome["dialogue_message"] is None
            and projected["status"] == "INDETERMINATE"
            and projected["effect_status"] == "unknown"
            and projected["verification_status"] == "indeterminate"
            and len(adapter.sent) == 1,
            (
                "unresolved authoritative effect evidence cannot be "
                "treated as a no-effect proposal"
            ),
            {
                "outcome": outcome,
                "collaboration": projected,
            },
        )

    with TemporaryDirectory(
        prefix="veyra-phase6-agent-reported-effect-"
    ) as raw:
        store = WorldStateStore(Path(raw) / "state")
        store.patch_json(
            "local_world.json", {"current_project": WORKSPACE}
        )
        adapter = RecoverableOpenClaw()
        runtime = runtime_for(store, adapter)
        pending = runtime.start(
            event=event(),
            workspace_id=WORKSPACE,
            runtime="openclaw",
            operation_id="phase6-agent-reported-effect-start",
            context_summary="Agent-reported effect fixture",
            evidence_refs=[],
        )
        run_id = str(adapter.sent[0].runtime_run_id)
        task_context = runtime.task_tracker.get_context(run_id)
        terminal = adapter.terminal[run_id]
        reported_execution = ExecutionResult(
            task_id=terminal.task_id,
            executor=terminal.executor,
            status=terminal.status,
            result=terminal.result,
            tool_calls=[],
            changed_files=[],
            raw={
                **terminal.raw,
                "agent_response": {
                    **terminal.raw["agent_response"],
                    "tool_calls": ["forbidden_phase6_tool"],
                    "changed_files": ["forbidden.txt"],
                },
                "agent_reported_tool_evidence": {
                    "status": "diagnostic_only",
                    "authoritative": False,
                    "tool_call_count": 1,
                    "changed_file_count": 1,
                    "tool_calls_reported": True,
                    "changed_files_reported": True,
                },
            },
        )
        outcome = runtime.bounded_negotiation.accept_registered_execution(
            execution=reported_execution,
            task_context=task_context or {},
        )
        projected = runtime.reconcile_case_projection(
            case_id=pending["case_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
        )
        expect(
            outcome["verification"]["status"] == "indeterminate"
            and outcome["dialogue_message"] is None
            and projected["status"] == "INDETERMINATE"
            and projected["effect_status"] == "unknown"
            and projected["verification_status"] == "indeterminate"
            and len(adapter.sent) == 1,
            (
                "Agent-reported effects cannot advance an otherwise valid "
                "bound dialogue"
            ),
            {
                "outcome": outcome,
                "collaboration": projected,
            },
        )

    with TemporaryDirectory(
        prefix="veyra-phase6-cancel-unconfirmed-"
    ) as raw:
        store = WorldStateStore(Path(raw) / "state")
        store.patch_json(
            "local_world.json", {"current_project": WORKSPACE}
        )
        adapter = RecoverableOpenClaw()
        runtime = runtime_for(store, adapter)
        pending = runtime.start(
            event=event(),
            workspace_id=WORKSPACE,
            runtime="openclaw",
            operation_id="phase6-unconfirmed-start",
            context_summary="unconfirmed cancellation fixture",
            evidence_refs=[],
        )
        adapter.cancel_confirmed = False
        unconfirmed = runtime.cancel(
            case_id=pending["case_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
            expected_revision=int(pending["case_revision"]),
            operation_id="phase6-unconfirmed-cancel",
            reason="first cancellation attempt",
        )
        expect(
            unconfirmed["status"] == "CANCELLING"
            and unconfirmed["execution_authorized"] is False,
            "unconfirmed authority closure stays CANCELLING",
            unconfirmed,
        )
        adapter.cancel_confirmed = True
        replayed = runtime.cancel(
            case_id=pending["case_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
            expected_revision=int(unconfirmed["case_revision"]),
            operation_id="phase6-unconfirmed-cancel",
            reason="first cancellation attempt",
        )
        expect(
            replayed["status"] == "CANCELLED"
            and len(adapter.sent) == 1,
            "same persisted cancellation intent can confirm without redispatch",
            replayed,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
