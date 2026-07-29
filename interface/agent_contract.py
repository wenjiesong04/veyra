from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from interface.agent_compatibility import compatibility_policy_summary, evaluate_agent_compatibility
from interface.agent_dialogue_contract import (
    DialogueType,
    collaboration_turn_transport_identity,
    dialogue_contract_summary,
    dump_dialogue_message,
    expected_agent_reply_message_id,
    parse_dialogue_message,
    validate_dialogue_message,
)


AGENT_CONTRACT_VERSION = "veyra.agent_adapter.v2"

TERMINAL_STATUSES = {"success", "failed", "error", "adapter_unconfigured", "timeout", "blocked"}
NON_TERMINAL_STATUSES = {"submitted", "running", "pending"}
KNOWN_STATUSES = TERMINAL_STATUSES | NON_TERMINAL_STATUSES

def contract_summary() -> dict[str, Any]:
    return {
        "contract_version": AGENT_CONTRACT_VERSION,
        "task_packet": {
            "required": [
                "task_id",
                "target_agent",
                "session_id",
                "user_message",
                "user_goal",
                "required_capabilities",
                "context_patch",
                "persona_patch",
                "policy_patch",
                "verification_policy",
                "rollback_requirement",
                "memory_policy",
            ],
            "optional": ["dialogue_message"],
            "transport": "structured_json_with_rendered_prompt_fallback",
        },
        "execution_result": {
            "required": ["task_id", "executor", "status", "result"],
            "optional": ["logs", "changed_files", "tool_calls", "raw"],
            "tool_proxy_evidence": [
                "raw.action_proposals",
                "raw.tool_proxy_traces",
                "raw.policy_trace",
                "raw.review_id",
                "raw.approved_by",
            ],
            "known_statuses": sorted(KNOWN_STATUSES),
        },
        "capabilities": {
            "required": ["runtime", "status", "connected", "tools", "skills", "requires_tool_proxy", "compatibility"],
        },
        "compatibility": compatibility_policy_summary(),
        "dialogue": dialogue_contract_summary(),
    }


