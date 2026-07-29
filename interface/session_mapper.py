from __future__ import annotations

from memory_bridge.scope import framed_sha256, normalize_scope_component


class SessionMapper:
    def map(self, channel: str, user_id: str, session_id: str) -> str:
        channel_scope = normalize_scope_component(
            channel or "api",
            "channel",
        )
        user_scope = normalize_scope_component(
            user_id or "local-user",
            "user_id",
        )
        session_scope = normalize_scope_component(
            session_id or "local-session",
            "session_id",
        )
        digest = framed_sha256(
            "veyra-dialogue-session-v2",
            channel_scope,
            user_scope,
            session_scope,
        )
        return f"veyra-session-v2-{digest[:32]}"
