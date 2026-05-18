class ActionReview:
    def build(self, action: dict, foresight: dict, guardian_decision: dict) -> dict:
        return {
            "action": action,
            "foresight": foresight,
            "guardian_decision": guardian_decision,
            "risk_level": guardian_decision.get("risk_level") or foresight.get("risk_level"),
            "decision": guardian_decision.get("decision"),
            "required_preconditions": guardian_decision.get("required_preconditions", []),
            "forbidden": guardian_decision.get("forbidden", []),
            "reversible": foresight.get("reversible", "unknown"),
            "safer_alternatives": foresight.get("safer_alternatives", []),
        }
