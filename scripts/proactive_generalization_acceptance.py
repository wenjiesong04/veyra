#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def expect(condition: bool, label: str, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


def artifact(body: dict[str, Any]) -> dict[str, Any]:
    return (body.get("artifacts") or {}).get("commitment") or {}


def message_text(body: dict[str, Any]) -> str:
    return "\n".join([str(body.get("response") or ""), *[str(item) for item in body.get("followup_messages", []) if item]])


def commitment_statuses(store: Any, ids: set[str]) -> dict[str, str]:
    state = store.read_json("user_commitments.json")
    items = state.get("commitments") if isinstance(state.get("commitments"), list) else []
    return {str(item.get("commitment_id")): str(item.get("status")) for item in items if isinstance(item, dict) and item.get("commitment_id") in ids}


def main() -> int:
    with TemporaryDirectory(prefix="veyra-proactive-generalization-") as tmp:
        state_root = Path(tmp) / "state"
        agency_root = Path(tmp) / "agency"
        os.environ["VEYRA_STATE_DIR"] = str(state_root)
        os.environ["VEYRA_STATE_ROOT"] = str(state_root)
        os.environ["VEYRA_AGENCY_DIR"] = str(agency_root)
        os.environ["VEYRA_AGENCY_ROOT"] = str(agency_root)
        agency_root.mkdir(parents=True, exist_ok=True)
        (agency_root / "goals.json").write_text("{}", encoding="utf-8")

        import main as app_module  # noqa: E402

        client = TestClient(app_module.app)
        store = app_module.state_store

        learning = client.post(
            "/events/message",
            json={
                "text": "我现在要开始学习深度学习了，你能帮我吗？",
                "channel": "accept",
                "user_id": "learn-user",
                "session_id": "learn",
            },
        ).json()
        learning_artifact = artifact(learning)
        expect(learning_artifact.get("status") == "goal_recorded", "learning goal recorded", learning_artifact)
        expect((learning_artifact.get("intent") or {}).get("intent_type") == "learning_plan", "learning intent typed", learning_artifact)
        expect((learning_artifact.get("commitment") or {}).get("status") == "pending_confirmation", "learning digest pending", learning_artifact)
        expect((learning_artifact.get("watchlist_draft") or {}).get("status") == "pending_confirmation", "learning watchlist draft created", learning_artifact)

        external = client.post(
            "/events/message",
            json={"text": "帮我关注一下 PyTorch 新版本和重要更新", "channel": "accept", "user_id": "track-user", "session_id": "track"},
        ).json()
        external_artifact = artifact(external)
        expect((external_artifact.get("intent") or {}).get("intent_type") == "track_external_topic", "external tracking intent typed", external_artifact)
        expect((external_artifact.get("watchlist_draft") or {}).get("status") == "pending_confirmation", "external watchlist draft pending", external_artifact)
        expect("回复「好的」" in message_text(external), "external tracking asks for authorization", external)

        monitor = client.post(
            "/events/message",
            json={
                "text": "以后每天早上帮我看看我的 Veyra 服务是不是还在跑",
                "channel": "accept",
                "user_id": "monitor-user",
                "session_id": "monitor",
            },
        ).json()
        monitor_artifact = artifact(monitor)
        expect((monitor_artifact.get("intent") or {}).get("intent_type") == "monitor_local_state", "local monitor intent typed", monitor_artifact)
        expect((monitor_artifact.get("commitment") or {}).get("status") == "pending_confirmation", "local monitor pending confirmation", monitor_artifact)
        expect((monitor_artifact.get("commitment") or {}).get("payload", {}).get("probe_family") == "runtime_status", "local monitor uses safe probe family", monitor_artifact)

        watchlist_count = len(store.read_json("external_world.json").get("watchlist", []))
        reminder = client.post(
            "/events/message",
            json={"text": "明天上午九点提醒我交作业", "channel": "accept", "user_id": "reminder-user", "session_id": "reminder"},
        ).json()
        reminder_artifact = artifact(reminder)
        expect((reminder_artifact.get("intent") or {}).get("intent_type") == "reminder", "reminder intent typed", reminder_artifact)
        expect((reminder_artifact.get("commitment") or {}).get("kind") == "generic_reminder", "reminder commitment created", reminder_artifact)
        expect((reminder_artifact.get("commitment") or {}).get("status") == "active", "explicit reminder is active", reminder_artifact)
        expect(len(store.read_json("external_world.json").get("watchlist", [])) == watchlist_count, "reminder creates no external watchlist", store.read_json("external_world.json"))

        weather = client.post(
            "/events/message",
            json={"text": "每天早上帮我推送伦敦天气", "channel": "accept", "user_id": "weather-user", "session_id": "weather"},
        ).json()
        weather_artifact = artifact(weather)
        expect("已开启" in message_text(weather), "explicit weather request activates daily push", weather)
        expect((weather_artifact.get("commitment") or {}).get("status") == "active", "weather push active", weather_artifact)

        intents_before_identity = len(store.read_json("proactive_intents.json").get("intents", []))
        proposals_before_identity = len(store.read_json("self_improvement_proposals.json").get("proposals", []))
        identity = client.post(
            "/events/message",
            json={"text": "你现在是 Veyra 还是 OpenClaw？", "channel": "accept", "user_id": "identity-user", "session_id": "identity"},
        ).json()
        expect("我是 Veyra" in str(identity.get("response") or ""), "identity answer is not overridden by proactive planner", identity)
        expect(not artifact(identity), "identity answer creates no commitment artifact", artifact(identity))
        expect(len(store.read_json("proactive_intents.json").get("intents", [])) == intents_before_identity, "identity answer records no proactive intent", identity)
        expect(len(store.read_json("self_improvement_proposals.json").get("proposals", [])) == proposals_before_identity, "identity answer records no self-improvement proposal", identity)

        cancel_weather = client.post(
            "/commitments",
            json={"kind": "weather_daily", "status": "active", "user_id": "cancel-user", "channel": "api", "session_id": "cancel", "payload": {"location": "北京", "topic": "weather"}},
        ).json()["commitment"]
        cancel_learning = client.post(
            "/commitments",
            json={"kind": "learning_digest", "status": "active", "user_id": "cancel-user", "channel": "api", "session_id": "cancel", "payload": {"topic": "深度学习"}},
        ).json()["commitment"]
        before_cancel_count = len(store.read_json("user_commitments.json").get("commitments", []))
        cancel = client.post(
            "/events/message",
            json={"text": "以后都停止推送", "channel": "api", "user_id": "cancel-user", "session_id": "cancel"},
        ).json()
        statuses = commitment_statuses(store, {cancel_weather["commitment_id"], cancel_learning["commitment_id"]})
        expect(set(statuses.values()) == {"cancelled"}, "cancel all proactive pushes", statuses)
        expect(len(store.read_json("user_commitments.json").get("commitments", [])) == before_cancel_count, "cancel creates no new commitment", cancel)

        pytorch = client.post(
            "/commitments",
            json={"kind": "external_digest", "status": "active", "user_id": "pause-user", "channel": "api", "session_id": "pause", "payload": {"topic": "PyTorch"}},
        ).json()["commitment"]
        pause = client.post(
            "/events/message",
            json={"text": "最近先暂停 PyTorch 更新提醒", "channel": "api", "user_id": "pause-user", "session_id": "pause"},
        ).json()
        expect(commitment_statuses(store, {pytorch["commitment_id"]})[pytorch["commitment_id"]] == "paused", "pause matching PyTorch commitment", pause)
        resume = client.post(
            "/events/message",
            json={"text": "恢复 PyTorch 更新提醒", "channel": "api", "user_id": "pause-user", "session_id": "pause"},
        ).json()
        expect(commitment_statuses(store, {pytorch["commitment_id"]})[pytorch["commitment_id"]] == "active", "resume matching PyTorch commitment", resume)

        unknown_before = len(store.read_json("user_commitments.json").get("commitments", []))
        unknown = client.post(
            "/events/message",
            json={"text": "帮我持续关注这个奇怪的新领域 X", "channel": "api", "user_id": "unknown-user", "session_id": "unknown"},
        ).json()
        unknown_artifact = artifact(unknown)
        expect(unknown_artifact.get("status") == "intent_draft", "unknown creates intent draft", unknown_artifact)
        expect((unknown_artifact.get("intent") or {}).get("intent_type") == "unknown", "unknown remains unknown", unknown_artifact)
        expect(bool(unknown_artifact.get("self_improvement_proposal")), "unknown records self-improvement proposal", unknown_artifact)
        expect(len(store.read_json("user_commitments.json").get("commitments", [])) == unknown_before, "unknown creates no active commitment", unknown_artifact)

    print("proactive generalization acceptance passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
