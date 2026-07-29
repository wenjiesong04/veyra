#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.commitment_core import CommitmentCore  # noqa: E402
from core.context_scope import tenant_scope_storage_key  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402
from routers.commitments import build_commitments_router  # noqa: E402
from runtime.commitment_push import CommitmentPushRuntime  # noqa: E402


USER_A = "commitment-scope-user-a"
USER_B = "commitment-scope-user-b"
SESSION_A = "commitment-scope-session-a"
SESSION_A_2 = "commitment-scope-session-a-2"
SESSION_B = "commitment-scope-session-b"


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail!r}")
    print(f"PASS {label}")


def commitment_payload(
    *,
    user_id: str,
    session_id: str,
    kind: str = "generic_reminder",
    status: str = "active",
    title: str = "scope smoke",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "status": status,
        "title": title,
        "user_id": user_id,
        "session_id": session_id,
        "channel": "api",
        "schedule": {
            "kind": "daily",
            "time_local": "08:00",
            "timezone": "Asia/Shanghai",
        },
        "payload": payload or {"note": title},
    }


class RecordingCommitmentPush:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def run_due(self, *, limit: int, reason: str) -> dict[str, Any]:
        self.calls.append({"limit": limit, "reason": reason})
        return {
            "status": "idle",
            "reason": reason,
            "due_count": 0,
            "processed_count": 0,
            "processed": [],
        }


def push_scope_checks(store: WorldStateStore, core: CommitmentCore) -> None:
    push = CommitmentPushRuntime(
        state_store=store,
        commitment_core=core,
    )
    external_commitment = core.create_commitment(
        commitment_payload(
            user_id=USER_A,
            session_id=SESSION_A,
            kind="external_digest",
            title="A external digest",
            payload={"topic": "A-TOPIC"},
        )
    )
    commitment_id = str(external_commitment["commitment_id"])
    store.patch_json(
        "external_world.json",
        {
            "push_candidates": [
                {
                    "candidate_id": "shared-candidate-id",
                    "commitment_id": commitment_id,
                    "scope_kind": "tenant",
                    "tenant_derived": True,
                    "user_id": USER_B,
                    "session_id": SESSION_B,
                    "status": "new",
                    "score": 1.0,
                    "title": "B-SECRET-CANDIDATE",
                    "snippet": "B-SECRET-CANDIDATE",
                },
                {
                    "candidate_id": "ownerless-candidate",
                    "commitment_id": commitment_id,
                    "scope_kind": "tenant",
                    "tenant_derived": True,
                    "status": "new",
                    "score": 0.99,
                    "title": "OWNERLESS-SECRET-CANDIDATE",
                    "snippet": "OWNERLESS-SECRET-CANDIDATE",
                },
                {
                    "candidate_id": "shared-candidate-id",
                    "commitment_id": commitment_id,
                    "scope_kind": "tenant",
                    "tenant_derived": True,
                    "user_id": USER_A,
                    "session_id": SESSION_A,
                    "status": "new",
                    "score": 0.8,
                    "title": "A-OWN-CANDIDATE",
                    "snippet": "A-OWN-CANDIDATE",
                },
            ]
        },
    )
    message, message_context = push._build_message(external_commitment)
    expect(
        "A-OWN-CANDIDATE" in message
        and "B-SECRET-CANDIDATE" not in message
        and "OWNERLESS-SECRET-CANDIDATE" not in message,
        "push candidate rendering requires exact commitment owner",
        {"message": message, "context": message_context},
    )
    push._mark_push_candidate(message_context, "delivered")
    candidates = store.read_json("external_world.json").get(
        "push_candidates",
        [],
    )
    a_candidate = next(
        item
        for item in candidates
        if item.get("candidate_id") == "shared-candidate-id"
        and item.get("user_id") == USER_A
    )
    b_candidate = next(
        item
        for item in candidates
        if item.get("candidate_id") == "shared-candidate-id"
        and item.get("user_id") == USER_B
    )
    expect(
        a_candidate.get("status") == "delivered"
        and b_candidate.get("status") == "new",
        "candidate status update cannot mutate duplicate peer id",
        {"user_a": a_candidate, "user_b": b_candidate},
    )

    monitor = core.create_commitment(
        commitment_payload(
            user_id=USER_A,
            session_id=SESSION_A,
            kind="local_probe_monitor",
            title="A local monitor",
            payload={"topic": "A runtime"},
        )
    )
    a_scope_key = tenant_scope_storage_key(USER_A, SESSION_A)
    b_scope_key = tenant_scope_storage_key(USER_B, SESSION_B)
    store.patch_json(
        "local_world.json",
        {
            "probes": {
                "peer_probe": {
                    "probe": "peer_probe",
                    "scope_kind": "tenant",
                    "tenant_derived": True,
                    "user_id": USER_B,
                    "session_id": SESSION_B,
                    "status": "B-SECRET-PROBE",
                }
            },
            "scoped_probes": {
                a_scope_key: {
                    "file_probe": {
                        "probe": "file_probe",
                        "scope_kind": "tenant",
                        "tenant_derived": True,
                        "user_id": USER_A,
                        "session_id": SESSION_A,
                        "status": "A-OWN-PROBE",
                    }
                },
                b_scope_key: {
                    "file_probe": {
                        "probe": "file_probe",
                        "scope_kind": "tenant",
                        "tenant_derived": True,
                        "user_id": USER_B,
                        "session_id": SESSION_B,
                        "status": "B-SECRET-SCOPED-PROBE",
                    }
                },
            },
        },
    )
    monitor_message, _ = push._build_message(monitor)
    expect(
        "A-OWN-PROBE" in monitor_message
        and "B-SECRET-PROBE" not in monitor_message
        and "B-SECRET-SCOPED-PROBE" not in monitor_message,
        "local monitor renders only visible exact-owner probe cache",
        monitor_message,
    )


