#!/usr/bin/env python3
from __future__ import annotations

import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.task_packet_builder import (  # noqa: E402
    PHASE6_READ_ONLY_COLLABORATION_PROFILE,
    TaskPacketBuilder,
)
from core.world_state import WorldStateStore  # noqa: E402
from interface.agent_contract import (  # noqa: E402
    validate_task_packet_payload,
)
from interface.agent_dialogue_contract import (  # noqa: E402
    build_collaboration_binding,
    build_task_request,
    collaboration_turn_transport_identity,
    scope_digest,
)
from interface.event_schema import (  # noqa: E402
    EventSource,
    EventType,
    VeyraEvent,
)
from runtime.openclaw_tool_broker import (  # noqa: E402
    CUSTOM_TOOL_REGISTRY,
    OpenClawHookDenied,
    OpenClawToolBroker,
)
from runtime.tool_governance_runtime import (  # noqa: E402
    ToolGovernanceRuntime,
)


def expect(condition: bool, label: str, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail!r}")
    print(f"ok - {label}")


def expect_dispatch_denied(
    broker: OpenClawToolBroker,
    packet: object,
    *,
    run_id: str,
    session_key: str,
    label: str,
) -> None:
    try:
        broker.prepare_dispatch(  # type: ignore[arg-type]
            packet,
            run_id=run_id,
            session_key=session_key,
        )
    except (OpenClawHookDenied, ValueError):
        print(f"ok - {label}")
        return
    raise AssertionError(f"{label} failed: dispatch was registered")


def openclaw_runtime_session(contract_session_key: str) -> str:
    return f"agent:main:{contract_session_key}"


