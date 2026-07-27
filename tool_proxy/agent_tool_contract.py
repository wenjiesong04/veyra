from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from core.action_risk import RISK_ORDER, assess_text_risk
from core.definitions import RiskLevel
from interface.agent_adapter import ExecutionResult


AGENT_TOOL_PROXY_CONTRACT_VERSION = "veyra.tool_proxy.v2"
ToolReceiptResolver = Callable[[str, str], Mapping[str, Any] | None]


def agent_tool_proxy_contract(task_id: str | None = None) -> dict[str, Any]:
    return {
        "contract_version": AGENT_TOOL_PROXY_CONTRACT_VERSION,
        "task_id": task_id,
        "enforcement": "required_for_agent_actions",
        "proposal_endpoint": "/actions/proposals",
        "result_callback_endpoint": "/agent/results",
        "risk_policy": {
            "R0_R1": "read-only actions may run directly but must return evidence",
            "R2": "scoped write actions must return policy/tool trace or ActionProposal evidence",
            "R3_R4": "must submit ActionProposal and wait for review approval before execution",
            "R5": "blocked; do not execute",
        },
        "required_result_evidence": [
            "tool_calls",
            "changed_files",
            "tool_calls containing exact canonical tool names",
            "raw.tool_receipt_refs containing run_id, tool_call_id, invocation_digest, and reported_call_index",
            "authoritative observed receipt resolved by Veyra for every reported tool call",
            "one exact Veyra VerifiedToolEffect projection for every observed receipt",
        ],
        "caller_reported_authority": "diagnostic_only; raw traces, proposals, review IDs, approved_by, result prose, and changed-file claims never establish authority",
        "forbidden": ["rm -rf", "curl | bash", "drop database", "drop table", "truncate table", "git push --force", "git reset --hard", "externalize_secrets"],
        "review_required": ["launchctl kickstart/bootstrap/bootout", "systemctl start/stop/restart", "brew services restart", "kill/pkill/killall"],
    }


