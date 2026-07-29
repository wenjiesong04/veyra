from __future__ import annotations

from typing import Any

from awareness.belief_core import BeliefCore
from awareness.uncertainty_core import UncertaintyCore
from core.context_scope import visible_probe_map
from core.model_client import redact_sensitive
from core.world_state import WorldStateStore


class AwarenessContextAssembler:
    """Assemble Veyra's existing perception/world-state into compact cognition packets.

    This is the deterministic "Observe" layer before model reasoning - similar to how
    agent runtimes inject retrieved memory and tool state, but grounded in Veyra's
    belief claims, local_world probe cache, and attention focus.
    """

    def __init__(self, state_store: WorldStateStore) -> None:
        self.state_store = state_store
        self.belief = BeliefCore(state_store)
        self.uncertainty = UncertaintyCore()

    def snapshot(
        self,
        *,
        user_message: str,
        attention_focus: list[str],
        evidence_kind: str = "",
        user_id: str = "",
        session_id: str = "",
    ) -> dict[str, Any]:
        visible_claims = self.belief.relevant_claims(
            [],
            limit=250,
            user_id=user_id,
            session_id=session_id,
        )
        relevant = self.belief.relevant_claims(
            attention_focus,
            limit=12,
            user_id=user_id,
            session_id=session_id,
        )
        fresh = [self._claim_brief(c) for c in relevant if str(c.get("status") or "fresh") in {"fresh", "conflict"}]
        stale = [self._claim_brief(c) for c in relevant if str(c.get("status") or "") in {"stale", "expired"} or c.get("next_action")]
        local_world = self.state_store.read_json("local_world.json")
        probe_cache = self._probe_cache(
            local_world,
            attention_focus=attention_focus,
            evidence_kind=evidence_kind,
            user_id=user_id,
            session_id=session_id,
        )
        belief_summary = self.belief.summary_from_claims(visible_claims)
        return redact_sensitive(
            {
                "attention_focus": attention_focus[:8],
                "belief_summary": {
                    "total": belief_summary.get("total"),
                    "fresh": belief_summary.get("fresh"),
                    "stale": belief_summary.get("stale"),
                    "conflict": belief_summary.get("conflict"),
                    "refreshable": belief_summary.get("refreshable"),
                },
                "relevant_fresh_claims": fresh[:6],
                "relevant_stale_claims": stale[:4],
                "uncertainty": self.uncertainty.uncertainty_summary(relevant),
                "local_probe_cache": probe_cache,
                "world_freshness": self._world_freshness(probe_cache, fresh, stale, evidence_kind=evidence_kind),
            },
            max_string=480,
            max_list=8,
        )

    def expand(
        self,
        *,
        retrieval_hints: list[str],
        attention_focus: list[str],
        turn_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        hints = {str(item).strip().lower() for item in retrieval_hints if str(item).strip()}
        expanded: dict[str, Any] = {"retrieval_hints": sorted(hints)}
        ctx = turn_context if isinstance(turn_context, dict) else {}
        if not hints or "conversation" in hints or "conversation_tail" in hints:
            expanded["conversation_tail"] = (ctx.get("short_memory") or {}).get("conversation_tail")
        if not hints or "belief" in hints or "belief_claims" in hints:
            expanded["belief"] = ctx.get("belief")
            expanded["stale_beliefs"] = ctx.get("stale_beliefs")
        if not hints or "user" in hints or "user_preferences" in hints:
            expanded["user"] = (ctx.get("short_memory") or {}).get("user")
        if not hints or "task" in hints or "task_state" in hints:
            expanded["task"] = (ctx.get("short_memory") or {}).get("task")
        if not hints or "capabilities" in hints:
            expanded["available_capabilities"] = ctx.get("available_capabilities")
        if not hints or "runtime" in hints or "executor" in hints:
            expanded["runtime_summary"] = ctx.get("runtime_summary")
        return redact_sensitive(expanded, max_string=520, max_list=8)

    def evidence_sufficient(
        self,
        *,
        understanding_entities: dict[str, Any],
        evidence_kind: str,
        snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        fresh = snapshot.get("relevant_fresh_claims") if isinstance(snapshot.get("relevant_fresh_claims"), list) else []
        probe_cache = snapshot.get("local_probe_cache") if isinstance(snapshot.get("local_probe_cache"), list) else []
        entities = understanding_entities if isinstance(understanding_entities, dict) else {}
        markers = self._entity_markers(entities, evidence_kind)
        if not markers:
            return {"sufficient": False, "reason": "no_entity_markers"}

        for claim in fresh:
            if self._matches_markers(claim, markers):
                return {
                    "sufficient": True,
                    "reason": "fresh_belief_claim",
                    "source": "belief",
                    "claim_key": claim.get("key"),
                    "claim": claim.get("claim"),
                }
        for probe in probe_cache:
            if str(probe.get("status") or "") != "ok":
                continue
            if self._matches_markers(probe, markers):
                return {
                    "sufficient": True,
                    "reason": "fresh_local_probe_cache",
                    "source": "local_world",
                    "probe": probe.get("probe"),
                    "summary": probe.get("summary"),
                }
        stale = snapshot.get("relevant_stale_claims") if isinstance(snapshot.get("relevant_stale_claims"), list) else []
        if stale:
            return {"sufficient": False, "reason": "stale_evidence_requires_refresh", "stale_count": len(stale)}
        return {"sufficient": False, "reason": "missing_evidence"}

    def _probe_cache(
        self,
        local_world: dict[str, Any],
        *,
        attention_focus: list[str],
        evidence_kind: str,
        user_id: str,
        session_id: str,
    ) -> list[dict[str, Any]]:
        probes = visible_probe_map(
            local_world,
            user_id=user_id,
            session_id=session_id,
        )
        items: list[dict[str, Any]] = []
        for name, payload in probes.items():
            if not isinstance(payload, dict):
                continue
            summary = {
                "probe": str(name),
                "status": payload.get("status"),
                "summary": payload.get("summary"),
                "target": payload.get("target"),
                "observed_at": payload.get("observed_at") or payload.get("timestamp"),
                "confidence": payload.get("confidence"),
            }
            haystack = " ".join(str(part or "") for part in (name, summary["summary"], summary["target"])).lower()
            if evidence_kind and evidence_kind.replace("_probe", "") in haystack:
                items.append(summary)
                continue
            if any(focus.lower() in haystack for focus in attention_focus if focus):
                items.append(summary)
                continue
            if not attention_focus and not evidence_kind:
                items.append(summary)
        return items[-8:]

    def _world_freshness(
        self,
        probe_cache: list[dict[str, Any]],
        fresh: list[dict[str, Any]],
        stale: list[dict[str, Any]],
        *,
        evidence_kind: str,
    ) -> dict[str, Any]:
        has_fresh = bool(fresh) or any(str(item.get("status") or "") == "ok" for item in probe_cache)
        return {
            "has_fresh_relevant_claims": bool(fresh),
            "has_stale_relevant_claims": bool(stale),
            "has_cached_probe_ok": any(str(item.get("status") or "") == "ok" for item in probe_cache),
            "evidence_kind_hint": evidence_kind or "unknown",
            "recommendation": (
                "answer_from_world_state"
                if has_fresh and not stale
                else "refresh_probe"
                if stale or evidence_kind
                else "direct_or_clarify"
            ),
        }

    def _entity_markers(self, entities: dict[str, Any], evidence_kind: str) -> list[str]:
        markers: list[str] = []
        for key in ("location", "place", "city", "query", "person", "topic", "url", "port", "platform"):
            value = entities.get(key)
            if value:
                markers.append(str(value).lower())
        if evidence_kind:
            markers.append(evidence_kind.lower().replace("_probe", ""))
        return [item for item in markers if len(item) >= 2]

    def _matches_markers(self, item: dict[str, Any], markers: list[str]) -> bool:
        haystack = " ".join(
            str(part or "").lower()
            for part in (item.get("key"), item.get("claim"), item.get("summary"), item.get("target"), item.get("probe"))
        )
        return any(marker in haystack for marker in markers)

    def _claim_brief(self, claim: dict[str, Any]) -> dict[str, Any]:
        return {
            "key": claim.get("key"),
            "claim": claim.get("claim"),
            "status": claim.get("status"),
            "source": claim.get("source"),
            "confidence": claim.get("confidence"),
            "observed_at": claim.get("observed_at") or claim.get("updated_at"),
            "next_action": claim.get("next_action"),
        }
