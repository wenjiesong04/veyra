import subprocess

from rollback_audit.policy_trace import PolicyTrace
from core.world_state import WorldStateStore
from tool_proxy.tool_policy import ToolPolicy


class SafeShell:
    def __init__(self, policy: ToolPolicy | None = None, state_store: WorldStateStore | None = None) -> None:
        self.policy = policy or ToolPolicy()
        self.state_store = state_store
        self.policy_trace = PolicyTrace(state_store)

    def run(self, command: list[str], approved_by: str | None = None) -> dict:
        review = self.policy.review_command(command)
        self._record_policy("shell_command", command, review, approved_by)
        if review["decision"] == "block":
            result = {"status": review["decision"], "review": review, "command": command}
            self._record(result)
            return result
        if review["decision"] == "ask_user" and not approved_by:
            result = {"status": "needs_confirmation", "review": review, "command": command}
            self._record(result)
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
        self._record(payload)
        return payload

    def _record(self, payload: dict) -> None:
        if self.state_store:
            self.state_store.append_jsonl("tool_call_log.jsonl", {"tool": "safe_shell", **payload})

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
