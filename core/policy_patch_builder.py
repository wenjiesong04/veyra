from core.guardian_controller import GuardianController
from interface.event_schema import RiskLevel


class PolicyPatchBuilder:
    def build(self, risk_level: RiskLevel):
        return GuardianController().policy_patch(risk_level)
