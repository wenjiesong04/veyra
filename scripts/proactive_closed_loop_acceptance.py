#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from probes.schema import probe_payload  # noqa: E402


class FakeSearchProbe:
    def run(self, query: str, *, max_results: int = 5) -> dict[str, Any]:
        return probe_payload(
            probe="search_probe",
            target=query,
            status="ok",
            summary=f"fake search for {query}",
            confidence=0.9,
            ttl_seconds=1800,
            details={
                "query": query,
                "results": [
                    {
                        "title": "Deep Learning latest practical course 2026",
                        "url": "https://www.deeplearning.ai/courses/deep-learning",
                        "snippet": "Updated deep learning course path.",
                        "source": "www.deeplearning.ai",
                    }
                ],
            },
        )


class SentAdapter:
    sent_messages: list[dict[str, Any]] = []

    def __init__(self, state_store: Any = None, channel: str = "api") -> None:
        self.state_store = state_store
        self.channel = channel

    def send(self, session_id: str, message: str, *, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        self.sent_messages.append({"session_id": session_id, "message": message, "metadata": metadata or {}})
        return {"status": "sent", "delivery_status": "provider_sent", "provider": "acceptance"}


def expect(condition: bool, label: str, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


def message_text(body: dict[str, Any]) -> str:
    return "\n".join([str(body.get("response") or ""), *[str(item) for item in body.get("followup_messages", []) if item]])


def past_iso() -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()


LEARNING_TEXT = "现在我要开始学习深度学习了，你能不能帮我？"
CONFIRM_TEXT = "好的"
WEATHER_TEXT = "每天早上帮我推送北京天气"


def install_semantic_fixtures(semantic: Any) -> None:
    """Drive the closed loop through validated production semantic frames."""

    semantic.FRAME_FIXTURES[LEARNING_TEXT] = semantic._frame(
        acts=[
            semantic._act(
                LEARNING_TEXT,
                act_id="closed-learning-1",
                kind="proactive_request",
                operation="create_learning_support",
                goal="建立深度学习学习辅导并在开启前确认",
                quote=LEARNING_TEXT,
                target=semantic._target("learning_topic", "深度学习"),
                arguments={"topic": "深度学习"},
            )
        ]
    )
    semantic.FRAME_FIXTURES[CONFIRM_TEXT] = semantic._frame(
        acts=[
            semantic._act(
                CONFIRM_TEXT,
                act_id="closed-confirm-1",
                kind="commitment_control",
                operation="confirm_commitment",
                goal="确认当前会话中的待确认学习辅导",
                quote=CONFIRM_TEXT,
                target=semantic._target("commitment", "current_session"),
            )
        ]
    )
    semantic.FRAME_FIXTURES[WEATHER_TEXT] = semantic._frame(
        acts=[
            semantic._act(
                WEATHER_TEXT,
                act_id="closed-weather-1",
                kind="recurring_request",
                operation="schedule_weather_push",
                goal="每天早上接收北京天气",
                quote=WEATHER_TEXT,
                target=semantic._target("weather_summary", "北京"),
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


def main() -> int:
    with TemporaryDirectory(prefix="veyra-closed-loop-") as tmp:
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
        import runtime.commitment_push as commitment_push_module  # noqa: E402

        install_semantic_fixtures(semantic_smoke)
        semantic_model = semantic_smoke.ScriptedSemanticModelClient()
        app_module.awareness_loop.core_reasoning.client = semantic_model
        app_module.commitment_core.intent_planner.client = semantic_model

        client = TestClient(app_module.app)
        app_module.external_world_refresh.search_probe = FakeSearchProbe()
        store = app_module.state_store

        learning = client.post(
            "/events/message",
            json={
                "text": LEARNING_TEXT,
                "channel": "acceptance",
                "user_id": "closed-loop-user",
                "session_id": "learn",
            },
        )
        expect(learning.status_code == 200, "learning request accepted", learning.text)
        learning_body = learning.json()
        artifact = (learning_body.get("artifacts") or {}).get("commitment") or {}
        expect(artifact.get("status") == "created", "learning commitment recorded", artifact)
        expect(artifact.get("semantic_source") == "authoritative_semantic_frame", "learning is authorized by semantic frame", artifact)
        pending = artifact.get("commitment") if isinstance(artifact.get("commitment"), dict) else {}
        expect(pending.get("kind") == "learning_digest", "learning commitment uses bounded digest kind", pending)
        expect(pending.get("status") == "pending_confirmation", "learning push pending before consent", pending)
        expect(not store.read_json("user_goals.json").get("goals"), "learning request does not silently create a separate durable goal", store.read_json("user_goals.json"))

        confirm = client.post(
            "/events/message",
            json={"text": CONFIRM_TEXT, "channel": "acceptance", "user_id": "closed-loop-user", "session_id": "learn"},
        )
        expect(confirm.status_code == 200 and (confirm.json().get("artifacts") or {}).get("commitment", {}).get("status") == "confirmed", "learning push confirmed", confirm.text)
        active_learning = client.get(
            "/commitments",
            params={
                "user_id": "closed-loop-user",
                "session_id": pending["session_id"],
                "status": "active",
            },
        ).json()["commitments"][0]

        def install_authorized_learning_watch(external_state: dict[str, Any]) -> dict[str, Any]:
            watchlist = external_state.get("watchlist") if isinstance(external_state.get("watchlist"), list) else []
            watchlist.append(
                {
                    "target": f"learning:{active_learning['commitment_id']}",
                    "kind": "learning_search",
                    "enabled": True,
                    "topic": "深度学习",
                    "query": "深度学习 latest tutorial course paper 2026",
                    "user_id": active_learning["user_id"],
                    "session_id": active_learning["session_id"],
                    "commitment_id": active_learning["commitment_id"],
                    "reason": "authorized_commitment_acceptance_fixture",
                }
            )
            external_state["watchlist"] = watchlist
            return external_state

        store.mutate_json("external_world.json", install_authorized_learning_watch)

        external = client.post("/external/refresh", params={"limit": 5})
        expect(
            external.status_code == 200
            and external.json().get("schema_version")
            == "veyra.external_refresh.aggregate.v1"
            and external.json().get("refreshed_count", 0) > 0,
            "authorized external search refreshed",
            external.text,
        )
        external_state = store.read_json("external_world.json")
        expect(external_state.get("knowledge_items") and external_state.get("push_candidates"), "external knowledge and candidates recorded", external_state)

        commitments_state = store.read_json("user_commitments.json")
        for item in commitments_state.get("commitments", []):
            if item.get("commitment_id") == active_learning["commitment_id"]:
                item["next_run_at"] = past_iso()
        store.write_json("user_commitments.json", commitments_state)

        original_adapter = commitment_push_module.ChannelAdapter
        SentAdapter.sent_messages = []
        commitment_push_module.ChannelAdapter = SentAdapter  # type: ignore[assignment]
        try:
            delivered = client.post("/commitments/run-due", json={"limit": 5, "reason": "closed_loop_learning"})
        finally:
            commitment_push_module.ChannelAdapter = original_adapter  # type: ignore[assignment]
        delivered_body = delivered.json()
        delivered_row = next(row for row in delivered_body.get("processed", []) if row.get("commitment_id") == active_learning["commitment_id"])
        expect(delivered_row.get("status") == "delivered", "learning candidate delivered by provider", delivered_row)
        expect(SentAdapter.sent_messages and "deeplearning.ai" in SentAdapter.sent_messages[0]["message"], "delivered message uses external candidate", SentAdapter.sent_messages)

        weather = client.post(
            "/events/message",
            json={"text": WEATHER_TEXT, "channel": "acceptance", "user_id": "closed-loop-user", "session_id": "weather"},
        )
        expect(weather.status_code == 200, "weather message accepted", weather.text)
        weather_turn = (weather.json().get("artifacts") or {}).get("commitment", {})
        weather_offer = weather_turn.get("commitment") if isinstance(weather_turn.get("commitment"), dict) else {}
        expect((weather_offer.get("payload") or {}).get("location") == "北京", "weather commitment records clean location", weather_offer)
        if weather_offer.get("status") == "pending_confirmation":
            weather_confirm = client.post(
                "/events/message",
                json={"text": "同意", "channel": "acceptance", "user_id": "closed-loop-user", "session_id": "weather"},
            )
            expect((weather_confirm.json().get("artifacts") or {}).get("commitment", {}).get("status") == "confirmed", "weather subscription confirmed", weather_confirm.json())
        else:
            expect(weather_offer.get("status") == "active", "explicit complete weather request activates subscription", weather_offer)

        tick = client.post("/runtime/active-loop/tick", json={"reason": "closed_loop_acceptance"})
        steps = [step.get("name") for step in tick.json().get("steps", []) if isinstance(step, dict)]
        expect("external_world" in steps and "commitment_push" in steps, "active loop includes external and push steps", steps)

    print("proactive closed-loop acceptance passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
