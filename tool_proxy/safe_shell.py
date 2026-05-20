import subprocess

from rollback_audit.policy_trace import PolicyTrace
from rollback_audit.tool_trace import ToolTrace
from core.world_state import WorldStateStore
from tool_proxy.tool_policy import ToolPolicy


class SafeShell:
    def __init__(self, policy: ToolPolicy | None = None, state_store: WorldStateStore | None = None) -> None:
        self.policy = policy or ToolPolicy()
        self.state_store = state_store
        self.policy_trace = PolicyTrace(state_store)
        self.tool_trace = ToolTrace(state_store)

    def run(self, command: list[str], approved_by: str | None = None) -> dict:
        review = self.policy.review_command(command)
        self._record_policy("shell_command", command, review, approved_by)
        if review["decision"] == "block":
            result = {"status": review["decision"], "review": review, "command": command}
            result["tool_trace"] = self._record("shell_command", command, result, review, approved_by)
            return result
        if review["decision"] == "ask_user" and not approved_by:
            result = {"status": "needs_confirmation", "review": review, "command": command}
            result["tool_trace"] = self._record("shell_command", command, result, review, approved_by)
            return result
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        payload = {
            "status": "ok" if result.returncode == 0 else "error",
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
            "command": command,
            "approved_by": approved_by,
        }
        payload["tool_trace"] = self._record("shell_command", command, payload, review, approved_by)
        return payload

    def _record(self, action_type: str, target: object, payload: dict, review: dict, approved_by: str | None = None) -> dict:
        return self.tool_trace.record(
            tool="safe_shell",
            action_type=action_type,
            target=target,
            result=payload,
            review=review,
            approved_by=approved_by,
        )

    def _record_policy(self, action_type: str, target: object, review: dict, approved_by: str | None = None) -> None:
        self.policy_trace.record(
            {
                "tool": "safe_shell",
                "action_type": action_type,
                "target": target,
                "decision": review.get("decision"),
                "risk_level": review.get("risk_level"),
                "reason": review.get("reason"),
                "approved_by": approved_by,
                "review": review,
            }
        )
