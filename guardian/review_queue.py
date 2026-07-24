from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
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
        def append_review(state: dict[str, Any]) -> None:
            items = state.setdefault("items", [])
            if not isinstance(items, list):
                state["items"] = items = []
            items.append(review)

        self.state_store.mutate_json("review_queue.json", append_review)
        self.state_store.append_jsonl("action_record.jsonl", {"event_id": event_id, "route": "human_review", "status": "pending", "artifacts": {"review": review}})
        return review

    def list(self, status: str | None = None) -> list[dict[str, Any]]:
        items = self.state_store.read_json("review_queue.json").get("items", [])
        if status:
            return [item for item in items if item.get("status") == status]
        return items

    def diagnostic(self, *, stale_after_days: int = 7) -> dict[str, Any]:
        items = [item for item in self.list() if isinstance(item, dict)]
        pending = [self._diagnostic_item(item, stale_after_days=stale_after_days) for item in items if item.get("status") == "pending"]
        by_risk: dict[str, int] = {}
        by_type: dict[str, int] = {}
        for item in pending:
            risk = str(item.get("risk") or "unknown")
            review_type = str(item.get("type") or "unknown")
            by_risk[risk] = by_risk.get(risk, 0) + 1
            by_type[review_type] = by_type.get(review_type, 0) + 1
        stale = [item for item in pending if item.get("stale")]
        return {
            "status": "success",
            "checked_at": utc_now_iso(),
            "pending_count": len(pending),
            "stale_pending_count": len(stale),
            "by_risk": by_risk,
            "by_type": by_type,
            "pending": pending,
            "actions": {
                "mark_resolved": "/ops/reviews/{review_id}/resolve",
                "archive": "/ops/reviews/{review_id}/archive",
            },
        }

    def decide(self, review_id: str, decision: str, reason: str = "") -> dict[str, Any]:
        if decision not in {"approved", "rejected"}:
            raise ValueError("decision must be approved or rejected")
        selected: dict[str, Any] | None = None
        changed = False

        def apply_decision(state: dict[str, Any]) -> None:
            nonlocal selected, changed
            items = state.setdefault("items", [])
            if not isinstance(items, list):
                state["items"] = items = []
            for item in items:
                if not isinstance(item, dict) or item.get("review_id") != review_id:
                    continue
                if item.get("status") == "pending":
                    item["status"] = decision
                    item["decided_at"] = utc_now_iso()
                    item["decision_reason"] = reason
                    changed = True
                selected = dict(item)
                return
            raise KeyError(f"Review not found: {review_id}")

        self.state_store.mutate_json("review_queue.json", apply_decision)
        if selected is None:
            raise KeyError(f"Review not found: {review_id}")
        if changed:
            self.state_store.append_jsonl(
                "action_record.jsonl",
                {
                    "event_id": selected.get("event_id"),
                    "route": "human_review",
                    "status": decision,
                    "artifacts": {"review_id": review_id, "reason": reason},
                },
            )
            self.state_store.patch_json(
                "risk_state.json",
                {"current_risk": selected.get("risk_level", "R0") if decision == "approved" else "R0"},
            )
        return selected

    def mark_resolved(self, review_id: str, reason: str = "") -> dict[str, Any]:
        return self._set_terminal_status(review_id, "resolved", reason or "marked resolved by runtime hygiene")

    def archive(self, review_id: str, reason: str = "") -> dict[str, Any]:
        review = self._set_terminal_status(review_id, "archived", reason or "archived by runtime hygiene")
        archive_path = self._archive_review(review)
        review["archive_path"] = archive_path

        def attach_archive_path(state: dict[str, Any]) -> None:
            items = state.setdefault("items", [])
            if not isinstance(items, list):
                state["items"] = items = []
            for item in items:
                if isinstance(item, dict) and item.get("review_id") == review_id:
                    item["archive_path"] = archive_path
                    return
            raise KeyError(f"Review not found: {review_id}")

        self.state_store.mutate_json("review_queue.json", attach_archive_path)
        self.state_store.append_jsonl(
            "action_record.jsonl",
            {
                "event_id": review.get("event_id"),
                "route": "human_review_archive",
                "status": "archived",
                "artifacts": {"review_id": review_id, "reason": reason, "archive_path": archive_path},
            },
        )
        return review

    def update_execution(self, review_id: str, execution_result: dict[str, Any]) -> dict[str, Any]:
        selected: dict[str, Any] | None = None

        def apply_execution(state: dict[str, Any]) -> None:
            nonlocal selected
            items = state.setdefault("items", [])
            if not isinstance(items, list):
                state["items"] = items = []
            for item in items:
                if not isinstance(item, dict) or item.get("review_id") != review_id:
                    continue
                item["execution_result"] = execution_result
                selected = dict(item)
                return
            raise KeyError(f"Review not found: {review_id}")

        self.state_store.mutate_json("review_queue.json", apply_execution)
        if selected is None:
            raise KeyError(f"Review not found: {review_id}")
        self.state_store.append_jsonl(
            "action_record.jsonl",
            {
                "event_id": selected.get("event_id"),
                "route": "human_review",
                "status": "executed" if execution_result.get("status") in {"ok", "success"} else "execution_failed",
                "artifacts": {"review_id": review_id, "execution_result": execution_result},
            },
        )
        return selected

    def _set_terminal_status(self, review_id: str, status: str, reason: str) -> dict[str, Any]:
        if status not in {"resolved", "archived"}:
            raise ValueError("status must be resolved or archived")
        selected: dict[str, Any] | None = None
        changed = False

        def apply_terminal_status(state: dict[str, Any]) -> None:
            nonlocal selected, changed
            items = state.setdefault("items", [])
            if not isinstance(items, list):
                state["items"] = items = []
            for item in items:
                if not isinstance(item, dict) or item.get("review_id") != review_id:
                    continue
                if item.get("status") == "pending":
                    item["status"] = status
                    item["decided_at"] = utc_now_iso()
                    item["decision_reason"] = reason
                    changed = True
                selected = dict(item)
                return
            raise KeyError(f"Review not found: {review_id}")

        self.state_store.mutate_json("review_queue.json", apply_terminal_status)
        if selected is None:
            raise KeyError(f"Review not found: {review_id}")
        if changed:
            self.state_store.append_jsonl(
                "action_record.jsonl",
                {
                    "event_id": selected.get("event_id"),
                    "route": "human_review",
                    "status": status,
                    "artifacts": {"review_id": review_id, "reason": reason},
                },
            )
            self.state_store.patch_json("risk_state.json", {"current_risk": "R0"})
        return selected

    def _diagnostic_item(self, item: dict[str, Any], *, stale_after_days: int) -> dict[str, Any]:
        created_at = str(item.get("created_at") or "")
        age_seconds = self._age_seconds(created_at)
        review_type = self._review_type(item)
        source = self._source(item)
        risk = str(item.get("risk_level") or "unknown")
        stale = age_seconds is not None and age_seconds > max(1, stale_after_days) * 86400
        return {
            "review_id": item.get("review_id"),
            "type": review_type,
            "source": source,
            "created_at": created_at,
            "age_seconds": age_seconds,
            "stale": stale,
            "risk": risk,
            "risk_level": risk,
            "status": item.get("status"),
            "task_text": str(item.get("task_text") or "")[:240],
            "guardian_decision": (item.get("guardian_decision") or {}).get("decision") if isinstance(item.get("guardian_decision"), dict) else None,
            "can_mark_resolved": stale,
            "can_archive": stale,
        }

    def _review_type(self, item: dict[str, Any]) -> str:
        proposal = item.get("proposal") if isinstance(item.get("proposal"), dict) else {}
        if proposal.get("type"):
            return str(proposal.get("type"))
        text = str(item.get("task_text") or "").lower()
        if "openclaw" in text and ("重启" in text or "restart" in text):
            return "service_restart_request"
        if "删除" in text or "rm -rf" in text or "delete" in text:
            return "destructive_action_review"
        return "action_review"

    def _source(self, item: dict[str, Any]) -> str:
        proposal = item.get("proposal") if isinstance(item.get("proposal"), dict) else {}
        for key in ("source", "route", "origin"):
            if proposal.get(key):
                return str(proposal.get(key))
        event_id = str(item.get("event_id") or "")
        return f"event:{event_id}" if event_id else "guardian_review_queue"

    def _age_seconds(self, value: str) -> int | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0, int((datetime.now(timezone.utc) - parsed).total_seconds()))

    def _archive_review(self, review: dict[str, Any]) -> str:
        review_id = str(review.get("review_id") or f"review_{uuid4().hex[:12]}")
        archive_root = self.state_store.root / "archive" / "reviews"
        archive_root.mkdir(parents=True, exist_ok=True)
        timestamp = utc_now_iso().replace(":", "").replace(".", "")
        archive_path = archive_root / f"{review_id}-{timestamp}.json"
        archive_path.write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            return str(archive_path.relative_to(self.state_store.root))
        except ValueError:
            return str(Path(archive_path))
