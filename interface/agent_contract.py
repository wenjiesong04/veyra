from __future__ import annotations

import json
from typing import Any


AGENT_CONTRACT_VERSION = "veyra.agent_adapter.v1"

TERMINAL_STATUSES = {"success", "failed", "error", "adapter_unconfigured", "timeout", "blocked"}
NON_TERMINAL_STATUSES = {"submitted", "running", "pending"}
KNOWN_STATUSES = TERMINAL_STATUSES | NON_TERMINAL_STATUSES


def contract_summary() -> dict[str, Any]:
    return {
        "contract_version": AGENT_CONTRACT_VERSION,
        "task_packet": {
            "required": ["task_id", "target_agent", "session_id", "user_message", "context_patch", "persona_patch", "policy_patch"],
            "transport": "structured_json_with_rendered_prompt_fallback",
        },
        "execution_result": {
            "required": ["task_id", "executor", "status", "result"],
            "optional": ["logs", "changed_files", "tool_calls", "raw"],
            "known_statuses": sorted(KNOWN_STATUSES),
        },
        "capabilities": {
            "required": ["runtime", "status", "connected", "tools", "skills", "requires_tool_proxy"],
        },
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
    return errors


def render_prompt_payload(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"Veyra Agent Contract: {AGENT_CONTRACT_VERSION}",
            f"Task ID: {payload.get('task_id', '')}",
            f"Session ID: {payload.get('session_id', '')}",
            f"User Task: {payload.get('user_message', '')}",
            "Context Patch:",
            json.dumps(payload.get("context_patch", {}), ensure_ascii=False, indent=2, sort_keys=True),
            "Persona Patch:",
            json.dumps(payload.get("persona_patch", {}), ensure_ascii=False, indent=2, sort_keys=True),
            "Policy Patch:",
            json.dumps(payload.get("policy_patch", {}), ensure_ascii=False, indent=2, sort_keys=True),
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
        "features": {
            "structured_task_packet": True,
            "rendered_prompt_fallback": True,
            "memory_summary": True,
            "memory_patch": True,
            "stop_task": True,
            **(data.get("features") if isinstance(data.get("features"), dict) else {}),
        },
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
