from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from core.world_state import WorldStateStore
from interface.event_schema import utc_now_iso


class Cron:
    """Persistent lightweight scheduler for Veyra runtime jobs.

    The scheduler intentionally triggers only Veyra-owned bounded jobs. It does
    not execute arbitrary shell commands or external callbacks.
    """

    DEFAULT_JOB_ID = "active_awareness_tick"
    COMMITMENT_PUSH_JOB_ID = "commitment_push_due"

    def __init__(self, *, state_store: WorldStateStore, active_loop: Any, commitment_push: Any | None = None) -> None:
        self.state_store = state_store
        self.active_loop = active_loop
        self.commitment_push = commitment_push
        self._ensure_state()

    def status(self) -> dict[str, Any]:
        state = self._read_state()
        jobs = state.get("jobs") if isinstance(state.get("jobs"), dict) else {}
        enabled = [job_id for job_id, job in jobs.items() if isinstance(job, dict) and job.get("enabled")]
        return {
            **state,
            "summary": {
                "job_count": len(jobs),
                "enabled_count": len(enabled),
                "enabled_jobs": enabled,
                "placeholder_removed": True,
            },
        }

    def configure(
        self,
        *,
        job_id: str = DEFAULT_JOB_ID,
        enabled: bool | None = None,
        interval_seconds: float | None = None,
        include_runtime_matrix: bool | None = None,
    ) -> dict[str, Any]:
        state = self._read_state()
        jobs = state.setdefault("jobs", {})
        job = jobs.setdefault(job_id, self._default_job())
        if enabled is not None:
            job["enabled"] = bool(enabled)
        if interval_seconds is not None:
            job["interval_seconds"] = max(10.0, min(float(interval_seconds), 86400.0))
        if include_runtime_matrix is not None:
            job["include_runtime_matrix"] = bool(include_runtime_matrix)
        job["updated_at"] = utc_now_iso()
        job["next_run_at"] = job.get("next_run_at") or self._next_run_at(job)
        state["updated_at"] = utc_now_iso()
        self._write_state(state)
        return {"status": "success", "job": job, "runtime": self.status()}

    def run_once(self, *, job_id: str = DEFAULT_JOB_ID, reason: str = "cron_manual") -> dict[str, Any]:
        state = self._read_state()
        jobs = state.setdefault("jobs", {})
        job = jobs.setdefault(job_id, self._default_job())
        if not job.get("enabled", True):
            return {"status": "skipped", "reason": "job disabled", "job": job}
        result = self._execute_job(job_id, job, reason=reason)
        state["updated_at"] = utc_now_iso()
        self._write_state(state)
        self.state_store.append_jsonl("action_record.jsonl", {"route": "runtime_cron", "status": result.get("status"), "artifacts": {"job_id": job_id, "reason": reason, "result_status": result.get("result_status")}})
        return result

    def run_due(self) -> dict[str, Any]:
        state = self._read_state()
        jobs = state.setdefault("jobs", {})
        processed: list[dict[str, Any]] = []
        now = datetime.now(timezone.utc)
        for job_id, job in jobs.items():
            if not isinstance(job, dict) or not job.get("enabled", True):
                continue
            if self._is_due(job, now):
                processed.append(self._execute_job(job_id, job, reason="cron_due"))
        state["last_run_due_at"] = utc_now_iso()
        state["updated_at"] = utc_now_iso()
        self._write_state(state)
        status = "success" if processed else "idle"
        self.state_store.append_jsonl("action_record.jsonl", {"route": "runtime_cron_due", "status": status, "artifacts": {"processed_count": len(processed)}})
        return {"status": status, "processed_count": len(processed), "processed": processed, "runtime": self.status()}

    def _execute_job(self, job_id: str, job: dict[str, Any], *, reason: str) -> dict[str, Any]:
        if job_id == self.DEFAULT_JOB_ID:
            tick = self.active_loop.tick(
                reason=reason,
                include_runtime_matrix=bool(job.get("include_runtime_matrix", False)),
            )
            result = {"status": "success", "result_status": tick.get("status"), "job_id": job_id, "tick": tick}
        elif job_id == self.COMMITMENT_PUSH_JOB_ID:
            if self.commitment_push is None:
                result = {"status": "not_configured", "reason": "commitment push runtime is not wired"}
            else:
                push = self.commitment_push.run_due(limit=int(job.get("limit") or 10), reason=reason)
                result = {"status": push.get("status"), "result_status": push.get("status"), "job_id": job_id, "push": push}
        else:
            result = {"status": "unsupported", "reason": f"Unsupported cron job: {job_id}"}
        job["last_run_at"] = utc_now_iso()
        job["last_result_status"] = result.get("result_status") or result.get("status")
        job["run_count"] = int(job.get("run_count") or 0) + 1
        job["next_run_at"] = self._next_run_at(job)
        job["updated_at"] = utc_now_iso()
        return result

    def _ensure_state(self) -> None:
        state = self.state_store.read_json("runtime_cron_state.json")
        if not state:
            self._write_state(
                {
                    "status": "configured",
                    "jobs": {
                        self.DEFAULT_JOB_ID: self._default_job(),
                        self.COMMITMENT_PUSH_JOB_ID: self._default_commitment_job(),
                    },
                    "updated_at": utc_now_iso(),
                }
            )
            return
        jobs = state.setdefault("jobs", {})
        changed = False
        if self.DEFAULT_JOB_ID not in jobs:
            jobs[self.DEFAULT_JOB_ID] = self._default_job()
            changed = True
        if self.COMMITMENT_PUSH_JOB_ID not in jobs:
            jobs[self.COMMITMENT_PUSH_JOB_ID] = self._default_commitment_job()
            changed = True
        if changed:
            state["updated_at"] = utc_now_iso()
            self._write_state(state)

    def _default_job(self) -> dict[str, Any]:
        return {
            "job_id": self.DEFAULT_JOB_ID,
            "enabled": True,
            "interval_seconds": 300.0,
            "include_runtime_matrix": False,
            "last_run_at": None,
            "next_run_at": utc_now_iso(),
            "run_count": 0,
            "updated_at": utc_now_iso(),
        }

    def _default_commitment_job(self) -> dict[str, Any]:
        return {
            "job_id": self.COMMITMENT_PUSH_JOB_ID,
            "enabled": True,
            "interval_seconds": 60.0,
            "limit": 10,
            "last_run_at": None,
            "next_run_at": utc_now_iso(),
            "run_count": 0,
            "updated_at": utc_now_iso(),
        }

    def _is_due(self, job: dict[str, Any], now: datetime) -> bool:
        next_run_at = self._parse_time(job.get("next_run_at"))
        return next_run_at is None or next_run_at <= now

    def _next_run_at(self, job: dict[str, Any]) -> str:
        interval = max(10.0, min(float(job.get("interval_seconds") or 300.0), 86400.0))
        return (datetime.now(timezone.utc) + timedelta(seconds=interval)).isoformat()

    def _parse_time(self, value: Any) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    def _read_state(self) -> dict[str, Any]:
        return self.state_store.read_json("runtime_cron_state.json") or {"status": "configured", "jobs": {}}

    def _write_state(self, state: dict[str, Any]) -> None:
        state["status"] = "configured"
        self.state_store.write_json("runtime_cron_state.json", state)
