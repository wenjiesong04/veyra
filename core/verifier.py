from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import ValidationError

from core.definitions import RiskLevel
from interface.agent_adapter import ExecutionResult
from interface.agent_dialogue_contract import DialogueContractError, extract_agent_dialogue
from tool_proxy.agent_tool_contract import AgentToolCompliance, ToolReceiptResolver
from tool_proxy.governance_contract import VerifiedToolEffect, canonical_json


EXECUTION_FAILURE_MARKERS = (
    "assistant turn failed before producing content",
    "failed before producing content",
    "model turn failed",
    "no assistant content",
)
MAX_EFFECT_CLOCK_SKEW = timedelta(minutes=5)


class Verifier:
    def __init__(self, tool_receipt_resolver: ToolReceiptResolver | None = None) -> None:
        self._tool_receipt_resolver = tool_receipt_resolver
        self.agent_tool_compliance = AgentToolCompliance(tool_receipt_resolver)

    @property
    def tool_receipt_resolver(self) -> ToolReceiptResolver | None:
        return self._tool_receipt_resolver

    @tool_receipt_resolver.setter
    def tool_receipt_resolver(self, resolver: ToolReceiptResolver | None) -> None:
        self._tool_receipt_resolver = resolver
        self.agent_tool_compliance.receipt_resolver = resolver

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

    def verify_agent_dialogue(
        self,
        agent_response: Any,
        *,
        expected_message_id: str | None = None,
        expected_case_id: str,
        expected_case_revision: int,
        expected_turn_index: int,
        expected_in_reply_to: str,
        expected_task_packet_id: str,
        expected_operation_id: str,
        expected_scope_digest: str,
        expected_collaboration_binding: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Validate a proposal envelope without promoting it to evidence."""

        try:
            message = extract_agent_dialogue(
                agent_response,
                expected_message_id=expected_message_id,
                expected_case_id=expected_case_id,
                expected_case_revision=expected_case_revision,
                expected_turn_index=expected_turn_index,
                expected_in_reply_to=expected_in_reply_to,
                expected_task_packet_id=expected_task_packet_id,
                expected_operation_id=expected_operation_id,
                expected_scope_digest=expected_scope_digest,
                expected_collaboration_binding=(
                    expected_collaboration_binding
                ),
            )
        except DialogueContractError as exc:
            return {
                # A malformed proposal proves only that this Agent reply
                # cannot be accepted. It is not durable execution evidence
                # that the user's underlying Situation failed.
                "status": "needs_more_probe",
                "verdict": "agent_dialogue_contract_invalid",
                "confidence": 0.9,
                "evidence": {
                    "contract_errors": list(exc.errors),
                    "proposal_is_evidence": False,
                    "proposal_is_authority": False,
                },
                "next_action": "reject_dialogue_and_keep_case_evaluable",
                "needs_rollback": False,
                "needs_memory_patch": False,
            }
        if message is None:
            return {
                "status": "needs_more_probe",
                "verdict": "agent_dialogue_message_missing",
                "confidence": 0.9,
                "evidence": {
                    "proposal_is_evidence": False,
                    "proposal_is_authority": False,
                },
                "next_action": "request_exact_dialogue_envelope",
                "needs_rollback": False,
                "needs_memory_patch": False,
            }
        return {
            "status": "partially_success",
            "verdict": "agent_dialogue_proposal_requires_case_evaluation",
            "confidence": 0.8,
            "evidence": {
                "message_id": message["message_id"],
                "message_type": message["message_type"],
                "case_id": message["case_id"],
                "case_revision": message["case_revision"],
                "proposal_is_evidence": False,
                "proposal_is_authority": False,
                "proposal_is_verified_outcome": False,
            },
            "next_action": "evaluate_proposal_in_durable_case",
            "needs_rollback": False,
            "needs_memory_patch": False,
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

        if status == "success" and structured_evidence["authoritative_failure_observed"]:
            return {
                "status": "verified_failed",
                "verdict": "authoritative_tool_receipt_reported_failure",
                "confidence": 0.9,
                "evidence": evidence,
                "next_action": "inspect_tool_failure_or_reconcile_execution",
                "needs_rollback": bool(changed_files),
                "needs_memory_patch": False,
                "risk_level": tool_proxy_compliance.get("max_risk"),
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
        if (
            status == "success"
            and structured_evidence["authoritative_effect_evidence_invalid"]
        ):
            return {
                "status": "needs_more_probe",
                "verdict": "authoritative_effect_evidence_invalid",
                "confidence": 0.26,
                "evidence": evidence,
                "next_action": "reconcile_effect_evidence_with_tool_receipt",
                "needs_rollback": bool(changed_files),
                "needs_memory_patch": False,
                "risk_level": tool_proxy_compliance.get("max_risk"),
            }
        if (
            status == "success"
            and structured_evidence["caller_projection_mismatch"]
        ):
            return {
                "status": "needs_more_probe",
                "verdict": "caller_outcome_projection_mismatch",
                "confidence": 0.25,
                "evidence": evidence,
                "next_action": "use_authoritative_projection_and_reconcile_agent_report",
                "needs_rollback": bool(changed_files),
                "needs_memory_patch": False,
                "risk_level": tool_proxy_compliance.get("max_risk"),
            }

        if status == "success" and structured_evidence["dialogue_proposal_only"]:
            return {
                "status": "partially_success",
                "verdict": "agent_dialogue_proposal_requires_case_evaluation",
                "confidence": 0.62,
                "evidence": evidence,
                "next_action": "evaluate_proposal_in_durable_case",
                "needs_rollback": False,
                "needs_memory_patch": False,
            }
        if status == "success" and structured_evidence["sufficient"]:
            verified = {
                "status": "verified_success",
                "verdict": "execution_success_supported_by_structured_evidence",
                "confidence": 0.84 if tool_proxy_compliance.get("enforcement_observed") else 0.72,
                "evidence": evidence,
                "next_action": "update_state_and_memory",
                "needs_rollback": False,
                "needs_memory_patch": True,
            }
            if execution_result.tool_calls:
                verified.update(
                    {
                        "claim_scope": "authoritative_verified_projection_only",
                        "verified_projection": structured_evidence[
                            "authoritative_projection"
                        ],
                    }
                )
            return verified
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
        outcome_sources: list[str] = []
        reported_sources: list[str] = []
        reported_outcome_sources: list[str] = []

        for key in ("evidence", "evidence_used", "verification_evidence", "post_execution_evidence"):
            if self._is_evidence_container(raw.get(key)):
                if key == "post_execution_evidence":
                    outcome_sources.append(f"raw.{key}")
                else:
                    reported_sources.append(f"raw.{key}")
                    if self._is_outcome_evidence_container(raw.get(key)):
                        reported_outcome_sources.append(f"raw.{key}")

        probe_result = raw.get("probe_result")
        if isinstance(probe_result, dict) and any(probe_result.get(key) for key in ("source", "probe", "observed_at", "details")):
            outcome_sources.append("raw.probe_result")

        # A nested caller-reported verification verdict is not verification.
        verification = raw.get("verification")
        if isinstance(verification, dict) and str(verification.get("status") or "").startswith("verified"):
            reported_sources.append("raw.verification")

        execution = raw.get("execution_result")
        if self._has_observed_execution_payload(execution):
            # This nested projection comes from the caller and remains useful
            # for diagnostics, but it is not an authoritative observation.
            # Durable probe and tool-receipt evidence are classified above.
            reported_sources.append("raw.execution_result")
            reported_outcome_sources.append("raw.execution_result")

        agent_response = raw.get("agent_response")
        if isinstance(agent_response, dict):
            if self._is_evidence_container(agent_response.get("evidence_used")):
                reported_sources.append("raw.agent_response.evidence_used")
                if self._is_outcome_evidence_container(
                    agent_response.get("evidence_used")
                ):
                    reported_outcome_sources.append(
                        "raw.agent_response.evidence_used"
                    )
        dialogue_proposal_only = bool(
            isinstance(agent_response, dict)
            and isinstance(agent_response.get("dialogue_message"), dict)
        )
        if dialogue_proposal_only:
            reported_sources.append("raw.agent_response.dialogue_message")

        raw_change_evidence = any(
            self._is_evidence_container(raw.get(key))
            for key in ("diff", "snapshot", "checksums", "file_verification")
        )
        if execution_result.changed_files and raw_change_evidence:
            outcome_sources.append("changed_files_with_verification")

        proxy_evidence = tool_proxy_compliance.get("proxy_evidence")
        resolved_receipts = (
            proxy_evidence.get("authoritative_receipts", [])
            if isinstance(proxy_evidence, dict)
            else []
        )
        authoritative_receipts = sorted(
            resolved_receipts,
            key=lambda receipt: int(
                receipt.get("_reported_call_index", 0)
                if isinstance(receipt, Mapping)
                else 0
            ),
        )
        authoritative_failure_observed = any(
            isinstance(receipt, Mapping)
            and str(receipt.get("ledger_state") or "").strip().lower()
            == "observed_failure"
            for receipt in authoritative_receipts
        )
        authority_observed = bool(
            execution_result.tool_calls
            and tool_proxy_compliance.get("status") == "compliant"
            and authoritative_receipts
            and not authoritative_failure_observed
        )
        authoritative_effects: list[dict[str, Any]] = []
        authoritative_effect_errors: list[dict[str, str]] = []
        missing_authoritative_effects = 0
        for receipt in authoritative_receipts:
            if not isinstance(receipt, Mapping):
                authoritative_effect_errors.append(
                    {"reason": "authoritative_receipt_is_not_a_mapping"}
                )
                continue
            if not isinstance(receipt.get("effect_evidence"), Mapping):
                missing_authoritative_effects += 1
                continue
            effect, error = self._validated_authoritative_effect(receipt)
            if effect is None:
                authoritative_effect_errors.append(
                    {
                        "receipt_id": str(receipt.get("receipt_id") or ""),
                        "tool_call_id": str(
                            receipt.get("tool_call_id") or ""
                        ),
                        "reason": error
                        or "authoritative_effect_evidence_invalid",
                    }
                )
                continue
            authoritative_effects.append(effect)
        authoritative_effect_sources = [
            str(effect["source"]) for effect in authoritative_effects
        ]
        authoritative_projection = self._authoritative_projection(
            authoritative_effects
        )
        authoritative_effects_complete = bool(authoritative_receipts) and (
            len(authoritative_effects) == len(authoritative_receipts)
            and not authoritative_effect_errors
            and missing_authoritative_effects == 0
        )
        caller_projection_mismatch = bool(
            execution_result.tool_calls
            and authoritative_effects_complete
            and not self._changed_files_match_projection(
                execution_result.changed_files,
                authoritative_projection.get("changed_files", []),
            )
        )
        unique_outcome_sources = list(dict.fromkeys(outcome_sources))
        unique_reported_sources = list(dict.fromkeys(reported_sources))
        unique_reported_outcome_sources = list(dict.fromkeys(reported_outcome_sources))
        # Caller/Agent raw fields remain diagnostic even after an observed tool
        # receipt. A persistent effect is independently verified only when the
        # Veyra-owned receipt resolver attaches a verified effect-evidence record.
        # The contract-only Phase 3 runtime deliberately does not create one.
        independent_outcome_observed = (
            authoritative_effects_complete
            if execution_result.tool_calls
            else bool(unique_outcome_sources)
        )
        sufficient = (
            authority_observed
            and independent_outcome_observed
            and not caller_projection_mismatch
            if execution_result.tool_calls
            else bool(unique_outcome_sources)
        )
        agent_plan_only = bool(
            isinstance(agent_response, dict)
            and (
                agent_response.get("answer_or_plan")
                or self._is_evidence_container(agent_response.get("proposed_actions"))
                or dialogue_proposal_only
            )
            and not execution_result.tool_calls
            and not execution_result.changed_files
            and not unique_outcome_sources
        )
        return {
            "sufficient": sufficient,
            "sources": unique_outcome_sources,
            "reported_sources": unique_reported_sources,
            "reported_outcome_sources": unique_reported_outcome_sources,
            "authoritative_effect_sources": authoritative_effect_sources,
            "authoritative_effect_count": len(authoritative_effects),
            "authoritative_effect_missing_count": missing_authoritative_effects,
            "authoritative_effect_errors": authoritative_effect_errors,
            "authoritative_effect_evidence_invalid": bool(
                authoritative_effect_errors
            ),
            "authoritative_projection": authoritative_projection,
            "caller_projection_mismatch": caller_projection_mismatch,
            "caller_result_authenticated": False,
            "authority_observed": authority_observed,
            "authoritative_failure_observed": authoritative_failure_observed,
            "authoritative_receipt_count": len(authoritative_receipts),
            "independent_outcome_observed": independent_outcome_observed,
            "agent_plan_only": agent_plan_only,
            "dialogue_proposal_only": dialogue_proposal_only,
            "result_text_only": bool(execution_result.result.strip())
            and not unique_outcome_sources
            and not unique_reported_sources,
            "raw_metadata_only": bool(raw)
            and not unique_outcome_sources
            and not unique_reported_sources,
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

    def _validated_authoritative_effect(
        self,
        receipt: Mapping[str, Any],
    ) -> tuple[dict[str, Any] | None, str | None]:
        raw_effect = receipt.get("effect_evidence")
        if not isinstance(raw_effect, Mapping):
            return None, "authoritative_effect_evidence_missing"
        try:
            effect = VerifiedToolEffect.model_validate_json(
                canonical_json(dict(raw_effect)),
                strict=True,
            )
        except (TypeError, ValueError, ValidationError):
            return None, "authoritative_effect_contract_invalid"

        bindings = {
            "receipt_id": receipt.get("receipt_id"),
            "run_id": receipt.get("run_id"),
            "tool_call_id": receipt.get("tool_call_id"),
            "tool_name": receipt.get("tool_name"),
            "invocation_digest": receipt.get("invocation_digest"),
            "result_digest": receipt.get("result_digest"),
            "targets_digest": receipt.get("targets_digest"),
        }
        for field_name, expected in bindings.items():
            if getattr(effect, field_name) != expected:
                return None, f"authoritative_effect_{field_name}_mismatch"
        receipt_observed_at = self._aware_datetime(
            receipt.get("observed_at")
        )
        if receipt_observed_at is None:
            return None, "authoritative_receipt_observed_at_invalid"
        if effect.observed_at < receipt_observed_at:
            return None, "authoritative_effect_predates_receipt_observation"
        if effect.observed_at > datetime.now(timezone.utc) + MAX_EFFECT_CLOCK_SKEW:
            return None, "authoritative_effect_observed_at_in_future"
        return effect.model_dump(mode="json"), None

    def _authoritative_projection(
        self,
        effects: list[dict[str, Any]],
    ) -> dict[str, Any]:
        summaries: list[str] = []
        changed_files: list[str] = []
        tool_calls: list[str] = []
        receipt_ids: list[str] = []
        seen_files: set[str] = set()
        for effect in effects:
            summaries.append(str(effect["summary"]))
            tool_calls.append(str(effect["tool_name"]))
            receipt_ids.append(str(effect["receipt_id"]))
            for path in effect.get("changed_files", []):
                normalized = str(path)
                if normalized not in seen_files:
                    seen_files.add(normalized)
                    changed_files.append(normalized)
        return {
            "summary": "; ".join(summaries),
            "changed_files": changed_files,
            "tool_calls": tool_calls,
            "receipt_ids": receipt_ids,
            "source": "veyra_authoritative_tool_effects",
        }

    def _changed_files_match_projection(
        self,
        caller_files: list[str],
        authoritative_files: Any,
    ) -> bool:
        if not isinstance(authoritative_files, list):
            return False
        normalized_caller: list[str] = []
        for path in caller_files:
            if (
                not isinstance(path, str)
                or not path
                or path != path.strip()
                or "\x00" in path
            ):
                return False
            normalized_caller.append(path)
        if len(normalized_caller) != len(set(normalized_caller)):
            return False
        return set(normalized_caller) == set(authoritative_files)

    def _aware_datetime(self, value: Any) -> datetime | None:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str) and value.strip():
            try:
                parsed = datetime.fromisoformat(
                    value.strip().replace("Z", "+00:00")
                )
            except ValueError:
                return None
        else:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc)

    def _is_outcome_evidence_container(self, value: Any) -> bool:
        outcome_keys = {
            "source",
            "observed_at",
            "path",
            "checksum",
            "source_checksum",
            "target_checksum",
            "exit_code",
            "returncode",
            "stdout",
            "stderr",
            "changed",
            "result",
            "outcome",
        }
        if isinstance(value, dict):
            return any(key in value and value.get(key) is not None for key in outcome_keys)
        if isinstance(value, (list, tuple)):
            return any(
                isinstance(item, dict)
                and any(
                    key in item and item.get(key) is not None
                    for key in outcome_keys
                )
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
