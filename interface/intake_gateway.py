from core.awareness_loop import AwarenessLoop
from interface.event_normalizer import EventNormalizer
from interface.event_schema import LoopResult


class IntakeGateway:
    """Auth/session/dedupe hooks can be added here without touching VeyraCore."""

    def __init__(self, awareness_loop: AwarenessLoop, normalizer: EventNormalizer | None = None) -> None:
        self.awareness_loop = awareness_loop
        self.normalizer = normalizer or EventNormalizer()

    def receive_text(self, text: str, channel: str = "cli", user_id: str = "local-user", session_id: str = "local") -> LoopResult:
        event = self.normalizer.user_message(text=text, channel=channel, user_id=user_id, session_id=session_id)
        return self.awareness_loop.handle_event(event)
