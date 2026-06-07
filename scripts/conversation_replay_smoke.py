#!/usr/bin/env python3
"""Smoke test: Feishu-like multi-turn replay for conversation slots and commitments."""
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

_RUNTIME = TemporaryDirectory(prefix="veyra-conversation-replay-")
TEST_STATE_ROOT = Path(_RUNTIME.name) / "state"
TEST_AGENCY_ROOT = Path(_RUNTIME.name) / "agency"
os.environ["VEYRA_STATE_DIR"] = str(TEST_STATE_ROOT)
os.environ["VEYRA_STATE_ROOT"] = str(TEST_STATE_ROOT)
os.environ["VEYRA_AGENCY_DIR"] = str(TEST_AGENCY_ROOT)
os.environ["VEYRA_AGENCY_ROOT"] = str(TEST_AGENCY_ROOT)
TEST_AGENCY_ROOT.mkdir(parents=True, exist_ok=True)
(TEST_AGENCY_ROOT / "goals.json").write_text("{}", encoding="utf-8")
(TEST_AGENCY_ROOT / "intention_queue.json").write_text("[]", encoding="utf-8")

from core.definitions import RiskLevel  # noqa: E402
from interface.event_schema import Decision, Route, utc_now_iso  # noqa: E402
from probes.schema import probe_payload  # noqa: E402
from probes.weather_probe import WeatherProbe  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402
from main import app, awareness_loop  # noqa: E402


FORBIDDEN = (
    "No geocoding result",
    "Weather probe needs a city",
    "Search returned 1 result(s)",
    "traceback",
    "raw exception",
    "internal probe error",
)


class FakeWeatherProbe:
    def __init__(self) -> None:
        self.real = WeatherProbe()

    def _extract_location(self, text: str) -> str:
        return self.real._extract_location(text)

    def run(self, text: str = "", *, location: str | None = None) -> dict[str, Any]:
        target = (location or "").strip() or self._extract_location(text)
        if not target:
            return probe_payload(
                probe="weather_probe",
                target="weather",
                status="missing_target",
                summary="Weather probe needs a city or place name in the question.",
                confidence=0.5,
                ttl_seconds=900,
                details={},
            )
        resolved = "贵阳" if "花溪" in target else target
        summary = f"{resolved}: 小雨, 23.5°C"
        return probe_payload(
            probe="weather_probe",
            target=target,
            status="ok",
            summary=summary,
            confidence=0.9,
            ttl_seconds=900,
            details={
                "location": resolved,
                "weather_description": "小雨",
                "current": {"temperature_2m": 23.5, "weather_description": "小雨", "time": "2026-06-07T10:00"},
            },
        )


class FakeSearchProbe:
    def run(self, query: str, *, max_results: int = 5) -> dict[str, Any]:
        return probe_payload(
            probe="search_probe",
            target=query,
            status="ok",
            summary=f"Search returned 1 result(s) for {query}.",
            confidence=0.8,
            ttl_seconds=1800,
            details={
                "query": query,
                "results": [
                    {
                        "title": "卢成风公开视频合集",
                        "url": "https://example.com/luchengfeng-video",
                        "source": "example.com",
                        "snippet": "公开视频搜索结果摘要，包含卢成风相关视频标题和页面说明。",
                        "published_at": "2026-06-01",
                    }
                ],
            },
        )


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


def messages(body: dict[str, Any]) -> list[str]:
    return [str(item.get("message") or "") for item in body.get("messages", []) if isinstance(item, dict)]


def full_text(body: dict[str, Any]) -> str:
    return "\n".join(messages(body) or [str(body.get("response") or "")])


def post(client: TestClient, text: str, *, index: int) -> dict[str, Any]:
    response = client.post(
        "/events/message",
        json={
            "text": text,
            "channel": "api",
            "user_id": "replay-user",
            "session_id": "feishu-like-replay",
            "message_id": f"conversation-replay-{index}",
            "metadata": {
                "feishu": {
                    "message_id": f"om_replay_{index}",
                    "chat_id": "oc_replay",
                    "chat_type": "p2p",
                    "message_type": "text",
                    "source": "replay",
                    "content_text": text,
                }
            },
        },
    )
    expect(response.status_code == 200, f"message {index} accepted", response.text)
    return response.json()


def fake_decision_assist(**kwargs: Any) -> dict[str, Any]:
    text = str(kwargs.get("text") or "")
    if "卢成风" in text and "视频" in text:
        return {
            "status": "model_assisted",
            "recommended_route": "probe",
            "risk_level": "R1",
            "intent": "information",
            "complexity": "simple",
            "freshness_required": True,
            "needs_probe": True,
            "selected_probe": "search_probe",
            "capability_request": {"capability": "web_search", "probe": "search_probe"},
            "reason": "read-only video search needs external evidence",
        }
    return {"status": "skipped"}


