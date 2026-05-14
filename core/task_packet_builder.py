from __future__ import annotations

from uuid import uuid4

from core.world_state import WorldStateStore
from interface.event_schema import VeyraEvent, VeyraTaskPacket


class TaskPacketBuilder:
    def __init__(self, state_store: WorldStateStore) -> None:
        self.state_store = state_store

    def build(
        self,
        event: VeyraEvent,
        target_agent: str,
        context_patch: dict[str, object],
        persona_patch: dict[str, object],
        policy_patch: dict[str, object],
    ) -> VeyraTaskPacket:
        return VeyraTaskPacket(
            task_id=f"task_{uuid4().hex[:12]}",
            target_agent=target_agent,
            session_id=event.source.session_id,
            user_message=str(event.payload.get("text", "")),
            context_patch=context_patch,
            persona_patch=persona_patch,
            policy_patch=policy_patch,
        )
