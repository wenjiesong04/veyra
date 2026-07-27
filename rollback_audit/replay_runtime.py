from __future__ import annotations

from datetime import datetime, timezone
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
    PROCESSING_LEASE_SECONDS = 300
    AUTO_EXECUTION_SUPPORTED = False

    def __init__(
        self,
        *,
        state_store: WorldStateStore,
        replay: Replay,
        journal: ActionJournal,
        review_queue: ReviewQueue,
        foresight_engine: ForesightEngine,
        action_executor: Any | None = None,
    ) -> None:
        self.state_store = state_store
        self.replay = replay
        self.journal = journal
        self.review_queue = review_queue
        self.foresight_engine = foresight_engine
        self.action_executor = action_executor

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
        current_jobs = self._read_state().get("jobs", [])
        known = {
            self._job_key(job)
            for job in current_jobs
            if isinstance(job, dict)
        } if isinstance(current_jobs, list) else set()
        proposed: list[dict[str, Any]] = []
        for item in candidates:
            key = self._candidate_key(item)
            if key in known:
                continue
            known.add(key)
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
            proposed.append(job)
        created: list[dict[str, Any]] = []

        def merge_candidates(state: dict[str, Any]) -> None:
            jobs = state.get("jobs") if isinstance(state.get("jobs"), list) else []
            existing = {self._job_key(job) for job in jobs if isinstance(job, dict)}
            for job in proposed:
                key = self._job_key(job)
                if key in existing:
                    continue
                jobs.append(job)
                created.append(dict(job))
                existing.add(key)
            state.update(
                {
                    "status": "pending" if jobs else "idle",
                    "last_scan_at": utc_now_iso(),
                    "jobs": jobs[-500:],
                }
            )
            config = (
                state.get("config")
                if isinstance(state.get("config"), dict)
                else {}
            )
            config["auto_execute_enabled"] = False
            config["allow_r4_restore"] = False
            state["config"] = config

        self.state_store.mutate_json("replay_runtime_state.json", merge_candidates)
        self.state_store.append_jsonl("action_record.jsonl", {"route": "replay_runtime_scan", "status": "success", "artifacts": {"created": len(created), "candidate_count": len(candidates)}})
        return {"status": "success", "candidate_count": len(candidates), "created_count": len(created), "created": created, "runtime": self.status()}

    def configure(self, *, auto_execute_enabled: bool | None = None, allow_r4_restore: bool | None = None) -> dict[str, Any]:
        configured: dict[str, Any] = {}
        enable_requested = bool(auto_execute_enabled or allow_r4_restore)

        def configure_runtime(state: dict[str, Any]) -> None:
            nonlocal configured
            config = state.setdefault("config", {})
            if not isinstance(config, dict):
                state["config"] = config = {}
            # Phase 3 keeps rollback execution behind an explicit, one-time
            # ReviewQueue claim. Runtime/API configuration cannot mint that
            # authority or turn R4 compensation into an unattended action.
            config["auto_execute_enabled"] = False
            config["allow_r4_restore"] = False
            state["updated_at"] = utc_now_iso()
            configured = dict(config)

        self.state_store.mutate_json("replay_runtime_state.json", configure_runtime)
        return {
            "status": "blocked" if enable_requested else "success",
            "reason": (
                "Replay auto-execution is deny-only; approve the generated "
                "R4 review explicitly."
                if enable_requested
                else "Replay remains review-only."
            ),
            "config": configured,
            "runtime": self.status(),
        }

    def run_pending(
        self,
        *,
        auto_create_reviews: bool = True,
        auto_execute: bool = False,
        allow_r4_restore: bool = False,
        limit: int = 20,
    ) -> dict[str, Any]:
        claim_token = f"claim_{uuid4().hex}"
        claimed: list[dict[str, Any]] = []
        auto_execute_requested = bool(auto_execute or allow_r4_restore)

        def claim_jobs(state: dict[str, Any]) -> None:
            config = state.get("config") if isinstance(state.get("config"), dict) else {}
            config["auto_execute_enabled"] = False
            config["allow_r4_restore"] = False
            state["config"] = config
            jobs = state.get("jobs") if isinstance(state.get("jobs"), list) else []
            runnable_statuses = {"pending", "plan_ready"}
            for job in jobs:
                if len(claimed) >= max(0, int(limit)):
                    break
                if not isinstance(job, dict):
                    continue
                status = str(job.get("status") or "")
                if status not in runnable_statuses and not (
                    status == "processing" and self._processing_claim_is_stale(job)
                ):
                    continue
                prior_status = status or "pending"
                job["status"] = "processing"
                job["processing_token"] = claim_token
                job["processing_started_at"] = utc_now_iso()
                job["updated_at"] = utc_now_iso()
                claimed_job = dict(job)
                claimed_job["claimed_from_status"] = prior_status
                claimed.append(claimed_job)
            state["status"] = (
                "pending"
                if any(
                    isinstance(job, dict)
                    and job.get("status") in {"pending", "plan_ready", "processing"}
                    for job in jobs
                )
                else "idle"
            )
            state.setdefault("config", {"auto_execute_enabled": False, "allow_r4_restore": False})
            state["updated_at"] = utc_now_iso()

        self.state_store.mutate_json("replay_runtime_state.json", claim_jobs)
        processed: list[dict[str, Any]] = []
        for job in claimed:
            try:
                proposal = self.replay.compensation_proposal(
                    trace_id=job.get("trace_id"),
                    event_id=job.get("event_id"),
                )
                update = {
                    "status": "plan_ready",
                    "updated_at": utc_now_iso(),
                    "proposal_status": proposal.get("proposal_status"),
                    "plan": redact_sensitive(proposal.get("plan", {}), max_string=2200),
                }
                if proposal.get("proposal_status") == "ready" and (
                    auto_create_reviews or auto_execute_requested
                ):
                    review = self._review_for_job(job, proposal)
                    update.update(
                        {
                            "status": "needs_confirmation",
                            "review_id": review.get("review_id"),
                            "review": review,
                            **(
                                {
                                    "reason": (
                                        "Replay auto-execution request was "
                                        "downgraded to explicit review."
                                    )
                                }
                                if auto_execute_requested
                                else {}
                            ),
                        }
                    )
                elif proposal.get("proposal_status") != "ready":
                    update.update(
                        {
                            "status": "plan_only",
                            "reason": proposal.get("reason") or proposal.get("status"),
                        }
                    )
                processed.append(self._finalize_claim(job, claim_token=claim_token, update=update))
            except Exception as exc:
                self._finalize_claim(
                    job,
                    claim_token=claim_token,
                    update={
                        "status": "processing_failed",
                        "reason": str(exc)[:500],
                        "updated_at": utc_now_iso(),
                    },
                )
                raise
        self.state_store.append_jsonl(
            "action_record.jsonl",
            {
                "route": "replay_runtime_run",
                "status": "success",
                "artifacts": {
                    "processed": len(processed),
                    "auto_create_reviews": auto_create_reviews,
                    "auto_execute_requested": auto_execute_requested,
                    "auto_execute": False,
                },
            },
        )
        return {
            "status": (
                "needs_review"
                if auto_execute_requested
                else "success"
            ),
            "reason": (
                "Replay auto-execution is disabled; ready compensation was "
                "left behind an explicit review."
                if auto_execute_requested
                else "Replay plans were processed without execution authority."
            ),
            "execution_authority_enabled": False,
            "processed_count": len(processed),
            "processed": processed,
            "runtime": self.status(),
        }

    def _finalize_claim(
        self,
        claimed_job: dict[str, Any],
        *,
        claim_token: str,
        update: dict[str, Any],
    ) -> dict[str, Any]:
        job_id = str(claimed_job.get("job_id") or "")
        finalized: dict[str, Any] = {}

        def finalize(state: dict[str, Any]) -> None:
            nonlocal finalized
            jobs = state.get("jobs") if isinstance(state.get("jobs"), list) else []
            for job in jobs:
                if not isinstance(job, dict) or str(job.get("job_id") or "") != job_id:
                    continue
                if str(job.get("processing_token") or "") != claim_token:
                    finalized = dict(job)
                    return
                job.update(update)
                job.pop("processing_token", None)
                job.pop("processing_started_at", None)
                finalized = dict(job)
                break
            if not finalized:
                raise KeyError(f"Replay job not found: {job_id}")
            state["jobs"] = jobs[-500:]
            state["status"] = (
                "pending"
                if any(
                    isinstance(job, dict)
                    and job.get("status") in {"pending", "plan_ready", "processing"}
                    for job in jobs
                )
                else "idle"
            )
            state["updated_at"] = utc_now_iso()

        self.state_store.mutate_json("replay_runtime_state.json", finalize)
        return finalized

    def _processing_claim_is_stale(self, job: dict[str, Any]) -> bool:
        value = str(job.get("processing_started_at") or "")
        if not value:
            return True
        try:
            started = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return True
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        age_seconds = (datetime.now(timezone.utc) - started).total_seconds()
        return age_seconds >= self.PROCESSING_LEASE_SECONDS

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

    def _review_for_job(self, job: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        review_id = str(job.get("review_id") or "")
        if review_id:
            existing = next((item for item in self.review_queue.list() if item.get("review_id") == review_id), None)
            if existing:
                return existing
        return self._create_review(payload)

    def _auto_execute_review(self, review: dict[str, Any], *, allow_r4_restore: bool) -> dict[str, Any]:
        return {
            "status": "needs_confirmation",
            "reason": (
                "ReplayRuntime cannot approve or execute rollback reviews. "
                "A user must approve the canonical review through the one-time "
                "execution-claim path."
            ),
            "execution_authority_enabled": False,
            "review": review,
        }

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
        state = self.state_store.read_json("replay_runtime_state.json") or {"status": "idle", "jobs": [], "last_scan_at": None}
        config = state.get("config") if isinstance(state.get("config"), dict) else {}
        stale_authority_flags = bool(
            config.get("auto_execute_enabled")
            or config.get("allow_r4_restore")
        )
        state["config"] = {
            **config,
            "auto_execute_enabled": False,
            "allow_r4_restore": False,
        }
        state["execution_authority_enabled"] = False
        if stale_authority_flags:
            state["stale_authority_flags_ignored"] = True
        return state
