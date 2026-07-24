from __future__ import annotations

from typing import Any

from core.action_risk import assess_text_risk
from core.definitions import RiskLevel
from interface.agent_adapter import ExecutionResult


AGENT_TOOL_PROXY_CONTRACT_VERSION = "veyra.tool_proxy.v1"


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
            "raw.action_proposals or raw.tool_proxy_traces for R2+ tool actions",
            "review_id or approved_by for R3-R4 actions",
        ],
        "forbidden": ["rm -rf", "curl | bash", "drop database", "drop table", "truncate table", "git push --force", "git reset --hard", "externalize_secrets"],
        "review_required": ["launchctl kickstart/bootstrap/bootout", "systemctl start/stop/restart", "brew services restart", "kill/pkill/killall"],
    }


class AgentToolCompliance:
    def review_execution(self, execution_result: ExecutionResult) -> dict[str, Any]:
        tool_calls = [str(item) for item in execution_result.tool_calls]
        raw = execution_result.raw if isinstance(execution_result.raw, dict) else {}
        proxy_evidence = self._proxy_evidence(raw)
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
        for call in tool_calls:
            assessment = assess_text_risk(call)
            risk = RiskLevel(assessment.risk_level)
            max_risk = self._max_risk(max_risk, risk)
            finding = {"tool_call": call, "risk_level": risk.value, "risk_assessment": assessment.to_dict()}
            if risk == RiskLevel.R5:
                finding["decision"] = "blocked"
                finding["reason"] = "forbidden tool call was reported by agent"
            elif risk in {RiskLevel.R3, RiskLevel.R4} and not proxy_evidence["review"]:
                finding["decision"] = "bypass_suspected"
                finding["reason"] = "R3-R4 tool call lacks approved ActionProposal or review evidence"
            elif risk == RiskLevel.R2 and not proxy_evidence["trace"]:
                finding["decision"] = "trace_missing"
                finding["reason"] = "R2 tool call lacks Tool Proxy or ActionProposal trace"
                warnings.append(f"R2 tool call lacks proxy trace: {call}")
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
            "enforcement_observed": bool(
                proxy_evidence["tool_trace"]
                or proxy_evidence["proposal"]
                or proxy_evidence["policy_trace"]
                or proxy_evidence["review"]
            ),
        }

    def _proxy_evidence(self, raw: dict[str, Any]) -> dict[str, bool]:
        action_proposals = raw.get("action_proposals")
        tool_proxy_traces = raw.get("tool_proxy_traces") or raw.get("tool_traces")
        review_id = raw.get("review_id") or raw.get("approved_by")
        policy_trace = raw.get("policy_trace")
        has_proposal = self._has_nonempty_dict_items(action_proposals)
        has_tool_trace = self._has_nonempty_dict_items(tool_proxy_traces)
        has_policy_trace = (
            isinstance(policy_trace, dict)
            and bool(policy_trace)
            or self._has_nonempty_dict_items(policy_trace)
        )
        has_review = bool(review_id) or self._has_approved_proposal(action_proposals)
        return {
            "proposal": has_proposal,
            "tool_trace": has_tool_trace,
            "policy_trace": has_policy_trace,
            "review": has_review,
            "trace": has_proposal or has_tool_trace or has_policy_trace or has_review,
        }

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
