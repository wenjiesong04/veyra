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
        adapter_getter: Callable[[str], AgentAdapter] | None = None,
        provider_names: Callable[[], list[str]] | None = None,
        reasoning: CoreReasoning | None = None,
    ) -> None:
        self.state_store = state_store
        self.filter = MemoryFilter()
        self.adapter_resolver = adapter_resolver
        self.adapter_getter = adapter_getter
        self.provider_names = provider_names
        self.reasoning = reasoning or CoreReasoning(state_store)

    def read_summary(self, session_id: str, focus: list[str] | None = None, provider: str = "selected") -> dict[str, Any]:
        items = self.state_store.read_json("agent_memory.json").get("items", [])
        focused = self._relevant(items, focus)
        local, relevance = self._model_relevant(session_id, focus or [], focused)
        external = self._external_summary(session_id, provider)
        return {
            "session_id": session_id,
            "provider": provider,
            "summary": local,
            "external_summary": external,
            "freshness": self._freshness(local),
            "trust": "mixed" if external.get("summary") else "local",
            "relevance": relevance,
        }

    def write_patch(self, patch: dict[str, Any], provider: str = "selected") -> dict[str, Any]:
        filtered = self.filter.filter(patch)
        if filtered is None:
            result = {"status": "blocked", "reason": "memory policy rejected sensitive patch"}
        else:
            state = self.state_store.read_json("agent_memory.json") or {"items": []}
            item = {
                "patch": filtered,
                "freshness": filtered.get("freshness", "fresh"),
                "trust": filtered.get("trust", "observed"),
                "provider": provider,
                "created_at": utc_now_iso(),
            }
            state.setdefault("items", []).append(item)
            self.state_store.write_json("agent_memory.json", state)
            external = self._external_write(filtered, provider)
            result = {"status": "written", "item": item, "external_write": external}
        self.state_store.append_jsonl("memory_log.jsonl", result)
        return result

    def provider_status(self) -> dict[str, Any]:
        names = self._provider_names()
        return {
            "providers": names,
            "default": "selected",
            "supports": ["summary", "patch"],
            "diagnostics_endpoint": "/memory/providers/diagnostics",
            "validation": {name: self._provider_validation(name) for name in names},
            "notes": {
                "local": "Veyra local JSON memory only",
                "selected": "currently selected AgentAdapter memory API",
                "all": "fan-out read/write across configured external adapters plus local",
            },
        }

    def provider_diagnostics(self, provider: str = "all", session_id: str = "memory-diagnostics", write_probe: bool = False) -> dict[str, Any]:
        if provider == "all":
            results = [
                self.provider_diagnostics(name, session_id=session_id, write_probe=write_probe)
                for name in self._provider_names()
                if name != "all"
            ]
            status = "success" if all(item.get("status") == "success" for item in results) else "degraded"
            output = {"status": status, "provider": "all", "session_id": session_id, "write_probe": write_probe, "results": results}
            self.state_store.append_jsonl("memory_log.jsonl", {"status": status, "reason": "provider_diagnostics", "result": redact_sensitive(output, max_string=1800)})
            return output
        if provider == "local":
            state = self.state_store.read_json("agent_memory.json")
            items = state.get("items", []) if isinstance(state.get("items"), list) else []
            output = {
                "status": "success",
                "provider": "local",
                "mode": "local_json",
                "session_id": session_id,
                "summary": {"status": "success", "items": len(items), "freshness": self._freshness(items[-20:]), "trust": "local"},
                "write_probe": {"status": "skipped", "reason": "local provider does not need external probe"},
            }
            return output
        adapter = self._adapter_for(provider)
        if not adapter:
            return {
                "status": "not_configured",
                "provider": provider,
                "mode": "external_adapter",
                "session_id": session_id,
                "connection": {"status": "adapter_unconfigured", "connected": False},
                "summary": {"status": "not_configured", "freshness": "stale", "trust": "untrusted"},
                "write_probe": {"status": "skipped"},
            }
        try:
            connection = adapter.connection_status()
        except Exception as exc:
            connection = {"status": "error", "connected": False, "error": str(exc)}
        summary = self._external_summary(session_id, provider)
        probe_result = {"status": "skipped", "reason": "write_probe disabled"}
        if write_probe:
            probe_result = self._external_write(
                {
                    "session_id": session_id,
                    "task": "veyra_memory_provider_diagnostics",
                    "result": "probe_write",
                    "freshness": "fresh",
                    "trust": "veyra_probe",
                },
                provider,
            )
        status = self._diagnostic_status(connection, summary, probe_result, write_probe)
        output = {
            "status": status,
            "provider": provider,
            "mode": "external_adapter",
            "session_id": session_id,
            "connection": redact_sensitive(connection, max_string=1200),
            "summary": summary,
            "write_probe": probe_result,
        }
        self.state_store.append_jsonl("memory_log.jsonl", {"status": status, "reason": "provider_diagnostics", "result": redact_sensitive(output, max_string=1800)})
        return output

    def _external_summary(self, session_id: str, provider: str) -> dict[str, Any]:
        if provider == "local":
            return {"provider": "local", "status": "local_only", "summary": "", "freshness": "fresh", "trust": "local"}
        if provider == "all":
            summaries = [self._external_summary(session_id, name) for name in self._provider_names() if name not in {"local", "all"}]
            return {
                "provider": "all",
                "status": "success",
                "summary": summaries,
                "freshness": self._freshness_from_external(summaries),
                "trust": "mixed",
            }
        adapter = self._adapter_for(provider)
        if not adapter:
            return {"provider": provider, "status": "not_configured", "summary": "", "freshness": "stale", "trust": "untrusted"}
        try:
            response = adapter.fetch_memory_summary(session_id)
        except Exception as exc:
            return {"provider": provider, "status": "error", "summary": "", "error": str(exc), "freshness": "stale", "trust": "untrusted"}
        return self._normalize_external_summary(provider, response)

    def _external_write(self, patch: dict[str, Any], provider: str) -> dict[str, Any]:
        if provider == "local":
            return {"provider": "local", "status": "local_only"}
        if provider == "all":
            results = [self._external_write(patch, name) for name in self._provider_names() if name not in {"local", "all"}]
            return {"provider": "all", "status": "submitted" if any(item.get("status") == "submitted" for item in results) else "not_configured", "results": results}
        adapter = self._adapter_for(provider)
        if not adapter:
            return {"provider": provider, "status": "not_configured"}
        try:
            adapter.write_memory_patch(patch)
        except Exception as exc:
            return {"provider": provider, "status": "error", "error": str(exc)}
        return {"provider": provider, "status": "submitted"}

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

    def _adapter_for(self, provider: str) -> AgentAdapter | None:
        if provider == "selected":
            return self.adapter_resolver() if self.adapter_resolver else None
        if self.adapter_getter:
            try:
                return self.adapter_getter(provider)
            except Exception:
                return None
        return None

    def _provider_names(self) -> list[str]:
        names = ["local", "selected"]
        if self.provider_names:
            for name in self.provider_names():
                if name not in names:
                    names.append(name)
        names.append("all")
        return names

    def _provider_validation(self, provider: str) -> dict[str, Any]:
        if provider == "local":
            return {"implemented": True, "configured": True, "validated": True, "status": "validated"}
        if provider == "all":
            return {"implemented": True, "configured": True, "validated": False, "status": "fan_out_runtime_dependent"}
        adapter = self._adapter_for(provider)
        if not adapter:
            return {"implemented": True, "configured": False, "validated": False, "status": "not_configured"}
        try:
            connection = adapter.connection_status()
        except Exception as exc:
            return {"implemented": True, "configured": True, "validated": False, "status": "validation_pending", "error": str(exc)}
        validation = connection.get("validation") if isinstance(connection.get("validation"), dict) else {}
        if validation:
            return redact_sensitive(validation, max_string=600)
        connected = bool(connection.get("connected"))
        configured = str(connection.get("status")) != "adapter_unconfigured"
        return {
            "implemented": True,
            "configured": configured,
            "validated": connected,
            "status": "validated" if connected else "validation_pending" if configured else "not_configured",
        }

    def _normalize_external_summary(self, provider: str, response: dict[str, Any]) -> dict[str, Any]:
        summary = response.get("summary", "")
        freshness = str(response.get("freshness") or ("fresh" if summary else "stale"))
        if freshness not in {"fresh", "stale", "conflict"}:
            freshness = "fresh"
        trust = str(response.get("trust") or ("external" if summary else "untrusted"))
        status = str(response.get("status") or ("success" if summary else "empty"))
        return {
            "provider": provider,
            "status": status,
            "summary": summary,
            "freshness": freshness,
            "trust": trust,
            "raw": redact_sensitive(response, max_string=1200),
        }

    def _freshness_from_external(self, summaries: list[dict[str, Any]]) -> str:
        if any(item.get("freshness") == "conflict" for item in summaries):
            return "conflict"
        if any(item.get("freshness") == "stale" for item in summaries):
            return "stale"
        return "fresh"

    def _diagnostic_status(
        self,
        connection: dict[str, Any],
        summary: dict[str, Any],
        probe_result: dict[str, Any],
        write_probe: bool,
    ) -> str:
        if str(connection.get("status")) in {"adapter_unconfigured", "unavailable", "error"}:
            return "not_configured" if str(connection.get("status")) == "adapter_unconfigured" else "error"
        if summary.get("status") == "error":
            return "error"
        if summary.get("status") in {"not_configured", "local_memory_bridge"}:
            return "degraded"
        if write_probe and probe_result.get("status") not in {"submitted", "local_only"}:
            return "degraded"
        return "success"

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
