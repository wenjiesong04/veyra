#!/usr/bin/env python3
from __future__ import annotations

import copy
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.durable_case import DialogueMessageType, DialogueRecord  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402
from interface.agent_contract import (  # noqa: E402
    render_prompt_payload,
    validate_task_packet_payload,
)
from interface.agent_dialogue_contract import (  # noqa: E402
    DIALOGUE_CONTRACT_VERSION,
    DialogueContractError,
    DialogueType,
    build_collaboration_binding,
    build_context_patch,
    build_plan_selection,
    build_task_request,
    collaboration_scope_digest,
    collaboration_turn_transport_identity,
    expected_agent_reply_message_id,
    extract_agent_dialogue,
    parse_collaboration_binding,
    parse_dialogue_message,
    scope_digest,
    validate_agent_reply_scope,
)
from runtime.durable_case_store import (  # noqa: E402
    CaseOperationConflictError,
    DurableCaseStore,
)


NOW = datetime.now(timezone.utc)
USER_ID = "phase6-user"
WORKSPACE_ID = "phase6-workspace"
SCOPE_DIGEST = scope_digest(
    user_id=USER_ID,
    workspace_id=WORKSPACE_ID,
)


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail!r}")
    print(f"ok - {label}")


def binding(
    *,
    participant_id: str,
    role: str,
    parent_participant_id: str | None,
    handoff_index: int,
    capabilities: list[str],
    evidence: list[str],
    remaining_calls: int,
    remaining_handoffs: int,
    remaining_patches: int,
    offset_seconds: int,
    max_wall_time_seconds: int,
    max_context_bytes: int = 4096,
) -> dict[str, Any]:
    return build_collaboration_binding(
        participant_id=participant_id,
        role=role,  # type: ignore[arg-type]
        parent_participant_id=parent_participant_id,
        handoff_index=handoff_index,
        capability_scope=capabilities,
        evidence_scope=evidence,
        provider={
            "runtime": "openclaw",
            "provider": "moonshot",
            "model": "kimi-k2",
            "instance_id": "openclaw-local-1",
        },
        budget={
            "remaining_agent_calls": remaining_calls,
            "remaining_handoffs": remaining_handoffs,
            "remaining_evidence_patches": remaining_patches,
            "max_wall_time_seconds": max_wall_time_seconds,
            "max_context_bytes": max_context_bytes,
            "max_output_bytes": 8192,
        },
        issued_at=NOW + timedelta(seconds=offset_seconds),
        expires_at=NOW + timedelta(seconds=200),
    )


def record(message: dict[str, Any]) -> DialogueRecord:
    return DialogueRecord.model_validate(
        {
            "message_id": message["message_id"],
            "message_type": DialogueMessageType(message["message_type"]),
            "sender": message["sender"],
            "direction": (
                "veyra_to_agent"
                if message["sender"] == "veyra"
                else "agent_to_veyra"
            ),
            "case_revision": message["case_revision"],
            "turn_index": message["turn_index"],
            "in_reply_to": message.get("in_reply_to"),
            "content": message,
            "authority_granted": False,
            "evidence_verified": False,
            "recorded_at": NOW,
        },
        strict=True,
    )


