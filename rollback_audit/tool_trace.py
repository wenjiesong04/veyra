from __future__ import annotations

from typing import Any
from uuid import uuid4

from core.world_state import WorldStateStore
from interface.event_schema import utc_now_iso


class ToolTrace:
    def __init__(self, state_store: WorldStateStore | None = None) -> None:
        self.state_store = state_store

    def record(
        self,
        *,
        tool: str,
        action_type: str,
        target: Any,
        result: dict[str, Any],
        review: dict[str, Any] | None = None,
        approved_by: str | None = None,
    ) -> dict[str, Any]:
        snapshot = result.get("snapshot") if isinstance(result.get("snapshot"), dict) else None
        trace = {
            "trace_id": str(result.get("trace_id") or f"tool_{uuid4().hex[:12]}"),
            "tool": tool,
            "action_type": action_type,
            "target": target,
            "status": result.get("status"),
            "risk_level": (review or {}).get("risk_level"),
            "policy_decision": (review or {}).get("decision"),
            "approved_by": approved_by,
            "snapshot_id": snapshot.get("snapshot_id") if snapshot else result.get("snapshot_id"),
            "result_summary": self._summary(result),
            "recorded_at": utc_now_iso(),
            "details": self._details(result),
        }
        if self.state_store:
            self.state_store.append_jsonl("tool_call_log.jsonl", trace)
        return trace

    def _summary(self, result: dict[str, Any]) -> str:
        if result.get("reason"):
            return str(result["reason"])
        if result.get("stderr"):
            return str(result["stderr"]).strip()[:240]
        if result.get("stdout"):
            return str(result["stdout"]).strip()[:240]
        if result.get("operation"):
            return str(result["operation"])
        return str(result.get("status") or "unknown")

    def _details(self, result: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in result.items() if key != "tool_trace"}
