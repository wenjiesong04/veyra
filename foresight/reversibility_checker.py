class ReversibilityChecker:
    def check(self, action: dict) -> str:
        return "partial" if action.get("type") in {"service_restart", "file_write"} else "full"
