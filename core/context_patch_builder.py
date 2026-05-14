from __future__ import annotations

from core.world_state import WorldStateStore


class ContextPatchBuilder:
    def __init__(self, state_store: WorldStateStore) -> None:
        self.state_store = state_store

    def build(self, user_message: str, attention_focus: list[str]) -> dict[str, object]:
        state = self.state_store.read_all()
        return {
            "user_goal": user_message,
            "attention_focus": attention_focus,
            "local_state": state["local_world"],
            "belief_state": state["belief_state"],
            "risk_state": state["risk_state"],
        }
