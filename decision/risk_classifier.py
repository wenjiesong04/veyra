from core.decision_core import DecisionCore


class RiskClassifier:
    def classify(self, text: str):
        return DecisionCore()._risk_for_text(text.lower())
