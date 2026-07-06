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
from core.commitment_core import CommitmentCore  # noqa: E402
from core.decision_core import DecisionCore  # noqa: E402
from core.definitions import RiskLevel  # noqa: E402
from core.runtime_entity import RuntimeEntity  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402
from interface.event_normalizer import EventNormalizer  # noqa: E402
from interface.event_schema import utc_now_iso  # noqa: E402
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
        response = loop._direct_answer(event, decision, [], persona_patch={})
        cases.append(
            _result(
                "direct_answer_prefers_answer_assist",
                response == "这是最终回答。",
                f"response={response}",
            )
        )

        loop.core_reasoning.answer_assist = lambda **_: {"status": "skipped"}  # type: ignore[method-assign]
        response = loop._direct_answer(event, decision, [], persona_patch={})
        no_decision_draft_leak = "OpenClaw" not in response and response != "好的，我将使用OpenClaw来处理您的请求。"
        cases.append(
            _result(
                "direct_answer_never_leaks_decision_draft",
                no_decision_draft_leak,
                f"response={response}",
            )
        )

        low_quality_draft = "我现在无法稳定访问认知模型，所以请稍后再试。"
        loop.core_reasoning.answer_assist = lambda **_: {"status": "model_assisted", "draft_response": low_quality_draft}  # type: ignore[method-assign]
        response = loop._direct_answer(event, decision, [], persona_patch={})
        rejects_low_quality = response != low_quality_draft and "探针" in response
        cases.append(
            _result(
                "direct_answer_rejects_low_quality_template",
                response != low_quality_draft and "无法稳定访问认知模型" not in response,
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

        probe_raw = {
            "probe": "time_probe",
            "summary": "Asia/Tokyo 当前时间是 2026-05-29 14:28:55 (UTC+09:00).",
            "details": {"timezone": "Asia/Tokyo", "time": "14:28:55", "utc_offset": "UTC+09:00"},
        }
        probe_answer = loop._probe_response(
            probe_name="time_probe",
            raw=probe_raw,
            verified={"message": "probe ok"},
            answer_assist={"status": "model_assisted", "draft_response": "我猜现在大概是晚上。"},
        )
        cases.append(
            _result(
                "probe_answer_rejects_ungrounded_model_draft",
                probe_answer == probe_raw["summary"],
                f"response={probe_answer}",
            )
        )

        channel_state = state_store.read_json("channel_state.json")
        inbox = channel_state.get("inbox") if isinstance(channel_state.get("inbox"), list) else []
        outbox = channel_state.get("outbox") if isinstance(channel_state.get("outbox"), list) else []
        inbox.append(
            {
                "message_id": "ctx-in-1",
                "event_id": "evt_ctx_in_1",
                "channel": "smoke",
                "user_id": "dialogue-regression",
                "session_id": "dialogue-regression-session",
                "text": "上一轮问题",
                "metadata": {},
                "received_at": utc_now_iso(),
            }
        )
        outbox.append(
            {
                "channel": "smoke",
                "session_id": "dialogue-regression-session",
                "message": "上一轮回答",
                "metadata": {"route": "direct_answer"},
                "status": "queued",
                "created_at": utc_now_iso(),
                "delivery": "local_outbox",
            }
        )
        channel_state["inbox"] = inbox
        channel_state["outbox"] = outbox
        state_store.write_json("channel_state.json", channel_state)
        context = loop.core_reasoning.turn_context.build(
            user_message="继续说下去",
            attention_focus=[],
            event=event,
            rule_decision={},
        )
        conversation_tail = (
            ((context.get("short_memory") if isinstance(context.get("short_memory"), dict) else {}).get("conversation_tail"))
            if isinstance(context, dict)
            else []
        )
        directions = {str(item.get("direction")) for item in conversation_tail if isinstance(item, dict)}
        cases.append(
            _result(
                "conversation_tail_keeps_inbound_and_outbound",
                "inbound" in directions and "outbound" in directions,
                f"directions={sorted(directions)}",
            )
        )

        commitment_core = CommitmentCore(state_store)
        ordinary_search_text = "帮我找一下柴静在 YouTube 最新一期视频的标题是什么"
        cases.append(
            _result(
                "ordinary_help_search_is_not_proactive_request",
                not commitment_core._looks_like_proactive_request(ordinary_search_text, ordinary_search_text.lower()),
                "plain help/search request should stay in Core answer or evidence route",
            )
        )
        ordinary_search_event = normalizer.user_message(
            text=ordinary_search_text,
            channel="smoke",
            user_id="dialogue-regression",
            session_id="dialogue-regression-session",
        )
        ordinary_turn = commitment_core.process_turn(
            event=ordinary_search_event,
            user_text=str(ordinary_search_event.payload.get("text") or ""),
            assistant_response="需要可验证外部证据。",
            route="agent",
            status="success",
        )
        cases.append(
            _result(
                "ordinary_latest_search_does_not_create_proactive_draft",
                ordinary_turn == {},
                f"commitment_turn={ordinary_turn}",
            )
        )

        topic_correction_event = normalizer.user_message(
            text="什么 PyTorch，我说的是柴静在 YouTube 最新一期视频标题",
            channel="smoke",
            user_id="dialogue-regression",
            session_id="dialogue-regression-session",
        )
        topic_correction_turn = commitment_core.process_turn(
            event=topic_correction_event,
            user_text=str(topic_correction_event.payload.get("text") or ""),
            assistant_response="我会按柴静 YouTube 最新视频继续处理。",
            route="agent",
            status="success",
        )
        cases.append(
            _result(
                "topic_correction_with_pytorch_word_does_not_create_tracking",
                topic_correction_turn == {},
                f"commitment_turn={topic_correction_turn}",
            )
        )

        pending = commitment_core.create_commitment(
            {
                "kind": "external_digest",
                "status": "pending_confirmation",
                "title": "外部追踪：PyTorch",
                "user_id": "dialogue-regression",
                "channel": "smoke",
                "session_id": "dialogue-regression-session",
                "payload": {"topic": "PyTorch"},
            }
        )
        followup_event = normalizer.user_message(
            text="找到了吗",
            channel="smoke",
            user_id="dialogue-regression",
            session_id="dialogue-regression-session",
        )
        followup_turn = commitment_core.process_turn(
            event=followup_event,
            user_text=str(followup_event.payload.get("text") or ""),
            assistant_response="还没有可验证结果。",
            route="direct_answer",
            status="success",
        )
        still_pending = commitment_core.get_commitment(str(pending.get("commitment_id") or ""))
        cases.append(
            _result(
                "followup_question_does_not_confirm_pending_commitment",
                followup_turn == {} and (still_pending or {}).get("status") == "pending_confirmation",
                f"commitment_turn={followup_turn}, pending={still_pending}",
            )
        )

        loop.commitment_core = commitment_core
        confirm_event = normalizer.user_message(
            text="好的",
            channel="smoke",
            user_id="dialogue-regression",
            session_id="dialogue-regression-session",
        )
        confirm_result = loop.handle_event(confirm_event).to_dict()
        followups = confirm_result.get("followup_messages") or []
        cases.append(
            _result(
                "commitment_confirmation_renders_primary_outcome",
                "已开启" in str(confirm_result.get("response") or "") and not any("已开启" in str(item) for item in followups),
                f"response={confirm_result.get('response')}, followups={followups}",
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
