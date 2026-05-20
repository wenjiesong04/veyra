from __future__ import annotations

from typing import Any, Callable

from core.world_state import WorldStateStore
from interface.agent_adapter import AgentAdapter
from interface.event_schema import utc_now_iso
from memory_bridge.memory_filter import MemoryFilter


class LocalMemoryBridge:
    def __init__(self, state_store: WorldStateStore, adapter_resolver: Callable[[], AgentAdapter] | None = None) -> None:
        self.state_store = state_store
        self.filter = MemoryFilter()
        self.adapter_resolver = adapter_resolver

    def read_summary(self, session_id: str, focus: list[str] | None = None) -> dict[str, Any]:
        items = self.state_store.read_json("agent_memory.json").get("items", [])
        local = self._relevant(items, focus)[-5:]
        external = self._external_summary(session_id)
        return {
            "session_id": session_id,
            "summary": local,
            "external_summary": external,
            "freshness": self._freshness(local),
            "trust": "mixed" if external.get("summary") else "local",
        }

    def write_patch(self, patch: dict[str, Any]) -> dict[str, Any]:
        filtered = self.filter.filter(patch)
        if filtered is None:
            result = {"status": "blocked", "reason": "memory policy rejected sensitive patch"}
        else:
            state = self.state_store.read_json("agent_memory.json") or {"items": []}
            item = {
                "patch": filtered,
                "freshness": filtered.get("freshness", "fresh"),
                "trust": filtered.get("trust", "observed"),
                "created_at": utc_now_iso(),
            }
            state.setdefault("items", []).append(item)
            self.state_store.write_json("agent_memory.json", state)
            external = self._external_write(filtered)
            result = {"status": "written", "item": item, "external_write": external}
        self.state_store.append_jsonl("memory_log.jsonl", result)
        return result

    def _external_summary(self, session_id: str) -> dict[str, Any]:
        adapter = self.adapter_resolver() if self.adapter_resolver else None
        if not adapter:
            return {"status": "not_configured", "summary": ""}
        try:
            return adapter.fetch_memory_summary(session_id)
        except Exception as exc:
            return {"status": "error", "summary": "", "error": str(exc)}

    def _external_write(self, patch: dict[str, Any]) -> dict[str, Any]:
        adapter = self.adapter_resolver() if self.adapter_resolver else None
        if not adapter:
            return {"status": "not_configured"}
        try:
            adapter.write_memory_patch(patch)
        except Exception as exc:
            return {"status": "error", "error": str(exc)}
        return {"status": "submitted"}

    def _relevant(self, items: list[Any], focus: list[str] | None) -> list[Any]:
        if not focus:
            return items
        relevant: list[Any] = []
        for item in items:
            haystack = str(item).lower()
            if any(str(term).lower() in haystack for term in focus):
                relevant.append(item)
        return relevant or items

    def _freshness(self, items: list[Any]) -> str:
        if any(isinstance(item, dict) and item.get("freshness") == "conflict" for item in items):
            return "conflict"
        if any(isinstance(item, dict) and item.get("freshness") == "stale" for item in items):
            return "stale"
        return "fresh"
