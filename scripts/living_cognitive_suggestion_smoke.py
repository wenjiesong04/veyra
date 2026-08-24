#!/usr/bin/env python3
"""Focused acceptance for cognitive-hypothesis -> Living Reaction delivery."""

from __future__ import annotations

import copy
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.world_state import WorldStateStore  # noqa: E402
from interface.living_reaction_contract import stable_digest  # noqa: E402
from runtime.living_context_admission import LivingContextAdmissionLedger  # noqa: E402
from runtime.living_context_orchestrator import LivingContextOrchestrator  # noqa: E402
from runtime.living_reaction_runtime import LivingReactionRuntime  # noqa: E402
from runtime.product_conversation_runtime import ProductConversationRuntime  # noqa: E402


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value.astimezone(timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: int) -> None:
        self.value += timedelta(**kwargs)


class CoreStub:
    def __init__(self, row: dict[str, Any], store: WorldStateStore) -> None:
        self.row = row
        self.state_store = store
        self.admission = LivingContextAdmissionLedger(store)
        self.advance_after_first_read = False
        self._read_count = 0

    def get_situation(self, situation_id: str, *, owner_id: str, session_id: str) -> dict[str, Any] | None:
        self._read_count += 1
        if (
            situation_id == self.row["situation_id"]
            and owner_id == self.row["user_id"]
            and session_id == self.row["session_id"]
        ):
            value = copy.deepcopy(self.row)
            if self.advance_after_first_read and self._read_count == 1:
                self.row["observation_revision"] = int(self.row["observation_revision"]) + 1
            return value
        return None


class SourceStub:
    def status(self, **_: Any) -> dict[str, Any]:
        return {"capabilities": {}, "consent": {}}


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"PASS {label}")


def candidate(
    *,
    digest: str,
    candidate_id: str,
    material_digest: str,
    owner: str = "cognitive-owner",
    session: str = "cognitive-session",
    revision: int = 1,
    material_revision: int = 1,
) -> dict[str, Any]:
    change_token = "lcchg_" + stable_digest(
        "veyra.cognitive_living_context.change_token.v1",
        {
            "owner_id": owner,
            "session_id": session,
            "situation_id": "sit_cognitive",
            "material_revision": material_revision,
            "material_digest": material_digest,
        },
    )[:32]
    return {
        "schema_version": "veyra.cognitive_suggestion_candidate.v1",
        "candidate_id": candidate_id,
        "owner_id": owner,
        "session_id": session,
        "situation_id": "sit_cognitive",
        "situation_revision": revision,
        "change_token": change_token,
        "cycle_id": "cog_0000000000000001",
        "semantic_digest": digest,
        "material_revision": material_revision,
        "material_digest": material_digest,
        "statement": f"A hypothesis from {candidate_id} may affect the next step.",
        "why_now": "The background view changed after the last observation.",
        "suggested_next_step": "Review the smallest useful next step.",
        "evidence_refs": [f"view:{candidate_id}"],
        "confidence": 0.8,
        "epistemic_status": "hypothesis",
        "is_fact": False,
        "authority": False,
    }


