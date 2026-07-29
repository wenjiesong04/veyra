#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, current_thread
from typing import Any
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.response_synthesizer import ResponseSynthesizer  # noqa: E402
from core.task_packet_builder import (  # noqa: E402
    PHASE6_READ_ONLY_COLLABORATION_PROFILE,
    TaskPacketBuilder,
)
from core.verifier import Verifier  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402
from interface.agent_adapter import AgentAdapter, ExecutionResult  # noqa: E402
from interface.agent_contract import (  # noqa: E402
    validate_task_packet_payload,
)
from interface.agent_dialogue_contract import (  # noqa: E402
    expected_agent_reply_message_id,
)
from interface.event_schema import (  # noqa: E402
    EventSource,
    EventType,
    VeyraEvent,
    VeyraTaskPacket,
)
from runtime.agent_capability_directory import (  # noqa: E402
    AgentCapabilityDirectory,
)
from runtime.agent_task_tracker import AgentTaskTracker  # noqa: E402
from runtime.bounded_agent_negotiation import (  # noqa: E402
    BoundedNegotiationRuntime,
)
from runtime.durable_case_store import (  # noqa: E402
    CaseOperationConflictError,
    DurableCaseStore,
)
from runtime.read_only_agent_collaboration import (  # noqa: E402
    CollaborationNotFoundError,
    ReadOnlyAgentCollaborationRuntime,
)


WORKSPACE = "workspace-phase6-collaboration"
USER = "user-phase6-collaboration"


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"PASS {label}")


def options(prefix: str) -> dict[str, Any]:
    return {
        "options": [
            {
                "option_id": f"{prefix}_observe",
                "summary": "Keep the case read-only and collect evidence.",
                "assumptions": ["No execution authority exists."],
                "expected_outcome": "A safer evidence-backed decision.",
                "costs": ["One later Veyra probe."],
                "risks": ["The decision is delayed."],
                "evidence_refs": [],
                "required_capabilities": [],
            },
            {
                "option_id": f"{prefix}_pause",
                "summary": "Pause without changing any system.",
                "assumptions": ["The missing fact may matter."],
                "expected_outcome": "No unsupported action is taken.",
                "costs": ["Human review is needed."],
                "risks": ["Resolution takes longer."],
                "evidence_refs": [],
                "required_capabilities": [],
            },
        ],
        "recommended_option_id": f"{prefix}_observe",
    }


