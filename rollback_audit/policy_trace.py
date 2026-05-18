from __future__ import annotations

from typing import Any

from core.world_state import WorldStateStore


class PolicyTrace:
    def __init__(self, state_store: WorldStateStore | None = None) -> None:
        self.state_store = state_store

    def record(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.state_store:
            self.state_store.append_jsonl("policy_trace.jsonl", payload)
        return payload
