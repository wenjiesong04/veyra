#!/usr/bin/env python3
"""Focused backend smoke for quiet policy, feedback fencing, and chat replay."""

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
from interface.product_living_context import ReactionFeedbackRequest  # noqa: E402
from runtime.living_context_composition import build_living_context_composition  # noqa: E402
from runtime.living_context_orchestrator import LivingContextOrchestrator  # noqa: E402
from runtime.living_reaction_runtime import LivingReactionRuntime  # noqa: E402
from runtime.product_experience import ProductExperienceService  # noqa: E402
from runtime.product_conversation_runtime import ProductConversationRuntime  # noqa: E402
from runtime.suggestion_outbox import SuggestionOutbox  # noqa: E402


class Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value.astimezone(timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: int) -> None:
        self.value += timedelta(**kwargs)


class CoreStub:
    def __init__(self, store: WorldStateStore, row: dict[str, Any]) -> None:
        self.state_store = store
        self.row = row

    def get_situation(self, situation_id: str, *, owner_id: str, session_id: str) -> dict[str, Any] | None:
        if (
            situation_id == self.row["situation_id"]
            and owner_id == self.row["user_id"]
            and session_id == self.row["session_id"]
        ):
            return copy.deepcopy(self.row)
        return None

    def command_situation(self, _event: Any, situation_id: str, *, owner_id: str, session_id: str, command: str, expected_revision: int, reason: str) -> dict[str, Any]:
        if command != "resolve" or expected_revision != self.row["observation_revision"]:
            raise RuntimeError("unexpected command")
        self.row["status"] = "resolved"
        self.row["observation_revision"] += 1
        self.row["semantic"] = {**self.row["semantic"], "lifecycle": "resolved"}
        return {"status": "resolved", "situation": copy.deepcopy(self.row), "reason": reason}


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"PASS {label}")


