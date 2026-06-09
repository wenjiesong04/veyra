from __future__ import annotations

from typing import Any

from core.world_state import WorldStateStore
from interface.event_schema import utc_now_iso


class AttentionCore:
    def __init__(self, state_store: WorldStateStore) -> None:
        self.state_store = state_store

    def focus_for_text(self, text: str) -> list[str]:
        lowered = text.lower()
        focus: list[str] = []
        mapping = {
            "veyra": "veyra_project",
            "架构": "architecture",
            "architecture": "architecture",
            "openclaw": "openclaw_runtime",
            "hermes": "hermes_runtime",
            "docker": "docker_runtime",
            "容器": "docker_runtime",
            "端口": "ports",
            "port": "ports",
            "git": "git_workspace",
            "日志": "logs",
            "log": "logs",
            "部署": "deployment",
            "项目": "project_context",
            "作业": "school_work",
            "学校": "school_work",
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
        previous = self._previous_focus()
        inherited = False
        if previous and self._is_continuation(text, lowered):
            if not focus:
                focus = previous
            elif set(focus).issubset({"project_context"}):
                focus = list(dict.fromkeys(previous + focus))
            inherited = True
        self.state_store.write_json(
            "attention_state.json",
            {
                "focus": focus,
                "ignored_noise": [],
                "context_scope": self._context_scope(focus),
                "previous_focus": previous,
                "inherited_from_previous": inherited,
                "source": "attention_core",
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
        if "docker_runtime" in focus:
            probe_priority.extend(["process_probe", "log_probe"])
        if "deployment" in focus:
            probe_priority.extend(["process_probe", "log_probe", "port_probe"])
        if "logs" in focus:
            probe_priority.append("log_probe")
        if "current_time" in focus:
            probe_priority.append("time_probe")
        return {"probe_priority": list(dict.fromkeys(probe_priority))}

    def _previous_focus(self) -> list[str]:
        state = self.state_store.read_json("attention_state.json")
        focus = state.get("focus") if isinstance(state.get("focus"), list) else []
        return [str(item) for item in focus if item]

    def _is_continuation(self, text: str, lowered: str) -> bool:
        compact = "".join(str(text or "").split()).strip("，,。！？!?")
        if compact in {"继续", "接着", "继续说", "继续处理", "继续刚才", "继续上次", "继续昨天那个项目"}:
            return True
        return any(marker in lowered for marker in ("continue", "resume previous", "same topic"))

    def active_scope(self) -> dict[str, Any]:
        state = self.state_store.read_json("attention_state.json")
        focus = state.get("focus") if isinstance(state.get("focus"), list) else []
        return {
            "status": "success",
            "source": state.get("source") or "attention_core",
            "updated_at": state.get("updated_at"),
            "confidence": state.get("confidence", 0.76),
            "ttl_seconds": state.get("ttl_seconds", 300),
            "focus": focus,
            "context_scope": state.get("context_scope") if isinstance(state.get("context_scope"), dict) else self._context_scope([str(item) for item in focus]),
            "ignored_noise": state.get("ignored_noise") if isinstance(state.get("ignored_noise"), list) else [],
        }
