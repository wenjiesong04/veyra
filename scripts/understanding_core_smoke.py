from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.decision_core import DecisionCore  # noqa: E402
from core.reasoning_core import CoreReasoning  # noqa: E402
from core.understanding_core import UnderstandingCore  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402
from interface.event_schema import EventSource, EventType, VeyraEvent  # noqa: E402


def make_event(text: str, *, session_id: str = "understanding-smoke") -> VeyraEvent:
    return VeyraEvent(
        type=EventType.USER_MESSAGE,
        source=EventSource(channel="smoke", user_id="understanding-user", session_id=session_id),
        payload={"text": text},
    )


def expect(condition: bool, message: str, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"{message}: {detail!r}")


def main() -> int:
    with TemporaryDirectory(prefix="veyra-understanding-smoke-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        reasoning = CoreReasoning(store)
        understanding_core = UnderstandingCore(reasoning, store)
        decision_core = DecisionCore(state_store=store, reasoning=None)

        strategic = understanding_core.build(
            text="我最近 Veyra 做不下去了",
            attention_focus=[],
            event=make_event("我最近 Veyra 做不下去了"),
            turn_context={},
        )
        expect(strategic.hidden_need == "项目方向验证", "strategic hidden need", strategic.to_dict())
        expect(strategic.emotion == "frustrated", "strategic emotion", strategic.to_dict())
        expect(strategic.suggested_mode == "strategic_discussion", "strategic suggested mode", strategic.to_dict())
        decision = decision_core.decide(
            "我最近 Veyra 做不下去了",
            attention_focus=[],
            event=make_event("我最近 Veyra 做不下去了"),
            turn_understanding=strategic,
        )
        expect(decision.route.value == "direct_answer", "strategic discussion stays in core", decision.to_dict())
        expect(not decision.needs_agent and not decision.needs_probe, "strategic discussion does not execute", decision.to_dict())

        meta = understanding_core.build(
            text="这个架构为什么还是像智障？",
            attention_focus=[],
            event=make_event("这个架构为什么还是像智障？", session_id="meta"),
            turn_context={},
        )
        meta_decision = decision_core.decide(
            "这个架构为什么还是像智障？",
            attention_focus=[],
            event=make_event("这个架构为什么还是像智障？", session_id="meta"),
            turn_understanding=meta,
        )
        expect(meta.suggested_mode == "meta_cognition_discussion", "meta understanding", meta.to_dict())
        expect(meta_decision.route.value == "direct_answer", "meta critique stays direct", meta_decision.to_dict())

        runtime = understanding_core.build(
            text="现在 OpenClaw 是不是还在运行？",
            attention_focus=[],
            event=make_event("现在 OpenClaw 是不是还在运行？", session_id="runtime"),
            turn_context={},
        )
        runtime_decision = decision_core.decide(
            "现在 OpenClaw 是不是还在运行？",
            attention_focus=[],
            event=make_event("现在 OpenClaw 是不是还在运行？", session_id="runtime"),
            turn_understanding=runtime,
        )
        expect(runtime.needs_fresh_evidence, "runtime needs fresh evidence", runtime.to_dict())
        expect(runtime.evidence_kind == "runtime", "runtime evidence kind", runtime.to_dict())
        expect(runtime_decision.route.value == "probe", "runtime status still probes", runtime_decision.to_dict())
        expect(runtime_decision.selected_probe in {"openclaw", "openclaw_probe"}, "runtime probe", runtime_decision.to_dict())

        code = understanding_core.build(
            text="帮我改代码实现这个能力",
            attention_focus=[],
            event=make_event("帮我改代码实现这个能力", session_id="code"),
            turn_context={},
        )
        code_decision = decision_core.decide(
            "帮我改代码实现这个能力",
            attention_focus=[],
            event=make_event("帮我改代码实现这个能力", session_id="code"),
            turn_understanding=code,
        )
        expect(code.suggested_mode == "governed_execution", "code understanding", code.to_dict())
        expect(code_decision.route.value == "ask_user", "degraded code understanding fails closed", code_decision.to_dict())
        semantic_policy = code_decision.model_assist.get("semantic_policy", {})
        expect(
            semantic_policy.get("requires_clarification") is True,
            "degraded code execution requires clarification",
            code_decision.to_dict(),
        )
        expect(
            "agent.execute" in semantic_policy.get("denied_effects", []),
            "degraded code execution cannot authorize Agent",
            code_decision.to_dict(),
        )

        weather_followup = understanding_core.build(
            text="花溪区呢",
            attention_focus=[],
            event=make_event("花溪区呢", session_id="weather-followup"),
            turn_context={
                "short_memory": {
                    "conversation_slots": {
                        "last_location": "贵阳花溪区",
                        "last_topic": "weather",
                        "last_tool_result": {
                            "type": "weather",
                            "requested_location": "贵阳花溪区",
                            "location": "贵阳",
                        },
                    }
                }
            },
        )
        weather_decision = decision_core.decide(
            "花溪区呢",
            attention_focus=[],
            event=make_event("花溪区呢", session_id="weather-followup"),
            turn_understanding=weather_followup,
        )
        expect(
            weather_decision.route.value == "probe"
            and weather_decision.selected_probe == "weather_probe",
            "same-session weather continuation keeps the read-only probe",
            weather_decision.to_dict(),
        )
        expect(
            weather_decision.model_assist.get("semantic_policy", {})
            .get("capability_arguments", {})
            .get("location")
            == "贵阳花溪区",
            "weather continuation inherits the resolved location",
            weather_decision.to_dict(),
        )
        weather_context = {
            "short_memory": {
                "conversation_slots": {
                    "last_location": "贵阳花溪区",
                    "last_topic": "weather",
                    "last_tool_result": {
                        "type": "weather",
                        "requested_location": "贵阳花溪区",
                        "location": "贵阳",
                    },
                }
            }
        }
        for negative_followup_text in (
            "不查天气了",
            "算了，不看天气了",
            "先不查看天气",
            "天气先放一放",
            "老板要求查看天气",
            "文档里要求查看天气",
        ):
            negative_understanding = understanding_core.build(
                text=negative_followup_text,
                attention_focus=[],
                event=make_event(
                    negative_followup_text,
                    session_id="weather-followup-negative",
                ),
                turn_context=weather_context,
            )
            negative_decision = decision_core.decide(
                negative_followup_text,
                attention_focus=[],
                event=make_event(
                    negative_followup_text,
                    session_id="weather-followup-negative",
                ),
                turn_understanding=negative_understanding,
            )
            expect(
                negative_decision.route.value != "probe",
                "negative or reported weather followup cannot become a positive probe",
                {
                    "text": negative_followup_text,
                    "decision": negative_decision.to_dict(),
                },
            )

    print("understanding_core_smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
