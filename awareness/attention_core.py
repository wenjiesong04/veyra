from __future__ import annotations

from core.world_state import WorldStateStore
from interface.event_schema import utc_now_iso


class AttentionCore:
    def __init__(self, state_store: WorldStateStore) -> None:
        self.state_store = state_store

    def focus_for_text(self, text: str) -> list[str]:
        lowered = text.lower()
        focus: list[str] = []
        mapping = {
            "openclaw": "openclaw_runtime",
            "hermes": "hermes_runtime",
            "端口": "ports",
            "port": "ports",
            "git": "git_workspace",
            "日志": "logs",
            "log": "logs",
            "部署": "deployment",
            "代码": "codebase",
            "时间": "current_time",
            "几点": "current_time",
            "日期": "current_time",
            "今天": "current_time",
            "图片": "attachment_context",
            "截图": "attachment_context",
            "图上": "attachment_context",
        }
        for marker, item in mapping.items():
            if marker in lowered and item not in focus:
                focus.append(item)
        self.state_store.write_json(
            "attention_state.json",
            {
                "focus": focus,
                "ignored_noise": [],
                "context_scope": self._context_scope(focus),
                "updated_at": utc_now_iso(),
            },
        )
        return focus

    def _context_scope(self, focus: list[str]) -> dict[str, list[str]]:
        probe_priority: list[str] = []
        if "ports" in focus:
            probe_priority.append("port_probe")
        if "git_workspace" in focus:
            probe_priority.append("git_probe")
        if "openclaw_runtime" in focus:
            probe_priority.extend(["openclaw_probe", "port_probe"])
        if "hermes_runtime" in focus:
            probe_priority.append("hermes_probe")
        if "logs" in focus:
            probe_priority.append("log_probe")
        if "current_time" in focus:
            probe_priority.append("time_probe")
        return {"probe_priority": list(dict.fromkeys(probe_priority))}
