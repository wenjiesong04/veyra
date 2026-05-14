from core.persona_engine import PersonaEngine
from interface.event_schema import RiskLevel


class PersonaPatchBuilder:
    def build(self, text: str, risk_level: RiskLevel):
        return PersonaEngine().patch_for(text, risk_level)