def validate_task_packet_payload(payload: dict[str, Any]) -> list[str]:
    required = contract_summary()["task_packet"]["required"]
    errors: list[str] = []
    for key in required:
        if key not in payload:
            errors.append(f"missing task_packet.{key}")
    for key in ["context_patch", "persona_patch", "policy_patch"]:
        if key in payload and not isinstance(payload[key], dict):
            errors.append(f"task_packet.{key} must be an object")
    if "required_capabilities" in payload and not isinstance(payload["required_capabilities"], list):
        errors.append("task_packet.required_capabilities must be a list")
    for key in ["verification_policy", "rollback_requirement"]:
        if key in payload and not isinstance(payload[key], dict):
            errors.append(f"task_packet.{key} must be an object")
    if payload.get("dialogue_message") is not None:
        if not isinstance(payload["dialogue_message"], dict):
            errors.append("task_packet.dialogue_message must be an object")
        else:
            dialogue_errors = [
                f"task_packet.dialogue_message {error}"
                for error in validate_dialogue_message(
                    payload["dialogue_message"],
                    expected_sender="veyra",
                    allowed_types={
                        DialogueType.TASK_REQUEST,
                        DialogueType.CONTEXT_PATCH,
                        DialogueType.PLAN_SELECTION,
                    },
                )
            ]
            errors.extend(dialogue_errors)
            if not dialogue_errors:
                parsed_dialogue = parse_dialogue_message(
                    payload["dialogue_message"],
                    expected_sender="veyra",
                    allowed_types={
                        DialogueType.TASK_REQUEST,
                        DialogueType.CONTEXT_PATCH,
                        DialogueType.PLAN_SELECTION,
                    },
                )
                binding = parsed_dialogue.collaboration_binding
                required_capabilities = payload.get(
                    "required_capabilities",
                    [],
                )
                if (
                    binding is not None
                    and isinstance(required_capabilities, list)
                ):
                    now = datetime.now(timezone.utc)
                    if binding.issued_at > now:
                        errors.append(
                            "dialogue collaboration binding is not active yet"
                        )
                    if binding.expires_at <= now:
                        errors.append(
                            "dialogue collaboration binding has expired"
                        )
                    if not all(
                        isinstance(item, str)
                        for item in required_capabilities
                    ):
                        errors.append(
                            "task_packet.required_capabilities must contain "
                            "strings"
                        )
                    elif not set(required_capabilities).issubset(
                        set(binding.capability_scope)
                    ):
                        errors.append(
                            "task_packet.required_capabilities exceeds "
                            "dialogue collaboration capability scope"
                        )
                    if payload.get("task_id") != parsed_dialogue.task_packet_id:
                        errors.append(
                            "task_packet.task_id must exactly match the "
                            "dialogue task_packet_id"
                        )
                    if payload.get("target_agent") != binding.provider.runtime:
                        errors.append(
                            "task_packet.target_agent must exactly match the "
                            "collaboration provider runtime"
                        )
                    dialogue_type = DialogueType(
                        parsed_dialogue.message_type
                    )
                    expected_context = (
                        parsed_dialogue.payload.context
                        if dialogue_type
                        in {
                            DialogueType.TASK_REQUEST,
                            DialogueType.CONTEXT_PATCH,
                        }
                        else {}
                    )
                    if payload.get("context_patch") != expected_context:
                        errors.append(
                            "task_packet.context_patch must exactly match the "
                            "bounded dialogue context"
                        )
                    if (
                        _canonical_json_size(expected_context)
                        > binding.budget.max_context_bytes
                    ):
                        errors.append(
                            "task_packet.context_patch exceeds the "
                            "collaboration context budget"
                        )
                    if payload.get("persona_patch") != {}:
                        errors.append(
                            "bound collaboration persona_patch must be empty"
                        )
                    if payload.get("policy_patch") != {}:
                        errors.append(
                            "bound collaboration policy_patch must be empty"
                        )
                    if payload.get("verification_policy") != {}:
                        errors.append(
                            "bound collaboration verification_policy must be "
                            "empty"
                        )
                    if payload.get("rollback_requirement") != {}:
                        errors.append(
                            "bound collaboration rollback_requirement must be "
                            "empty"
                        )
                    if payload.get("memory_policy") != "forget":
                        errors.append(
                            "bound collaboration memory_policy must be forget"
                        )
                    transport_identity = (
                        collaboration_turn_transport_identity(
                            payload["dialogue_message"]
                        )
                    )
                    if (
                        payload.get("session_id")
                        != transport_identity["packet_session_id"]
                    ):
                        errors.append(
                            "task_packet.session_id must match the exact "
                            "bound collaboration turn"
                        )
                    if (
                        payload.get("agent_execution_session_id")
                        != transport_identity[
                            "agent_execution_session_id"
                        ]
                    ):
                        errors.append(
                            "task_packet.agent_execution_session_id must "
                            "match the exact bound collaboration turn"
                        )
                    if (
                        payload.get("agent_session_policy")
                        != "ephemeral_per_task"
                    ):
                        errors.append(
                            "bound collaboration agent_session_policy must "
                            "be ephemeral_per_task"
                        )
                    expected_user_text = (
                        parsed_dialogue.payload.user_goal
                        if dialogue_type == DialogueType.TASK_REQUEST
                        else ""
                    )
                    if payload.get("user_goal") != expected_user_text:
                        errors.append(
                            "task_packet.user_goal must exactly match the "
                            "bounded dialogue goal"
                        )
                    if payload.get("user_message") != expected_user_text:
                        errors.append(
                            "task_packet.user_message must exactly match the "
                            "bounded dialogue goal"
                        )
    return errors


def _canonical_json_size(value: Any) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )


