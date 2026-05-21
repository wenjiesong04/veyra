from __future__ import annotations

from typing import Any

from core.world_state import WorldStateStore
from interface.agent_adapter import AgentAdapter, ExecutionResult
from interface.agent_contract import NON_TERMINAL_STATUSES
from interface.event_schema import utc_now_iso
from rollback_audit.execution_trace import ExecutionTrace


class AgentTaskTracker:
    """Persists non-terminal Agent tasks and refreshes them later."""

    def __init__(self, state_store: WorldStateStore, execution_trace: ExecutionTrace | None = None) -> None:
        self.state_store = state_store
        self.execution_trace = execution_trace or ExecutionTrace(state_store)

    def register(
        self,
        *,
        event_id: str,
        route: str,
        execution: ExecutionResult,
        verification: dict[str, Any],
    ) -> dict[str, Any] | None:
        if execution.status not in NON_TERMINAL_STATUSES:
            return None
        task = {
            "task_id": execution.task_id,
            "event_id": event_id,
            "route": route,
            "executor": execution.executor,
            "status": execution.status,
            "verification_status": verification.get("status"),
            "next_action": verification.get("next_action"),
            "registered_at": utc_now_iso(),
            "last_polled_at": None,
            "poll_count": 0,
        }
        self._upsert_pending(task)
        return task

    def apply_result(
        self,
        *,
        execution: ExecutionResult,
        verification: dict[str, Any],
        event_id: str | None = None,
        route: str = "agent",
    ) -> dict[str, Any]:
        task_state = self.state_store.read_json("task_state.json")
        pending = task_state.setdefault("pending_agent_tasks", [])
        matched = False
        for item in pending:
            if item.get("task_id") != execution.task_id:
                continue
            item.update(
                {
                    "status": execution.status,
                    "verification_status": verification.get("status"),
                    "last_polled_at": utc_now_iso(),
                    "poll_count": int(item.get("poll_count") or 0) + 1,
                }
            )
            matched = True
        if not matched and execution.status in NON_TERMINAL_STATUSES:
            pending.append(
                {
                    "task_id": execution.task_id,
                    "event_id": event_id or f"task_{execution.task_id}",
                    "route": route,
                    "executor": execution.executor,
                    "status": execution.status,
                    "verification_status": verification.get("status"),
                    "next_action": verification.get("next_action"),
                    "registered_at": utc_now_iso(),
                    "last_polled_at": utc_now_iso(),
                    "poll_count": 1,
                }
            )
        if execution.status in {"success", "failed", "error", "blocked"}:
            pending = [item for item in pending if item.get("task_id") != execution.task_id]
        task_state["pending_agent_tasks"] = pending[-100:]
        task_state["current_task"] = {"task_id": execution.task_id, "route": route, "status": verification.get("status")}
        self.state_store.write_json("task_state.json", task_state)
        return {"pending_agent_tasks": task_state["pending_agent_tasks"], "matched": matched}

    def refresh_pending(self, adapter: AgentAdapter, verifier: Any, limit: int = 20) -> dict[str, Any]:
        pending = self.state_store.read_json("task_state.json").get("pending_agent_tasks", [])
        refreshed: list[dict[str, Any]] = []
        for item in list(pending)[:limit]:
            task_id = str(item.get("task_id") or "")
            if not task_id:
                continue
            execution = adapter.fetch_task_status(task_id)
            verification = verifier.verify_execution_result(execution)
            trace = self.execution_trace.record(
                {
                    "event_id": item.get("event_id") or f"poll_{task_id}",
                    "route": item.get("route") or "agent",
                    "task_id": execution.task_id,
                    "executor": execution.executor,
                    "status": verification["status"],
                    "execution_result": execution.to_dict(),
                    "verification": verification,
                }
            )
            self.apply_result(execution=execution, verification=verification, event_id=str(item.get("event_id") or ""), route=str(item.get("route") or "agent"))
            refreshed.append({"execution_result": execution.to_dict(), "verification": verification, "execution_trace": trace})
        return {"status": "success", "refreshed": refreshed, "remaining": self.state_store.read_json("task_state.json").get("pending_agent_tasks", [])}

    def _upsert_pending(self, task: dict[str, Any]) -> None:
        task_state = self.state_store.read_json("task_state.json")
        pending = task_state.setdefault("pending_agent_tasks", [])
        for index, item in enumerate(pending):
            if item.get("task_id") == task["task_id"]:
                pending[index] = {**item, **task}
                break
        else:
            pending.append(task)
        task_state["pending_agent_tasks"] = pending[-100:]
        self.state_store.write_json("task_state.json", task_state)
