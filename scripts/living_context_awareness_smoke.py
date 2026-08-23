#!/usr/bin/env python3
"""Model-shaped V1 smoke: three generic Situations plus the main loop seam.

The fake model emits the same typed candidate contract for all fixtures.  The
fixture words live only in this test; production code has no scenario keyword
fallback or source query construction.
"""

from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.awareness_loop import AwarenessLoop  # noqa: E402
from core.runtime_entity import RuntimeEntity  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402
from interface.event_schema import Decision, EventSource, EventType, RiskLevel, Route, VeyraEvent  # noqa: E402
from interface.living_context_contract import LivingContextCandidate  # noqa: E402
from runtime.living_context_composition import build_living_context_composition  # noqa: E402
from runtime.living_context_runtime import LivingContextRuntime  # noqa: E402


FIXTURES = [
    {
        "id": "travel",
        "text": "下周去上海出差",
        "subject": "上海出差",
        "category": "travel",
        "goal": "顺利完成出差",
        "unknown": "出发时间",
        "question": "什么时候出发？",
    },
    {
        "id": "interview",
        "text": "最近准备面试",
        "subject": "面试准备",
        "category": "work",
        "goal": "准备好面试",
        "unknown": "面试时间和准备重点",
        "question": "面试大约什么时候、最想先准备哪一部分？",
    },
    {
        "id": "move",
        "text": "月底要搬家",
        "subject": "月底搬家",
        "category": "logistics",
        "goal": "按时完成搬家",
        "unknown": "搬家具体日期",
        "question": "搬家具体是哪一天？",
    },
]
FIXTURE_BY_ID = {str(item["id"]): item for item in FIXTURES}


def expect(condition: bool, label: str) -> None:
    if not condition:
        raise AssertionError(label)
    print(f"ok - {label}")


def event(event_id: str, fixture_id: str) -> VeyraEvent:
    fixture = FIXTURE_BY_ID[fixture_id]
    return VeyraEvent(
        type=EventType.USER_MESSAGE,
        source=EventSource(channel="api", user_id="awareness-user", session_id="awareness-session"),
        payload={
            "text": fixture["text"],
            "metadata": {"fixture_id": fixture_id},
        },
        event_id=event_id,
    )


