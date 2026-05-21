from __future__ import annotations

from typing import Any

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
        return {"status": status, "iterations": iterations, "runs": runs}
