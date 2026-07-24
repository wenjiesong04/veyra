from __future__ import annotations

import threading
from typing import Any
from uuid import uuid4

from core.model_client import redact_sensitive
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
        external_runtime_probe: Any | None = None,
    ) -> None:
        self.proactive_checks = proactive_checks
        self.task_tracker = task_tracker
        self.state_refresh = state_refresh
        self.retention_policy = retention_policy
        self.safety_validation = safety_validation
        self.adapter_resolver = adapter_resolver
        self.verifier = verifier
        self.external_runtime_probe = external_runtime_probe
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
                    "external_runtime": self._external_runtime_status(),
                }
            )
        status = "success" if all(run["safety"]["status"] == "passed" for run in runs) else "failed"
        external_validation = self._external_runtime_validation(runs)
        result = {
            "status": status,
            "iterations": iterations,
            "runs": runs,
            "validation": {
                "bounded": True,
                "read_only": True,
                "safety_status": "validated" if status == "success" else "failed",
                "external_runtime": external_validation,
                "status": self._validation_status(status=status, external_status=external_validation.get("status", "not_configured")),
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
            elif stop_event.is_set():
                final_status = "stopped"
            with self._lock:
                recorded = False

                def append_iteration(state: dict[str, Any]) -> None:
                    nonlocal recorded
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
                    state.update(
                        {
                            "status": "running" if final_status == "completed" else final_status,
                            "updated_at": utc_now_iso(),
                            "completed_iterations": index + 1,
                            "runs": runs[-200:],
                        }
                    )
                    recorded = True

                self.retention_policy.state_store.mutate_json(
                    "ops_soak_state.json",
                    append_iteration,
                )
                if not recorded:
                    return
            if final_status == "failed":
                break
            if index < iterations - 1 and stop_event.wait(interval_seconds):
                final_status = "stopped"
                break
        with self._lock:
            final_state: dict[str, Any] = {}

            def finish_session(state: dict[str, Any]) -> None:
                nonlocal final_state
                if state.get("run_id") != run_id:
                    return
                state.update(
                    {
                        "status": final_status,
                        "updated_at": utc_now_iso(),
                        "finished_at": utc_now_iso(),
                    }
                )
                final_state = dict(state)

            self.retention_policy.state_store.mutate_json(
                "ops_soak_state.json",
                finish_session,
            )
            if final_state:
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
        self.retention_policy.state_store.patch_json(
            "ops_soak_state.json",
            {**patch, "updated_at": utc_now_iso()},
        )

    def _external_runtime_status(self) -> dict[str, Any]:
        if not self.external_runtime_probe:
            return {
                "status": "not_configured",
                "validation": {"status": "not_configured"},
                "reason": "external runtime probe is not configured",
            }
        try:
            result = self.external_runtime_probe()
        except Exception as exc:
            return {"status": "error", "validation": {"status": "error"}, "reason": str(exc)}
        if not isinstance(result, dict):
            return {"status": "error", "validation": {"status": "error"}, "reason": "external runtime probe did not return dict"}
        validation = result.get("validation") if isinstance(result.get("validation"), dict) else {}
        if "status" not in validation:
            validation["status"] = str(result.get("status") or "validation_pending")
        return {**result, "validation": validation}

    def _external_runtime_validation(self, runs: list[dict[str, Any]]) -> dict[str, Any]:
        statuses: list[str] = []
        for run in runs:
            external = run.get("external_runtime") if isinstance(run.get("external_runtime"), dict) else {}
            validation = external.get("validation") if isinstance(external.get("validation"), dict) else {}
            statuses.append(str(validation.get("status") or external.get("status") or "validation_pending"))
        by_status: dict[str, int] = {}
        for item in statuses:
            by_status[item] = by_status.get(item, 0) + 1
        latest = runs[-1].get("external_runtime") if runs else {}
        if any(item == "error" for item in statuses):
            status = "error"
        elif any(item == "validation_pending" for item in statuses):
            status = "validation_pending"
        elif any(item == "validated" for item in statuses):
            status = "validated"
        else:
            status = "not_configured"
        return {
            "status": status,
            "samples": len(statuses),
            "by_status": by_status,
            "latest": redact_sensitive(latest, max_string=1400, max_list=40),
        }

    def _validation_status(self, *, status: str, external_status: str) -> str:
        if status != "success":
            return "validation_pending"
        if external_status == "error":
            return "validation_pending"
        if external_status == "validation_pending":
            return "validation_pending"
        if external_status == "validated":
            return "validated"
        return "not_configured"
