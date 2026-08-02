from __future__ import annotations

import threading
from time import perf_counter
from typing import Any, Callable
from uuid import uuid4

from core.runtime_entity import RuntimeEntity
from core.world_state import WorldStateStore
from interface.event_schema import utc_now_iso


class ActiveRuntimeLoop:
    """Background read-only awareness loop for Veyra's continuous entity mode."""

    def __init__(
        self,
        *,
        state_store: WorldStateStore,
        runtime_entity: RuntimeEntity,
        proactive_checks: Any,
        state_refresh: Any,
        external_world_refresh: Any,
        runtime_matrix: Any,
        retention_policy: Any,
        task_tracker: Any,
        adapter_resolver: Callable[[], Any],
        verifier: Any,
        replay_runtime: Any | None = None,
        commitment_push: Any | None = None,
        event_consumer: Callable[..., Any] | None = None,
        project_guardian_producers: Callable[..., Any] | None = None,
        project_guardian: Callable[..., Any] | None = None,
        project_guardian_attention: Callable[..., Any] | None = None,
        case_recovery: Callable[..., Any] | None = None,
    ) -> None:
        self.state_store = state_store
        self.runtime_entity = runtime_entity
        self.proactive_checks = proactive_checks
        self.state_refresh = state_refresh
        self.external_world_refresh = external_world_refresh
        self.runtime_matrix = runtime_matrix
        self.retention_policy = retention_policy
        self.replay_runtime = replay_runtime
        self.commitment_push = commitment_push
        self.event_consumer = event_consumer
        self.project_guardian_producers = project_guardian_producers
        self.project_guardian = project_guardian
        self.project_guardian_attention = project_guardian_attention
        self.case_recovery = case_recovery
        self.task_tracker = task_tracker
        self.adapter_resolver = adapter_resolver
        self.verifier = verifier
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, *, interval_seconds: float = 300.0) -> dict[str, Any]:
        interval_seconds = max(5.0, min(float(interval_seconds), 3600.0))
        with self._lock:
            if self._thread and self._thread.is_alive():
                return {**self.status(), "status": "already_running"}
            loop_id = f"active_{uuid4().hex[:12]}"
            self._stop_event = threading.Event()
            self._write_state(
                {
                    "status": "running",
                    "enabled": True,
                    "loop_id": loop_id,
                    "started_at": utc_now_iso(),
                    "updated_at": utc_now_iso(),
                    "interval_seconds": interval_seconds,
                    "ticks": [],
                }
            )
            self._thread = threading.Thread(
                target=self._run_forever,
                args=(loop_id, interval_seconds, self._stop_event),
                daemon=True,
                name="veyra-active-loop",
            )
            self._thread.start()
        self.state_store.append_jsonl("action_record.jsonl", {"route": "active_loop_start", "status": "running", "artifacts": {"loop_id": loop_id, "interval_seconds": interval_seconds}})
        return self.status()

    def stop(self) -> dict[str, Any]:
        thread = self._thread
        if thread and thread.is_alive():
            self._stop_event.set()
            self._patch_state({"status": "stopping", "enabled": False, "stop_requested_at": utc_now_iso()})
            thread.join(timeout=2.0)
        state = self._read_state()
        if state.get("status") in {"running", "stopping", "stale"}:
            self._patch_state({"status": "stopped", "enabled": False, "stopped_at": utc_now_iso()})
        self.state_store.append_jsonl("action_record.jsonl", {"route": "active_loop_stop", "status": self.status().get("status"), "artifacts": self.status()})
        return self.status()

    def status(self) -> dict[str, Any]:
        state = self._read_state()
        alive = bool(self._thread and self._thread.is_alive())
        status = str(state.get("status") or "stopped")
        if status == "running" and not alive:
            status = "stale"
        return {**state, "status": status, "thread_alive": alive}

    def tick(self, *, reason: str = "manual", include_runtime_matrix: bool = False) -> dict[str, Any]:
        started = perf_counter()
        tick_id = f"tick_{uuid4().hex[:12]}"
        steps = [
            self._step("heartbeat", lambda: self._heartbeat()),
            self._step("event_inbox", lambda: self._event_inbox_tick()),
            self._step(
                "project_guardian_producers",
                lambda: self._project_guardian_producers_tick(),
            ),
            self._step("project_guardian", lambda: self._project_guardian_tick()),
            self._step(
                "project_guardian_attention",
                lambda: self._project_guardian_attention_tick(),
            ),
            self._step("durable_cases", lambda: self._durable_case_tick()),
            self._step("pending_tasks", lambda: self.task_tracker.refresh_pending(self.adapter_resolver(), self.verifier, limit=20)),
            self._step("stale_state", lambda: self.state_refresh.refresh_stale(limit=20)),
            self._step("proactive", lambda: self.proactive_checks.run_read_only(timeout_seconds=12)),
            self._step("external_world", lambda: self.external_world_refresh.refresh_watchlist(limit=5)),
            self._step("commitment_push", lambda: self._commitment_push_tick()),
            self._step("replay_runtime", lambda: self._run_replay_runtime()),
            self._step("retention", lambda: self._retention_tick()),
        ]
        if include_runtime_matrix:
            steps.append(self._step("runtime_matrix", lambda: self.runtime_matrix.run(write_memory_probe=False)))
        status = (
            "success"
            if all(step.get("status") not in {"degraded", "error", "timeout"} for step in steps)
            else "degraded"
        )
        tick = {
            "tick_id": tick_id,
            "status": status,
            "reason": reason,
            "started_at": utc_now_iso(),
            "duration_ms": int((perf_counter() - started) * 1000),
            "steps": steps,
        }
        self._append_tick(tick)
        self.state_store.append_jsonl("action_record.jsonl", {"route": "active_loop_tick", "status": status, "artifacts": tick})
        self._retention_after_tick_audit()
        return tick

    def _run_forever(self, loop_id: str, interval_seconds: float, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            state = self._read_state()
            if state.get("loop_id") != loop_id:
                return
            try:
                self.tick(reason="scheduled", include_runtime_matrix=False)
            except Exception as exc:
                self._append_tick({"tick_id": f"tick_{uuid4().hex[:12]}", "status": "error", "reason": "scheduled", "error": str(exc), "error_type": type(exc).__name__, "started_at": utc_now_iso()})
            if stop_event.wait(interval_seconds):
                break
        state = self._read_state()
        if state.get("loop_id") == loop_id and state.get("status") in {"running", "stopping"}:
            def stop_matching_loop(current: dict[str, Any]) -> None:
                if current.get("loop_id") != loop_id or current.get("status") not in {"running", "stopping"}:
                    return
                current.update(
                    {
                        "status": "stopped",
                        "enabled": False,
                        "updated_at": utc_now_iso(),
                        "stopped_at": utc_now_iso(),
                    }
                )

            self.state_store.mutate_json("active_loop_state.json", stop_matching_loop)

    def _step(self, name: str, call: Callable[[], Any]) -> dict[str, Any]:
        started = perf_counter()
        try:
            result = call()
            result_status = str(result.get("status")) if isinstance(result, dict) and result.get("status") else "success"
            return {
                "name": name,
                "status": (
                    "success"
                    if result_status not in {"degraded", "error", "timeout"}
                    else result_status
                ),
                "result_status": result_status,
                "duration_ms": int((perf_counter() - started) * 1000),
                "result": self._compact_result(result),
            }
        except Exception as exc:
            return {"name": name, "status": "error", "duration_ms": int((perf_counter() - started) * 1000), "error": str(exc), "error_type": type(exc).__name__}

    def _heartbeat(self) -> dict[str, Any]:
        self.runtime_entity.set_status(self.runtime_entity.lifecycle.status)
        return {"status": "success", "heartbeat": self.runtime_entity.lifecycle.last_heartbeat_at}

    def _retention_tick(self) -> dict[str, Any]:
        summary = self.retention_policy.summary()
        over_limit = [item for item in summary.get("files", []) if item.get("status") == "over_limit"]
        if not over_limit:
            return {"status": "ok", "policy": summary.get("policy"), "files_checked": len(summary.get("files", [])), "changed": 0}
        enforced = self.retention_policy.enforce()
        return {
            "status": "success",
            "policy": enforced.get("policy"),
            "changed": enforced.get("changed", 0),
            "over_limit_before": len(over_limit),
            "files": enforced.get("files", []),
        }

    def _retention_after_tick_audit(self) -> None:
        try:
            summary = self.retention_policy.summary()
            if any(item.get("status") == "over_limit" for item in summary.get("files", []) if isinstance(item, dict)):
                self.retention_policy.enforce()
        except Exception:
            return

    def _commitment_push_tick(self) -> dict[str, Any]:
        if self.commitment_push is None:
            return {"status": "not_configured"}
        return self.commitment_push.run_due(limit=10, reason="active_loop")

    def _event_inbox_tick(self) -> dict[str, Any]:
        if self.event_consumer is None:
            return {"status": "not_configured"}
        result = self.event_consumer(limit=100)
        output = dict(result) if isinstance(result, dict) else {
            "status": "success",
            "result": result,
        }
        # The main runtime already supplies AwarenessLoop.process_event_inbox as
        # a bound method. Discover its observational maintenance hook without a
        # new constructor dependency, and keep any failure outside the Active
        # Loop and foreground route status.
        maintenance: dict[str, Any] = {"status": "not_configured"}
        try:
            owner = getattr(self.event_consumer, "__self__", None)
            event_awareness = getattr(owner, "event_awareness", None)
            reconcile = getattr(
                event_awareness,
                "reconcile_general_situations",
                None,
            )
            if callable(reconcile):
                candidate = reconcile(limit=100)
                maintenance = (
                    candidate
                    if isinstance(candidate, dict)
                    else {"status": "success"}
                )
        except Exception as exc:
            maintenance = {
                "status": "degraded",
                "error_type": type(exc).__name__,
                "route_change_allowed": False,
            }
        output["general_situation_maintenance"] = maintenance
        return output

    def _project_guardian_tick(self) -> dict[str, Any]:
        if self.project_guardian is None:
            return {"status": "not_configured"}
        return self.project_guardian(reason="active_loop")

    def _project_guardian_producers_tick(self) -> dict[str, Any]:
        if self.project_guardian_producers is None:
            return {"status": "not_configured"}
        return self.project_guardian_producers(reason="active_loop")

    def _project_guardian_attention_tick(self) -> dict[str, Any]:
        if self.project_guardian_attention is None:
            return {"status": "not_configured"}
        return self.project_guardian_attention(reason="active_loop")

    def _durable_case_tick(self) -> dict[str, Any]:
        if self.case_recovery is None:
            return {"status": "not_configured"}
        return self.case_recovery(limit=20, reason="active_loop")

    def _run_replay_runtime(self) -> dict[str, Any]:
        if self.replay_runtime is None:
            return {"status": "not_configured"}
        scan = self.replay_runtime.scan(limit=100)
        run = self.replay_runtime.run_pending(auto_create_reviews=True, limit=20)
        return {
            "status": "success",
            "created_count": scan.get("created_count", 0),
            "processed_count": run.get("processed_count", 0),
            "runtime": run.get("runtime") or scan.get("runtime"),
        }

    def _compact_result(self, value: Any) -> Any:
        if isinstance(value, dict):
            compact = {
                key: value.get(key)
                for key in (
                    "status",
                    "autonomy_level",
                    "state_gaps",
                    "summary",
                    "validation",
                    "skipped",
                    "remaining_stale",
                    "created_count",
                    "processed_count",
                    "due_count",
                    "candidate_count",
                    "policy_count",
                    "assessment_count",
                    "general_situation_count",
                    "observed_count",
                    "would_publish_count",
                    "published_count",
                    "projected_count",
                    "deduplicated_count",
                    "closure_count",
                )
                if key in value
            }
            refreshed = value.get("refreshed")
            if isinstance(refreshed, list):
                compact["refreshed"] = [
                    {
                        "claim": item.get("claim"),
                        "status": (item.get("probe_result") or {}).get("status") if isinstance(item.get("probe_result"), dict) else item.get("status"),
                    }
                    for item in refreshed[:8]
                    if isinstance(item, dict)
                ]
            self_heal = value.get("self_heal")
            if isinstance(self_heal, dict):
                compact["self_heal"] = {
                    key: self_heal.get(key)
                    for key in (
                        "playbook_id",
                        "status",
                        "mode",
                        "effective_autonomy_level",
                        "failure_confirmation_count",
                        "attempt_count",
                        "cooldown_until",
                        "breaker_open",
                        "review_id",
                    )
                    if key in self_heal
                }
            return compact
        return value

    def _append_tick(self, tick: dict[str, Any]) -> None:
        with self._lock:
            def append_tick(state: dict[str, Any]) -> None:
                ticks = state.get("ticks") if isinstance(state.get("ticks"), list) else []
                ticks.append(tick)
                state.update(
                    {
                        "status": "running" if state.get("enabled") else state.get("status", "stopped"),
                        "updated_at": utc_now_iso(),
                        "last_tick": tick,
                        "ticks": ticks[-100:],
                    }
                )

            self.state_store.mutate_json("active_loop_state.json", append_tick)

    def _read_state(self) -> dict[str, Any]:
        return self.state_store.read_json("active_loop_state.json") or {"status": "stopped", "enabled": False, "ticks": []}

    def _write_state(self, payload: dict[str, Any]) -> None:
        self.state_store.write_json("active_loop_state.json", payload)

    def _patch_state(self, patch: dict[str, Any]) -> None:
        self.state_store.patch_json(
            "active_loop_state.json",
            {**patch, "updated_at": utc_now_iso()},
        )
