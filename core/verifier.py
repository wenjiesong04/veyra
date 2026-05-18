from __future__ import annotations

from typing import Any

from core.definitions import RiskLevel
from interface.agent_adapter import ExecutionResult


class Verifier:
    def verify_probe_result(self, probe_result: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "success" if probe_result.get("status") != "error" else "failed",
            "message": probe_result.get("summary", "Probe completed."),
            "risk_level": RiskLevel.R1.value,
        }

    def verify_execution_result(self, execution_result: ExecutionResult) -> dict[str, str]:
        if execution_result.status in {"success", "submitted"}:
            return {"status": execution_result.status, "verdict": "accepted_for_v0_1"}
        if execution_result.status == "adapter_unconfigured":
            return {"status": "adapter_unconfigured", "verdict": "agent_runtime_not_connected"}
        return {"status": "failed", "verdict": "execution_result_reported_failure"}
