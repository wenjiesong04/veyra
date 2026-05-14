class ComplexityClassifier:
    def classify(self, text: str) -> str:
        return "complex" if len(text) > 120 or any(token in text for token in ["架构", "多步骤", "整体"]) else "simple"
