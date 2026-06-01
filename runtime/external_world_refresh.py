from __future__ import annotations

import hashlib
from typing import Any
from urllib.parse import urlparse

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

    def refresh_watchlist(self, limit: int = 10) -> dict[str, Any]:
        external = self.state_store.read_json("external_world.json")
        self._sync_goal_watchlist(external)
        watchlist = external.get("watchlist", [])
        if not isinstance(watchlist, list):
            watchlist = []
        refreshed: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for raw_item in watchlist[:limit]:
            item = self._normalize_watch_item(raw_item)
            if not item.get("enabled", True):
                skipped.append({"target": item.get("target"), "reason": "disabled"})
                continue
            target = str(item.get("target") or "").strip()
            if not target:
                skipped.append({"target": "", "reason": "missing_target"})
                continue
            if item.get("kind") == "learning_search":
                refreshed.append(self._refresh_learning_search(item))
                continue
            probe_result = self._probe_target(target)
            state_patch = self.perception.interpret_probe_result(probe_result)
            assist = self.reasoning.external_world_assist(
                target=target,
                probe_result=probe_result,
                current_goal=str(self.state_store.read_json("user_world.json").get("current_goal") or ""),
            )
            refreshed.append(
                {
                    "target": target,
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
        external["summaries"] = (external.get("summaries", []) if isinstance(external.get("summaries"), list) else []) + refreshed
        external["summaries"] = external["summaries"][-100:]
        external["knowledge_items"] = self._merge_knowledge_items(
            external.get("knowledge_items", []) if isinstance(external.get("knowledge_items"), list) else [],
            refreshed,
        )
        external["push_candidates"] = self._merge_push_candidates(
            external.get("push_candidates", []) if isinstance(external.get("push_candidates"), list) else [],
            refreshed,
        )
        external["watchlist"] = watchlist[-100:]
        external["last_refresh_at"] = utc_now_iso()
        self.state_store.write_json("external_world.json", external)
        return {"status": "success", "refreshed": refreshed, "skipped": skipped, "summary_count": len(external["summaries"])}

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
        search_result = self.search_probe.run(query, max_results=6)
        results = search_result.get("details", {}).get("results") if isinstance(search_result.get("details"), dict) else []
        scored = self._score_search_results(results if isinstance(results, list) else [], topic=topic)
        useful = [entry for entry in scored if float(entry.get("score") or 0) >= 0.45]
        return {
            "target": item.get("target"),
            "kind": "learning_search",
            "goal_id": item.get("goal_id"),
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

    def _learning_query(self, topic: str) -> str:
        return f"{topic} latest tutorial course paper 2026"

    def _score_search_results(self, results: list[Any], *, topic: str) -> list[dict[str, Any]]:
        scored: list[dict[str, Any]] = []
        topic_lower = topic.lower()
        trusted_hosts = ("arxiv.org", "deeplearning.ai", "pytorch.org", "tensorflow.org", "paperswithcode.com", "openreview.net", "github.com")
        for raw in results:
            if not isinstance(raw, dict):
                continue
            title = str(raw.get("title") or "")
            url = str(raw.get("url") or "")
            snippet = str(raw.get("snippet") or "")
            host = str(raw.get("source") or self._host(url))
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
            scored.append(
                {
                    "title": title[:240],
                    "url": url,
                    "snippet": snippet[:500],
                    "source": host,
                    "score": round(min(score, 1.0), 3),
                }
            )
        return sorted(scored, key=lambda item: float(item.get("score") or 0), reverse=True)

    def _learning_search_summary(self, topic: str, useful: list[dict[str, Any]], search_result: dict[str, Any]) -> str:
        if useful:
            top = useful[0]
            return f"Found {len(useful)} useful external item(s) for {topic}; top item: {top.get('title')}"
        return str(search_result.get("summary") or f"No useful external item found for {topic}.")

    def _merge_knowledge_items(self, existing: list[Any], refreshed: list[dict[str, Any]]) -> list[dict[str, Any]]:
        by_key: dict[str, dict[str, Any]] = {str(item.get("item_id")): item for item in existing if isinstance(item, dict) and item.get("item_id")}
        for refresh in refreshed:
            if refresh.get("kind") != "learning_search":
                continue
            for result in refresh.get("results", []) if isinstance(refresh.get("results"), list) else []:
                if not isinstance(result, dict) or not result.get("url"):
                    continue
                item_id = self._stable_id("knowledge", str(result.get("url")))
                by_key[item_id] = {
                    "item_id": item_id,
                    "goal_id": refresh.get("goal_id"),
                    "topic": refresh.get("topic"),
                    "title": result.get("title"),
                    "url": result.get("url"),
                    "source": result.get("source"),
                    "snippet": result.get("snippet"),
                    "score": result.get("score"),
                    "status": "fresh",
                    "observed_at": refresh.get("observed_at") or utc_now_iso(),
                }
        return list(by_key.values())[-200:]

    def _merge_push_candidates(self, existing: list[Any], refreshed: list[dict[str, Any]]) -> list[dict[str, Any]]:
        by_key: dict[str, dict[str, Any]] = {str(item.get("candidate_id")): item for item in existing if isinstance(item, dict) and item.get("candidate_id")}
        for refresh in refreshed:
            if refresh.get("kind") != "learning_search" or not refresh.get("commitment_id"):
                continue
            for result in refresh.get("results", []) if isinstance(refresh.get("results"), list) else []:
                if not isinstance(result, dict) or float(result.get("score") or 0) < 0.6:
                    continue
                candidate_id = self._stable_id("push", f"{refresh.get('commitment_id')}:{result.get('url')}")
                current = by_key.get(candidate_id, {})
                by_key[candidate_id] = {
                    **current,
                    "candidate_id": candidate_id,
                    "commitment_id": refresh.get("commitment_id"),
                    "goal_id": refresh.get("goal_id"),
                    "topic": refresh.get("topic"),
                    "title": result.get("title"),
                    "url": result.get("url"),
                    "snippet": result.get("snippet"),
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

    def _host(self, url: str) -> str:
        return urlparse(url).netloc.lower()
