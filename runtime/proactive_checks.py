from __future__ import annotations

from core.perception_layer import PerceptionLayer
from core.world_state import WorldStateStore
from probes.git_probe import GitProbe
from probes.openclaw_probe import OpenClawProbe
from probes.system_probe import SystemProbe


class ProactiveChecks:
    """A2/A3 MVP: run low-risk read-only checks and surface state gaps."""

    def __init__(self, state_store: WorldStateStore) -> None:
        self.state_store = state_store
        self.perception = PerceptionLayer(state_store)

    def run_read_only(self) -> dict:
        results = {
            "system": SystemProbe().run(""),
            "git": GitProbe().run(""),
            "openclaw": OpenClawProbe().run("18789"),
        }
        for result in results.values():
            self.perception.interpret_probe_result(result)
        gaps = []
        openclaw = results["openclaw"]
        if openclaw.get("status") != "listening":
            gaps.append({"target": "openclaw_runtime", "status": "unavailable", "suggestion": "Check OpenClaw runtime or configure its port."})
        git = results["git"]
        if git.get("dirty"):
            gaps.append({"target": "git_workspace", "status": "dirty", "suggestion": "Review changes before risky operations."})
        output = {"status": "success", "autonomy_level": "A3", "results": results, "state_gaps": gaps}
        self.state_store.append_jsonl("action_record.jsonl", {"route": "proactive_check", "status": "success", "artifacts": output})
        return output
