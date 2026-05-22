from __future__ import annotations

from typing import Any, Callable

from core.model_client import redact_sensitive
from core.reasoning_core import CoreReasoning
from core.world_state import WorldStateStore
from interface.agent_adapter import AgentAdapter
from interface.event_schema import utc_now_iso
from memory_bridge.memory_filter import MemoryFilter


class LocalMemoryBridge:
    def __init__(
        self,
        state_store: WorldStateStore,
        adapter_resolver: Callable[[], AgentAdapter] | None = None,
        reasoning: CoreReasoning | None = None,
    ) -> None:
        self.state_store = state_store
        self.filter = MemoryFilter()
        self.adapter_resolver = adapter_resolver
        self.reasoning = reasoning or CoreReasoning(state_store)

    def read_summary(self, session_id: str, focus: list[str] | None = None) -> dict[str, Any]:
        items = self.state_store.read_json("agent_memory.json").get("items", [])
        focused = self._relevant(items, focus)
        local, relevance = self._model_relevant(session_id, focus or [], focused)
        external = self._external_summary(session_id)
        return {
            "session_id": session_id,
            "summary": local,
            "external_summary": external,
            "freshness": self._freshness(local),
            "trust": "mixed" if external.get("summary") else "local",
            "relevance": relevance,
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

    def _model_relevant(self, session_id: str, focus: list[str], items: list[Any]) -> tuple[list[Any], dict[str, Any]]:
        fallback = items[-5:]
        candidates = [
            {
                "index": index,
                "item": redact_sensitive(item, max_string=900),
                "freshness": item.get("freshness") if isinstance(item, dict) else None,
                "trust": item.get("trust") if isinstance(item, dict) else None,
            }
            for index, item in enumerate(items[-20:])
        ]
        assist = self.reasoning.memory_assist(session_id=session_id, focus=focus, candidates=candidates)
        if assist.get("status") != "model_assisted":
            return fallback, {"status": assist.get("status", "rule_only"), "strategy": "focus_match_recent"}
        raw_indexes = assist.get("selected_indexes")
        if not isinstance(raw_indexes, list):
            return fallback, {"status": "fallback", "strategy": "invalid_model_indexes"}
        selected: list[Any] = []
        for raw_index in raw_indexes[:8]:
            try:
                index = int(raw_index)
            except (TypeError, ValueError):
                continue
            if 0 <= index < len(candidates):
                selected.append(items[-20:][index])
        if not selected:
            return fallback, {"status": "fallback", "strategy": "empty_model_selection"}
        return selected[-5:], {
            "status": "model_assisted",
            "strategy": "core_model_relevance",
            "selected_indexes": raw_indexes[:8],
            "notes": str(assist.get("relevance_notes") or assist.get("summary") or "")[:1000],
        }
