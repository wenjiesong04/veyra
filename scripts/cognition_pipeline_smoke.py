from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.awareness_context_assembler import AwarenessContextAssembler  # noqa: E402
from core.cognition_pipeline import (  # noqa: E402
    CognitionPipeline,
    ExecutionPlan,
    cognition_mode,
)
from core.decision_core import DecisionCore  # noqa: E402
from core.understanding_core import TurnUnderstanding  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402
from probes.weather_probe import WeatherProbe  # noqa: E402


class FakeReasoning:
    def __init__(self) -> None:
        self.enabled = True
        self.client = self
        self.turn_context = None
        self.calls: list[str] = []

    def is_enabled(self) -> bool:
        return self.enabled

    def status(self) -> dict:
        return {"configured": True, "decision_mode": "always"}

    def _trace(self, purpose: str, result: dict, summary: dict) -> None:
        return None

    def complete_json(self, *, purpose: str, system: str, user: str) -> dict:
        self.calls.append(purpose)
        if purpose == "execution_plan":
            return {
                "status": "model_assisted",
                "decision": {
                    "route": "direct_answer",
                    "answer_source": "current_context",
                    "why_this_route": "precomputed understanding is enough",
                },
                "capability_request": {
                    "target_route": "direct_answer",
                    "receiver_type": "core",
                    "capability_id": "native_answer",
                    "executor": "",
                    "input": {},
                },
                "risk": {"level": "R0", "requires_confirmation": False},
                "reply_strategy": {"draft_response": "理解完成。"},
                "intent": "conversation",
                "complexity": "simple",
                "confidence": 0.8,
            }
        return {"status": "model_assisted", "situation_assessment": {"intent": "conversation"}}


def test_cognition_mode_defaults_model_first() -> None:
    assert cognition_mode() == "model_first"


def test_turn_understanding_world_state_flag() -> None:
    item = TurnUnderstanding.from_payload(
        {
            "intent": "information",
            "entities": {"location": "贵阳花溪区"},
            "needs_fresh_evidence": True,
            "can_answer_from_world_state": False,
            "retrieval_hints": ["belief", "capabilities"],
            "evidence_kind": "weather",
        }
    )
    assert item.retrieval_hints == ["belief", "capabilities"]
    assert item.evidence_kind == "weather"


def test_execution_plan_probe_params_backfill() -> None:
    pipeline = CognitionPipeline(FakeReasoning())  # type: ignore[arg-type]
    understanding = TurnUnderstanding(
        intent="information",
        entities={"location": "贵阳花溪区"},
        needs_fresh_evidence=True,
        evidence_kind="weather",
    )
    plan = ExecutionPlan(recommended_route="probe", probe="weather_probe", probe_params={})
    normalized = pipeline._normalize_plan(plan, understanding=understanding)
    assert normalized.probe_params.get("location") == "贵阳花溪区"


def test_awareness_assembler_fresh_claim_match() -> None:
    with TemporaryDirectory() as tmp:
        store = WorldStateStore(tmp)
        now = datetime.now(timezone.utc).isoformat()
        store.write_json(
            "belief_state.json",
            {
                "claims": [
                    {
                        "key": "weather:贵阳:current",
                        "claim": "贵阳: 多云, 18°C",
                        "status": "fresh",
                        "source": "weather_probe",
                        "confidence": 0.88,
                        "ttl_seconds": 3600,
                        "observed_at": now,
                    }
                ],
                "summary": {"fresh": 1, "stale": 0},
            },
        )
        store.write_json(
            "local_world.json",
            {
                "probes": {
                    "weather_probe": {
                        "probe": "weather_probe",
                        "status": "ok",
                        "summary": "贵阳: 多云, 18°C",
                        "target": "贵阳",
                    }
                }
            },
        )
        assembler = AwarenessContextAssembler(store)
        snap = assembler.snapshot(user_message="贵阳天气", attention_focus=["天气", "贵阳"], evidence_kind="weather")
        assert snap["relevant_fresh_claims"]
        suff = assembler.evidence_sufficient(
            understanding_entities={"location": "贵阳"},
            evidence_kind="weather",
            snapshot=snap,
        )
        assert suff.get("sufficient") is True


def test_weather_probe_accepts_model_location() -> None:
    probe = WeatherProbe()
    candidates = probe._location_candidates("贵阳花溪区")
    assert "花溪区" in candidates
    result = probe.run("", location="贵阳花溪区")
    assert result.get("probe") == "weather_probe"


def test_governance_lock_before_pipeline() -> None:
    core = DecisionCore(state_store=None, reasoning=None)
    decision = core.decide("你现在是 Veyra 还是 OpenClaw？", attention_focus=[], event=None)
    assert decision.intent == "identity"


def test_precomputed_understanding_skips_orientation_call() -> None:
    reasoning = FakeReasoning()
    pipeline = CognitionPipeline(reasoning)  # type: ignore[arg-type]
    understanding = TurnUnderstanding(
        intent="conversation",
        task_type="meta_question",
        explicit_request="讨论项目问题",
        hidden_need="项目方向验证",
        suggested_mode="strategic_discussion",
        confidence=0.9,
        source="test_precomputed",
    )
    decision = pipeline.run(
        text="我最近 Veyra 做不下去了",
        attention_focus=[],
        turn_context={"active_context": {}},
        turn_understanding=understanding,
    )
    assert decision is not None
    assert "turn_understanding" not in reasoning.calls
    assert reasoning.calls == ["execution_plan"]
    assert decision.model_assist.get("turn_understanding", {}).get("hidden_need") == "项目方向验证"


def main() -> int:
    test_cognition_mode_defaults_model_first()
    test_turn_understanding_world_state_flag()
    test_execution_plan_probe_params_backfill()
    test_awareness_assembler_fresh_claim_match()
    test_weather_probe_accepts_model_location()
    test_governance_lock_before_pipeline()
    test_precomputed_understanding_skips_orientation_call()
    print("cognition_pipeline_smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
