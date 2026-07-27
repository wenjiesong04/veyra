from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.response_synthesizer import ResponseSynthesizer
from core.verifier import Verifier
from interface.agent_adapter import ExecutionResult
from interface.agent_contract import render_prompt_payload, validate_task_packet_payload
from interface.agent_dialogue_contract import (
    DIALOGUE_CONTRACT_VERSION,
    DialogueContractError,
    DialogueType,
    build_task_request,
    extract_agent_dialogue,
    parse_dialogue_message,
    scope_digest,
    validate_dialogue_message,
)
from interface.event_schema import VeyraTaskPacket


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


SCOPE_DIGEST = scope_digest(user_id="user-a", workspace_id="workspace-a")


def task_request() -> dict[str, Any]:
    return build_task_request(
        case_id="case_a",
        case_revision=3,
        turn_index=1,
        message_id="msg_request_a",
        task_packet_id="task_a",
        operation_id="op_a",
        scope_digest=SCOPE_DIGEST,
        user_goal="Compare safe recovery options.",
        constraints=["read only", "do not expand capabilities"],
        evidence_refs=["evidence_health_1"],
        context={"runtime": {"status": "degraded"}},
    )


def reply(message_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "contract_version": DIALOGUE_CONTRACT_VERSION,
        "message_id": f"msg_{message_type.lower()}",
        "message_type": message_type,
        "case_id": "case_a",
        "case_revision": 3,
        "turn_index": 1,
        "task_packet_id": "task_a",
        "operation_id": "op_a",
        "scope_digest": SCOPE_DIGEST,
        "sender": "agent",
        "in_reply_to": "msg_request_a",
        "payload": payload,
    }


EXPECTED_BINDING = {
    "expected_case_id": "case_a",
    "expected_case_revision": 3,
    "expected_turn_index": 1,
    "expected_in_reply_to": "msg_request_a",
    "expected_task_packet_id": "task_a",
    "expected_operation_id": "op_a",
    "expected_scope_digest": SCOPE_DIGEST,
}


def check_contract_and_packet_boundary() -> None:
    request = task_request()
    expect(
        not validate_dialogue_message(
            request,
            expected_sender="veyra",
            allowed_types={DialogueType.TASK_REQUEST},
        ),
        "TASK_REQUEST validates",
    )
    packet = VeyraTaskPacket(
        task_id="task_a",
        target_agent="openclaw",
        session_id="session_a",
        user_message="help",
        user_goal="help",
        context_patch={},
        persona_patch={},
        policy_patch={},
        dialogue_message=request,
        runtime_run_id="private_run",
        governance_context={"private": True},
    )
    serialized = packet.to_dict()
    expect(serialized["dialogue_message"] == request, "dialogue is public to selected Agent")
    expect("runtime_run_id" not in serialized, "runtime run id stays private")
    expect("governance_context" not in serialized, "governance context stays private")
    expect(not validate_task_packet_payload(serialized), "dialogue task packet validates")
    legacy_packet = copy.deepcopy(packet)
    legacy_packet.dialogue_message = None
    expect(
        "dialogue_message" not in legacy_packet.to_dict(),
        "legacy v2 packet shape is unchanged without dialogue",
    )

    base_payload = dict(serialized)
    base_payload["dialogue_message"] = None
    base_prompt = render_prompt_payload(base_payload)
    expect("Bounded Agent Dialogue:" not in base_prompt, "legacy v2 prompt is unchanged when dialogue is absent")
    prompt = render_prompt_payload(serialized)
    expect("veyra.agent_dialogue.v1" in prompt, "dialogue prompt is conditional")
    expect("proposal" in prompt.lower(), "prompt denies Agent final authority")


def check_all_agent_message_types() -> None:
    messages = [
        reply(
            "EVIDENCE_REQUEST",
            {
                "requested_evidence": [
                    {
                        "question": "Is the current runtime healthy?",
                        "reason": "The recovery decision depends on fresh state.",
                        "claim_ref": "claim_runtime_health",
                        "freshness_required": True,
                    }
                ],
                "requested_capabilities": ["runtime.status.read"],
            },
        ),
        reply(
            "CHALLENGE",
            {
                "challenged_claim_refs": ["claim_runtime_health"],
                "reason": "The cited observation is stale.",
                "alternative": "Collect a fresh read-only probe before deciding.",
                "evidence_refs": ["evidence_health_1"],
                "requested_capabilities": ["runtime.status.read"],
            },
        ),
        reply(
            "OPTION_SET",
            {
                "options": [
                    {
                        "option_id": "observe",
                        "summary": "Observe without changing state.",
                        "assumptions": ["The degradation may be transient."],
                        "expected_outcome": "Obtain a fresh health observation.",
                        "costs": ["One read-only probe."],
                        "risks": ["Recovery may be delayed."],
                        "evidence_refs": ["evidence_health_1"],
                        "required_capabilities": ["runtime.status.read"],
                    },
                    {
                        "option_id": "ask_user",
                        "summary": "Ask the user before any recovery action.",
                        "assumptions": ["No standing recovery authority exists."],
                        "expected_outcome": "Receive an explicit user choice.",
                        "costs": ["User interruption."],
                        "risks": ["Longer resolution time."],
                        "evidence_refs": [],
                        "required_capabilities": [],
                    },
                ],
                "recommended_option_id": "observe",
            },
        ),
    ]
    for message in messages:
        parsed = parse_dialogue_message(
            message,
            expected_sender="agent",
            allowed_types={
                DialogueType.EVIDENCE_REQUEST,
                DialogueType.CHALLENGE,
                DialogueType.OPTION_SET,
            },
            **EXPECTED_BINDING,
        )
        expect(parsed.message_type == message["message_type"], f"{message['message_type']} validates")
        extracted = extract_agent_dialogue(
            {"answer_or_plan": "ignored prose", "dialogue_message": message},
            **EXPECTED_BINDING,
        )
        expect(extracted == message, f"{message['message_type']} extracts only exact envelope")