class FakeUnderstandingCore:
    """A deterministic model seam returning strict JSON-shaped output."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def build(self, *, text: str, attention_focus: list[str], event: VeyraEvent, turn_context: dict[str, object]):
        from core.understanding_core import TurnUnderstanding

        self.calls.append(str(event.payload.get("metadata", {}).get("fixture_id")))
        fixture_id = str(event.payload.get("metadata", {}).get("fixture_id") or "")
        fixture = FIXTURE_BY_ID[fixture_id]
        catalog = turn_context.get("living_context_situation_candidates")
        matching = next(
            (
                item
                for item in catalog
                if isinstance(item, dict) and item.get("category") == fixture["category"]
            ),
            None,
        ) if isinstance(catalog, list) else None
        candidate: dict[str, object] = {
            "schema_version": "veyra.living_context_candidate.v1",
            "disposition": "update" if matching else "create",
            "situation_token": matching.get("situation_token") if matching else None,
            "situation_revision": matching.get("observation_revision") if matching else None,
            "catalog_token": matching.get("catalog_token") if matching else None,
            "create_subject": fixture["subject"] if not matching else "",
            "category": fixture["category"],
            "label": fixture["subject"],
            "title": fixture["subject"],
            "summary": str(fixture["text"]),
            "goal": fixture["goal"],
            "lifecycle": "active",
            "known": [{"statement": str(fixture["text"]), "epistemic_status": "reported"}],
            "unknown": [fixture["unknown"]],
            "assumptions": [],
            "timeline": [{"statement": str(fixture["text"]), "occurred_at": event.occurred_at, "material": True}],
            "material_change": "用户补充了新的进展" if matching else "",
            "next_step": f"确认{fixture['unknown']}",
            "next_step_epistemic_status": "inferred",
            "needs": [
                {
                    "blocked_judgment": fixture["unknown"],
                    "evidence_kind": "user",
                    "why_now": "这个未知会改变下一步判断。",
                    "urgency": 0.6,
                    "allowed_source_classes": ["user"],
                    "fallback_reaction": "ask",
                    "question": fixture["question"],
                }
            ],
            "answered_need_tokens": [],
            "requested_reaction": "ask",
            "reopen": False,
            "reopen_reason": "",
            "source": "model",
        }
        return TurnUnderstanding.from_payload(
            {
                "source": "model",
                "situation_assessment": {
                    "intent": "conversation",
                    "task_type": "chat",
                    "task_summary": str(fixture["text"]),
                    "explicit_request": str(fixture["text"]),
                    "user_goal": fixture["goal"],
                    "living_context_candidate": candidate,
                },
            },
            source_text=text,
        )


class CountingModelClient:
    """One real Understanding seam; any second model purpose fails the smoke."""

    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.purposes: list[str] = []

    def status(self) -> dict[str, object]:
        return {"configured": True, "decision_mode": "always"}

    def complete_json(self, *, purpose: str, system: str, user: str) -> dict[str, object]:
        del system, user
        self.purposes.append(purpose)
        if purpose != "turn_understanding":
            raise AssertionError(f"unexpected second model purpose: {purpose}")
        return self.payload


def typed_model_payload(text: str) -> dict[str, object]:
    # Reuse the established strict model-boundary fixture so this test counts
    # real complete_json purposes without duplicating the whole candidate schema.
    from scripts.model_boundary_smoke import candidate_payload, model_payload

    candidate = candidate_payload()
    candidate.update(
        {
            "create_subject": "户外团建",
            "summary": text,
            "goal": "完成户外团建",
            "known": [{"statement": text, "epistemic_status": "报告"}],
            "unknown": ["具体日期"],
            "next_step": "确认具体日期",
        }
    )
    candidate["needs"] = [
        {
            "blocked_judgment": "具体日期",
            "evidence_kind": "weather",
            "observation_requirement": {
                "coverage": "forecast_day",
                "metrics": ["weather_description", "temperature_2m_max", "temperature_2m_min"],
            },
            "why_now": "影响安排",
            "urgency": 0.6,
            "allowed_source_classes": ["weather"],
            "fallback_reaction": "询问",
            "question": "团建具体是哪一天？",
        }
    ]
    payload = model_payload(candidate, text=text)
    # A pure state statement opts into the exact typed kind that permits the
    # server-owned foreground acknowledgement. Questions/requests keep their
    # ordinary primary response and LC follow-up.
    payload["semantic_frame"]["acts"][0]["kind"] = "assertion"
    payload["semantic_frame"]["acts"][0]["goal"] = text
    payload["situation_assessment"]["evidence_gap"] = {
        "needs_fresh_evidence": True,
        "evidence_kind": "weather",
        "what_would_change_the_answer": "天气观测",
    }
    return payload


def main() -> int:
    with TemporaryDirectory(prefix="veyra-living-context-awareness-") as tmp:
        store = WorldStateStore(tmp)
        composition = build_living_context_composition(store)
        runtime = composition.orchestrator
        loop = AwarenessLoop(store, RuntimeEntity(store))
        loop.attach_living_context_runtime(runtime)
        fake = FakeUnderstandingCore()
        loop.understanding_core = fake

        situation_ids: list[str] = []
        for index, fixture in enumerate(FIXTURES, start=1):
            result = loop.handle_event(event(f"evt_awareness_{index}", str(fixture["id"])))
            artifact = result.artifacts.get("living_context")
            expect(isinstance(artifact, dict), f"fixture {index} has living_context artifact")
            expect(artifact.get("status") == "recorded", f"fixture {index} is admitted")
            situation = artifact.get("situation")
            expect(isinstance(situation, dict), f"fixture {index} has Situation projection")
            situation_ids.append(str(situation["situation_id"]))
            expect(result.followup_messages, f"fixture {index} keeps the non-typed Living Context reaction as a followup")

        update = loop.handle_event(event("evt_awareness_update", str(FIXTURES[0]["id"])))
        update_artifact = update.artifacts["living_context"]
        expect(update_artifact.get("operation") == "update", "second turn reuses catalog token")
        expect(update_artifact["situation"]["situation_id"] == situation_ids[0], "second turn keeps stable identity")

        low_latency = loop.handle_event(
            VeyraEvent(
                type=EventType.USER_MESSAGE,
                source=EventSource(channel="api", user_id="awareness-user", session_id="awareness-session"),
                payload={"text": "hi"},
                event_id="evt_awareness_low_latency",
            )
        )
        expect(
            low_latency.artifacts["living_context"].get("status") == "skipped",
            "low-latency reply carries an explicit skipped Living Context artifact",
        )

        restarted = build_living_context_composition(WorldStateStore(tmp)).core
        restored = restarted.list_situations(owner_id="awareness-user", session_id="awareness-session")
        expect({str(item["situation_id"]) for item in restored} == set(situation_ids), "restart restores all three Situations")
        expect(len(fake.calls) == 4, "fake model was used through one generic loop path")

        typed_text = "下周六户外团建在上海。"
        typed_store = WorldStateStore(tmp)
        typed_loop = AwarenessLoop(typed_store, RuntimeEntity(typed_store))
        typed_loop.attach_living_context_runtime(runtime)
        counting_client = CountingModelClient(typed_model_payload(typed_text))
        typed_loop.core_reasoning.client = counting_client
        typed_result = typed_loop.handle_event(
            VeyraEvent(
                type=EventType.USER_MESSAGE,
                source=EventSource(channel="api", user_id="typed-user", session_id="typed-session"),
                payload={"text": typed_text},
                event_id="evt_awareness_typed_model",
            )
        )
        expect(counting_client.purposes == ["turn_understanding"], "typed state turn uses only Understanding model call")
        typed_assist = typed_result.artifacts.get("decision", {}).get("model_assist", {})
        typed_turn = typed_assist.get("turn_understanding", {}) if isinstance(typed_assist, dict) else {}
        typed_frame = typed_assist.get("semantic_frame", {}) if isinstance(typed_assist, dict) else {}
        expect(typed_turn.get("needs_fresh_evidence") is True, "background weather Need remains visible in understanding")
        expect(typed_frame.get("acts", [{}])[0].get("evidence_need") == "none", "foreground assertion keeps its own evidence need")
        expect(typed_assist.get("typed_policy_fast_path") is True, "foreground uses typed fast path")
        expect(not typed_result.followup_messages and typed_result.response.startswith("已记录"), "typed server reaction is the only response")

        typed_decision = Decision(
            route=Route.DIRECT_ANSWER,
            risk_level=RiskLevel.R0,
            reason="typed read-only state turn",
            model_assist={
                "typed_policy_fast_path": True,
                "typed_state_assertion_fast_path": True,
                "semantic_policy": {"preferred_route": "direct_answer"},
            },
        )
        typed_artifact = {
            "status": "recorded",
            "authority": {"route": False, "tool": False, "execution": False, "delivery": False},
            "situation": {"semantic": {"title": "团建安排"}},
            "current_reaction": {
                "disposition": "ask",
                "suggested_next_step": "团建具体是哪一天？",
            },
        }
        loop._attach_typed_living_context_response(typed_decision, typed_artifact)
        answer_calls: list[str] = []
        loop.core_reasoning.answer_assist = lambda **_: answer_calls.append("answer_assist") or {"status": "skipped"}  # type: ignore[method-assign]
        typed_event = VeyraEvent(
            type=EventType.USER_MESSAGE,
            source=EventSource(channel="api", user_id="awareness-user", session_id="awareness-session"),
            payload={"text": "下周六户外团建在上海。"},
            event_id="evt_awareness_typed_primary",
        )
        typed_response = loop._direct_answer(typed_event, typed_decision, [], persona_patch={})
        expect("团建具体是哪一天？" in typed_response, "typed Living Context response is reused directly")
        expect(not answer_calls, "typed Living Context response skips answer_assist")

        # A question/request is not a pure state assertion: its real primary
        # answer must survive while the LC reaction remains a follow-up.
        mixed_decision = Decision(
            route=Route.DIRECT_ANSWER,
            risk_level=RiskLevel.R0,
            reason="typed question turn",
            model_assist={
                "typed_policy_fast_path": True,
                "typed_state_assertion_fast_path": False,
                "semantic_policy": {"preferred_route": "direct_answer"},
            },
        )
        mixed_artifact = {
            "status": "recorded",
            "authority": {"route": False, "tool": False, "execution": False, "delivery": False},
            "situation": {"semantic": {"title": "天气查询"}},
            "current_reaction": {
                "disposition": "ask",
                "suggested_next_step": "要查哪个城市？",
            },
        }
        loop._attach_typed_living_context_response(mixed_decision, mixed_artifact)
        mixed_calls: list[str] = []
        loop.core_reasoning.answer_assist = lambda **_: mixed_calls.append("answer") or {
            "status": "model_assisted",
            "draft_response": "这是问题本身的回答。",
        }  # type: ignore[method-assign]
        mixed_response = loop._direct_answer(typed_event, mixed_decision, [], persona_patch={})
        expect(mixed_response == "这是问题本身的回答。", "question primary is not swallowed by LC reaction")
        expect(mixed_calls == ["answer"], "question primary may use its normal answer model")

        for disposition in ("ask", "suggest", "read", "wait", "silent"):
            reaction = {"disposition": disposition}
            if disposition == "ask":
                reaction["suggested_next_step"] = "补充一个关键信息？"
            elif disposition == "suggest":
                reaction.update(
                    {
                        "what_happened": "状态出现变化",
                        "why_it_matters": "会影响下一步",
                        "why_now": "现在值得看一眼",
                        "suggested_next_step": "确认下一步",
                    }
                )
            reaction_artifact = {
                "status": "recorded",
                "authority": {"route": False, "tool": False, "execution": False, "delivery": False},
                "situation": {"semantic": {"title": "四种反应"}},
                "current_reaction": reaction,
            }
            reaction_decision = Decision(
                route=Route.DIRECT_ANSWER,
                risk_level=RiskLevel.R0,
                reason="typed assertion reaction",
                model_assist={
                    "typed_policy_fast_path": True,
                    "typed_state_assertion_fast_path": True,
                    "semantic_policy": {"preferred_route": "direct_answer"},
                },
            )
            loop._attach_typed_living_context_response(reaction_decision, reaction_artifact)
            reaction_calls: list[str] = []
            loop.core_reasoning.answer_assist = lambda **_: reaction_calls.append("answer") or {"status": "skipped"}  # type: ignore[method-assign]
            response = loop._direct_answer(typed_event, reaction_decision, [], persona_patch={})
            expect(response.startswith("已记录"), f"{disposition} typed assertion has server foreground ack")
            expect(not reaction_calls, f"{disposition} typed assertion skips second model")

        ask_followup = AwarenessLoop._living_context_followup(
            {
                "status": "recorded",
                "situation": {"semantic": {"title": "面试准备"}},
                "current_reaction": {
                    "disposition": "ask",
                    "suggested_next_step": "面试大约是什么时候？",
                },
            }
        )
        expect("面试大约是什么时候？" in ask_followup, "ask followup uses the reaction-bound question")
        suggest_followup = AwarenessLoop._living_context_followup(
            {
                "status": "recorded",
                "situation": {"semantic": {"title": "搬家安排"}},
                "current_reaction": {
                    "disposition": "suggest",
                    "what_happened": "搬家日期发生变化",
                    "why_it_matters": "它影响原来的安排",
                    "why_now": "时间已经临近",
                    "suggested_next_step": "重新确认搬家清单",
                },
            }
        )
        expect("发生了" in suggest_followup and "建议" in suggest_followup, "suggest followup explains why now")
        for disposition, reason in (("read", "open_information_need"), ("wait", "deadline_outside_reminder_window"), ("silent", "quiet_hours"), ("silent", "feedback_suppression")):
            expect(
                not AwarenessLoop._living_context_followup(
                    {
                        "status": "recorded",
                        "situation": {"semantic": {"title": "受控 Situation"}},
                        "current_reaction": {"disposition": disposition, "reason": reason},
                    }
                ),
                f"{reason} reaction does not leak a user followup",
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
