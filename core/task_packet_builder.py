from __future__ import annotations

from uuid import uuid4

from core.world_state import WorldStateStore
from interface.event_schema import VeyraEvent, VeyraTaskPacket
from tool_proxy.agent_tool_contract import agent_tool_proxy_contract


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
        task_id = f"task_{uuid4().hex[:12]}"
        patched_policy = dict(policy_patch)
        patched_policy["tool_proxy_contract"] = agent_tool_proxy_contract(task_id)
        return VeyraTaskPacket(
            task_id=task_id,
            target_agent=target_agent,
            session_id=event.source.session_id,
            user_message=str(event.payload.get("text", "")),
            context_patch=context_patch,
            persona_patch=persona_patch,
            policy_patch=patched_policy,
        )
