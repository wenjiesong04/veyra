from core.definitions import classify_text_risk


class RiskClassifier:
    def classify(self, text: str):
        return classify_text_risk(text)
