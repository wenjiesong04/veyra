#!/usr/bin/env python3
"""Focused Product Conversation ledger and HTTP contract smoke."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.world_state import WorldStateStore  # noqa: E402
from routers.product_conversations import build_product_conversations_router  # noqa: E402
from runtime.product_conversation_runtime import (  # noqa: E402
    ProductConversationConflict,
    ProductConversationRevisionConflict,
    ProductConversationRuntime,
    ProductConversationStorageError,
)


def expect(condition: bool, label: str) -> None:
    if not condition:
        raise AssertionError(f"{label} failed")
    print(f"PASS {label}")


def files_snapshot(store: WorldStateStore) -> dict[str, bytes]:
    return {
        str(path.relative_to(store.root)): path.read_bytes()
        for path in store.root.rglob("*")
        if path.is_file() and path.name != ".veyra-writer.lock"
    }


def main() -> int:
    with TemporaryDirectory(prefix="veyra-product-conversation-") as temporary:
        store = WorldStateStore(Path(temporary) / "state")
        runtime = ProductConversationRuntime(store)
        app = FastAPI()
        app.include_router(build_product_conversations_router(runtime))
        client = TestClient(app)

        first = runtime.record_reaction(
            {
                "owner_id": "owner-a",
                "session_id": "session-a",
                "situation_id": "situation-1",
                "reaction_id": "reaction-1",
                "disposition": "suggest",
                "suggested_next_step": "Keep the next step visible.",
            }
        )
        first_conversation = first["conversation"]
        first_id = str(first_conversation["conversation_id"])
        expect(first["status"] == "recorded", "first proactive creates a conversation message")
        expect(first_conversation["binding_type"] == "situation", "proactive binding is typed")
        expect(len(first_conversation["messages"]) == 1, "first proactive has one assistant message")
        first_message = first_conversation["messages"][0]
        expect(
            first_message["metadata"]["record_only"] is True
            and first_message["metadata"]["external_delivery"] is False
            and first_message["metadata"]["reaction_id"] == "reaction-1"
            and first_message["metadata"]["feedback_available"] is True,
            "proactive metadata preserves record-only feedback boundary",
        )

        feedback_update = runtime.update_reaction_feedback(
            "reaction-1",
            owner_id="owner-a",
            session_id="session-a",
            label="useful",
            feedback_at="2026-08-24T00:00:00+00:00",
        )
        expect(
            feedback_update["status"] == "recorded"
            and feedback_update["message"]["metadata"]["feedback_available"] is False
            and feedback_update["message"]["metadata"]["feedback_label"] == "useful"
            and feedback_update["message"]["metadata"]["feedback_at"] == "2026-08-24T00:00:00+00:00",
            "feedback closes the proactive message affordance",
        )
        feedback_replay = runtime.update_reaction_feedback(
            "reaction-1",
            owner_id="owner-a",
            session_id="session-a",
            label="useful",
            feedback_at="2026-08-24T00:00:00+00:00",
        )
        expect(feedback_replay["status"] == "duplicate", "same feedback label is idempotent")
        try:
            runtime.update_reaction_feedback(
                "reaction-1",
                owner_id="owner-a",
                session_id="session-a",
                label="not_useful",
            )
        except ProductConversationConflict:
            print("PASS feedback label cannot be replaced")
        else:
            raise AssertionError("feedback label replacement unexpectedly succeeded")

        second = runtime.record_reaction(
            {
                "owner_id": "owner-a",
                "session_id": "session-a",
                "situation_id": "situation-1",
                "reaction_id": "reaction-2",
                "disposition": "suggest",
                "suggested_next_step": "Review the next meaningful step.",
            }
        )
        expect(second["conversation"]["conversation_id"] == first_id, "same Situation reuses conversation")
        expect(len(second["conversation"]["messages"]) == 2, "later proactive appends to reused conversation")

        isolated = runtime.record_reaction(
            {
                "owner_id": "owner-b",
                "session_id": "session-a",
                "situation_id": "situation-1",
                "reaction_id": "reaction-1",
                "disposition": "suggest",
                "suggested_next_step": "This belongs to another owner.",
            }
        )
        expect(isolated["conversation"]["conversation_id"] != first_id, "cross-scope proactive is isolated")

        duplicate = runtime.record_reaction(
            {
                "owner_id": "owner-a",
                "session_id": "session-a",
                "situation_id": "situation-1",
                "reaction_id": "reaction-2",
                "disposition": "suggest",
                "suggested_next_step": "Review the next meaningful step.",
            }
        )
        expect(duplicate["status"] == "duplicate", "duplicate reaction is idempotent")
        expect(len(duplicate["conversation"]["messages"]) == 2, "duplicate reaction does not append")

        before_get = files_snapshot(store)
        listed = client.get("/product/conversations?user_id=owner-a&session_id=session-a")
        detail = client.get(f"/product/conversations/{first_id}?user_id=owner-a&session_id=session-a")
        after_get = files_snapshot(store)
        expect(listed.status_code == 200 and detail.status_code == 200, "GET list/detail succeed")
        expect(before_get == after_get, "GET list/detail are byte-pure")
        expect(detail.json()["messages"][0]["kind"] == "proactive", "detail exposes proactive message")

        foreground = runtime.record_foreground_turn(
            owner_id="owner-a",
            session_id="session-a",
            text="I am continuing this Situation.",
            message_id="user-message-1",
            response="Veyra recorded the turn.",
            situation_id="situation-1",
            conversation_id=first_id,
        )
        expect(foreground["conversation_id"] == first_id, "foreground turn uses bound conversation")
        expect([item["role"] for item in foreground["messages"]] == ["user", "assistant"], "foreground user and response append")
        repeated_foreground = runtime.record_foreground_turn(
            owner_id="owner-a",
            session_id="session-a",
            text="I am continuing this Situation.",
            message_id="user-message-1",
            response="Veyra recorded the turn.",
            situation_id="situation-1",
            conversation_id=first_id,
        )
        expect(repeated_foreground["status"] == "duplicate", "message_id makes foreground turn idempotent")
        expect(len(repeated_foreground["conversation"]["messages"]) == 4, "duplicate foreground turn does not append")

        try:
            runtime.record_foreground_turn(
                owner_id="owner-a",
                session_id="session-a",
                text="This belongs to another Situation.",
                message_id="wrong-situation-message",
                response="must not append",
                situation_id="situation-2",
                conversation_id=first_id,
            )
        except ProductConversationConflict:
            pass
        else:
            raise AssertionError("a bound conversation accepted another Situation")
        unchanged = runtime.get_conversation(first_id, owner_id="owner-a", session_id="session-a")
        expect(unchanged is not None and len(unchanged["messages"]) == 4, "Situation binding conflict is byte-stable")

        atomic_store = WorldStateStore(Path(temporary) / "atomic-state")
        atomic_runtime = ProductConversationRuntime(
            atomic_store,
            max_messages_per_conversation=1,
        )
        atomic_conversation = atomic_runtime.create_conversation(
            owner_id="atomic-owner",
            session_id="atomic-session",
        )
        try:
            atomic_runtime.record_foreground_turn(
                owner_id="atomic-owner",
                session_id="atomic-session",
                text="one user message",
                message_id="atomic-message",
                response="one assistant response",
                conversation_id=str(atomic_conversation["conversation_id"]),
            )
        except ProductConversationStorageError:
            pass
        else:
            raise AssertionError("over-capacity foreground turn was accepted")
        atomic_after = atomic_runtime.get_conversation(
            str(atomic_conversation["conversation_id"]),
            owner_id="atomic-owner",
            session_id="atomic-session",
        )
        expect(atomic_after is not None and atomic_after["messages"] == [], "foreground user and assistant messages commit atomically")

        admitted = runtime.record_foreground_turn(
            owner_id="admit-owner",
            session_id="admit-session",
            text="Save this before cognition finishes.",
            message_id="admit-message",
            responses=[],
        )
        expect(
            [item["role"] for item in admitted["messages"]] == ["user"],
            "admission records the user turn without a response",
        )
        completed_admission = runtime.record_foreground_turn(
            owner_id="admit-owner",
            session_id="admit-session",
            text="Save this before cognition finishes.",
            message_id="admit-message",
            responses=["The turn completed after admission."],
            conversation_id=str(admitted["conversation_id"]),
        )
        expect(completed_admission["status"] == "recorded", "assistant append after admission is not a conflict")
        expect(
            [item["role"] for item in completed_admission["conversation"]["messages"]] == ["user", "assistant"],
            "admission then completion yields one user turn and one response",
        )

        existing_situation_store = WorldStateStore(Path(temporary) / "existing-situation-state")
        existing_situation_runtime = ProductConversationRuntime(existing_situation_store)
        proactive = existing_situation_runtime.record_reaction(
            {
                "owner_id": "identity-owner",
                "session_id": "identity-session",
                "situation_id": "identity-situation",
                "reaction_id": "identity-reaction-1",
                "disposition": "suggest",
                "suggested_next_step": "Existing proactive thread.",
            }
        )
        proactive_id = str(proactive["conversation"]["conversation_id"])
        foreground = existing_situation_runtime.record_foreground_turn(
            owner_id="identity-owner",
            session_id="identity-session",
            text="Foreground stays in A.",
            message_id="identity-user-message",
            responses=[],
        )
        foreground_id = str(foreground["conversation_id"])
        completed_foreground = existing_situation_runtime.record_foreground_turn(
            owner_id="identity-owner",
            session_id="identity-session",
            text="Foreground stays in A.",
            message_id="identity-user-message",
            response="Assistant stays with the user turn.",
            conversation_id=foreground_id,
            situation_id="identity-situation",
        )
        expect(completed_foreground["conversation_id"] == foreground_id, "explicit foreground conversation identity is preserved")
        foreground_detail = existing_situation_runtime.get_conversation(
            foreground_id,
            owner_id="identity-owner",
            session_id="identity-session",
        )
        proactive_detail = existing_situation_runtime.get_conversation(
            proactive_id,
            owner_id="identity-owner",
            session_id="identity-session",
        )
        expect(
            foreground_detail is not None
            and [item["role"] for item in foreground_detail["messages"]] == ["user", "assistant"]
            and foreground_detail["binding_id"] is None,
            "foreground A remains unbound with its assistant completion",
        )
        expect(
            proactive_detail is not None
            and len(proactive_detail["messages"]) == 1
            and proactive_detail["messages"][0]["text"] == "Existing proactive thread.",
            "existing proactive B remains an independent thread",
        )
        related_proactive = existing_situation_runtime.record_reaction(
            {
                "owner_id": "identity-owner",
                "session_id": "identity-session",
                "situation_id": "identity-situation",
                "reaction_id": "identity-reaction-2",
                "disposition": "suggest",
                "suggested_next_step": "Later proactive stays in B.",
            }
        )
        expect(related_proactive["conversation"]["conversation_id"] == proactive_id, "later proactive reuses the unique B thread")
        expect(len(related_proactive["conversation"]["messages"]) == 2, "later proactive appends to B only")

        no_target_store = WorldStateStore(Path(temporary) / "no-target-state")
        no_target_runtime = ProductConversationRuntime(no_target_store)
        no_target_pending = no_target_runtime.record_foreground_turn(
            owner_id="bind-owner",
            session_id="bind-session",
            text="Bind this foreground thread.",
            message_id="bind-user-message",
            responses=[],
        )
        no_target_id = str(no_target_pending["conversation_id"])
        no_target_completed = no_target_runtime.record_foreground_turn(
            owner_id="bind-owner",
            session_id="bind-session",
            text="Bind this foreground thread.",
            message_id="bind-user-message",
            response="Bound response.",
            conversation_id=no_target_id,
            situation_id="bind-situation",
        )
        expect(no_target_completed["conversation_id"] == no_target_id, "without B the explicit A thread is retained")
        no_target_proactive = no_target_runtime.record_reaction(
            {
                "owner_id": "bind-owner",
                "session_id": "bind-session",
                "situation_id": "bind-situation",
                "reaction_id": "bind-reaction",
                "disposition": "suggest",
                "suggested_next_step": "Proactive reuses bound A.",
            }
        )
        expect(no_target_proactive["conversation"]["conversation_id"] == no_target_id, "first Situation proactive reuses bound A")
        expect(len(no_target_proactive["conversation"]["messages"]) == 3, "bound A contains the foreground turn and proactive message")

        for disposition in ("ask", "read", "wait", "silent"):
            ignored = runtime.record_reaction(
                {
                    "owner_id": "owner-a",
                    "session_id": "session-a",
                    "situation_id": "situation-1",
                    "reaction_id": f"reaction-{disposition}",
                    "disposition": disposition,
                    "suggested_next_step": "must not be recorded",
                }
            )
            expect(ignored["status"] == "ignored", f"{disposition} does not create assistant message")
        current = runtime.get_conversation(first_id, owner_id="owner-a", session_id="session-a")
        expect(current is not None and len(current["messages"]) == 4, "non-suggest reactions leave ledger unchanged")

        created_http = client.post(
            "/product/conversations?user_id=owner-c&session_id=session-c",
            json={"title": "HTTP conversation"},
        )
        expect(created_http.status_code == 200, "POST conversation creates a server record")
        expect(created_http.json()["conversation"]["owner_id"] == "owner-c", "POST conversation preserves exact scope")
        rejected_binding = client.post(
            "/product/conversations?user_id=owner-c&session_id=session-c",
            json={"binding_type": "situation", "binding_id": "situation-http"},
        )
        expect(rejected_binding.status_code == 422, "public POST cannot claim a server-owned Situation binding")

        try:
            runtime.append_message(
                first_id,
                owner_id="owner-a",
                session_id="session-a",
                message_id="stale-message",
                role="assistant",
                text="stale",
                expected_revision=1,
            )
        except ProductConversationRevisionConflict:
            print("PASS stale conversation revision is rejected")
        else:
            raise AssertionError("stale conversation revision was accepted")

        replay_conversation = runtime.create_conversation(
            owner_id="replay-owner",
            session_id="replay-session",
        )
        replay_id = str(replay_conversation["conversation_id"])
        runtime.append_message(
            replay_id,
            owner_id="replay-owner",
            session_id="replay-session",
            message_id="revision-replay",
            role="assistant",
            text="stable",
            expected_revision=1,
        )
        duplicate_with_old_revision = runtime.append_message(
            replay_id,
            owner_id="replay-owner",
            session_id="replay-session",
            message_id="revision-replay",
            role="assistant",
            text="stable",
            expected_revision=1,
        )
        expect(duplicate_with_old_revision["status"] == "duplicate", "idempotent replay precedes stale CAS rejection")

        payload: dict[str, Any] = listed.json()
        expect(payload["items"][0]["conversation_id"] == first_id, "list returns stable conversation id")
        json.dumps(payload, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
