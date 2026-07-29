#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.proactive_intent_planner import ProactiveIntentPlanner  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402
from interface.event_schema import EventSource, EventType, VeyraEvent  # noqa: E402


USER_A = "planner-user-a"
USER_B = "planner-user-b"
SESSION_A = "planner-session-a"
OTHER_SESSION = "planner-session-other"

A_MARKER = "PLANNER_PRIVATE_A_MARKER"
B_MARKER = "PLANNER_PRIVATE_B_MARKER"
OTHER_SESSION_MARKER = "PLANNER_OTHER_SESSION_MARKER"
OWNERLESS_MARKER = "PLANNER_OWNERLESS_MARKER"
CONFLICT_MARKER = "PLANNER_CONFLICT_MARKER"
IMPLICIT_GLOBAL_MARKER = "PLANNER_IMPLICIT_GLOBAL_MARKER"
OPERATOR_GLOBAL_MARKER = "PLANNER_OPERATOR_GLOBAL_MARKER"
LEGACY_MARKER = "PLANNER_LEGACY_ROOT_MARKER"
OWNERLESS_MEMORY_MARKER = "PLANNER_OWNERLESS_MEMORY_MARKER"


def expect(condition: bool, label: str, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


class CaptureModelClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def status(self) -> dict[str, Any]:
        return {"configured": True}

    def complete_json(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(dict(kwargs))
        return {"status": "unavailable"}


def event(*, user_id: str = USER_A, session_id: str = SESSION_A) -> VeyraEvent:
    return VeyraEvent(
        type=EventType.USER_MESSAGE,
        source=EventSource(
            channel="scope-smoke",
            user_id=user_id,
            session_id=session_id,
        ),
        payload={"text": "请规划我的后续关注任务"},
    )


def exact_item(marker: str, *, user_id: str, session_id: str) -> dict[str, Any]:
    return {
        "user_id": user_id,
        "session_id": session_id,
        "scope_kind": "tenant",
        "target": marker,
        "topic": marker,
        "title": marker,
        "summary": marker,
        "kind": "external_search",
        "status": "fresh",
    }


def external_rows(field: str) -> list[dict[str, Any]]:
    rows = [
        exact_item(A_MARKER, user_id=USER_A, session_id=SESSION_A),
        exact_item(B_MARKER, user_id=USER_B, session_id=SESSION_A),
        exact_item(
            OTHER_SESSION_MARKER,
            user_id=USER_A,
            session_id=OTHER_SESSION,
        ),
        {
            **exact_item(
                CONFLICT_MARKER,
                user_id=USER_A,
                session_id=SESSION_A,
            ),
            "owner": {
                "user_id": USER_B,
                "session_id": SESSION_A,
            },
        },
        {
            "scope_kind": "tenant",
            "target": OWNERLESS_MARKER,
            "topic": OWNERLESS_MARKER,
            "title": OWNERLESS_MARKER,
            "summary": OWNERLESS_MARKER,
            "kind": "external_search",
            "status": "fresh",
        },
        {
            "target": "http://127.0.0.1",
            "topic": IMPLICIT_GLOBAL_MARKER,
            "title": IMPLICIT_GLOBAL_MARKER,
            "summary": IMPLICIT_GLOBAL_MARKER,
            "kind": "web",
            "status": "fresh",
        },
        {
            "scope_kind": "operator_global",
            "target": OPERATOR_GLOBAL_MARKER,
            "topic": OPERATOR_GLOBAL_MARKER,
            "title": OPERATOR_GLOBAL_MARKER,
            "summary": OPERATOR_GLOBAL_MARKER,
            "kind": "system",
            "status": "fresh",
        },
    ]
    if field == "push_candidates":
        for row in rows:
            row["created_at"] = "2026-07-29T00:00:00+00:00"
    return rows


def seed_state(store: WorldStateStore) -> None:
    store.write_json(
        "user_world.json",
        {
            "current_project": LEGACY_MARKER,
            "profile": {"marker": LEGACY_MARKER},
            "profiles_by_user": {
                USER_A: {
                    "current_project": A_MARKER,
                    "profile": {"marker": A_MARKER},
                },
                USER_B: {
                    "current_project": B_MARKER,
                    "profile": {"marker": B_MARKER},
                },
            },
        },
    )
    store.write_json(
        "local_world.json",
        {
            "current_project": B_MARKER,
            "probes": {
                B_MARKER: exact_item(
                    B_MARKER,
                    user_id=USER_B,
                    session_id=SESSION_A,
                ),
                OPERATOR_GLOBAL_MARKER: {
                    "scope_kind": "operator_global",
                    "probe": "system_probe",
                    "status": "ok",
                },
            },
        },
    )
    store.write_json(
        "user_commitments.json",
        {
            "commitments": [
                {
                    "commitment_id": "commitment-a-active",
                    "user_id": USER_A,
                    "session_id": SESSION_A,
                    "status": "active",
                    "kind": "external_digest",
                    "title": A_MARKER,
                    "payload": {"topic": A_MARKER},
                },
                {
                    "commitment_id": "commitment-a-paused",
                    "user_id": USER_A,
                    "session_id": SESSION_A,
                    "status": "paused",
                    "kind": "external_digest",
                    "title": A_MARKER,
                    "payload": {"topic": A_MARKER},
                },
                {
                    "commitment_id": "commitment-b",
                    "user_id": USER_B,
                    "session_id": SESSION_A,
                    "status": "active",
                    "kind": "external_digest",
                    "title": B_MARKER,
                    "payload": {"topic": B_MARKER},
                },
                {
                    "commitment_id": "commitment-other-session",
                    "user_id": USER_A,
                    "session_id": OTHER_SESSION,
                    "status": "active",
                    "kind": "external_digest",
                    "title": OTHER_SESSION_MARKER,
                    "payload": {"topic": OTHER_SESSION_MARKER},
                },
                {
                    "commitment_id": "commitment-ownerless",
                    "status": "active",
                    "kind": "external_digest",
                    "title": OWNERLESS_MARKER,
                    "payload": {"topic": OWNERLESS_MARKER},
                },
                {
                    "commitment_id": "commitment-conflict",
                    "user_id": USER_A,
                    "session_id": SESSION_A,
                    "owner": {
                        "user_id": USER_B,
                        "session_id": SESSION_A,
                    },
                    "status": "active",
                    "kind": "external_digest",
                    "title": CONFLICT_MARKER,
                    "payload": {"topic": CONFLICT_MARKER},
                },
                {
                    "commitment_id": "commitment-global",
                    "scope_kind": "operator_global",
                    "status": "active",
                    "kind": "external_digest",
                    "title": OPERATOR_GLOBAL_MARKER,
                    "payload": {"topic": OPERATOR_GLOBAL_MARKER},
                },
            ],
        },
    )
    store.write_json(
        "external_world.json",
        {
            field: external_rows(field)
            for field in (
                "watchlist",
                "summaries",
                "knowledge_items",
                "push_candidates",
            )
        },
    )


def captured_payload(client: CaptureModelClient) -> dict[str, Any]:
    expect(bool(client.calls), "planner invoked configured model")
    return json.loads(str(client.calls[-1]["user"]))


def main() -> int:
    with TemporaryDirectory(prefix="veyra-planner-scope-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        seed_state(store)
        client = CaptureModelClient()
        planner = ProactiveIntentPlanner(store, client=client)  # type: ignore[arg-type]

        planned = planner._model_plan(  # noqa: SLF001
            user_text="请规划我的后续关注任务",
            event=event(),
            memory_summary={
                "user_id": USER_A,
                "session_id": SESSION_A,
                "summary": A_MARKER,
            },
        )
        expect(planned is None, "capture client leaves planner result unavailable")
        payload = captured_payload(client)
        context = payload["world_context"]
        serialized = json.dumps(context, ensure_ascii=False, sort_keys=True)

        expect(
            payload["event_source"]["user_id"] == USER_A
            and payload["event_source"]["session_id"] == SESSION_A
            and context.get("scope_status") == "exact_owner",
            "event owner is passed into planner context",
            payload,
        )
        expect(
            A_MARKER in serialized,
            "exact owner data reaches its own model payload",
            context,
        )
        expect(
            OPERATOR_GLOBAL_MARKER in serialized,
            "explicit operator-global ExternalWorld data is shared",
            context,
        )
        for marker in (
            B_MARKER,
            OWNERLESS_MARKER,
            CONFLICT_MARKER,
            IMPLICIT_GLOBAL_MARKER,
            LEGACY_MARKER,
        ):
            expect(
                marker not in serialized,
                f"{marker} is excluded from user A model payload",
                context,
            )

        active_ids = {
            str(item.get("commitment_id"))
            for item in context["active_commitments"]
        }
        paused_ids = {
            str(item.get("commitment_id"))
            for item in context["paused_commitments"]
        }
        expect(
            active_ids
            == {
                "commitment-a-active",
                "commitment-other-session",
            }
            and paused_ids == {"commitment-a-paused"},
            "commitments require exact user ownership and preserve cross-session continuity",
            {"active": active_ids, "paused": paused_ids},
        )
        external_serialized = json.dumps(
            context["external_world"],
            ensure_ascii=False,
            sort_keys=True,
        )
        expect(
            OTHER_SESSION_MARKER not in external_serialized,
            "ExternalWorld remains exact to the event session",
            context["external_world"],
        )
        external = context["external_world"]
        expect(
            external.get("knowledge_count") == 2
            and external.get("push_candidate_count") == 2,
            "ExternalWorld counts include only exact owner and explicit global rows",
            external,
        )

        client.calls.clear()
        planner._model_plan(  # noqa: SLF001
            user_text="ownerless memory",
            event=event(),
            memory_summary={
                "session_id": SESSION_A,
                "summary": OWNERLESS_MEMORY_MARKER,
            },
        )
        ownerless_memory_context = captured_payload(client)["world_context"]
        expect(
            ownerless_memory_context.get("memory_summary") == {}
            and OWNERLESS_MEMORY_MARKER
            not in json.dumps(
                ownerless_memory_context,
                ensure_ascii=False,
                sort_keys=True,
            ),
            "memory summary requires an explicit exact user-session envelope",
            ownerless_memory_context,
        )

        client.calls.clear()
        planner._model_plan(  # noqa: SLF001
            user_text="invalid owner",
            event=event(user_id=f"{USER_A}\x00"),
            memory_summary={
                "user_id": USER_A,
                "session_id": SESSION_A,
                "summary": A_MARKER,
            },
        )
        invalid_context = captured_payload(client)["world_context"]
        invalid_serialized = json.dumps(
            invalid_context,
            ensure_ascii=False,
            sort_keys=True,
        )
        expect(
            invalid_context.get("scope_status") == "invalid_owner"
            and A_MARKER not in invalid_serialized
            and OPERATOR_GLOBAL_MARKER not in invalid_serialized,
            "invalid event owner fails closed before loading planner state",
            invalid_context,
        )

    print("proactive planner scope smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
