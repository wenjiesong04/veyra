class SessionMapper:
    def map(self, channel: str, user_id: str, session_id: str) -> str:
        return f"{channel}:{user_id}:{session_id}"