class ScriptedOpenClaw(AgentAdapter):
    trusted_native_provider_adapter = True

    def __init__(self) -> None:
        self.sent: list[VeyraTaskPacket] = []
        self.closed: list[str] = []

    def connection_status(
        self, *, force_refresh: bool = False
    ) -> dict[str, Any]:
        del force_refresh
        features = {
            "structured_task_packet": True,
            "rendered_prompt_fallback": True,
            "task_status": True,
            "stop_task": True,
            "agent_dialogue_v1": True,
            "caller_supplied_run_id": True,
            "idempotent_submit": True,
            "exact_stop": True,
            "tool_proxy_enforced": True,
            "tool_proxy_identity_match": True,
            "tool_proxy_enforcement_scope": (
                "veyra_governed_openclaw_sessions"
            ),
            "governance_callbacks_complete": True,
            "enforced_execution_profile": "phase3_sandbox_proposal",
            "enforced_execution_profiles": [
                "phase3_sandbox_proposal",
                PHASE6_READ_ONLY_COLLABORATION_PROFILE,
            ],
        }
        return {
            "name": "openclaw",
            "status": "available",
            "connected": True,
            "features": features,
            "provider_certification": {
                "certification_status": "validated",
                "validated": True,
                "observed_at": "2026-07-29T00:00:00+00:00",
                "freshness": {"status": "fresh", "age_seconds": 0},
                "issues": [],
            },
        }

    def send_task(self, packet: VeyraTaskPacket) -> ExecutionResult:
        validation_errors = validate_task_packet_payload(packet.to_dict())
        if validation_errors:
            raise AssertionError(
                "production TaskPacket validator rejected Phase 6 packet: "
                + "; ".join(validation_errors)
            )
        self.sent.append(packet)
        request = dict(packet.dialogue_message or {})
        binding = dict(request["collaboration_binding"])
        role = str(binding["role"])
        message_type = str(request["message_type"])
        if message_type == "TASK_REQUEST" and role == "primary_analyst":
            reply_type = "EVIDENCE_REQUEST"
            payload = {
                "requested_evidence": [
                    {
                        "request_id": "need_runtime_health",
                        "question": "Is the runtime currently healthy?",
                        "reason": "The claim needs a fresh Veyra observation.",
                        "freshness_required": True,
                    }
                ],
                "requested_capabilities": [],
            }
        elif message_type == "CONTEXT_PATCH":
            reply_type = "OPTION_SET"
            payload = options("primary")
        elif message_type == "TASK_REQUEST" and role == "critic":
            reply_type = "OPTION_SET"
            payload = options("critic")
        else:
            raise AssertionError(
                f"unexpected Phase 6 dialogue: {message_type}/{role}"
            )
        reply = {
            "contract_version": request["contract_version"],
            "message_id": expected_agent_reply_message_id(request),
            "message_type": reply_type,
            "case_id": request["case_id"],
            "case_revision": request["case_revision"],
            "turn_index": request["turn_index"],
            "task_packet_id": request["task_packet_id"],
            "operation_id": request["operation_id"],
            "scope_digest": request["scope_digest"],
            "collaboration_binding": binding,
            "sender": "agent",
            "in_reply_to": request["message_id"],
            "payload": payload,
        }
        return ExecutionResult(
            task_id=packet.runtime_run_id,
            executor="openclaw",
            status="success",
            result="bounded proposal",
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

    def cancel_task_authority(
        self,
        task_id: str,
        *,
        reason: str = "user_requested_stop",
        identity: dict[str, Any] | None = None,
        abort_agent: bool = True,
    ) -> dict[str, Any]:
        del identity, abort_agent
        self.closed.append(task_id)
        return {
            "status": "cancelled",
            "run_id": task_id,
            "reason": reason,
            "authority_revoked": True,
            "plugin_authority_closed": True,
            "agent_abort_confirmed": True,
            "executing_reservations": [],
            "revoked_grants": [],
            "cancelled_reservations": [],
            "identity": {
                "binding_digest": "a" * 64,
            },
        }


class Registry:
    def __init__(self, adapter: ScriptedOpenClaw) -> None:
        self.adapter = adapter
        self.current_config = {
            "selected_agent": "openclaw",
            "agents": {
                "openclaw": {
                    "kind": "openclaw",
                    "enabled": True,
                    "base_url": "ws://127.0.0.1:18789",
                }
            },
        }

    def config(self) -> dict[str, Any]:
        return self.current_config

    def names(self) -> list[str]:
        return ["openclaw"]

    def get(self, name: str) -> ScriptedOpenClaw:
        if name != "openclaw":
            raise KeyError(name)
        return self.adapter


def build_runtime(
    root: Path,
) -> tuple[
    ReadOnlyAgentCollaborationRuntime,
    ScriptedOpenClaw,
    WorldStateStore,
]:
    store = WorldStateStore(root)
    store.patch_json(
        "local_world.json", {"current_project": WORKSPACE}
    )
    adapter = ScriptedOpenClaw()
    registry = Registry(adapter)
    cases = DurableCaseStore(store)
    tracker = AgentTaskTracker(store)
    bounded = BoundedNegotiationRuntime(
        state_store=store,
        case_store=cases,
        task_packet_builder=TaskPacketBuilder(store),
        verifier=Verifier(),
        response_synthesizer=ResponseSynthesizer(),
        registry=registry,
        task_tracker=tracker,
    )
    runtime = ReadOnlyAgentCollaborationRuntime(
        state_store=store,
        case_store=cases,
        task_packet_builder=TaskPacketBuilder(store),
        bounded_negotiation=bounded,
        capability_directory=AgentCapabilityDirectory(
            state_store=store,
            registry=registry,
        ),
        task_tracker=tracker,
    )
    return runtime, adapter, store


def event() -> VeyraEvent:
    return VeyraEvent(
        type=EventType.USER_MESSAGE,
        source=EventSource(
            channel="api",
            user_id=USER,
            session_id="phase6-collaboration-session",
        ),
        payload={
            "text": (
                "Compare safe ways to handle an uncertain runtime health "
                "signal without changing anything."
            )
        },
        event_id="phase6-collaboration-event",
    )


def contains_key(value: Any, forbidden: set[str]) -> bool:
    if isinstance(value, dict):
        return bool(set(value).intersection(forbidden)) or any(
            contains_key(item, forbidden) for item in value.values()
        )
    if isinstance(value, list):
        return any(contains_key(item, forbidden) for item in value)
    return False


def main() -> int:
    with TemporaryDirectory(
        prefix="veyra-phase6-collaboration-"
    ) as raw:
        runtime, adapter, store = build_runtime(Path(raw) / "state")
        result = runtime.start(
            event=event(),
            workspace_id=WORKSPACE,
            runtime="openclaw",
            operation_id="phase6-collaboration-start",
            context_summary=(
                "A prior diagnostic is stale and is not authoritative."
            ),
            evidence_refs=[],
        )
        expect(
            result["status"] == "PROPOSED"
            and len(adapter.sent) == 3
            and len(adapter.closed) == 3,
            "primary, unresolved context patch, and critic run sequentially",
            result,
        )
        expect(
            result["budgets"]["agent_calls"] == {
                "claimed": 3,
                "max": 3,
            }
            and result["budgets"]["participants"] == {
                "used": 2,
                "max": 2,
            }
            and result["budgets"]["handoffs"] == {
                "claimed": 1,
                "max": 1,
            }
            and result["budgets"]["evidence_patches"] == {
                "claimed": 1,
                "max": 1,
            },
            "all collaboration budgets are claimed before dispatch",
            result["budgets"],
        )
        expect(
            [item["role"] for item in result["participants"]]
            == ["primary_analyst", "critic"]
            and result["participants"][1]["parent_participant_id"]
            == result["participants"][0]["participant_id"],
            "critic inherits exact parent lineage",
            result["participants"],
        )
        expect(
            len(
                {
                    packet.agent_execution_session_id
                    for packet in adapter.sent
                }
            )
            == 3
            and all(
                packet.memory_policy == "forget"
                and packet.required_capabilities == []
                and packet.persona_patch == {}
                and packet.policy_patch == {}
                and packet.verification_policy == {}
                and packet.rollback_requirement == {}
                and packet.governance_context["execution_profile"]
                == PHASE6_READ_ONLY_COLLABORATION_PROFILE
                for packet in adapter.sent
            ),
            (
                "each turn is canonical, isolated, and uses the private "
                "no-tool profile"
            ),
            [packet.to_dict() for packet in adapter.sent],
        )
        expect(
            adapter.sent[0].context_patch
            == adapter.sent[0].dialogue_message["payload"]["context"]
            and adapter.sent[0].user_goal
            == adapter.sent[0].dialogue_message["payload"]["user_goal"]
            and all(
                packet.context_patch
                == packet.dialogue_message["payload"].get("context", {})
                for packet in adapter.sent
            )
            and all(
                (
                    packet.user_goal
                    == packet.dialogue_message["payload"]["user_goal"]
                    if packet.dialogue_message["message_type"]
                    == "TASK_REQUEST"
                    else packet.user_goal == ""
                )
                for packet in adapter.sent
            ),
            "outer packet cannot introduce context or goal outside dialogue",
            [packet.to_dict() for packet in adapter.sent],
        )
        context_patch = adapter.sent[1].dialogue_message or {}
        expect(
            context_patch["message_type"] == "CONTEXT_PATCH"
            and context_patch["payload"]["resolved_request_ids"] == []
            and context_patch["payload"]["evidence_refs"] == []
            and context_patch["payload"]["unresolved_request_ids"]
            == ["need_runtime_health"],
            "missing evidence stays explicitly unresolved",
            context_patch,
        )
        expect(
            result["effect_status"] == "not_started"
            and result["verification_status"] == "unverified"
            and result["execution_authorized"] is False,
            "proposals never become effects, verification, or authority",
            result,
        )
        expect(
            not contains_key(
                result,
                {
                    "runtime_run_id",
                    "session_key",
                    "binding_digest",
                    "dispatch_token",
                    "raw",
                    "logs",
                },
            ),
            "public projection hides private dispatch identity and raw data",
            result,
        )

        selected = runtime.select_plan(
            case_id=result["case_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
            expected_revision=int(result["case_revision"]),
            operation_id="phase6-human-plan-selection",
            selected_option_id="critic_observe",
            decision_reason=(
                "Use the conservative read-only proposal; do not execute it."
            ),
        )
        expect(
            selected["status"] == "CLOSED"
            and selected["selection"]["selected_option_id"]
            == "critic_observe"
            and selected["selection"]["execution_authorized"] is False
            and len(adapter.sent) == 3,
            "human plan selection closes without another dispatch or effect",
            selected,
        )
        selected_replay = runtime.select_plan(
            case_id=result["case_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
            expected_revision=int(result["case_revision"]),
            operation_id="phase6-human-plan-selection",
            selected_option_id="critic_observe",
            decision_reason=(
                "Use the conservative read-only proposal; do not execute it."
            ),
        )
        expect(
            selected_replay["status"] == "CLOSED"
            and selected_replay["operation_replayed"] is True
            and selected_replay["case_revision"]
            == selected["case_revision"]
            and selected_replay["selection"] == selected["selection"]
            and len(adapter.sent) == 3,
            "identical plan-selection retry is stable and dispatch-free",
            selected_replay,
        )
        try:
            runtime.select_plan(
                case_id=result["case_id"],
                user_id=USER,
                workspace_id=WORKSPACE,
                expected_revision=int(result["case_revision"]),
                operation_id="phase6-human-plan-selection",
                selected_option_id="critic_observe",
                decision_reason=(
                    "Reuse the operation identity with different semantics."
                ),
            )
        except CaseOperationConflictError:
            conflicting_selection_blocked = True
        else:
            conflicting_selection_blocked = False
        expect(
            conflicting_selection_blocked
            and len(adapter.sent) == 3,
            "plan-selection operation identity rejects semantic rebinding",
        )

        replay = runtime.start(
            event=event(),
            workspace_id=WORKSPACE,
            runtime="openclaw",
            operation_id="phase6-collaboration-start",
            context_summary=(
                "A prior diagnostic is stale and is not authoritative."
            ),
            evidence_refs=[],
        )
        expect(
            replay["operation_replayed"] is True
            and len(adapter.sent) == 3,
            "at-least-once start replay never redispatches",
            replay,
        )
        try:
            runtime.get(
                case_id=result["case_id"],
                user_id="another-user",
                workspace_id=WORKSPACE,
            )
        except CollaborationNotFoundError:
            wrong_owner_blocked = True
        else:
            wrong_owner_blocked = False
        expect(
            wrong_owner_blocked,
            "cross-owner collaboration read is not found",
        )

        state = store.read_json("phase6_collaboration_state.json")
        expect(
            state["collaboration_count"] == 1
            and not any(
                "tool" in json.dumps(item).lower()
                and item.get("status") == "effect_started"
                for item in state["collaborations"].values()
                if isinstance(item, dict)
            ),
            "durable graph remains bounded and records no started effect",
            state,
        )

    with TemporaryDirectory(
        prefix="veyra-phase6-root-crash-replay-"
    ) as raw:
        runtime, adapter, store = build_runtime(Path(raw) / "state")
        original_prepare = runtime._prepare_task_turn
        crashed = False

        def crash_before_first_turn(**kwargs: Any) -> dict[str, Any]:
            nonlocal crashed
            if not crashed:
                crashed = True
                raise RuntimeError(
                    "simulated crash after graph persistence"
                )
            return original_prepare(**kwargs)

        runtime._prepare_task_turn = crash_before_first_turn  # type: ignore[method-assign]
        try:
            runtime.start(
                event=event(),
                workspace_id=WORKSPACE,
                runtime="openclaw",
                operation_id="phase6-root-crash-replay",
                context_summary="crash replay fixture",
                evidence_refs=[],
            )
        except RuntimeError:
            print("PASS simulated crash leaves a resumable root")
        else:
            raise AssertionError("simulated root crash was not raised")
        runtime._prepare_task_turn = original_prepare  # type: ignore[method-assign]
        replayed = runtime.start(
            event=event(),
            workspace_id=WORKSPACE,
            runtime="openclaw",
            operation_id="phase6-root-crash-replay",
            context_summary="crash replay fixture",
            evidence_refs=[],
        )
        crash_state = store.read_json(
            "phase6_collaboration_state.json"
        )
        expect(
            replayed["operation_replayed"] is True
            and replayed["status"] == "PROPOSED"
            and len(adapter.sent) == 3
            and crash_state["collaboration_count"] == 1,
            (
                "same start operation resumes a graph persisted before "
                "the first turn without duplicating the Case"
            ),
            {
                "collaboration": replayed,
                "state": crash_state,
            },
        )

    with TemporaryDirectory(
        prefix="veyra-phase6-root-crash-advance-"
    ) as raw:
        runtime, adapter, store = build_runtime(Path(raw) / "state")
        original_prepare = runtime._prepare_task_turn

        def always_crash_before_first_turn(
            **_kwargs: Any,
        ) -> dict[str, Any]:
            raise RuntimeError(
                "simulated crash before first prepared turn"
            )

        runtime._prepare_task_turn = always_crash_before_first_turn  # type: ignore[method-assign]
        try:
            runtime.start(
                event=event(),
                workspace_id=WORKSPACE,
                runtime="openclaw",
                operation_id="phase6-root-crash-advance",
                context_summary="crash advance fixture",
                evidence_refs=[],
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("simulated advance crash was not raised")
        runtime._prepare_task_turn = original_prepare  # type: ignore[method-assign]
        crash_state = store.read_json(
            "phase6_collaboration_state.json"
        )
        case_id = next(iter(crash_state["collaborations"]))
        advanced = runtime.advance(
            case_id=case_id,
            user_id=USER,
            workspace_id=WORKSPACE,
            operation_id="phase6-root-crash-explicit-advance",
        )
        expect(
            advanced["status"] == "PROPOSED"
            and len(adapter.sent) == 3
            and crash_state["collaboration_count"] == 1,
            (
                "explicit advance resumes a graph persisted before the "
                "first turn"
            ),
            advanced,
        )

    with TemporaryDirectory(
        prefix="veyra-phase6-selection-recovery-"
    ) as raw:
        runtime, adapter, _ = build_runtime(Path(raw) / "state")
        proposed = runtime.start(
            event=event(),
            workspace_id=WORKSPACE,
            runtime="openclaw",
            operation_id="phase6-collaboration-start",
            context_summary=(
                "A prior diagnostic is stale and is not authoritative."
            ),
            evidence_refs=[],
        )
        selection_kwargs = {
            "case_id": proposed["case_id"],
            "user_id": USER,
            "workspace_id": WORKSPACE,
            "expected_revision": int(proposed["case_revision"]),
            "operation_id": "phase6-selection-crash-recovery",
            "selected_option_id": "critic_observe",
            "decision_reason": (
                "Persist the conservative proposal without executing it."
            ),
        }
        try:
            with patch.object(
                runtime,
                "_update_graph",
                side_effect=RuntimeError(
                    "simulated crash after atomic Case selection"
                ),
            ):
                runtime.select_plan(**selection_kwargs)
        except RuntimeError:
            simulated_selection_crash = True
        else:
            simulated_selection_crash = False
        durable_after_crash = runtime.case_store.get_case(
            case_id=proposed["case_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
        )
        recovered_selection = runtime.select_plan(**selection_kwargs)
        expect(
            simulated_selection_crash
            and durable_after_crash["status"] == "CLOSED"
            and recovered_selection["status"] == "CLOSED"
            and recovered_selection["operation_replayed"] is True
            and recovered_selection["selection"]["selected_option_id"]
            == "critic_observe"
            and len(adapter.sent) == 3,
            (
                "retry repairs projection after a crash following the "
                "atomic selection without dispatch or effect"
            ),
            {
                "case": durable_after_crash,
                "collaboration": recovered_selection,
            },
        )

    with TemporaryDirectory(
        prefix="veyra-phase6-selection-concurrency-"
    ) as raw:
        runtime, adapter, _ = build_runtime(Path(raw) / "state")
        proposed = runtime.start(
            event=event(),
            workspace_id=WORKSPACE,
            runtime="openclaw",
            operation_id="phase6-concurrent-selection-start",
            context_summary="concurrent exact selection fixture",
            evidence_refs=[],
        )
        selection_kwargs = {
            "case_id": proposed["case_id"],
            "user_id": USER,
            "workspace_id": WORKSPACE,
            "expected_revision": int(proposed["case_revision"]),
            "operation_id": "phase6-concurrent-selection",
            "selected_option_id": "critic_observe",
            "decision_reason": (
                "Select one read-only proposal without execution."
            ),
        }
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    lambda _index: runtime.select_plan(
                        **selection_kwargs
                    ),
                    range(2),
                )
            )
        durable = runtime.case_store.get_case(
            case_id=proposed["case_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
        )
        selection_records = [
            item
            for item in durable["dialogue"]
            if item["message_type"] == "PLAN_SELECTION"
        ]
        expect(
            all(item["status"] == "CLOSED" for item in results)
            and sorted(
                item["operation_replayed"] for item in results
            )
            == [False, True]
            and len(selection_records) == 1
            and durable["revision"]
            == int(proposed["case_revision"]) + 1
            and len(adapter.sent) == 3,
            (
                "concurrent exact plan selections commit once and replay "
                "once without dispatch"
            ),
            {
                "results": results,
                "case": durable,
            },
        )

    with TemporaryDirectory(
        prefix="veyra-phase6-stale-projection-race-"
    ) as raw:
        runtime, adapter, store = build_runtime(Path(raw) / "state")
        proposed = runtime.start(
            event=event(),
            workspace_id=WORKSPACE,
            runtime="openclaw",
            operation_id="phase6-stale-projection-start",
            context_summary="stale graph writer race fixture",
            evidence_refs=[],
        )
        runtime._update_graph(
            proposed["case_id"],
            lambda value: value.update(
                {
                    "status": "DELIBERATING",
                    "case_revision": int(
                        proposed["case_revision"]
                    )
                    - 1,
                    "selection": None,
                }
            ),
        )
        original_update_graph = runtime._update_graph
        stale_update_waiting = Event()
        release_stale_update = Event()
        delayed_once = False

        def delay_stale_reader_update(
            selected_case_id: str,
            updater: Any,
        ) -> None:
            nonlocal delayed_once
            if (
                current_thread().name.startswith(
                    "phase6-stale-reader"
                )
                and not delayed_once
            ):
                delayed_once = True
                stale_update_waiting.set()
                if not release_stale_update.wait(timeout=5):
                    raise AssertionError(
                        "stale projection race gate timed out"
                    )
            original_update_graph(selected_case_id, updater)

        runtime._update_graph = delay_stale_reader_update  # type: ignore[method-assign]
        with ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="phase6-stale-reader",
        ) as executor:
            stale_read = executor.submit(
                runtime.get,
                case_id=proposed["case_id"],
                user_id=USER,
                workspace_id=WORKSPACE,
            )
            if not stale_update_waiting.wait(timeout=5):
                raise AssertionError(
                    "stale projection reader did not reach the race gate"
                )
            try:
                selected = runtime.select_plan(
                    case_id=proposed["case_id"],
                    user_id=USER,
                    workspace_id=WORKSPACE,
                    expected_revision=int(
                        proposed["case_revision"]
                    ),
                    operation_id=(
                        "phase6-stale-projection-selection"
                    ),
                    selected_option_id="critic_observe",
                    decision_reason=(
                        "Commit the read-only proposal atomically."
                    ),
                )
            finally:
                release_stale_update.set()
            stale_result = stale_read.result(timeout=5)
        runtime._update_graph = original_update_graph  # type: ignore[method-assign]
        persisted_graph = store.read_json(
            "phase6_collaboration_state.json"
        )["collaborations"][proposed["case_id"]]
        durable = runtime.case_store.get_case(
            case_id=proposed["case_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
        )
        expect(
            selected["status"] == "CLOSED"
            and stale_result["status"] == "CLOSED"
            and stale_result["case"]["status"] == "CLOSED"
            and stale_result["case_revision"]
            == stale_result["case"]["revision"]
            and stale_result["selection"]["selected_option_id"]
            == "critic_observe"
            and persisted_graph["status"] == "CLOSED"
            and persisted_graph["case_revision"]
            == durable["revision"]
            and persisted_graph["selection"]["selected_option_id"]
            == "critic_observe"
            and len(adapter.sent) == 3,
            (
                "a stale projection writer cannot regress an atomically "
                "newer Case or produce a contradictory public snapshot"
            ),
            {
                "selected": selected,
                "stale_result": stale_result,
                "graph": persisted_graph,
                "case": durable,
            },
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
