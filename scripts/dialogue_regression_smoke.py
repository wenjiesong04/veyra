from __future__ import annotations

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.awareness_loop import AwarenessLoop  # noqa: E402
from core.decision_core import DecisionCore  # noqa: E402
from core.definitions import RiskLevel  # noqa: E402
from core.runtime_entity import RuntimeEntity  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402
from interface.event_normalizer import EventNormalizer  # noqa: E402
from interface.event_schema import Decision, Route  # noqa: E402


class _StubReasoning:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def decision_assist(self, **_: Any) -> dict[str, Any]:
        return self.payload


def _result(case_id: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"case_id": case_id, "passed": passed, "detail": detail}


def main() -> int:
    with TemporaryDirectory(prefix="veyra-dialogue-regression-") as tmp:
        state_store = WorldStateStore(Path(tmp) / "state")
        runtime = RuntimeEntity(state_store=state_store)
        loop = AwarenessLoop(state_store=state_store, runtime_entity=runtime)
        normalizer = EventNormalizer()
        event = normalizer.user_message(
            text="测试消息",
            channel="smoke",
            user_id="dialogue-regression",
            session_id="dialogue-regression-session",
        )
        cases: list[dict[str, Any]] = []

        decision = Decision(
            route=Route.DIRECT_ANSWER,
            risk_level=RiskLevel.R0,
            reason="test",
            intent="information",
            complexity="simple",
            capability="native_answer",
            model_assist={"draft_response": "好的，我将使用OpenClaw来处理您的请求。"},
        )
        loop.core_reasoning.answer_assist = lambda **_: {"status": "model_assisted", "draft_response": "这是最终回答。"}  # type: ignore[method-assign]
        response = loop._direct_answer(event, decision, [])
        cases.append(
            _result(
                "direct_answer_prefers_answer_assist",
                response == "这是最终回答。",
                f"response={response}",
            )
        )

        loop.core_reasoning.answer_assist = lambda **_: {"status": "skipped"}  # type: ignore[method-assign]
        response = loop._direct_answer(event, decision, [])
        no_decision_draft_leak = "OpenClaw" not in response and response != "好的，我将使用OpenClaw来处理您的请求。"
        cases.append(
            _result(
                "direct_answer_never_leaks_decision_draft",
                no_decision_draft_leak,
                f"response={response}",
            )
        )

        probe_decision = Decision(
            route=Route.PROBE,
            risk_level=RiskLevel.R1,
            reason="test",
            intent="information",
            complexity="simple",
            capability="probe",
            freshness_required=True,
            needs_probe=True,
        )
        probe_result = loop._run_probe(event, probe_decision, [])
        probe_guard_ok = probe_result.route == Route.ASK_USER and "system_probe" in probe_result.response
        cases.append(
            _result(
                "probe_without_name_does_not_fallback_system",
                probe_guard_ok,
                f"route={probe_result.route.value}, response={probe_result.response}",
            )
        )

        assist_payload = {
            "status": "model_assisted",
            "recommended_route": "direct_answer",
            "risk_level": "R0",
            "intent": "information",
            "complexity": "simple",
            "reason": "force direct answer",
        }
        decision_core = DecisionCore(state_store=state_store, reasoning=_StubReasoning(assist_payload))
        base_agent = Decision(
            route=Route.AGENT,
            risk_level=RiskLevel.R1,
            reason="rule route",
            intent="action",
            complexity="complex",
            capability="selected_agent_runtime",
            needs_agent=True,
            required_capabilities=["selected_agent_runtime"],
            signals=["agent_required"],
        )
        final_agent = decision_core._apply_model_assist("帮我修改代码", ["代码"], base_agent, event=event)
        agent_locked = final_agent.route == Route.AGENT and "policy:rule_agent_route_preserved" in final_agent.signals
        cases.append(
            _result(
                "agent_route_not_downgraded_by_model",
                agent_locked,
                f"route={final_agent.route.value}, signals={final_agent.signals}",
            )
        )

        base_probe = Decision(
            route=Route.PROBE,
            risk_level=RiskLevel.R1,
            reason="freshness",
            intent="information",
            complexity="simple",
            capability="probe",
            selected_probe="time",
            freshness_required=True,
            needs_probe=True,
            required_capabilities=["time_probe"],
        )
        final_probe = decision_core._apply_model_assist("现在几点", [], base_probe, event=event)
        probe_locked = final_probe.route == Route.PROBE and final_probe.selected_probe == "time"
        cases.append(
            _result(
                "probe_route_not_downgraded_by_model",
                probe_locked,
                f"route={final_probe.route.value}, selected_probe={final_probe.selected_probe}",
            )
        )

        normalized_probe = decision_core._selected_probe_from_model({"selected_probe": "time_probe"}, None)
        cases.append(
            _result(
                "probe_alias_normalized",
                normalized_probe == "time",
                f"selected_probe={normalized_probe}",
            )
        )

    failed = [case for case in cases if not case["passed"]]
    output = {
        "schema": "veyra.dialogue_regression_smoke.v1",
        "summary": {"total": len(cases), "passed": len(cases) - len(failed), "failed": len(failed)},
        "cases": cases,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
