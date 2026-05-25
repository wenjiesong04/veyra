from __future__ import annotations

from typing import Any

from core.world_state import WorldStateStore
from interface.event_schema import utc_now_iso


class ChannelAdapter:
    """Local delivery adapter that persists outbound channel messages."""

    def __init__(self, state_store: WorldStateStore | None = None, channel: str = "api") -> None:
        self.state_store = state_store
        self.channel = channel

    def send(self, session_id: str, message: str, *, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        item = {
            "channel": self.channel,
            "session_id": session_id,
            "message": message,
            "metadata": metadata or {},
            "status": "queued",
            "created_at": utc_now_iso(),
            "delivery": "local_outbox",
        }
        if self.state_store:
            state = self.state_store.read_json("channel_state.json")
            outbox = state.setdefault("outbox", [])
            if not isinstance(outbox, list):
                outbox = []
                state["outbox"] = outbox
            outbox.append(item)
            state["outbox"] = outbox[-500:]
            self.state_store.write_json("channel_state.json", state)
        return item
