from __future__ import annotations

import hashlib
from typing import Any
from urllib.parse import urlparse

from core.context_scope import (
    OPERATOR_GLOBAL_SCOPE,
    item_visible_to_scope,
    probe_scope_metadata,
)
from core.model_client import redact_sensitive
from core.perception_layer import PerceptionLayer
from core.reasoning_core import CoreReasoning
from core.world_state import WorldStateStore
from interface.event_schema import utc_now_iso
from probes.network_probe import NetworkProbe
from probes.search_probe import SearchProbe
from probes.web_probe import WebProbe


class ExternalWorldRefresh:
    """Refreshes ExternalWorld watchlist entries with read-only probes."""

    def __init__(self, state_store: WorldStateStore, reasoning: CoreReasoning | None = None, search_probe: SearchProbe | None = None) -> None:
        self.state_store = state_store
        self.reasoning = reasoning or CoreReasoning(state_store)
        self.perception = PerceptionLayer(state_store, reasoning=self.reasoning)
        self.search_probe = search_probe or SearchProbe()

    def refresh_watchlist(
        self,
        limit: int = 10,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        external = self.state_store.read_json("external_world.json")
        if external.get("_state_corrupt"):
            return {
                "status": "degraded",
                "reason": "external_world_integrity_invalid",
                "refreshed": [],
                "skipped": [],
                "summary_count": 0,
            }
        scoped = bool(user_id is not None or session_id is not None)
        if scoped and (not user_id or not session_id):
            return {
                "status": "degraded",
                "reason": "external_refresh_scope_incomplete",
                "refreshed": [],
                "skipped": [],
                "summary_count": 0,
            }
        # Goal discovery is an operator-wide maintenance operation.  An exact
        # owner/session refresh must not import or compact another owner's
        # watch targets as an incidental side effect.
        if not scoped:
            self._sync_goal_watchlist(external)
        watchlist = external.get("watchlist", [])
        if not isinstance(watchlist, list):
            watchlist = []
        if scoped:
            watchlist = [
                item
                for item in watchlist
                if item_visible_to_scope(
                    item,
                    user_id=user_id,
                    session_id=session_id,
                )
            ]
        refreshed: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for raw_item in watchlist[:limit]:
            item = self._normalize_watch_item(raw_item)
            scope_metadata = probe_scope_metadata(item)
            target = str(item.get("target") or "").strip()
            try:
                if not item.get("enabled", True):
                    skipped.append({"target": item.get("target"), "reason": "disabled"})
                    continue
                if not target:
                    skipped.append({"target": "", "reason": "missing_target"})
                    continue
                if item.get("kind") == "learning_search":
                    refreshed.append(
                        self._scoped_refresh_result(
                            self._refresh_learning_search(item),
                            scope_metadata,
                        )
                    )
                    continue
                if item.get("kind") == "external_search":
                    refreshed.append(
                        self._scoped_refresh_result(
                            self._refresh_external_search(item),
                            scope_metadata,
                        )
                    )
                    continue
                probe_result = {
                    **self._probe_target(target),
                    **scope_metadata,
                }
                state_patch: dict[str, Any] = {}
                if scope_metadata.get("scope_kind") == OPERATOR_GLOBAL_SCOPE:
                    state_patch = self.perception.interpret_probe_result(probe_result)
                assist = self.reasoning.external_world_assist(
                    target=target,
                    probe_result=probe_result,
                    current_goal=self._current_goal_for_watch_item(item),
                )
                refreshed.append(
                    {
                        "target": target,
                        **scope_metadata,
                        "kind": item.get("kind") or self._kind_for_target(target),
                        "status": probe_result.get("status"),
                        "summary": self._summary_for(target, probe_result, assist),
                        "observed_at": probe_result.get("observed_at") or probe_result.get("timestamp") or utc_now_iso(),
                        "watch_recommendation": self._watch_recommendation(assist),
                        "probe": redact_sensitive(probe_result, max_string=1200),
                        "state_patch": state_patch,
                        "model_assist": self._compact_assist(assist),
                    }
                )
            except Exception as exc:
                refreshed.append(
                    {
                        "target": target,
                        **scope_metadata,
                        "kind": item.get("kind") or self._kind_for_target(target),
                        "status": "error",
                        "summary": f"External watch refresh failed without blocking active loop: {type(exc).__name__}: {str(exc)[:240]}",
                        "observed_at": utc_now_iso(),
                        "watch_recommendation": "keep",
                        "error_type": type(exc).__name__,
                    }
                )
        summary_count = 0
        integrity_invalid = False

        def merge_refresh(current: dict[str, Any]) -> dict[str, Any]:
            nonlocal integrity_invalid, summary_count
            if current.get("_state_corrupt"):
                # A concurrent integrity failure must not be converted into a
                # valid-looking document by this refresh.
                integrity_invalid = True
                return current
            summaries = current.get("summaries") if isinstance(current.get("summaries"), list) else []
            knowledge_items = (
                current.get("knowledge_items", [])
                if isinstance(current.get("knowledge_items"), list)
                else []
            )
            push_candidates = (
                current.get("push_candidates", [])
                if isinstance(current.get("push_candidates"), list)
                else []
            )
            if scoped:
                assert user_id is not None and session_id is not None
                current["summaries"] = self._merge_scoped_partition(
                    summaries,
                    refreshed,
                    user_id=user_id,
                    session_id=session_id,
                    limit=100,
                )
                current["knowledge_items"] = self._merge_scoped_knowledge_items(
                    knowledge_items,
                    refreshed,
                    user_id=user_id,
                    session_id=session_id,
                )
                current["push_candidates"] = self._merge_scoped_push_candidates(
                    push_candidates,
                    refreshed,
                    user_id=user_id,
                    session_id=session_id,
                )
                # A scoped refresh reads the watchlist but never performs
                # operator-wide discovery or retention on that shared list.
            else:
                self._sync_goal_watchlist(current)
                current["summaries"] = [*summaries, *refreshed][-100:]
                current["knowledge_items"] = self._merge_knowledge_items(
                    knowledge_items,
                    refreshed,
                )
                current["push_candidates"] = self._merge_push_candidates(
                    push_candidates,
                    refreshed,
                )
                current_watchlist = (
                    current.get("watchlist")
                    if isinstance(current.get("watchlist"), list)
                    else []
                )
                current["watchlist"] = current_watchlist[-100:]
            current["last_refresh_at"] = utc_now_iso()
            if scoped:
                summary_count = sum(
                    1
                    for item in current["summaries"]
                    if item_visible_to_scope(
                        item,
                        user_id=str(user_id or ""),
                        session_id=str(session_id or ""),
                    )
                )
            else:
                summary_count = len(current["summaries"])
            return current

        self.state_store.mutate_json("external_world.json", merge_refresh)
        if integrity_invalid:
            return {
                "status": "degraded",
                "reason": "external_world_integrity_invalid",
                "refreshed": [],
                "skipped": [],
                "summary_count": 0,
            }
        return {"status": "success", "refreshed": refreshed, "skipped": skipped, "summary_count": summary_count}

    @staticmethod
    def _scope_partition(
        items: list[Any],
        *,
        user_id: str,
        session_id: str,
    ) -> tuple[list[Any], list[Any]]:
        """Split one shared collection without deleting other-owner rows."""

        visible: list[Any] = []
        preserved: list[Any] = []
        for item in items:
            if item_visible_to_scope(
                item,
                user_id=user_id,
                session_id=session_id,
            ):
                visible.append(item)
            else:
                preserved.append(item)
        return preserved, visible

    def _merge_scoped_partition(
        self,
        existing: list[Any],
        additions: list[dict[str, Any]],
        *,
        user_id: str,
        session_id: str,
        limit: int,
    ) -> list[Any]:
        preserved, visible = self._scope_partition(
            existing,
            user_id=user_id,
            session_id=session_id,
        )
        return [*preserved, *[*visible, *additions][-limit:]]

    def _merge_scoped_knowledge_items(
        self,
        existing: list[Any],
        refreshed: list[dict[str, Any]],
        *,
        user_id: str,
        session_id: str,
    ) -> list[dict[str, Any]]:
        preserved, visible = self._scope_partition(
            existing,
            user_id=user_id,
            session_id=session_id,
        )
        return [
            *(item for item in preserved if isinstance(item, dict)),
            *self._merge_knowledge_items(visible, refreshed),
        ]

    def _merge_scoped_push_candidates(
        self,
        existing: list[Any],
        refreshed: list[dict[str, Any]],
        *,
        user_id: str,
        session_id: str,
    ) -> list[dict[str, Any]]:
        preserved, visible = self._scope_partition(
            existing,
            user_id=user_id,
            session_id=session_id,
        )
        return [
            *(item for item in preserved if isinstance(item, dict)),
            *self._merge_push_candidates(visible, refreshed),
        ]

    def _sync_goal_watchlist(self, external: dict[str, Any]) -> None:
        watchlist = external.setdefault("watchlist", [])
        if not isinstance(watchlist, list):
            watchlist = []
            external["watchlist"] = watchlist
        existing_targets = {str(item.get("target")) for item in watchlist if isinstance(item, dict)}
        goal_state = self.state_store.read_json("user_goals.json")
        goals = goal_state.get("goals") if isinstance(goal_state.get("goals"), list) else []
        for goal in goals:
            if not isinstance(goal, dict) or goal.get("kind") != "learning" or goal.get("status") != "active":
                continue
            permissions = goal.get("permissions") if isinstance(goal.get("permissions"), dict) else {}
            if not str(permissions.get("external_search") or "").startswith("granted"):
                continue
            goal_id = str(goal.get("goal_id") or "")
            topic = str(goal.get("topic") or "").strip()
            if not goal_id or not topic:
                continue
            target = f"learning:{goal_id}"
            if target in existing_targets:
                continue
            watchlist.append(
                {
                    "target": target,
                    "kind": "learning_search",
                    "enabled": True,
                    "topic": topic,
                    "goal_id": goal_id,
                    "user_id": goal.get("user_id"),
                    "session_id": goal.get("session_id"),
                    "commitment_id": goal.get("commitment_id"),
                    "query": self._learning_query(topic),
                    "reason": "authorized_learning_goal",
                    "created_at": utc_now_iso(),
                }
            )
            existing_targets.add(target)

    def _refresh_learning_search(self, item: dict[str, Any]) -> dict[str, Any]:
        topic = str(item.get("topic") or item.get("target") or "学习主题")
        query = str(item.get("query") or self._learning_query(topic))
        search_result = self._safe_search(query, max_results=6)
        results = search_result.get("details", {}).get("results") if isinstance(search_result.get("details"), dict) else []
        scored = self._score_search_results(results if isinstance(results, list) else [], topic=topic, ttl_seconds=int(search_result.get("ttl_seconds") or 1800))
        useful = [entry for entry in scored if float(entry.get("score") or 0) >= 0.45]
        return {
            "target": item.get("target"),
            "kind": "learning_search",
            "goal_id": item.get("goal_id"),
            "user_id": item.get("user_id"),
            "session_id": item.get("session_id"),
            "commitment_id": item.get("commitment_id"),
            "topic": topic,
            "query": query,
            "status": search_result.get("status"),
            "summary": self._learning_search_summary(topic, useful, search_result),
            "observed_at": search_result.get("observed_at") or search_result.get("timestamp") or utc_now_iso(),
            "results": useful[:5],
            "probe": redact_sensitive(search_result, max_string=1000),
            "watch_recommendation": "keep",
        }

    def _refresh_external_search(self, item: dict[str, Any]) -> dict[str, Any]:
        topic = str(item.get("topic") or item.get("target") or "外部主题")
        query = str(item.get("query") or f"{topic} latest important updates")
        search_result = self._safe_search(query, max_results=6)
        results = search_result.get("details", {}).get("results") if isinstance(search_result.get("details"), dict) else []
        scored = self._score_search_results(results if isinstance(results, list) else [], topic=topic, ttl_seconds=int(search_result.get("ttl_seconds") or 1800))
        useful = [entry for entry in scored if float(entry.get("score") or 0) >= 0.45]
        return {
            "target": item.get("target"),
            "kind": "external_search",
            "watchlist_id": item.get("watchlist_id"),
            "user_id": item.get("user_id"),
            "session_id": item.get("session_id"),
            "commitment_id": item.get("commitment_id"),
            "topic": topic,
            "query": query,
            "status": search_result.get("status"),
            "summary": self._external_search_summary(topic, useful, search_result),
            "observed_at": search_result.get("observed_at") or search_result.get("timestamp") or utc_now_iso(),
            "results": useful[:5],
            "probe": redact_sensitive(search_result, max_string=1000),
            "watch_recommendation": "keep",
        }

    def _learning_query(self, topic: str) -> str:
        return f"{topic} latest tutorial course paper 2026"

    def _current_goal_for_watch_item(self, item: dict[str, Any]) -> str:
        user_world = self.state_store.read_json("user_world.json")
        user_id = str(item.get("user_id") or "").strip()
        if user_id:
            profiles = user_world.get("profiles_by_user") if isinstance(user_world.get("profiles_by_user"), dict) else {}
            scoped = profiles.get(user_id) if isinstance(profiles.get(user_id), dict) else {}
            return str(scoped.get("current_goal") or "").strip()
        return str(user_world.get("current_goal") or "").strip()

    @staticmethod
    def _scoped_refresh_result(
        result: dict[str, Any],
        scope_metadata: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            **result,
            **scope_metadata,
        }

    def _safe_search(self, query: str, *, max_results: int) -> dict[str, Any]:
        try:
            return self.search_probe.run(query, max_results=max_results)
        except Exception as exc:
            return {
                "probe": "search_probe",
                "source": "search_probe",
                "target": query,
                "status": "unavailable",
                "summary": f"Search failed without blocking active loop for {query}: {type(exc).__name__}: {str(exc)[:240]}",
                "confidence": 0.2,
                "ttl_seconds": 300,
                "observed_at": utc_now_iso(),
                "details": {"query": query, "error": str(exc), "error_type": type(exc).__name__, "results": []},
            }

    def _score_search_results(self, results: list[Any], *, topic: str, ttl_seconds: int = 1800) -> list[dict[str, Any]]:
        scored: list[dict[str, Any]] = []
        topic_lower = topic.lower()
        trusted_hosts = ("arxiv.org", "deeplearning.ai", "pytorch.org", "tensorflow.org", "paperswithcode.com", "openreview.net", "github.com")
        retrieved_at = utc_now_iso()
        ttl = max(60, min(int(ttl_seconds or 1800), 86400))
        for raw in results:
            if not isinstance(raw, dict):
                continue
            title = str(raw.get("title") or "")
            url = str(raw.get("url") or "")
            snippet = str(raw.get("snippet") or "")
            host = str(raw.get("source") or self._host(url))
            source = host or str(raw.get("source") or "unknown")
            haystack = f"{title} {snippet}".lower()
            score = 0.2
            if topic_lower and topic_lower in haystack:
                score += 0.35
            if any(marker in haystack for marker in ("latest", "2026", "2025", "new", "updated", "最新")):
                score += 0.15
            if any(host.endswith(trusted) for trusted in trusted_hosts):
                score += 0.18
            if any(marker in haystack for marker in ("tutorial", "course", "paper", "guide", "入门", "课程", "论文")):
                score += 0.12
            score = round(min(score, 1.0), 3)
            summary = self._result_summary(title=title, snippet=snippet)
            dedupe_key = self._dedupe_key(url=url, title=title, source=source, snippet=snippet)
            scored.append(
                {
                    "title": title[:240],
                    "url": url,
                    "snippet": snippet[:500],
                    "summary": summary,
                    "source": source,
                    "retrieved_at": retrieved_at,
                    "ttl": ttl,
                    "dedupe_key": dedupe_key,
                    "quality_score": score,
                    "score": score,
                }
            )
        return sorted(scored, key=lambda item: float(item.get("score") or 0), reverse=True)

    def _learning_search_summary(self, topic: str, useful: list[dict[str, Any]], search_result: dict[str, Any]) -> str:
        if useful:
            top = useful[0]
            return f"Found {len(useful)} useful external item(s) for {topic}; top item: {top.get('title')}"
        return str(search_result.get("summary") or f"No useful external item found for {topic}.")

    def _external_search_summary(self, topic: str, useful: list[dict[str, Any]], search_result: dict[str, Any]) -> str:
        if useful:
            top = useful[0]
            return f"Found {len(useful)} useful external update(s) for {topic}; top item: {top.get('title')}"
        return str(search_result.get("summary") or f"No useful external update found for {topic}.")

    def _merge_knowledge_items(self, existing: list[Any], refreshed: list[dict[str, Any]]) -> list[dict[str, Any]]:
        by_key: dict[str, dict[str, Any]] = {str(item.get("item_id")): item for item in existing if isinstance(item, dict) and item.get("item_id")}
        for refresh in refreshed:
            if refresh.get("kind") not in {"learning_search", "external_search"}:
                continue
            scope_metadata = probe_scope_metadata(refresh)
            scope_key = self._scope_dedupe_key(scope_metadata)
            for result in refresh.get("results", []) if isinstance(refresh.get("results"), list) else []:
                if not isinstance(result, dict) or not (result.get("url") or result.get("title")):
                    continue
                dedupe_key = str(result.get("dedupe_key") or result.get("url") or f"{refresh.get('topic')}:{result.get('source')}:{result.get('title')}")
                item_id = self._stable_id("knowledge", f"{scope_key}:{dedupe_key}")
                by_key[item_id] = {
                    "item_id": item_id,
                    "dedupe_key": dedupe_key,
                    **scope_metadata,
                    "goal_id": refresh.get("goal_id"),
                    "topic": refresh.get("topic"),
                    "title": result.get("title"),
                    "url": result.get("url"),
                    "source": result.get("source"),
                    "snippet": result.get("snippet"),
                    "summary": result.get("summary"),
                    "retrieved_at": result.get("retrieved_at") or refresh.get("observed_at") or utc_now_iso(),
                    "ttl": result.get("ttl"),
                    "quality_score": result.get("quality_score") or result.get("score"),
                    "score": result.get("score"),
                    "status": "fresh",
                    "observed_at": refresh.get("observed_at") or utc_now_iso(),
                }
        return list(by_key.values())[-200:]

    def _merge_push_candidates(self, existing: list[Any], refreshed: list[dict[str, Any]]) -> list[dict[str, Any]]:
        by_key: dict[str, dict[str, Any]] = {str(item.get("candidate_id")): item for item in existing if isinstance(item, dict) and item.get("candidate_id")}
        for refresh in refreshed:
            if refresh.get("kind") not in {"learning_search", "external_search"} or not refresh.get("commitment_id"):
                continue
            scope_metadata = probe_scope_metadata(refresh)
            scope_key = self._scope_dedupe_key(scope_metadata)
            for result in refresh.get("results", []) if isinstance(refresh.get("results"), list) else []:
                if not isinstance(result, dict) or not (result.get("url") or result.get("title")) or float(result.get("score") or 0) < 0.6:
                    continue
                dedupe_key = str(result.get("dedupe_key") or result.get("url") or result.get("title") or "")
                candidate_id = self._stable_id(
                    "push",
                    f"{scope_key}:{refresh.get('commitment_id')}:{dedupe_key}",
                )
                current = by_key.get(candidate_id, {})
                by_key[candidate_id] = {
                    **current,
                    "candidate_id": candidate_id,
                    "dedupe_key": dedupe_key,
                    **scope_metadata,
                    "commitment_id": refresh.get("commitment_id"),
                    "goal_id": refresh.get("goal_id"),
                    "topic": refresh.get("topic"),
                    "title": result.get("title"),
                    "url": result.get("url"),
                    "snippet": result.get("snippet"),
                    "summary": result.get("summary"),
                    "source": result.get("source"),
                    "retrieved_at": result.get("retrieved_at") or refresh.get("observed_at") or utc_now_iso(),
                    "ttl": result.get("ttl"),
                    "quality_score": result.get("quality_score") or result.get("score"),
                    "score": result.get("score"),
                    "status": current.get("status") or "new",
                    "created_at": current.get("created_at") or utc_now_iso(),
                    "updated_at": utc_now_iso(),
                }
        return list(by_key.values())[-100:]

    def _probe_target(self, target: str) -> dict[str, Any]:
        if target.startswith("http://") or target.startswith("https://"):
            return WebProbe().run(target)
        return NetworkProbe().run(target)

    def _normalize_watch_item(self, item: Any) -> dict[str, Any]:
        if isinstance(item, str):
            return {"target": item, "enabled": True}
        if isinstance(item, dict):
            return {"enabled": True, **item}
        return {"target": "", "enabled": False}

    def _summary_for(self, target: str, probe_result: dict[str, Any], assist: dict[str, Any]) -> str:
        if assist.get("status") == "model_assisted" and assist.get("summary"):
            return str(assist.get("summary"))[:1000]
        return str(probe_result.get("summary") or f"{target} returned {probe_result.get('status', 'unknown')}")

    def _watch_recommendation(self, assist: dict[str, Any]) -> str:
        value = str(assist.get("watch_recommendation") or "keep")
        return value if value in {"keep", "pause", "remove"} else "keep"

    def _compact_assist(self, assist: dict[str, Any]) -> dict[str, Any]:
        if assist.get("status") != "model_assisted":
            return {"status": assist.get("status", "skipped")}
        return {
            "status": "model_assisted",
            "relevance": assist.get("relevance"),
            "watch_recommendation": self._watch_recommendation(assist),
            "reasons": assist.get("reasons") if isinstance(assist.get("reasons"), list) else [],
        }

    def _kind_for_target(self, target: str) -> str:
        return "web" if target.startswith("http://") or target.startswith("https://") else "network"

    def _stable_id(self, prefix: str, value: str) -> str:
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
        return f"{prefix}_{digest}"

    def _scope_dedupe_key(self, scope_metadata: dict[str, Any]) -> str:
        return "|".join(
            [
                str(scope_metadata.get("scope_kind") or ""),
                str(scope_metadata.get("user_id") or ""),
                str(scope_metadata.get("session_id") or ""),
                str(scope_metadata.get("scope_status") or ""),
            ]
        )

    def _host(self, url: str) -> str:
        return urlparse(url).netloc.lower()

    def _dedupe_key(self, *, url: str, title: str, source: str, snippet: str) -> str:
        normalized_url = (url or "").strip().lower().rstrip("/")
        if normalized_url:
            return normalized_url
        seed = "|".join([source.strip().lower(), " ".join(title.split()).lower(), " ".join(snippet.split()).lower()[:220]])
        return self._stable_id("search", seed)

    def _result_summary(self, *, title: str, snippet: str) -> str:
        title = " ".join(str(title or "").split())
        snippet = " ".join(str(snippet or "").split())
        if title and snippet:
            return f"{title}: {snippet[:360]}"
        return (title or snippet)[:400]
