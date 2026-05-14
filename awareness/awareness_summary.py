class AwarenessSummary:
    def build(self, state: dict) -> dict:
        return {
            "attention": state.get("attention_state", {}),
            "risk": state.get("risk_state", {}),
            "beliefs": state.get("belief_state", {}).get("claims", [])[-5:],
        }
