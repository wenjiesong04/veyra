from __future__ import annotations

from typing import Any
from uuid import uuid4

from core.state_compact import compact_foresight, compact_guardian_decision, compact_review_item
from core.world_state import WorldStateStore
from interface.event_schema import utc_now_iso


class ReviewQueue:
    def __init__(self, state_store: WorldStateStore) -> None:
        self.state_store = state_store

    def create(
        self,
        event_id: str,
        task_text: str,
        risk_level: str,
        foresight: dict[str, Any],
        guardian_decision: dict[str, Any],
        proposal: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        review = compact_review_item(
            {
                "review_id": f"rev_{uuid4().hex[:12]}",
                "event_id": event_id,
                "task_text": task_text,
                "risk_level": risk_level,
                "status": "pending",
                "foresight": compact_foresight(foresight),
                "guardian_decision": compact_guardian_decision(guardian_decision),
                "proposal": proposal,
                "execution_result": None,
                "created_at": utc_now_iso(),
                "decided_at": None,
                "decision_reason": None,
            }
        )
        state = self.state_store.read_json("review_queue.json") or {"items": []}
        items = state.setdefault("items", [])
        items.append(review)
        self.state_store.write_json("review_queue.json", state)
        self.state_store.append_jsonl("action_record.jsonl", {"event_id": event_id, "route": "human_review", "status": "pending", "artifacts": {"review": review}})
        return review

    def list(self, status: str | None = None) -> list[dict[str, Any]]:
        items = self.state_store.read_json("review_queue.json").get("items", [])
        if status:
            return [item for item in items if item.get("status") == status]
        return items

    def decide(self, review_id: str, decision: str, reason: str = "") -> dict[str, Any]:
        if decision not in {"approved", "rejected"}:
            raise ValueError("decision must be approved or rejected")
        state = self.state_store.read_json("review_queue.json") or {"items": []}
        for item in state.setdefault("items", []):
            if item.get("review_id") == review_id:
                if item.get("status") != "pending":
                    return item
                item["status"] = decision
                item["decided_at"] = utc_now_iso()
                item["decision_reason"] = reason
                self.state_store.write_json("review_queue.json", state)
                self.state_store.append_jsonl(
                    "action_record.jsonl",
                    {
                        "event_id": item.get("event_id"),
                        "route": "human_review",
                        "status": decision,
                        "artifacts": {"review_id": review_id, "reason": reason},
                    },
                )
                self.state_store.patch_json("risk_state.json", {"current_risk": item.get("risk_level", "R0") if decision == "approved" else "R0"})
                return item
        raise KeyError(f"Review not found: {review_id}")

    def update_execution(self, review_id: str, execution_result: dict[str, Any]) -> dict[str, Any]:
        state = self.state_store.read_json("review_queue.json") or {"items": []}
        for item in state.setdefault("items", []):
            if item.get("review_id") == review_id:
                item["execution_result"] = execution_result
                self.state_store.write_json("review_queue.json", state)
                self.state_store.append_jsonl(
                    "action_record.jsonl",
                    {
                        "event_id": item.get("event_id"),
                        "route": "human_review",
                        "status": "executed" if execution_result.get("status") in {"ok", "success"} else "execution_failed",
                        "artifacts": {"review_id": review_id, "execution_result": execution_result},
                    },
                )
                return item
        raise KeyError(f"Review not found: {review_id}")
