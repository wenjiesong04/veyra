from __future__ import annotations

from typing import Any

from core.model_client import redact_sensitive
from core.perception_layer import PerceptionLayer
from core.reasoning_core import CoreReasoning
from core.world_state import WorldStateStore
from interface.event_schema import utc_now_iso
from probes.network_probe import NetworkProbe
from probes.web_probe import WebProbe


class ExternalWorldRefresh:
    """Refreshes ExternalWorld watchlist entries with read-only probes."""

    def __init__(self, state_store: WorldStateStore, reasoning: CoreReasoning | None = None) -> None:
        self.state_store = state_store
        self.reasoning = reasoning or CoreReasoning(state_store)
        self.perception = PerceptionLayer(state_store, reasoning=self.reasoning)

    def refresh_watchlist(self, limit: int = 10) -> dict[str, Any]:
        external = self.state_store.read_json("external_world.json")
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
        external["last_refresh_at"] = utc_now_iso()
        self.state_store.write_json("external_world.json", external)
        return {"status": "success", "refreshed": refreshed, "skipped": skipped, "summary_count": len(external["summaries"])}

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
