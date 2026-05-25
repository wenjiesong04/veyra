from __future__ import annotations


class SessionMapper:
    def map(self, channel: str, user_id: str, session_id: str) -> str:
        return ":".join(
            [
                self._clean(channel or "api"),
                self._clean(user_id or "local-user"),
                self._clean(session_id or "local-session"),
            ]
        )

    def _clean(self, value: str) -> str:
        return str(value).strip().replace(":", "_") or "unknown"
