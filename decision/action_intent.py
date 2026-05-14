class ActionIntentEngine:
    def extract(self, text: str) -> dict:
        return {"text": text, "requires_action": any(token in text for token in ["执行", "检查", "修复"])}
