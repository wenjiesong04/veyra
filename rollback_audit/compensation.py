class Compensation:
    def plan(self, failed_action: dict) -> dict:
        return {"status": "planned", "steps": ["manual review"], "failed_action": failed_action}