def build() -> tuple[LivingContextOrchestrator, CoreStub, MutableClock, ProductConversationRuntime, dict[str, Any]]:
    now = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
    store = WorldStateStore(Path(temp_dir) / "state")
    clock = MutableClock(now)
    semantic = {
        "lifecycle": "active",
        "title": "Cognitive Situation",
        "label": "Cognitive Situation",
        "summary": "A durable Situation for a background hypothesis.",
        "goal": "Keep the next useful step clear.",
        "category": "cognitive",
        "known": [
            {
                "statement": "An older server Known is retained for context.",
                "epistemic_status": "reported",
                "recorded_at": "2026-08-23T11:00:00+00:00",
            },
            {
                "statement": "The latest server Known is current.",
                "epistemic_status": "reported",
                "recorded_at": "2026-08-23T12:00:00+00:00",
            },
        ],
        "unknown": [],
    }
    material_digest = stable_digest(
        "veyra.cognitive_living_context.material.v1",
        {"checkpoint": "cognitive-smoke", "revision": 1},
    )
    semantic["material_revision"] = 1
    semantic["material_digest"] = material_digest
    row = {
        "record_kind": "semantic_situation",
        "situation_id": "sit_cognitive",
        "user_id": "cognitive-owner",
        "session_id": "cognitive-session",
        "observation_revision": 1,
        "status": "active",
        "semantic": semantic,
    }
    core = CoreStub(row, store)
    conversations = ProductConversationRuntime(store, clock=clock)
    orchestrator = LivingContextOrchestrator(
        core,
        LivingReactionRuntime(store, clock=clock),
        SourceStub(),
        clock=clock,
        conversation_runtime=conversations,
    )
    digest = stable_digest("veyra.cognitive_living_context.semantic.v1", semantic)
    return orchestrator, core, clock, conversations, candidate(
        digest=digest,
        candidate_id="candidate-1",
        material_revision=1,
        material_digest=material_digest,
    )


