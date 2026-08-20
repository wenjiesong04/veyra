#!/usr/bin/env python3
"""Parameterized user-life Situation smoke.

The fixture labels are intentionally different, while the runtime path below
is identical for every row.
"""

from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.world_state import WorldStateStore  # noqa: E402
from interface.event_schema import EventSource, EventType, VeyraEvent  # noqa: E402
from interface.living_context_contract import CandidateNeed, ContextQuote, LivingContextCandidate  # noqa: E402
from runtime.living_context_runtime import LivingContextRuntime  # noqa: E402


def expect(condition: bool, label: str, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail}")
    print(f"ok - {label}")


FIXTURES = [
    {
        "text": "下周去上海出差",
        "subject": "上海出差",
        "title": "上海出差",
        "goal": "顺利完成出差",
        "blocked": "出发时间",
        "question": "什么时候出发？",
    },
    {
        "text": "最近准备面试",
        "subject": "面试准备",
        "title": "面试准备",
        "goal": "准备好面试",
        "blocked": "面试时间和准备重点",
        "question": "面试大约什么时候、最想先准备哪一部分？",
    },
    {
        "text": "月底要搬家",
        "subject": "月底搬家",
        "title": "月底搬家",
        "goal": "按时完成搬家",
        "blocked": "搬家日期和当前安排",
        "question": "搬家具体是哪一天？",
    },
]


def event(event_id: str, text: str, *, user_id: str = "lifecycle-user", session_id: str = "lifecycle-session") -> VeyraEvent:
    return VeyraEvent(
        type=EventType.USER_MESSAGE,
        source=EventSource(channel="api", user_id=user_id, session_id=session_id),
        payload={"text": text},
        event_id=event_id,
    )


def candidate(
    item: dict[str, str],
    *,
    token: str | None = None,
    disposition: str = "create",
    catalog_row: dict[str, object] | None = None,
    source_text: str | None = None,
    direct_user: bool = False,
) -> LivingContextCandidate:
    need = CandidateNeed(
        blocked_judgment=item["blocked"],
        evidence_kind="user",
        why_now="这个信息会改变下一步判断。",
        urgency=0.6,
        allowed_source_classes=["user"],
        fallback_reaction="ask",
        question=item["question"],
    )
    return LivingContextCandidate(
        schema_version="veyra.living_context_candidate.v1",
        disposition=disposition,
        situation_token=token,
        situation_revision=(int(catalog_row["observation_revision"]) if catalog_row else None),
        catalog_token=(str(catalog_row["catalog_token"]) if catalog_row else None),
        create_subject=item["subject"] if disposition == "create" else "",
        title=item["title"],
        summary=item["text"],
        goal=item["goal"],
        lifecycle="active",
        known=[],
        unknown=[item["blocked"]],
        timeline=[],
        material_change="",
        needs=[need],
        requested_reaction="ask",
        source_quote=(
            ContextQuote(text=source_text, start=0, end=len(source_text))
            if direct_user and source_text
            else None
        ),
        assertion_mode="direct_user" if direct_user else "inferred",
        source="model",
    )


def main() -> int:
    with TemporaryDirectory(prefix="veyra-living-context-") as tmp:
        store = WorldStateStore(tmp)
        runtime = LivingContextRuntime(store)
        situation_ids: list[str] = []
        need_ids: list[str] = []
        for index, fixture in enumerate(FIXTURES, start=1):
            first_event = event(f"evt_lc_{index}_create", fixture["text"])
            result = runtime.process_user_turn(
                first_event,
                SimpleNamespace(living_context_candidate=candidate(fixture)),
            )
            expect(result["status"] == "recorded", f"fixture {index} recorded")
            situation = result["situation"]
            situation_ids.append(str(situation["situation_id"]))
            expect(situation["record_kind"] == "semantic_situation", f"fixture {index} uses shared Situation truth")
            expect(situation["semantic"]["known"][0]["epistemic_status"] == "inferred", f"fixture {index} unquoted model summary is inferred")
            expect(situation["observations"][0]["epistemic_status"] == "reported", f"fixture {index} original event is reported")
            expect(situation["semantic"]["timeline"], f"fixture {index} has timeline")
            expect(result["information_needs"], f"fixture {index} has InformationNeed")
            expect(result["reaction_hint"]["kind"] == "ask", f"fixture {index} exposes ask as a non-authoritative hint")
            need_ids.append(str(result["information_needs"][0]["need_id"]))

            update_text = f"{fixture['blocked']} 已有新的确认"
            update_event = event(f"evt_lc_{index}_update", update_text)
            catalog_row = next(
                item for item in runtime.model_catalog(
                    owner_id="lifecycle-user",
                    session_id="lifecycle-session",
                )
                if str(item.get("situation_token")) == str(situation["situation_id"])
            )
            update = runtime.process_user_turn(
                update_event,
                SimpleNamespace(
                    living_context_candidate=candidate(
                        {**fixture, "text": update_text},
                        token=str(situation["situation_id"]),
                        disposition="update",
                        catalog_row=catalog_row,
                    )
                ),
                catalog=runtime.model_catalog(
                    owner_id="lifecycle-user",
                    session_id="lifecycle-session",
                ),
                expected_revision=int(situation["observation_revision"]),
            )
            expect(update["situation"]["situation_id"] == situation["situation_id"], f"fixture {index} updates stable identity")
            expect(int(update["situation"]["observation_revision"]) == 2, f"fixture {index} CAS revision advances")

            replay = runtime.process_user_turn(
                first_event,
                SimpleNamespace(living_context_candidate=candidate(fixture)),
            )
            expect(replay["situation"]["semantic_replayed"] is True, f"fixture {index} event replay is idempotent")

        answer_event = event("evt_lc_answer", "周一上午出发、我会提前准备材料")
        answered = runtime.answer_need(answer_event, need_ids[0])
        expect(answered["status"] == "answered", "answer resolves one InformationNeed")
        expect(answered["need"]["status"] == "resolved", "resolved need is durable")
        expect(answered["situation"]["semantic"]["known"][-1]["epistemic_status"] == "reported", "answer updates Known as reported")
        expect(answered["situation"]["semantic"]["timeline"][-1]["source_event_id"] == "evt_lc_answer", "answer updates timeline")
        answered_replay = runtime.answer_need(answer_event, need_ids[0])
        expect(answered_replay.get("replayed") is True, "answer event replay is idempotent")

        restarted = LivingContextRuntime(WorldStateStore(tmp))
        restored = restarted.list_situations(owner_id="lifecycle-user", session_id="lifecycle-session")
        expect(len(restored) == 3, "three Situations survive restart")
        expect({item["situation_id"] for item in restored} == set(situation_ids), "restart preserves stable Situation IDs")

        wrong_owner = restarted.get_situation(
            situation_ids[0],
            owner_id="other-user",
            session_id="lifecycle-session",
        )
        expect(wrong_owner is None, "cross-owner read is isolated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
