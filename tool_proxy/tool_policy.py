from core.definitions import GuardianDecision, RiskLevel, classify_text_risk, risk_policy


class ToolPolicy:
    def review_command(self, command: list[str] | str) -> dict:
        text = " ".join(command) if isinstance(command, list) else command
        risk_level = classify_text_risk(text)
        policy = risk_policy(risk_level)
        if policy.default_decision == GuardianDecision.BLOCK:
            return {
                "decision": GuardianDecision.BLOCK.value,
                "risk_level": risk_level.value,
                "reason": "forbidden command pattern",
                "policy": policy.to_dict(),
                "required_preconditions": ["do not execute"],
                "forbidden": ["rm -rf", "curl | bash", "drop database", "git push --force", "externalize_secrets"],
            }
        if risk_level in {RiskLevel.R3, RiskLevel.R4} or policy.requires_confirmation:
            return {
                "decision": GuardianDecision.ASK_USER.value,
                "risk_level": risk_level.value,
                "reason": "command requires human confirmation",
                "policy": policy.to_dict(),
                "required_preconditions": ["explain impact", "confirm target", "record rollback path"],
                "forbidden": ["rm -rf", "curl | bash", "drop database", "git push --force", "externalize_secrets"],
            }
        return {
            "decision": policy.default_decision.value,
            "risk_level": risk_level.value,
            "policy": policy.to_dict(),
            "required_preconditions": ["read-only first"] if risk_level == RiskLevel.R1 else ["record evidence"],
            "forbidden": ["rm -rf", "curl | bash", "drop database", "git push --force", "externalize_secrets"],
        }
