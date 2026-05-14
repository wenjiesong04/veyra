class AgentMemoryReader:
    def read_summary(self, session_id: str) -> dict:
        return {"session_id": session_id, "summary": ""}