def agent_reply(
    *,
    parent: dict[str, Any],
    message_id: str,
    message_type: str,
    collaboration_binding: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    message = {
        "contract_version": DIALOGUE_CONTRACT_VERSION,
        "message_id": message_id,
        "message_type": message_type,
        "case_id": parent["case_id"],
        "case_revision": parent["case_revision"],
        "turn_index": parent["turn_index"],
        "task_packet_id": parent["task_packet_id"],
        "operation_id": parent["operation_id"],
        "scope_digest": parent["scope_digest"],
        "collaboration_binding": copy.deepcopy(
            collaboration_binding
        ),
        "sender": "agent",
        "in_reply_to": parent["message_id"],
        "payload": payload,
    }
    parse_dialogue_message(
        message,
        expected_sender="agent",
        allowed_types={
            DialogueType.EVIDENCE_REQUEST,
            DialogueType.CHALLENGE,
            DialogueType.OPTION_SET,
        },
    )
    return message


def option_payload(prefix: str, *, evidence_ref: str) -> dict[str, Any]:
    return {
        "options": [
            {
                "option_id": f"{prefix}-observe",
                "summary": "Continue with bounded read-only observation.",
                "assumptions": ["No execution authority exists."],
                "expected_outcome": "A reviewable analysis proposal.",
                "costs": ["One bounded Agent response."],
                "risks": ["The evidence may remain incomplete."],
                "evidence_refs": [evidence_ref],
                "required_capabilities": ["project.read"],
            },
            {
                "option_id": f"{prefix}-pause",
                "summary": "Pause and ask the user for direction.",
                "assumptions": ["User input can resolve the ambiguity."],
                "expected_outcome": "No side effect occurs.",
                "costs": ["One user interruption."],
                "risks": ["The Case remains open longer."],
                "evidence_refs": [],
                "required_capabilities": [],
            },
        ],
        "recommended_option_id": f"{prefix}-observe",
    }


def packet(dialogue: dict[str, Any]) -> dict[str, Any]:
    dialogue_payload = dialogue.get("payload", {})
    dialogue_context = (
        dialogue_payload.get("context", {})
        if dialogue["message_type"]
        in {"TASK_REQUEST", "CONTEXT_PATCH"}
        else {}
    )
    user_goal = (
        dialogue_payload.get("user_goal")
        if dialogue["message_type"] == "TASK_REQUEST"
        else ""
    )
    transport_identity = collaboration_turn_transport_identity(
        dialogue
    )
    return {
        "task_id": dialogue["task_packet_id"],
        "target_agent": "openclaw",
        "session_id": transport_identity["packet_session_id"],
        "user_message": user_goal,
        "user_goal": user_goal,
        "required_capabilities": ["project.read"],
        "context_patch": dialogue_context,
        "persona_patch": {},
        "policy_patch": {},
        "verification_policy": {},
        "rollback_requirement": {},
        "memory_policy": "forget",
        "agent_execution_session_id": transport_identity[
            "agent_execution_session_id"
        ],
        "agent_session_policy": "ephemeral_per_task",
        "dialogue_message": dialogue,
    }


def expect_conflict(call: Any, label: str) -> None:
    try:
        call()
    except CaseOperationConflictError:
        print(f"ok - {label}")
        return
    raise AssertionError(f"{label} did not fail closed")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="veyra-phase6-contract-") as tmp:
        store = DurableCaseStore(
            WorldStateStore(Path(tmp)),
            clock=lambda: NOW,
        )
        admitted = store.admit_event(
            event_id="phase6-contract-event",
            user_id=USER_ID,
            workspace_id=WORKSPACE_ID,
            user_goal="Compare one primary analysis with one critique.",
        )
        case_id = admitted["case_id"]

        expired = build_collaboration_binding(
            participant_id="expired-primary",
            role="primary_analyst",
            parent_participant_id=None,
            handoff_index=0,
            capability_scope=[],
            evidence_scope=[],
            provider={
                "runtime": "openclaw",
                "provider": "moonshot",
                "model": "kimi-k2",
                "instance_id": "openclaw-local-1",
            },
            budget={
                "remaining_agent_calls": 1,
                "remaining_handoffs": 0,
                "remaining_evidence_patches": 0,
                "max_wall_time_seconds": 60,
                "max_context_bytes": 4096,
                "max_output_bytes": 8192,
            },
            issued_at=NOW - timedelta(seconds=20),
            expires_at=NOW - timedelta(seconds=10),
        )
        expired_request = build_task_request(
            case_id=case_id,
            case_revision=1,
            turn_index=0,
            message_id="phase6-expired-request",
            task_packet_id="phase6-expired-task",
            operation_id="phase6-expired-operation",
            scope_digest=SCOPE_DIGEST,
            user_goal="This request must never be persisted.",
            collaboration_binding=expired,
        )
        expect_conflict(
            lambda: store.transition(
                case_id=case_id,
                user_id=USER_ID,
                workspace_id=WORKSPACE_ID,
                operation_id="reject-expired-binding",
                expected_revision=1,
                to_status="DELIBERATING",
                reason="Reject an expired collaboration binding.",
                dialogue_message=record(expired_request),
            ),
            "expired collaboration binding fails closed",
        )
        expect(
            any(
                "expired" in error
                for error in validate_task_packet_payload(
                    packet(expired_request)
                )
            ),
            "public task contract rejects an expired collaboration binding",
        )
        future = binding(
            participant_id="future-primary",
            role="primary_analyst",
            parent_participant_id=None,
            handoff_index=0,
            capabilities=[],
            evidence=[],
            remaining_calls=1,
            remaining_handoffs=0,
            remaining_patches=0,
            offset_seconds=10,
            max_wall_time_seconds=230,
        )
        future_request = build_task_request(
            case_id=case_id,
            case_revision=1,
            turn_index=0,
            message_id="phase6-future-request",
            task_packet_id="phase6-future-task",
            operation_id="phase6-future-operation",
            scope_digest=SCOPE_DIGEST,
            user_goal="This request is not active yet.",
            collaboration_binding=future,
        )
        expect_conflict(
            lambda: store.transition(
                case_id=case_id,
                user_id=USER_ID,
                workspace_id=WORKSPACE_ID,
                operation_id="reject-future-binding",
                expected_revision=1,
                to_status="DELIBERATING",
                reason="Reject a future-issued collaboration binding.",
                dialogue_message=record(future_request),
            ),
            "future-issued collaboration binding fails closed",
        )
        expect(
            any(
                "not active yet" in error
                for error in validate_task_packet_payload(
                    packet(future_request)
                )
            ),
            (
                "public task contract rejects a future-issued "
                "collaboration binding"
            ),
        )

        primary = binding(
            participant_id="primary-1",
            role="primary_analyst",
            parent_participant_id=None,
            handoff_index=0,
            capabilities=["project.read"],
            evidence=["evidence-initial"],
            remaining_calls=3,
            remaining_handoffs=1,
            remaining_patches=1,
            offset_seconds=0,
            max_wall_time_seconds=300,
        )
        wrong_scope_request = build_task_request(
            case_id=case_id,
            case_revision=1,
            turn_index=0,
            message_id="phase6-wrong-scope-request",
            task_packet_id="phase6-wrong-scope-task",
            operation_id="phase6-wrong-scope-operation",
            scope_digest="0" * 64,
            user_goal="This request has the wrong owner scope.",
            collaboration_binding=primary,
        )
        expect_conflict(
            lambda: store.transition(
                case_id=case_id,
                user_id=USER_ID,
                workspace_id=WORKSPACE_ID,
                operation_id="reject-wrong-scope-digest",
                expected_revision=1,
                to_status="DELIBERATING",
                reason="Reject a cross-scope collaboration digest.",
                dialogue_message=record(wrong_scope_request),
            ),
            "collaboration dialogue binds exact Case owner scope",
        )
        small_context_binding = binding(
            participant_id="small-context-primary",
            role="primary_analyst",
            parent_participant_id=None,
            handoff_index=0,
            capabilities=[],
            evidence=[],
            remaining_calls=1,
            remaining_handoffs=0,
            remaining_patches=0,
            offset_seconds=0,
            max_wall_time_seconds=300,
            max_context_bytes=256,
        )
        oversized_context_request = build_task_request(
            case_id=case_id,
            case_revision=1,
            turn_index=0,
            message_id="phase6-oversized-context-request",
            task_packet_id="phase6-oversized-context-task",
            operation_id="phase6-oversized-context-operation",
            scope_digest=SCOPE_DIGEST,
            user_goal="This request exceeds its context budget.",
            context={"data": "x" * 300},
            collaboration_binding=small_context_binding,
        )
        expect_conflict(
            lambda: store.transition(
                case_id=case_id,
                user_id=USER_ID,
                workspace_id=WORKSPACE_ID,
                operation_id="reject-context-over-budget",
                expected_revision=1,
                to_status="DELIBERATING",
                reason="Reject context above the collaboration budget.",
                dialogue_message=record(oversized_context_request),
            ),
            "collaboration context budget is enforced",
        )
        expect(
            primary["collaboration_scope_digest"]
            == collaboration_scope_digest(primary),
            "collaboration digest is canonical",
        )
        unsigned_binding = copy.deepcopy(primary)
        unsigned_binding.pop("collaboration_scope_digest")
        unsigned_binding.pop("parent_participant_id")
        unsigned_binding["collaboration_scope_digest"] = (
            collaboration_scope_digest(unsigned_binding)
        )
        expect(
            parse_collaboration_binding(
                unsigned_binding
            ).parent_participant_id
            is None,
            "canonical digest fills the optional root parent default",
        )
        request = build_task_request(
            case_id=case_id,
            case_revision=1,
            turn_index=0,
            message_id="phase6-request-primary",
            task_packet_id="phase6-task-primary",
            operation_id="phase6-operation-primary",
            scope_digest=SCOPE_DIGEST,
            user_goal="Compare one primary analysis with one critique.",
            constraints=["read only", "one handoff maximum"],
            evidence_refs=["evidence-initial"],
            collaboration_binding=primary,
        )
        alternate_primary = binding(
            participant_id="primary-2",
            role="primary_analyst",
            parent_participant_id=None,
            handoff_index=0,
            capabilities=["project.read"],
            evidence=["evidence-initial"],
            remaining_calls=3,
            remaining_handoffs=1,
            remaining_patches=1,
            offset_seconds=0,
            max_wall_time_seconds=300,
        )
        alternate_request = build_task_request(
            case_id=case_id,
            case_revision=1,
            turn_index=0,
            message_id="phase6-request-primary",
            task_packet_id="phase6-task-primary",
            operation_id="phase6-operation-primary",
            scope_digest=SCOPE_DIGEST,
            user_goal="Compare one primary analysis with one critique.",
            constraints=["read only", "one handoff maximum"],
            evidence_refs=["evidence-initial"],
            collaboration_binding=alternate_primary,
        )
        expect(
            collaboration_turn_transport_identity(request)
            != collaboration_turn_transport_identity(
                alternate_request
            ),
            (
                "transport identity binds the complete participant and "
                "collaboration scope"
            ),
        )
        expected_primary_reply_id = expected_agent_reply_message_id(
            request
        )
        phase6_prompt = render_prompt_payload(packet(request))
        expect(
            expected_primary_reply_id
            in phase6_prompt,
            "prompt preallocates the exact inbound reply id",
        )
        expect(
            '"challenged_claim_refs": [\n'
            '      "evidence-initial"\n'
            "    ]"
            in phase6_prompt
            and (
                "For CHALLENGE, challenged_claim_refs is required, "
                "must contain at least one item"
            )
            in phase6_prompt,
            (
                "bound prompt gives CHALLENGE a non-empty exact-scope "
                "reference"
            ),
        )
        expect(
            '"agent_understanding"' not in phase6_prompt
            and "only top-level field is dialogue_message"
            in phase6_prompt,
            "bound prompt exposes one non-competing response contract",
        )
        expect(
            not validate_task_packet_payload(packet(request)),
            "bound TASK_REQUEST is accepted by the public task contract",
        )
        expanded_packet = packet(request)
        expanded_packet["required_capabilities"] = ["network.write"]
        expect(
            any(
                "exceeds dialogue collaboration capability scope" in error
                for error in validate_task_packet_payload(expanded_packet)
            ),
            "task packet cannot bypass collaboration capability scope",
        )
        widened_packet = packet(request)
        widened_packet["task_id"] = "different-task"
        widened_packet["target_agent"] = "different-runtime"
        widened_packet["context_patch"] = {"unbound": "secret"}
        widened_packet["policy_patch"] = {
            "execution_authorized": True,
            "allowed_tools": ["shell.run"],
        }
        widened_errors = validate_task_packet_payload(widened_packet)
        expect(
            all(
                any(marker in error for error in widened_errors)
                for marker in (
                    "task_id must exactly match",
                    "target_agent must exactly match",
                    "context_patch must exactly match",
                    "policy_patch must be empty",
                )
            ),
            "outer task packet cannot widen bound identity, context, or policy",
            widened_errors,
        )
        shared_session_packet = packet(request)
        shared_session_packet["agent_execution_session_id"] = (
            "agent:openclaw:main"
        )
        shared_session_packet["agent_session_policy"] = "persistent"
        session_errors = validate_task_packet_payload(
            shared_session_packet
        )
        expect(
            any(
                "agent_execution_session_id must match" in error
                for error in session_errors
            )
            and any(
                "agent_session_policy must be ephemeral_per_task" in error
                for error in session_errors
            ),
            "bound collaboration cannot reuse a shared Agent session",
            session_errors,
        )
        expect(
            primary["collaboration_scope_digest"]
            in render_prompt_payload(packet(request))
            and f"Session ID: {packet(request)['session_id']}"
            not in render_prompt_payload(packet(request))
            and "Memory Policy: forget"
            in render_prompt_payload(packet(request))
            and "has no tool, workspace, memory, or capability authority"
            in render_prompt_payload(packet(request)),
            (
                "prompt carries the binding without exposing a session or "
                "suggesting memory access"
            ),
        )
        expect_conflict(
            lambda: store.append_dialogue(
                case_id=case_id,
                user_id=USER_ID,
                workspace_id=WORKSPACE_ID,
                operation_id="reject-root-without-deliberating",
                expected_revision=1,
                dialogue_message=record(request),
                reason="Reject an active root that leaves Case QUALIFIED.",
            ),
            "bound root must result in DELIBERATING",
        )
        first = store.transition(
            case_id=case_id,
            user_id=USER_ID,
            workspace_id=WORKSPACE_ID,
            operation_id="persist-primary-request",
            expected_revision=1,
            to_status="DELIBERATING",
            reason="Start bounded primary analysis.",
            dialogue_message=record(request),
        )
        expect(first["revision"] == 2, "primary request is durable")

        evidence_reply = agent_reply(
            parent=request,
            message_id="phase6-evidence-request",
            message_type="EVIDENCE_REQUEST",
            collaboration_binding=primary,
            payload={
                "requested_evidence": [
                    {
                        "request_id": "fresh-project-observation",
                        "question": "What is the fresh project observation?",
                        "reason": "The comparison needs a current source.",
                        "claim_ref": "evidence-initial",
                        "freshness_required": True,
                    }
                ],
                "requested_capabilities": ["project.read"],
            },
        )
        try:
            extract_agent_dialogue(
                {
                    "dialogue_message": evidence_reply,
                    "tool_calls": [],
                },
                expected_message_id=evidence_reply["message_id"],
                expected_case_id=request["case_id"],
                expected_case_revision=request["case_revision"],
                expected_turn_index=request["turn_index"],
                expected_in_reply_to=request["message_id"],
                expected_task_packet_id=request["task_packet_id"],
                expected_operation_id=request["operation_id"],
                expected_scope_digest=request["scope_digest"],
                expected_collaboration_binding=primary,
            )
        except DialogueContractError as exc:
            strict_top_level = (
                "bound Agent response must contain only the "
                "dialogue_message top-level field"
            ) in exc.errors
        else:
            strict_top_level = False
        expect(
            strict_top_level,
            "bound Agent response rejects every extra top-level field",
        )

        wrong_reply_binding = binding(
            participant_id="primary-1",
            role="primary_analyst",
            parent_participant_id=None,
            handoff_index=0,
            capabilities=[],
            evidence=["evidence-initial"],
            remaining_calls=3,
            remaining_handoffs=1,
            remaining_patches=1,
            offset_seconds=0,
            max_wall_time_seconds=300,
        )
        wrong_binding_reply = agent_reply(
            parent=request,
            message_id="phase6-wrong-binding",
            message_type="EVIDENCE_REQUEST",
            collaboration_binding=wrong_reply_binding,
            payload=evidence_reply["payload"],
        )
        expect_conflict(
            lambda: store.transition(
                case_id=case_id,
                user_id=USER_ID,
                workspace_id=WORKSPACE_ID,
                operation_id="reject-wrong-parent-binding",
                expected_revision=2,
                to_status="AWAITING_EVIDENCE",
                reason="Reject a reply that changed its parent binding.",
                dialogue_message=record(wrong_binding_reply),
            ),
            "Agent reply must echo the exact parent binding",
        )

        expanded_request = agent_reply(
            parent=request,
            message_id="phase6-expanded-capability",
            message_type="EVIDENCE_REQUEST",
            collaboration_binding=primary,
            payload={
                **evidence_reply["payload"],
                "requested_capabilities": ["network.write"],
            },
        )
        expect(
            validate_agent_reply_scope(
                expanded_request,
                parent=primary,
            )
            == [
                "Agent reply capability request exceeds its exact parent "
                "scope"
            ],
            "verifier rejects Agent capability scope before Case mutation",
        )
        expect_conflict(
            lambda: store.transition(
                case_id=case_id,
                user_id=USER_ID,
                workspace_id=WORKSPACE_ID,
                operation_id="reject-agent-capability-expansion",
                expected_revision=2,
                to_status="AWAITING_EVIDENCE",
                reason="Reject a capability expansion.",
                dialogue_message=record(expanded_request),
            ),
            "Agent payload cannot expand capability scope",
        )
        evidence_state = store.transition(
            case_id=case_id,
            user_id=USER_ID,
            workspace_id=WORKSPACE_ID,
            operation_id="persist-evidence-request",
            expected_revision=2,
            to_status="AWAITING_EVIDENCE",
            reason="Persist exact-bound evidence request.",
            dialogue_message=record(evidence_reply),
        )
        expect(evidence_state["revision"] == 3, "evidence request is durable")
        duplicate_reply = agent_reply(
            parent=request,
            message_id="phase6-duplicate-evidence-request",
            message_type="EVIDENCE_REQUEST",
            collaboration_binding=primary,
            payload=evidence_reply["payload"],
        )
        expect_conflict(
            lambda: store.append_dialogue(
                case_id=case_id,
                user_id=USER_ID,
                workspace_id=WORKSPACE_ID,
                operation_id="reject-second-agent-reply",
                expected_revision=3,
                dialogue_message=record(duplicate_reply),
                reason="Reject a second reply to one bound request.",
            ),
            "bound Veyra request accepts exactly one Agent reply",
        )

        patched_primary = binding(
            participant_id="primary-1",
            role="primary_analyst",
            parent_participant_id=None,
            handoff_index=0,
            capabilities=["project.read"],
            evidence=["evidence-fresh", "evidence-initial"],
            remaining_calls=2,
            remaining_handoffs=1,
            remaining_patches=0,
            offset_seconds=0,
            max_wall_time_seconds=240,
        )
        unresolved_primary = binding(
            participant_id="primary-1",
            role="primary_analyst",
            parent_participant_id=None,
            handoff_index=0,
            capabilities=["project.read"],
            evidence=["evidence-initial"],
            remaining_calls=2,
            remaining_handoffs=1,
            remaining_patches=0,
            offset_seconds=0,
            max_wall_time_seconds=240,
        )
        unresolved_patch = build_context_patch(
            case_id=case_id,
            case_revision=3,
            turn_index=1,
            message_id="phase6-unresolved-context",
            task_packet_id="phase6-task-context",
            operation_id="phase6-operation-context",
            scope_digest=SCOPE_DIGEST,
            in_reply_to=evidence_reply["message_id"],
            collaboration_binding=unresolved_primary,
            resolved_request_ids=[],
            evidence_refs=[],
            unresolved_request_ids=["fresh-project-observation"],
            context={"status": "no authoritative evidence available"},
        )
        expect(
            parse_dialogue_message(
                unresolved_patch,
                expected_sender="veyra",
                allowed_types={DialogueType.CONTEXT_PATCH},
            ).payload.evidence_refs
            == [],
            "unresolved CONTEXT_PATCH does not require invented evidence",
        )
        expanded_binding = binding(
            participant_id="primary-1",
            role="primary_analyst",
            parent_participant_id=None,
            handoff_index=0,
            capabilities=["network.write", "project.read"],
            evidence=["evidence-fresh", "evidence-initial"],
            remaining_calls=2,
            remaining_handoffs=1,
            remaining_patches=0,
            offset_seconds=0,
            max_wall_time_seconds=240,
        )
        bad_patch = build_context_patch(
            case_id=case_id,
            case_revision=3,
            turn_index=1,
            message_id="phase6-expanded-context",
            task_packet_id="phase6-task-context",
            operation_id="phase6-operation-context",
            scope_digest=SCOPE_DIGEST,
            in_reply_to=evidence_reply["message_id"],
            collaboration_binding=expanded_binding,
            resolved_request_ids=["fresh-project-observation"],
            evidence_refs=["evidence-fresh"],
            context={"observation": "fresh and read-only"},
        )
        expect_conflict(
            lambda: store.transition(
                case_id=case_id,
                user_id=USER_ID,
                workspace_id=WORKSPACE_ID,
                operation_id="reject-context-capability-expansion",
                expected_revision=3,
                to_status="DELIBERATING",
                reason="Reject a Veyra child scope expansion.",
                dialogue_message=record(bad_patch),
            ),
            "CONTEXT_PATCH cannot expand capability scope",
        )
        bad_partition = build_context_patch(
            case_id=case_id,
            case_revision=3,
            turn_index=1,
            message_id="phase6-wrong-request-partition",
            task_packet_id="phase6-task-context",
            operation_id="phase6-operation-context",
            scope_digest=SCOPE_DIGEST,
            in_reply_to=evidence_reply["message_id"],
            collaboration_binding=patched_primary,
            resolved_request_ids=["fresh-project-observation"],
            evidence_refs=["evidence-fresh"],
            unresolved_request_ids=["not-requested"],
            context={"observation": "fresh and read-only"},
        )
        expect_conflict(
            lambda: store.transition(
                case_id=case_id,
                user_id=USER_ID,
                workspace_id=WORKSPACE_ID,
                operation_id="reject-wrong-request-partition",
                expected_revision=3,
                to_status="DELIBERATING",
                reason="Reject a patch for a request the Agent did not make.",
                dialogue_message=record(bad_partition),
            ),
            "CONTEXT_PATCH must partition the exact parent request set",
        )
        context_patch = build_context_patch(
            case_id=case_id,
            case_revision=3,
            turn_index=1,
            message_id="phase6-context-patch",
            task_packet_id="phase6-task-context",
            operation_id="phase6-operation-context",
            scope_digest=SCOPE_DIGEST,
            in_reply_to=evidence_reply["message_id"],
            collaboration_binding=patched_primary,
            resolved_request_ids=["fresh-project-observation"],
            evidence_refs=["evidence-fresh"],
            context={"observation": "fresh and read-only"},
        )
        expect(
            not validate_task_packet_payload(packet(context_patch)),
            "CONTEXT_PATCH is accepted by the public task contract",
        )
        patched = store.transition(
            case_id=case_id,
            user_id=USER_ID,
            workspace_id=WORKSPACE_ID,
            operation_id="persist-context-patch",
            expected_revision=3,
            to_status="DELIBERATING",
            reason="Supply bounded evidence context.",
            dialogue_message=record(context_patch),
        )
        expect(patched["revision"] == 4, "context patch is durable")

        primary_options = agent_reply(
            parent=context_patch,
            message_id="phase6-primary-options",
            message_type="OPTION_SET",
            collaboration_binding=patched_primary,
            payload=option_payload(
                "primary",
                evidence_ref="evidence-fresh",
            ),
        )
        widened_options = copy.deepcopy(primary_options)
        widened_options["payload"]["options"][0]["evidence_refs"] = [
            "evidence-outside-parent-scope"
        ]
        expect(
            validate_agent_reply_scope(
                widened_options,
                parent=patched_primary,
            )
            == [
                "Agent reply evidence reference exceeds its exact parent "
                "scope"
            ],
            "verifier rejects Agent evidence scope before Case mutation",
        )
        proposed = store.transition(
            case_id=case_id,
            user_id=USER_ID,
            workspace_id=WORKSPACE_ID,
            operation_id="persist-primary-options",
            expected_revision=4,
            to_status="PROPOSED",
            reason="Persist primary options.",
            dialogue_message=record(primary_options),
        )
        expect(proposed["revision"] == 5, "primary options are durable")

        critic = binding(
            participant_id="critic-1",
            role="critic",
            parent_participant_id="primary-1",
            handoff_index=1,
            capabilities=["project.read"],
            evidence=["evidence-fresh", "evidence-initial"],
            remaining_calls=1,
            remaining_handoffs=0,
            remaining_patches=0,
            offset_seconds=0,
            max_wall_time_seconds=230,
        )
        critic_request = build_task_request(
            case_id=case_id,
            case_revision=5,
            turn_index=2,
            message_id="phase6-request-critic",
            task_packet_id="phase6-task-critic",
            operation_id="phase6-operation-critic",
            scope_digest=SCOPE_DIGEST,
            user_goal="Critique the bounded primary options.",
            constraints=["read only", "no further handoff"],
            evidence_refs=["evidence-fresh", "evidence-initial"],
            collaboration_binding=critic,
            in_reply_to=primary_options["message_id"],
        )
        expect(
            expected_agent_reply_message_id(critic_request)
            != expected_primary_reply_id,
            "each collaboration turn gets a distinct Veyra reply id",
        )
        delegated = store.transition(
            case_id=case_id,
            user_id=USER_ID,
            workspace_id=WORKSPACE_ID,
            operation_id="persist-critic-request",
            expected_revision=5,
            to_status="DELIBERATING",
            reason="Use the single bounded critic handoff.",
            dialogue_message=record(critic_request),
        )
        expect(delegated["revision"] == 6, "critic handoff is durable")

        critic_options = agent_reply(
            parent=critic_request,
            message_id="phase6-critic-options",
            message_type="OPTION_SET",
            collaboration_binding=critic,
            payload=option_payload(
                "critic",
                evidence_ref="evidence-fresh",
            ),
        )
        critiqued = store.transition(
            case_id=case_id,
            user_id=USER_ID,
            workspace_id=WORKSPACE_ID,
            operation_id="persist-critic-options",
            expected_revision=6,
            to_status="PROPOSED",
            reason="Persist critic options.",
            dialogue_message=record(critic_options),
        )
        expect(critiqued["revision"] == 7, "critic options are durable")

        selected_scope = binding(
            participant_id="critic-1",
            role="critic",
            parent_participant_id="primary-1",
            handoff_index=1,
            capabilities=["project.read"],
            evidence=["evidence-fresh", "evidence-initial"],
            remaining_calls=0,
            remaining_handoffs=0,
            remaining_patches=0,
            offset_seconds=0,
            max_wall_time_seconds=220,
        )
        wrong_selection = build_plan_selection(
            case_id=case_id,
            case_revision=7,
            turn_index=3,
            message_id="phase6-wrong-selection",
            task_packet_id="phase6-task-selection",
            operation_id="phase6-operation-selection",
            scope_digest=SCOPE_DIGEST,
            in_reply_to=critic_options["message_id"],
            collaboration_binding=selected_scope,
            option_set_message_id=critic_options["message_id"],
            selected_option_id="missing-option",
            decision_reason="This option is intentionally absent.",
        )
        expect_conflict(
            lambda: store.append_dialogue(
                case_id=case_id,
                user_id=USER_ID,
                workspace_id=WORKSPACE_ID,
                operation_id="reject-missing-option",
                expected_revision=7,
                dialogue_message=record(wrong_selection),
                reason="Reject a selection absent from its parent.",
            ),
            "PLAN_SELECTION must select an exact parent option",
        )
        selection = build_plan_selection(
            case_id=case_id,
            case_revision=7,
            turn_index=3,
            message_id="phase6-plan-selection",
            task_packet_id="phase6-task-selection",
            operation_id="phase6-operation-selection",
            scope_digest=SCOPE_DIGEST,
            in_reply_to=critic_options["message_id"],
            collaboration_binding=selected_scope,
            option_set_message_id=critic_options["message_id"],
            selected_option_id="critic-observe",
            decision_reason=(
                "It preserves read-only analysis and the strongest bounded "
                "evidence."
            ),
            decision_evidence_refs=["evidence-fresh"],
            rejected_option_ids=["critic-pause"],
        )
        expect(
            not validate_task_packet_payload(packet(selection)),
            "PLAN_SELECTION is accepted by the public task contract",
        )
        selection_prompt = render_prompt_payload(packet(selection))
        expect(
            "does not grant execution" in selection_prompt,
            "PLAN_SELECTION prompt preserves non-authority",
        )
        final_state = store.transition(
            case_id=case_id,
            user_id=USER_ID,
            workspace_id=WORKSPACE_ID,
            operation_id="persist-plan-selection",
            expected_revision=7,
            to_status="CLOSED",
            dialogue_message=record(selection),
            reason=(
                "Persist and close a bounded non-authorizing plan selection."
            ),
        )
        expect(final_state["revision"] == 8, "plan selection is durable")
        expect(
            final_state["status"] == "CLOSED",
            "plan selection does not create execution state",
        )
        expect(
            all(
                not item["authority_granted"]
                and not item["evidence_verified"]
                for item in final_state["dialogue"]
            ),
            "all collaboration turns remain non-authorizing and unverified",
        )

        legacy = build_task_request(
            case_id=case_id,
            case_revision=8,
            turn_index=4,
            message_id="phase6-legacy-request",
            task_packet_id="phase6-legacy-task",
            operation_id="phase6-legacy-operation",
            scope_digest=SCOPE_DIGEST,
            user_goal="Keep the Phase 4 wire shape compatible.",
        )
        expect(
            "collaboration_binding" not in legacy,
            "legacy TASK_REQUEST wire shape remains unchanged",
        )

    print("phase6 collaboration contract smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