def main() -> int:
    now = datetime(2026, 8, 23, 23, 0, tzinfo=timezone.utc)
    owner = "backend-owner"
    session = "backend-session"
    with TemporaryDirectory(prefix="veyra-living-reaction-backend-") as directory:
        store = WorldStateStore(Path(directory) / "state")
        clock = Clock(now)

        outbox = SuggestionOutbox(store, clock=clock)
        expect(not outbox.in_quiet_hours(owner, session), "missing quiet policy is not quiet")
        revision = int(store.read_json("suggestion_outbox.json").get("_state_revision") or 0)
        outbox.configure_policy(
            user_id=owner,
            session_id=session,
            sandbox_enabled=True,
            daily_budget=1,
            quiet_hours={"start_hour": 22, "end_hour": 7, "timezone": "UTC"},
            cooldown_seconds=3600,
            dismiss_cooldown_seconds=86400,
            expected_state_revision=revision,
        )
        expect(outbox.in_quiet_hours(owner, session), "configured quiet policy is read by exact owner/session")
        expect(not outbox.in_quiet_hours(owner, "other-session"), "quiet policy does not cross session scope")

        semantic = {
            "lifecycle": "active",
            "title": "Team event",
            "summary": "A durable Situation",
            "goal": "Keep the next step clear",
            "category": "calendar",
            "known": [{"statement": "The user reported the event", "epistemic_status": "reported"}],
            "unknown": [],
            "material_change": {"statement": "A candidate signal changed", "epistemic_status": "hypothesis"},
        }
        row = {
            "situation_id": "sit_backend",
            "user_id": owner,
            "session_id": session,
            "observation_revision": 1,
            "status": "active",
            "semantic": semantic,
        }
        core = CoreStub(store, row)
        reactions = LivingReactionRuntime(store, clock=clock)
        conversations = ProductConversationRuntime(store, clock=clock)
        orchestrator = LivingContextOrchestrator(
            core,
            reactions,
            object(),
            clock=clock,
            conversation_runtime=conversations,
        )
        reaction = reactions.evaluate(
            {
                "owner_id": owner,
                "session_id": session,
                "now": clock().isoformat(),
                "quiet_hours": False,
                "consent": {},
                "source_availability": {},
                "situation": row,
                "information_need": None,
            }
        )
        reaction_id = reaction["decision"]["reaction_id"]
        feedback_reaction = reactions.evaluate(
            {
                "owner_id": owner,
                "session_id": session,
                "now": clock().isoformat(),
                "quiet_hours": False,
                "consent": {},
                "source_availability": {},
                "situation": {
                    **row,
                    "material_change": {
                        "statement": "A separate feedback target signal was recorded",
                        "revision": 2,
                    },
                },
                "information_need": None,
            }
        )
        feedback_reaction_id = feedback_reaction["decision"]["reaction_id"]
        proactive = conversations.record_reaction(
            {**feedback_reaction["decision"], "reaction_token": "rxn_feedback"}
        )
        expect(proactive["status"] == "recorded", "Product Conversation records the feedback target")
        reaction_key = conversations.reaction_key(
            owner_id=owner,
            session_id=session,
            reaction_id=feedback_reaction_id,
        )
        indexed_message_id = store.read_json(ProductConversationRuntime.STATE_FILE)["reaction_index"].get(reaction_key)
        expect(indexed_message_id == proactive["message"]["message_id"], "reaction index stores the message identity")
        legacy_state = store.read_json(ProductConversationRuntime.STATE_FILE)
        legacy_state["message_index"].pop(
            conversations.message_key(
                owner_id=owner,
                session_id=session,
                message_id=str(indexed_message_id),
            ),
            None,
        )
        store.write_json(ProductConversationRuntime.STATE_FILE, legacy_state)
        service = ProductExperienceService(
            store,
            living_context_runtime=core,
            reaction_runtime=reactions,
            living_context_orchestrator=orchestrator,
        )
        feedback = service.feedback_reaction(
            feedback_reaction_id,
            user_id=owner,
            session_id=session,
            request=ReactionFeedbackRequest(
                label="resolved",
                situation_revision=1,
                category="forged-client-category",
            ),
        )
        semantics = feedback["feedback"]["semantics"]
        expect(semantics["category"] == "calendar", "feedback ignores forged client category")
        expect(feedback["situation"]["status"] == "resolved", "resolved feedback uses the Situation command path")
        expect(feedback["conversation"]["status"] == "recorded", "Product feedback closes the indexed proactive message")

        # A revision advanced after the reaction was issued must fail closed.
        row["status"] = "active"
        row["observation_revision"] += 1
        stale_reaction = reactions.evaluate(
            {
                "owner_id": owner,
                "session_id": session,
                "now": clock().isoformat(),
                "quiet_hours": False,
                "consent": {},
                "source_availability": {},
                "situation": {**row, "status": "active"},
                "information_need": None,
            }
        )
        row["observation_revision"] += 1
        stale_service = ProductExperienceService(
            store,
            living_context_runtime=core,
            reaction_runtime=reactions,
            living_context_orchestrator=orchestrator,
        )
        try:
            stale_service.feedback_reaction(
                stale_reaction["decision"]["reaction_id"],
                user_id=owner,
                session_id=session,
                request=ReactionFeedbackRequest(label="useful", situation_revision=2),
            )
        except Exception as exc:
            expect(type(exc).__name__ == "StateRevisionConflictError", "stale feedback fails closed")
        else:
            raise AssertionError("stale feedback unexpectedly recorded")

        suggestion = {
            **reaction["decision"],
            "disposition": "suggest",
            "reaction_token": "rxn_backend",
        }
        first = conversations.record_reaction(suggestion)
        replay = conversations.record_reaction(suggestion)
        expect(first["status"] == "recorded" and replay["status"] == "duplicate", "duplicate reaction replay repairs chat idempotently")
        expect(first["conversation"]["message_count"] == replay["conversation"]["message_count"], "chat replay does not duplicate the message")

        # Composition defaults to no quiet resolver; the application supplies
        # SuggestionOutbox.in_quiet_hours explicitly in main.py.
        composition = build_living_context_composition(store, clock=clock)
        expect(composition.orchestrator._quiet_hours_resolver is None, "composition has no implicit quiet policy")

    print("RESULT living reaction backend smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
