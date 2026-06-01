from __future__ import annotations

import hashlib
from datetime import datetime, timezone
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
            filtered = self._normalize_patch(filtered)
            state = self.state_store.read_json("agent_memory.json") or {"items": []}
            items = self._prune_items(state.get("items", []) if isinstance(state.get("items"), list) else [])
            memory_id = self._memory_id(filtered, provider)
            now = utc_now_iso()
            item = {
                "memory_id": memory_id,
                "patch": filtered,
                "session_id": filtered.get("session_id"),
                "memory_type": filtered.get("memory_type"),
                "topic": filtered.get("topic"),
                "summary": filtered.get("summary"),
                "freshness": filtered.get("freshness", "fresh"),
                "trust": filtered.get("trust", "observed"),
                "confidence": filtered.get("confidence"),
                "quality": filtered.get("quality"),
                "provider": provider,
                "created_at": now,
                "updated_at": now,
                "merged_count": 1,
            }
            replaced = False
            for index, existing in enumerate(items):
                if isinstance(existing, dict) and existing.get("memory_id") == memory_id:
                    item["created_at"] = existing.get("created_at") or item["created_at"]
                    item["merged_count"] = int(existing.get("merged_count") or 1) + 1
                    items[index] = item
                    replaced = True
                    break
            if not replaced:
                items.append(item)
            state["items"] = self._rank_items(items)[-200:]
            self.state_store.write_json("agent_memory.json", state)
            external = self._external_write(filtered, provider)
            result = {"status": "written", "deduped": replaced, "item": item, "external_write": external}
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
            response = adapter.write_memory_patch(patch)
        except Exception as exc:
            return {"provider": provider, "status": "error", "error": str(exc)}
        if isinstance(response, dict):
            return {"provider": provider, **response}
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
        now = datetime.now(timezone.utc)
        for item in items:
            if not isinstance(item, dict):
                continue
            expires_at = str(item.get("expires_at") or "")
            if not expires_at:
                continue
            try:
                parsed = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            except ValueError:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            if parsed < now:
                return "stale"
        if any(isinstance(item, dict) and item.get("freshness") == "conflict" for item in items):
            return "conflict"
        if any(isinstance(item, dict) and item.get("freshness") == "stale" for item in items):
            return "stale"
        return "fresh"

    def _normalize_patch(self, patch: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(patch)
        memory_type = str(normalized.get("memory_type") or "")
        if not memory_type:
            memory_type = "user_preference" if normalized.get("preference") else "task_result"
        normalized["memory_type"] = memory_type
        normalized.setdefault("topic", self._topic_for_patch(normalized))
        normalized.setdefault("summary", self._summary_for_patch(normalized))
        normalized.setdefault("confidence", self._confidence_for_patch(normalized))
        normalized.setdefault("quality", self._quality_for_patch(normalized))
        normalized.setdefault("freshness", "fresh")
        normalized.setdefault("trust", "observed")
        return normalized

    def _topic_for_patch(self, patch: dict[str, Any]) -> str:
        for key in ("topic", "task", "route"):
            value = str(patch.get(key) or "").strip()
            if value:
                return value[:120]
        return str(patch.get("memory_type") or "general")[:120]

    def _summary_for_patch(self, patch: dict[str, Any]) -> str:
        if isinstance(patch.get("preference"), dict):
            source = str(patch["preference"].get("source_text") or "")
            if source:
                return source[:240]
        task = str(patch.get("task") or "").strip()
        result = str(patch.get("result") or "").strip()
        if task and result:
            return f"{task}: {result}"[:300]
        return (task or result or str(patch.get("memory_type") or "memory"))[:300]

    def _confidence_for_patch(self, patch: dict[str, Any]) -> float:
        try:
            return max(0.0, min(float(patch.get("confidence")), 1.0))
        except (TypeError, ValueError):
            pass
        trust = str(patch.get("trust") or "")
        if trust in {"verified", "veyra_probe"}:
            return 0.88
        if trust in {"observed", "external"}:
            return 0.68
        return 0.55

    def _quality_for_patch(self, patch: dict[str, Any]) -> dict[str, Any]:
        confidence = self._confidence_for_patch(patch)
        summary = str(patch.get("summary") or self._summary_for_patch(patch))
        score = confidence
        if len(summary) >= 20:
            score += 0.08
        if patch.get("session_id"):
            score += 0.04
        if patch.get("memory_type") == "user_preference":
            score += 0.08
        return {"score": round(min(score, 1.0), 3), "signals": ["confidence", "specificity"]}

    def _memory_id(self, patch: dict[str, Any], provider: str) -> str:
        key = "|".join(
            [
                provider,
                str(patch.get("session_id") or ""),
                str(patch.get("memory_type") or ""),
                str(patch.get("topic") or ""),
                str(patch.get("summary") or "")[:180],
            ]
        ).lower()
        return "mem_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]

    def _prune_items(self, items: list[Any]) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        pruned: dict[str, dict[str, Any]] = {}
        for raw in items:
            if not isinstance(raw, dict):
                continue
            expires_at = str(raw.get("expires_at") or "")
            if expires_at:
                try:
                    parsed = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    if parsed < now:
                        continue
                except ValueError:
                    pass
            patch = raw.get("patch") if isinstance(raw.get("patch"), dict) else {}
            memory_id = str(raw.get("memory_id") or self._memory_id(self._normalize_patch(patch), str(raw.get("provider") or "selected")))
            raw["memory_id"] = memory_id
            pruned[memory_id] = raw
        return list(pruned.values())

    def _rank_items(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        def score(item: dict[str, Any]) -> tuple[float, str]:
            quality = item.get("quality") if isinstance(item.get("quality"), dict) else {}
            return (float(quality.get("score") or 0.0), str(item.get("updated_at") or item.get("created_at") or ""))

        return sorted(items, key=score)

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
