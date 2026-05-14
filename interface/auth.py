class AuthPolicy:
    def allow(self, channel: str, user_id: str) -> bool:
        return bool(channel and user_id)
