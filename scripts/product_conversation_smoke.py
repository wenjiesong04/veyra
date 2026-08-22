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
