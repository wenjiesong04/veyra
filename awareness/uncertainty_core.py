class UncertaintyCore:
    def mark_stale(self, claim: dict) -> dict:
        updated = dict(claim)
        updated["status"] = "stale"
        updated["next_action"] = "refresh_probe"
        return updated
