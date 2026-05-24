from __future__ import annotations

from typing import Any

from core.world_state import WorldStateStore
from rollback_audit.action_journal import ActionJournal
from rollback_audit.compensation import Compensation


class Replay:
    def __init__(self, state_store: WorldStateStore) -> None:
        self.state_store = state_store
        self.journal = ActionJournal(state_store)
        self.compensation = Compensation()

    def replay(self, trace_id: str) -> dict[str, Any]:
        return self.plan(trace_id=trace_id)

    def compensation_proposal(self, trace_id: str | None = None, event_id: str | None = None) -> dict[str, Any]:
        plan = self.plan(trace_id=trace_id, event_id=event_id)
        if plan.get("status") != "planned":
            return {**plan, "proposal_status": "not_available"}
        rollback_step = next((step for step in plan.get("steps", []) if isinstance(step.get("rollback_option"), dict)), None)
        if not rollback_step:
            return {
                "status": "plan_only",
                "proposal_status": "not_available",
                "reason": "No snapshot-backed rollback step is available for this replay plan.",
                "plan": plan,
            }
        snapshot_id = str(rollback_step["rollback_option"]["snapshot_id"])
        proposal = {
            "agent": "veyra_replay",
            "action": {"type": "rollback_restore", "snapshot_id": snapshot_id, "source_trace_id": trace_id, "source_event_id": event_id},
            "risk_guess": "R4",
            "reversible": "yes",
            "reason": "Restore snapshot through guarded replay compensation.",
        }
        return {
            "status": "proposal_ready",
            "proposal_status": "ready",
            "risk_level": "R4",
            "snapshot_id": snapshot_id,
            "proposal": proposal,
            "plan": plan,
        }

    def plan(self, trace_id: str | None = None, event_id: str | None = None) -> dict[str, Any]:
        items = self.journal.find_by_trace(trace_id) if trace_id else self.journal.find_by_event(str(event_id))
        if not items:
            return {"status": "not_found", "trace_id": trace_id, "event_id": event_id, "steps": []}
        steps = self._steps_for(items)
        return {
            "status": "planned",
            "trace_id": trace_id,
            "event_id": event_id or self._first(items, "event_id"),
            "replayable": False,
            "mode": "plan_only",
            "reason": "Replay is non-destructive. Execute restore/resubmit steps through existing guarded APIs.",
            "steps": steps,
            "journal": items,
            "compensation": self.compensation.plan({"trace_id": trace_id, "event_id": event_id, "journal_count": len(items)}),
        }

    def _steps_for(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        steps: list[dict[str, Any]] = []
        for item in items:
            source = item.get("source")
            raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
            if source == "event":
                steps.append({"type": "inspect_event", "source": source, "event_id": item.get("event_id"), "summary": item.get("summary")})
            elif source == "policy":
                steps.append({"type": "review_policy_decision", "source": source, "decision": raw.get("decision"), "risk_level": item.get("risk_level")})
            elif source == "tool":
                step = {"type": "inspect_tool_trace", "source": source, "trace_id": item.get("trace_id"), "status": item.get("status")}
                if item.get("snapshot_id"):
                    step["rollback_option"] = {"snapshot_id": item["snapshot_id"], "endpoint": f"/rollback/{item['snapshot_id']}/restore", "requires_confirmation": True}
                steps.append(step)
            elif source == "execution":
                steps.append(
                    {
                        "type": "inspect_execution",
                        "source": source,
                        "trace_id": item.get("trace_id"),
                        "task_id": item.get("task_id"),
                        "status": item.get("status"),
                        "next_action": (raw.get("verification") or {}).get("next_action") if isinstance(raw.get("verification"), dict) else None,
                    }
                )
            elif source == "rollback":
                steps.append({"type": "inspect_rollback", "source": source, "snapshot_id": item.get("snapshot_id"), "summary": item.get("summary")})
            elif source == "memory":
                steps.append({"type": "inspect_memory_patch", "source": source, "status": item.get("status")})
            elif source == "core_model":
                steps.append({"type": "inspect_core_model_reasoning", "source": source, "summary": item.get("summary")})
        if not any(step.get("rollback_option") for step in steps):
            steps.append({"type": "no_direct_rollback", "reason": "No snapshot-backed tool trace was found in the journal slice."})
        return steps

    def _first(self, items: list[dict[str, Any]], key: str) -> Any:
        for item in items:
            if item.get(key):
                return item[key]
        return None
