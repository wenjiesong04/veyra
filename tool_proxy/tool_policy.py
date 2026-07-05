from pathlib import Path
from urllib.parse import urlparse

from core.action_risk import ActionRiskAssessment, assess_command_risk
from core.definitions import GuardianDecision, RiskLevel, risk_policy


class ToolPolicy:
    def review_command(self, command: list[str] | str) -> dict:
        assessment = assess_command_risk(command)
        return self._decision(RiskLevel(assessment.risk_level), reason=assessment.reason, assessment=assessment)

    def review_file_read(self, path: str) -> dict:
        target = Path(path)
        if self._is_sensitive_path(target):
            return self._decision(RiskLevel.R5, reason="sensitive file read is blocked")
        return self._decision(RiskLevel.R1, reason="file read is read-only")

    def review_file_write(self, path: str) -> dict:
        target = Path(path)
        if self._is_sensitive_path(target):
            return self._decision(RiskLevel.R4, reason="sensitive file write requires confirmation")
        return self._decision(RiskLevel.R2, reason="file write requires scoped diff and snapshot")

    def review_browser_open(self, url: str) -> dict:
        parsed = urlparse(url)
        if parsed.scheme in {"javascript", "data"}:
            return self._decision(RiskLevel.R5, reason="unsafe browser URL scheme is blocked")
        if parsed.scheme == "file":
            return self._decision(RiskLevel.R4, reason="local file browser access requires confirmation")
        if parsed.hostname in {"127.0.0.1", "localhost", "::1"}:
            return self._decision(RiskLevel.R1, reason="local browser target is read-only")
        return self._decision(RiskLevel.R2, reason="external browser target is allowed with constraints")

    def review_api_request(self, payload: dict) -> dict:
        method = str(payload.get("method", "GET")).upper()
        url = str(payload.get("url") or payload.get("endpoint") or "")
        body = payload.get("body") or payload.get("json") or {}
        text = f"{method} {url} {body}".lower()
        if any(token in text for token in ["api_key", "authorization", "bearer ", "secret", ".env"]):
            return self._decision(RiskLevel.R5, reason="API request appears to contain sensitive material")
        if method in {"DELETE", "PATCH", "PUT", "POST"}:
            return self._decision(RiskLevel.R4, reason="state-changing API request requires confirmation")
        return self._decision(RiskLevel.R1, reason="read-only API request")

    def _decision(self, risk_level: RiskLevel, reason: str, assessment: ActionRiskAssessment | None = None) -> dict:
        policy = risk_policy(risk_level)
        risk_assessment = assessment.to_dict() if assessment else None
        if policy.default_decision == GuardianDecision.BLOCK:
            return {
                "decision": GuardianDecision.BLOCK.value,
                "risk_level": risk_level.value,
                "reason": reason,
                "policy": policy.to_dict(),
                **({"risk_assessment": risk_assessment} if risk_assessment else {}),
                "required_preconditions": ["do not execute"],
                "forbidden": ["rm -rf", "curl | bash", "drop database", "drop table", "truncate table", "git push --force", "externalize_secrets"],
            }
        if risk_level in {RiskLevel.R3, RiskLevel.R4} or policy.requires_confirmation:
            return {
                "decision": GuardianDecision.ASK_USER.value,
                "risk_level": risk_level.value,
                "reason": reason,
                "policy": policy.to_dict(),
                **({"risk_assessment": risk_assessment} if risk_assessment else {}),
                "required_preconditions": ["explain impact", "confirm target", "record rollback path"],
                "forbidden": ["rm -rf", "curl | bash", "drop database", "drop table", "truncate table", "git push --force", "externalize_secrets"],
            }
        return {
            "decision": policy.default_decision.value,
            "risk_level": risk_level.value,
            "reason": reason,
            "policy": policy.to_dict(),
            **({"risk_assessment": risk_assessment} if risk_assessment else {}),
            "required_preconditions": ["read-only first"] if risk_level == RiskLevel.R1 else ["record evidence"],
            "forbidden": ["rm -rf", "curl | bash", "drop database", "drop table", "truncate table", "git push --force", "externalize_secrets"],
        }

    def _is_sensitive_path(self, target: Path) -> bool:
        sensitive_names = {".env", ".env.local", ".env.production", "id_rsa", "id_ed25519"}
        return target.name in sensitive_names or any(part in {".ssh", ".gnupg"} for part in target.parts)
