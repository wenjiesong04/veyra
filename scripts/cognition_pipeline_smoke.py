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
from core.semantic_frame import TurnSemanticFrame  # noqa: E402
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
                        "scope_kind": "tenant",
                        "tenant_derived": True,
                        "user_id": "user-a",
                        "session_id": "session-a",
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
                        "scope_kind": "tenant",
                        "tenant_derived": True,
                        "user_id": "user-a",
                        "session_id": "session-a",
                    }
                }
            },
        )
        assembler = AwarenessContextAssembler(store)
        snap = assembler.snapshot(
            user_message="贵阳天气",
            attention_focus=["天气", "贵阳"],
            evidence_kind="weather",
            user_id="user-a",
            session_id="session-a",
        )
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


def _typed_understanding(text: str, *, operation: str, target: dict[str, object], evidence_need: str = "none") -> TurnUnderstanding:
    frame = TurnSemanticFrame.from_payload(
        {
            "schema_version": "veyra.semantic_frame.v1",
            "acts": [
                {
                    "act_id": "act_1",
                    "kind": "information" if evidence_need != "none" else "conversation",
                    "goal": text,
                    "operation": operation,
                    "target": target,
                    "polarity": "positive",
                    "explicitness": "explicit",
                    "source_quote": {"text": text, "start": 0, "end": len(text)},
                    "speaker": "direct_user",
                    "authority": "direct_user",
                    "mention_mode": "normal_use",
                    "evidence_need": evidence_need,
                    "referent": {"surface": "", "resolved": "", "status": "not_applicable", "candidates": []},
                    "condition": None,
                    "modality": "asserted",
                    "arguments": {},
                }
            ],
            "relations": [],
            "ambiguities": [],
            "resolver_status": "resolved",
            "source": "model",
        },
        source_text=text,
    )
    return TurnUnderstanding(
        intent="information" if evidence_need != "none" else "conversation",
        task_type="chat",
        source="model",
        confidence=0.95,
        semantic_frame=frame,
    )


def test_typed_policy_skips_planner_for_direct_and_probe() -> None:
    reasoning = FakeReasoning()
    core = DecisionCore(reasoning=reasoning)  # type: ignore[arg-type]
    direct_text = "下周六户外团建在上海。"
    direct = core.decide(
        direct_text,
        attention_focus=[],
        turn_understanding=_typed_understanding(
            direct_text,
            operation="share_context",
            target={"type": "event", "value": "户外团建"},
        ),
    )
    assert direct.route.value == "direct_answer"
    assert reasoning.calls == []

    probe_text = "上海的情况怎么样？"
    probe = core.decide(
        probe_text,
        attention_focus=[],
        turn_understanding=_typed_understanding(
            probe_text,
            operation="query_weather",
            target={"type": "weather", "value": "上海"},
            evidence_need="weather",
        ),
    )
    assert probe.route.value == "probe"
    assert probe.selected_probe == "weather_probe"
    assert probe.model_assist["semantic_policy"]["capability_arguments"] == {"location": "上海"}
    assert reasoning.calls == []

    typed_read = core.decide(
        "读取上海天气",
        attention_focus=[],
        turn_understanding=_typed_understanding(
            "读取上海天气",
            operation="read",
            target={"type": "weather", "value": "上海"},
        ),
    )
    assert typed_read.route.value == "probe"
    assert typed_read.selected_probe == "weather_probe"

    process_status = core.decide(
        "读取当前进程状态",
        attention_focus=[],
        turn_understanding=_typed_understanding(
            "读取当前进程状态",
            operation="query_current_process_status",
            target={"type": "process", "value": "current"},
        ),
    )
    assert process_status.route.value == "probe"
    assert process_status.selected_probe == "process"


def test_text_marker_does_not_grant_probe() -> None:
    text = "天气"
    decision = DecisionCore(reasoning=FakeReasoning()).decide(
        text,
        attention_focus=[],
        turn_understanding=_typed_understanding(
            text,
            operation="discuss_topic",
            target={"type": "topic", "value": "天气"},
        ),
    )
    assert decision.route.value == "direct_answer"
    assert decision.selected_probe is None


def test_fresh_evidence_without_observer_asks_instead_of_stale_direct() -> None:
    reasoning = FakeReasoning()
    text = "这个主题需要最新证据。"
    decision = DecisionCore(reasoning=reasoning).decide(
        text,
        attention_focus=[],
        turn_understanding=_typed_understanding(
            text,
            operation="discuss_topic",
            target={"type": "topic", "value": "这个主题"},
            evidence_need="fresh_external",
        ),
    )
    assert decision.route.value == "ask_user"
    assert decision.model_assist["semantic_policy"]["requires_clarification"] is True
    assert "semantic_policy:unresolved_observer" in decision.model_assist["semantic_policy"]["policy_signals"]
    assert reasoning.calls == []


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
    test_typed_policy_skips_planner_for_direct_and_probe()
    test_text_marker_does_not_grant_probe()
    test_fresh_evidence_without_observer_asks_instead_of_stale_direct()
    test_precomputed_understanding_skips_orientation_call()
    print("cognition_pipeline_smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