def main() -> None:
    with TemporaryDirectory(prefix="veyra-phase6-no-tool-") as raw_root:
        temp_root = Path(raw_root)
        store = WorldStateStore(temp_root / "state")
        workspace_id = "workspace-phase6-no-tool"
        store.patch_json(
            "local_world.json",
            {"current_project": workspace_id},
        )
        clock_now = datetime.now(timezone.utc)
        clock_state = [clock_now]
        clock = lambda: clock_state[0]
        broker = OpenClawToolBroker(
            store,
            ToolGovernanceRuntime(store, clock=clock),
            sandbox_base=temp_root / "sandboxes",
            clock=clock,
        )
        event = VeyraEvent(
            type=EventType.USER_MESSAGE,
            source=EventSource(
                channel="api",
                user_id="phase6-user",
                session_id="phase6-dialogue",
            ),
            payload={
                "text": "Compare two specialist analyses without tools."
            },
            event_id="evt_phase6_no_tool",
        )
        builder = TaskPacketBuilder(store)
        run_id = "phase6-run-no-tools"
        issued_at = clock()
        collaboration_binding = build_collaboration_binding(
            participant_id="participant-phase6-no-tool",
            role="primary_analyst",
            parent_participant_id=None,
            handoff_index=0,
            capability_scope=[],
            evidence_scope=[],
            provider={
                "runtime": "openclaw",
                "provider": "moonshot",
                "model": "kimi",
                "instance_id": "openclaw-main",
                "automatic_switch_allowed": False,
            },
            budget={
                "remaining_agent_calls": 3,
                "remaining_handoffs": 1,
                "remaining_evidence_patches": 1,
                "max_wall_time_seconds": 300,
                "max_context_bytes": 8 * 1024,
                "max_output_bytes": 24 * 1024,
            },
            issued_at=issued_at,
            expires_at=issued_at + timedelta(seconds=300),
        )
        dialogue_context = {
            "collaboration_mode": "read_only_proposal_only",
            "purpose": "validate exact no-tool dispatch",
        }
        dialogue_message = build_task_request(
            case_id="case_phase6_no_tools",
            case_revision=0,
            turn_index=0,
            message_id="msg_phase6_no_tools",
            task_packet_id="task_phase6_no_tools",
            operation_id="op_phase6_no_tools",
            scope_digest=scope_digest(
                user_id="phase6-user",
                workspace_id=workspace_id,
            ),
            user_goal=str(event.payload["text"]),
            constraints=["do not call tools"],
            evidence_refs=[],
            context=dialogue_context,
            collaboration_binding=collaboration_binding,
        )
        session_key = collaboration_turn_transport_identity(
            dialogue_message
        )["agent_execution_session_id"]
        runtime_session_key = openclaw_runtime_session(session_key)
        packet = builder.build(
            event=event,
            target_agent="openclaw",
            context_patch=dialogue_context,
            persona_patch={},
            policy_patch={},
            task_id="task_phase6_no_tools",
            case_id="case_phase6_no_tools",
            step_id="step_phase6_no_tools",
            runtime_run_id=run_id,
            agent_execution_session_id=session_key,
            dialogue_message=dialogue_message,
            execution_profile=PHASE6_READ_ONLY_COLLABORATION_PROFILE,
        )
        build_args = {
            "event": event,
            "target_agent": "openclaw",
            "context_patch": dialogue_context,
            "persona_patch": {},
            "policy_patch": {},
            "task_id": "task_phase6_no_tools",
            "case_id": "case_phase6_no_tools",
            "step_id": "step_phase6_no_tools",
            "runtime_run_id": run_id,
            "agent_execution_session_id": session_key,
            "dialogue_message": dialogue_message,
            "execution_profile": (
                PHASE6_READ_ONLY_COLLABORATION_PROFILE
            ),
        }
        builder_rejections: list[str] = []
        other_user_event = VeyraEvent(
            type=EventType.USER_MESSAGE,
            source=EventSource(
                channel="api",
                user_id="other-phase6-user",
                session_id="other-phase6-dialogue",
            ),
            payload={"text": event.payload["text"]},
            event_id="evt_phase6_other_user",
        )
        for label, override in (
            ("owner", {"event": other_user_event}),
            ("case", {"case_id": "case_other_owner"}),
            (
                "session",
                {"agent_execution_session_id": "agent:openclaw:main"},
            ),
            (
                "session_policy",
                {"agent_session_policy": "persistent"},
            ),
        ):
            candidate_args = {**build_args, **override}
            try:
                builder.build(**candidate_args)
            except ValueError:
                builder_rejections.append(label)
        expect(
            builder_rejections
            == ["owner", "case", "session", "session_policy"],
            (
                "builder binds owner, Case, and one ephemeral Agent session "
                "before dispatch"
            ),
            builder_rejections,
        )
        expect(
            validate_task_packet_payload(packet.to_dict()) == [],
            "production TaskPacket validator accepts the canonical packet",
            validate_task_packet_payload(packet.to_dict()),
        )
        expect(
            packet.governance_context.get("execution_profile")
            == PHASE6_READ_ONLY_COLLABORATION_PROFILE,
            "builder binds the Phase 6 profile in private governance context",
            packet.governance_context,
        )
        expect(
            "governance_context" not in packet.to_dict()
            and PHASE6_READ_ONLY_COLLABORATION_PROFILE
            not in str(packet.to_dict()),
            "the trusted execution profile is not Agent-visible",
            packet.to_dict(),
        )

        registration = broker.prepare_dispatch(
            packet,
            run_id=run_id,
            session_key=runtime_session_key,
        )
        hook_state = store.read_json("openclaw_tool_hook_state.json")
        dispatch = hook_state["dispatches"][run_id]
        expect(
            registration.allowed_tools == ()
            and registration.plugin_payload()["allowedTools"] == []
            and dispatch["allowed_tools"] == []
            and dispatch["execution_profile"]
            == PHASE6_READ_ONLY_COLLABORATION_PROFILE,
            "Phase 6 dispatch exposes an exact empty tool allowlist",
            {
                "registration": registration,
                "dispatch": dispatch,
            },
        )
        binding_expires_at = datetime.fromisoformat(
            str(collaboration_binding["expires_at"]).replace(
                "Z", "+00:00"
            )
        )
        expect(
            registration.expires_at
            == binding_expires_at,
            "dispatch authority cannot outlive the collaboration binding",
            {
                "dispatch_expires_at": registration.expires_at,
                "binding_expires_at": binding_expires_at,
            },
        )

        denied = broker.preflight(
            run_id=run_id,
            session_key=runtime_session_key,
            tool_call_id="phase6-call-read",
            tool_name="veyra_file_read",
            params={"path": "must-not-read.txt"},
            dispatch_token=registration.dispatch_token,
        )
        governance_state = store.read_json("tool_governance_state.json")
        expect(
            denied["allow"] is False
            and "not allowed" in str(denied["reason"])
            and governance_state["grants"] == {}
            and governance_state["calls"] == {},
            "broker rejects a known tool before creating grant or call authority",
            {
                "decision": denied,
                "governance": governance_state,
            },
        )
        first_observation = broker.observe(
            run_id=run_id,
            tool_call_id="phase6-call-read",
            tool_name="veyra_file_read",
            dispatch_token=registration.dispatch_token,
            outcome="blocked",
            duration_ms=0,
            reason=str(denied["reason"]),
        )
        clock_state[0] = clock_now + timedelta(seconds=1)
        replayed_observation = broker.observe(
            run_id=run_id,
            tool_call_id="phase6-call-read",
            tool_name="veyra_file_read",
            dispatch_token=registration.dispatch_token,
            outcome="blocked",
            duration_ms=0,
            reason=str(denied["reason"]),
        )
        observation_metrics = store.read_json(
            "openclaw_tool_hook_state.json"
        )["metrics"]
        expect(
            first_observation["replayed"] is False
            and replayed_observation["replayed"] is True
            and observation_metrics["plugin_observations"] == 1,
            (
                "identical blocked plugin observations replay without a "
                "timestamp contradiction"
            ),
            {
                "first": first_observation,
                "replayed": replayed_observation,
                "metrics": observation_metrics,
            },
        )

        def tamper_allowlist(document: dict[str, object]) -> None:
            dispatches = document["dispatches"]
            assert isinstance(dispatches, dict)
            stored = dispatches[run_id]
            assert isinstance(stored, dict)
            stored["allowed_tools"] = sorted(CUSTOM_TOOL_REGISTRY)

        store.mutate_json(
            "openclaw_tool_hook_state.json",
            tamper_allowlist,
        )
        tampered = broker.preflight(
            run_id=run_id,
            session_key=runtime_session_key,
            tool_call_id="phase6-call-tampered",
            tool_name="veyra_file_read",
            params={"path": "must-not-read.txt"},
            dispatch_token=registration.dispatch_token,
        )
        expect(
            tampered["allow"] is False
            and "cannot allow tools" in str(tampered["reason"]),
            "broker rejects a widened persisted Phase 6 allowlist",
            tampered,
        )
        degraded_status = broker.status()
        expect(
            degraded_status["status"] == "degraded"
            and degraded_status["execution_authority_enabled"] is False,
            (
                "broker health fails closed for a tampered Phase 6 "
                "dispatch profile"
            ),
            degraded_status,
        )

        def restore_allowlist(document: dict[str, object]) -> None:
            dispatches = document["dispatches"]
            assert isinstance(dispatches, dict)
            stored = dispatches[run_id]
            assert isinstance(stored, dict)
            stored["allowed_tools"] = []

        store.mutate_json(
            "openclaw_tool_hook_state.json",
            restore_allowlist,
        )

        strict_cases: list[tuple[str, object, str, str]] = []
        for field in (
            "user_id",
            "workspace_id",
            "channel_id",
            "case_id",
            "step_id",
        ):
            context = dict(packet.governance_context)
            context[field] = ""
            strict_cases.append(
                (
                    f"missing {field}",
                    replace(
                        packet,
                        governance_context=context,
                        runtime_run_id=f"phase6-run-missing-{field}",
                        agent_execution_session_id=(
                            f"agent-exec:phase6-missing-{field}"
                        ),
                    ),
                    f"phase6-run-missing-{field}",
                    f"agent-exec:phase6-missing-{field}",
                )
            )
        wrong_owner_context = dict(packet.governance_context)
        wrong_owner_context["user_id"] = "other-phase6-user"
        strict_cases.append(
            (
                "mismatched owner scope",
                replace(
                    packet,
                    governance_context=wrong_owner_context,
                    runtime_run_id="phase6-run-wrong-owner",
                ),
                "phase6-run-wrong-owner",
                session_key,
            )
        )
        wrong_case_context = dict(packet.governance_context)
        wrong_case_context["case_id"] = "case_other_owner"
        strict_cases.append(
            (
                "mismatched private Case",
                replace(
                    packet,
                    governance_context=wrong_case_context,
                    runtime_run_id="phase6-run-wrong-case",
                ),
                "phase6-run-wrong-case",
                session_key,
            )
        )
        strict_cases.extend(
            [
                (
                    "uncanonicalized OpenClaw runtime session",
                    replace(
                        packet,
                        runtime_run_id="phase6-run-raw-session",
                    ),
                    "phase6-run-raw-session",
                    session_key,
                ),
                (
                    "mismatched dialogue task",
                    replace(
                        packet,
                        task_id="task_phase6_wrong_dialogue_binding",
                        runtime_run_id="phase6-run-wrong-task",
                    ),
                    "phase6-run-wrong-task",
                    session_key,
                ),
                (
                    "mismatched bound provider",
                    replace(
                        packet,
                        target_agent="other-runtime",
                        runtime_run_id="phase6-run-wrong-provider",
                    ),
                    "phase6-run-wrong-provider",
                    session_key,
                ),
                (
                    "missing runtime_run_id",
                    replace(packet, runtime_run_id=""),
                    "phase6-run-missing-runtime",
                    session_key,
                ),
                (
                    "mismatched Agent session",
                    replace(
                        packet,
                        runtime_run_id="phase6-run-session-mismatch",
                    ),
                    "phase6-run-session-mismatch",
                    "agent-exec:another-session",
                ),
                (
                    "missing target Agent",
                    replace(
                        packet,
                        target_agent="",
                        runtime_run_id="phase6-run-missing-agent",
                        agent_execution_session_id=(
                            "agent-exec:phase6-missing-agent"
                        ),
                    ),
                    "phase6-run-missing-agent",
                    "agent-exec:phase6-missing-agent",
                ),
            ]
        )
        for label, candidate, candidate_run, candidate_session in strict_cases:
            expect_dispatch_denied(
                broker,
                candidate,
                run_id=candidate_run,
                session_key=(
                    candidate_session
                    if label
                    == "uncanonicalized OpenClaw runtime session"
                    else openclaw_runtime_session(candidate_session)
                ),
                label=f"Phase 6 fails closed for {label}",
            )

        expired_binding = build_collaboration_binding(
            participant_id="participant-phase6-expired",
            role="primary_analyst",
            parent_participant_id=None,
            handoff_index=0,
            capability_scope=[],
            evidence_scope=[],
            provider={
                "runtime": "openclaw",
                "provider": "moonshot",
                "model": "kimi",
                "instance_id": "openclaw-main",
                "automatic_switch_allowed": False,
            },
            budget={
                "remaining_agent_calls": 3,
                "remaining_handoffs": 1,
                "remaining_evidence_patches": 1,
                "max_wall_time_seconds": 300,
                "max_context_bytes": 8 * 1024,
                "max_output_bytes": 24 * 1024,
            },
            issued_at=clock_now - timedelta(seconds=600),
            expires_at=clock_now - timedelta(seconds=300),
        )
        expired_dialogue = build_task_request(
            case_id="case_phase6_no_tools",
            case_revision=0,
            turn_index=0,
            message_id="msg_phase6_expired",
            task_packet_id="task_phase6_expired",
            operation_id="op_phase6_expired",
            scope_digest=scope_digest(
                user_id="phase6-user",
                workspace_id=workspace_id,
            ),
            user_goal=str(event.payload["text"]),
            constraints=["do not call tools"],
            evidence_refs=[],
            context=dialogue_context,
            collaboration_binding=expired_binding,
        )
        expired_transport = collaboration_turn_transport_identity(
            expired_dialogue
        )
        try:
            builder.build(
                **{
                    **build_args,
                    "task_id": "task_phase6_expired",
                    "runtime_run_id": "phase6-run-expired-builder",
                    "agent_execution_session_id": expired_transport[
                        "agent_execution_session_id"
                    ],
                    "dialogue_message": expired_dialogue,
                }
            )
        except ValueError:
            print("ok - builder rejects an expired collaboration binding")
        else:
            raise AssertionError(
                "builder rejects an expired collaboration binding failed"
            )
        expired_packet = replace(
            packet,
            task_id="task_phase6_expired",
            session_id=expired_transport["packet_session_id"],
            runtime_run_id="phase6-run-expired-broker",
            agent_execution_session_id=expired_transport[
                "agent_execution_session_id"
            ],
            dialogue_message=expired_dialogue,
        )
        expired_validation = validate_task_packet_payload(
            expired_packet.to_dict()
        )
        expect(
            any("expired" in error for error in expired_validation),
            "public TaskPacket validator rejects an expired binding",
            expired_validation,
        )
        expect_dispatch_denied(
            broker,
            expired_packet,
            run_id="phase6-run-expired-broker",
            session_key=openclaw_runtime_session(
                expired_transport["agent_execution_session_id"]
            ),
            label="broker rejects an expired collaboration binding",
        )

        untrusted_context = dict(packet.governance_context)
        untrusted_context["execution_profile"] = "untrusted_profile"
        expect_dispatch_denied(
            broker,
            replace(
                packet,
                governance_context=untrusted_context,
                runtime_run_id="phase6-run-untrusted",
                agent_execution_session_id="agent-exec:phase6-untrusted",
            ),
            run_id="phase6-run-untrusted",
            session_key=openclaw_runtime_session(
                "agent-exec:phase6-untrusted"
            ),
            label="unknown private execution profile fails closed",
        )

        legacy_context = dict(packet.governance_context)
        legacy_context.pop("execution_profile")
        expect_dispatch_denied(
            broker,
            replace(
                packet,
                governance_context=legacy_context,
                runtime_run_id="phase6-run-missing-profile",
            ),
            run_id="phase6-run-missing-profile",
            session_key=runtime_session_key,
            label=(
                "bound collaboration cannot fall back to the legacy "
                "full-tool profile"
            ),
        )

        true_legacy_packet = builder.build(
            event=event,
            target_agent="openclaw",
            context_patch={},
            persona_patch={},
            policy_patch={},
            task_id="task_legacy_full_tools",
            case_id="case_legacy_full_tools",
            step_id="step_legacy_full_tools",
            runtime_run_id="legacy-run-full-tools",
            agent_execution_session_id="agent-exec:legacy-full-tools",
        )
        true_legacy_context = dict(
            true_legacy_packet.governance_context
        )
        true_legacy_context.pop("execution_profile")
        legacy_run = "legacy-run-full-tools"
        legacy_session = "agent-exec:legacy-full-tools"
        legacy_registration = broker.prepare_dispatch(
            replace(
                true_legacy_packet,
                governance_context=true_legacy_context,
            ),
            run_id=legacy_run,
            session_key=legacy_session,
        )
        expect(
            set(legacy_registration.allowed_tools)
            == set(CUSTOM_TOOL_REGISTRY),
            (
                "true pre-profile packets without a collaboration binding "
                "preserve the legacy governed allowlist"
            ),
            legacy_registration.allowed_tools,
        )

    print("phase6_no_tool_execution_profile_smoke: ok")


if __name__ == "__main__":
    main()
