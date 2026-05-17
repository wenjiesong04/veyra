from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from interface.event_schema import utc_now_iso


class WorldStateStore:
    def __init__(self, root: str | Path = "state") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._ensure_defaults()

    def _ensure_defaults(self) -> None:
        defaults: dict[str, Any] = {
            "user_world.json": {"preferences": {"language": "zh-CN", "style": "direct_structured"}, "current_goal": ""},
            "local_world.json": {"current_project": str(Path.cwd()), "probes": {}, "updated_at": utc_now_iso()},
            "external_world.json": {"watchlist": [], "summaries": []},
            "executor_state.json": {"selected_agent": "openclaw", "status": "unknown"},
            "risk_state.json": {"current_risk": "R0", "signals": []},
            "belief_state.json": {"claims": []},
            "task_state.json": {"current_task": None, "history": []},
            "attention_state.json": {"focus": [], "ignored_noise": []},
            "review_queue.json": {"items": []},
            "agent_memory.json": {"items": []},
            "agent_config.json": {
                "selected_agent": "openclaw",
                "agents": {
                    "openclaw": {"kind": "openclaw", "base_url": "", "api_key_env": "OPENCLAW_GATEWAY_TOKEN", "enabled": True},
                    "hermes": {"kind": "hermes", "base_url": "", "api_key_env": "HERMES_API_KEY", "enabled": True},
                    "custom": {"kind": "custom", "base_url": "", "api_key_env": "CUSTOM_AGENT_API_KEY", "enabled": True},
                },
            },
            "rollback_state.json": {"snapshots": []},
        }
        for name, payload in defaults.items():
            path = self.root / name
            if not path.exists():
                path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        for name in ["event_log.jsonl", "action_record.jsonl", "tool_call_log.jsonl", "rollback_log.jsonl", "memory_log.jsonl"]:
            path = self.root / name
            if not path.exists():
                path.write_text("", encoding="utf-8")
        heartbeat = self.root / "heartbeat.md"
        if not heartbeat.exists():
            heartbeat.write_text(f"# Veyra Heartbeat\n\nstatus: online\nupdated_at: {utc_now_iso()}\n", encoding="utf-8")

    def read_json(self, name: str) -> dict[str, Any]:
        path = self.root / name
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8") or "{}")

    def write_json(self, name: str, payload: dict[str, Any]) -> None:
        payload.setdefault("updated_at", utc_now_iso())
        (self.root / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def patch_json(self, name: str, patch: dict[str, Any]) -> dict[str, Any]:
        current = self.read_json(name)
        current.update(patch)
        self.write_json(name, current)
        return current

    def append_jsonl(self, name: str, payload: dict[str, Any]) -> None:
        payload.setdefault("timestamp", utc_now_iso())
        with (self.root / name).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def read_jsonl(self, name: str, limit: int = 100) -> list[dict[str, Any]]:
        path = self.root / name
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                rows.append({"raw": line, "parse_error": True})
        return rows[-limit:]

    def read_text(self, name: str) -> str:
        path = self.root / name
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def write_text(self, name: str, content: str) -> None:
        (self.root / name).write_text(content, encoding="utf-8")

    def read_all(self) -> dict[str, Any]:
        return {
            "user_world": self.read_json("user_world.json"),
            "local_world": self.read_json("local_world.json"),
            "external_world": self.read_json("external_world.json"),
            "executor_state": self.read_json("executor_state.json"),
            "risk_state": self.read_json("risk_state.json"),
            "belief_state": self.read_json("belief_state.json"),
            "task_state": self.read_json("task_state.json"),
            "attention_state": self.read_json("attention_state.json"),
            "review_queue": self.read_json("review_queue.json"),
            "rollback_state": self.read_json("rollback_state.json"),
            "agent_memory": self.read_json("agent_memory.json"),
            "agent_config": self.read_json("agent_config.json"),
        }
