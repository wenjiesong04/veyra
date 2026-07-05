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
TEST_AGENCY_ROOT.mkdir(parents=True, exist_ok=True)
(TEST_AGENCY_ROOT / "goals.json").write_text("{}", encoding="utf-8")
(TEST_AGENCY_ROOT / "intention_queue.json").write_text("[]", encoding="utf-8")

from main import app  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402


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
            "text": "现在我要开始学习深度学习了，你能不能帮我？",
            "channel": "self-test",
            "user_id": "cmt-user",
            "session_id": "cmt-learn",
        },
    )
    expect(learning_turn.status_code == 200, "learning goal message", learning_turn.text)
    learning_body = learning_turn.json()
    learning_artifact = (learning_body.get("artifacts") or {}).get("commitment") or {}
    expect(learning_artifact.get("status") == "goal_recorded", "learning goal recorded", learning_artifact)
    expect("学习目标" in message_text(learning_body) and "同意开启" in message_text(learning_body), "learning response includes plan and opt-in", learning_body)

    goals = store.read_json("user_goals.json").get("goals", [])
    learning_goal = next((goal for goal in goals if isinstance(goal, dict) and goal.get("topic") == "深度学习"), None)
    expect(bool(learning_goal), "user_goals stores learning topic", goals)
    expect((learning_goal.get("permissions") or {}).get("proactive_push") == "pending_confirmation", "learning push requires confirmation", learning_goal)
    pending_digest = learning_artifact.get("commitment") if isinstance(learning_artifact.get("commitment"), dict) else {}
    expect(pending_digest.get("status") == "pending_confirmation", "learning digest is pending before consent", pending_digest)

    learn_session = pending_digest.get("session_id") or "self-test:cmt-user:cmt-learn"
    confirm_learning = client.post(
        "/events/message",
        json={
            "text": "好的",
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
    learning_list = client.get("/commitments", params={"session_id": learn_session, "status": "active"})
    expect(learning_list.status_code == 200 and learning_list.json().get("count", 0) >= 1, "active learning commitment exists", learning_list.json())
    learn_id = learning_list.json()["commitments"][0]["commitment_id"]
    goals = store.read_json("user_goals.json").get("goals", [])
    learning_goal = next((goal for goal in goals if isinstance(goal, dict) and goal.get("topic") == "深度学习"), {})
    expect((learning_goal.get("permissions") or {}).get("proactive_push") == "granted", "confirmed learning updates goal permissions", learning_goal)

    weather = client.post(
        "/events/message",
        json={
            "text": "每天早上帮我推送北京天气",
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
        if isinstance(item, dict) and item.get("session_id") == "self-test:cmt-user:cmt-weather"
    ][-1:]
    message_types = [(item.get("metadata") or {}).get("message_type") for item in weather_outbox]
    expect(message_types == ["primary"], "weather turn sends active confirmation once", weather_outbox)
    weather_offer = (body.get("artifacts") or {}).get("commitment", {}).get("commitment", {})
    expect("response_override" not in ((body.get("artifacts") or {}).get("commitment") or {}), "commitment artifact has no response_override", body.get("artifacts"))
    expect((weather_offer.get("payload") or {}).get("location") == "北京", "weather commitment extracts clean location", weather_offer)
    expect(weather_offer.get("status") == "active", "weather commitment is active after explicit complete request", weather_offer)

    mapped_session = (
        weather_offer.get("session_id")
        or "self-test:cmt-user:cmt-weather"
    )
    listed = client.get("/commitments", params={"session_id": mapped_session, "status": "active"})
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
    expect(bool(scoped.get("current_goal")), "scoped user_world reflects commitment goal", user_world)

    print("commitment smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
