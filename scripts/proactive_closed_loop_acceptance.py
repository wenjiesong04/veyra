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


def past_iso() -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()


def main() -> int:
    with TemporaryDirectory(prefix="veyra-closed-loop-") as tmp:
        state_root = Path(tmp) / "state"
        agency_root = Path(tmp) / "agency"
        os.environ["VEYRA_STATE_DIR"] = str(state_root)
        os.environ["VEYRA_STATE_ROOT"] = str(state_root)
        os.environ["VEYRA_AGENCY_DIR"] = str(agency_root)
        os.environ["VEYRA_AGENCY_ROOT"] = str(agency_root)
        agency_root.mkdir(parents=True, exist_ok=True)
        (agency_root / "goals.json").write_text("{}", encoding="utf-8")

        import main as app_module  # noqa: E402
        import runtime.commitment_push as commitment_push_module  # noqa: E402

        client = TestClient(app_module.app)
        app_module.external_world_refresh.search_probe = FakeSearchProbe()
        store = app_module.state_store

        learning = client.post(
            "/events/message",
            json={
                "text": "现在我要开始学习深度学习了，你能不能帮我？",
                "channel": "acceptance",
                "user_id": "closed-loop-user",
                "session_id": "learn",
            },
        )
        expect(learning.status_code == 200, "learning request accepted", learning.text)
        learning_body = learning.json()
        artifact = (learning_body.get("artifacts") or {}).get("commitment") or {}
        expect(artifact.get("status") == "goal_recorded", "learning goal recorded", artifact)
        pending = artifact.get("commitment") if isinstance(artifact.get("commitment"), dict) else {}
        expect(pending.get("status") == "pending_confirmation", "learning push pending before consent", pending)

        confirm = client.post(
            "/events/message",
            json={"text": "好的", "channel": "acceptance", "user_id": "closed-loop-user", "session_id": "learn"},
        )
        expect(confirm.status_code == 200 and (confirm.json().get("artifacts") or {}).get("commitment", {}).get("status") == "confirmed", "learning push confirmed", confirm.text)
        active_learning = client.get("/commitments", params={"session_id": pending["session_id"], "status": "active"}).json()["commitments"][0]

        external = client.post("/external/refresh", params={"limit": 5})
        expect(external.status_code == 200 and external.json().get("refreshed"), "authorized external search refreshed", external.text)
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
            json={"text": "今天北京天气怎么样", "channel": "acceptance", "user_id": "closed-loop-user", "session_id": "weather"},
        )
        expect(weather.status_code == 200 and "推送" in weather.json().get("response", ""), "weather answer offers subscription", weather.text)
        weather_offer = (weather.json().get("artifacts") or {}).get("commitment", {}).get("commitment", {})
        expect((weather_offer.get("payload") or {}).get("location") == "北京", "weather offer records clean location", weather_offer)
        weather_confirm = client.post(
            "/events/message",
            json={"text": "同意", "channel": "acceptance", "user_id": "closed-loop-user", "session_id": "weather"},
        )
        expect((weather_confirm.json().get("artifacts") or {}).get("commitment", {}).get("status") == "confirmed", "weather subscription confirmed", weather_confirm.json())

        tick = client.post("/runtime/active-loop/tick", json={"reason": "closed_loop_acceptance"})
        steps = [step.get("name") for step in tick.json().get("steps", []) if isinstance(step, dict)]
        expect("external_world" in steps and "commitment_push" in steps, "active loop includes external and push steps", steps)

    print("proactive closed-loop acceptance passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
