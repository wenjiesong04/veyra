#!/usr/bin/env python3
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.awareness_loop import AwarenessLoop
from awareness.attention_core import AttentionCore
from core.commitment_core import CommitmentCore
from core.definitions import RiskLevel
from core.world_state import WorldStateStore
from interface.event_schema import Decision, EventSource, EventType, Route, VeyraEvent


def expect(condition: bool, label: str, details: object = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {details!r}")


def event_for(user_id: str, session_id: str) -> VeyraEvent:
    return VeyraEvent(
        type=EventType.USER_MESSAGE,
        source=EventSource(channel="api", user_id=user_id, session_id=session_id),
        payload={"text": "当前还有哪些未完成任务和关注主题？"},
    )


def read_only_decision() -> Decision:
    return Decision(
        route=Route.DIRECT_ANSWER,
        risk_level=RiskLevel.R0,
        reason="smoke-authorized local state read",
        model_assist={
            "semantic_policy": {
                "preferred_route": "direct_answer",
                "allowed_effects": [],
                "denied_effects": [
                    "agent.execute",
                    "commitment.mutate",
                    "external.write",
                    "memory.write",
                    "proactive.create",
                    "profile.write",
                    "workspace.write",
                ],
                "authoritative_act_ids": ["state-read-1"],
                "requires_clarification": False,
            }
        },
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = WorldStateStore(Path(tmpdir) / "state")

        before = store.read_json("external_world.json")
        unchanged = store.mutate_json("external_world.json", lambda current: current)
        expect(
            unchanged.get("_state_revision") == before.get("_state_revision"),
            "no-op mutation preserves revision",
            unchanged,
        )
        expect(
            unchanged.get("updated_at") == before.get("updated_at"),
            "no-op mutation preserves freshness timestamp",
            unchanged,
        )

        store.mutate_json(
            "task_state.json",
            lambda state: {
                **state,
                "pending_agent_tasks": [
                    {
                        "task_id": "task-alpha",
                        "title": "ALPHA_PRIVATE_TASK",
                        "status": "pending",
                        "user_id": "user-a",
                        "session_id": "session-a",
                    },
                    {
                        "task_id": "task-beta",
                        "title": "BETA_PRIVATE_TASK",
                        "status": "pending",
                        "user_id": "user-b",
                        "session_id": "session-b",
                    },
                ],
            },
        )
        store.mutate_json(
            "user_commitments.json",
            lambda state: {
                **state,
                "commitments": [
                    {
                        "commitment_id": "commitment-alpha",
                        "title": "ALPHA_PRIVATE_COMMITMENT",
                        "status": "active",
                        "user_id": "user-a",
                        "session_id": "session-a",
                        "payload": {"topic": "ALPHA_PRIVATE_TOPIC"},
                    },
                    {
                        "commitment_id": "commitment-beta",
                        "title": "BETA_PRIVATE_COMMITMENT",
                        "status": "active",
                        "user_id": "user-b",
                        "session_id": "session-b",
                        "payload": {"topic": "BETA_PRIVATE_TOPIC"},
                    },
                ],
            },
        )
        store.mutate_json(
            "external_world.json",
            lambda state: {
                **state,
                "watchlist": [
                    {
                        "target": "alpha",
                        "topic": "ALPHA_PRIVATE_WATCH",
                        "status": "active",
                        "user_id": "user-a",
                        "session_id": "session-a",
                    },
                    {
                        "target": "beta",
                        "topic": "BETA_PRIVATE_WATCH",
                        "status": "active",
                        "user_id": "user-b",
                        "session_id": "session-b",
                    },
                ],
            },
        )

        loop = AwarenessLoop.__new__(AwarenessLoop)
        loop.state_store = store
        loop.attention = AttentionCore(store)
        loop.commitment_core = CommitmentCore(store)
        user_a = event_for("user-a", "session-a")
        user_b = event_for("user-b", "session-b")
        answer_a = loop._local_state_answer_contract("还有哪些未完成任务", user_a)["answer_text"]
        answer_b = loop._local_state_answer_contract("还有哪些未完成任务", user_b)["answer_text"]
        expect("ALPHA_PRIVATE_TASK" in answer_a, "user A sees own task", answer_a)
        expect("BETA_PRIVATE_TASK" not in answer_a, "user A cannot see user B task", answer_a)
        expect("ALPHA_PRIVATE_TOPIC" in answer_a, "user A sees own topic", answer_a)
        expect("BETA_PRIVATE_TOPIC" not in answer_a, "user A cannot see user B topic", answer_a)
        expect("BETA_PRIVATE_TASK" in answer_b, "user B sees own task", answer_b)
        expect("ALPHA_PRIVATE_TASK" not in answer_b, "user B cannot see user A task", answer_b)

        tracking_a = loop._tracking_memory_response(user_a)
        tracking_b = loop._tracking_memory_response(user_b)
        expect("ALPHA_PRIVATE_WATCH" in tracking_a, "tracking recall is user scoped", tracking_a)
        expect("BETA_PRIVATE_WATCH" not in tracking_a, "tracking recall blocks cross-user watchlist", tracking_a)
        expect("BETA_PRIVATE_WATCH" in tracking_b, "second user sees own watchlist", tracking_b)

        inventory_text = "我现在有哪些正在进行的任务或提醒？"
        expect(loop._is_state_workload_question(inventory_text), "task inventory query is recognized before model routing")
        expect(loop._is_commitment_state_question(inventory_text), "reminder inventory query is read-only")
        early = loop._early_awareness_response(
            user_a,
            inventory_text,
            [],
            decision=read_only_decision(),
        )
        expect(
            early.get("reason") == "state_grounded_local_answer",
            "task inventory query uses local state instead of a probe",
            early,
        )
        before_commitments = list(store.read_json("user_commitments.json").get("commitments", []))
        commitment_answer = loop.commitment_core.process_turn(
            event=user_a,
            user_text=inventory_text,
            assistant_response=str(early.get("response") or ""),
            route="direct_answer",
            status="success",
        )
        after_commitments = list(store.read_json("user_commitments.json").get("commitments", []))
        expect(
            commitment_answer.get("status") == "state_answer",
            "task inventory query returns commitment state",
            commitment_answer,
        )
        expect(
            before_commitments == after_commitments,
            "task inventory query cannot create a reminder",
            after_commitments,
        )

    print("state truth and user isolation smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
