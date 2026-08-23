#!/usr/bin/env python3
"""End-to-end V1 Living Context acceptance over one generic orchestrator.

The three fixtures differ only in typed data.  The production path contains no
fixture lookup or natural-language keyword fallback.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.runtime_entity import RuntimeEntity  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402
from interface.event_schema import EventSource, EventType, VeyraEvent  # noqa: E402
from interface.living_context_contract import LivingContextCandidate, LivingReactionFeedback  # noqa: E402
from runtime.calendar_source import CalendarSource  # noqa: E402
from runtime.active_loop import ActiveRuntimeLoop  # noqa: E402
from runtime.living_context_composition import build_living_context_composition  # noqa: E402
from runtime.product_conversation_runtime import ProductConversationRuntime  # noqa: E402
from core.living_reaction_feedback_extractor import extract_living_reaction_feedback  # noqa: E402
from routers.product import build_product_router  # noqa: E402
from runtime.product_experience import ProductExperienceService  # noqa: E402


NOW = datetime(2026, 8, 17, 12, tzinfo=timezone.utc)


class Clock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: int) -> None:
        self.value += timedelta(**kwargs)


class Provider:
    def __init__(self, payload: dict[str, object], provider_id: str) -> None:
        self.payload = payload
        self.provider_id = provider_id
        self.calls = 0

    def read(self, context):
        self.calls += 1
        return dict(self.payload)


class CalendarFixture:
    provider_id = "calendar.fixture.v1"

    def read(self, context):
        return {
            "status": "ok",
            "events": [
                {
                    "event_id": "evt-a",
                    "title": "Scheduled block",
                    "starts_at": "2026-08-20T09:00:00Z",
                    "ends_at": "2026-08-20T10:00:00Z",
                    "location": "Shanghai",
                },
                {
                    "event_id": "evt-b",
                    "title": "Following block",
                    "starts_at": "2026-08-20T10:30:00Z",
                    "ends_at": "2026-08-20T11:30:00Z",
                    "location": "Hangzhou",
                },
            ],
        }


FIXTURES = (
    {
        "id": "a",
        "text": "A planned activity has a date window.",
        "subject": "Planned activity",
        "category": "travel",
        "goal": "arrive prepared",
        "source": "calendar",
        "unknown": "schedule fit",
        "question": "Which scheduled blocks overlap the window?",
        "entities": [],
    },
    {
        "id": "b",
        "text": "A preparation task is underway in Shanghai.",
        "subject": "Preparation task",
        "category": "work",
        "goal": "finish preparation",
        "source": "weather",
        "unknown": "conditions at the reported place",
        "question": "What are the current conditions there?",
        "entities": [{"kind": "place", "value": "Shanghai", "epistemic_status": "reported"}],
    },
    {
        "id": "c",
        "text": "A change is being coordinated.",
        "subject": "Coordination change",
        "category": "logistics",
        "goal": "keep the plan aligned",
        "source": "public_web",
        "unknown": "current public context",
        "question": "What public information could change the plan?",
        "entities": [],
    },
)


def event(event_id: str, owner: str, session: str, text: str) -> VeyraEvent:
    return VeyraEvent(
        type=EventType.USER_MESSAGE,
        source=EventSource(channel="api", user_id=owner, session_id=session),
        payload={"text": text},
        event_id=event_id,
    )


def candidate(fixture: dict[str, object], catalog: list[dict[str, object]], *, deadline: str | None = None) -> LivingContextCandidate:
    match = next((row for row in catalog if row.get("category") == fixture["category"]), None)
    entities: list[dict[str, object]] = []
    text = str(fixture["text"])
    for raw_entity in fixture["entities"]:
        entity = dict(raw_entity)
        value = str(entity.get("value") or "")
        start = text.find(value)
        if start >= 0:
            entity["source_quote"] = {"text": value, "start": start, "end": start + len(value)}
        entities.append(entity)
    data: dict[str, object] = {
        "schema_version": "veyra.living_context_candidate.v1",
        "disposition": "update" if match else "create",
        "situation_token": match.get("situation_token") if match else None,
        "situation_revision": match.get("observation_revision") if match else None,
        "catalog_token": match.get("catalog_token") if match else None,
        "create_subject": fixture["subject"] if not match else "",
        "category": fixture["category"],
        "label": fixture["subject"],
        "title": fixture["subject"],
        "summary": fixture["text"],
        "goal": fixture["goal"],
        "deadline_at": deadline,
        "progress": {"status": "in_progress", "value": 0.25},
        "entities": entities,
        "lifecycle": "active",
        "known": [{"statement": fixture["text"], "epistemic_status": "reported"}],
        "unknown": [fixture["unknown"]],
        "assumptions": [],
        "timeline": [{"statement": fixture["text"], "occurred_at": NOW.isoformat(), "material": True}],
        "material_change": "A material update was recorded." if match else "",
        "next_step": "evaluate the next observation",
        "next_step_epistemic_status": "inferred",
        "needs": [{
            "blocked_judgment": fixture["unknown"],
            "evidence_kind": fixture["source"],
            "why_now": "The missing observation changes the next judgment.",
            "urgency": 0.8,
            "observation_requirement": (
                {
                    "coverage": "current",
                    "metrics": ["weather_description", "temperature_2m"],
                }
                if fixture["source"] == "weather"
                else {
                    "coverage": "window" if fixture["source"] == "calendar" else "results",
                    "metrics": ["events"] if fixture["source"] == "calendar" else ["results"],
                }
                if fixture["source"] == "calendar" or fixture["source"] == "public_web"
                else None
            ),
            "allowed_source_classes": [fixture["source"]],
            "fallback_reaction": "read",
            "question": fixture["question"],
        }],
        "answered_need_tokens": [],
        "answered_need_bindings": [],
        "requested_reaction": "read",
        "reopen": False,
        "reopen_reason": "",
        "source": "model",
    }
    return LivingContextCandidate.model_validate(data, strict=True)


def feedback_candidate(token: str, label: str, text: str, *, remind_before_seconds: int | None = None) -> LivingReactionFeedback:
    return LivingReactionFeedback.model_validate({
        "schema_version": "veyra.living_reaction_feedback.v1",
        "reaction_token": token,
        "label": label,
        "remind_before_seconds": remind_before_seconds,
        "source_quote": {"text": text, "start": 0, "end": len(text)},
    }, strict=True)


def expect(condition: bool, label: str) -> None:
    if not condition:
        raise AssertionError(label)
    print(f"PASS {label}")


def main() -> int:
    with TemporaryDirectory(prefix="veyra-v1-acceptance-") as temp:
        clock = Clock()
        store = WorldStateStore(Path(temp) / "state")
        weather = Provider({"status": "ok", "details": {"location": "Shanghai", "current": {"temperature_2m": 22, "weather_description": "clear"}}}, "weather.fixture.v1")
        web = Provider({"status": "ok", "details": {"results": [{"title": "Public result", "url": "https://example.test/item", "snippet": "bounded public snippet"}]}}, "web.fixture.v1")
        conversation_runtime = ProductConversationRuntime(store)
        composition = build_living_context_composition(
            store,
            clock=clock,
            source_providers={"weather": weather, "public_web": web},
            calendar_source=CalendarSource(CalendarFixture()),
            conversation_runtime=conversation_runtime,
        )
        orchestrator = composition.orchestrator
        owner, session = "accept-owner", "accept-session"
        for source in ("calendar", "weather", "public_web"):
            orchestrator.grant_source_consent(source, owner_id=owner, session_id=session, expected_generation=0)

        ids: list[str] = []
        for index, fixture in enumerate(FIXTURES, start=1):
            catalog = orchestrator.model_catalog(owner_id=owner, session_id=session)
            result = orchestrator.process_user_turn(event(f"create-{index}", owner, session, str(fixture["text"])), type("U", (), {"living_context_candidate": candidate(fixture, catalog), "living_reaction_feedback": None})(), catalog=catalog)
            expect(result.get("status") == "recorded", f"fixture {index} admitted")
            situation = result.get("situation")
            expect(isinstance(situation, dict), f"fixture {index} has Situation")
            ids.append(str(situation["situation_id"]))
            expect(isinstance(result.get("reaction"), dict), f"fixture {index} has one reaction evaluation")
        ask_fixture = dict(
            FIXTURES[0],
            id="ask",
            category="personal",
            source="user",
            subject="Personal confirmation",
            unknown="a user confirmation",
            question="Can you confirm the next step?",
        )
        ask_catalog = orchestrator.model_catalog(owner_id=owner, session_id=session)
        ask_result = orchestrator.process_user_turn(
            event("create-ask", owner, session, "A personal plan needs confirmation."),
            type("U", (), {"living_context_candidate": candidate(ask_fixture, ask_catalog), "living_reaction_feedback": None})(),
            catalog=ask_catalog,
        )
        expect(ask_result["reaction"]["decision"]["disposition"] == "ask", "ask disposition is reachable")
        tick = orchestrator.tick(owner_id=owner, session_id=session, limit=20)
        expect(tick["evaluated_count"] >= 3, "one bounded tick scans all three Situations")
        calendar_item = next(item for item in tick["items"] if item.get("situation_id") == ids[0])
        calendar_source = calendar_item.get("source") if isinstance(calendar_item.get("source"), dict) else {}
        calendar_applied = calendar_source.get("applied") if isinstance(calendar_source.get("applied"), dict) else {}
        calendar_semantic = (calendar_applied.get("situation") or {}).get("semantic") if isinstance(calendar_applied.get("situation"), dict) else {}
        calendar_reevaluated = calendar_item.get("reevaluated") if isinstance(calendar_item.get("reevaluated"), dict) else {}
        calendar_decision = calendar_reevaluated.get("decision") if isinstance(calendar_reevaluated.get("decision"), dict) else {}
        expect(
            calendar_source.get("status") == "ok"
            and calendar_applied.get("attention_trigger") == "material_observation"
            and str(calendar_semantic.get("material_change") or "").startswith("Calendar "),
            "Calendar conflict creates a concrete server-owned material observation",
        )
        expect(
            calendar_decision.get("disposition") == "suggest"
            and calendar_decision.get("reason") == "actionable_material_observation",
            "Calendar conflict becomes one proactive suggestion after the read",
        )
        calendar_conversation = calendar_item.get("conversation") if isinstance(calendar_item.get("conversation"), dict) else {}
        bound_conversation = conversation_runtime.create_conversation(
            owner_id=owner,
            session_id=session,
            binding_type="situation",
            binding_id=ids[0],
        )
        expect(
            calendar_conversation.get("status") == "recorded"
            and len(bound_conversation.get("messages") or []) == 1
            and (bound_conversation.get("messages") or [])[0].get("kind") == "proactive",
            "background material suggestion enters the bound Product Conversation once",
        )
        calendar_catalog = orchestrator.model_catalog(owner_id=owner, session_id=session)
        calendar_row = next(
            row for row in calendar_catalog
            if str(row.get("situation_token") or "") == ids[0]
        )
        calendar_reaction = calendar_row.get("reaction") if isinstance(calendar_row.get("reaction"), dict) else {}
        expect(
            str(calendar_reaction.get("reaction_token") or "").startswith("rxn_")
            and int(calendar_reaction.get("situation_revision") or 0) == int(
                (calendar_applied.get("situation") or {}).get("observation_revision") or 0
            )
            and calendar_reaction.get("disposition") == "suggest",
            "Calendar reevaluation exposes the exact current reaction token in the model catalog",
        )

        class FeedbackClient:
            def __init__(self, reaction_token: str) -> None:
                self.reaction_token = reaction_token

            def complete_json(self, *, purpose: str, system: str, user: str):
                expect(purpose == "living_reaction_feedback_extraction", "feedback extractor uses its bounded purpose")
                return {
                    "living_reaction_feedback": {
                        "schema_version": "veyra.living_reaction_feedback.v1",
                        "reaction_token": self.reaction_token,
                        "label": "ignore",
                        "source_quote": {"text": "这个日历提醒不用管", "start": 0, "end": 9},
                    }
                }

        feedback_text = "这个日历提醒不用管"
        extracted = extract_living_reaction_feedback(
            FeedbackClient(str(calendar_reaction["reaction_token"])),
            text=feedback_text,
            catalog=calendar_catalog,
        )
        expect(
            extracted.feedback is not None
            and extracted.metrics.get("extractor_attempted") is True
            and int(extracted.metrics.get("row_count") or 0) >= 1,
            "feedback extractor can attempt against the current Calendar reaction row",
        )
        calendar_feedback_result = orchestrator.process_user_turn(
            event("calendar-feedback-ignore", owner, session, feedback_text),
            type("U", (), {
                "living_context_candidate": None,
                "living_reaction_feedback": extracted.feedback,
                "living_reaction_feedback_issues": [],
            })(),
            catalog=calendar_catalog,
        )
        expect(
            (calendar_feedback_result.get("feedback") or {}).get("status") == "recorded",
            "Calendar current reaction feedback is recorded",
        )
        restarted_calendar = build_living_context_composition(
            store,
            clock=clock,
            source_providers={"weather": weather, "public_web": web},
            calendar_source=CalendarSource(CalendarFixture()),
        )
        ignore_tick = restarted_calendar.orchestrator.tick(owner_id=owner, session_id=session, limit=20)
        ignore_item = next(item for item in ignore_tick["items"] if item.get("situation_id") == ids[0])
        ignore_decision = ((ignore_item.get("reaction") or {}).get("decision") or {})
        expect(
            ignore_decision.get("disposition") == "silent"
            and (ignore_decision.get("suppression") or {}).get("reason") == "ignore",
            "Calendar ignore feedback suppresses the current reaction after restart",
        )
        rows = {str(item["situation_id"]): item for item in composition.core.list_situations(owner_id=owner, session_id=session)}
        for situation_id, fixture in zip(ids, FIXTURES):
            semantic = rows[situation_id]["semantic"]
            expect(isinstance(semantic.get("evidence"), list), "source receipt evidence is durable")
            observations = rows[situation_id].get("observations", [])
            expect(
                any(
                    isinstance(item, dict)
                    and item.get("source") == f"source:{fixture['source']}"
                    and item.get("epistemic_status") == "inferred"
                    for item in observations
                ),
                "source receipt observation provenance is honest",
            )
        expect(weather.calls == 1 and web.calls == 1, "typed weather and web providers each run once")
        expect(any(item.get("status") == "asked" for item in composition.core.needs.list(owner_id=owner, session_id=session, limit=20)), "ask disposition marks the Need asked")

        # A consented source is only a usable read target when the server can
        # also derive a bounded request for that exact Need.  Reporting a read
        # for an underivable request would strand the Need: the read never runs
        # and the user is never asked.
        weather_need = {
            "allowed_source_classes": ["weather"],
            "evidence_kind": "weather",
            "blocked_judgment": "conditions at the reported place",
            "observation_requirement": {
                "coverage": "current",
                "metrics": ["weather_description", "temperature_2m"],
            },
            "evidence_target": {
                "location": "Shanghai",
                "observation_requirement": {
                    "coverage": "current",
                    "metrics": ["weather_description", "temperature_2m"],
                },
            },
        }
        placed = {"semantic": {"entities": [{"kind": "place", "value": "Shanghai", "epistemic_status": "reported", "provenance_scope": "span", "source_quote": {"text": "Shanghai", "start": 0, "end": 8}}]}}
        unplaced = {"semantic": {"entities": []}}
        inferred_place = {"semantic": {"entities": [{"kind": "place", "value": "Shanghai", "epistemic_status": "inferred"}]}}
        weather_need_without_target = {
            key: value for key, value in weather_need.items() if key != "evidence_target"
        }
        expect(
            orchestrator.source_policy.can_resolve_parameters("weather", situation=placed, need=weather_need, now=clock()),
            "a reported place resolves a bounded weather request",
        )
        expect(
            not orchestrator.source_policy.can_resolve_parameters("weather", situation=unplaced, need=weather_need_without_target, now=clock()),
            "no stated place leaves the weather request underivable",
        )
        expect(
            not orchestrator.source_policy.can_resolve_parameters("weather", situation=inferred_place, need=weather_need_without_target, now=clock()),
            "an inferred place is not a weather source target",
        )

        reaction_count_before_replay = composition.reaction.status().get("reaction_count")
        replay_tick = orchestrator.tick(owner_id=owner, session_id=session, limit=20)
        expect(replay_tick["error_count"] == 0, "replayed bounded tick stays healthy")
        expect(composition.reaction.status().get("reaction_count") == reaction_count_before_replay, "replayed bounded tick does not grow the reaction ledger")

        # An unavailable or truthful empty read updates Need lifecycle without
        # manufacturing a proactive suggestion from the failure/absence.
        failure_fixture = dict(FIXTURES[2], id="failure", category="health", subject="Unavailable public context", unknown="offline context")
        failure_catalog = orchestrator.model_catalog(owner_id=owner, session_id=session)
        failure_result = orchestrator.process_user_turn(
            event("create-failure", owner, session, "A public context read may be unavailable."),
            type("U", (), {"living_context_candidate": candidate(failure_fixture, failure_catalog), "living_reaction_feedback": None})(),
            catalog=failure_catalog,
        )
        web.payload = {"status": "unavailable", "reason": "fixture_offline"}
        failure_tick = orchestrator.tick(owner_id=owner, session_id=session, limit=20)
        failure_item = next(item for item in failure_tick["items"] if item.get("situation_id") == failure_result["situation"]["situation_id"])
        failure_disposition = ((failure_item.get("reevaluated") or {}).get("decision") or {}).get("disposition")
        failure_semantic = ((failure_item.get("source", {}).get("applied") or {}).get("situation") or {}).get("semantic") or {}
        expect(failure_item.get("source", {}).get("status") == "unavailable" and not str(failure_semantic.get("material_change") or "").strip() and failure_disposition != "suggest", "failed source read remains non-suggesting")

        empty_fixture = dict(FIXTURES[2], id="empty", category="other", subject="Empty public context", unknown="whether public context exists")
        empty_catalog = orchestrator.model_catalog(owner_id=owner, session_id=session)
        empty_result = orchestrator.process_user_turn(
            event("create-empty", owner, session, "There may be no public context to read."),
            type("U", (), {"living_context_candidate": candidate(empty_fixture, empty_catalog), "living_reaction_feedback": None})(),
            catalog=empty_catalog,
        )
        web.payload = {"status": "empty", "details": {"results": []}}
        empty_tick = orchestrator.tick(owner_id=owner, session_id=session, limit=20)
        empty_item = next(item for item in empty_tick["items"] if item.get("situation_id") == empty_result["situation"]["situation_id"])
        empty_need = composition.core.needs.list(owner_id=owner, session_id=session, situation_id=empty_result["situation"]["situation_id"], limit=8)
        empty_disposition = ((empty_item.get("reevaluated") or {}).get("decision") or {}).get("disposition")
        empty_semantic = ((empty_item.get("source", {}).get("applied") or {}).get("situation") or {}).get("semantic") or {}
        expect(empty_item.get("source", {}).get("status") == "empty" and not str(empty_semantic.get("material_change") or "").strip() and any(item.get("status") == "resolved" for item in empty_need), "empty source result resolves its InformationNeed without material change")
        expect(empty_disposition != "suggest", "empty source result does not manufacture a suggestion")

        # A wait and a deadline-driven suggest use the same reaction mechanism.
        wait_fixture = dict(FIXTURES[0], id="wait", category="education", source="other", subject="Later signal", unknown="later signal", question="What changes later?")
        wait_candidate = candidate(wait_fixture, orchestrator.model_catalog(owner_id=owner, session_id=session))
        wait_candidate = wait_candidate.model_copy(update={"needs": [wait_candidate.needs[0].model_copy(update={"allowed_source_classes": [], "evidence_kind": "other", "fallback_reaction": "wait"})]})
        wait_result = orchestrator.process_user_turn(event("create-wait", owner, session, "A later signal is being monitored."), type("U", (), {"living_context_candidate": wait_candidate, "living_reaction_feedback": None})(), catalog=orchestrator.model_catalog(owner_id=owner, session_id=session))
        expect(wait_result["reaction"]["decision"]["disposition"] == "wait", "wait disposition is reachable")

        deadline = (clock() + timedelta(hours=2)).isoformat()
        suggest_fixture = dict(FIXTURES[2], id="suggest", category="finance", source="other", subject="Deadline coverage", unknown="deadline coverage", question="What remains before the deadline?")
        suggest_candidate = candidate(suggest_fixture, orchestrator.model_catalog(owner_id=owner, session_id=session), deadline=deadline)
        suggest_candidate = suggest_candidate.model_copy(update={"needs": []})
        suggest_result = orchestrator.process_user_turn(event("create-suggest", owner, session, "A deadline is close."), type("U", (), {"living_context_candidate": suggest_candidate, "living_reaction_feedback": None})(), catalog=orchestrator.model_catalog(owner_id=owner, session_id=session))
        expect(suggest_result["reaction"]["decision"]["disposition"] == "suggest", "deadline suggest disposition is reachable")

        # Natural-language feedback is a typed model seam, not a keyword path.
        suggestion = suggest_result["reaction"]["decision"]
        feedback_text = "这个不用管"
        feedback = feedback_candidate(suggestion["reaction_token"], "ignore", feedback_text)
        feedback_result = orchestrator.process_user_turn(event("feedback-ignore", owner, session, feedback_text), type("U", (), {"living_context_candidate": None, "living_reaction_feedback": feedback, "living_reaction_feedback_issues": []})(), catalog=orchestrator.model_catalog(owner_id=owner, session_id=session))
        expect(feedback_result["feedback"]["status"] == "recorded", "ignore feedback changes reaction ledger")
        other_catalog = orchestrator.model_catalog(owner_id="other-owner", session_id=session)
        expect(not other_catalog, "cross-owner catalog is isolated")

        restarted = build_living_context_composition(store, clock=clock, source_providers={"weather": weather, "public_web": web}, calendar_source=CalendarSource(CalendarFixture()))
        expect(len(restarted.core.list_situations(owner_id=owner, session_id=session)) >= 5, "restart restores the shared Situation truth")

        # Product source control stays on the same orchestrator seam and uses
        # exact consent generations for revoke/regrant/CAS.
        product = ProductExperienceService(store, living_context_orchestrator=restarted.orchestrator)
        app = FastAPI()
        app.include_router(build_product_router(service=product))
        client = TestClient(app)
        web_detail = client.get(f"/product/situations/{ids[2]}?user_id={owner}&session_id={session}")
        web_detail_value = web_detail.json()
        web_evidence = web_detail_value.get("situation", {}).get("evidence", []) if isinstance(web_detail_value.get("situation"), dict) else []
        expect(web_detail.status_code == 200 and any(item.get("source") == "public_web" and item.get("title") for item in web_evidence), "Product Situation detail exposes bounded source evidence")
        expect("url" not in str(web_detail_value).lower(), "Product Situation evidence does not expose provider URLs")
        weather_consent = restarted.orchestrator.source_status(owner_id=owner, session_id=session)["consent"]["weather"]
        consent_id = str(weather_consent.get("consent_id") or "")
        revoke = client.request(
            "DELETE",
            f"/product/sources/weather/consent?user_id={owner}&session_id={session}",
            json={"expected_generation": 1, "consent_id": consent_id},
        )
        expect(revoke.status_code == 200 and revoke.json().get("status") == "revoked", "Product source revoke uses the current generation")
        regrant = client.post(
            f"/product/sources/weather/consent?user_id={owner}&session_id={session}",
            json={"expected_generation": 1, "consent_id": consent_id},
        )
        expect(regrant.status_code == 200 and regrant.json()["consent"]["generation"] == 2, "Product source regrant advances consent generation")
        stale_grant = client.post(
            f"/product/sources/weather/consent?user_id={owner}&session_id={session}",
            json={"expected_generation": 1, "consent_id": consent_id},
        )
        expect(stale_grant.status_code == 409, "stale Product source consent fails closed")

        class NoOp:
            def __getattr__(self, name):
                return lambda *args, **kwargs: {"status": "not_configured"}

        active = ActiveRuntimeLoop(
            state_store=store,
            runtime_entity=RuntimeEntity(store),
            proactive_checks=NoOp(), state_refresh=NoOp(), external_world_refresh=NoOp(), runtime_matrix=NoOp(), retention_policy=NoOp(), task_tracker=NoOp(), adapter_resolver=lambda: NoOp(), verifier=NoOp(), living_context=orchestrator,
        )
        active_result = active._living_context_tick()
        expect(active_result.get("evaluated_count", 0) <= 20, "ActiveLoop living_context callback is bounded")
        print("V1_LIVING_CONTEXT_ACCEPTANCE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
