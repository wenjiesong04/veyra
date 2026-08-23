"""Duplicate watch receipt keeps material/Situation revisions stable."""

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


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 23, 12, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value


class WeatherProvider:
    provider_id = "weather.material.fixture.v1"

    def read(self, context):
        return {
            "status": "ok",
            "details": {
                "location": context.parameters["location"],
                "current": {"temperature_2m": 28, "weather_description": "晴"},
            },
        }


def main() -> int:
    with TemporaryDirectory(prefix="veyra-material-revision-") as temp:
        clock = Clock()
        store = WorldStateStore(Path(temp) / "state")
        composition = build_living_context_composition(
            store,
            clock=clock,
            source_providers={"weather": WeatherProvider()},
        )
        orchestrator = composition.orchestrator
        owner, session = "material-owner", "material-session"
        orchestrator.grant_source_consent("weather", owner_id=owner, session_id=session, expected_generation=0)
        text = "上海团建"
        candidate = LivingContextCandidate.model_validate(
            {
                "schema_version": "veyra.living_context_candidate.v1",
                "disposition": "create",
                "create_subject": "上海团建",
                "category": "travel",
                "title": "上海团建",
                "summary": text,
                "goal": "顺利完成团建",
                "progress": {"status": "in_progress", "value": 0.2},
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
            event_id="material-create",
        )
        first = orchestrator.process_user_turn(
            event,
            type("U", (), {"living_context_candidate": candidate, "living_reaction_feedback": None})(),
            catalog=[],
        )
        situation_id = str(first["situation"]["situation_id"])
        tick_first = orchestrator.tick(owner_id=owner, session_id=session, limit=8)
        assert tick_first["error_count"] == 0
        first_row = composition.core.get_situation(situation_id, owner_id=owner, session_id=session)
        first_semantic = first_row["semantic"]
        clock.value += timedelta(hours=7)
        tick_second = orchestrator.tick(owner_id=owner, session_id=session, limit=8)
        assert tick_second["error_count"] == 0
        second_row = composition.core.get_situation(situation_id, owner_id=owner, session_id=session)
        second_semantic = second_row["semantic"]
        assert second_semantic["material_digest"] == first_semantic["material_digest"]
        assert second_semantic["material_revision"] == first_semantic["material_revision"]
        assert second_row["observation_revision"] == first_row["observation_revision"]
    print("SOURCE_MATERIAL_REVISION_SMOKE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
