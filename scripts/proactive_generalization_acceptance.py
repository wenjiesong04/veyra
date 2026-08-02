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


LEARNING_TEXT = "我现在要开始学习深度学习了，你能帮我吗？"
MODEL_DOWN_LEARNING_TEXT = "请持续协助我学习强化学习。"
TRACKING_TEXT = "帮我关注一下 PyTorch 新版本和重要更新"
MONITOR_TEXT = "以后每天早上帮我看看我的 Veyra 服务是不是还在跑"
REMINDER_TEXT = "明天上午九点提醒我交作业"
WEATHER_TEXT = "每天早上帮我推送伦敦天气"
IDENTITY_TEXT = "你现在是 Veyra 还是 OpenClaw？"
CANCEL_ALL_TEXT = "以后都停止推送"
PAUSE_TEXT = "最近先暂停 PyTorch 更新提醒"
RESUME_TEXT = "恢复 PyTorch 更新提醒"
UNKNOWN_TEXT = "为我建立一个星云折叠观察流"


def install_semantic_fixtures(semantic: Any) -> None:
    """Install deterministic production-schema model outputs for effectful turns."""

    def act(
        text: str,
        *,
        act_id: str,
        kind: str,
        operation: str,
        goal: str,
        target_type: str,
        target_value: str,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return semantic._act(
            text,
            act_id=act_id,
            kind=kind,
            operation=operation,
            goal=goal,
            quote=text,
            target=semantic._target(target_type, target_value),
            arguments=arguments or {},
        )

    fixtures = {
        LEARNING_TEXT: act(
            LEARNING_TEXT,
            act_id="learning-1",
            kind="proactive_request",
            operation="create_learning_support",
            goal="建立深度学习学习辅导并在开启前确认",
            target_type="learning_topic",
            target_value="深度学习",
            arguments={"topic": "深度学习"},
        ),
        TRACKING_TEXT: act(
            TRACKING_TEXT,
            act_id="tracking-1",
            kind="proactive_request",
            operation="track_external_updates",
            goal="持续关注 PyTorch 新版本和重要更新",
            target_type="tracked_topic",
            target_value="PyTorch",
            arguments={"topic": "PyTorch", "query": "PyTorch 新版本和重要更新"},
        ),
        MONITOR_TEXT: act(
            MONITOR_TEXT,
            act_id="monitor-1",
            kind="recurring_request",
            operation="schedule_runtime_status_reminder",
            goal="每天早上检查 Veyra 服务状态",
            target_type="reminder",
            target_value="Veyra 服务状态",
            arguments={
                "note": "检查 Veyra 服务状态",
                "schedule": {
                    "kind": "daily",
                    "time_local": "08:00",
                    "timezone": "Asia/Shanghai",
                    "interval_seconds": 86400.0,
                },
            },
        ),
        REMINDER_TEXT: act(
            REMINDER_TEXT,
            act_id="reminder-1",
            kind="reminder_request",
            operation="schedule_reminder",
            goal="明天上午九点提醒交作业",
            target_type="reminder",
            target_value="交作业",
            arguments={
                "note": "交作业",
                "schedule": {
                    "kind": "once",
                    "relative_day": "tomorrow",
                    "time_local": "09:00",
                    "timezone": "Asia/Shanghai",
                    "interval_seconds": 86400.0,
                },
            },
        ),
        WEATHER_TEXT: act(
            WEATHER_TEXT,
            act_id="weather-1",
            kind="recurring_request",
            operation="schedule_weather_push",
            goal="每天早上接收伦敦天气",
            target_type="weather_summary",
            target_value="伦敦",
            arguments={
                "location": "伦敦",
                "schedule": {
                    "kind": "daily",
                    "time_local": "08:00",
                    "timezone": "Asia/Shanghai",
                    "interval_seconds": 86400.0,
                },
            },
        ),
        IDENTITY_TEXT: act(
            IDENTITY_TEXT,
            act_id="identity-1",
            kind="question",
            operation="query_identity",
            goal="确认当前交互主体身份",
            target_type="identity",
            target_value="Veyra",
        ),
        CANCEL_ALL_TEXT: act(
            CANCEL_ALL_TEXT,
            act_id="cancel-1",
            kind="commitment_control",
            operation="cancel_all_commitments",
            goal="停止当前用户的全部主动推送",
            target_type="commitment",
            target_value="all",
            arguments={"scope": "all"},
        ),
        PAUSE_TEXT: act(
            PAUSE_TEXT,
            act_id="pause-1",
            kind="commitment_control",
            operation="pause_commitment",
            goal="暂停 PyTorch 更新提醒",
            target_type="commitment",
            target_value="PyTorch",
            arguments={"topic": "PyTorch", "scope": "matching"},
        ),
        RESUME_TEXT: act(
            RESUME_TEXT,
            act_id="resume-1",
            kind="commitment_control",
            operation="resume_commitment",
            goal="恢复 PyTorch 更新提醒",
            target_type="commitment",
            target_value="PyTorch",
            arguments={"topic": "PyTorch", "scope": "matching"},
        ),
    }
    semantic.FRAME_FIXTURES.update(
        {text: semantic._frame(acts=[semantic_act]) for text, semantic_act in fixtures.items()}
    )


def main() -> int:
    with TemporaryDirectory(prefix="veyra-proactive-generalization-") as tmp:
        state_root = Path(tmp) / "state"
        agency_root = Path(tmp) / "agency"
        os.environ["VEYRA_STATE_DIR"] = str(state_root)
        os.environ["VEYRA_STATE_ROOT"] = str(state_root)
        os.environ["VEYRA_AGENCY_DIR"] = str(agency_root)
        os.environ["VEYRA_AGENCY_ROOT"] = str(agency_root)
        os.environ["VEYRA_CORE_MODEL_ENABLED"] = "0"
        os.environ["VEYRA_ACTIVE_LOOP_AUTOSTART"] = "0"
        os.environ["VEYRA_FEISHU_WS_AUTOSTART"] = "0"
        agency_root.mkdir(parents=True, exist_ok=True)
        (agency_root / "goals.json").write_text("{}", encoding="utf-8")

        import scripts.semantic_generalization_smoke as semantic_smoke  # noqa: E402
        import main as app_module  # noqa: E402

        install_semantic_fixtures(semantic_smoke)
        semantic_model = semantic_smoke.ScriptedSemanticModelClient(
            fail_for={MODEL_DOWN_LEARNING_TEXT, UNKNOWN_TEXT}
        )
        app_module.awareness_loop.core_reasoning.client = semantic_model
        app_module.commitment_core.intent_planner.client = semantic_model

        client = TestClient(app_module.app)
        store = app_module.state_store

        before_model_down = len(store.read_json("user_commitments.json").get("commitments", []))
        model_down = client.post(
            "/events/message",
            json={
                "text": MODEL_DOWN_LEARNING_TEXT,
                "channel": "accept",
                "user_id": "model-down-user",
                "session_id": "model-down",
            },
        ).json()
        expect(
            len(store.read_json("user_commitments.json").get("commitments", [])) == before_model_down,
            "model outage cannot authorize a commitment from raw text",
            model_down,
        )

        learning = client.post(
            "/events/message",
            json={
                "text": LEARNING_TEXT,
                "channel": "accept",
                "user_id": "learn-user",
                "session_id": "learn",
            },
        ).json()
        learning_artifact = artifact(learning)
        expect(learning_artifact.get("status") == "created", "learning commitment recorded", learning_artifact)
        expect(learning_artifact.get("semantic_source") == "authoritative_semantic_frame", "learning uses semantic authority", learning_artifact)
        expect((learning_artifact.get("commitment") or {}).get("kind") == "learning_digest", "learning commitment typed", learning_artifact)
        expect((learning_artifact.get("commitment") or {}).get("status") == "pending_confirmation", "learning digest pending", learning_artifact)
        expect(not store.read_json("user_goals.json").get("goals"), "learning request does not widen into a separate goal write", store.read_json("user_goals.json"))

        external = client.post(
            "/events/message",
            json={"text": TRACKING_TEXT, "channel": "accept", "user_id": "track-user", "session_id": "track"},
        ).json()
        external_artifact = artifact(external)
        expect(external_artifact.get("semantic_source") == "authoritative_semantic_frame", "external tracking uses semantic authority", external_artifact)
        expect((external_artifact.get("commitment") or {}).get("kind") == "external_digest", "external tracking commitment typed", external_artifact)
        expect((external_artifact.get("commitment") or {}).get("status") == "pending_confirmation", "external tracking pending", external_artifact)
        expect(not store.read_json("external_world.json").get("watchlist"), "unconfirmed tracking creates no watchlist", store.read_json("external_world.json"))
        expect(
            "回复" in message_text(external) and "好的" in message_text(external),
            "external tracking asks for authorization",
            external,
        )

        monitor = client.post(
            "/events/message",
            json={
                "text": MONITOR_TEXT,
                "channel": "accept",
                "user_id": "monitor-user",
                "session_id": "monitor",
            },
        ).json()
        monitor_artifact = artifact(monitor)
        expect(monitor_artifact.get("semantic_source") == "authoritative_semantic_frame", "local monitor uses semantic authority", monitor_artifact)
        expect((monitor_artifact.get("commitment") or {}).get("kind") == "generic_reminder", "local monitor uses bounded reminder contract", monitor_artifact)
        expect((monitor_artifact.get("commitment") or {}).get("status") == "active", "explicit scheduled local monitor is active", monitor_artifact)
        expect((monitor_artifact.get("commitment") or {}).get("payload", {}).get("note") == "检查 Veyra 服务状态", "local monitor retains bounded note", monitor_artifact)

        watchlist_count = len(store.read_json("external_world.json").get("watchlist", []))
        reminder = client.post(
            "/events/message",
            json={"text": REMINDER_TEXT, "channel": "accept", "user_id": "reminder-user", "session_id": "reminder"},
        ).json()
        reminder_artifact = artifact(reminder)
        expect(reminder_artifact.get("semantic_source") == "authoritative_semantic_frame", "reminder uses semantic authority", reminder_artifact)
        expect((reminder_artifact.get("commitment") or {}).get("kind") == "generic_reminder", "reminder commitment created", reminder_artifact)
        expect((reminder_artifact.get("commitment") or {}).get("status") == "active", "explicit reminder is active", reminder_artifact)
        expect(len(store.read_json("external_world.json").get("watchlist", [])) == watchlist_count, "reminder creates no external watchlist", store.read_json("external_world.json"))

        weather = client.post(
            "/events/message",
            json={"text": WEATHER_TEXT, "channel": "accept", "user_id": "weather-user", "session_id": "weather"},
        ).json()
        weather_artifact = artifact(weather)
        expect("已开启" in message_text(weather), "explicit weather request activates daily push", weather)
        expect((weather_artifact.get("commitment") or {}).get("status") == "active", "weather push active", weather_artifact)

        intents_before_identity = len(store.read_json("proactive_intents.json").get("intents", []))
        proposals_before_identity = len(store.read_json("self_improvement_proposals.json").get("proposals", []))
        identity = client.post(
            "/events/message",
            json={"text": IDENTITY_TEXT, "channel": "accept", "user_id": "identity-user", "session_id": "identity"},
        ).json()
        expect(identity.get("route") == "direct_answer", "identity query stays on the read-only core route", identity)
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
            json={"text": CANCEL_ALL_TEXT, "channel": "api", "user_id": "cancel-user", "session_id": "cancel"},
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
            json={"text": PAUSE_TEXT, "channel": "api", "user_id": "pause-user", "session_id": "pause"},
        ).json()
        expect(commitment_statuses(store, {pytorch["commitment_id"]})[pytorch["commitment_id"]] == "paused", "pause matching PyTorch commitment", pause)
        resume = client.post(
            "/events/message",
            json={"text": RESUME_TEXT, "channel": "api", "user_id": "pause-user", "session_id": "pause"},
        ).json()
        expect(commitment_statuses(store, {pytorch["commitment_id"]})[pytorch["commitment_id"]] == "active", "resume matching PyTorch commitment", resume)

        unknown_before = len(store.read_json("user_commitments.json").get("commitments", []))
        unknown = client.post(
            "/events/message",
            json={"text": UNKNOWN_TEXT, "channel": "api", "user_id": "unknown-user", "session_id": "unknown"},
        ).json()
        unknown_artifact = artifact(unknown)
        expect(not unknown_artifact, "model-down unknown request has no authorized commitment artifact", unknown_artifact)
        expect(len(store.read_json("user_commitments.json").get("commitments", [])) == unknown_before, "unknown creates no active commitment", unknown)

        from core.proactive_intent import ProactiveIntent  # noqa: E402

        unknown_intent = ProactiveIntent(
            user_id="unknown-user",
            session_id="unknown",
            channel_id="api",
            raw_text=UNKNOWN_TEXT,
            intent_type="unknown",
            topic="星云折叠观察流",
            confidence=0.98,
            source="validated_semantic_unknown",
        )
        gap = app_module.commitment_core.self_improvement.propose_from_intent(
            unknown_intent,
            reason="unknown_proactive_intent",
        )
        expect(bool(gap.get("proposal_id")), "validated unknown capability records a reviewable gap", gap)
        expect(
            gap.get("status") == "draft" and gap.get("requires_human_review") is True,
            "unknown gap cannot self-promote",
            gap,
        )

    print("proactive generalization acceptance passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
