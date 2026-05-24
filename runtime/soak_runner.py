from __future__ import annotations

import threading
from typing import Any
from uuid import uuid4

from interface.event_schema import utc_now_iso
from runtime.agent_task_tracker import AgentTaskTracker
from runtime.proactive_checks import ProactiveChecks
from runtime.retention_policy import RetentionPolicy
from runtime.safety_validation import SafetyValidation
from runtime.state_refresh import StateRefresh


class SoakRunner:
    """Runs a bounded operational health loop without destructive actions."""

    def __init__(
        self,
        *,
        proactive_checks: ProactiveChecks,
        task_tracker: AgentTaskTracker,
        state_refresh: StateRefresh,
        retention_policy: RetentionPolicy,
        safety_validation: SafetyValidation,
        adapter_resolver: Any,
        verifier: Any,
    ) -> None:
        self.proactive_checks = proactive_checks
        self.task_tracker = task_tracker
        self.state_refresh = state_refresh
        self.retention_policy = retention_policy
        self.safety_validation = safety_validation
        self.adapter_resolver = adapter_resolver
        self.verifier = verifier
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def run(self, iterations: int = 1) -> dict[str, Any]:
        iterations = max(1, min(iterations, 10))
        runs: list[dict[str, Any]] = []
        for _ in range(iterations):
            adapter = self.adapter_resolver()
            runs.append(
                {
                    "agent_status": adapter.connection_status(),
                    "pending_tasks": self.task_tracker.refresh_pending(adapter, self.verifier, limit=20),
                    "stale_refresh": self.state_refresh.refresh_stale(limit=20),
                    "proactive": self.proactive_checks.run_read_only(),
                    "retention": self.retention_policy.summary(),
                    "safety": self.safety_validation.run(),
                }
            )
        status = "success" if all(run["safety"]["status"] == "passed" for run in runs) else "failed"
        result = {
            "status": status,
            "iterations": iterations,
            "runs": runs,
            "validation": {
                "bounded": True,
                "read_only": True,
                "status": "validated" if status == "success" else "validation_pending",
            },
        }
        self.retention_policy.state_store.append_jsonl("action_record.jsonl", {"route": "ops_soak", "status": status, "artifacts": {"iterations": iterations, "validation": result["validation"]}})
        return result

    def start(self, *, iterations: int = 60, interval_seconds: float = 60.0) -> dict[str, Any]:
        iterations = max(1, min(int(iterations), 1440))
        interval_seconds = max(0.0, min(float(interval_seconds), 3600.0))
        with self._lock:
            if self._thread and self._thread.is_alive():
                return {**self.status(), "status": "already_running"}
            run_id = f"soak_{uuid4().hex[:12]}"
            self._stop_event = threading.Event()
            state = {
                "status": "running",
                "run_id": run_id,
                "started_at": utc_now_iso(),
                "updated_at": utc_now_iso(),
                "requested_iterations": iterations,
                "completed_iterations": 0,
                "interval_seconds": interval_seconds,
                "runs": [],
            }
            self._write_state(state)
            self._thread = threading.Thread(
                target=self._run_session,
                args=(run_id, iterations, interval_seconds, self._stop_event),
                daemon=True,
            )
            self._thread.start()
            self.retention_policy.state_store.append_jsonl("action_record.jsonl", {"route": "ops_soak_start", "status": "running", "artifacts": state})
        return self.status()

    def status(self) -> dict[str, Any]:
        state = self._read_state()
        alive = bool(self._thread and self._thread.is_alive())
        status = str(state.get("status") or "idle")
        if status == "running" and not alive:
            status = "stale"
        return {**state, "status": status, "thread_alive": alive}

    def stop(self) -> dict[str, Any]:
        thread = self._thread
        if thread and thread.is_alive():
            self._stop_event.set()
            self._patch_state({"status": "stopping", "stop_requested_at": utc_now_iso()})
            thread.join(timeout=2.0)
            status = self.status()
            self.retention_policy.state_store.append_jsonl("action_record.jsonl", {"route": "ops_soak_stop", "status": status.get("status"), "artifacts": status})
            return status
        state = self._read_state()
        if state.get("status") in {"running", "stale", "stopping"}:
            self._patch_state({"status": "stopped", "stopped_at": utc_now_iso()})
            self.retention_policy.state_store.append_jsonl("action_record.jsonl", {"route": "ops_soak_stop", "status": "stopped", "artifacts": self.status()})
        return self.status()

    def _run_session(self, run_id: str, iterations: int, interval_seconds: float, stop_event: threading.Event) -> None:
        final_status = "completed"
        for index in range(iterations):
            if stop_event.is_set():
                final_status = "stopped"
                break
            iteration = self.run(iterations=1)
            run_payload = iteration["runs"][0] if iteration.get("runs") else {}
            if iteration.get("status") == "failed":
                final_status = "failed"
            with self._lock:
                state = self._read_state()
                if state.get("run_id") != run_id:
                    return
                runs = state.get("runs") if isinstance(state.get("runs"), list) else []
                runs.append(
                    {
                        "iteration": index + 1,
                        "status": iteration.get("status"),
                        "checked_at": utc_now_iso(),
                        "result": run_payload,
                    }
                )
                self._write_state(
                    {
                        **state,
                        "status": "running" if final_status == "completed" else final_status,
                        "updated_at": utc_now_iso(),
                        "completed_iterations": index + 1,
                        "runs": runs[-200:],
                    }
                )
            if final_status == "failed":
                break
            if index < iterations - 1 and stop_event.wait(interval_seconds):
                final_status = "stopped"
                break
        with self._lock:
            state = self._read_state()
            if state.get("run_id") == run_id:
                final_state = {
                    **state,
                    "status": final_status,
                    "updated_at": utc_now_iso(),
                    "finished_at": utc_now_iso(),
                }
                self._write_state(final_state)
                self.retention_policy.state_store.append_jsonl(
                    "action_record.jsonl",
                    {"route": "ops_soak_session", "status": final_status, "artifacts": {"run_id": run_id, "completed_iterations": final_state.get("completed_iterations")}},
                )

    def _read_state(self) -> dict[str, Any]:
        state = self.retention_policy.state_store.read_json("ops_soak_state.json")
        return state or {"status": "idle", "runs": []}

    def _write_state(self, payload: dict[str, Any]) -> None:
        self.retention_policy.state_store.write_json("ops_soak_state.json", payload)

    def _patch_state(self, patch: dict[str, Any]) -> None:
        state = self._read_state()
        self._write_state({**state, **patch, "updated_at": utc_now_iso()})
