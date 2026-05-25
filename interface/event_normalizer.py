from typing import Any

from interface.event_schema import EventSource, EventType, VeyraEvent


class EventNormalizer:
    """Converts external channel payloads into VeyraEvent objects."""

    def user_message(self, text: str, channel: str, user_id: str, session_id: str, metadata: dict[str, Any] | None = None) -> VeyraEvent:
        payload: dict[str, Any] = {"text": text}
        if metadata:
            payload["metadata"] = metadata
        return VeyraEvent(
            type=EventType.USER_MESSAGE,
            source=EventSource(channel=channel, user_id=user_id, session_id=session_id),
            payload=payload,
        )
