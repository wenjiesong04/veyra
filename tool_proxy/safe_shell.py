import subprocess

from core.world_state import WorldStateStore
from tool_proxy.tool_policy import ToolPolicy


class SafeShell:
    def __init__(self, policy: ToolPolicy | None = None, state_store: WorldStateStore | None = None) -> None:
        self.policy = policy or ToolPolicy()
        self.state_store = state_store

    def run(self, command: list[str]) -> dict:
        review = self.policy.review_command(command)
        if review["decision"] != "allow":
            result = {"status": review["decision"], "review": review, "command": command}
            self._record(result)
            return result
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        payload = {"status": "ok" if result.returncode == 0 else "error", "stdout": result.stdout, "stderr": result.stderr, "returncode": result.returncode, "command": command}
        self._record(payload)
        return payload

    def _record(self, payload: dict) -> None:
        if self.state_store:
            self.state_store.append_jsonl("tool_call_log.jsonl", {"tool": "safe_shell", **payload})