def goal_scope_checks(store: WorldStateStore, core: CommitmentCore) -> None:
    goal = core._upsert_user_goal(
        {
            "kind": "learning",
            "status": "active",
            "topic": "A-LEARNING-GOAL",
            "user_id": USER_A,
            "session_id": SESSION_A,
            "permissions": {
                "external_search": "pending_confirmation",
                "proactive_push": "pending_confirmation",
            },
        }
    )
    goal_id = str(goal["goal_id"])
    b_commitment = core.create_commitment(
        commitment_payload(
            user_id=USER_B,
            session_id=SESSION_B,
            kind="learning_digest",
            title="B forged goal binding",
            payload={"topic": "B-TOPIC", "goal_id": goal_id},
        )
    )
    after_b = next(
        item
        for item in store.read_json("user_goals.json").get("goals", [])
        if item.get("goal_id") == goal_id
    )
    expect(
        after_b.get("commitment_id") != b_commitment.get("commitment_id")
        and after_b.get("permissions", {}).get("proactive_push")
        == "pending_confirmation",
        "cross-user commitment cannot mutate learning goal permissions",
        after_b,
    )

    same_user_commitment = core.create_commitment(
        commitment_payload(
            user_id=USER_A,
            session_id=SESSION_A_2,
            kind="learning_digest",
            title="A cross-session goal continuity",
            payload={"topic": "A-TOPIC", "goal_id": goal_id},
        )
    )
    after_same_user = next(
        item
        for item in store.read_json("user_goals.json").get("goals", [])
        if item.get("goal_id") == goal_id
    )
    expect(
        after_same_user.get("commitment_id")
        == same_user_commitment.get("commitment_id")
        and after_same_user.get("permissions", {}).get("proactive_push")
        == "granted",
        "same-user cross-session goal continuity remains allowed",
        after_same_user,
    )


