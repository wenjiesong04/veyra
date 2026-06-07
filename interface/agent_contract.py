from __future__ import annotations

import json
from typing import Any

from interface.agent_compatibility import compatibility_policy_summary, evaluate_agent_compatibility


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
    return errors


def render_prompt_payload(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"Veyra Agent Contract: {AGENT_CONTRACT_VERSION}",
            "You are an Agent Runtime operating under Veyra governance.",
            "You are not the final authority. Veyra owns policy, memory, confirmation, verification, and delivery.",
            "Your job is to analyze the user goal and provided awareness/context, then return a proposal or bounded low-risk result.",
            "You may reason deeply, synthesize context, inspect provided workspace/context when allowed, propose actions, and perform only actions explicitly allowed by capability_request and policy_patch.",
            "You must not bypass Veyra, assume user approval for risky actions, claim unavailable capabilities, invent runtime state, or treat stale awareness as current fact.",
            f"Task ID: {payload.get('task_id', '')}",
            f"Session ID: {payload.get('session_id', '')}",
            f"User Goal: {payload.get('user_goal') or payload.get('user_message', '')}",
            f"Required Capabilities: {json.dumps(payload.get('required_capabilities', []), ensure_ascii=False)}",
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
                    "memory_recommendation": "none|read|write_candidate",
                    "confidence": 0.0,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
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
        **(data.get("features") if isinstance(data.get("features"), dict) else {}),
    }
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
