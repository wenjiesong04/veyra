#!/usr/bin/env python3
"""Smoke test: user commitments and proactive push loop."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TEST_RUNTIME = TemporaryDirectory(prefix="veyra-commitment-")
TEST_STATE_ROOT = Path(_TEST_RUNTIME.name) / "state"
TEST_AGENCY_ROOT = Path(_TEST_RUNTIME.name) / "agency"
os.environ["VEYRA_STATE_DIR"] = str(TEST_STATE_ROOT)
os.environ["VEYRA_STATE_ROOT"] = str(TEST_STATE_ROOT)
os.environ["VEYRA_AGENCY_DIR"] = str(TEST_AGENCY_ROOT)
os.environ["VEYRA_AGENCY_ROOT"] = str(TEST_AGENCY_ROOT)
os.environ["VEYRA_CORE_MODEL_ENABLED"] = "0"
os.environ["VEYRA_ACTIVE_LOOP_AUTOSTART"] = "0"
os.environ["VEYRA_FEISHU_WS_AUTOSTART"] = "0"
TEST_AGENCY_ROOT.mkdir(parents=True, exist_ok=True)
(TEST_AGENCY_ROOT / "goals.json").write_text("{}", encoding="utf-8")
(TEST_AGENCY_ROOT / "intention_queue.json").write_text("[]", encoding="utf-8")

import scripts.semantic_generalization_smoke as semantic_smoke  # noqa: E402
from main import app, awareness_loop, commitment_core  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402
from interface.session_mapper import SessionMapper  # noqa: E402


LEARNING_TEXT = "请建立深度学习学习辅导，开启前先让我确认。"
CONFIRM_TEXT = "好的"
LEARNING_RECALL_TEXT = "你记得我现在在学什么吗？"
WEATHER_TEXT = "每天早上八点帮我推送北京天气"


def install_semantic_fixtures() -> None:
    semantic_smoke.FRAME_FIXTURES[LEARNING_TEXT] = semantic_smoke._frame(
        acts=[
            semantic_smoke._act(
                LEARNING_TEXT,
                act_id="learning-1",
                kind="proactive_request",
                operation="create_learning_support",
                goal="建立深度学习学习辅导并在开启前确认",
                quote=LEARNING_TEXT[:-1],
                target=semantic_smoke._target("learning_topic", "深度学习"),
                arguments={"topic": "深度学习"},
            )
        ]
    )
    semantic_smoke.FRAME_FIXTURES[CONFIRM_TEXT] = semantic_smoke._frame(
        acts=[
            semantic_smoke._act(
                CONFIRM_TEXT,
                act_id="confirm-1",
                kind="commitment_control",
                operation="confirm",
                goal="确认当前会话中的待确认学习辅导",
                quote=CONFIRM_TEXT,
                target=semantic_smoke._target("pending_commitment", "current_session"),
            )
        ]
    )
    semantic_smoke.FRAME_FIXTURES[LEARNING_RECALL_TEXT] = semantic_smoke._frame(
        acts=[
            semantic_smoke._act(
                LEARNING_RECALL_TEXT,
                act_id="learning-query-1",
                kind="question",
                operation="query_learning_topic",
                goal="查询当前学习主题",
                quote=LEARNING_RECALL_TEXT[:-1],
                target=semantic_smoke._target("learning_topic", "current"),
            )
        ]
    )
    semantic_smoke.FRAME_FIXTURES[WEATHER_TEXT] = semantic_smoke._frame(
        acts=[
            semantic_smoke._act(
                WEATHER_TEXT,
                act_id="weather-1",
                kind="recurring_request",
                operation="schedule_weather_push",
                goal="每天早上八点收到北京天气",
                quote=WEATHER_TEXT,
                target=semantic_smoke._target("weather_summary", "北京"),
                arguments={
                    "location": "北京",
                    "schedule": {
                        "kind": "daily",
                        "time_local": "08:00",
                        "timezone": "Asia/Shanghai",
                        "interval_seconds": 86400.0,
                    },
                },
            )
        ]
    )


install_semantic_fixtures()
semantic_model = semantic_smoke.ScriptedSemanticModelClient()
awareness_loop.core_reasoning.client = semantic_model  # type: ignore[assignment]
commitment_core.intent_planner.client = semantic_model  # type: ignore[assignment]
client = TestClient(app)


def expect(condition: bool, label: str, detail=None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


def message_text(body: dict) -> str:
    return "\n".join([str(body.get("response") or ""), *[str(item) for item in body.get("followup_messages", []) if item]])


def main() -> int:
    store = WorldStateStore(TEST_STATE_ROOT)

    learning_turn = client.post(
        "/events/message",
        json={
            "text": LEARNING_TEXT,
            "channel": "self-test",
            "user_id": "cmt-user",
            "session_id": "cmt-learn",
        },
    )
    expect(learning_turn.status_code == 200, "learning goal message", learning_turn.text)
    learning_body = learning_turn.json()
    learning_artifact = (learning_body.get("artifacts") or {}).get("commitment") or {}
    expect(learning_artifact.get("status") == "created", "learning task recorded", learning_artifact)
    expect(
        learning_body.get("route") == "direct_answer"
        and learning_artifact.get("semantic_source") == "authoritative_semantic_frame"
        and not (learning_body.get("artifacts") or {}).get("execution_result"),
        "semantic learning request stays inside Veyra without Agent routing",
        learning_body,
    )
    expect("待确认" in message_text(learning_body), "learning response preserves explicit confirmation boundary", learning_body)

    goals = store.read_json("user_goals.json").get("goals", [])
    expect(
        not any(isinstance(goal, dict) and goal.get("topic") == "深度学习" for goal in goals),
        "proactive authorization does not silently widen into a separate durable goal write",
        goals,
    )
    pending_digest = learning_artifact.get("commitment") if isinstance(learning_artifact.get("commitment"), dict) else {}
    expect(pending_digest.get("status") == "pending_confirmation", "learning digest is pending before consent", pending_digest)

    learn_session = pending_digest.get("session_id") or "self-test:cmt-user:cmt-learn"
    confirm_learning = client.post(
        "/events/message",
        json={
            "text": CONFIRM_TEXT,
            "channel": "self-test",
            "user_id": "cmt-user",
            "session_id": "cmt-learn",
        },
    )
    expect(confirm_learning.status_code == 200, "confirm learning digest", confirm_learning.text)
    expect(
        (confirm_learning.json().get("artifacts") or {}).get("commitment", {}).get("status") == "confirmed",
        "learning confirmation activates commitment",
        confirm_learning.json().get("artifacts"),
    )
    expect(
        confirm_learning.json().get("route") == "direct_answer"
        and (
            (confirm_learning.json().get("artifacts") or {})
            .get("decision", {})
            .get("model_assist", {})
            .get("semantic_policy", {})
            .get("allowed_effects")
            == ["commitment.mutate"]
        )
        and not (confirm_learning.json().get("artifacts") or {}).get("execution_result"),
        "commitment confirmation follows semantic authority without Agent execution",
        confirm_learning.json(),
    )
    learning_list = client.get(
        "/commitments",
        params={
            "user_id": "cmt-user",
            "session_id": learn_session,
            "status": "active",
        },
    )
    expect(learning_list.status_code == 200 and learning_list.json().get("count", 0) >= 1, "active learning commitment exists", learning_list.json())
    learn_id = learning_list.json()["commitments"][0]["commitment_id"]
    recall_learning = client.post(
        "/events/message",
        json={
            "text": LEARNING_RECALL_TEXT,
            "channel": "self-test",
            "user_id": "cmt-user",
            "session_id": "cmt-learn",
        },
    ).json()
    expect(
        recall_learning.get("route") == "direct_answer"
        and "深度学习" in str(recall_learning.get("response") or "")
        and not (recall_learning.get("artifacts") or {}).get("execution_result"),
        "learning recall is answered from Veyra state without Agent routing",
        recall_learning,
    )

    weather = client.post(
        "/events/message",
        json={
            "text": WEATHER_TEXT,
            "channel": "self-test",
            "user_id": "cmt-user",
            "session_id": "cmt-weather",
        },
    )
    expect(weather.status_code == 200, "weather message", weather.text)
    body = weather.json()
    expect(body.get("route") in {"direct_answer", "probe", "agent", "ask_user"}, "weather request stays in safe route", body)
    expect("已开启" in message_text(body), "explicit weather request creates active subscription", body)
    primary = str(body.get("response") or "")
    expect(
        "已开启" in primary and not body.get("followup_messages"),
        "complete weather request activates without extra confirmation",
        body,
    )
    weather_outbox = [
        item
        for item in store.read_json("channel_state.json").get("outbox", [])
        if isinstance(item, dict)
        and item.get("session_id")
        == SessionMapper().map(
            "self-test",
            "cmt-user",
            "cmt-weather",
        )
    ][-1:]
    message_types = [(item.get("metadata") or {}).get("message_type") for item in weather_outbox]
    expect(message_types == ["primary"], "weather turn sends active confirmation once", weather_outbox)
    weather_offer = (body.get("artifacts") or {}).get("commitment", {}).get("commitment", {})
    expect("response_override" not in ((body.get("artifacts") or {}).get("commitment") or {}), "commitment artifact has no response_override", body.get("artifacts"))
    expect((weather_offer.get("payload") or {}).get("location") == "北京", "weather commitment extracts clean location", weather_offer)
    expect(weather_offer.get("status") == "active", "weather commitment is active after explicit complete request", weather_offer)
    user_world_after_create = store.read_json("user_world.json")
    profile_after_create = (
        (user_world_after_create.get("profiles_by_user") or {}).get("cmt-user", {})
        if isinstance(user_world_after_create.get("profiles_by_user"), dict)
        else {}
    )
    expect(
        not profile_after_create.get("current_goal")
        and not (profile_after_create.get("preferences") or {}).get("default_location"),
        "semantic commitment creation does not silently widen into profile.write",
        user_world_after_create,
    )

    mapped_session = (
        weather_offer.get("session_id")
        or "self-test:cmt-user:cmt-weather"
    )
    listed = client.get(
        "/commitments",
        params={
            "user_id": "cmt-user",
            "session_id": mapped_session,
            "status": "active",
        },
    )
    expect(listed.status_code == 200 and listed.json().get("count", 0) >= 1, "active commitment exists", listed.json())

    commitment_id = listed.json()["commitments"][0]["commitment_id"]

    past = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    state = store.read_json("user_commitments.json")
    for item in state.get("commitments", []):
        if item.get("commitment_id") in {commitment_id, learn_id}:
            item["next_run_at"] = past
            item["status"] = "active"
    store.write_json("user_commitments.json", state)

    pushed = client.post("/commitments/run-due", json={"limit": 5, "reason": "smoke"})
    expect(pushed.status_code == 200, "run due pushes", pushed.text)
    expect(pushed.json().get("processed_count", 0) >= 1, "at least one push processed", pushed.json())

    cron = client.post("/runtime/cron/run", json={"job_id": "commitment_push_due", "reason": "smoke_cron"})
    expect(cron.status_code == 200, "cron commitment job", cron.text)

    tick = client.post("/runtime/active-loop/tick", json={"reason": "smoke"})
    expect(tick.status_code == 200, "active loop tick", tick.text)
    step_names = [step.get("name") for step in tick.json().get("steps", []) if isinstance(step, dict)]
    expect("commitment_push" in step_names, "active loop includes commitment_push step", step_names)

    user_world = store.read_json("user_world.json")
    scoped = (user_world.get("profiles_by_user") or {}).get("cmt-user") if isinstance(user_world.get("profiles_by_user"), dict) else {}
    expect(
        bool(scoped.get("current_goal")),
        "runtime push projects the active commitment into scoped operational state",
        user_world,
    )

    print("commitment smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