class AgentToolCompliance:
    def __init__(self, receipt_resolver: ToolReceiptResolver | None = None) -> None:
        self.receipt_resolver = receipt_resolver

    def review_execution(self, execution_result: ExecutionResult) -> dict[str, Any]:
        tool_calls = [str(item) for item in execution_result.tool_calls]
        raw = execution_result.raw if isinstance(execution_result.raw, dict) else {}
        proxy_evidence = self._proxy_evidence(
            raw,
            expected_run_id=execution_result.task_id,
            reported_tool_calls=tool_calls,
        )
        if not tool_calls:
            return {
                "status": "not_observed",
                "reason": "agent reported no tool calls; Tool Proxy enforcement cannot be inferred",
                "max_risk": RiskLevel.R0.value,
                "findings": [],
                "warnings": ["empty tool-call evidence does not prove Tool Proxy enforcement"],
                "proxy_evidence": proxy_evidence,
                "tool_calls_reported": False,
                "enforcement_observed": False,
            }

        findings: list[dict[str, Any]] = []
        warnings: list[str] = []
        max_risk = RiskLevel.R0
        receipts_by_index = (
            proxy_evidence.get("receipts_by_index")
            if isinstance(proxy_evidence.get("receipts_by_index"), dict)
            else {}
        )
        for index, call in enumerate(tool_calls):
            assessment = assess_text_risk(call)
            receipt = receipts_by_index.get(str(index))
            authoritative_risk = (
                str(receipt.get("risk_level") or "")
                if isinstance(receipt, dict)
                else ""
            )
            try:
                reported_floor = RiskLevel(assessment.risk_level)
                risk = (
                    self._max_risk(
                        RiskLevel(authoritative_risk),
                        reported_floor,
                    )
                    if authoritative_risk
                    else reported_floor
                )
            except ValueError:
                risk = RiskLevel.R5
            max_risk = self._max_risk(max_risk, risk)
            finding = {
                "tool_call": call,
                "risk_level": risk.value,
                "risk_source": (
                    "authoritative_receipt_with_deterministic_floor"
                    if isinstance(receipt, dict)
                    else "caller_reported_fallback"
                ),
                "risk_assessment": assessment.to_dict(),
            }
            if isinstance(receipt, dict):
                finding["authoritative_tool"] = {
                    "tool_name": receipt.get("tool_name"),
                    "tool_call_id": receipt.get("tool_call_id"),
                    "invocation_digest": receipt.get("invocation_digest"),
                }
            if risk == RiskLevel.R5:
                finding["decision"] = "blocked"
                finding["reason"] = "forbidden tool call was reported by agent"
            elif risk in {RiskLevel.R3, RiskLevel.R4} and not (
                isinstance(receipt, dict)
                and receipt.get("approval_verified") is True
            ):
                finding["decision"] = "bypass_suspected"
                finding["reason"] = "R3-R4 tool call lacks an authoritative observed approval receipt"
            elif risk == RiskLevel.R2 and not proxy_evidence["trace"]:
                finding["decision"] = "trace_missing"
                finding["reason"] = "R2 tool call lacks an authoritative observed Tool Proxy receipt"
                warnings.append(f"R2 tool call lacks authoritative proxy receipt: {call}")
            elif not proxy_evidence["trace"]:
                finding["decision"] = "trace_missing"
                finding["reason"] = "tool call lacks an authoritative observed Tool Proxy receipt"
                warnings.append(f"tool call lacks authoritative proxy receipt: {call}")
            else:
                finding["decision"] = "compliant"
            findings.append(finding)

        if any(item.get("decision") == "blocked" for item in findings):
            status = "blocked"
            reason = "forbidden tool call reported"
        elif any(item.get("decision") == "bypass_suspected" for item in findings):
            status = "bypass_suspected"
            reason = "high-risk tool call lacks Veyra approval evidence"
        elif warnings:
            status = "warning"
            reason = "tool call proxy trace is incomplete"
        else:
            status = "compliant"
            reason = "tool calls have acceptable proxy evidence"
        return {
            "status": status,
            "reason": reason,
            "max_risk": max_risk.value,
            "findings": findings,
            "warnings": warnings,
            "proxy_evidence": proxy_evidence,
            "tool_calls_reported": True,
            "enforcement_observed": bool(proxy_evidence["tool_trace"]),
        }

    def _proxy_evidence(
        self,
        raw: dict[str, Any],
        *,
        expected_run_id: str,
        reported_tool_calls: list[str],
    ) -> dict[str, Any]:
        action_proposals = raw.get("action_proposals")
        tool_proxy_traces = raw.get("tool_proxy_traces") or raw.get("tool_traces")
        review_id = raw.get("review_id") or raw.get("approved_by")
        policy_trace = raw.get("policy_trace")
        caller_has_proposal = self._has_nonempty_dict_items(action_proposals)
        caller_has_tool_trace = self._has_nonempty_dict_items(tool_proxy_traces)
        caller_has_policy_trace = (
            isinstance(policy_trace, dict)
            and bool(policy_trace)
            or self._has_nonempty_dict_items(policy_trace)
        )
        caller_has_review = bool(review_id) or self._has_approved_proposal(action_proposals)
        authoritative_receipts, receipt_errors = self._resolve_authoritative_receipts(
            raw,
            expected_run_id=expected_run_id,
            reported_tool_calls=reported_tool_calls,
        )
        receipts_by_index = {
            str(receipt["_reported_call_index"]): receipt
            for receipt in authoritative_receipts
        }
        coverage_complete = (
            bool(reported_tool_calls)
            and len(authoritative_receipts) == len(reported_tool_calls)
            and set(receipts_by_index)
            == {str(index) for index in range(len(reported_tool_calls))}
            and not receipt_errors
        )
        has_tool_trace = coverage_complete
        has_review = bool(authoritative_receipts) and all(
            self._receipt_has_approval(receipt)
            for receipt in authoritative_receipts
        )
        return {
            # Caller-reported proposals, traces, policy records, and review IDs
            # are useful diagnostics only. They never establish authority.
            "proposal": False,
            "tool_trace": has_tool_trace,
            "policy_trace": False,
            "review": has_review,
            "trace": has_tool_trace,
            "authoritative_receipts": authoritative_receipts,
            "receipts_by_index": receipts_by_index,
            "receipt_errors": receipt_errors,
            "coverage_complete": coverage_complete,
            "caller_reported": {
                "proposal": caller_has_proposal,
                "tool_trace": caller_has_tool_trace,
                "policy_trace": caller_has_policy_trace,
                "review": caller_has_review,
            },
        }

    def _resolve_authoritative_receipts(
        self,
        raw: dict[str, Any],
        *,
        expected_run_id: str,
        reported_tool_calls: list[str],
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        refs = raw.get("tool_receipt_refs")
        if not isinstance(refs, list) or not refs:
            return [], []
        if self.receipt_resolver is None:
            return [], [{"reason": "authoritative_receipt_resolver_unavailable"}]

        receipts: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        seen_indices: set[int] = set()
        for item in refs:
            if not isinstance(item, dict):
                errors.append({"reason": "invalid_receipt_reference"})
                continue
            run_id = str(item.get("run_id") or "").strip()
            tool_call_id = str(item.get("tool_call_id") or "").strip()
            invocation_digest = str(
                item.get("invocation_digest") or ""
            ).strip()
            reported_call_index = item.get("reported_call_index")
            key = (run_id, tool_call_id)
            if (
                not run_id
                or not tool_call_id
                or len(invocation_digest) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in invocation_digest
                )
                or isinstance(reported_call_index, bool)
                or not isinstance(reported_call_index, int)
                or reported_call_index < 0
                or reported_call_index >= len(reported_tool_calls)
            ):
                errors.append(
                    {
                        "run_id": run_id,
                        "tool_call_id": tool_call_id,
                        "reason": "receipt_reference_missing_or_invalid_binding",
                    }
                )
                continue
            if run_id != expected_run_id:
                errors.append(
                    {
                        "run_id": run_id,
                        "tool_call_id": tool_call_id,
                        "reason": "receipt_reference_run_mismatch",
                    }
                )
                continue
            if key in seen:
                errors.append(
                    {
                        "run_id": run_id,
                        "tool_call_id": tool_call_id,
                        "reason": "duplicate_receipt_reference",
                    }
                )
                continue
            if reported_call_index in seen_indices:
                errors.append(
                    {
                        "run_id": run_id,
                        "tool_call_id": tool_call_id,
                        "reason": "duplicate_reported_call_index",
                    }
                )
                continue
            seen.add(key)
            seen_indices.add(reported_call_index)
            try:
                resolved = self.receipt_resolver(run_id, tool_call_id)
            except Exception:
                errors.append(
                    {
                        "run_id": run_id,
                        "tool_call_id": tool_call_id,
                        "reason": "receipt_resolution_failed",
                    }
                )
                continue
            if not isinstance(resolved, Mapping):
                errors.append(
                    {
                        "run_id": run_id,
                        "tool_call_id": tool_call_id,
                        "reason": "receipt_not_found",
                    }
                )
                continue
            receipt = dict(resolved)
            if str(receipt.get("run_id") or "").strip() != run_id or str(
                receipt.get("tool_call_id") or ""
            ).strip() != tool_call_id:
                errors.append(
                    {
                        "run_id": run_id,
                        "tool_call_id": tool_call_id,
                        "reason": "receipt_scope_mismatch",
                    }
                )
                continue
            if (
                str(receipt.get("invocation_digest") or "").strip()
                != invocation_digest
            ):
                errors.append(
                    {
                        "run_id": run_id,
                        "tool_call_id": tool_call_id,
                        "reason": "receipt_invocation_mismatch",
                    }
                )
                continue
            reported_tool_name = str(
                reported_tool_calls[reported_call_index] or ""
            ).strip()
            if (
                not reported_tool_name
                or str(receipt.get("tool_name") or "").strip()
                != reported_tool_name
            ):
                errors.append(
                    {
                        "run_id": run_id,
                        "tool_call_id": tool_call_id,
                        "reason": "receipt_tool_name_mismatch",
                    }
                )
                continue
            if str(receipt.get("risk_level") or "") not in {
                level.value for level in RiskLevel
            }:
                errors.append(
                    {
                        "run_id": run_id,
                        "tool_call_id": tool_call_id,
                        "reason": "receipt_risk_missing_or_invalid",
                    }
                )
                continue
            receipt_risk = str(receipt.get("risk_level") or "")
            deterministic_floor = assess_text_risk(
                reported_tool_name
            ).risk_level
            if RISK_ORDER.index(receipt_risk) < RISK_ORDER.index(
                deterministic_floor
            ):
                errors.append(
                    {
                        "run_id": run_id,
                        "tool_call_id": tool_call_id,
                        "reason": "receipt_risk_below_deterministic_floor",
                    }
                )
                continue
            if not self._receipt_is_observed(receipt):
                errors.append(
                    {
                        "run_id": run_id,
                        "tool_call_id": tool_call_id,
                        "reason": "receipt_not_observed",
                    }
                )
                continue
            receipt["_reported_call_index"] = reported_call_index
            receipts.append(receipt)
        return receipts, errors

    def _receipt_is_observed(self, receipt: dict[str, Any]) -> bool:
        # Only the authoritative ledger's explicit terminal states count. In
        # particular, reserved/authorized_not_observed is indeterminate and
        # must never be upgraded merely because a caller supplied observation
        # shaped metadata.
        state = str(receipt.get("ledger_state") or "").strip().lower()
        return state in {"observed_success", "observed_failure"}

    def _receipt_has_approval(self, receipt: dict[str, Any]) -> bool:
        return receipt.get("approval_verified") is True

    def _has_nonempty_dict_items(self, value: Any) -> bool:
        return isinstance(value, list) and any(
            isinstance(item, dict) and bool(item)
            for item in value
        )

    def _has_approved_proposal(self, proposals: Any) -> bool:
        if not isinstance(proposals, list):
            return False
        return any(isinstance(item, dict) and str(item.get("status") or item.get("decision")) in {"approved", "allow", "allow_with_constraints"} for item in proposals)

    def _max_risk(self, left: RiskLevel, right: RiskLevel) -> RiskLevel:
        order = list(RiskLevel)
        return right if order.index(right) > order.index(left) else left
