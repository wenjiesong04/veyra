from pathlib import Path

from core.world_state import WorldStateStore
from rollback_audit.rollback_manager import RollbackManager


class SafeFile:
    def __init__(self, state_store: WorldStateStore | None = None) -> None:
        self.state_store = state_store
        self.rollback = RollbackManager(state_store) if state_store else None

    def read_text(self, path: str) -> dict:
        target = Path(path)
        if target.name == ".env":
            result = {"status": "blocked", "reason": "sensitive file", "path": str(target)}
            self._record(result)
            return result
        result = {"status": "ok", "path": str(target), "content": target.read_text(encoding="utf-8")}
        self._record({"status": "ok", "path": str(target), "operation": "read_text"})
        return result

    def write_text(self, path: str, content: str, reason: str = "") -> dict:
        target = Path(path)
        snapshot = self.rollback.snapshot_file(str(target), reason=reason or "safe_file.write_text") if self.rollback and target.exists() else None
        target.write_text(content, encoding="utf-8")
        result = {"status": "ok", "path": str(target), "operation": "write_text", "snapshot": snapshot}
        self._record(result)
        return result

    def _record(self, payload: dict) -> None:
        if self.state_store:
            self.state_store.append_jsonl("tool_call_log.jsonl", {"tool": "safe_file", **payload})
