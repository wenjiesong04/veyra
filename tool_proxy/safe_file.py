from pathlib import Path

from rollback_audit.policy_trace import PolicyTrace
from rollback_audit.tool_trace import ToolTrace
from core.world_state import WorldStateStore
from rollback_audit.rollback_manager import RollbackManager
from tool_proxy.tool_policy import ToolPolicy


class SafeFile:
    def __init__(self, state_store: WorldStateStore | None = None, policy: ToolPolicy | None = None) -> None:
        self.state_store = state_store
        self.rollback = RollbackManager(state_store) if state_store else None
        self.policy = policy or ToolPolicy()
        self.policy_trace = PolicyTrace(state_store)
        self.tool_trace = ToolTrace(state_store)

    def read_text(self, path: str) -> dict:
        target = Path(path)
        review = self.policy.review_file_read(str(target))
        self._record_policy("file_read", str(target), review)
        if review["decision"] == "block":
            result = {"status": "blocked", "review": review, "path": str(target)}
            result["tool_trace"] = self._record("file_read", str(target), result, review)
            return result
        if not target.exists():
            result = {"status": "error", "reason": "file not found", "path": str(target), "review": review}
            result["tool_trace"] = self._record("file_read", str(target), result, review)
            return result
        result = {"status": "ok", "path": str(target), "content": target.read_text(encoding="utf-8")}
        result["tool_trace"] = self._record("file_read", str(target), {"status": "ok", "path": str(target), "operation": "read_text"}, review)
        return result

    def write_text(self, path: str, content: str, reason: str = "", approved_by: str | None = None) -> dict:
        target = Path(path)
        review = self.policy.review_file_write(str(target))
        self._record_policy("file_write", str(target), review, approved_by)
        if review["decision"] == "block":
            result = {"status": "blocked", "path": str(target), "operation": "write_text", "review": review}
            result["tool_trace"] = self._record("file_write", str(target), result, review, approved_by)
            return result
        if review["decision"] == "ask_user" and not approved_by:
            result = {"status": "needs_confirmation", "path": str(target), "operation": "write_text", "review": review}
            result["tool_trace"] = self._record("file_write", str(target), result, review, approved_by)
            return result
        snapshot = self.rollback.snapshot_file(str(target), reason=reason or "safe_file.write_text") if self.rollback and target.exists() else None
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        result = {"status": "ok", "path": str(target), "operation": "write_text", "snapshot": snapshot, "approved_by": approved_by, "review": review}
        result["tool_trace"] = self._record("file_write", str(target), result, review, approved_by)
        return result

    def _record(self, action_type: str, target: str, payload: dict, review: dict, approved_by: str | None = None) -> dict:
        return self.tool_trace.record(
            tool="safe_file",
            action_type=action_type,
            target=target,
            result=payload,
            review=review,
            approved_by=approved_by,
        )

    def _record_policy(self, action_type: str, target: str, review: dict, approved_by: str | None = None) -> None:
        self.policy_trace.record(
            {
                "tool": "safe_file",
                "action_type": action_type,
                "target": target,
                "decision": review.get("decision"),
                "risk_level": review.get("risk_level"),
                "reason": review.get("reason"),
                "approved_by": approved_by,
                "review": review,
            }
        )
