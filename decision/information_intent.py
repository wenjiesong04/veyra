class InformationIntentEngine:
    def needs_external_refresh(self, topic: str, state: dict) -> bool:
        return topic in state.get("watchlist", [])
