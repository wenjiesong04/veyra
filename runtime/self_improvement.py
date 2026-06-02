from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from core.proactive_intent import ProactiveIntent
from core.world_state import WorldStateStore
from interface.event_schema import utc_now_iso


class SelfImprovementProposalRegistry:
    """Records runtime-discovered capability gaps without changing code."""

    STATE_FILE = "self_improvement_proposals.json"

    def __init__(self, state_store: WorldStateStore) -> None:
        self.state_store = state_store

    def propose_from_intent(self, intent: ProactiveIntent, *, reason: str = "unknown_proactive_intent") -> dict[str, Any]:
        proposal_id = f"sip_{uuid4().hex[:12]}"
        gap = self._gap_description(intent, reason)
        proposal = {
            "proposal_id": proposal_id,
            "status": "draft",
            "created_at": utc_now_iso(),
            "source": "veyra_runtime",
            "created_from_intent_id": intent.intent_id,
            "gap_description": gap,
            "suggested_files_to_change": self._suggested_files(intent),
            "suggested_new_template": self._suggested_template(intent),
            "suggested_tests": [
                "scripts/proactive_generalization_acceptance.py",
                "scripts/proactive_closed_loop_acceptance.py",
                "scripts/commitment_smoke.py",
            ],
            "risk_level": "R2",
            "requires_human_review": True,
            "patch_draft": {
                "status": "not_generated",
                "reason": "runtime may record proposals but must not mutate source code",
            },
            "intent": intent.to_dict(),
        }
        self._record(proposal)
        return proposal

    def _record(self, proposal: dict[str, Any]) -> None:
        state = self.state_store.read_json(self.STATE_FILE)
        proposals = state.setdefault("proposals", [])
        if not isinstance(proposals, list):
            proposals = []
        proposals.append(proposal)
        state["proposals"] = proposals[-200:]
        state["updated_at"] = utc_now_iso()
        self.state_store.write_json(self.STATE_FILE, state)
        self.state_store.write_text(
            f"proposals/{proposal['proposal_id']}.json",
            json.dumps(proposal, ensure_ascii=False, indent=2),
        )
        self.state_store.append_jsonl(
            "action_record.jsonl",
            {
                "route": "self_improvement_proposal",
                "status": "draft",
                "artifacts": {
                    "proposal_id": proposal.get("proposal_id"),
                    "intent_id": proposal.get("created_from_intent_id"),
                    "gap_description": proposal.get("gap_description"),
                },
            },
        )

    def _gap_description(self, intent: ProactiveIntent, reason: str) -> str:
        topic = intent.topic or "unknown topic"
        gaps = ", ".join(intent.external_context_needed or intent.local_context_needed or ["template/source/cadence"])
        return f"{reason}: Veyra could not safely map proactive request for {topic}; missing {gaps}."

    def _suggested_files(self, intent: ProactiveIntent) -> list[str]:
        files = [
            "core/proactive_intent_planner.py",
            "core/proactive_templates.py",
            "scripts/proactive_generalization_acceptance.py",
        ]
        if intent.local_context_needed:
            files.append("runtime/commitment_push.py")
        if intent.external_context_needed:
            files.append("runtime/external_world_refresh.py")
        return files

    def _suggested_template(self, intent: ProactiveIntent) -> dict[str, Any]:
        return {
            "intent_type": intent.intent_type,
            "topic": intent.topic,
            "required_context": intent.local_context_needed + intent.external_context_needed,
            "authorization": "required before commitment/watchlist activation",
            "execution_boundary": "template may draft goals/commitments/watchlists only; source code changes require human review",
        }
