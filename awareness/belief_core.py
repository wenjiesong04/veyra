from core.world_state import WorldStateStore
from interface.event_schema import VeyraEvent


class BeliefCore:
    def __init__(self, state_store: WorldStateStore) -> None:
        self.state_store = state_store

    def update_from_event(self, event: VeyraEvent) -> None:
        belief = self.state_store.read_json("belief_state.json")
        belief.setdefault("claims", []).append(
            {
                "claim": f"received {event.type.value} from {event.source.channel}",
                "confidence": 1.0,
                "source": "event",
                "ttl_seconds": 300,
                "status": "fresh",
            }
        )
        self.state_store.write_json("belief_state.json", belief)
