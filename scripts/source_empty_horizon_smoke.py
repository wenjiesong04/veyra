"""Real source-runtime empty receipt obeys next_eligible_at for active Need."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.world_state import WorldStateStore  # noqa: E402
from interface.event_schema import EventSource, EventType, VeyraEvent  # noqa: E402
from interface.living_context_contract import LivingContextCandidate  # noqa: E402
from runtime.living_context_composition import build_living_context_composition  # noqa: E402
from interface.living_source_payload import weather_coverage  # noqa: E402


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 23, 12, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value


class EmptyThenOkWeather:
    provider_id = "weather.empty-horizon.fixture.v1"

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.calls = 0

    def read(self, context):
        self.calls += 1
        if self.calls == 1:
            return {
                "status": "empty",
                "ttl_seconds": 300,
                "details": {
                    "location": context.parameters["location"],
                    "coverage": weather_coverage(context.parameters["location"]),
                    "next_eligible_at": (self.clock.value + timedelta(days=2)).isoformat(),
                },
            }
        return {
            "status": "ok",
            "details": {
                "location": context.parameters["location"],
                "current": {"temperature_2m": 28, "weather_description": "晴"},
            },
        }


def main() -> int:
    with TemporaryDirectory(prefix="veyra-empty-horizon-") as temp:
        clock = Clock()
        provider = EmptyThenOkWeather(clock)
        store = WorldStateStore(Path(temp) / "state")
        composition = build_living_context_composition(
            store,
            clock=clock,
            source_providers={"weather": provider},
        )
        orchestrator = composition.orchestrator
        owner, session = "empty-owner", "empty-session"
        orchestrator.grant_source_consent("weather", owner_id=owner, session_id=session, expected_generation=0)
        text = "上海活动"
        candidate = LivingContextCandidate.model_validate(
            {
                "schema_version": "veyra.living_context_candidate.v1",
                "disposition": "create",
                "create_subject": text,
                "category": "travel",
                "title": text,
                "summary": text,
                "goal": "按时完成活动",
                "progress": {"status": "in_progress", "value": 0.1},
                "entities": [{
                    "kind": "place",
                    "value": "上海",
                    "epistemic_status": "reported",
                    "source_quote": {"text": "上海", "start": 0, "end": 2},
                }],
                "known": [{"statement": text, "epistemic_status": "reported", "source_quote": {"text": text, "start": 0, "end": 4}}],
                "unknown": ["天气条件"],
                "needs": [{
                    "blocked_judgment": "天气条件",
                    "evidence_kind": "weather",
                    "observation_mode": "watch",
                    "observation_requirement": {"coverage": "current", "metrics": ["weather_description", "temperature_2m"]},
                    "why_now": "天气会改变安排",
                    "urgency": 0.5,
                    "allowed_source_classes": ["weather"],
                    "fallback_reaction": "read",
                    "question": "天气如何？",
                }],
                "assumptions": [],
                "timeline": [],
                "next_step": "确认天气",
                "next_step_epistemic_status": "inferred",
                "requested_reaction": "read",
                "source": "model",
            },
            strict=True,
        )
        event = VeyraEvent(
            type=EventType.USER_MESSAGE,
            source=EventSource(channel="api", user_id=owner, session_id=session),
            payload={"text": text},
            event_id="empty-create",
        )
        orchestrator.process_user_turn(
            event,
            type("U", (), {"living_context_candidate": candidate, "living_reaction_feedback": None})(),
            catalog=[],
        )
        first = orchestrator.tick(owner_id=owner, session_id=session, limit=8)
        assert provider.calls == 1
        assert first["items"][0]["source"]["status"] == "empty"
        clock.value += timedelta(days=1)
        before_due = orchestrator.tick(owner_id=owner, session_id=session, limit=8)
        assert provider.calls == 1
        assert before_due["items"][0]["source"]["reason"] == "next_eligible_at"
        clock.value += timedelta(days=1)
        due = orchestrator.tick(owner_id=owner, session_id=session, limit=8)
        assert provider.calls == 2
        assert due["items"][0]["source"]["status"] == "ok"
    print("SOURCE_EMPTY_HORIZON_SMOKE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
