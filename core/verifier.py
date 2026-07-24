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
        tool_proxy_compliance = self.agent_tool_compliance.review_execution(execution_result)
        evidence["tool_proxy_compliance"] = tool_proxy_compliance
        structured_evidence = self._structured_execution_evidence(execution_result, tool_proxy_compliance)
        evidence["structured_evidence"] = structured_evidence
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

        if status == "success" and tool_proxy_compliance["status"] == "warning":
            return {
                "status": "needs_more_probe",
                "verdict": "tool_proxy_trace_missing",
                "confidence": 0.32,
                "evidence": evidence,
                "next_action": "require_action_proposal_or_tool_trace",
                "needs_rollback": bool(changed_files),
                "needs_memory_patch": False,
                "risk_level": tool_proxy_compliance.get("max_risk"),
            }
        if status == "success" and changed_files and not execution_result.tool_calls:
            return {
                "status": "needs_more_probe",
                "verdict": "changed_files_without_tool_execution_trace",
                "confidence": 0.28,
                "evidence": evidence,
                "next_action": "require_tool_trace_and_verify_changed_files",
                "needs_rollback": True,
                "needs_memory_patch": False,
            }

        if status == "success" and structured_evidence["sufficient"]:
            return {
                "status": "verified_success",
                "verdict": "execution_success_supported_by_structured_evidence",
                "confidence": 0.84 if tool_proxy_compliance.get("enforcement_observed") else 0.72,
                "evidence": evidence,
                "next_action": "update_state_and_memory",
                "needs_rollback": False,
                "needs_memory_patch": True,
            }
        if status == "success" and structured_evidence["agent_plan_only"]:
            return {
                "status": "partially_success",
                "verdict": "agent_plan_returned_without_execution_evidence",
                "confidence": 0.58,
                "evidence": evidence,
                "next_action": "present_plan_without_claiming_execution",
                "needs_rollback": False,
                "needs_memory_patch": False,
            }
        if status == "success":
            return {
                "status": "needs_more_probe",
                "verdict": "execution_success_without_structured_evidence",
                "confidence": 0.3,
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
            "result_text_is_evidence": False,
            "raw_presence_is_evidence": False,
        }

    def _structured_execution_evidence(
        self,
        execution_result: ExecutionResult,
        tool_proxy_compliance: dict[str, Any],
    ) -> dict[str, Any]:
        """Identify outcome evidence without treating arbitrary text/raw presence as proof."""
        raw = execution_result.raw if isinstance(execution_result.raw, dict) else {}
        sources: list[str] = []

        for key in ("evidence", "evidence_used", "verification_evidence", "post_execution_evidence"):
            if self._is_evidence_container(raw.get(key)):
                sources.append(f"raw.{key}")

        probe_result = raw.get("probe_result")
        if isinstance(probe_result, dict) and any(probe_result.get(key) for key in ("source", "probe", "observed_at", "details")):
            sources.append("raw.probe_result")

        verification = raw.get("verification")
        if isinstance(verification, dict) and str(verification.get("status") or "").startswith("verified"):
            sources.append("raw.verification")

        execution = raw.get("execution_result")
        if self._has_observed_execution_payload(execution):
            sources.append("raw.execution_result")

        agent_response = raw.get("agent_response")
        if isinstance(agent_response, dict):
            if self._is_evidence_container(agent_response.get("evidence_used")):
                sources.append("raw.agent_response.evidence_used")

        proxy_evidence = tool_proxy_compliance.get("proxy_evidence")
        if (
            isinstance(proxy_evidence, dict)
            and proxy_evidence.get("tool_trace")
            and tool_proxy_compliance.get("status") == "compliant"
        ):
            sources.append("raw.tool_proxy_traces")

        raw_change_evidence = any(
            self._is_evidence_container(raw.get(key))
            for key in ("diff", "snapshot", "checksums", "file_verification")
        )
        if execution_result.changed_files and raw_change_evidence:
            sources.append("changed_files_with_verification")

        unique_sources = list(dict.fromkeys(sources))
        agent_plan_only = bool(
            isinstance(agent_response, dict)
            and (
                agent_response.get("answer_or_plan")
                or self._is_evidence_container(agent_response.get("proposed_actions"))
            )
            and not execution_result.tool_calls
            and not execution_result.changed_files
            and not unique_sources
        )
        return {
            "sufficient": bool(unique_sources),
            "sources": unique_sources,
            "agent_plan_only": agent_plan_only,
            "result_text_only": bool(execution_result.result.strip()) and not unique_sources,
            "raw_metadata_only": bool(raw) and not unique_sources,
        }

    def _is_evidence_container(self, value: Any) -> bool:
        if isinstance(value, dict):
            return bool(value)
        if isinstance(value, (list, tuple)):
            return any(
                isinstance(item, dict) and bool(item)
                or isinstance(item, str) and bool(item.strip())
                for item in value
            )
        return False

    def _has_observed_execution_payload(self, value: Any) -> bool:
        if not isinstance(value, dict) or not value:
            return False
        observation_keys = {
            "path",
            "content",
            "operation",
            "exit_code",
            "returncode",
            "stdout",
            "stderr",
            "snapshot",
            "snapshot_id",
            "checksum",
            "source_checksum",
            "target_checksum",
            "changed",
            "tool_trace",
        }
        return any(key in value and value.get(key) is not None for key in observation_keys)

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
