from core.definitions import RiskLevel
from core.persona_engine import PersonaEngine


class PersonaPatchBuilder:
    def build(self, text: str, risk_level: RiskLevel):
        return PersonaEngine().patch_for(text, risk_level)
