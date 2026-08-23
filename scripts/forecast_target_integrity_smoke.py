"""Foreground event-to-Need forecast date authorization smoke."""

from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.understanding_core import TurnUnderstanding  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402
from interface.event_schema import EventSource, EventType, VeyraEvent  # noqa: E402
from runtime.living_context_composition import build_living_context_composition  # noqa: E402


TEXT = "下周六户外团建在上海。"


def payload(
    date_value: str | None,
    *,
    frame_dates: list[str] | None = None,
    deadline_at: str | None = None,
    text: str = TEXT,
) -> dict[str, object]:
    candidate = {
        "schema_version": "veyra.living_context_candidate.v1",
        "disposition": "create",
        "create_subject": "户外团建",
        "category": "travel",
        "title": "户外团建",
        "summary": text,
        "goal": "完成团建",
        "deadline_at": deadline_at,
        "progress": {"status": "not_started", "value": None},
        "entities": [{
            "kind": "place",
            "value": "上海",
            "epistemic_status": "reported",
            "source_quote": {"text": "上海", "start": text.index("上海"), "end": text.index("上海") + 2},
        }],
        "known": [{"statement": text, "epistemic_status": "reported", "source_quote": {"text": text, "start": 0, "end": len(text)}}],
        "unknown": ["天气条件"],
        "assumptions": [],
        "timeline": [],
        "needs": [{
            "blocked_judgment": "天气条件",
            "evidence_kind": "weather",
            "observation_mode": "once",
            "observation_requirement": {
                "coverage": "forecast_day",
                "target_date": date_value,
                "metrics": ["weather_description", "temperature_2m_max", "temperature_2m_min"],
            },
            "why_now": "天气会改变安排",
            "urgency": 0.5,
            "allowed_source_classes": ["weather"],
            "fallback_reaction": "ask",
            "question": "天气如何？",
        }],
        "requested_reaction": "ask",
        "source": "model",
    }
    acts = [{
        "act_id": f"a{index + 1}",
        "kind": "assertion",
                "goal": text,
        "operation": "record_situation",
        "target": {
            "type": "weather",
            "value": "户外团建",
            "attributes": {"target_date": selected_date},
        },
        "polarity": "positive",
        "explicitness": "explicit",
                "source_quote": {"text": text, "start": 0, "end": len(text)},
        "speaker": "user",
        "authority": "direct_user",
        "mention_mode": "normal_use",
        "evidence_need": "weather",
        "referent": {"surface": "", "resolved": "", "status": "not_applicable", "candidates": []},
        "condition": None,
        "modality": "asserted",
        "arguments": {},
    } for index, selected_date in enumerate(frame_dates or [])]
    if not acts:
        acts = [{
            "act_id": "a1", "kind": "assertion", "goal": text,
            "operation": "record_situation", "target": {"type": "event", "value": "户外团建", "attributes": {}},
            "polarity": "positive", "explicitness": "explicit",
            "source_quote": {"text": text, "start": 0, "end": len(text)},
            "speaker": "user", "authority": "direct_user", "mention_mode": "normal_use",
            "evidence_need": "none",
            "referent": {"surface": "", "resolved": "", "status": "not_applicable", "candidates": []},
            "condition": None, "modality": "asserted", "arguments": {},
        }]
    return {
        "source": "model",
        "situation_assessment": {
            "intent": "conversation",
            "task_type": "chat",
            "task_summary": text,
            "explicit_request": text,
            "user_goal": "完成团建",
            "living_context_candidate": candidate,
        },
        # Deliberately an event-shaped act: date authorization comes from the
        # server's direct reported-time resolver, not a model weather act.
        "semantic_frame": {
            "schema_version": "veyra.semantic_frame.v1",
            "acts": acts,
            "relations": [],
            "ambiguities": [],
            "resolver_status": "resolved",
            "source": "model",
        },
    }


def run(date_value: str | None, event_id: str, *, text: str = TEXT, **kwargs: object) -> dict[str, object]:
    with TemporaryDirectory(prefix="veyra-forecast-target-") as temp:
        store = WorldStateStore(Path(temp) / "state")
        composition = build_living_context_composition(store)
        event = VeyraEvent(
            type=EventType.USER_MESSAGE,
            source=EventSource(channel="api", user_id="u", session_id="s"),
            payload={"text": text},
            event_id=event_id,
            timestamp="2026-08-23T12:00:00+00:00",
        )
        understanding = TurnUnderstanding.from_payload(payload(date_value, text=text, **kwargs), source_text=text, current_time=event.timestamp)
        result = composition.core.process_user_turn(event, understanding, catalog=[])
        return result["information_needs"][0]


def main() -> int:
    explicit = run("2026-09-01", "forecast-explicit", frame_dates=["2026-08-29"])
    frame = run(None, "forecast-frame", frame_dates=["2026-08-29"])
    deadline = run(None, "forecast-deadline", deadline_at="2026-08-30T09:00:00+00:00")
    ambiguous = run(None, "forecast-ambiguous", frame_dates=["2026-08-29", "2026-08-30"])
    pure_week = run(None, "forecast-pure-week", text="下周有活动在上海。")
    assert explicit["evidence_target"] is None
    assert explicit["observation_requirement"]["target_date"] == "2026-09-01"
    expected_target = {
        "location": "上海",
        "target_date": "2026-08-29",
        "observation_requirement": {
            "coverage": "forecast_day",
            "target_date": None,
            "metrics": ["weather_description", "temperature_2m_max", "temperature_2m_min"],
        },
    }
    assert frame["evidence_target"] == expected_target
    assert deadline["evidence_target"] == expected_target
    assert ambiguous["evidence_target"] == expected_target
    assert pure_week["evidence_target"] is None
    print("FORECAST_TARGET_INTEGRITY_SMOKE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
