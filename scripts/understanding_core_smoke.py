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
        expect(code_decision.route.value == "agent", "code execution routes to agent", code_decision.to_dict())

    print("understanding_core_smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
