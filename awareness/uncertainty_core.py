from __future__ import annotations

from typing import Any

from awareness.claim_schema import refresh_claim_status


class UncertaintyCore:
    def mark_stale(self, claim: dict[str, Any]) -> dict[str, Any]:
        updated = dict(claim)
        updated["status"] = "stale"
        updated["next_action"] = "refresh_probe"
        return updated

    def refresh_claims(self, claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [refresh_claim_status(claim) for claim in claims]

    def uncertainty_summary(self, claims: list[dict[str, Any]]) -> dict[str, Any]:
        stale = [claim for claim in claims if claim.get("status") == "stale"]
        conflict = [claim for claim in claims if claim.get("status") == "conflict"]
        low_confidence = [claim for claim in claims if float(claim.get("confidence") or 0) < 0.6]
        return {
            "stale_count": len(stale),
            "conflict_count": len(conflict),
            "low_confidence_count": len(low_confidence),
            "next_actions": sorted(
                {
                    str(claim.get("next_action"))
                    for claim in stale + conflict + low_confidence
                    if claim.get("next_action")
                }
            ),
        }
