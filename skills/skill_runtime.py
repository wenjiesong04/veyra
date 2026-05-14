class SkillRuntime:
    def run(self, skill: dict, payload: dict) -> dict:
        return {"skill": skill.get("name"), "payload": payload, "status": "not_configured"}
