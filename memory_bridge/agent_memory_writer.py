from __future__ import annotations

from interface.agent_adapter import AgentAdapter
from memory_bridge.memory_filter import MemoryFilter


class AgentMemoryWriter:
    def __init__(self, adapter: AgentAdapter | None = None) -> None:
        self.adapter = adapter
        self.filter = MemoryFilter()

    def write_patch(self, patch: dict) -> dict:
        filtered = self.filter.filter(patch)
        if filtered is None:
            return {"status": "blocked", "reason": "memory policy rejected sensitive patch"}
        if not self.adapter:
            return {"status": "not_configured", "patch": filtered}
        self.adapter.write_memory_patch(filtered)
        return {"status": "submitted", "patch": filtered}
