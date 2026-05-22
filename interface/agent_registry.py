from __future__ import annotations

import os
from typing import Any

from core.world_state import WorldStateStore
from interface.agent_adapter import AgentAdapter
from interface.custom_agent_adapter import CustomAgentAdapter
from interface.hermes_adapter import HermesAdapter
from interface.openclaw_adapter import OpenClawAdapter


class AgentRegistry:
    """Keeps selected runtime and adapter configuration outside VeyraCore logic."""

    def __init__(self, state_store: WorldStateStore) -> None:
        self.state_store = state_store
        self._adapters: dict[str, AgentAdapter] = {}
        self._ensure_config()
        self.refresh()

    def _ensure_config(self) -> None:
        current = self.state_store.read_json("agent_config.json")
        if current:
            return
        selected = os.getenv("VEYRA_SELECTED_AGENT") or os.getenv("VEYRA_AGENT") or "openclaw"
        self.state_store.write_json(
            "agent_config.json",
            {
                "selected_agent": selected,
                "agents": {
                    "openclaw": {
                        "kind": "openclaw",
                        "base_url": os.getenv("OPENCLAW_BASE_URL", ""),
                        "api_key_env": "OPENCLAW_GATEWAY_TOKEN",
                        "enabled": True,
                    },
                    "hermes": {
                        "kind": "hermes",
                        "base_url": os.getenv("HERMES_BASE_URL", ""),
                        "api_key_env": "HERMES_API_KEY",
                        "enabled": True,
                    },
                    "custom": {
                        "kind": "custom",
                        "base_url": os.getenv("CUSTOM_AGENT_BASE_URL", ""),
                        "api_key_env": "CUSTOM_AGENT_API_KEY",
                        "enabled": True,
                    },
                },
            },
        )

    def refresh(self) -> None:
        config = self.config()
        agents = config.get("agents", {})
        self._adapters = {}
        for name, raw_agent in agents.items():
            if not isinstance(raw_agent, dict):
                continue
            adapter = self._build_adapter(str(name), raw_agent)
            if adapter:
                self._adapters[str(name)] = adapter

    def config(self) -> dict[str, Any]:
        return self.state_store.read_json("agent_config.json")

    def selected_name(self) -> str:
        selected = str(self.config().get("selected_agent") or "openclaw")
        return selected if selected in self._adapters else "openclaw"

    def selected(self) -> AgentAdapter:
        return self.get(self.selected_name())

    def names(self) -> list[str]:
        if not self._adapters:
            self.refresh()
        return sorted(self._adapters)

    def get(self, name: str) -> AgentAdapter:
        if name not in self._adapters:
            self.refresh()
        return self._adapters.get(name) or OpenClawAdapter()

    def list_status(self) -> dict[str, Any]:
        selected = self.selected_name()
        return {
            "selected_agent": selected,
            "agents": {name: adapter.connection_status() for name, adapter in self._adapters.items()},
        }

    def select(self, name: str) -> dict[str, Any]:
        if name not in self._adapters:
            raise KeyError(f"Unknown agent runtime: {name}")
        config = self.config()
        config["selected_agent"] = name
        self.state_store.write_json("agent_config.json", config)
        self.state_store.patch_json("executor_state.json", {"selected_agent": name, **self._adapters[name].connection_status()})
        return self.list_status()

    def upsert(self, name: str, patch: dict[str, Any]) -> dict[str, Any]:
        if name not in {"openclaw", "hermes", "custom"} and not patch.get("kind"):
            patch["kind"] = "custom"
        config = self.config()
        agents = config.setdefault("agents", {})
        current = agents.get(name, {}) if isinstance(agents.get(name), dict) else {}
        current.update({key: value for key, value in patch.items() if value is not None})
        agents[name] = current
        self.state_store.write_json("agent_config.json", config)
        self.refresh()
        return self.list_status()

    def _build_adapter(self, name: str, config: dict[str, Any]) -> AgentAdapter | None:
        if config.get("enabled") is False:
            return None
        kind = str(config.get("kind") or name).lower()
        base_url = str(config.get("base_url") or "")
        api_key = str(config.get("api_key") or os.getenv(str(config.get("api_key_env") or ""), ""))
        timeout = float(config.get("timeout") or 20)
        paths = {
            "task_path": config.get("task_path") or "/tasks",
            "capabilities_path": config.get("capabilities_path") or "/capabilities",
            "memory_summary_path": config.get("memory_summary_path") or "/memory/summary",
            "memory_patch_path": config.get("memory_patch_path") or "/memory/patch",
            "status_path_template": config.get("status_path_template") or "/tasks/{task_id}",
            "stop_path_template": config.get("stop_path_template") or "/tasks/{task_id}/stop",
        }
        if kind == "openclaw":
            return OpenClawAdapter(
                base_url=base_url,
                api_key=api_key,
                timeout=timeout,
                protocol_min=self._optional_int(config.get("protocol_min")),
                protocol_max=self._optional_int(config.get("protocol_max")),
                **paths,
            )
        if kind == "hermes":
            return HermesAdapter(base_url=base_url, api_key=api_key, timeout=timeout, **paths)
        if kind == "custom":
            return CustomAgentAdapter(base_url=base_url, api_key=api_key, timeout=timeout, **paths)
        return CustomAgentAdapter(base_url=base_url, api_key=api_key, timeout=timeout, **paths)

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
