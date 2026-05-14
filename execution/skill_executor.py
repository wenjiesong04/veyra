class SkillExecutor:
    def execute(self, name: str, payload: dict) -> dict:
        return {"status": "not_configured", "skill": name, "payload": payload}
