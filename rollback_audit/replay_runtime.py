from __future__ import annotations

from typing import Any
from uuid import uuid4

from core.definitions import RiskLevel
from core.foresight_engine import ForesightEngine
from core.model_client import redact_sensitive
from core.world_state import WorldStateStore
from guardian.review_queue import ReviewQueue
from interface.event_schema import utc_now_iso
from rollback_audit.action_journal import ActionJournal
from rollback_audit.replay import Replay


class ReplayRuntime:
    """Automatically discovers replay/compensation candidates from audit logs."""

    CANDIDATE_STATUSES = {"needs_rollback", "verified_failed", "execution_failed", "failed", "error", "timeout"}

    def __init__(
        self,
        *,
        state_store: WorldStateStore,
        replay: Replay,
        journal: ActionJournal,
        review_queue: ReviewQueue,
        foresight_engine: ForesightEngine,
    ) -> None:
        self.state_store = state_store
        self.replay = replay
        self.journal = journal
        self.review_queue = review_queue
        self.foresight_engine = foresight_engine

    def status(self) -> dict[str, Any]:
        state = self._read_state()
        jobs = state.get("jobs") if isinstance(state.get("jobs"), list) else []
        summary: dict[str, int] = {}
        for job in jobs:
            status = str(job.get("status") or "unknown")
            summary[status] = summary.get(status, 0) + 1
        return {**state, "summary": {"total": len(jobs), "by_status": summary}}

    def scan(self, *, limit: int = 200) -> dict[str, Any]:
        timeline = self.journal.timeline(limit=limit)
        candidates = [item for item in timeline.get("items", []) if self._is_candidate(item)]
        state = self._read_state()
        jobs = state.get("jobs") if isinstance(state.get("jobs"), list) else []
        existing = {self._job_key(job) for job in jobs}
        created: list[dict[str, Any]] = []
        for item in candidates:
            key = self._candidate_key(item)
            if key in existing:
                continue
            plan = self.replay.plan(trace_id=item.get("trace_id"), event_id=item.get("event_id"))
            job = {
                "job_id": f"replay_{uuid4().hex[:12]}",
                "status": "pending",
                "candidate_key": key,
                "trace_id": item.get("trace_id"),
                "event_id": item.get("event_id"),
                "task_id": item.get("task_id"),
                "source_status": item.get("status"),
                "summary": item.get("summary"),
                "plan_status": plan.get("status"),
                "created_at": utc_now_iso(),
                "updated_at": utc_now_iso(),
            }
            jobs.append(job)
            created.append(job)
            existing.add(key)
        state.update({"status": "pending" if jobs else "idle", "last_scan_at": utc_now_iso(), "jobs": jobs[-500:]})
        self._write_state(state)
        self.state_store.append_jsonl("action_record.jsonl", {"route": "replay_runtime_scan", "status": "success", "artifacts": {"created": len(created), "candidate_count": len(candidates)}})
        return {"status": "success", "candidate_count": len(candidates), "created_count": len(created), "created": created, "runtime": self.status()}

    def run_pending(self, *, auto_create_reviews: bool = True, limit: int = 20) -> dict[str, Any]:
        state = self._read_state()
        jobs = state.get("jobs") if isinstance(state.get("jobs"), list) else []
        processed: list[dict[str, Any]] = []
        for job in jobs:
            if len(processed) >= limit:
                break
            if job.get("status") not in {"pending", "plan_ready"}:
                continue
            proposal = self.replay.compensation_proposal(trace_id=job.get("trace_id"), event_id=job.get("event_id"))
            update = {
                "status": "plan_ready",
                "updated_at": utc_now_iso(),
                "proposal_status": proposal.get("proposal_status"),
                "plan": redact_sensitive(proposal.get("plan", {}), max_string=2200),
            }
            if proposal.get("proposal_status") == "ready" and auto_create_reviews:
                review = self._create_review(proposal)
                update.update({"status": "needs_confirmation", "review_id": review.get("review_id"), "review": review})
            elif proposal.get("proposal_status") != "ready":
                update.update({"status": "plan_only", "reason": proposal.get("reason") or proposal.get("status")})
            job.update(update)
            processed.append(job)
        state["jobs"] = jobs[-500:]
        state["status"] = "pending" if any(job.get("status") in {"pending", "plan_ready"} for job in jobs) else "idle"
        state["updated_at"] = utc_now_iso()
        self._write_state(state)
        self.state_store.append_jsonl("action_record.jsonl", {"route": "replay_runtime_run", "status": "success", "artifacts": {"processed": len(processed), "auto_create_reviews": auto_create_reviews}})
        return {"status": "success", "processed_count": len(processed), "processed": processed, "runtime": self.status()}

    def _create_review(self, payload: dict[str, Any]) -> dict[str, Any]:
        proposal = payload["proposal"]
        snapshot_id = str(payload.get("snapshot_id") or proposal.get("action", {}).get("snapshot_id") or "")
        task_text = f"Replay runtime compensation: restore snapshot {snapshot_id}"
        return self.review_queue.create(
            event_id=str(payload.get("plan", {}).get("event_id") or f"replay_{snapshot_id}"),
            task_text=task_text,
            risk_level=RiskLevel.R4.value,
            foresight=self.foresight_engine.predict_text_action(task_text, RiskLevel.R4),
            guardian_decision={
                "decision": "ask_user",
                "risk_level": RiskLevel.R4.value,
                "reason": "Replay runtime found a snapshot-backed compensation. Restore requires explicit approval.",
            },
            proposal=proposal,
        )

    def _is_candidate(self, item: dict[str, Any]) -> bool:
        status = str(item.get("status") or "")
        if status in self.CANDIDATE_STATUSES:
            return True
        raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
        verification = raw.get("verification") if isinstance(raw.get("verification"), dict) else {}
        return bool(verification.get("needs_rollback") or verification.get("next_action") == "rollback_or_compensate")

    def _candidate_key(self, item: dict[str, Any]) -> str:
        return str(item.get("trace_id") or item.get("event_id") or item.get("task_id") or item.get("journal_id"))

    def _job_key(self, job: dict[str, Any]) -> str:
        return str(job.get("candidate_key") or job.get("trace_id") or job.get("event_id") or job.get("task_id") or job.get("job_id"))

    def _read_state(self) -> dict[str, Any]:
        return self.state_store.read_json("replay_runtime_state.json") or {"status": "idle", "jobs": [], "last_scan_at": None}

    def _write_state(self, state: dict[str, Any]) -> None:
        self.state_store.write_json("replay_runtime_state.json", state)
