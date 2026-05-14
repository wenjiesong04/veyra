from __future__ import annotations

from typing import Any

from core.world_state import WorldStateStore
from interface.event_schema import utc_now_iso
from memory_bridge.memory_filter import MemoryFilter


class LocalMemoryBridge:
    def __init__(self, state_store: WorldStateStore) -> None:
        self.state_store = state_store
        self.filter = MemoryFilter()

    def read_summary(self, session_id: str) -> dict[str, Any]:
        items = self.state_store.read_json("agent_memory.json").get("items", [])
        return {"session_id": session_id, "summary": items[-5:]}

    def write_patch(self, patch: dict[str, Any]) -> dict[str, Any]:
        filtered = self.filter.filter(patch)
        if filtered is None:
            result = {"status": "blocked", "reason": "memory policy rejected sensitive patch"}
        else:
            state = self.state_store.read_json("agent_memory.json") or {"items": []}
            item = {"patch": filtered, "created_at": utc_now_iso()}
            state.setdefault("items", []).append(item)
            self.state_store.write_json("agent_memory.json", state)
            result = {"status": "written", "item": item}
        self.state_store.append_jsonl("memory_log.jsonl", result)
        return result
