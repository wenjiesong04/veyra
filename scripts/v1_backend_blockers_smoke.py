#!/usr/bin/env python3
"""Adversarial smoke for the V1 backend longevity seams.

This is intentionally separate from the product happy-path acceptance: it
checks retry recovery, consent expiry renewal, source retention, recurring
observations, answer preflight, and Today's server-owned attention ranking.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
import sys
import threading
from tempfile import TemporaryDirectory
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.world_state import StateRevisionConflictError, WorldStateStore  # noqa: E402
from interface.event_schema import EventSource, EventType, VeyraEvent  # noqa: E402
from interface.living_context_contract import CandidateNeed, CandidateNeedReference, ContextQuote, LivingReactionFeedback  # noqa: E402
from interface.living_source_contract import SourceConsent, canonical_utc  # noqa: E402
from runtime.calendar_source import CalendarSource  # noqa: E402
from runtime.living_context_composition import build_living_context_composition  # noqa: E402
from runtime.information_need_runtime import InformationNeedRuntime  # noqa: E402
from runtime.living_source_runtime import LivingSourceRuntime  # noqa: E402
from runtime.product_experience import ProductExperienceService  # noqa: E402
from scripts.living_source_smoke import FIXED_NOW, NeedCatalog, _binding, _consent  # noqa: E402
from scripts.v1_living_context_acceptance_smoke import FIXTURES, candidate, event  # noqa: E402


class Clock:
    def __init__(self) -> None:
        self.value = FIXED_NOW

    def __call__(self):
        return self.value

    def advance(self, **kwargs: int) -> None:
        self.value += timedelta(**kwargs)


class RetryProvider:
    provider_id = "retry.fixture.v1"

    def __init__(self) -> None:
        self.calls = 0

    def read(self, context):
        self.calls += 1
        if self.calls <= 4:
            return {"status": "unknown"}
        return {"status": "ok", "details": {"location": "Shanghai", "current": {"temperature_2m": 20}}}


class StableProvider:
    provider_id = "stable.fixture.v1"

    def __init__(self) -> None:
        self.calls = 0

    def read(self, context):
        self.calls += 1
        return {"status": "ok", "details": {"location": "Shanghai", "current": {"temperature_2m": 20}}}


def expect(value: bool, label: str) -> None:
    if not value:
        raise AssertionError(label)
    print(f"PASS {label}")


def retry_and_consent_checks(root: Path) -> None:
    catalog = NeedCatalog()
    catalog.add("retry", "weather")
    clock = Clock()
    provider = RetryProvider()
    runtime = LivingSourceRuntime(
        WorldStateStore(root / "retry"),
        current_need_resolver=catalog.resolve,
        providers={"weather": provider},
        clock=clock,
    )
    runtime.register_binding(_binding(catalog, "retry", "weather", {"location": "Shanghai"}))
    runtime.grant_consent(_consent("weather", now=FIXED_NOW))
    statuses = []
    for seconds in (0.0, 0.6, 1.7, 3.8, 8.0):
        receipt = runtime.request(
            "retry",
            "weather",
            user_id="u-v1",
            session_id="s-v1",
            now=FIXED_NOW + timedelta(seconds=seconds),
        )
        statuses.append(receipt.status)
    expect(statuses[:4] == ["unknown"] * 4 and statuses[4] == "ok", "transient source outage recovers after bounded attempt epoch")
    expect(provider.calls == 5, "retry backoff avoids tight-loop provider calls")

    catalog.add("renew", "weather")
    renewal = LivingSourceRuntime(
        WorldStateStore(root / "renew"),
        current_need_resolver=catalog.resolve,
        providers={"weather": StableProvider()},
        clock=clock,
    )
    renewal.register_binding(_binding(catalog, "renew", "weather", {"location": "Shanghai"}))
    consent = SourceConsent(
        consent_id="expiring-consent",
        user_id="u-v1",
        workspace_id="server-derived",
        session_id="s-v1",
        source="weather",
        purpose="expiry renewal",
        granted_at=canonical_utc(clock()),
        expires_at=canonical_utc(clock() + timedelta(seconds=1)),
        generation=1,
    )
    renewal.grant_consent(consent)
    clock.advance(seconds=2)
    renewed_row = dict(consent.to_dict())
    renewed_row.pop("schema_version", None)
    renewed_row.update({"granted_at": canonical_utc(clock()), "expires_at": canonical_utc(clock() + timedelta(days=1)), "granted": True})
    renewed = renewal.grant_consent(SourceConsent(**renewed_row))
    expect(renewed.generation == 2, "expired consent renews with an exact next generation")


def retention_check(root: Path) -> None:
    catalog = NeedCatalog()
    catalog.add("first", "weather")
    catalog.add("second", "weather")
    class FailingProvider:
        provider_id = "retention.fixture.v1"

        def read(self, context):
            return {"status": "unknown"}

    runtime = LivingSourceRuntime(
        WorldStateStore(root / "retention"),
        max_receipts=1,
        current_need_resolver=catalog.resolve,
        providers={"weather": FailingProvider()},
        clock=lambda: FIXED_NOW,
    )
    runtime.register_binding(_binding(catalog, "first", "weather", {"location": "Shanghai"}))
    runtime.register_binding(_binding(catalog, "second", "weather", {"location": "Beijing"}))
    runtime.grant_consent(_consent("weather"))
    runtime.request("first", "weather", user_id="u-v1", session_id="s-v1", now=FIXED_NOW)
    runtime.request("second", "weather", user_id="u-v1", session_id="s-v1", now=FIXED_NOW + timedelta(seconds=1))
    state = runtime.state_store.read_json(runtime.state_file)
    expect(len(state["requests"]) == 1 and len(state["receipts"]) == 1, "terminal source request and receipt retention remains bounded")


def recurring_and_today_checks(root: Path) -> None:
    clock = Clock()
    provider = StableProvider()
    store = WorldStateStore(root / "product")
    composition = build_living_context_composition(store, clock=clock, source_providers={"weather": provider})
    owner, session = "backend-owner", "backend-session"
    composition.orchestrator.grant_source_consent("weather", owner_id=owner, session_id=session, expected_generation=0)
    fixture = dict(FIXTURES[1], id="recurring", category="work", source="weather")
    catalog = composition.orchestrator.model_catalog(owner_id=owner, session_id=session)
    result = composition.orchestrator.process_user_turn(
        event("recurring-create", owner, session, str(fixture["text"])),
        type("Understanding", (), {"living_context_candidate": candidate(fixture, catalog), "living_reaction_feedback": None})(),
        catalog=catalog,
    )
    situation_id = str(result["situation"]["situation_id"])
    first_tick = composition.orchestrator.tick(owner_id=owner, session_id=session)
    first_need = composition.core.needs.list(owner_id=owner, session_id=session, situation_id=situation_id, limit=8)[0]
    expect(first_tick["evaluated_count"] == 1 and first_need["status"] == "resolved", "successful source observation resolves the current Need")
    first_revision = int(result["situation"]["observation_revision"])
    clock.advance(seconds=901)
    second_tick = composition.orchestrator.tick(owner_id=owner, session_id=session)
    second_need = composition.core.needs.list(owner_id=owner, session_id=session, situation_id=situation_id, limit=8)[0]
    expect(provider.calls == 2 and second_need["need_id"] == first_need["need_id"] and second_need["generation"] == 2, "receipt TTL reopens the exact Need for recurring observation")
    expect(int(second_tick["items"][0]["reaction"].get("decision", {}).get("situation_revision") or 0) > first_revision, "recurring observation updates Situation understanding")
    composition.orchestrator.tick(owner_id=owner, session_id=session)
    expect(provider.calls == 2, "recurring Need does not churn before the next receipt TTL")

    service = ProductExperienceService(store, living_context_orchestrator=composition.orchestrator)
    today = service.today(user_id=owner, session_id=session)
    expect(not today["attention"], "ordinary recurring weather does not manufacture Today attention")
    expect(not today["recent_changes"], "ordinary recurring weather does not appear as a recent material change")

    # Exercise the generic projection with two controlled signal shapes.  This
    # keeps ranking coverage independent from the ordinary-weather fixture:
    # one Situation is deadline-driven, the other is driven by a material
    # change and an unresolved unknown.
    controlled_situations = [
        {
            "situation_id": "controlled-deadline",
            "revision": 1,
            "title": "A planned item",
            "summary": "A bounded deadline signal.",
            "status": "active",
            "deadline_at": "2026-01-01T00:00:00+00:00",
            "material_change": "",
            "unknown": [],
        },
        {
            "situation_id": "controlled-unknown",
            "revision": 1,
            "title": "An evolving item",
            "summary": "A bounded change and unknown signal.",
            "status": "active",
            "deadline_at": None,
            "material_change": "The plan changed.",
            "unknown": ["The next decision is not yet known."],
        },
    ]
    controlled_reactions = [
        {
            "situation_id": "controlled-deadline",
            "situation_revision": 1,
            "disposition": "suggest",
            "rank": 0.9,
            "why_now": "The deadline is relevant now.",
        },
        {
            "situation_id": "controlled-unknown",
            "situation_revision": 1,
            "disposition": "wait",
            "rank": 0.2,
            "why_now": "The next decision is still unclear.",
        },
    ]
    attention = ProductExperienceService._attention_rows(controlled_situations, controlled_reactions, limit=8)
    expect(attention and all(attention[i]["rank"] >= attention[i + 1]["rank"] for i in range(len(attention) - 1)), "Today attention projection ranks controlled signals in descending order")
    expect(len(attention) == 2 and set(attention[0]["signals"]) != set(attention[1]["signals"]), "Today attention projection preserves distinct signal sources")
    expect(all(item["disposition"] == "suggest" for item in today["suggestions"]), "Today suggestions contain only suggest reactions")


def observation_reopen_boundary_checks(root: Path) -> None:
    """Observation reopen propagates corruption and uses an exact Need CAS."""

    corrupt_clock = Clock()
    corrupt_store = WorldStateStore(root / "observation-corrupt")
    corrupt_catalog = NeedCatalog()
    corrupt_catalog.add("observation-corrupt", "weather")
    corrupt_composition = build_living_context_composition(
        corrupt_store,
        clock=corrupt_clock,
        source_providers={"weather": StableProvider()},
    )
    owner, session = "observation-owner", "observation-session"
    corrupt_composition.orchestrator.grant_source_consent(
        "weather", owner_id=owner, session_id=session, expected_generation=0
    )
    fixture = dict(FIXTURES[1], id="observation-corrupt", category="work", source="weather")
    catalog = corrupt_composition.orchestrator.model_catalog(owner_id=owner, session_id=session)
    created = corrupt_composition.orchestrator.process_user_turn(
        event("observation-corrupt-create", owner, session, str(fixture["text"])),
        SimpleNamespace(living_context_candidate=candidate(fixture, catalog), living_reaction_feedback=None),
        catalog=catalog,
    )
    situation_id = str(created["situation"]["situation_id"])
    corrupt_composition.orchestrator.tick(owner_id=owner, session_id=session)
    corrupt_clock.advance(seconds=901)
    corrupt_store.mutate_json(
        "information_need_state.json",
        lambda state: state.__setitem__("_state_corrupt", True) or state,
    )
    corrupt_tick = corrupt_composition.orchestrator.tick(owner_id=owner, session_id=session)
    expect(corrupt_tick["status"] == "degraded" and corrupt_tick["error_count"] >= 1, "corrupt InformationNeed state degrades the background tick")

    source_corrupt_clock = Clock()
    source_corrupt_store = WorldStateStore(root / "observation-source-corrupt")
    source_corrupt_composition = build_living_context_composition(
        source_corrupt_store,
        clock=source_corrupt_clock,
        source_providers={"weather": StableProvider()},
    )
    source_owner, source_session = "source-corrupt-owner", "source-corrupt-session"
    source_corrupt_composition.orchestrator.grant_source_consent(
        "weather", owner_id=source_owner, session_id=source_session, expected_generation=0
    )
    source_fixture = dict(FIXTURES[1], id="observation-source-corrupt", category="work", source="weather")
    source_catalog = source_corrupt_composition.orchestrator.model_catalog(owner_id=source_owner, session_id=source_session)
    source_created = source_corrupt_composition.orchestrator.process_user_turn(
        event("observation-source-corrupt-create", source_owner, source_session, str(source_fixture["text"])),
        SimpleNamespace(living_context_candidate=candidate(source_fixture, source_catalog), living_reaction_feedback=None),
        catalog=source_catalog,
    )
    source_corrupt_composition.orchestrator.tick(owner_id=source_owner, session_id=source_session)
    source_corrupt_clock.advance(seconds=901)
    source_corrupt_store.mutate_json(
        "living_source_state.json",
        lambda state: state.__setitem__("_state_corrupt", True) or state,
    )
    source_corrupt_tick = source_corrupt_composition.orchestrator.tick(owner_id=source_owner, session_id=source_session)
    expect(source_corrupt_tick["status"] == "degraded" and source_corrupt_tick["error_count"] >= 1, "corrupt source state degrades the observation tick")

    # A concurrent terminal/new-generation transition must not be reopened a
    # second time from the scheduler's old resolved snapshot.
    race_clock = Clock()
    race_store = WorldStateStore(root / "observation-race")
    race_composition = build_living_context_composition(
        race_store,
        clock=race_clock,
        source_providers={"weather": StableProvider()},
    )
    race_owner, race_session = "observation-race-owner", "observation-race-session"
    race_composition.orchestrator.grant_source_consent(
        "weather", owner_id=race_owner, session_id=race_session, expected_generation=0
    )
    race_fixture = dict(FIXTURES[1], id="observation-race", category="work", source="weather")
    race_catalog = race_composition.orchestrator.model_catalog(owner_id=race_owner, session_id=race_session)
    race_created = race_composition.orchestrator.process_user_turn(
        event("observation-race-create", race_owner, race_session, str(race_fixture["text"])),
        SimpleNamespace(living_context_candidate=candidate(race_fixture, race_catalog), living_reaction_feedback=None),
        catalog=race_catalog,
    )
    race_situation_id = str(race_created["situation"]["situation_id"])
    race_composition.orchestrator.tick(owner_id=race_owner, session_id=race_session)
    race_clock.advance(seconds=901)
    race_situation = race_composition.core.get_situation(race_situation_id, owner_id=race_owner, session_id=race_session)
    original_upsert = race_composition.core.needs.upsert_for_situation

    def concurrent_generation(**kwargs):
        if kwargs.get("expected_generation") is not None:
            racing_kwargs = dict(kwargs)
            racing_kwargs.pop("expected_generation", None)
            racing_kwargs.pop("expected_status", None)
            racing_kwargs.pop("expected_record_digest", None)
            racing_kwargs["source_event_id"] = "observation-race-new-generation"
            advanced = original_upsert(**racing_kwargs)[0]
            race_composition.core.needs.resolve(
                advanced["need_id"],
                owner_id=race_owner,
                session_id=race_session,
                answered_by_event_id="observation-race-terminal",
                expected_generation=int(advanced["generation"]),
            )
        return original_upsert(**kwargs)

    race_composition.core.needs.upsert_for_situation = concurrent_generation
    try:
        reopened = race_composition.orchestrator._ensure_observation_need(
            race_situation,
            owner_id=race_owner,
            session_id=race_session,
        )
    finally:
        race_composition.core.needs.upsert_for_situation = original_upsert
    race_needs = race_composition.core.needs.list(
        owner_id=race_owner,
        session_id=race_session,
        situation_id=race_situation_id,
        limit=8,
    )
    expect(reopened is None, "stale observation reopen is a quiet no-op")
    second_reopened = race_composition.orchestrator._ensure_observation_need(
        race_situation,
        owner_id=race_owner,
        session_id=race_session,
    )
    expect(second_reopened is None, "an older receipt cannot reopen a later terminal Need generation")
    expect(
        len(race_needs) == 1
        and int(race_needs[0]["generation"]) == 2
        and race_needs[0]["status"] == "resolved",
        "concurrent terminal/new-generation transition is not reopened again",
    )


def answer_preflight_check(root: Path) -> None:
    store = WorldStateStore(root / "answer")
    composition = build_living_context_composition(store)
    owner, session = "answer-owner", "answer-session"
    fixture = dict(FIXTURES[1], id="answer", category="personal", source="user", subject="Answer context", unknown="user confirmation", question="Can you confirm?")
    catalog = composition.orchestrator.model_catalog(owner_id=owner, session_id=session)
    result = composition.orchestrator.process_user_turn(
        event("answer-create", owner, session, str(fixture["text"])),
        type("Understanding", (), {"living_context_candidate": candidate(fixture, catalog), "living_reaction_feedback": None})(),
        catalog=catalog,
    )
    need = composition.core.needs.list(owner_id=owner, session_id=session, situation_id=result["situation"]["situation_id"], limit=8)[0]
    composition.core.needs.resolve(need["need_id"], owner_id=owner, session_id=session, answered_by_event_id="external-resolution", expected_generation=need["generation"])
    before = composition.core.get_situation(result["situation"]["situation_id"], owner_id=owner, session_id=session)
    try:
        composition.core.answer_need(
            VeyraEvent(type=EventType.USER_MESSAGE, source=EventSource(channel="api", user_id=owner, session_id=session), payload={"text": "already resolved"}, event_id="answer-stale"),
            need["need_id"],
        )
    except StateRevisionConflictError:
        pass
    else:
        raise AssertionError("stale answer unexpectedly committed")
    after = composition.core.get_situation(result["situation"]["situation_id"], owner_id=owner, session_id=session)
    expect(before["observation_revision"] == after["observation_revision"], "stale answer preflight leaves Situation untouched")


def need_binding_checks(root: Path) -> None:
    """Need generations and user answers share one exact unknown endpoint."""

    store = WorldStateStore(root / "need-binding")
    needs = InformationNeedRuntime(store)
    source_need = CandidateNeed(
        blocked_judgment="具体日程",
        evidence_kind="calendar",
        why_now="日程会改变下一步判断。",
        allowed_source_classes=["calendar"],
        fallback_reaction="read",
        question="日程是什么？",
    )
    first = needs.upsert_for_situation(
        situation_id="binding-situation",
        owner_id="binding-owner",
        session_id="binding-session",
        needs=[source_need],
        source_event_id="binding-create",
        unknown_bindings={"具体日程": "旧日程表达"},
    )[0]
    needs.resolve(
        first["need_id"],
        owner_id="binding-owner",
        session_id="binding-session",
        answered_by_event_id="binding-answer",
        expected_generation=first["generation"],
    )
    reopened = needs.upsert_for_situation(
        situation_id="binding-situation",
        owner_id="binding-owner",
        session_id="binding-session",
        needs=[source_need],
        source_event_id="binding-reopen",
    )[0]
    expect(reopened["generation"] == 2 and reopened.get("unknown_binding") is None, "new Need generation does not inherit an old unknown binding")

    answer_store = WorldStateStore(root / "answer-binding")
    answer_composition = build_living_context_composition(answer_store)
    owner, session = "answer-binding-owner", "answer-binding-session"
    fixture = dict(FIXTURES[1], id="answer-binding", category="personal", source="user", subject="Answer context", unknown="user confirmation", question="Can you confirm?")
    catalog = answer_composition.orchestrator.model_catalog(owner_id=owner, session_id=session)
    paraphrase = candidate(fixture, catalog).model_copy(update={"unknown": ["用户确认的具体安排"]})
    created = answer_composition.orchestrator.process_user_turn(
        event("answer-binding-create", owner, session, str(fixture["text"])),
        SimpleNamespace(living_context_candidate=paraphrase, living_reaction_feedback=None),
        catalog=catalog,
    )
    situation_id = str(created["situation"]["situation_id"])
    need = answer_composition.core.needs.list(owner_id=owner, session_id=session, situation_id=situation_id, limit=8)[0]
    expect(need.get("unknown_binding") == "用户确认的具体安排", "Need stores the server-owned paraphrase binding")
    answered = answer_composition.core.answer_need(
        VeyraEvent(
            type=EventType.USER_MESSAGE,
            source=EventSource(channel="api", user_id=owner, session_id=session),
            payload={"text": "已经确认好了"},
            event_id="answer-binding-answer",
        ),
        str(need["need_id"]),
    )
    remaining_unknown = answered["situation"]["semantic"].get("unknown") or []
    expect(answered["status"] == "answered" and answered["unknown_binding"]["status"] == "cleared", "answer resolves a bound Need")
    expect("用户确认的具体安排" not in remaining_unknown and "user confirmation" not in remaining_unknown, "answer clears the exact bound unknown rather than only blocked text")

    rotation_store = WorldStateStore(root / "need-binding-rotation")
    rotation = build_living_context_composition(rotation_store)
    rotation_owner, rotation_session = "binding-rotation-owner", "binding-rotation-session"
    first_fixture = {
        "id": "binding-rotation-first",
        "text": "A first personal detail still needs confirmation.",
        "subject": "Changing information need",
        "category": "personal",
        "goal": "keep the current understanding accurate",
        "source": "user",
        "unknown": "the first missing detail",
        "question": "What is the first missing detail?",
        "entities": [],
    }
    first_catalog = rotation.orchestrator.model_catalog(
        owner_id=rotation_owner, session_id=rotation_session
    )
    rotation.orchestrator.process_user_turn(
        event(
            "binding-rotation-create",
            rotation_owner,
            rotation_session,
            str(first_fixture["text"]),
        ),
        SimpleNamespace(
            living_context_candidate=candidate(first_fixture, first_catalog),
            living_reaction_feedback=None,
        ),
        catalog=first_catalog,
    )
    second_fixture = {
        **first_fixture,
        "id": "binding-rotation-second",
        "text": "The next update introduces a different missing detail.",
        "unknown": "the second missing detail",
        "question": "What is the second missing detail?",
    }
    second_catalog = rotation.orchestrator.model_catalog(
        owner_id=rotation_owner, session_id=rotation_session
    )
    rotated = rotation.orchestrator.process_user_turn(
        event(
            "binding-rotation-update",
            rotation_owner,
            rotation_session,
            str(second_fixture["text"]),
        ),
        SimpleNamespace(
            living_context_candidate=candidate(second_fixture, second_catalog),
            living_reaction_feedback=None,
        ),
        catalog=second_catalog,
    )
    rotated_needs = rotation.core.needs.list(
        owner_id=rotation_owner,
        session_id=rotation_session,
        situation_id=str(rotated["situation"]["situation_id"]),
        limit=8,
    )
    expect(
        any(item.get("blocked_judgment") == "the second missing detail" for item in rotated_needs),
        "a new candidate Need does not inherit an unrelated old unknown binding",
    )

    # A natural-language continuation may explicitly answer one Need while
    # leaving another unknown open.  The runtime must clear only the exact
    # server-owned endpoint, never the human-readable blocked judgment.
    continuation_store = WorldStateStore(root / "unknown-reconciliation")
    continuation = build_living_context_composition(continuation_store)
    continuation_owner, continuation_session = "unknown-owner", "unknown-session"
    bound_unknown = "搬家公司服务未确认"
    remaining_unknown = "网络迁移服务未确认"
    continuation_catalog = continuation.orchestrator.model_catalog(
        owner_id=continuation_owner,
        session_id=continuation_session,
    )
    seed = candidate(
        {
            **FIXTURES[2],
            "category": "logistics",
            "subject": "月底搬家",
            "goal": "完成月底搬家",
            "unknown": bound_unknown,
            "question": "搬家公司是否已经预约？",
            "source": "user",
            "text": "月底要搬家。",
        },
        continuation_catalog,
    )
    second_need = seed.needs[0].model_copy(
        update={
            "blocked_judgment": remaining_unknown,
            "question": "网络迁移服务是否已经确认？",
        }
    )
    seed = seed.model_copy(
        update={
            "unknown": [bound_unknown, remaining_unknown],
            "needs": [seed.needs[0], second_need],
        }
    )
    created = continuation.orchestrator.process_user_turn(
        event(
            "unknown-reconciliation-create",
            continuation_owner,
            continuation_session,
            "月底要搬家。",
        ),
        SimpleNamespace(living_context_candidate=seed, living_reaction_feedback=None),
        catalog=continuation_catalog,
    )
    continuation_situation_id = str(created["situation"]["situation_id"])
    created_needs = continuation.core.needs.list(
        owner_id=continuation_owner,
        session_id=continuation_session,
        situation_id=continuation_situation_id,
        limit=8,
    )
    bound_need = next(item for item in created_needs if item["blocked_judgment"] == bound_unknown)
    open_second = next(item for item in created_needs if item["blocked_judgment"] == remaining_unknown)
    expect(
        bound_need.get("unknown_binding") == bound_unknown
        and open_second.get("unknown_binding") == remaining_unknown,
        "continuation fixture persists one exact binding per Need",
    )
    update_catalog = continuation.orchestrator.model_catalog(
        owner_id=continuation_owner,
        session_id=continuation_session,
    )
    update_row = next(
        row for row in update_catalog if str(row.get("situation_token")) == continuation_situation_id
    )
    update_payload = seed.model_dump(mode="json")
    update_payload.update(
        {
            "disposition": "update",
            "situation_token": continuation_situation_id,
            "situation_revision": int(update_row["observation_revision"]),
            "catalog_token": update_row["catalog_token"],
            "create_subject": "",
            "unknown": [remaining_unknown],
            "needs": [second_need.model_dump(mode="json")],
            "answered_need_tokens": [bound_need["need_id"]],
            "answered_need_bindings": [
                CandidateNeedReference(
                    need_token=str(bound_need["need_id"]),
                    generation=int(bound_need["generation"]),
                ).model_dump(mode="json")
            ],
            "material_change": "搬家公司已经预约好了；只剩网络迁移未确认。",
        }
    )
    update_candidate = seed.__class__.model_validate(update_payload, strict=True)
    followup_text = "日期确定8月29日，搬家公司已经预约好了；只剩网络迁移未确认。"
    followup_event = event(
        "unknown-reconciliation-update",
        continuation_owner,
        continuation_session,
        followup_text,
    )
    updated = continuation.orchestrator.process_user_turn(
        followup_event,
        SimpleNamespace(living_context_candidate=update_candidate, living_reaction_feedback=None),
        catalog=update_catalog,
    )
    updated_unknown = (updated["situation"].get("semantic") or {}).get("unknown") or []
    expect(
        updated["status"] == "recorded"
        and bound_unknown not in updated_unknown
        and remaining_unknown in updated_unknown
        and int(updated["situation"]["observation_revision"]) == 2,
        "natural-language continuation clears only the answered bound unknown",
    )
    updated_needs = continuation.core.needs.list(
        owner_id=continuation_owner,
        session_id=continuation_session,
        situation_id=continuation_situation_id,
        limit=8,
    )
    expect(
        next(item for item in updated_needs if item["need_id"] == bound_need["need_id"])["status"] == "resolved"
        and next(item for item in updated_needs if item["need_id"] == open_second["need_id"])["status"] in {"open", "waiting"},
        "answering one Need leaves the other Need active",
    )
    replayed = continuation.orchestrator.process_user_turn(
        followup_event,
        SimpleNamespace(living_context_candidate=update_candidate, living_reaction_feedback=None),
        catalog=update_catalog,
    )
    expect(
        replayed.get("semantic_replayed") is True
        and ((replayed.get("situation") or {}).get("semantic") or {}).get("unknown") == updated_unknown,
        "answered continuation replay is byte-stable",
    )
    restarted = build_living_context_composition(continuation_store)
    restarted_situation = restarted.core.get_situation(
        continuation_situation_id,
        owner_id=continuation_owner,
        session_id=continuation_session,
    )
    expect(
        ((restarted_situation or {}).get("semantic") or {}).get("unknown") == updated_unknown,
        "answered unknown reconciliation survives restart",
    )
    before_stale = restarted_situation
    try:
        restarted.orchestrator.process_user_turn(
            event(
                "unknown-reconciliation-stale-cas",
                continuation_owner,
                continuation_session,
                "搬家安排又补充了一条信息。",
            ),
            SimpleNamespace(living_context_candidate=update_candidate, living_reaction_feedback=None),
            catalog=update_catalog,
        )
    except StateRevisionConflictError:
        pass
    else:  # pragma: no cover - stale catalog must never be rebound
        raise AssertionError("stale unknown reconciliation catalog unexpectedly admitted")
    after_stale = restarted.core.get_situation(
        continuation_situation_id,
        owner_id=continuation_owner,
        session_id=continuation_session,
    )
    expect(
        (before_stale or {}).get("observation_revision") == (after_stale or {}).get("observation_revision")
        and ((after_stale or {}).get("semantic") or {}).get("unknown") == updated_unknown,
        "stale unknown reconciliation CAS leaves state untouched",
    )

    # If a Need has no binding because multiple unknowns were present, an
    # answer token resolves the Need but does not guess which semantic unknown
    # it addressed.
    unbound_store = WorldStateStore(root / "unknown-reconciliation-unbound")
    unbound = build_living_context_composition(unbound_store)
    unbound_owner, unbound_session = "unbound-owner", "unbound-session"
    unbound_catalog = unbound.orchestrator.model_catalog(
        owner_id=unbound_owner,
        session_id=unbound_session,
    )
    unbound_seed = candidate(
        {
            **FIXTURES[2],
            "category": "logistics",
            "subject": "Ambiguous move",
            "goal": "keep the move current",
            "unknown": "first unresolved detail",
            "question": "Which move detail is confirmed?",
            "source": "user",
            "text": "搬家还有几个细节待确认。",
        },
        unbound_catalog,
    ).model_copy(
        update={
            "unknown": ["first unresolved detail", "second unresolved detail"],
            "needs": [
                candidate(
                    {
                        **FIXTURES[2],
                        "category": "logistics",
                        "subject": "Ambiguous move",
                        "goal": "keep the move current",
                        "unknown": "a different blocked judgment",
                        "question": "Which move detail is confirmed?",
                        "source": "user",
                        "text": "搬家还有几个细节待确认。",
                    },
                    unbound_catalog,
                ).needs[0]
            ],
        }
    )
    unbound_created = unbound.orchestrator.process_user_turn(
        event("unknown-reconciliation-unbound-create", unbound_owner, unbound_session, "搬家还有几个细节待确认。"),
        SimpleNamespace(living_context_candidate=unbound_seed, living_reaction_feedback=None),
        catalog=unbound_catalog,
    )
    unbound_id = str(unbound_created["situation"]["situation_id"])
    unbound_need = unbound.core.needs.list(
        owner_id=unbound_owner,
        session_id=unbound_session,
        situation_id=unbound_id,
        limit=8,
    )[0]
    expect(unbound_need.get("unknown_binding") is None, "ambiguous unknowns remain unbound")
    unbound_update_catalog = unbound.orchestrator.model_catalog(
        owner_id=unbound_owner,
        session_id=unbound_session,
    )
    unbound_row = next(row for row in unbound_update_catalog if str(row.get("situation_token")) == unbound_id)
    unbound_payload = unbound_seed.model_dump(mode="json")
    unbound_payload.update(
        {
            "disposition": "update",
            "situation_token": unbound_id,
            "situation_revision": int(unbound_row["observation_revision"]),
            "catalog_token": unbound_row["catalog_token"],
            "create_subject": "",
            "unknown": ["second unresolved detail"],
            "needs": [],
            "answered_need_tokens": [unbound_need["need_id"]],
            "answered_need_bindings": [
                CandidateNeedReference(
                    need_token=str(unbound_need["need_id"]),
                    generation=int(unbound_need["generation"]),
                ).model_dump(mode="json")
            ],
        }
    )
    unbound_update = unbound.orchestrator.process_user_turn(
        event("unknown-reconciliation-unbound-update", unbound_owner, unbound_session, "搬家细节仍有多个未知。"),
        SimpleNamespace(
            living_context_candidate=unbound_seed.__class__.model_validate(unbound_payload, strict=True),
            living_reaction_feedback=None,
        ),
        catalog=unbound_update_catalog,
    )
    unbound_unknown = ((unbound_update["situation"].get("semantic") or {}).get("unknown") or [])
    expect(
        unbound_update["status"] == "recorded"
        and all(item in unbound_unknown for item in ("first unresolved detail", "second unresolved detail"))
        and unbound_unknown,
        "unbound multi-unknown answer resolves no guessed endpoint",
    )


def feedback_revision_checks(root: Path) -> None:
    """A stale catalog cannot learn; candidate+feedback remains one turn."""

    store = WorldStateStore(root / "feedback-revision")
    composition = build_living_context_composition(store)
    owner, session = "feedback-owner", "feedback-session"
    fixture = dict(FIXTURES[2], id="feedback", category="finance", source="other", subject="Deadline context", unknown="deadline gap", question="What remains?")
    catalog = composition.orchestrator.model_catalog(owner_id=owner, session_id=session)
    created = composition.orchestrator.process_user_turn(
        event("feedback-create", owner, session, str(fixture["text"])),
        SimpleNamespace(living_context_candidate=candidate(fixture, catalog), living_reaction_feedback=None),
        catalog=catalog,
    )
    situation_id = str(created["situation"]["situation_id"])
    old_catalog = composition.orchestrator.model_catalog(owner_id=owner, session_id=session)
    old_row = next(row for row in old_catalog if str(row.get("situation_token")) == situation_id)
    old_reaction = old_row.get("reaction") or {}
    reaction_token = str(old_reaction.get("reaction_token") or "")
    expect(reaction_token, "feedback regression has a server reaction token")

    feedback_text = "这个提醒很有用"
    feedback = LivingReactionFeedback(
        schema_version="veyra.living_reaction_feedback.v1",
        reaction_token=reaction_token,
        label="useful",
        source_quote=ContextQuote(text=feedback_text, start=0, end=len(feedback_text)),
    )
    combined = composition.orchestrator.process_user_turn(
        event("feedback-combined", owner, session, feedback_text),
        SimpleNamespace(
            living_context_candidate=candidate(fixture, old_catalog),
            living_reaction_feedback=feedback,
            living_reaction_feedback_issues=[],
        ),
        catalog=old_catalog,
    )
    expect(combined.get("status") == "recorded" and (combined.get("feedback") or {}).get("status") == "recorded", "candidate and turn-start feedback commit independently in one turn")
    feedback_count = int(composition.reaction.status().get("feedback_count") or 0)

    stale_text = "这个提醒不用管"
    stale_feedback = LivingReactionFeedback(
        schema_version="veyra.living_reaction_feedback.v1",
        reaction_token=reaction_token,
        label="ignore",
        source_quote=ContextQuote(text=stale_text, start=0, end=len(stale_text)),
    )
    stale = composition.orchestrator.process_user_turn(
        event("feedback-stale", owner, session, stale_text),
        SimpleNamespace(
            living_context_candidate=None,
            living_reaction_feedback=stale_feedback,
            living_reaction_feedback_issues=[],
        ),
        catalog=old_catalog,
    )
    expect((stale.get("feedback") or {}).get("status") == "ignored", "feedback from an old Situation revision is ignored")
    expect(int(composition.reaction.status().get("feedback_count") or 0) == feedback_count, "stale feedback does not alter cooldown or ranking state")

    # A resolved feedback and a candidate in the same turn must each keep its
    # own durable effect.  The final artifact is the post-resolve Situation,
    # not the active candidate snapshot returned before the resolve command.
    latest_catalog = composition.orchestrator.model_catalog(owner_id=owner, session_id=session)
    latest_row = next(row for row in latest_catalog if str(row.get("situation_token")) == situation_id)
    latest_revision = int(latest_row.get("observation_revision") or 0)
    resolved_text = "这件事已经解决"
    resolved_feedback = LivingReactionFeedback(
        schema_version="veyra.living_reaction_feedback.v1",
        reaction_token=str((latest_row.get("reaction") or {}).get("reaction_token") or ""),
        label="resolved",
        source_quote=ContextQuote(text=resolved_text, start=0, end=len(resolved_text)),
    )
    resolved_summary = "candidate update survives same-turn resolution"
    resolved_candidate = candidate(fixture, latest_catalog).model_copy(
        update={"needs": [], "summary": resolved_summary}
    )
    resolved = composition.orchestrator.process_user_turn(
        event("feedback-resolved-same-turn", owner, session, resolved_text),
        SimpleNamespace(
            living_context_candidate=resolved_candidate,
            living_reaction_feedback=resolved_feedback,
            living_reaction_feedback_issues=[],
        ),
        catalog=latest_catalog,
    )
    final_situation = composition.core.get_situation(situation_id, owner_id=owner, session_id=session)
    returned_situation = resolved.get("situation") if isinstance(resolved.get("situation"), dict) else {}
    feedback_situation = (resolved.get("feedback") or {}).get("situation")
    expect((resolved.get("feedback") or {}).get("status") == "recorded", "resolved feedback records beside the same-turn candidate")
    expect(
        isinstance(final_situation, dict)
        and final_situation.get("status") == "resolved"
        and (final_situation.get("semantic") or {}).get("lifecycle") == "resolved",
        "same-turn resolved feedback commits terminal Situation lifecycle",
    )
    expect(
        isinstance(final_situation, dict)
        and isinstance(returned_situation, dict)
        and isinstance(feedback_situation, dict)
        and returned_situation.get("observation_revision") == final_situation.get("observation_revision")
        and feedback_situation.get("observation_revision") == final_situation.get("observation_revision")
        and returned_situation.get("status") == "resolved"
        and feedback_situation.get("status") == "resolved",
        "resolved feedback returns the final current Situation revision instead of the old active snapshot",
    )
    expect(
        isinstance(final_situation, dict)
        and (final_situation.get("semantic") or {}).get("summary") == resolved_summary
        and int(final_situation.get("observation_revision") or 0) > latest_revision,
        "same-turn candidate semantic update survives feedback resolution",
    )

    independent_store = WorldStateStore(root / "feedback-independent")
    independent = build_living_context_composition(independent_store)
    independent_owner, independent_session = "feedback-independent-owner", "feedback-independent-session"
    first_fixture = dict(FIXTURES[2], id="feedback-independent-first", category="finance", source="other", subject="First independent context", unknown="first context")
    second_fixture = dict(FIXTURES[1], id="feedback-independent-second", category="work", source="other", subject="Second independent context", unknown="second context")
    independent_catalog = independent.orchestrator.model_catalog(owner_id=independent_owner, session_id=independent_session)
    first_candidate = candidate(first_fixture, independent_catalog).model_copy(update={"needs": []})
    independent.orchestrator.process_user_turn(
        event("feedback-independent-first-create", independent_owner, independent_session, str(first_fixture["text"])),
        SimpleNamespace(living_context_candidate=first_candidate, living_reaction_feedback=None),
        catalog=independent_catalog,
    )
    independent_catalog = independent.orchestrator.model_catalog(owner_id=independent_owner, session_id=independent_session)
    second_candidate = candidate(second_fixture, independent_catalog).model_copy(update={"needs": []})
    independent.orchestrator.process_user_turn(
        event("feedback-independent-second-create", independent_owner, independent_session, str(second_fixture["text"])),
        SimpleNamespace(living_context_candidate=second_candidate, living_reaction_feedback=None),
        catalog=independent_catalog,
    )
    independent_catalog = independent.orchestrator.model_catalog(owner_id=independent_owner, session_id=independent_session)
    first_row = next(row for row in independent_catalog if str(row.get("category")) == "finance")
    second_row = next(row for row in independent_catalog if str(row.get("category")) == "work")
    independent_feedback_text = f"{second_fixture['text']}；第一个情况已经解决"
    independent_feedback = LivingReactionFeedback(
        schema_version="veyra.living_reaction_feedback.v1",
        reaction_token=str((first_row.get("reaction") or {}).get("reaction_token") or ""),
        label="resolved",
        source_quote=ContextQuote(text=independent_feedback_text, start=0, end=len(independent_feedback_text)),
    )
    second_summary = "second candidate remains the returned Situation"
    second_update = candidate(second_fixture, independent_catalog).model_copy(
        update={"needs": [], "summary": second_summary}
    )
    independent_result = independent.orchestrator.process_user_turn(
        event("feedback-independent-combined", independent_owner, independent_session, independent_feedback_text),
        SimpleNamespace(
            living_context_candidate=second_update,
            living_reaction_feedback=independent_feedback,
            living_reaction_feedback_issues=[],
        ),
        catalog=independent_catalog,
    )
    independent_first = independent.core.get_situation(str(first_row["situation_token"]), owner_id=independent_owner, session_id=independent_session)
    independent_second = independent.core.get_situation(str(second_row["situation_token"]), owner_id=independent_owner, session_id=independent_session)
    independent_feedback_situation = (independent_result.get("feedback") or {}).get("situation")
    expect(
        isinstance(independent_result.get("situation"), dict)
        and isinstance(independent_second, dict)
        and independent_result["situation"].get("situation_id") == independent_second.get("situation_id")
        and (independent_result["situation"].get("semantic") or {}).get("summary") == second_summary,
        "same-turn candidate artifact is not overwritten by feedback for another Situation",
    )
    expect(
        isinstance(independent_first, dict)
        and independent_first.get("status") == "resolved"
        and isinstance(independent_feedback_situation, dict)
        and independent_feedback_situation.get("situation_id") == independent_first.get("situation_id")
        and independent_feedback_situation.get("observation_revision") == independent_first.get("observation_revision"),
        "independent resolved feedback retains its own final Situation artifact",
    )

    # Hold the reaction write after revision validation while a concurrent
    # candidate tries to advance the same Situation.  The candidate must wait
    # on the shared root writer fence; otherwise stale feedback could be
    # written after the Situation revision changed.
    fence_store = WorldStateStore(root / "feedback-fence")
    fence_composition = build_living_context_composition(fence_store)
    fence_owner, fence_session = "feedback-fence-owner", "feedback-fence-session"
    fence_fixture = dict(
        FIXTURES[2],
        id="feedback-fence",
        category="other",
        source="other",
        subject="Feedback fence context",
        unknown="fence context",
    )
    fence_catalog = fence_composition.orchestrator.model_catalog(owner_id=fence_owner, session_id=fence_session)
    fence_base = candidate(fence_fixture, fence_catalog).model_copy(update={"needs": []})
    fence_composition.orchestrator.process_user_turn(
        event("feedback-fence-create", fence_owner, fence_session, str(fence_fixture["text"])),
        SimpleNamespace(living_context_candidate=fence_base, living_reaction_feedback=None),
        catalog=fence_catalog,
    )
    fence_catalog = fence_composition.orchestrator.model_catalog(owner_id=fence_owner, session_id=fence_session)
    fence_row = next(row for row in fence_catalog if str(row.get("category")) == str(fence_fixture["category"]))
    fence_situation_id = str(fence_row.get("situation_token") or "")
    fence_revision = int(fence_row.get("observation_revision") or 0)
    fence_token = str((fence_row.get("reaction") or {}).get("reaction_token") or "")
    fence_text = "并发反馈仍绑定旧 revision"
    fence_feedback = LivingReactionFeedback(
        schema_version="veyra.living_reaction_feedback.v1",
        reaction_token=fence_token,
        label="useful",
        source_quote=ContextQuote(text=fence_text, start=0, end=len(fence_text)),
    )
    fence_candidate = candidate(fence_fixture, fence_catalog).model_copy(
        update={"needs": [], "summary": "concurrent candidate advances Situation"}
    )
    original_record_feedback = fence_composition.reaction.record_feedback
    feedback_entered = threading.Event()
    release_feedback = threading.Event()
    advance_started = threading.Event()
    advance_done = threading.Event()

    def blocked_record_feedback(payload):
        feedback_entered.set()
        if not release_feedback.wait(5):
            raise TimeoutError("feedback fence test did not release reaction writer")
        return original_record_feedback(payload)

    def advance_candidate():
        advance_started.set()
        try:
            return fence_composition.orchestrator.process_user_turn(
                event("feedback-fence-advance", fence_owner, fence_session, str(fence_fixture["text"])),
                SimpleNamespace(living_context_candidate=fence_candidate, living_reaction_feedback=None),
                catalog=fence_catalog,
            )
        finally:
            advance_done.set()

    fence_composition.reaction.record_feedback = blocked_record_feedback
    executor = ThreadPoolExecutor(max_workers=2)
    feedback_result = None
    advance_result = None
    try:
        feedback_future = executor.submit(
            fence_composition.orchestrator.process_user_turn,
            event("feedback-fence-feedback", fence_owner, fence_session, fence_text),
            SimpleNamespace(
                living_context_candidate=None,
                living_reaction_feedback=fence_feedback,
                living_reaction_feedback_issues=[],
            ),
            catalog=fence_catalog,
        )
        expect(feedback_entered.wait(5), "feedback revision check reaches reaction write under the root fence")
        advance_future = executor.submit(advance_candidate)
        expect(advance_started.wait(5), "concurrent candidate starts while feedback writer is held")
        expect(not advance_done.wait(0.2), "concurrent Situation candidate waits for the feedback root writer fence")
        release_feedback.set()
        feedback_result = feedback_future.result(timeout=5)
        advance_result = advance_future.result(timeout=5)
    finally:
        release_feedback.set()
        executor.shutdown(wait=True)
        fence_composition.reaction.record_feedback = original_record_feedback
    fence_current = fence_composition.core.get_situation(
        fence_situation_id,
        owner_id=fence_owner,
        session_id=fence_session,
    )
    expect((feedback_result or {}).get("feedback", {}).get("status") == "recorded", "feedback commits before the concurrent Situation advancement")
    expect(
        isinstance(fence_current, dict)
        and int(fence_current.get("observation_revision") or 0) > fence_revision
        and (advance_result or {}).get("status") == "recorded",
        "concurrent candidate advances only after the fenced feedback write",
    )


def admission_progress_and_reaction_integrity_checks(root: Path) -> None:
    """Inject one post-write failure at each cross-file admission seam."""

    # answer_need: Situation is durable before the Need transition raises;
    # the same event resumes from semantic_applied and resolves exactly once.
    answer_store = WorldStateStore(root / "admission-answer")
    answer_composition = build_living_context_composition(answer_store)
    owner, session = "admission-answer-owner", "admission-answer-session"
    fixture = dict(FIXTURES[0], id="admission-answer", source="other", category="work")
    catalog = answer_composition.orchestrator.model_catalog(owner_id=owner, session_id=session)
    created = answer_composition.orchestrator.process_user_turn(
        event("admission-answer-create", owner, session, str(fixture["text"])),
        SimpleNamespace(living_context_candidate=candidate(fixture, catalog), living_reaction_feedback=None),
        catalog=catalog,
    )
    need_id = str(created["information_needs"][0]["need_id"])
    answer_event = event("admission-answer-event", owner, session, "The missing detail is confirmed.")
    original_resolve = answer_composition.needs.resolve
    answer_failed = {"value": False}

    def answer_fail_once(*args, **kwargs):
        result = original_resolve(*args, **kwargs)
        if not answer_failed["value"]:
            answer_failed["value"] = True
            raise RuntimeError("answer Need failure after write")
        return result

    answer_composition.needs.resolve = answer_fail_once
    try:
        answer_composition.core.answer_need(answer_event, need_id)
    except RuntimeError:
        pass
    else:  # pragma: no cover
        raise AssertionError("answer fail-once did not raise")
    answer_entry = answer_store.read_json("situation_admission_ledger.json")["entries"][answer_event.event_id]
    expect(answer_entry["phase"] == "semantic_applied", "answer failure records semantic_applied phase")
    answer_composition.needs.resolve = original_resolve
    answer_retry = answer_composition.core.answer_need(answer_event, need_id)
    expect(
        answer_retry["need"]["status"] == "resolved"
        and answer_store.read_json("situation_admission_ledger.json")["entries"][answer_event.event_id]["phase"] == "committed",
        "answer retry resumes and commits the exact Need",
    )

    # Product command: a terminal resolve may have dismissed one Need before
    # the wrapper raises; retrying with the old expected revision is allowed
    # only because the admission row proves this exact event.
    command_store = WorldStateStore(root / "admission-command")
    command_composition = build_living_context_composition(command_store)
    owner, session = "admission-command-owner", "admission-command-session"
    fixture = dict(FIXTURES[0], id="admission-command", source="other", category="work")
    catalog = command_composition.orchestrator.model_catalog(owner_id=owner, session_id=session)
    created = command_composition.orchestrator.process_user_turn(
        event("admission-command-create", owner, session, str(fixture["text"])),
        SimpleNamespace(living_context_candidate=candidate(fixture, catalog), living_reaction_feedback=None),
        catalog=catalog,
    )
    situation_id = str(created["situation"]["situation_id"])
    expected_revision = int(created["situation"]["observation_revision"])
    command_event = event("admission-command-event", owner, session, "resolved")
    original_dismiss = command_composition.needs.dismiss
    command_failed = {"value": False}

    def command_fail_once(*args, **kwargs):
        result = original_dismiss(*args, **kwargs)
        if not command_failed["value"]:
            command_failed["value"] = True
            raise RuntimeError("command Need failure after write")
        return result

    command_composition.needs.dismiss = command_fail_once
    try:
        command_composition.core.command_situation(
            command_event,
            situation_id,
            owner_id=owner,
            session_id=session,
            command="resolve",
            expected_revision=expected_revision,
        )
    except RuntimeError:
        pass
    else:  # pragma: no cover
        raise AssertionError("command fail-once did not raise")
    command_entry = command_store.read_json("situation_admission_ledger.json")["entries"][command_event.event_id]
    expect(command_entry["phase"] == "semantic_applied", "Product command records semantic_applied phase")
    command_composition.needs.dismiss = original_dismiss
    command_retry = command_composition.core.command_situation(
        command_event,
        situation_id,
        owner_id=owner,
        session_id=session,
        command="resolve",
        expected_revision=expected_revision,
    )
    expect(
        command_retry["situation"]["status"] == "resolved"
        and command_store.read_json("situation_admission_ledger.json")["entries"][command_event.event_id]["phase"] == "committed",
        "Product command retry resumes after the stale expected revision",
    )

    # Source receipt: use a typed provider receipt, then fail after the Need
    # resolve.  The retry may use the server-owned raw receipt only for this
    # exact progressed admission; a caller-supplied look-alike is not used.
    source_store = WorldStateStore(root / "admission-source")
    source_clock = Clock()
    source_composition = build_living_context_composition(
        source_store,
        clock=source_clock,
        source_providers={"weather": StableProvider()},
    )
    owner, session = "admission-source-owner", "admission-source-session"
    source_composition.orchestrator.grant_source_consent(
        "weather", owner_id=owner, session_id=session, expected_generation=0
    )
    fixture = dict(FIXTURES[1], id="admission-source", source="weather", category="work")
    catalog = source_composition.orchestrator.model_catalog(owner_id=owner, session_id=session)
    created = source_composition.orchestrator.process_user_turn(
        event("admission-source-create", owner, session, str(fixture["text"])),
        SimpleNamespace(living_context_candidate=candidate(fixture, catalog), living_reaction_feedback=None),
        catalog=catalog,
    )
    source_situation = created["situation"]
    source_need = created["information_needs"][0]
    projection = source_composition.needs.authoritative_projection(
        str(source_need["need_id"]), owner_id=owner, session_id=session
    )
    binding = source_composition.policy.derive_binding(
        situation=source_situation,
        need={**source_need, **(projection or {})},
        now=source_clock(),
    )
    expect(binding is not None, "source admission test derives a typed binding")
    source_composition.source.register_binding(binding)
    request = source_composition.source.admit(
        binding.need_id,
        binding.source,
        user_id=owner,
        session_id=session,
        now=source_clock(),
        binding_id=binding.binding_id,
    )
    receipt = source_composition.source.execute(
        request.request_id,
        user_id=owner,
        session_id=session,
        now=source_clock(),
    )
    receipt_event = source_composition.orchestrator._event(
        owner,
        session,
        EventType.OBSERVATION,
        {
            "source_receipt_id": receipt.receipt_id,
            "need_id": binding.need_id,
            "source": binding.source,
            "status": receipt.status,
        },
        prefix="source_receipt",
    )
    original_resolve = source_composition.needs.resolve
    source_failed = {"value": False}

    def source_fail_once(*args, **kwargs):
        result = original_resolve(*args, **kwargs)
        if not source_failed["value"]:
            source_failed["value"] = True
            raise RuntimeError("source Need failure after write")
        return result

    source_composition.needs.resolve = source_fail_once
    try:
        source_composition.core.apply_source_receipt(
            receipt_event,
            receipt,
            expected_generation=int(binding.need_revision),
        )
    except RuntimeError:
        pass
    else:  # pragma: no cover
        raise AssertionError("source fail-once did not raise")
    source_entry = source_store.read_json("situation_admission_ledger.json")["entries"][receipt_event.event_id]
    expect(source_entry["phase"] == "semantic_applied", "source receipt records semantic_applied phase")
    source_composition.needs.resolve = original_resolve
    source_retry = source_composition.core.apply_source_receipt(
        receipt_event,
        receipt,
        expected_generation=int(binding.need_revision),
    )
    expect(
        source_retry["need"]["status"] == "resolved"
        and source_store.read_json("situation_admission_ledger.json")["entries"][receipt_event.event_id]["phase"] == "committed",
        "source receipt retry resumes from the exact typed receipt",
    )

    # A corrupt reaction ledger must be explicit in the model catalog, never
    # silently projected as if there were no current reaction.
    reaction_store = WorldStateStore(root / "reaction-catalog-corrupt")
    reaction_composition = build_living_context_composition(reaction_store)
    owner, session = "reaction-catalog-owner", "reaction-catalog-session"
    fixture = dict(FIXTURES[2], id="reaction-catalog", source="other", category="other")
    catalog = reaction_composition.orchestrator.model_catalog(owner_id=owner, session_id=session)
    reaction_composition.orchestrator.process_user_turn(
        event("reaction-catalog-create", owner, session, str(fixture["text"])),
        SimpleNamespace(living_context_candidate=candidate(fixture, catalog), living_reaction_feedback=None),
        catalog=catalog,
    )
    reaction_store.mutate_json(
        "living_reaction_state.json",
        lambda state: state["authority"].__setitem__("execution", True) or state,
    )
    corrupted_catalog = reaction_composition.orchestrator.model_catalog(owner_id=owner, session_id=session)
    expect(
        corrupted_catalog
        and all(
            row.get("reaction_status", {}).get("status") == "degraded"
            and "reaction" not in row
            for row in corrupted_catalog
        ),
        "corrupt reaction ledger is typed degraded rather than absence",
    )


def main() -> int:
    with TemporaryDirectory(prefix="veyra-v1-backend-blockers-") as temp:
        root = Path(temp)
        retry_and_consent_checks(root)
        retention_check(root)
        recurring_and_today_checks(root)
        observation_reopen_boundary_checks(root)
        answer_preflight_check(root)
        need_binding_checks(root)
        feedback_revision_checks(root)
        admission_progress_and_reaction_integrity_checks(root)
    print("V1_BACKEND_BLOCKERS_SMOKE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
