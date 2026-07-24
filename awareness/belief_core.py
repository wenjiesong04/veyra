from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from awareness.claim_schema import detect_conflicts, make_claim, refresh_claim_status
from core.world_state import WorldStateStore
from interface.event_schema import VeyraEvent


class BeliefCore:
    def __init__(self, state_store: WorldStateStore) -> None:
        self.state_store = state_store

    def update_from_event(self, event: VeyraEvent) -> None:
        self.upsert_claim(
            make_claim(
                key=f"event:{event.event_id}",
                claim=f"received {event.type.value} from {event.source.channel}",
                confidence=1.0,
                source="event",
                ttl_seconds=300,
                evidence={"event_id": event.event_id, "channel": event.source.channel},
            )
        )

    def upsert_claim(self, claim: dict[str, Any]) -> dict[str, Any]:
        claim = refresh_claim_status(claim)

        def update_belief(belief: dict[str, Any]) -> dict[str, Any]:
            claims = belief.setdefault("claims", [])
            if not isinstance(claims, list):
                claims = []
            key = str(claim.get("key") or claim.get("claim"))
            for index, current in enumerate(claims):
                if not isinstance(current, dict) or str(current.get("key") or current.get("claim")) != key:
                    continue
                history = current.get("history") if isinstance(current.get("history"), list) else []
                history.append(
                    {
                        "updated_at": current.get("updated_at") or current.get("observed_at"),
                        "status": current.get("status"),
                        "confidence": current.get("confidence"),
                    }
                )
                claims[index] = {
                    **current,
                    **claim,
                    "history": history[-12:],
                    "refresh_count": int(current.get("refresh_count") or 0) + 1,
                }
                break
            else:
                claim.setdefault("refresh_count", 0)
                claims.append(claim)
            belief["claims"] = detect_conflicts(
                [refresh_claim_status(item) for item in claims if isinstance(item, dict)]
            )[-250:]
            belief["summary"] = self.summary_from_claims(belief["claims"])
            return belief

        self.state_store.mutate_json("belief_state.json", update_belief)
        return claim

    def upsert_claims(self, claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [self.upsert_claim(claim) for claim in claims]

    def refresh(self, *, expire_after_seconds: int | None = 3600, prune_expired_after_seconds: int | None = None) -> dict[str, Any]:
        def refresh_belief(belief: dict[str, Any]) -> dict[str, Any]:
            raw_claims = belief.get("claims") if isinstance(belief.get("claims"), list) else []
            claims = [
                refresh_claim_status(item, expire_after_seconds=expire_after_seconds)
                for item in raw_claims
                if isinstance(item, dict)
            ]
            belief["claims"] = detect_conflicts(claims)
            if prune_expired_after_seconds is not None:
                belief["claims"] = self._prune_expired(belief["claims"], prune_expired_after_seconds)
            belief["summary"] = self.summary_from_claims(belief["claims"])
            return belief

        return self.state_store.mutate_json("belief_state.json", refresh_belief)

    def relevant_claims(self, focus: list[str], limit: int = 30, *, include_stale: bool = True) -> list[dict[str, Any]]:
        belief = self.refresh()
        claims = belief.get("claims", [])
        if not include_stale:
            claims = [claim for claim in claims if str(claim.get("status") or "fresh") in {"fresh", "conflict"}]
        if not focus:
            return self._rank_claims(claims)[-limit:]
        focused: list[dict[str, Any]] = []
        for claim in claims:
            haystack = " ".join(
                [
                    str(claim.get("key", "")),
                    str(claim.get("claim", "")),
                    str(claim.get("source", "")),
                ]
            )
            if any(item in haystack for item in focus):
                focused.append(claim)
        ranked = self._rank_claims(focused if focused else claims)
        return ranked[-limit:]

    def ttl_report(self, limit: int = 50) -> dict[str, Any]:
        belief = self.refresh()
        claims = self._rank_claims(belief.get("claims", []))
        refreshable = [claim for claim in claims if claim.get("next_action") == "refresh_probe"]
        return {
            "status": "success",
            "summary": belief.get("summary", {}),
            "refreshable": refreshable[-limit:],
            "oldest": claims[:limit],
            "newest": claims[-limit:],
        }

    def stale_report(self, limit: int = 50) -> dict[str, Any]:
        belief = self.refresh()
        stale = [
            claim
            for claim in self._rank_claims(belief.get("claims", []))
            if str(claim.get("status") or "") in {"stale", "expired", "conflict"} or claim.get("next_action") == "refresh_probe"
        ]
        return {
            "status": "success",
            "summary": belief.get("summary", {}),
            "items": stale[-limit:],
            "next_action": "refresh_probe" if stale else None,
        }

    def summary_from_claims(self, claims: list[dict[str, Any]]) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "fresh": 0,
            "stale": 0,
            "expired": 0,
            "conflict": 0,
            "total": len(claims),
            "refreshable": 0,
            "by_source": {},
            "average_confidence": 0.0,
            "average_source_trust": 0.0,
        }
        confidence_total = 0.0
        trust_total = 0.0
        for claim in claims:
            status = str(claim.get("status") or "fresh")
            if status in summary:
                summary[status] += 1
            if claim.get("next_action") == "refresh_probe":
                summary["refreshable"] += 1
            source = str(claim.get("source") or "unknown")
            by_source = summary["by_source"]
            by_source[source] = by_source.get(source, 0) + 1
            confidence_total += float(claim.get("confidence") or 0.0)
            trust_total += float(claim.get("source_trust") or 0.0)
        if claims:
            summary["average_confidence"] = round(confidence_total / len(claims), 3)
            summary["average_source_trust"] = round(trust_total / len(claims), 3)
        return summary

    def _rank_claims(self, claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
        def key(claim: dict[str, Any]) -> tuple[int, float, str]:
            status_order = {"expired": 0, "stale": 1, "conflict": 2, "fresh": 3}
            status = status_order.get(str(claim.get("status") or "fresh"), 1)
            confidence = float(claim.get("confidence") or 0.0) * float(claim.get("source_trust") or 0.5)
            return (status, confidence, str(claim.get("updated_at") or claim.get("observed_at") or ""))

        return sorted([claim for claim in claims if isinstance(claim, dict)], key=key)

    def _prune_expired(self, claims: list[dict[str, Any]], threshold_seconds: int) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        kept: list[dict[str, Any]] = []
        for claim in claims:
            if claim.get("status") != "expired":
                kept.append(claim)
                continue
            stale_since = str(claim.get("stale_since") or claim.get("expires_at") or "")
            try:
                parsed = datetime.fromisoformat(stale_since.replace("Z", "+00:00"))
            except ValueError:
                kept.append(claim)
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            if (now - parsed).total_seconds() <= threshold_seconds:
                kept.append(claim)
        return kept