def router_scope_checks(store: WorldStateStore, core: CommitmentCore) -> None:
    push = RecordingCommitmentPush()
    app = FastAPI()
    app.include_router(
        build_commitments_router(
            {
                "commitment_core": core,
                "commitment_push": push,
            }
        )
    )
    client = TestClient(app)

    missing_owner = client.post(
        "/commitments",
        json={"kind": "generic_reminder", "payload": {"note": "missing"}},
    )
    invalid_owner = client.post(
        "/commitments",
        json=commitment_payload(
            user_id=f"{USER_A}\u0000",
            session_id=SESSION_A,
            title="invalid owner",
        ),
    )
    expect(
        missing_owner.status_code == 422 and invalid_owner.status_code == 422,
        "commitment creation requires valid declared owner scope",
        {
            "missing": missing_owner.text,
            "invalid": invalid_owner.text,
        },
    )

    def create(
        *,
        user_id: str,
        session_id: str,
        title: str,
        status: str = "active",
    ) -> dict[str, Any]:
        response = client.post(
            "/commitments",
            json=commitment_payload(
                user_id=user_id,
                session_id=session_id,
                title=title,
                status=status,
            ),
        )
        expect(
            response.status_code == 200,
            f"create {title}",
            response.text,
        )
        return response.json()["commitment"]

    a_one = create(
        user_id=USER_A,
        session_id=SESSION_A,
        title="A one",
    )
    a_two = create(
        user_id=USER_A,
        session_id=SESSION_A_2,
        title="A two",
    )
    b_one = create(
        user_id=USER_B,
        session_id=SESSION_B,
        title="B one",
    )

    state = store.read_json("user_commitments.json")
    commitments = (
        state.get("commitments")
        if isinstance(state.get("commitments"), list)
        else []
    )
    commitments.extend(
        [
            {
                "commitment_id": "ownerless-commitment",
                "title": "OWNERLESS-SECRET",
                "status": "active",
            },
            {
                "commitment_id": "conflicting-commitment",
                "title": "CONFLICTING-SECRET",
                "status": "active",
                "user_id": USER_A,
                "session_id": SESSION_A,
                "owner": {
                    "user_id": USER_B,
                    "session_id": SESSION_A,
                },
            },
        ]
    )
    store.write_json(
        "user_commitments.json",
        {**state, "commitments": commitments},
    )

    no_scope = client.get("/commitments")
    user_list = client.get("/commitments", params={"user_id": USER_A})
    session_list = client.get(
        "/commitments",
        params={"user_id": USER_A, "session_id": SESSION_A},
    )
    listed_ids = {
        item.get("commitment_id")
        for item in user_list.json().get("commitments", [])
    }
    expect(
        no_scope.status_code == 422
        and user_list.status_code == 200
        and listed_ids == {
            a_one.get("commitment_id"),
            a_two.get("commitment_id"),
        }
        and session_list.status_code == 200
        and [
            item.get("commitment_id")
            for item in session_list.json().get("commitments", [])
        ]
        == [a_one.get("commitment_id")],
        "commitment list is required-user scoped with optional exact session",
        {
            "no_scope": no_scope.text,
            "user_list": user_list.text,
            "session_list": session_list.text,
            "peer": b_one,
        },
    )

    commitment_id = str(a_one["commitment_id"])
    exact_get = client.get(
        f"/commitments/{commitment_id}",
        params={"user_id": USER_A, "session_id": SESSION_A},
    )
    wrong_user_get = client.get(
        f"/commitments/{commitment_id}",
        params={"user_id": USER_B, "session_id": SESSION_A},
    )
    wrong_session_get = client.get(
        f"/commitments/{commitment_id}",
        params={"user_id": USER_A, "session_id": SESSION_A_2},
    )
    expect(
        exact_get.status_code == 200
        and wrong_user_get.status_code == 404
        and wrong_session_get.status_code == 404,
        "single commitment read requires exact owner",
        {
            "exact": exact_get.text,
            "wrong_user": wrong_user_get.text,
            "wrong_session": wrong_session_get.text,
        },
    )

    wrong_pause = client.post(
        f"/commitments/{commitment_id}/pause",
        params={"user_id": USER_B, "session_id": SESSION_B},
    )
    unchanged = core.get_commitment(commitment_id)
    exact_pause = client.post(
        f"/commitments/{commitment_id}/pause",
        params={"user_id": USER_A, "session_id": SESSION_A},
    )
    expect(
        wrong_pause.status_code == 404
        and unchanged is not None
        and unchanged.get("status") == "active"
        and exact_pause.status_code == 200
        and exact_pause.json()["commitment"].get("status") == "paused",
        "pause rejects peer and allows exact owner",
        {
            "wrong": wrong_pause.text,
            "unchanged": unchanged,
            "exact": exact_pause.text,
        },
    )

    pending = create(
        user_id=USER_A,
        session_id=SESSION_A,
        title="A pending",
        status="pending_confirmation",
    )
    pending_id = str(pending["commitment_id"])
    wrong_confirm = client.post(
        f"/commitments/{pending_id}/confirm",
        params={"user_id": USER_A, "session_id": SESSION_A_2},
    )
    exact_confirm = client.post(
        f"/commitments/{pending_id}/confirm",
        params={"user_id": USER_A, "session_id": SESSION_A},
    )
    expect(
        wrong_confirm.status_code == 404
        and exact_confirm.status_code == 200
        and exact_confirm.json()["commitment"].get("status") == "active",
        "confirm rejects wrong session and allows exact owner",
        {
            "wrong": wrong_confirm.text,
            "exact": exact_confirm.text,
        },
    )

    cancellable = create(
        user_id=USER_A,
        session_id=SESSION_A,
        title="A cancellable",
    )
    cancellable_id = str(cancellable["commitment_id"])
    wrong_cancel = client.post(
        f"/commitments/{cancellable_id}/cancel",
        params={"user_id": USER_B, "session_id": SESSION_B},
    )
    exact_cancel = client.post(
        f"/commitments/{cancellable_id}/cancel",
        params={"user_id": USER_A, "session_id": SESSION_A},
    )
    expect(
        wrong_cancel.status_code == 404
        and exact_cancel.status_code == 200
        and exact_cancel.json()["commitment"].get("status")
        == "cancelled",
        "cancel rejects peer and allows exact owner",
        {
            "wrong": wrong_cancel.text,
            "exact": exact_cancel.text,
        },
    )

    run_due = client.post(
        "/commitments/run-due",
        json={"limit": 3, "reason": "scope-smoke"},
    )
    expect(
        run_due.status_code == 200
        and run_due.json().get("operation_scope") == "operator_wide"
        and push.calls == [{"limit": 3, "reason": "scope-smoke"}],
        "run-due remains explicitly operator-wide",
        {"response": run_due.text, "calls": push.calls},
    )


def main() -> int:
    with TemporaryDirectory(prefix="veyra-commitment-scope-") as tmp:
        root = Path(tmp)
        push_store = WorldStateStore(root / "push-state")
        push_scope_checks(push_store, CommitmentCore(push_store))
        goal_store = WorldStateStore(root / "goal-state")
        goal_scope_checks(goal_store, CommitmentCore(goal_store))
        router_store = WorldStateStore(root / "router-state")
        router_scope_checks(router_store, CommitmentCore(router_store))
    print("commitment scope smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
