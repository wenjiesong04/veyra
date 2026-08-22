#!/usr/bin/env python3
"""Focused, virtual-clock acceptance for the generic Living Reaction slice."""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.world_state import WorldStateStore  # noqa: E402
from interface.living_reaction_contract import FEEDBACK_LABELS, ReactionInput  # noqa: E402
from runtime.living_reaction_policy import apply_feedback_effect, decide_reaction  # noqa: E402
from runtime.living_reaction_runtime import LivingReactionRuntime  # noqa: E402

# A Need-driven disposition must never reuse the no-material-change sentence:
# saying nothing needs an interruption while asking a question contradicts the
# question itself.
NO_MATERIAL_SIGNAL_COPY = "There is no new material signal that needs an interruption right now."


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value.astimezone(timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: int) -> None:
        self.value += timedelta(**kwargs)


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"PASS {label}")


SCENARIOS = (
    {
        "name": "下周去上海出差",
        "category": "calendar_deadline",
        "title": "上海行程",
        "goal": "按时到达并完成安排",
        "summary": "一项有明确时间窗口的安排",
        "change": "时间窗口已进入需要确认的阶段",
        "need_source": "calendar",
        "need_question": "具体的会议时间和地点是什么？",
    },
    {
        "name": "最近准备面试",
        "category": "preparation_deadline",
        "title": "准备事项",
        "goal": "在目标日期前完成准备",
        "summary": "一个需要逐步推进的准备事项",
        "change": "准备目标仍有关键未知项",
        "need_source": "user_input",
        "need_question": "目标日期和最重要的准备项是什么？",
    },
    {
        "name": "月底要搬家",
        "category": "transition_deadline",
        "title": "月底安排",
        "goal": "平稳完成月底安排",
        "summary": "一项会影响多个后续安排的变化",
        "change": "原先的安排发生了材料级变化",
        "need_source": "user_input",
        "need_question": "哪些安排已经确认，哪些还可能变化？",
    },
)


def payload(
    scenario: dict[str, str],
    *,
    owner: str,
    session: str,
    situation_id: str,
    revision: int = 1,
    now: datetime,
    quiet: bool = False,
    need: bool = True,
    source_available: bool = True,
    consented: bool = True,
    material: bool = True,
    category: str | None = None,
    deadline_at: datetime | None = None,
    attention_trigger: str = "none",
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "owner_id": owner,
        "session_id": session,
        "now": now.isoformat(),
        "quiet_hours": quiet,
        "consent": {scenario["need_source"]: consented},
        "source_availability": {scenario["need_source"]: source_available},
        "attention_trigger": attention_trigger,
        "situation": {
            "owner_id": owner,
            "session_id": session,
            "situation_id": situation_id,
            "revision": revision,
            "category": category or scenario["category"],
            "status": "active",
            "title": scenario["title"],
            "goal": scenario["goal"],
            "summary": scenario["summary"],
            "known": [f"用户明确提到：{scenario['name']}"],
            "unknown": [scenario["need_question"]],
            "evidence": [{"ref": f"user:{situation_id}:1", "kind": "user_report"}],
            "next_step": "先确认最有影响的下一步",
            "risk": "medium",
        },
    }
    if material:
        result["situation"]["material_change"] = {
            "id": f"change_{situation_id}_{revision}",
            "revision": 1,
            "statement": scenario["change"],
            "why_now": "这个变化会影响下一次判断。",
        }
    else:
        result["situation"]["unknown"] = ["下一次观察点尚未到来"]
    if deadline_at is not None:
        result["situation"]["deadline_at"] = deadline_at.isoformat()
    if need:
        result["information_need"] = {
            "need_id": f"need_{situation_id}",
            "revision": 1,
            "status": "open",
            "kind": "clarification",
            "question": scenario["need_question"],
            "source": scenario["need_source"],
            "priority": "high",
        }
    else:
        result["information_need"] = None
    return result


