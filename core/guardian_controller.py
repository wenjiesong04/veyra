from __future__ import annotations

from interface.event_schema import Decision, RiskLevel


class GuardianController:
    def review_text_action(self, text: str, decision: Decision, foresight: dict[str, object]) -> dict[str, object]:
        if decision.risk_level == RiskLevel.R5:
            return {
                "decision": "block",
                "risk_level": decision.risk_level.value,
                "reason": "Blocked by Guardian: destructive or forbidden action requires a safer plan and explicit review.",
            }
        if decision.risk_level in {RiskLevel.R3, RiskLevel.R4} or decision.requires_confirmation:
            return {
                "decision": "ask_user",
                "risk_level": decision.risk_level.value,
                "reason": "Action needs confirmation because it may alter service, files, or data.",
                "foresight": foresight,
            }
        return {"decision": "allow", "risk_level": decision.risk_level.value}

    def policy_patch(self, risk_level: RiskLevel) -> dict[str, object]:
        return {
            "risk_level": risk_level.value,
            "forbidden_actions": ["rm -rf", "curl | bash", "drop database", "git push --force", "externalize_secrets"],
            "requires_review": ["service_restart", "config_modify", "file_delete", "paid_api_call"],
            "default_tool_policy": "read-only first",
        }
