from __future__ import annotations

from typing import Any
from uuid import uuid4

from core.world_state import WorldStateStore
from interface.event_schema import utc_now_iso


class ExecutionTrace:
    def __init__(self, state_store: WorldStateStore | None = None) -> None:
        self.state_store = state_store

    def record(self, payload: dict[str, Any]) -> dict[str, Any]:
        trace = {
            "trace_id": str(payload.get("trace_id") or f"exec_{uuid4().hex[:12]}"),
            "event_id": payload.get("event_id"),
            "route": payload.get("route"),
            "task_id": payload.get("task_id"),
            "executor": payload.get("executor"),
            "status": payload.get("status"),
            "decision": payload.get("decision"),
            "guardian": payload.get("guardian"),
            "execution_result": payload.get("execution_result"),
            "verification": payload.get("verification"),
            "artifacts": payload.get("artifacts", {}),
            "recorded_at": utc_now_iso(),
        }
        if self.state_store:
            self.state_store.append_jsonl("execution_trace.jsonl", trace)
        return trace
