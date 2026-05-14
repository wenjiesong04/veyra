class ActionReview:
    def build(self, action: dict, foresight: dict, guardian_decision: dict) -> dict:
        return {"action": action, "foresight": foresight, "guardian_decision": guardian_decision}
