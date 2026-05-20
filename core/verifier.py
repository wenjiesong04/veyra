from __future__ import annotations

from typing import Any

from core.definitions import RiskLevel
from interface.agent_adapter import ExecutionResult


class Verifier:
    def verify_probe_result(self, probe_result: dict[str, Any]) -> dict[str, Any]:
        raw_status = str(probe_result.get("status") or "unknown")
        evidence = {
            "source": probe_result.get("source"),
            "target": probe_result.get("target"),
            "observed_at": probe_result.get("observed_at"),
            "details": probe_result.get("details", {}),
        }
        success = raw_status not in {"error", "failed", "timeout"}
        return {
            "status": "verified_success" if success else "verified_failed",
            "verdict": "probe_result_has_structured_evidence" if success else "probe_reported_failure",
            "message": probe_result.get("summary", "Probe completed."),
            "risk_level": RiskLevel.R1.value,
            "confidence": float(probe_result.get("confidence", 0.8 if success else 0.35)),
            "evidence": evidence,
            "next_action": "update_world_state" if success else "retry_or_escalate_probe",
        }

    def verify_execution_result(self, execution_result: ExecutionResult) -> dict[str, Any]:
        evidence = self._execution_evidence(execution_result)
        status = execution_result.status
        changed_files = bool(execution_result.changed_files)
        has_tool_calls = bool(execution_result.tool_calls)
        has_raw = bool(execution_result.raw)
        has_result = bool(execution_result.result.strip())
        has_evidence = has_result or changed_files or has_tool_calls or has_raw

        if status == "success" and has_evidence:
            return {
                "status": "verified_success",
                "verdict": "execution_success_supported_by_evidence",
                "confidence": 0.82 if changed_files or has_tool_calls else 0.74,
                "evidence": evidence,
                "next_action": "update_state_and_memory",
                "needs_rollback": False,
                "needs_memory_patch": True,
            }
        if status == "success":
            return {
                "status": "needs_more_probe",
                "verdict": "execution_success_without_evidence",
                "confidence": 0.42,
                "evidence": evidence,
                "next_action": "collect_post_execution_evidence",
                "needs_rollback": False,
                "needs_memory_patch": False,
            }
        if status in {"submitted", "running", "pending"}:
            return {
                "status": "partially_success",
                "verdict": f"execution_{status}_but_not_final",
                "confidence": 0.55,
                "evidence": evidence,
                "next_action": "poll_runtime_or_probe_result",
                "needs_rollback": False,
                "needs_memory_patch": False,
            }
        if execution_result.status == "adapter_unconfigured":
            return {
                "status": "needs_more_probe",
                "verdict": "agent_runtime_not_connected",
                "confidence": 0.9,
                "evidence": evidence,
                "next_action": "configure_or_refresh_agent_runtime",
                "needs_rollback": False,
                "needs_memory_patch": False,
            }
        if status in {"failed", "error", "timeout"}:
            needs_rollback = changed_files or self._raw_has_snapshot(execution_result.raw)
            return {
                "status": "needs_rollback" if needs_rollback else "verified_failed",
                "verdict": "execution_result_reported_failure",
                "confidence": 0.78,
                "evidence": evidence,
                "next_action": "rollback_or_compensate" if needs_rollback else "inspect_logs_and_retry",
                "needs_rollback": needs_rollback,
                "needs_memory_patch": False,
            }
        if status == "blocked":
            return {
                "status": "verified_failed",
                "verdict": "execution_blocked_by_policy",
                "confidence": 0.86,
                "evidence": evidence,
                "next_action": "show_policy_reason",
                "needs_rollback": False,
                "needs_memory_patch": False,
            }
        return {
            "status": "needs_more_probe",
            "verdict": "execution_status_unknown",
            "confidence": 0.35,
            "evidence": evidence,
            "next_action": "collect_execution_trace",
            "needs_rollback": False,
            "needs_memory_patch": False,
        }

    def _execution_evidence(self, execution_result: ExecutionResult) -> dict[str, Any]:
        return {
            "task_id": execution_result.task_id,
            "executor": execution_result.executor,
            "reported_status": execution_result.status,
            "has_result_text": bool(execution_result.result.strip()),
            "logs_present": bool(execution_result.logs.strip()),
            "changed_files": execution_result.changed_files,
            "tool_calls": execution_result.tool_calls,
            "raw_keys": sorted(execution_result.raw.keys()) if isinstance(execution_result.raw, dict) else [],
        }

    def _raw_has_snapshot(self, raw: dict[str, Any]) -> bool:
        if not isinstance(raw, dict):
            return False
        if raw.get("snapshot") or raw.get("snapshot_id"):
            return True
        return any(isinstance(value, dict) and self._raw_has_snapshot(value) for value in raw.values())
