class Lifecycle:
    VALID_STATUSES = {
        "online",
        "idle",
        "monitoring",
        "thinking",
        "acting",
        "waiting_confirmation",
        "blocked",
        "recovering",
        "degraded",
        "offline",
    }

    def validate(self, status: str) -> str:
        if status not in self.VALID_STATUSES:
            raise ValueError(f"Unknown Veyra lifecycle status: {status}")
        return status
