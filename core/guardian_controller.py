from __future__ import annotations

from core.definitions import GuardianDecision, RiskLevel, risk_catalog, risk_policy
from interface.event_schema import Decision
from tool_proxy.agent_tool_contract import agent_tool_proxy_contract


class GuardianController:
    def review_text_action(self, text: str, decision: Decision, foresight: dict[str, object]) -> dict[str, object]:
        policy = risk_policy(decision.risk_level)
        base = self._review_base(decision, foresight)
        if policy.default_decision == GuardianDecision.BLOCK:
            return {
                **base,
                "decision": GuardianDecision.BLOCK.value,
                "risk_level": decision.risk_level.value,
                "reason": "Blocked by Guardian: destructive or forbidden action requires a safer plan and explicit review.",
                "required_preconditions": [],
                "forbidden": self._forbidden_actions(),
                "message_to_executor": "Stop. Produce a safer plan and do not execute the proposed action.",
            }
        if policy.requires_confirmation or decision.requires_confirmation:
            return {
                **base,
                "decision": GuardianDecision.ASK_USER.value,
                "risk_level": decision.risk_level.value,
                "reason": "Action needs confirmation because it may alter service, files, or data.",
                "required_preconditions": self._preconditions_for(decision.risk_level),
                "forbidden": self._forbidden_actions(),
                "message_to_executor": "Pause and request user confirmation with impact and rollback notes.",
            }
        return {
            **base,
            "decision": policy.default_decision.value,
            "risk_level": decision.risk_level.value,
            "required_preconditions": self._preconditions_for(decision.risk_level),
            "forbidden": self._forbidden_actions(),
            "message_to_executor": "Proceed within the policy constraints and return evidence.",
        }

    def policy_patch(self, risk_level: RiskLevel) -> dict[str, object]:
        policy = risk_policy(risk_level)
        return {
            "risk_level": risk_level.value,
            "risk_policy": policy.to_dict(),
            "risk_catalog": risk_catalog(),
            "forbidden_actions": ["rm -rf", "curl | bash", "drop database", "drop table", "truncate table", "git push --force", "git reset --hard", "externalize_secrets"],
            "requires_review": ["service_restart", "config_modify", "file_delete", "paid_api_call"],
            "requires_snapshot": policy.requires_snapshot,
            "whether_snapshot_required": policy.requires_snapshot,
            "requires_confirmation": policy.requires_confirmation,
            "default_tool_policy": "read-only first",
            "allowed_actions": self._allowed_actions(risk_level),
            "allowed_tools": self._allowed_tools(risk_level),
            "required_preconditions": self._preconditions_for(risk_level),
            "file_scope": self._file_scope(risk_level),
            "network_scope": self._network_scope(risk_level),
            "executor_constraints": [
                "return evidence for execution claims",
                "do not escalate risk without ActionProposal",
                "refresh stale state before relying on it",
                "route R3-R4 tool actions through /actions/proposals before execution",
                "return tool_proxy_traces or action_proposals for R2+ tool calls",
            ],
            "tool_proxy_contract": agent_tool_proxy_contract(),
        }

    def _review_base(self, decision: Decision, foresight: dict[str, object]) -> dict[str, object]:
        policy = risk_policy(decision.risk_level)
        return {
            "policy": policy.to_dict(),
            "foresight": foresight,
            "decision_trace": decision.to_dict(),
            "constraints": list(decision.constraints),
        }

    def _allowed_actions(self, risk_level: RiskLevel) -> list[str]:
        if risk_level == RiskLevel.R0:
            return ["answer"]
        if risk_level == RiskLevel.R1:
            return ["read_state", "run_probe", "read_logs"]
        if risk_level == RiskLevel.R2:
            return ["scoped_write", "snapshot_then_write", "local_commit"]
        if risk_level in {RiskLevel.R3, RiskLevel.R4}:
            return ["prepare_plan", "create_snapshot", "request_confirmation"]
        return ["explain_block", "suggest_safe_alternative"]

    def _allowed_tools(self, risk_level: RiskLevel) -> list[str]:
        if risk_level == RiskLevel.R0:
            return []
        if risk_level == RiskLevel.R1:
            return ["read_only_probe", "safe_file_read", "safe_browser_read", "safe_api_get"]
        if risk_level == RiskLevel.R2:
            return ["read_only_probe", "safe_file_read", "safe_file_write_with_snapshot", "safe_shell_low_risk"]
        if risk_level in {RiskLevel.R3, RiskLevel.R4}:
            return ["read_only_probe", "action_proposal", "snapshot", "diff"]
        return ["action_proposal_for_safe_alternative_only"]

    def _file_scope(self, risk_level: RiskLevel) -> dict[str, object]:
        return {
            "default": "read_only" if risk_level in {RiskLevel.R0, RiskLevel.R1} else "scoped_write_with_snapshot",
            "forbidden": [".env", ".ssh", ".gnupg", "secrets", "private_keys"],
            "requires_snapshot": risk_level in {RiskLevel.R2, RiskLevel.R3, RiskLevel.R4, RiskLevel.R5},
        }

    def _network_scope(self, risk_level: RiskLevel) -> dict[str, object]:
        if risk_level == RiskLevel.R0:
            return {"default": "none", "external": False}
        if risk_level == RiskLevel.R1:
            return {"default": "read_only", "external": True, "state_changing_methods": False}
        return {"default": "review_required", "external": True, "state_changing_methods": "human_review"}

    def _preconditions_for(self, risk_level: RiskLevel) -> list[str]:
        if risk_level == RiskLevel.R0:
            return []
        if risk_level == RiskLevel.R1:
            return ["use read-only probe"]
        if risk_level == RiskLevel.R2:
            return ["limit write scope", "record diff or snapshot"]
        if risk_level == RiskLevel.R3:
            return ["create snapshot", "show diff scope", "ask user"]
        if risk_level == RiskLevel.R4:
            return ["create snapshot if possible", "explain downtime or data impact", "ask user"]
        return ["do not execute"]

    def _forbidden_actions(self) -> list[str]:
        return ["rm -rf", "curl | bash", "drop database", "drop table", "truncate table", "git push --force", "git reset --hard", "externalize_secrets"]
