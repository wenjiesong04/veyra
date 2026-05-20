from __future__ import annotations

from interface.agent_adapter import AgentAdapter


class AgentMemoryReader:
    def __init__(self, adapter: AgentAdapter | None = None) -> None:
        self.adapter = adapter

    def read_summary(self, session_id: str) -> dict:
        if not self.adapter:
            return {"session_id": session_id, "summary": "", "status": "not_configured"}
        return self.adapter.fetch_memory_summary(session_id)
