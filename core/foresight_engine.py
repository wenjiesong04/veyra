from core.definitions import RiskLevel, risk_policy


class ForesightEngine:
    def predict_text_action(self, text: str, risk_level: RiskLevel) -> dict[str, object]:
        lowered = text.lower()
        reversible = "full"
        side_effects: list[str] = []
        safer_alternatives: list[str] = []
        if any(marker in lowered for marker in ["重启", "restart"]):
            reversible = "partial"
            side_effects.append("service interruption")
            safer_alternatives.append("check status and logs before restart")
        if any(marker in lowered for marker in ["删除", "rm ", "delete"]):
            reversible = "low"
            side_effects.append("file or data loss")
            safer_alternatives.append("create snapshot and list targets before deletion")
        return {
            "risk_level": risk_level.value,
            "risk_policy": risk_policy(risk_level).to_dict(),
            "reversible": reversible,
            "side_effects": side_effects,
            "safer_alternatives": safer_alternatives,
        }