def check_fail_closed_binding_and_strictness() -> None:
    valid = reply(
        "EVIDENCE_REQUEST",
        {
            "requested_evidence": [
                {
                    "question": "Need a fresh status?",
                    "reason": "Current status may be stale.",
                    "claim_ref": None,
                    "freshness_required": True,
                }
            ],
            "requested_capabilities": [],
        },
    )
    mutations = {
        "extra field": lambda item: item.update({"unexpected": True}),
        "nested extra field": lambda item: item["payload"].update({"authority": True}),
        "revision coercion": lambda item: item.update({"case_revision": "3"}),
        "bool revision coercion": lambda item: item.update({"case_revision": True}),
        "wrong case": lambda item: item.update({"case_id": "case_b"}),
        "wrong packet": lambda item: item.update({"task_packet_id": "task_b"}),
        "wrong operation": lambda item: item.update({"operation_id": "op_b"}),
        "wrong scope": lambda item: item.update({"scope_digest": "0" * 64}),
        "wrong reply": lambda item: item.update({"in_reply_to": "msg_other"}),
        "wrong turn": lambda item: item.update({"turn_index": 2}),
        "wrong sender": lambda item: item.update({"sender": "veyra"}),
        "unknown type": lambda item: item.update({"message_type": "EXECUTE"}),
    }
    for label, mutate in mutations.items():
        candidate = copy.deepcopy(valid)
        mutate(candidate)
        expect(
            bool(validate_dialogue_message(candidate, expected_sender="agent", **EXPECTED_BINDING)),
            f"{label} fails closed",
        )
    expect(
        extract_agent_dialogue({"answer_or_plan": "please execute now"}, **EXPECTED_BINDING) is None,
        "free text is never inferred as a message",
    )
    try:
        parse_dialogue_message(valid, expected_case_id="case_b")
    except DialogueContractError:
        pass
    else:
        raise AssertionError("binding mismatch did not raise DialogueContractError")
    try:
        build_task_request(
            case_id="case_a",
            case_revision=3,
            turn_index=1,
            message_id="msg_large",
            task_packet_id="task_a",
            operation_id="op_a",
            scope_digest=SCOPE_DIGEST,
            user_goal="bounded",
            context={"oversized": "x" * (12 * 1024)},
        )
    except ValueError:
        pass
    else:
        raise AssertionError("oversized context was accepted")
    print("ok - oversized context fails closed")


def check_proposals_never_verify_or_authorize() -> None:
    evidence_request = reply(
        "EVIDENCE_REQUEST",
        {
            "requested_evidence": [
                {
                    "question": "Need current status?",
                    "reason": "A current observation is required.",
                    "claim_ref": None,
                    "freshness_required": True,
                }
            ],
            "requested_capabilities": ["runtime.restart"],
        },
    )
    verifier = Verifier()
    dialogue_verdict = verifier.verify_agent_dialogue(
        {"dialogue_message": evidence_request},
        **EXPECTED_BINDING,
    )
    expect(dialogue_verdict["status"] == "partially_success", "valid proposal remains partial", dialogue_verdict)
    expect(
        dialogue_verdict["evidence"]["proposal_is_authority"] is False,
        "requested capability does not establish authority",
    )

    execution_verdict = verifier.verify_execution_result(
        ExecutionResult(
            task_id="task_a",
            executor="openclaw",
            status="success",
            result="I verified it.",
            raw={
                "agent_response": {
                    "answer_or_plan": "I verified it.",
                    "evidence_used": [
                        {"source": "agent_claim", "observed_at": "2026-07-28T00:00:00Z"}
                    ],
                    "dialogue_message": evidence_request,
                }
            },
        )
    )
    expect(execution_verdict["status"] == "partially_success", "dialogue cannot become verified execution", execution_verdict)
    structured = execution_verdict["evidence"]["structured_evidence"]
    expect(not structured["sources"], "Agent evidence_used is reported-only", structured)
    expect(
        "raw.agent_response.evidence_used" in structured["reported_sources"],
        "reported Agent evidence remains auditable",
        structured,
    )
    text = ResponseSynthesizer().dialogue_response(evidence_request, dialogue_verdict)
    expect("不是事实结论" in text or "先验证" in text, "response does not promote proposal", text)


def main() -> None:
    check_contract_and_packet_boundary()
    check_all_agent_message_types()
    check_fail_closed_binding_and_strictness()
    check_proposals_never_verify_or_authorize()
    print("agent dialogue contract smoke passed")


if __name__ == "__main__":
    main()