def render_prompt_payload(payload: dict[str, Any]) -> str:
    dialogue = payload.get("dialogue_message")
    bound_collaboration = (
        isinstance(dialogue, dict)
        and isinstance(dialogue.get("collaboration_binding"), dict)
    )
    sections = [
            f"Veyra Agent Contract: {AGENT_CONTRACT_VERSION}",
            "You are an Agent Runtime operating under Veyra governance.",
            "You are not the final authority. Veyra owns policy, memory, confirmation, verification, and delivery.",
            "Your job is to analyze the user goal and provided awareness/context, then return a proposal or bounded low-risk result.",
            "You may reason deeply, synthesize context, inspect provided workspace/context when allowed, propose actions, and perform only actions explicitly allowed by capability_request and policy_patch.",
            "You must not bypass Veyra, assume user approval for risky actions, claim unavailable capabilities, invent runtime state, or treat stale awareness as current fact.",
            f"Task ID: {payload.get('task_id', '')}",
            (
                "Session Scope: isolated ephemeral collaboration turn"
                if bound_collaboration
                else f"Session ID: {payload.get('session_id', '')}"
            ),
            f"User Goal: {payload.get('user_goal') or payload.get('user_message', '')}",
            f"Required Capabilities: {json.dumps(payload.get('required_capabilities', []), ensure_ascii=False)}",
    ]
    if bound_collaboration:
        sections.extend(
            [
                "Expected Response: exactly one strict JSON object with the "
                "single top-level key dialogue_message, using the bounded "
                "envelope below. Do not return the ordinary Agent response "
                "schema or any prose outside that JSON object.",
            ]
        )
    else:
        sections.extend(
            [
                "Expected Structured JSON Response:",
                json.dumps(
                    {
                        "agent_understanding": "",
                        "answer_or_plan": "",
                        "evidence_used": [],
                        "evidence_needed": [],
                        "proposed_actions": [
                            {
                                "action": "",
                                "risk_level": "",
                                "requires_confirmation": True,
                                "reversible": True,
                                "reason": "",
                            }
                        ],
                        "verification_steps": [],
                        "rollback_notes": "",
                        "memory_recommendation": (
                            "none|read|write_candidate"
                        ),
                        "confidence": 0.0,
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                ),
            ]
        )
    sections.extend(
        [
            "Context Patch:",
            json.dumps(payload.get("context_patch", {}), ensure_ascii=False, indent=2, sort_keys=True),
            "Persona Patch:",
            json.dumps(payload.get("persona_patch", {}), ensure_ascii=False, indent=2, sort_keys=True),
            "Policy Patch:",
            json.dumps(payload.get("policy_patch", {}), ensure_ascii=False, indent=2, sort_keys=True),
            "Verification Policy:",
            json.dumps(payload.get("verification_policy", {}), ensure_ascii=False, indent=2, sort_keys=True),
            "Rollback Requirement:",
            json.dumps(payload.get("rollback_requirement", {}), ensure_ascii=False, indent=2, sort_keys=True),
            f"Memory Policy: {payload.get('memory_policy', 'forget')}",
        ]
    )
    if dialogue is not None:
        outbound = parse_dialogue_message(
            dialogue,
            expected_sender="veyra",
            allowed_types={
                DialogueType.TASK_REQUEST,
                DialogueType.CONTEXT_PATCH,
                DialogueType.PLAN_SELECTION,
            },
        )
        request_payload = dump_dialogue_message(outbound)
        outbound_type = DialogueType(outbound.message_type)
        if outbound_type == DialogueType.PLAN_SELECTION:
            sections.extend(
                [
                    "Bounded Agent Dialogue:",
                    "This task carries a non-authorizing PLAN_SELECTION under veyra.agent_dialogue.v1.",
                    "Treat it only as Veyra's bounded plan choice. It does not grant execution, tool, verification, memory, or capability authority.",
                    "Do not emit a follow-up dialogue_message for this terminal analysis-only selection.",
                    "Plan Selection:",
                    json.dumps(
                        request_payload,
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    ),
                ]
            )
            return "\n".join(sections)
        reply_envelope = {
            "contract_version": request_payload["contract_version"],
            "message_id": expected_agent_reply_message_id(
                request_payload
            ),
            "message_type": "EVIDENCE_REQUEST",
            "case_id": request_payload["case_id"],
            "case_revision": request_payload["case_revision"],
            "turn_index": request_payload["turn_index"],
            "task_packet_id": request_payload["task_packet_id"],
            "operation_id": request_payload["operation_id"],
            "scope_digest": request_payload["scope_digest"],
            "sender": "agent",
            "in_reply_to": request_payload["message_id"],
            "payload": {},
        }
        if request_payload.get("collaboration_binding") is not None:
            reply_envelope["collaboration_binding"] = request_payload[
                "collaboration_binding"
            ]
        collaboration_binding = request_payload.get(
            "collaboration_binding"
        )
        evidence_scope = (
            list(collaboration_binding.get("evidence_scope") or [])
            if isinstance(collaboration_binding, dict)
            else []
        )
        reply_payload_shapes = {
            "EVIDENCE_REQUEST": {
                "requested_evidence": [
                    {
                        "request_id": "evidence_request_1",
                        "question": "bounded question",
                        "reason": "why this evidence is needed",
                        "claim_ref": "optional_claim_ref_or_null",
                        "freshness_required": True,
                    }
                ],
                "requested_capabilities": [],
            },
            "CHALLENGE": {
                "challenged_claim_refs": (
                    [evidence_scope[0]]
                    if evidence_scope
                    else ["claim_ref"]
                ),
                "reason": "bounded reason",
                "alternative": "bounded alternative",
                "evidence_refs": [],
                "requested_capabilities": [],
            },
            "OPTION_SET": {
                "options": [
                    {
                        "option_id": "option_1",
                        "summary": "bounded summary",
                        "assumptions": [],
                        "expected_outcome": "bounded outcome",
                        "costs": [],
                        "risks": [],
                        "evidence_refs": [],
                        "required_capabilities": [],
                    },
                    {
                        "option_id": "option_2",
                        "summary": "bounded summary",
                        "assumptions": [],
                        "expected_outcome": "bounded outcome",
                        "costs": [],
                        "risks": [],
                        "evidence_refs": [],
                        "required_capabilities": [],
                    },
                ],
                "recommended_option_id": None,
            },
        }
        sections.extend(
            [
                "Bounded Agent Dialogue:",
                (
                    "This task participates in veyra.agent_dialogue.v1. "
                    "Return exactly one JSON object whose only top-level "
                    "field is dialogue_message."
                    if bound_collaboration
                    else "This task participates in "
                    "veyra.agent_dialogue.v1. Return the normal structured "
                    "response with exactly one dialogue_message envelope."
                ),
                "Choose exactly one of EVIDENCE_REQUEST, CHALLENGE, or OPTION_SET. Do not invent other message types.",
                "Use the exact preallocated message_id shown in the reply envelope. Echo case_id, case_revision, task_packet_id, operation_id, scope_digest, and in_reply_to exactly. A capability field is only a request; it is not permission.",
                (
                    "Echo collaboration_binding byte-for-field exactly; do not "
                    "change participant, role, parent, handoff, capability, "
                    "evidence, privacy, effect, provider, budget, or expiry."
                    if request_payload.get("collaboration_binding") is not None
                    else "This legacy request has no collaboration_binding; do not introduce one."
                ),
                *(
                    [
                        (
                            "This bound collaboration has no tool, workspace, "
                            "memory, or capability authority. Do not call "
                            "tools. Keep every requested_capabilities and "
                            "required_capabilities field empty."
                        ),
                        (
                            "Every non-null claim_ref and every evidence_refs "
                            "item must be an exact member of "
                            "collaboration_binding.evidence_scope. For "
                            "CHALLENGE, challenged_claim_refs is required, "
                            "must contain at least one item, and every item "
                            "must be an exact member of that evidence_scope. "
                            "Request IDs are not evidence references."
                        ),
                    ]
                    if request_payload.get("collaboration_binding")
                    is not None
                    else []
                ),
                "Your message is a proposal. It cannot establish facts, authorization, execution success, or verification.",
                (
                    "Context Patch:"
                    if outbound_type == DialogueType.CONTEXT_PATCH
                    else "Task Request:"
                ),
                json.dumps(request_payload, ensure_ascii=False, indent=2, sort_keys=True),
                "Dialogue Reply Envelope (replace message_type and payload together):",
                json.dumps(
                    {"dialogue_message": reply_envelope},
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                ),
                "Allowed Strict Payload Shapes (choose exactly one; do not add fields):",
                json.dumps(
                    reply_payload_shapes,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                ),
            ]
        )
    return "\n".join(sections)


def normalize_capabilities(
    raw: dict[str, Any] | None,
    *,
    runtime: str,
    base_url: str | None = None,
    default_status: str = "adapter_unconfigured",
) -> dict[str, Any]:
    data = dict(raw or {})
    status = str(data.get("status") or default_status)
    connected = bool(data.get("connected")) if "connected" in data else status in {"available", "ok", "success"}
    features = {
        "structured_task_packet": True,
        "rendered_prompt_fallback": True,
        "memory_summary": True,
        "memory_patch": True,
        "task_status": True,
        "stop_task": True,
        "bounded_agent_dialogue": bool(
            isinstance(data.get("features"), dict)
            and data["features"].get("bounded_agent_dialogue")
        ),
        **(data.get("features") if isinstance(data.get("features"), dict) else {}),
    }
    features["bounded_agent_dialogue"] = (
        features.get("bounded_agent_dialogue") is True
    )
    compatibility = evaluate_agent_compatibility(
        data,
        runtime=str(data.get("runtime") or runtime),
        expected_contract_version=AGENT_CONTRACT_VERSION,
        connected=connected,
    )
    return {
        "contract_version": AGENT_CONTRACT_VERSION,
        "runtime": str(data.get("runtime") or runtime),
        "status": status,
        "connected": connected,
        "base_url": data.get("base_url", base_url),
        "protocol": data.get("protocol", "http_json"),
        "tools": _list(data.get("tools")),
        "skills": _list(data.get("skills")),
        "permissions": data.get("permissions", "unknown"),
        "requires_tool_proxy": bool(data.get("requires_tool_proxy", True)),
        "features": features,
        "compatibility": compatibility,
        "raw": data,
    }


def normalize_execution_payload(
    raw: dict[str, Any] | None,
    *,
    default_task_id: str,
    default_executor: str,
    default_status: str = "submitted",
) -> dict[str, Any]:
    data = dict(raw or {})
    status = str(data.get("status") or default_status)
    if status not in KNOWN_STATUSES:
        status = "success" if status in {"ok", "done", "completed"} else status
    return {
        "task_id": str(data.get("task_id") or data.get("run_id") or data.get("id") or default_task_id),
        "executor": str(data.get("executor") or default_executor),
        "status": status,
        "result": str(data.get("result") or data.get("message") or data.get("summary") or ""),
        "logs": _string(data.get("logs", "")),
        "changed_files": _list(data.get("changed_files")),
        "tool_calls": _list(data.get("tool_calls")),
        "raw": data,
    }


def _list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _string(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True) if value else ""
