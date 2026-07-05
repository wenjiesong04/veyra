from __future__ import annotations

from typing import Any


def clip_text(value: Any, limit: int = 320) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def compact_guardian_decision(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    compact = {
        "decision": value.get("decision"),
        "risk_level": value.get("risk_level"),
        "reason": clip_text(value.get("reason"), 400),
    }
    preconditions = value.get("required_preconditions")
    if isinstance(preconditions, list) and preconditions:
        compact["required_preconditions"] = [clip_text(item, 160) for item in preconditions[:5]]
    forbidden = value.get("forbidden")
    if isinstance(forbidden, list) and forbidden:
        compact["forbidden"] = [clip_text(item, 120) for item in forbidden[:5]]
    return {key: item for key, item in compact.items() if item not in (None, "", [])}


def compact_foresight(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    compact = {
        "risk_level": value.get("risk_level"),
        "reversible": value.get("reversible"),
        "impact_summary": clip_text(value.get("impact_summary"), 400),
    }
    side_effects = value.get("side_effects")
    if isinstance(side_effects, list) and side_effects:
        compact["side_effects"] = [clip_text(item, 160) for item in side_effects[:5]]
    safer = value.get("safer_alternatives")
    if isinstance(safer, list) and safer:
        compact["safer_alternatives"] = [clip_text(item, 160) for item in safer[:5]]
    return {key: item for key, item in compact.items() if item not in (None, "", [])}


def compact_intention(item: dict[str, Any]) -> dict[str, Any]:
    compact = dict(item)
    if "guardian_decision" in compact:
        compact["guardian_decision"] = compact_guardian_decision(compact.get("guardian_decision"))
    if "foresight" in compact:
        compact["foresight"] = compact_foresight(compact.get("foresight"))
    source_gap = compact.get("source_gap")
    if isinstance(source_gap, dict):
        compact["source_gap"] = {
            "gap_id": source_gap.get("gap_id"),
            "target": source_gap.get("target"),
            "observed_status": source_gap.get("observed_status"),
            "risk_level": source_gap.get("risk_level"),
            "suggested_action": source_gap.get("suggested_action"),
            "action_text": clip_text(source_gap.get("action_text"), 240),
        }
    return compact


def compact_review_item(item: dict[str, Any]) -> dict[str, Any]:
    compact = dict(item)
    compact["guardian_decision"] = compact_guardian_decision(compact.get("guardian_decision"))
    compact["foresight"] = compact_foresight(compact.get("foresight"))
    execution = compact.get("execution_result")
    if isinstance(execution, dict):
        compact["execution_result"] = {
            key: execution.get(key)
            for key in ("status", "returncode", "approved_by", "reason")
            if execution.get(key) is not None
        }
        stdout = str(execution.get("stdout") or "")
        if stdout:
            compact["execution_result"]["stdout_preview"] = clip_text(stdout, 240)
    return compact


def compact_persona_binding(binding: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(binding, dict):
        return {}
    return {
        "event_id": binding.get("event_id"),
        "channel": binding.get("channel"),
        "session_id": binding.get("session_id"),
        "active_modes": binding.get("active_modes"),
        "route": binding.get("route"),
        "target_agent": binding.get("target_agent"),
        "risk_level": binding.get("risk_level"),
        "response_style": binding.get("response_style"),
        "context_budget_chars": binding.get("context_budget_chars"),
        "guideline_count": binding.get("guideline_count"),
        "updated_at": binding.get("updated_at"),
    }


def compact_channel_inbox_item(item: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {}
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    feishu = metadata.get("feishu") if isinstance(metadata.get("feishu"), dict) else {}
    compact_metadata: dict[str, Any] = {}
    if feishu:
        compact_metadata["feishu"] = {
            "message_id": feishu.get("message_id"),
            "chat_id": feishu.get("chat_id"),
            "chat_type": feishu.get("chat_type"),
            "message_type": feishu.get("message_type"),
        }
    return {
        "message_id": item.get("message_id"),
        "event_id": item.get("event_id"),
        "channel": item.get("channel"),
        "user_id": item.get("user_id"),
        "session_id": item.get("session_id"),
        "text": clip_text(item.get("text"), 500),
        "metadata": compact_metadata,
        "received_at": item.get("received_at"),
    }


def compact_channel_outbox_item(item: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {}
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    compact = {
        "channel": item.get("channel"),
        "session_id": item.get("session_id"),
        "message": clip_text(item.get("message"), 500),
        "status": item.get("status"),
        "delivery": item.get("delivery"),
        "delivery_status": item.get("delivery_status"),
        "provider": item.get("provider"),
        "external_message_id": item.get("external_message_id"),
        "created_at": item.get("created_at"),
    }
    if metadata:
        compact["metadata"] = {
            "event_id": metadata.get("event_id"),
            "message_id": metadata.get("message_id"),
            "route": metadata.get("route"),
            "status": metadata.get("status"),
            "message_type": metadata.get("message_type"),
            "message_index": metadata.get("message_index"),
            "message_count": metadata.get("message_count"),
            "commitment_id": metadata.get("commitment_id"),
            "kind": metadata.get("kind"),
            "push_reason": metadata.get("push_reason"),
        }
    return compact


def compact_tick_step_result(result: Any) -> Any:
    if not isinstance(result, dict):
        return result
    compact = {
        key: result.get(key)
        for key in ("status", "changed", "processed_count", "due_count", "created_count", "remaining_stale", "skipped")
        if key in result
    }
    if "refreshed" in result and isinstance(result["refreshed"], list):
        compact["refreshed"] = [
            {
                "claim": item.get("claim") if isinstance(item, dict) else None,
                "status": item.get("status") if isinstance(item, dict) else None,
            }
            for item in result["refreshed"][:5]
            if isinstance(item, dict)
        ]
    if "files" in result and isinstance(result["files"], list):
        compact["files"] = [
            {
                "file": item.get("file"),
                "status": item.get("status"),
                "pruned_entries": item.get("pruned_entries"),
            }
            for item in result["files"][:8]
            if isinstance(item, dict)
        ]
    return compact or {"status": result.get("status")}


def compact_active_loop_tick(tick: dict[str, Any]) -> dict[str, Any]:
    compact = {
        key: tick.get(key)
        for key in ("tick_id", "status", "reason", "started_at", "duration_ms", "error", "error_type")
        if tick.get(key) is not None
    }
    steps = tick.get("steps")
    if isinstance(steps, list):
        compact["steps"] = [
            {
                "name": step.get("name"),
                "status": step.get("status"),
                "result_status": step.get("result_status"),
                "duration_ms": step.get("duration_ms"),
                "result": compact_tick_step_result(step.get("result")) if isinstance(step.get("result"), dict) else step.get("result"),
            }
            for step in steps
            if isinstance(step, dict)
        ]
    return compact


def compact_action_record(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    compact = {
        key: payload.get(key)
        for key in ("timestamp", "event_id", "route", "status", "task_id", "trace_id")
        if payload.get(key) is not None
    }
    artifacts = payload.get("artifacts")
    if isinstance(artifacts, dict):
        compact["artifacts"] = _compact_action_artifacts(artifacts, route=str(payload.get("route") or ""))
    return compact


def _compact_action_artifacts(artifacts: dict[str, Any], *, route: str) -> dict[str, Any]:
    if route == "proactive_check":
        return {
            "status": artifacts.get("status"),
            "autonomy_level": artifacts.get("autonomy_level"),
            "duration_ms": artifacts.get("duration_ms"),
            "state_gaps": artifacts.get("state_gaps"),
            "intentions": [
                {
                    "intention_id": item.get("intention_id"),
                    "status": item.get("status"),
                    "suggested_action": item.get("suggested_action"),
                }
                for item in (artifacts.get("intentions") or [])[:10]
                if isinstance(item, dict)
            ],
            "results": {
                name: {
                    "probe": value.get("probe"),
                    "status": value.get("status"),
                    "summary": clip_text(value.get("summary"), 240),
                }
                for name, value in (artifacts.get("results") or {}).items()
                if isinstance(value, dict)
            },
        }
    if route == "active_loop_tick":
        return compact_active_loop_tick(artifacts) if isinstance(artifacts, dict) else artifacts
    if route == "active_loop_stop":
        return {
            key: artifacts.get(key)
            for key in ("status", "enabled", "loop_id", "updated_at", "interval_seconds")
            if artifacts.get(key) is not None
        }
    if route == "human_review":
        review = artifacts.get("review") if isinstance(artifacts.get("review"), dict) else artifacts
        if isinstance(review, dict):
            return {"review": compact_review_item(review)}
    if route == "ops_retention_enforce":
        return {
            "status": artifacts.get("status"),
            "dry_run": artifacts.get("dry_run"),
            "changed": artifacts.get("changed"),
            "files": [
                {
                    "file": item.get("file"),
                    "status": item.get("status"),
                    "pruned_entries": item.get("pruned_entries"),
                    "archive_path": item.get("archive_path"),
                }
                for item in (artifacts.get("files") or [])[:12]
                if isinstance(item, dict)
            ],
        }
    compact: dict[str, Any] = {}
    for key, value in artifacts.items():
        if key in {"guardian", "decision", "controller", "persona", "verification", "review", "execution_result", "tool_trace", "execution_trace"}:
            compact[key] = _compact_generic(value)
        elif key in {"loop_id", "interval_seconds", "job_id", "reason", "result_status", "task_id", "validation", "iterations"}:
            compact[key] = value
        elif isinstance(value, (str, int, float, bool)) or value is None:
            compact[key] = clip_text(value, 400) if isinstance(value, str) else value
        elif isinstance(value, dict):
            compact[key] = _compact_generic(value)
        elif isinstance(value, list):
            compact[key] = value[:5]
    return compact


def _compact_generic(value: Any, *, depth: int = 0) -> Any:
    if depth > 2:
        return "<truncated>"
    if isinstance(value, dict):
        if "policy" in value and isinstance(value.get("policy"), dict):
            return compact_guardian_decision(value)
        if "risk_level" in value and "impact_summary" in value:
            return compact_foresight(value)
        return {
            key: _compact_generic(item, depth=depth + 1)
            for key, item in list(value.items())[:20]
            if key not in {"raw", "details", "claims", "model_assist", "artifacts"}
        }
    if isinstance(value, list):
        return [_compact_generic(item, depth=depth + 1) for item in value[:5]]
    if isinstance(value, str):
        return clip_text(value, 400)
    return value
