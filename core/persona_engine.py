from interface.event_schema import RiskLevel


class PersonaEngine:
    def patch_for(self, text: str, risk_level: RiskLevel) -> dict[str, object]:
        modes = ["Minimalist"]
        lowered = text.lower()
        if any(marker in lowered for marker in ["端口", "进程", "部署", "服务", "openclaw", "hermes"]):
            modes.append("Operator")
        if any(marker in lowered for marker in ["代码", "实现", "修复", "重构", "开发"]):
            modes.append("Engineer")
        if risk_level in {RiskLevel.R3, RiskLevel.R4, RiskLevel.R5}:
            modes.append("Guardian")
        return {
            "mode": list(dict.fromkeys(modes)),
            "behavior": [
                "prefer read-only diagnosis first",
                "ask before destructive or hard-to-revert actions",
                "return evidence for execution claims",
            ],
        }