def main() -> int:
    global temp_dir
    with TemporaryDirectory(prefix="veyra-cognitive-suggestion-") as directory:
        temp_dir = Path(directory)
        orchestrator, core, clock, conversations, first = build()
        recorded = orchestrator.record_cognitive_suggestion(first)
        expect(recorded["status"] == "recorded", "first cognitive candidate records")
        decision = recorded["reaction"]["decision"]
        expect(decision["disposition"] == "suggest", "cognitive candidate produces suggest")
        expect(decision["attention_trigger"] == "cognitive_hypothesis", "cognitive trigger is explicit")
        expect(decision["fact_vs_inference"]["inferences"], "hypothesis is projected as inference")
        expect(
            decision["fact_vs_inference"]["facts"][0] == "The latest server Known is current.",
            "candidate fact projection uses the latest server Known",
        )
        expect(not decision["fact_vs_inference"]["facts"] or all("hypothesis" not in value for value in decision["fact_vs_inference"]["facts"]), "hypothesis does not become fact")

        replay = orchestrator.record_cognitive_suggestion(first)
        expect(replay["status"] == "duplicate", "same candidate replay is idempotent")
        conversation = conversations.list_conversations(owner_id="cognitive-owner", session_id="cognitive-session")[0]
        expect(conversation["message_count"] == 1, "same Situation reuses one proactive conversation")
        message = conversations.get_conversation(conversation["conversation_id"], owner_id="cognitive-owner", session_id="cognitive-session")["messages"][0]
        expect(message["text"].startswith("可能的变化：") and "为什么重要：" in message["text"] and "为什么现在：" in message["text"] and "建议下一步：" in message["text"], "proactive text contains hypothesis explanation")
        expect(
            message["metadata"]["record_only"] is True
            and message["metadata"]["external_delivery"] is False
            and message["metadata"]["feedback_available"] is True
            and message["metadata"]["attention_candidate_id"] == "candidate-1"
            and message["metadata"]["reaction_id"],
            "conversation metadata keeps candidate/reaction record-only boundary",
        )

        second = copy.deepcopy(first)
        second["candidate_id"] = "candidate-2"
        second["statement"] = "A different hypothesis arrived."
        suppressed = orchestrator.record_cognitive_suggestion(second)
        expect(suppressed["status"] == "suppressed" and suppressed["reason"] == "cognitive_hypothesis_cooldown", "different candidate is cooled down")
        effects = orchestrator.reaction_runtime.read_state()["situation_effects"]
        effect = next(iter(effects.values()))
        expect(effect.get("next_allowed_at") == effect.get("cooldown_until"), "cognitive cooldown persists on Situation scope")

        # A stale revision/digest is rejected without changing the Situation.
        stale = copy.deepcopy(first)
        stale["candidate_id"] = "candidate-stale"
        stale["situation_revision"] = 2
        rejected = orchestrator.record_cognitive_suggestion(stale)
        expect(rejected["status"] == "rejected", "stale cognitive candidate is rejected")
        expect("material_change" not in core.row["semantic"], "ephemeral hypothesis is not written to Situation")

        # A Situation update between the first admission hint and the locked
        # re-read must reject the candidate before either reaction or chat.
        core.row["observation_revision"] = 1
        core._read_count = 0
        core.advance_after_first_read = True
        raced = copy.deepcopy(first)
        raced["candidate_id"] = "candidate-raced"
        raced_result = orchestrator.record_cognitive_suggestion(raced)
        expect(raced_result["status"] == "rejected" and raced_result["reason"] == "situation_revision_or_digest_mismatch", "concurrent Situation advancement is rejected")
        core.advance_after_first_read = False
        core.row["observation_revision"] = 1

        clock.advance(hours=2)
        third = copy.deepcopy(first)
        third["candidate_id"] = "candidate-3"
        after_cooldown = orchestrator.record_cognitive_suggestion(third)
        expect(after_cooldown["status"] == "recorded", "new candidate can speak after cooldown")
        expect(conversations.list_conversations(owner_id="cognitive-owner", session_id="cognitive-session")[0]["message_count"] == 2, "new candidate appends to same Situation conversation")

        # Chat failure must not lose the durable reaction.  A replay of the
        # same candidate then uses the reaction idempotency row and fills the
        # missing Product Conversation message exactly once.
        clock.advance(hours=2)
        failing = copy.deepcopy(first)
        failing["candidate_id"] = "candidate-chat-retry"
        original_record_reaction = conversations.record_reaction
        conversations.record_reaction = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("chat unavailable"))
        failed_delivery = orchestrator.record_cognitive_suggestion(failing)
        expect(
            failed_delivery["status"] == "degraded"
            and failed_delivery.get("retryable") is True
            and failed_delivery.get("reaction", {}).get("decision", {}).get("disposition") == "suggest",
            "chat failure is retryable after reaction recording",
        )
        conversations.record_reaction = original_record_reaction
        replayed_delivery = orchestrator.record_cognitive_suggestion(failing)
        expect(
            replayed_delivery["status"] == "duplicate"
            and replayed_delivery.get("conversation", {}).get("status") == "recorded",
            "candidate replay repairs missing chat idempotently",
        )
        expect(conversations.list_conversations(owner_id="cognitive-owner", session_id="cognitive-session")[0]["message_count"] == 3, "chat retry does not duplicate the conversation message")

        orchestrator._quiet_hours_resolver = lambda *_: True  # noqa: SLF001
        clock.advance(hours=2)
        quiet = copy.deepcopy(first)
        quiet["candidate_id"] = "candidate-quiet"
        quiet_result = orchestrator.record_cognitive_suggestion(quiet)
        expect(quiet_result["status"] == "suppressed" and quiet_result["reason"] == "quiet_hours", "quiet hours suppress cognitive suggestion")
        orchestrator._quiet_hours_resolver = None  # noqa: SLF001

        feedback = orchestrator.reaction_runtime.record_feedback(
            {
                "feedback_id": "feedback-cognitive-ignore",
                "owner_id": "cognitive-owner",
                "session_id": "cognitive-session",
                "situation_id": "sit_cognitive",
                "reaction_id": after_cooldown["reaction"]["decision"]["reaction_id"],
                "label": "ignore",
                "now": clock().isoformat(),
            }
        )
        expect(feedback["status"] == "recorded", "feedback records for cognitive suggestion")
        clock.advance(days=1)
        feedback_candidate = copy.deepcopy(first)
        feedback_candidate["candidate_id"] = "candidate-after-feedback"
        suppressed_by_feedback = orchestrator.record_cognitive_suggestion(feedback_candidate)
        expect(suppressed_by_feedback["status"] == "suppressed" and suppressed_by_feedback["reason"] == "ignore", "feedback suppression overrides later cognitive candidates")

    print("RESULT living cognitive suggestion smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
