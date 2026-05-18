from __future__ import annotations

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
        belief = self.state_store.read_json("belief_state.json")
        claims = belief.setdefault("claims", [])
        claim = refresh_claim_status(claim)
        key = str(claim.get("key") or claim.get("claim"))
        for index, current in enumerate(claims):
            if str(current.get("key") or current.get("claim")) == key:
                claims[index] = {**current, **claim}
                break
        else:
            claims.append(claim)
        belief["claims"] = detect_conflicts([refresh_claim_status(item) for item in claims])[-250:]
        belief["summary"] = self.summary_from_claims(belief["claims"])
        self.state_store.write_json("belief_state.json", belief)
        return claim

    def upsert_claims(self, claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [self.upsert_claim(claim) for claim in claims]

    def refresh(self) -> dict[str, Any]:
        belief = self.state_store.read_json("belief_state.json")
        claims = [refresh_claim_status(item) for item in belief.get("claims", [])]
        belief["claims"] = detect_conflicts(claims)
        belief["summary"] = self.summary_from_claims(belief["claims"])
        self.state_store.write_json("belief_state.json", belief)
        return belief

    def relevant_claims(self, focus: list[str], limit: int = 30) -> list[dict[str, Any]]:
        belief = self.refresh()
        claims = belief.get("claims", [])
        if not focus:
            return claims[-limit:]
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
        return focused[-limit:] if focused else claims[-limit:]

    def summary_from_claims(self, claims: list[dict[str, Any]]) -> dict[str, int]:
        summary = {"fresh": 0, "stale": 0, "conflict": 0, "total": len(claims)}
        for claim in claims:
            status = str(claim.get("status") or "fresh")
            if status in summary:
                summary[status] += 1
        return summary
