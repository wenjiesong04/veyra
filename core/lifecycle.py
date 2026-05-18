from core.definitions import lifecycle_statuses


class Lifecycle:
    VALID_STATUSES = set(lifecycle_statuses())

    def validate(self, status: str) -> str:
        if status not in self.VALID_STATUSES:
            raise ValueError(f"Unknown Veyra lifecycle status: {status}")
        return status
