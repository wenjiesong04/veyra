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
os.environ["VEYRA_STATE_ROOT"] = str(TEST_STATE_ROOT)
os.environ["VEYRA_AGENCY_ROOT"] = str(TEST_AGENCY_ROOT)
TEST_AGENCY_ROOT.mkdir(parents=True, exist_ok=True)
(TEST_AGENCY_ROOT / "goals.json").write_text("{}", encoding="utf-8")
(TEST_AGENCY_ROOT / "intention_queue.json").write_text("[]", encoding="utf-8")

from main import app  # noqa: E402


client = TestClient(app)


def expect(condition: bool, label: str, detail=None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


def main() -> int:
    weather = client.post(
        "/events/message",
        json={
            "text": "今天北京天气怎么样",
            "channel": "self-test",
            "user_id": "cmt-user",
            "session_id": "cmt-weather",
        },
    )
    expect(weather.status_code == 200, "weather message", weather.text)
    body = weather.json()
    expect(body.get("route") == "probe", "weather routes to probe", body)
    expect("推送" in str(body.get("response") or ""), "weather response offers subscription", body.get("response"))

    mapped_session = (
        (body.get("artifacts") or {}).get("commitment", {}).get("commitment", {}).get("session_id")
        or "self-test:cmt-user:cmt-weather"
    )
    confirm = client.post(
        "/events/message",
        json={
            "text": "好的",
            "channel": "self-test",
            "user_id": "cmt-user",
            "session_id": "cmt-weather",
        },
    )
    expect(confirm.status_code == 200, "confirm subscription", confirm.text)
    expect(
        (confirm.json().get("artifacts") or {}).get("commitment", {}).get("status") == "confirmed",
        "confirm turn status",
        confirm.json().get("artifacts"),
    )
    listed = client.get("/commitments", params={"session_id": mapped_session, "status": "active"})
    expect(listed.status_code == 200 and listed.json().get("count", 0) >= 1, "active commitment exists", listed.json())

    commitment_id = listed.json()["commitments"][0]["commitment_id"]

    learning = client.post(
        "/commitments",
        json={
            "kind": "learning_digest",
            "title": "学习辅导：深度学习",
            "user_id": "cmt-user",
            "channel": "self-test",
            "session_id": "self-test:cmt-user:cmt-learn",
            "status": "active",
            "schedule": {"kind": "interval", "interval_seconds": 60, "timezone": "Asia/Shanghai"},
            "payload": {"topic": "深度学习", "phase": "入门"},
        },
    )
    expect(learning.status_code == 200, "create learning commitment", learning.text)
    learn_id = learning.json()["commitment"]["commitment_id"]

    past = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    from core.world_state import WorldStateStore

    store = WorldStateStore(TEST_STATE_ROOT)
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
    expect(bool(user_world.get("current_goal")), "user_world reflects commitment goal", user_world)

    print("commitment smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
