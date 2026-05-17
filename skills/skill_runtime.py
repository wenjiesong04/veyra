from __future__ import annotations

from core.world_state import WorldStateStore
from probes.git_probe import GitProbe
from probes.openclaw_probe import OpenClawProbe
from probes.port_probe import PortProbe
from probes.system_probe import SystemProbe


class SkillRuntime:
    def __init__(self, state_store: WorldStateStore | None = None) -> None:
        self.state_store = state_store

    def run(self, skill: dict, payload: dict) -> dict:
        name = skill.get("name")
        if skill.get("status") != "available":
            return {"skill": name, "status": "missing", "payload": payload}
        text = str(payload.get("text", ""))
        if name == "check_port":
            result = PortProbe().run(text)
        elif name == "diagnose_openclaw":
            result = {
                "status": "success",
                "checks": {
                    "openclaw": OpenClawProbe().run(text),
                    "git": GitProbe().run(text),
                    "system": SystemProbe().run(text),
                },
                "summary": "OpenClaw read-only diagnosis completed.",
            }
        elif name == "summarize_logs":
            result = {"status": "success", "summary": "Log summarization skill is ready; provide a log path through log_probe for real summaries."}
        elif name == "safe_git_commit":
            result = {"status": "needs_confirmation", "summary": "safe_git_commit requires review before writing a commit."}
        else:
            result = {"status": "not_supported", "skill": name}
        if self.state_store:
            self.state_store.append_jsonl("action_record.jsonl", {"route": "skill", "status": result.get("status"), "artifacts": {"skill": name, "result": result}})
        return {"skill": name, "payload": payload, **result}
