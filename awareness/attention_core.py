from __future__ import annotations

from core.world_state import WorldStateStore


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
        }
        for marker, item in mapping.items():
            if marker in lowered and item not in focus:
                focus.append(item)
        self.state_store.write_json("attention_state.json", {"focus": focus, "ignored_noise": []})
        return focus
