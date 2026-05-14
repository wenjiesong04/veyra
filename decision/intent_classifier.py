class IntentClassifier:
    def classify(self, text: str) -> str:
        return "action" if any(token in text for token in ["检查", "修复", "执行", "修改"]) else "information"
