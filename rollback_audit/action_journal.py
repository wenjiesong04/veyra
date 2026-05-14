from core.world_state import WorldStateStore


class ActionJournal:
    def __init__(self, state_store: WorldStateStore) -> None:
        self.state_store = state_store

    def record(self, payload: dict) -> None:
        self.state_store.append_jsonl("action_record.jsonl", payload)
