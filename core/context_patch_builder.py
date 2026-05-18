from __future__ import annotations

from awareness.belief_core import BeliefCore
from awareness.uncertainty_core import UncertaintyCore
from core.world_state import WorldStateStore


class ContextPatchBuilder:
    def __init__(self, state_store: WorldStateStore) -> None:
        self.state_store = state_store
        self.belief = BeliefCore(state_store)
        self.uncertainty = UncertaintyCore()

    def build(self, user_message: str, attention_focus: list[str]) -> dict[str, object]:
        state = self.state_store.read_all()
        relevant_claims = self.belief.relevant_claims(attention_focus)
        return {
            "user_goal": user_message,
            "attention_focus": attention_focus,
            "local_state": state["local_world"],
            "belief_state": {
                "summary": state["belief_state"].get("summary", {}),
                "relevant_claims": relevant_claims,
            },
            "uncertainty": self.uncertainty.uncertainty_summary(relevant_claims),
            "risk_state": state["risk_state"],
        }
