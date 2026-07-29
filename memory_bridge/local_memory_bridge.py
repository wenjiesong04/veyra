from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from core.model_client import redact_sensitive
from core.reasoning_core import CoreReasoning
from core.world_state import WorldStateStore
from interface.agent_adapter import AgentAdapter
from interface.event_schema import utc_now_iso
from memory_bridge.memory_filter import MemoryFilter
from memory_bridge.scope import framed_sha256, normalize_scope_component


PROVIDER_VALIDATION_TTL_SECONDS = 900
MEMORY_READ_OK_STATUSES = {"success", "ok", "empty", "workspace_file_fallback", "workspace_file_empty"}
MEMORY_WRITE_OK_STATUSES = {
    "accepted",
    "ok",
    "success",
    "submitted",
    "written",
    "workspace_file_fallback",
}


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

    def read_summary(
        self,
        session_id: str,
        focus: list[str] | None = None,
        provider: str = "selected",
        *,
        user_id: str,
        model_assist: bool = True,
        external_read: bool = True,
    ) -> dict[str, Any]:
        user_scope, session_scope = self._required_scope(user_id=user_id, session_id=session_id)
        if provider not in self._provider_names():
            raise ValueError(f"unknown memory provider: {provider}")
        items = self.state_store.read_json("agent_memory.json").get("items", [])
        items = self._scoped_items(items, user_id=user_scope, session_id=session_scope)
        focused = self._relevant(items, focus)
        scoped_session_id = self._scoped_session_id(user_scope, session_scope)
        if model_assist:
            local, relevance = self._model_relevant(scoped_session_id, focus or [], focused)
        else:
            local = focused[-5:]
            relevance = {
                "status": "deterministic",
                "strategy": "owner_session_focus_recent",
            }
        if external_read:
            external = self._external_summary(user_scope, session_scope, provider)
        else:
            external = {
                "provider": provider,
                "status": "skipped",
                "summary": "",
                "freshness": "stale",
                "trust": "untrusted",
                "reason": "external_read_disabled",
            }
        return {
            "session_id": session_scope,
            "provider": provider,
            "summary": local,
            "external_summary": external,
            "freshness": self._freshness(local),
            "trust": "mixed" if external.get("summary") else "local",
            "relevance": relevance,
        }

    def write_patch(self, patch: dict[str, Any], provider: str = "selected") -> dict[str, Any]:
        if provider not in self._provider_names():
            return {
                "status": "blocked",
                "reason": "unknown_provider",
                "provider": provider,
            }
        try:
            user_id, session_id = self._required_scope(
                user_id=patch.get("user_id"),
                session_id=patch.get("session_id"),
            )
        except ValueError as exc:
            result = {
                "status": "blocked",
                "reason": "invalid_memory_scope",
                "error": str(exc),
            }
            self.state_store.append_jsonl("memory_log.jsonl", result)
            return result
        scoped_patch = dict(patch)
        scoped_patch["user_id"] = user_id
        scoped_patch["session_id"] = session_id
        filtered = self.filter.filter(scoped_patch)
        if filtered is None:
            result = {"status": "blocked", "reason": "memory policy rejected sensitive patch"}
            self.state_store.append_jsonl("memory_log.jsonl", result)
            return result
        filtered = self._normalize_patch(filtered)
        memory_id = self._memory_id(filtered, provider)
        now = utc_now_iso()
        item = {
            "memory_id": memory_id,
            "patch": filtered,
            "user_id": filtered.get("user_id"),
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
            "memory_class": "soft",
            "memory_namespace": "agent_bridge",
        }
        replaced = False

        def update_memory(state: dict[str, Any]) -> dict[str, Any]:
            nonlocal replaced
            items = self._prune_items(state.get("items", []) if isinstance(state.get("items"), list) else [])
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
            state["memory_class"] = "soft"
            state["memory_namespace"] = "agent_bridge"
            return state

        self.state_store.mutate_json("agent_memory.json", update_memory)
        external = self._external_write(filtered, provider)
        external_status = str(external.get("status") or "")
        status = (
            "written"
            if provider == "local"
            or external_status in MEMORY_WRITE_OK_STATUSES
            else "partial"
        )
        result = {
            "status": status,
            "local_status": "written",
            "deduped": replaced,
            "item": item,
            "external_write": external,
        }
        self.state_store.append_jsonl("memory_log.jsonl", result)
        return result

    def provider_status(self, *, probe: bool = True) -> dict[str, Any]:
        names = self._provider_names()
        return {
            "providers": names,
            "default": "selected",
            "supports": ["summary", "patch"],
            "diagnostics_endpoint": "/memory/providers/diagnostics",
            "validation": {
                name: self._provider_validation(name, probe=probe)
                for name in names
            },
            "notes": {
                "local": "Veyra local JSON memory only",
                "selected": "currently selected AgentAdapter memory API",
                "all": "fan-out read/write across configured external adapters plus local",
            },
        }

    def provider_diagnostics(
        self,
        provider: str = "all",
        session_id: str = "memory-diagnostics",
        write_probe: bool = False,
        *,
        user_id: str = "local-operator",
        record: bool = False,
        persist: bool = False,
        active_probe: bool = True,
        _adapter: AgentAdapter | None = None,
    ) -> dict[str, Any]:
        user_scope, session_scope = self._required_scope(user_id=user_id, session_id=session_id)
        if provider not in self._provider_names():
            return {
                "status": "not_configured",
                "provider": provider,
                "session_id": session_scope,
                "reason": "unknown_provider",
                "write_probe": {"status": "skipped"},
            }
        if provider == "all":
            if active_probe:
                results = [
                    self.provider_diagnostics(
                        "local",
                        session_id=session_scope,
                        write_probe=write_probe,
                        user_id=user_scope,
                        record=record,
                        persist=persist,
                        active_probe=True,
                    ),
                    *[
                        self.provider_diagnostics(
                            name,
                            session_id=session_scope,
                            write_probe=write_probe,
                            user_id=user_scope,
                            record=record,
                            persist=persist,
                            active_probe=True,
                            _adapter=adapter,
                        )
                        for name, adapter
                        in self._external_provider_bindings()
                    ],
                ]
            else:
                results = [
                    self.provider_diagnostics(
                        name,
                        session_id=session_scope,
                        write_probe=write_probe,
                        user_id=user_scope,
                        record=record,
                        persist=persist,
                        active_probe=False,
                    )
                    for name in self._provider_names()
                    if name != "all"
                ]
            statuses = {str(item.get("status") or "") for item in results}
            if statuses <= {"success"}:
                status = "success"
            elif not active_probe and statuses <= {"success", "probe_skipped"}:
                status = "probe_skipped"
            else:
                status = "degraded"
            output = {
                "status": status,
                "provider": "all",
                "session_id": session_scope,
                "write_probe": write_probe,
                "results": results,
            }
            if record:
                self._record_diagnostics(status, output)
            return output
        if provider == "local":
            state = self.state_store.read_json("agent_memory.json")
            items = state.get("items", []) if isinstance(state.get("items"), list) else []
            items = self._scoped_items(items, user_id=user_scope, session_id=session_scope)
            output = {
                "status": "success",
                "provider": "local",
                "mode": "local_json",
                "session_id": session_scope,
                "summary": {"status": "success", "items": len(items), "freshness": self._freshness(items[-20:]), "trust": "local"},
                "write_probe": {"status": "skipped", "reason": "local provider does not need external probe"},
                "validation": self._provider_validation("local"),
            }
            if record:
                self._record_diagnostics("success", output)
            return output
        if not active_probe:
            output = {
                "status": "probe_skipped",
                "provider": provider,
                "mode": "external_adapter",
                "session_id": session_scope,
                "connection": {
                    "status": "skipped",
                    "reason": "active_probe_disabled",
                },
                "summary": {
                    "status": "skipped",
                    "freshness": "stale",
                    "trust": "untrusted",
                    "content_present": False,
                },
                "write_probe": {
                    "status": "skipped",
                    "reason": "active_probe_disabled",
                },
                "read_after_write": {
                    "status": "skipped",
                    "content_present": False,
                },
                "validation": self._provider_validation(provider, probe=False),
            }
            if record:
                self._record_diagnostics("probe_skipped", output)
            return output
        adapter = _adapter or self._adapter_for(provider)
        if not adapter:
            output = {
                "status": "not_configured",
                "provider": provider,
                "mode": "external_adapter",
                "session_id": session_scope,
                "connection": {"status": "adapter_unconfigured", "connected": False},
                "summary": {"status": "not_configured", "freshness": "stale", "trust": "untrusted"},
                "write_probe": {"status": "skipped"},
                "validation": {
                    "implemented": True,
                    "configured": False,
                    "validated": False,
                    "status": "not_configured",
                    "validation_source": "none",
                },
            }
            if record:
                self._record_diagnostics("not_configured", output)
            return output
        try:
            connection = adapter.connection_status()
        except Exception as exc:
            connection = {"status": "error", "connected": False, "error": str(exc)}
        summary = self._external_summary_with_adapter(
            user_scope,
            session_scope,
            provider,
            adapter,
        )
        probe_result = {"status": "skipped", "reason": "write_probe disabled"}
        read_after_write = {"status": "skipped", "reason": "write_probe disabled"}
        probe_marker = ""
        if write_probe:
            probe_marker = self._diagnostic_probe_marker(provider, self._scoped_session_id(user_scope, session_scope))
            probe_result = self._external_write_with_adapter(
                {
                    "user_id": user_scope,
                    "session_id": session_scope,
                    "task": "veyra_memory_provider_diagnostics",
                    "memory_type": "diagnostic_probe",
                    "topic": probe_marker,
                    "summary": f"Veyra memory roundtrip marker {probe_marker}",
                    "result": probe_marker,
                    "freshness": "fresh",
                    "trust": "veyra_probe",
                },
                provider,
                adapter,
            )
            read_after_write = self._external_summary_with_adapter(
                user_scope,
                session_scope,
                provider,
                adapter,
            )
        validation = self._diagnostic_validation(
            adapter=adapter,
            connection=connection,
            summary=summary,
            probe_result=probe_result,
            read_after_write=read_after_write,
            probe_marker=probe_marker,
            write_probe=write_probe,
        )
        if persist:
            self._record_provider_validation(provider, validation)
        status = self._diagnostic_status(connection, summary, probe_result, write_probe)
        output = {
            "status": status,
            "provider": provider,
            "mode": "external_adapter",
            "session_id": session_scope,
            "connection": redact_sensitive(connection, max_string=1200),
            "summary": self._diagnostic_summary(summary),
            "write_probe": probe_result,
            "read_after_write": self._diagnostic_summary(read_after_write),
            "validation": validation,
        }
        if record:
            self._record_diagnostics(status, output)
        return output

    def _external_summary(self, user_id: str, session_id: str, provider: str) -> dict[str, Any]:
        if provider == "local":
            return {"provider": "local", "status": "local_only", "summary": "", "freshness": "fresh", "trust": "local"}
        if provider == "all":
            summaries = [
                self._external_summary_with_adapter(
                    user_id,
                    session_id,
                    name,
                    adapter,
                )
                for name, adapter in self._external_provider_bindings()
            ]
            return {
                "provider": "all",
                "status": self._aggregate_external_read_status(
                    summaries
                ),
                "summary": summaries,
                "freshness": self._freshness_from_external(summaries),
                "trust": "mixed",
            }
        adapter = self._adapter_for(provider)
        if not adapter:
            return {"provider": provider, "status": "not_configured", "summary": "", "freshness": "stale", "trust": "untrusted"}
        return self._external_summary_with_adapter(
            user_id,
            session_id,
            provider,
            adapter,
        )

    def _external_summary_with_adapter(
        self,
        user_id: str,
        session_id: str,
        provider: str,
        adapter: AgentAdapter,
    ) -> dict[str, Any]:
        try:
            response = adapter.fetch_memory_summary(self._scoped_session_id(user_id, session_id))
        except Exception as exc:
            return {"provider": provider, "status": "error", "summary": "", "error": str(exc), "freshness": "stale", "trust": "untrusted"}
        return self._normalize_external_summary(provider, response)

    def _external_write(self, patch: dict[str, Any], provider: str) -> dict[str, Any]:
        if provider == "local":
            return {"provider": "local", "status": "local_only"}
        if provider == "all":
            results = [
                self._external_write_with_adapter(
                    patch,
                    name,
                    adapter,
                )
                for name, adapter in self._external_provider_bindings()
            ]
            return {
                "provider": "all",
                "status": self._aggregate_external_write_status(results),
                "results": results,
            }
        adapter = self._adapter_for(provider)
        if not adapter:
            return {"provider": provider, "status": "not_configured"}
        return self._external_write_with_adapter(
            patch,
            provider,
            adapter,
        )

    def _external_write_with_adapter(
        self,
        patch: dict[str, Any],
        provider: str,
        adapter: AgentAdapter,
    ) -> dict[str, Any]:
        try:
            user_id, session_id = self._required_scope(
                user_id=patch.get("user_id"),
                session_id=patch.get("session_id"),
            )
        except ValueError:
            return {
                "provider": provider,
                "status": "blocked",
                "reason": "invalid_memory_scope",
            }
        external_patch = dict(patch)
        external_patch.pop("user_id", None)
        external_patch["session_id"] = self._scoped_session_id(user_id, session_id)
        external_patch["memory_scope"] = "veyra_user_session_v2"
        try:
            response = adapter.write_memory_patch(external_patch)
        except Exception as exc:
            return {"provider": provider, "status": "error", "error": str(exc)}
        if isinstance(response, dict):
            return {"provider": provider, **response}
        return {"provider": provider, "status": "submitted"}

    def _aggregate_external_read_status(
        self,
        results: list[dict[str, Any]],
    ) -> str:
        statuses = [
            str(item.get("status") or "error")
            for item in results
        ]
        if not statuses:
            return "not_configured"
        successes = [
            status
            for status in statuses
            if status in MEMORY_READ_OK_STATUSES
        ]
        if len(successes) == len(statuses):
            return "success"
        if successes:
            return "degraded"
        if all(status == "not_configured" for status in statuses):
            return "not_configured"
        if all(status in {"blocked", "invalid_memory_scope"} for status in statuses):
            return "blocked"
        return "error"

    def _aggregate_external_write_status(
        self,
        results: list[dict[str, Any]],
    ) -> str:
        statuses = [
            str(item.get("status") or "error")
            for item in results
        ]
        if not statuses:
            return "not_configured"
        successes = [
            status
            for status in statuses
            if status in MEMORY_WRITE_OK_STATUSES
        ]
        if len(successes) == len(statuses):
            return "submitted"
        if successes:
            return "degraded"
        if all(status == "not_configured" for status in statuses):
            return "not_configured"
        if all(status == "blocked" for status in statuses):
            return "blocked"
        return "error"

    def _relevant(self, items: list[Any], focus: list[str] | None) -> list[Any]:
        if not focus:
            return items
        relevant: list[Any] = []
        for item in items:
            haystack = str(item).lower()
            if any(str(term).lower() in haystack for term in focus):
                relevant.append(item)
        return relevant

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
        if trust in {"verified", "veyra_probe", "user_attested"}:
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
        digest = framed_sha256(
            "veyra-memory-id-v3",
            str(provider or "").lower(),
            str(patch.get("user_id") or ""),
            str(patch.get("session_id") or ""),
            str(patch.get("memory_type") or "").lower(),
            str(patch.get("topic") or "").lower(),
            str(patch.get("summary") or "")[:180].lower(),
        )
        return "mem_" + digest[:16]

    def _required_scope(
        self,
        *,
        user_id: Any,
        session_id: Any,
    ) -> tuple[str, str]:
        return (
            normalize_scope_component(user_id, "user_id"),
            normalize_scope_component(session_id, "session_id"),
        )

    def _scoped_session_id(self, user_id: str, session_id: str) -> str:
        user_scope, session_scope = self._required_scope(user_id=user_id, session_id=session_id)
        digest = framed_sha256(
            "veyra-memory-scope-v2",
            user_scope,
            session_scope,
        )
        return f"veyra-memory-v2-{digest[:32]}"

    def _scoped_items(self, items: list[Any], *, user_id: str, session_id: str) -> list[dict[str, Any]]:
        scoped: list[dict[str, Any]] = []
        for raw in items:
            if not isinstance(raw, dict):
                continue
            patch = raw.get("patch") if isinstance(raw.get("patch"), dict) else {}
            item_user = str(raw.get("user_id") or "").strip()
            item_session = str(raw.get("session_id") or "").strip()
            patch_user = str(patch.get("user_id") or "").strip()
            patch_session = str(patch.get("session_id") or "").strip()
            if not item_user or not item_session or not patch_user or not patch_session:
                continue
            if item_user != patch_user or item_session != patch_session:
                continue
            if item_user != user_id or item_session != session_id:
                continue
            scoped.append(raw)
        return scoped

    def _diagnostic_summary(self, summary: dict[str, Any]) -> dict[str, Any]:
        content = summary.get("summary")
        return {
            "provider": summary.get("provider"),
            "status": summary.get("status"),
            "freshness": summary.get("freshness"),
            "trust": summary.get("trust"),
            "content_present": bool(content),
        }

    def _record_diagnostics(self, status: str, output: dict[str, Any]) -> None:
        self.state_store.append_jsonl(
            "memory_log.jsonl",
            {
                "status": status,
                "reason": "provider_diagnostics",
                "result": redact_sensitive(output, max_string=1800),
            },
        )

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

    def _external_provider_names(self) -> list[str]:
        return [
            name
            for name, _ in self._external_provider_bindings()
        ]

    def _external_provider_bindings(
        self,
    ) -> list[tuple[str, AgentAdapter]]:
        bindings: list[tuple[str, AgentAdapter]] = []
        seen_adapters: set[int] = set()
        for name in self._provider_names():
            if name in {"local", "all"}:
                continue
            adapter = self._adapter_for(name)
            if adapter is not None:
                identity = id(adapter)
                if identity in seen_adapters:
                    continue
                seen_adapters.add(identity)
                bindings.append((name, adapter))
        return bindings

    def _provider_validation(self, provider: str, *, probe: bool = True) -> dict[str, Any]:
        if provider == "local":
            observed_at = utc_now_iso()
            return {
                "implemented": True,
                "configured": True,
                "connected": True,
                "validated": True,
                "status": "validated",
                "validation_source": "explicit_local_provider",
                "capabilities": {"memory_summary": True, "memory_patch": True},
                "observed_at": observed_at,
                "expires_at": None,
                "ttl_seconds": 0,
            }
        if provider == "all":
            return {"implemented": True, "configured": True, "validated": False, "status": "fan_out_runtime_dependent"}
        if not probe:
            cached = self._cached_provider_validation(provider)
            if cached:
                return {
                    **cached,
                    "validation_source": str(cached.get("validation_source") or "cached"),
                    "read_only": True,
                }
            return {
                "implemented": True,
                "configured": None,
                "connected": None,
                "validated": False,
                "status": "validation_unavailable",
                "validation_source": "none",
                "read_only": True,
            }
        adapter = self._adapter_for(provider)
        if not adapter:
            return {"implemented": True, "configured": False, "validated": False, "status": "not_configured"}
        try:
            connection = adapter.connection_status()
        except Exception as exc:
            return {"implemented": True, "configured": True, "validated": False, "status": "validation_pending", "error": str(exc)}
        capabilities = self._memory_capability_evidence(adapter, connection)
        cached = self._cached_provider_validation(provider)
        summary_validated = capabilities["memory_summary"] is True or bool(
            cached.get("read", {}).get("validated") if isinstance(cached.get("read"), dict) else False
        )
        patch_validated = capabilities["memory_patch"] is True or bool(
            cached.get("write", {}).get("validated") if isinstance(cached.get("write"), dict) else False
        )
        connected = bool(connection.get("connected"))
        configured = str(connection.get("status")) not in {"adapter_unconfigured", "not_configured"}
        validated = configured and summary_validated and patch_validated
        if capabilities["memory_summary"] is True and capabilities["memory_patch"] is True:
            validation_source = "explicit_capability"
        elif validated and cached:
            validation_source = str(cached.get("validation_source") or "diagnostic_roundtrip")
        else:
            validation_source = "none"
        if validated:
            status = "validated" if connected else "validated_unavailable"
        elif capabilities["memory_summary"] is False or capabilities["memory_patch"] is False:
            status = "capability_unavailable"
        else:
            status = "validation_pending" if configured else "not_configured"
        observed_at = str(cached.get("observed_at") or utc_now_iso())
        expires_at = str(
            cached.get("expires_at")
            or (datetime.now(timezone.utc) + timedelta(seconds=PROVIDER_VALIDATION_TTL_SECONDS)).isoformat()
        )
        return {
            "implemented": True,
            "configured": configured,
            "connected": connected,
            "validated": validated,
            "status": status,
            "validation_source": validation_source,
            "capabilities": capabilities,
            "read": {
                "validated": summary_validated,
                "source": "explicit_capability"
                if capabilities["memory_summary"] is True
                else str(cached.get("read", {}).get("source") or "none")
                if isinstance(cached.get("read"), dict)
                else "none",
            },
            "write": {
                "validated": patch_validated,
                "source": "explicit_capability"
                if capabilities["memory_patch"] is True
                else str(cached.get("write", {}).get("source") or "none")
                if isinstance(cached.get("write"), dict)
                else "none",
            },
            "observed_at": observed_at,
            "expires_at": expires_at,
            "ttl_seconds": PROVIDER_VALIDATION_TTL_SECONDS,
        }

    def _memory_capability_evidence(self, adapter: AgentAdapter, connection: dict[str, Any]) -> dict[str, bool | None]:
        candidates: list[dict[str, Any]] = []
        if isinstance(connection.get("capabilities"), dict):
            candidates.append(connection["capabilities"])
        candidates.append(connection)
        found = self._memory_capabilities_from_candidates(candidates)
        if found["memory_summary"] is not None and found["memory_patch"] is not None:
            return found
        try:
            fetched = adapter.fetch_capabilities()
        except Exception:
            return found
        if isinstance(fetched, dict):
            candidates.append(fetched)
        return self._memory_capabilities_from_candidates(candidates)

    def _memory_capabilities_from_candidates(self, candidates: list[dict[str, Any]]) -> dict[str, bool | None]:
        result: dict[str, bool | None] = {"memory_summary": None, "memory_patch": None}
        for candidate in candidates:
            raw = candidate.get("raw") if isinstance(candidate.get("raw"), dict) else None
            if raw is not None:
                features = raw.get("features") if isinstance(raw.get("features"), dict) else {}
            else:
                features = candidate.get("features") if isinstance(candidate.get("features"), dict) else {}
            compatibility = candidate.get("compatibility") if isinstance(candidate.get("compatibility"), dict) else {}
            optional_methods = (
                compatibility.get("optional_methods")
                if isinstance(compatibility.get("optional_methods"), dict)
                else {}
            )
            optional_features = (
                compatibility.get("optional_features")
                if isinstance(compatibility.get("optional_features"), dict)
                else {}
            )
            for field, method in (("memory_summary", "memory.summary"), ("memory_patch", "memory.patch")):
                if field in features:
                    result[field] = bool(features.get(field))
                elif method in optional_methods:
                    result[field] = bool(optional_methods.get(method))
                elif isinstance(optional_features.get(field), bool):
                    result[field] = optional_features[field]
        return result

    def _diagnostic_validation(
        self,
        *,
        adapter: AgentAdapter,
        connection: dict[str, Any],
        summary: dict[str, Any],
        probe_result: dict[str, Any],
        read_after_write: dict[str, Any],
        probe_marker: str,
        write_probe: bool,
    ) -> dict[str, Any]:
        capabilities = self._memory_capability_evidence(adapter, connection)
        summary_status = str(summary.get("status") or "")
        read_probe_succeeded = (
            self._adapter_overrides(adapter, "fetch_memory_summary", AgentAdapter.fetch_memory_summary)
            and summary_status in MEMORY_READ_OK_STATUSES
        )
        write_status = str(probe_result.get("status") or "")
        write_accepted = (
            write_probe
            and self._adapter_overrides(adapter, "write_memory_patch", AgentAdapter.write_memory_patch)
            and write_status in MEMORY_WRITE_OK_STATUSES
        )
        post_summary = str(read_after_write.get("summary") or "")
        roundtrip_confirmed = bool(write_accepted and probe_marker and probe_marker in post_summary)
        read_validated = capabilities["memory_summary"] is True or read_probe_succeeded
        write_validated = capabilities["memory_patch"] is True or roundtrip_confirmed
        validated = read_validated and write_validated
        if capabilities["memory_summary"] is True and capabilities["memory_patch"] is True:
            validation_source = "explicit_capability"
        elif roundtrip_confirmed:
            validation_source = "diagnostic_roundtrip"
        elif read_probe_succeeded:
            validation_source = "diagnostic_read_only"
        else:
            validation_source = "none"
        observed_at = utc_now_iso()
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=PROVIDER_VALIDATION_TTL_SECONDS)).isoformat()
        return {
            "implemented": True,
            "configured": str(connection.get("status")) not in {"adapter_unconfigured", "not_configured"},
            "connected": bool(connection.get("connected")),
            "validated": validated,
            "status": "validated"
            if validated
            else "roundtrip_unconfirmed"
            if write_probe and write_accepted
            else "write_probe_required"
            if read_validated
            else "validation_pending",
            "validation_source": validation_source,
            "capabilities": capabilities,
            "read": {
                "validated": read_validated,
                "source": "explicit_capability" if capabilities["memory_summary"] is True else "diagnostic_read",
                "status": summary_status,
            },
            "write": {
                "validated": write_validated,
                "source": "explicit_capability"
                if capabilities["memory_patch"] is True
                else "diagnostic_roundtrip"
                if roundtrip_confirmed
                else "none",
                "status": write_status,
                "accepted": write_accepted,
                "roundtrip_confirmed": roundtrip_confirmed,
            },
            "observed_at": observed_at,
            "expires_at": expires_at,
            "ttl_seconds": PROVIDER_VALIDATION_TTL_SECONDS,
        }

    def _record_provider_validation(self, provider: str, validation: dict[str, Any]) -> None:
        if provider in {"local", "all"}:
            return

        def update(state: dict[str, Any]) -> dict[str, Any]:
            validations = (
                state.get("provider_validation")
                if isinstance(state.get("provider_validation"), dict)
                else {}
            )
            current = validations.get(provider) if isinstance(validations.get(provider), dict) else {}
            if bool(current.get("validated")) and not bool(validation.get("validated")) and not self._validation_expired(current):
                return state
            validations[provider] = redact_sensitive(validation, max_string=1200)
            state["provider_validation"] = validations
            return state

        self.state_store.mutate_json("agent_memory.json", update)

    def _cached_provider_validation(self, provider: str) -> dict[str, Any]:
        state = self.state_store.read_json("agent_memory.json")
        validations = state.get("provider_validation") if isinstance(state.get("provider_validation"), dict) else {}
        validation = validations.get(provider) if isinstance(validations.get(provider), dict) else {}
        if not validation or self._validation_expired(validation):
            return {}
        return validation

    def _validation_expired(self, validation: dict[str, Any]) -> bool:
        expires_at = str(validation.get("expires_at") or "")
        if not expires_at:
            return True
        try:
            parsed = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError:
            return True
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed <= datetime.now(timezone.utc)

    def _diagnostic_probe_marker(self, provider: str, session_id: str) -> str:
        raw = f"{provider}|{session_id}|{utc_now_iso()}"
        return "veyra_mem_probe_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def _adapter_overrides(self, adapter: AgentAdapter, name: str, base_method: Any) -> bool:
        method = getattr(adapter, name, None)
        implementation = getattr(method, "__func__", method)
        return callable(method) and implementation is not base_method

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
        if write_probe and probe_result.get("status") not in MEMORY_WRITE_OK_STATUSES | {"local_only"}:
            return "degraded"
        return "success"

    def _model_relevant(self, session_id: str, focus: list[str], items: list[Any]) -> tuple[list[Any], dict[str, Any]]:
        if not items:
            return [], {"status": "empty", "strategy": "owner_session_scope"}
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
