from __future__ import annotations

from typing import Any

from core.definitions import RiskLevel
from interface.agent_adapter import ExecutionResult
from tool_proxy.agent_tool_contract import AgentToolCompliance


EXECUTION_FAILURE_MARKERS = (
    "assistant turn failed before producing content",
    "failed before producing content",
    "model turn failed",
    "no assistant content",
)


class Verifier:
    def __init__(self) -> None:
        self.agent_tool_compliance = AgentToolCompliance()

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
        tool_proxy_compliance = self.agent_tool_compliance.review_execution(execution_result)
        evidence["tool_proxy_compliance"] = tool_proxy_compliance
        evidence_mismatch = self._evidence_mismatch(execution_result)
        if evidence_mismatch:
            evidence["evidence_mismatch"] = evidence_mismatch
            return {
                "status": "verified_failed",
                "verdict": "evidence_mismatch",
                "confidence": 0.87,
                "evidence": evidence,
                "next_action": "refresh_evidence_or_retry_agent",
                "needs_rollback": bool(changed_files),
                "needs_memory_patch": False,
            }

        if status == "success" and self._has_failure_marker(execution_result):
            return {
                "status": "verified_failed",
                "verdict": "execution_result_contains_failure_marker",
                "confidence": 0.84,
                "evidence": evidence,
                "next_action": "retry_agent_or_inspect_runtime",
                "needs_rollback": bool(changed_files),
                "needs_memory_patch": False,
            }

        if tool_proxy_compliance["status"] == "blocked":
            return {
                "status": "verified_failed",
                "verdict": "forbidden_tool_call_reported",
                "confidence": 0.88,
                "evidence": evidence,
                "next_action": "block_and_request_safe_plan",
                "needs_rollback": bool(changed_files),
                "needs_memory_patch": False,
                "risk_level": tool_proxy_compliance.get("max_risk"),
            }
        if status == "success" and tool_proxy_compliance["status"] == "bypass_suspected":
            return {
                "status": "needs_more_probe",
                "verdict": "tool_proxy_bypass_suspected",
                "confidence": 0.3,
                "evidence": evidence,
                "next_action": "require_action_proposal_or_tool_trace",
                "needs_rollback": bool(changed_files),
                "needs_memory_patch": False,
                "risk_level": tool_proxy_compliance.get("max_risk"),
            }

        if status == "success" and has_evidence:
            return {
                "status": "verified_success",
                "verdict": "execution_success_supported_by_evidence"
                if tool_proxy_compliance["status"] != "warning"
                else "execution_success_with_tool_proxy_warning",
                "confidence": 0.68 if tool_proxy_compliance["status"] == "warning" else (0.82 if changed_files or has_tool_calls else 0.74),
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

    def _has_failure_marker(self, execution_result: ExecutionResult) -> bool:
        text = str(execution_result.result or "").strip().lower()
        if any(marker in text for marker in EXECUTION_FAILURE_MARKERS):
            return True
        raw = execution_result.raw if isinstance(execution_result.raw, dict) else {}
        for key in ("error", "failure", "message", "result", "summary"):
            value = raw.get(key)
            if isinstance(value, str) and any(marker in value.strip().lower() for marker in EXECUTION_FAILURE_MARKERS):
                return True
        return False

    def _raw_has_snapshot(self, raw: dict[str, Any]) -> bool:
        if not isinstance(raw, dict):
            return False
        if raw.get("snapshot") or raw.get("snapshot_id"):
            return True
        return any(isinstance(value, dict) and self._raw_has_snapshot(value) for value in raw.values())

    def _evidence_mismatch(self, execution_result: ExecutionResult) -> dict[str, Any] | None:
        raw = execution_result.raw if isinstance(execution_result.raw, dict) else {}
        text = str(execution_result.result or "")
        if not raw or not text:
            return None
        expected_terms = self._string_list(raw.get("expected_terms"))
        forbidden_terms = self._string_list(raw.get("forbidden_terms"))
        probe = raw.get("probe_result") if isinstance(raw.get("probe_result"), dict) else {}
        if probe.get("probe") == "weather_probe":
            details = probe.get("details") if isinstance(probe.get("details"), dict) else {}
            location = str(details.get("location") or probe.get("target") or "").strip()
            requested = str(probe.get("target") or details.get("location_query") or "").strip()
            if (location or requested) and any(term in text for term in ("纽约", "New York", "new york")) and not any(
                term and term in text for term in (location, requested)
            ):
                return {"reason": "weather_location_conflict", "expected_location": location or requested, "conflicting_location": "New York"}
        missing = [term for term in expected_terms if term and term not in text]
        forbidden = [term for term in forbidden_terms if term and term in text]
        if forbidden:
            return {"reason": "forbidden_term_present", "forbidden_terms": forbidden[:8]}
        if expected_terms and len(missing) == len(expected_terms):
            return {"reason": "expected_evidence_absent", "missing_terms": missing[:8]}
        return None

    def _string_list(self, value: Any) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return [str(item) for item in value if item]
        return []