def main() -> int:
    awareness_loop.probes["weather_probe"] = FakeWeatherProbe()
    awareness_loop.probes["search_probe"] = FakeSearchProbe()
    awareness_loop.compact_external_lookup.search = FakeSearchProbe()
    awareness_loop.core_reasoning.decision_assist = fake_decision_assist  # type: ignore[method-assign]
    client = TestClient(app)
    store = WorldStateStore(TEST_STATE_ROOT)

    outputs: list[dict[str, Any]] = []
    outputs.append(post(client, "现在贵阳花溪区天气怎么样", index=1))
    expect("贵阳" in full_text(outputs[-1]) and "No geocoding result" not in full_text(outputs[-1]), "weather first turn is user-readable", full_text(outputs[-1]))

    outputs.append(post(client, "花溪区呢", index=2))
    expect("贵阳" in full_text(outputs[-1]) and "city" not in full_text(outputs[-1]).lower(), "weather followup inherits location", full_text(outputs[-1]))

    outputs.append(post(client, "帮我找一下卢成风的视频", index=3))
    search_text = full_text(outputs[-1])
    expect("卢成风公开视频合集" in search_text and "https://example.com/luchengfeng-video" in search_text, "search result renders title url snippet", search_text)

    outputs.append(post(client, "标题是什么", index=4))
    expect("卢成风公开视频合集" in full_text(outputs[-1]), "title followup uses last_tool_result", full_text(outputs[-1]))

    before_weather_count = len([item for item in store.read_json("user_commitments.json").get("commitments", []) if isinstance(item, dict) and item.get("kind") == "weather_daily"])
    outputs.append(post(client, "你以后能每天发天气吗", index=5))
    after_weather_count = len([item for item in store.read_json("user_commitments.json").get("commitments", []) if isinstance(item, dict) and item.get("kind") == "weather_daily"])
    expect(before_weather_count == after_weather_count, "incomplete daily weather does not create commitment", store.read_json("user_commitments.json"))
    expect("几点" in full_text(outputs[-1]) and "城市/地区" in full_text(outputs[-1]), "incomplete daily weather asks parameters", full_text(outputs[-1]))

    outputs.append(post(client, "每天早上九点发贵阳花溪区天气", index=6))
    commitments = store.read_json("user_commitments.json").get("commitments", [])
    weather_items = [item for item in commitments if isinstance(item, dict) and item.get("kind") == "weather_daily"]
    created = weather_items[-1] if weather_items else {}
    expect(created.get("status") == "active", "complete weather commitment is active", created)
    expect((created.get("payload") or {}).get("location") == "贵阳市花溪区", "weather location normalized", created)
    expect((created.get("schedule") or {}).get("time_local") == "09:00", "weather schedule is 09:00", created)

    state = store.read_json("user_commitments.json")
    bad = {
        "commitment_id": "cmt_bad_weather_replay",
        "kind": "weather_daily",
        "status": "active",
        "title": "每日天气（错误地点）",
        "user_id": "replay-user",
        "channel": "api",
        "session_id": "api:replay-user:feishu-like-replay",
        "risk_level": "R0",
        "schedule": {"kind": "daily", "time_local": "08:00", "timezone": "Asia/Shanghai", "interval_seconds": 86400.0},
        "payload": {"location": "你以后能每天发天气", "topic": "weather"},
        "source_event_id": "evt_bad_weather_replay",
        "created_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
        "confirmed_at": utc_now_iso(),
        "next_run_at": utc_now_iso(),
        "last_run_at": None,
        "run_count": 0,
        "push_history": [],
    }
    state.setdefault("commitments", []).append(bad)
    store.write_json("user_commitments.json", state)
    healed = client.post("/commitments/run-due", json={"limit": 5, "reason": "conversation_replay"})
    expect(healed.status_code == 200, "invalid commitment run-due accepted", healed.text)
    bad_after = next(item for item in store.read_json("user_commitments.json").get("commitments", []) if item.get("commitment_id") == "cmt_bad_weather_replay")
    expect(bad_after.get("status") == "paused" and bad_after.get("invalid") is True, "invalid weather commitment pauses itself", bad_after)

    combined = "\n".join(full_text(body) for body in outputs)
    expect(not any(marker.lower() in combined.lower() for marker in FORBIDDEN), "final user replies hide internal errors", combined)

    print("conversation replay smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
