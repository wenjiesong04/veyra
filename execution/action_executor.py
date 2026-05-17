from __future__ import annotations

import shlex
from typing import Any

from core.world_state import WorldStateStore
from tool_proxy.safe_file import SafeFile
from tool_proxy.safe_shell import SafeShell


class ActionExecutor:
    def __init__(self, state_store: WorldStateStore) -> None:
        self.state_store = state_store
        self.safe_shell = SafeShell(state_store=state_store)
        self.safe_file = SafeFile(state_store=state_store)

    def execute_review(self, review: dict[str, Any]) -> dict[str, Any]:
        proposal = review.get("proposal")
        if not proposal:
            return {
                "status": "approved_noop",
                "reason": "Review item has no executable proposal. Approval is recorded for governance only.",
            }
        action = proposal.get("action", {})
        action_type = action.get("type")
        approval_id = str(review.get("review_id", ""))
        if action_type == "shell_command":
            command = action.get("command", [])
            if isinstance(command, str):
                command = shlex.split(command)
            if not isinstance(command, list) or not command:
                return {"status": "error", "reason": "shell_command action requires a non-empty command"}
            return self.safe_shell.run([str(part) for part in command], approved_by=approval_id)
        if action_type == "file_write":
            path = str(action.get("path", ""))
            content = str(action.get("content", ""))
            if not path:
                return {"status": "error", "reason": "file_write action requires path"}
            return self.safe_file.write_text(path, content, reason=f"approved review {approval_id}")
        if action_type == "file_read":
            path = str(action.get("path", ""))
            if not path:
                return {"status": "error", "reason": "file_read action requires path"}
            return self.safe_file.read_text(path)
        return {"status": "not_supported", "reason": f"Unsupported action type: {action_type}", "proposal": proposal}