def feedback(
    *,
    feedback_id: str,
    label: str,
    decision: dict[str, Any],
    now: datetime,
    category: str = "",
    remind_before_seconds: int | None = None,
) -> dict[str, Any]:
    return {
        "feedback_id": feedback_id,
        "owner_id": decision["owner_id"],
        "session_id": decision["session_id"],
        "situation_id": decision["situation_id"],
        "reaction_id": decision["reaction_id"],
        "label": label,
        "category": category,
        "remind_before_seconds": remind_before_seconds,
        "now": now.isoformat(),
    }


def main() -> int:
    now = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
    owner = "living-owner"
    session = "living-session"
    with TemporaryDirectory(prefix="veyra-living-reaction-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        clock = MutableClock(now)
        runtime = LivingReactionRuntime(store, clock=clock)

        # The same mechanism receives three unrelated shapes of life context.
        rows: list[dict[str, Any]] = []
        for index, scenario in enumerate(SCENARIOS, start=1):
            row = payload(
                scenario,
                owner=owner,
                session=session,
                situation_id=f"situation_{index}",
                now=clock(),
                need=index < 3,
                source_available=index == 1,
                consented=index == 1,
            )
            result = runtime.evaluate(row)
            rows.append(result["decision"])
        expect(rows[0]["disposition"] == "read", "authorised source selects read")
        expect(rows[1]["disposition"] == "ask", "missing source/consent selects ask")
        expect(rows[2]["disposition"] == "suggest", "material change selects suggest")
        risk_need = payload(
            SCENARIOS[1],
            owner=owner,
            session=session,
            situation_id="situation_high_risk_need",
            now=clock(),
            need=True,
            material=False,
            source_available=False,
            consented=False,
        )
        risk_need["situation"]["risk"] = "high"
        risk_result = runtime.evaluate(risk_need)
        expect(risk_result["decision"]["disposition"] == "suggest", "high risk outranks a remaining Need")
        calendar_trigger = runtime.evaluate(
            payload(
                SCENARIOS[0],
                owner=owner,
                session=session,
                situation_id="situation_calendar_trigger",
                now=clock(),
                need=True,
                material=False,
                source_available=True,
                consented=True,
                attention_trigger="material_observation",
            )
        )
        expect(calendar_trigger["decision"]["disposition"] == "suggest", "actionable Calendar observation outranks a remaining Need")
        ordinary_calendar = runtime.evaluate(
            payload(
                SCENARIOS[0],
                owner=owner,
                session=session,
                situation_id="situation_ordinary_calendar",
                now=clock(),
                need=True,
                material=False,
                source_available=True,
                consented=True,
            )
        )
        expect(ordinary_calendar["decision"]["disposition"] == "read", "ordinary Calendar keeps the remaining Need first")
        expect(all(row["authority"] == {"execution": False, "external_delivery": False, "permission_expansion": False} for row in rows), "all reactions remain non-authoritative")
        expect(all(row["external_delivery"] is False for row in rows), "no reaction delivers externally")
        expect(all(row["what_happened"] and row["why_it_matters"] and row["why_now"] and row["suggested_next_step"] for row in rows), "reaction explanation fields are populated")
        expect(all(set(row["fact_vs_inference"]) == {"facts", "inferences"} for row in rows), "fact and inference boundary is explicit")

        reaction_count_before_replay = runtime.status()["reaction_count"]
        replay = runtime.evaluate(
            payload(SCENARIOS[0], owner=owner, session=session, situation_id="situation_1", now=clock(), source_available=True, consented=True)
        )
        expect(replay["status"] == "duplicate", "same material revision is idempotent")
        expect(runtime.status()["reaction_count"] == reaction_count_before_replay, "duplicate replay does not grow ledger")

        quiet_read = runtime.evaluate(
            payload(SCENARIOS[0], owner=owner, session=session, situation_id="situation_read_quiet", now=clock(), quiet=True, need=True, source_available=True, consented=True)
        )
        expect(quiet_read["decision"]["disposition"] == "read", "quiet hours do not block an authorised background read")
        suppressed_read = runtime.record_feedback(
            feedback(feedback_id="feedback_read_suppressed", label="too_frequent", decision=quiet_read["decision"], now=clock())
        )
        expect(suppressed_read["feedback"]["aftereffects"]["current_situation"]["new"]["suppression_until"], "read suppression is recorded as an interruption strategy")
        read_after_suppression = runtime.evaluate(
            payload(SCENARIOS[0], owner=owner, session=session, situation_id="situation_read_quiet", revision=2, now=clock(), quiet=True, need=True, source_available=True, consented=True)
        )
        expect(read_after_suppression["decision"]["disposition"] == "read", "cooldown and suppression do not block an authorised background read")
        expect(read_after_suppression["decision"]["suppression"]["bypassed_for_background_read"] is True and read_after_suppression["decision"]["cooldown"]["bypassed_for_background_read"] is True, "read bypass is explicit in the reaction audit")

        # Every core active InformationNeed state remains actionable.  A ready
        # connector wins over fallback_reaction; a user source still requires
        # an ask even when its flag is present.
        for index, status in enumerate(("asked", "observing", "waiting"), start=1):
            active_need = payload(
                SCENARIOS[0],
                owner=owner,
                session=session,
                situation_id=f"situation_need_{status}",
                now=clock(),
                source_available=True,
                consented=True,
            )
            active_need["information_need"]["status"] = status
            active_need["information_need"]["fallback_reaction"] = "ask"
            active_result = runtime.evaluate(active_need)
            expect(active_result["decision"]["disposition"] == "read", f"{status} need prefers a ready connector")
        user_need = payload(
            SCENARIOS[1],
            owner=owner,
            session=session,
            situation_id="situation_user_source",
            now=clock(),
            source_available=True,
            consented=True,
        )
        user_need["information_need"]["fallback_reaction"] = "read"
        expect(runtime.evaluate(user_need)["decision"]["disposition"] == "ask", "user source remains an ask")

        ignore_read = runtime.evaluate(
            payload(SCENARIOS[0], owner=owner, session=session, situation_id="situation_ignore_read", now=clock(), source_available=True, consented=True)
        )
        runtime.record_feedback(feedback(feedback_id="feedback_ignore_read", label="ignore", decision=ignore_read["decision"], now=clock()))
        ignored = runtime.evaluate(
            payload(SCENARIOS[0], owner=owner, session=session, situation_id="situation_ignore_read", revision=2, now=clock(), source_available=True, consented=True)
        )
        expect(ignored["decision"]["disposition"] == "silent" and ignored["decision"]["reason"] == "ignore", "ignore stops further reads")
        resolved_read = runtime.evaluate(
            payload(SCENARIOS[0], owner=owner, session=session, situation_id="situation_resolved_read", now=clock(), source_available=True, consented=True)
        )
        runtime.record_feedback(feedback(feedback_id="feedback_resolved_read", label="resolved", decision=resolved_read["decision"], now=clock()))
        resolved_after = runtime.evaluate(
            payload(SCENARIOS[0], owner=owner, session=session, situation_id="situation_resolved_read", revision=2, now=clock(), source_available=True, consented=True)
        )
        expect(resolved_after["decision"]["disposition"] == "silent" and resolved_after["decision"]["reason"] == "resolved", "resolved stops further reads")

        terminal = payload(SCENARIOS[0], owner=owner, session=session, situation_id="situation_terminal", now=clock(), need=True, source_available=True, consented=True)
        terminal["situation"]["status"] = "resolved"
        terminal_result = runtime.evaluate(terminal)
        expect(terminal_result["decision"]["disposition"] == "silent" and terminal_result["decision"]["reason"] == "terminal_situation", "resolved Situation is terminally silent")

        deadline = clock() + timedelta(hours=48)
        before_window = runtime.evaluate(
            payload(SCENARIOS[0], owner=owner, session=session, situation_id="situation_deadline", now=clock(), need=False, deadline_at=deadline)
        )
        expect(before_window["decision"]["disposition"] == "wait", "deadline outside reminder window waits")
        expect(before_window["decision"]["timing"]["phase"] == "before_window", "deadline phase is server-owned")
        before_replay = runtime.evaluate(
            payload(SCENARIOS[0], owner=owner, session=session, situation_id="situation_deadline", now=clock(), need=False, deadline_at=deadline)
        )
        expect(before_replay["status"] == "duplicate", "same temporal phase is idempotent")
        clock.advance(hours=25)
        in_window = runtime.evaluate(
            payload(SCENARIOS[0], owner=owner, session=session, situation_id="situation_deadline", now=clock(), need=False, deadline_at=deadline)
        )
        expect(in_window["decision"]["disposition"] == "suggest", "crossing reminder threshold creates a suggest")
        expect(in_window["decision"]["timing"]["phase"] == "in_window" and in_window["status"] == "recorded", "temporal phase boundary creates a new ledger reaction")
        in_window_replay = runtime.evaluate(
            payload(SCENARIOS[0], owner=owner, session=session, situation_id="situation_deadline", now=clock(), need=False, deadline_at=deadline)
        )
        expect(in_window_replay["status"] == "duplicate", "repeated in-window ticks do not grow ledger")

        reminder_deadline = clock() + timedelta(hours=48)
        reminder_before = runtime.evaluate(
            payload(SCENARIOS[0], owner=owner, session=session, situation_id="situation_reminder", now=clock(), need=False, deadline_at=reminder_deadline)
        )
        expect(reminder_before["decision"]["disposition"] == "wait", "default reminder remains outside the window")
        reminder_feedback = runtime.record_feedback(
            feedback(feedback_id="feedback_remind_before", label="remind_before", decision=reminder_before["decision"], now=clock(), remind_before_seconds=72 * 3600)
        )
        expect(reminder_feedback["feedback"]["aftereffects"]["current_situation"]["new"]["remind_before_seconds"] == 72 * 3600, "remind_before persists a new trigger policy")
        reminder_after = runtime.evaluate(
            payload(SCENARIOS[0], owner=owner, session=session, situation_id="situation_reminder", now=clock(), need=False, deadline_at=reminder_deadline)
        )
        expect(reminder_after["decision"]["disposition"] == "suggest" and reminder_after["decision"]["timing"]["phase"] == "in_window", "remind_before actually moves the suggest window earlier")

        early_deadline = clock() + timedelta(hours=48)
        early_before = runtime.evaluate(
            payload(SCENARIOS[0], owner=owner, session=session, situation_id="situation_early", now=clock(), need=False, deadline_at=early_deadline)
        )
        runtime.record_feedback(
            feedback(feedback_id="feedback_too_early", label="too_early", decision=early_before["decision"], now=clock())
        )
        clock.advance(hours=26)
        early_delayed = runtime.evaluate(
            payload(SCENARIOS[0], owner=owner, session=session, situation_id="situation_early", revision=2, now=clock(), need=False, deadline_at=early_deadline)
        )
        expect(early_delayed["decision"]["disposition"] == "wait" and early_delayed["decision"]["timing"]["offset_seconds"] == 6 * 3600, "too_early delays the actual suggest boundary")
        clock.advance(hours=5)
        early_due = runtime.evaluate(
            payload(SCENARIOS[0], owner=owner, session=session, situation_id="situation_early", revision=3, now=clock(), need=False, deadline_at=early_deadline)
        )
        expect(early_due["decision"]["disposition"] == "suggest", "too_early eventually reaches the shifted boundary")

        late_deadline = clock() + timedelta(hours=48)
        late_before = runtime.evaluate(
            payload(SCENARIOS[0], owner=owner, session=session, situation_id="situation_late", now=clock(), need=False, deadline_at=late_deadline)
        )
        runtime.record_feedback(
            feedback(feedback_id="feedback_too_late", label="too_late", decision=late_before["decision"], now=clock())
        )
        clock.advance(hours=20)
        late_due = runtime.evaluate(
            payload(SCENARIOS[0], owner=owner, session=session, situation_id="situation_late", revision=2, now=clock(), need=False, deadline_at=late_deadline)
        )
        expect(late_due["decision"]["disposition"] == "suggest" and late_due["decision"]["timing"]["offset_seconds"] == -6 * 3600, "too_late advances the actual suggest boundary")

        quiet = runtime.evaluate(
            payload(SCENARIOS[1], owner=owner, session=session, situation_id="situation_1_quiet", now=clock(), quiet=True, need=True, source_available=False, consented=False)
        )
        expect(quiet["decision"]["disposition"] == "silent", "quiet hours suppress an otherwise necessary ask")
        expect(quiet["decision"]["reason"] == "quiet_hours", "quiet suppression is explainable")

        waiting = runtime.evaluate(
            payload(SCENARIOS[0], owner=owner, session=session, situation_id="situation_wait", now=clock(), need=False, material=False)
        )
        expect(waiting["decision"]["disposition"] == "wait", "no actionable change chooses wait")

        # Explicit feedback immediately changes a current Situation's future.
        too_frequent = runtime.record_feedback(
            feedback(feedback_id="feedback_too_frequent", label="too_frequent", decision=rows[2], now=clock())
        )
        effect = too_frequent["feedback"]["aftereffects"]["current_situation"]["new"]
        expect(effect["suppression_until"] is not None and effect["rank_multiplier"] < 1, "too_frequent immediately changes suppression and ranking")
        revised_input = payload(SCENARIOS[2], owner=owner, session=session, situation_id="situation_3", revision=2, now=clock(), need=False)
        suppressed = runtime.evaluate(revised_input)
        expect(suppressed["decision"]["disposition"] == "silent", "current Situation feedback suppresses next revision")

        # Timing feedback is observable after its local cooldown, without
        # widening any permission boundary.
        reminder = runtime.record_feedback(
            feedback(feedback_id="feedback_reminder", label="remind_before", decision=rows[0], now=clock(), remind_before_seconds=7200)
        )
        expect(reminder["feedback"]["aftereffects"]["current_situation"]["new"]["remind_before_seconds"] == 7200, "remind_before changes timing")
        clock.advance(hours=3)
        timed = runtime.evaluate(
            payload(SCENARIOS[0], owner=owner, session=session, situation_id="situation_1", revision=2, now=clock(), source_available=True, consented=True)
        )
        expect(timed["decision"]["timing"]["remind_before_seconds"] == 7200, "timing aftereffect persists into a new revision")

        # Three distinct situations are required before category policy learns.
        cross_results: list[dict[str, Any]] = []
        for index, scenario in enumerate(SCENARIOS, start=1):
            current = runtime.evaluate(
                payload(scenario, owner=owner, session=session, situation_id=f"cross_{index}", now=clock(), need=False, category="shared_timing")
            )
            cross_results.append(current["decision"])
            aftereffect = runtime.record_feedback(
                feedback(feedback_id=f"feedback_early_{index}", label="too_early", decision=current["decision"], now=clock(), category="shared_timing")
            )
            if index < 3:
                expect(aftereffect["feedback"]["aftereffects"]["cross_situation"]["status"] == "insufficient_samples", f"cross-category sample {index} stays descriptive")
            else:
                cross_policy = aftereffect["feedback"]["aftereffects"]["cross_situation"]
                expect(cross_policy["status"] == "revised", "three independent situations create bounded policy revision")
                expect(cross_policy["old"] != cross_policy["new"], "policy revision exposes old and new")
                expect(cross_policy["evidence_refs"] and cross_policy["rollback"], "policy revision has evidence and rollback")
                expect(cross_policy["expires_at"], "policy revision expires")
        policy_input = payload(SCENARIOS[0], owner=owner, session=session, situation_id="cross_after", now=clock(), need=False, category="shared_timing")
        learned = runtime.evaluate(policy_input)
        expect(learned["decision"]["timing"]["offset_seconds"] == 6 * 3600, "bounded category timing effect is applied after three samples")

        expect(FEEDBACK_LABELS == {"ignore", "resolved", "useful", "not_useful", "too_early", "too_late", "too_frequent", "remind_before"}, "all feedback labels are in the contract")
        for label in sorted(FEEDBACK_LABELS):
            effect = apply_feedback_effect({}, label, now=clock())
            expect(effect["cooldown_until"] and effect["last_feedback_label"] == label, f"feedback label {label} has a bounded aftereffect")
        status = runtime.status()
        expect(status["reaction_count"] >= 9 and status["feedback_count"] == 11, "ledger retains reactions and feedback")
        expect(status["policy_revision_count"] == 1 and status["category_count"] == 1, "one bounded category revision is recorded")

        # The ledger freezes on a malformed record instead of filtering it,
        # and concurrent identical evaluations still produce one durable row.
        reaction_path = store.path_for(runtime.STATE_FILE)
        valid_state = runtime.read_state()
        def corrupt_reaction(state: dict[str, Any]) -> dict[str, Any]:
            next(iter(state["reactions"].values()))["external_delivery"] = True
            return state

        store.mutate_json(runtime.STATE_FILE, corrupt_reaction)
        expect(runtime.status()["status"] == "degraded", "malformed reaction authority freezes the ledger")
        store.mutate_json(runtime.STATE_FILE, lambda state: valid_state)
        expect(reaction_path.exists() and runtime.status()["status"] == "success", "valid reaction state restores")
        same_input = payload(SCENARIOS[0], owner=owner, session=session, situation_id="concurrent_replay", revision=1, now=clock(), source_available=True, consented=True)
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(runtime.evaluate, same_input) for _ in range(8)]
            outcomes = [future.result() for future in as_completed(futures)]
        expect(sum(item["status"] == "recorded" for item in outcomes) == 1 and sum(item["status"] == "duplicate" for item in outcomes) == 7, "concurrent reaction replay is idempotent")

        need_driven = payload(SCENARIOS[0], owner=owner, session=session, situation_id="need_why_now", revision=1, now=clock(), source_available=False, consented=False, material=False)
        need_driven["information_need"].pop("why_now", None)
        asked = decide_reaction(ReactionInput.from_mapping(need_driven))
        expect(asked.disposition == "ask", "an unconsented user Need still asks", asked.disposition)
        expect(asked.why_now != NO_MATERIAL_SIGNAL_COPY, "an ask never claims that nothing needs an interruption", asked.why_now)
        expect(bool(asked.why_now.strip()), "an ask still explains why now", asked.why_now)

        bound_need = payload(SCENARIOS[1], owner=owner, session=session, situation_id="need_why_now_bound", revision=1, now=clock(), source_available=False, consented=False, material=False)
        bound_need["information_need"]["why_now"] = "用户需要先确认这一项才能继续。"
        bound = decide_reaction(ReactionInput.from_mapping(bound_need))
        expect(bound.why_now == "用户需要先确认这一项才能继续。", "the Need's own why_now reaches the reaction", bound.why_now)

        no_need = payload(SCENARIOS[2], owner=owner, session=session, situation_id="need_why_now_absent", revision=1, now=clock(), need=False, material=False)
        quiet_decision = decide_reaction(ReactionInput.from_mapping(no_need))
        expect(quiet_decision.disposition in {"silent", "wait"}, "no open Need stays non-interrupting", quiet_decision.disposition)
        print("RESULT living reaction smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
