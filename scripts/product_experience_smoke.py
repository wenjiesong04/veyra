#!/usr/bin/env python3
"""V1 Product backend contract, scope, mutation, and purity smoke."""

from __future__ import annotations

import json
import hashlib
import sys
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.world_state import WorldStateStore  # noqa: E402
from interface.event_schema import EventSource, EventType, VeyraEvent  # noqa: E402
from routers.product import build_product_router  # noqa: E402
from runtime.living_context_runtime import LivingContextRuntime  # noqa: E402
from runtime.product_experience import ProductExperienceService  # noqa: E402


SENSITIVE_VALUES = {"/private/veyra/workspace-secret", "control-token-should-never-leak", "state/path/should-stay-private.json"}


class Understanding:
    living_context_candidate: dict[str, Any]


def expect(condition: bool, label: str) -> None:
    if not condition:
        raise AssertionError(f"{label} failed")
    print(f"PASS {label}")


def event(owner: str, session: str, number: int, text: str) -> VeyraEvent:
    return VeyraEvent(
        type=EventType.USER_MESSAGE,
        source=EventSource(channel="api", user_id=owner, session_id=session),
        payload={"text": text},
        event_id=f"product-smoke-event-{number}",
        timestamp=datetime(2026, 8, 17, 12, number, tzinfo=timezone.utc).isoformat(),
    )


def candidate(number: int) -> Understanding:
    value = Understanding()
    value.living_context_candidate = {
        "schema_version": "veyra.living_context_candidate.v1",
        "disposition": "create",
        "create_subject": f"subject-{number}",
        "category": "personal" if number == 1 else "work",
        "title": f"Situation {number}",
        "label": f"Focus {number}",
        "summary": f"A generic long-lived Situation {number}.",
        "goal": "Keep the next meaningful step clear.",
        "lifecycle": "active",
        "progress": {"status": "in_progress", "value": 0.25},
        "known": [{"statement": f"The user reported Situation {number}."}],
        "unknown": [f"The next fact for Situation {number} is not known."],
        "assumptions": [{"statement": "The next observation may change the plan."}],
        "timeline": [],
        "material_change": f"Situation {number} entered the user's active context.",
        "next_step": "Confirm the highest-impact next step.",
        "needs": [{
            "blocked_judgment": f"Missing fact {number}",
            "evidence_kind": "user",
            "why_now": "This is the next useful piece of context.",
            "urgency": 0.7,
            "allowed_source_classes": ["user"],
            "fallback_reaction": "ask",
            "question": f"What is the missing fact for Situation {number}?",
        }],
    }
    return value


def bytes_snapshot(store: WorldStateStore) -> dict[str, bytes]:
    return {
        str(path.relative_to(store.root)): path.read_bytes()
        for path in store.root.rglob("*")
        if path.is_file() and path.name != ".veyra-writer.lock"
    }


def assert_safe(value: Any) -> None:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
    leaked = [item for item in SENSITIVE_VALUES if item in encoded]
    if leaked:
        raise AssertionError(f"private value leaked: {leaked}")


def assert_authority(value: dict[str, Any]) -> None:
    authority = value.get("authority")
    expect(isinstance(authority, dict) and all(item is False for item in authority.values()), "authority remains disabled")


def need_row(
    *,
    need_id: str,
    situation_id: str,
    owner: str = "owner-a",
    session: str = "session-a",
    status: str = "open",
    created_at: str = "2026-08-17T12:00:00+00:00",
    binding: str | None = None,
) -> dict[str, Any]:
    row = {
        "need_id": need_id,
        "situation_id": situation_id,
        "owner_id": owner,
        "session_id": session,
        "status": status,
        "created_at": created_at,
        "question": need_id,
    }
    if binding is not None:
        row["unknown_binding"] = binding
        row["unknown_binding_digest"] = hashlib.sha256(binding.encode("utf-8")).hexdigest()
    return row


def main() -> int:
    deduped = ProductExperienceService._dedupe_active_needs(
        [
            need_row(need_id="new-open", situation_id="s1", status="open", created_at="2026-01-02T00:00:00+00:00", binding="shared endpoint"),
            need_row(need_id="old-asked", situation_id="s1", status="asked", created_at="2026-01-01T00:00:00+00:00", binding="shared endpoint"),
            need_row(need_id="other-situation", situation_id="s2", status="open", binding="shared endpoint"),
            need_row(need_id="unbound-a", situation_id="s1"),
            need_row(need_id="unbound-b", situation_id="s1"),
        ],
        owner="owner-a",
        session="session-a",
    )
    expect(
        [row["need_id"] for row in deduped]
        == ["old-asked", "other-situation", "unbound-a", "unbound-b"],
        "Product questions dedupe only one exact bound endpoint and prefer asked state",
    )
    with TemporaryDirectory(prefix="veyra-product-v1-") as temporary:
        store = WorldStateStore(Path(temporary) / "state")
        living = LivingContextRuntime(store)
        owner, session = "owner-a", "session-a"
        service = ProductExperienceService(store, living_context_runtime=living)
        outputs: list[dict[str, Any]] = []
        for number in range(1, 4):
            current_event = event(owner, session, number, f"Situation {number} is real.")
            outputs.append(living.process_user_turn(current_event, candidate(number)))
        ids = [str(item["situation"]["situation_id"]) for item in outputs]
        need_id = str(outputs[0]["information_needs"][0]["need_id"])

        app = FastAPI()
        app.include_router(build_product_router(service=service))
        client = TestClient(app)
        routes = {route.path for route in build_product_router(service=service).routes}
        expect({"/product/context", "/product/today", "/product/matters", "/product/status"}.issubset(routes), "legacy product routes remain")
        expect({"/product/situations", "/product/situations/{situation_id}", "/product/questions", "/product/reactions", "/product/sources"}.issubset(routes), "V1 product routes are registered")

        today = client.get(f"/product/today?user_id={owner}&session_id={session}&first_meeting=true")
        expect(today.status_code == 200 and today.json()["schema_version"] == "veyra.product_today.v1", "Today contract is served")
        today_value = today.json()
        expect(len(today_value["situations"]) == 3 and today_value["first_meeting"] is False, "Today leads with three semantic Situations")
        expect(len(today_value["questions"]["items"]) == 3 and today_value["focus"] is None, "questions work without a Workspace Goal")
        assert_authority(today_value)
        assert_safe(today_value)

        listed = client.get(f"/product/situations?user_id={owner}&session_id={session}").json()
        expect(listed["count"] == 3 and all("situation_id" in item for item in listed["items"]), "Situation list uses semantic truth")
        detail = client.get(f"/product/situations/{ids[0]}?user_id={owner}&session_id={session}")
        expect(detail.status_code == 200, "Situation detail is available")
        detail_value = detail.json()["situation"]
        expect({"title", "goal", "status", "progress", "deadline_at", "known", "unknown", "assumptions", "timeline", "evidence_refs", "next_observation_at", "next_step", "material_change"}.issubset(detail_value), "Situation detail exposes the V1 fields")
        assert_safe(detail.json())
        expect(client.get(f"/product/situations?user_id=owner-b&session_id=session-b").json()["items"] == [], "Situation list is owner/session isolated")

        questions = client.get(f"/product/questions?user_id={owner}&session_id={session}").json()
        expect(any(item["need_id"] == need_id for item in questions["items"]), "InformationNeed is projected as a question")
        answer = client.post(f"/product/questions/{need_id}/answer?user_id={owner}&session_id={session}", json={"answer": "The user supplied the missing fact.", "expected_generation": 1, "expected_revision": 1})
        expect(answer.status_code == 200 and answer.json()["status"] == "answered", "answer updates Situation through LivingContextRuntime")
        after_answer = client.get(f"/product/situations/{ids[0]}?user_id={owner}&session_id={session}").json()
        expect(after_answer["situation"]["revision"] == 2 and after_answer["questions"] == [], "answer changes understanding and resolves the need")

        command = client.post(f"/product/situations/{ids[0]}/command?user_id={owner}&session_id={session}", json={"command": "correct", "expected_revision": 2, "patch": {"next_step": "Take the next small step."}})
        expect(command.status_code == 200 and command.json()["situation"]["revision"] == 3, "typed Situation correction uses revision CAS")
        wrong_revision = client.post(f"/product/situations/{ids[0]}/command?user_id={owner}&session_id={session}", json={"command": "correct", "expected_revision": 2, "patch": {}})
        expect(wrong_revision.status_code == 409, "stale Situation command fails closed")
        expect(client.get(f"/product/situations/{ids[0]}?user_id=owner-b&session_id=session-b").status_code == 404, "wrong Situation scope is not observable")
        resolved = client.post(f"/product/situations/{ids[2]}/command?user_id={owner}&session_id={session}", json={"command": "resolve", "expected_revision": 1, "patch": {}})
        expect(resolved.status_code == 200 and resolved.json()["situation"]["status"] == "resolved", "resolve command closes a Situation")
        reopened = client.post(f"/product/situations/{ids[2]}/command?user_id={owner}&session_id={session}", json={"command": "reopen", "expected_revision": 2, "reason": "the user has more to do", "patch": {}})
        expect(reopened.status_code == 200 and reopened.json()["situation"]["status"] == "active", "reopen command requires and restores a terminal Situation")
        quieted = client.post(f"/product/situations/{ids[2]}/command?user_id={owner}&session_id={session}", json={"command": "quiet", "expected_revision": 3, "patch": {}})
        expect(quieted.status_code == 200 and quieted.json()["situation"]["status"] == "waiting", "quiet command enters a waiting phase")

        # A generic reaction is persisted through the reaction runtime, then
        # projected as a user-facing explanation and feedback target.
        current = service.situation_detail(ids[1], user_id=owner, session_id=session)["situation"]
        reaction = service.reaction_runtime.evaluate({
            "owner_id": owner,
            "session_id": session,
            "now": datetime(2026, 8, 17, 13, tzinfo=timezone.utc).isoformat(),
            "quiet_hours": False,
            "consent": {"user": True},
            "source_availability": {"user": True},
            "situation": {**current, "owner_id": owner, "session_id": session, "revision": current["revision"], "progress": 0.25, "material_change": {"statement": current["material_change"] or "A material signal was recorded.", "revision": 1}},
            "information_need": {"need_id": "need-reaction", "revision": 1, "status": "open", "kind": "clarification", "question": "What changed?", "source": "user", "priority": "high"},
        })
        reaction_id = reaction["decision"]["reaction_id"]
        reactions = client.get(f"/product/reactions?user_id={owner}&session_id={session}").json()
        expect(any(item["reaction_id"] == reaction_id and item["what_changed"] and item["why_relevant"] and item["why_now"] and item["recommendation"] for item in reactions["items"]), "reaction exposes what/why/why-now/recommendation")
        feedback = client.post(f"/product/reactions/{reaction_id}/feedback?user_id={owner}&session_id={session}", json={"label": "useful", "situation_revision": current["revision"]})
        expect(feedback.status_code == 200 and feedback.json()["status"] == "recorded", "typed reaction feedback is accepted")
        stale_feedback = client.post(f"/product/reactions/{reaction_id}/feedback?user_id={owner}&session_id={session}", json={"label": "ignore", "situation_revision": current["revision"] + 1})
        expect(stale_feedback.status_code == 409, "feedback revision mismatch fails closed")
        suggestion_situation = service.situation_detail(ids[0], user_id=owner, session_id=session)["situation"]
        suggested = service.reaction_runtime.evaluate({
            "owner_id": owner,
            "session_id": session,
            "now": datetime(2026, 8, 17, 13, 1, tzinfo=timezone.utc).isoformat(),
            "quiet_hours": False,
            "consent": {"user": True},
            "source_availability": {"user": True},
            "situation": {**suggestion_situation, "owner_id": owner, "session_id": session, "revision": suggestion_situation["revision"], "progress": 0.25, "material_change": {"statement": "A second material signal was recorded.", "revision": 2}},
            "information_need": None,
        })
        suggestions = client.get(f"/product/suggestions?user_id={owner}&session_id={session}")
        expect(suggestions.status_code == 200 and suggestions.json()["items"] and all(item["disposition"] == "suggest" for item in suggestions.json()["items"]), "suggestions expose only suggest dispositions")
        today_after_reaction = client.get(f"/product/today?user_id={owner}&session_id={session}").json()
        expect(all(item["disposition"] == "suggest" for item in today_after_reaction["suggestions"]), "Today suggestions exclude ask/read/wait")
        expect(all(item["disposition"] == "wait" for item in today_after_reaction["waiting"]), "Today waiting contains only wait dispositions")
        boundary = today_after_reaction["suggestions_boundary"]
        expect(
            boundary["projected_ledger"] == "living_reaction.suggest"
            and boundary["other_ledger"] == "general_suggestion_outbox"
            and boundary["other_ledger_projected"] is False
            and boundary["other_ledger_recorded_count"] == 0,
            f"Today names the other suggestion ledger without projecting it: {boundary}",
        )
        store.mutate_json(
            "suggestion_outbox.json",
            lambda state: state.__setitem__(
                "proposals",
                {
                    "prop_scoped_console": {
                        "proposal_id": "prop_scoped_console",
                        "user_id": owner,
                        "session_id": session,
                        "status": "would_suggest",
                        "delivery": {"channel": "owner_scoped_console", "external_delivery": False},
                    },
                    "prop_scoped_recorded": {
                        "proposal_id": "prop_scoped_recorded",
                        "user_id": owner,
                        "session_id": session,
                        "status": "recorded",
                        "delivery": {"channel": "none", "external_delivery": False},
                    },
                    "prop_other_owner": {
                        "proposal_id": "prop_other_owner",
                        "user_id": "someone-else",
                        "session_id": session,
                        "status": "recorded",
                        "delivery": {"channel": "none", "external_delivery": False},
                    },
                },
            ),
        )
        scoped_boundary = client.get(f"/product/today?user_id={owner}&session_id={session}").json()["suggestions_boundary"]
        expect(
            scoped_boundary["other_ledger_recorded_count"] == 2
            and scoped_boundary["other_ledger_console_deliverable_count"] == 1
            and scoped_boundary["other_ledger_projected"] is False,
            f"the other suggestion ledger is counted for this exact owner only: {scoped_boundary}",
        )
        expect(
            all(item["disposition"] == "suggest" for item in client.get(f"/product/today?user_id={owner}&session_id={session}").json()["suggestions"]),
            "legacy proposals never enter the product suggestion list",
        )
        detail_with_reactions = client.get(f"/product/situations/{ids[1]}?user_id={owner}&session_id={session}").json()
        expect(all(item["situation_revision"] == current["revision"] for item in detail_with_reactions["reactions"]), "Situation detail excludes stale reactions")
        advanced = client.post(f"/product/situations/{ids[1]}/command?user_id={owner}&session_id={session}", json={"command": "correct", "expected_revision": 1, "patch": {"next_step": "The current next step changed."}})
        expect(advanced.status_code == 200 and advanced.json()["situation"]["revision"] == 2, "reaction stale-revision fixture advances through real command seam")
        stale_detail = client.get(f"/product/situations/{ids[1]}?user_id={owner}&session_id={session}").json()
        expect(stale_detail["reactions"] == [], "stale reaction is absent from current Situation detail")
        stale_row_feedback = client.post(f"/product/reactions/{reaction_id}/feedback?user_id={owner}&session_id={session}", json={"label": "ignore", "situation_revision": 1})
        expect(stale_row_feedback.status_code == 409, "feedback against stale current Situation fails closed")
        expect(client.get(f"/product/reactions?user_id={owner}&session_id={session}&situation_id=missing").status_code == 404, "missing reaction Situation is not observable")
        expect(client.post(f"/product/reactions/missing/feedback?user_id={owner}&session_id={session}", json={"label": "ignore", "situation_revision": 1}).status_code == 404, "missing reaction feedback target is not observable")

        sources = client.get(f"/product/sources?user_id={owner}&session_id={session}").json()
        expect(sources["schema_version"] == "veyra.product_sources.v1" and sources["items"]["calendar"]["available"] is False, "sources are projected without provider execution")
        matters = client.get(f"/product/matters?user_id={owner}&session_id={session}").json()
        expect(set(matters["sections"]) >= {"situations", "questions", "suggestions", "deadlines"}, "Matters keeps a stable section envelope")
        expect(client.get("/product/today?user_id=owner-a").status_code == 422, "legacy Today still requires exact scope when partially supplied")
        status_before = bytes_snapshot(store)
        product_status = client.get("/product/status")
        expect(product_status.status_code == 200 and product_status.json()["integrity"]["status"] == "success", "status performs a healthy read-only integrity check")
        expect(bytes_snapshot(store) == status_before, "status integrity check is byte-pure")

        class PagedReactionRuntime:
            def __init__(self, rows: list[dict[str, Any]]) -> None:
                self.rows = rows

            def list_reactions(self, **_: Any) -> list[dict[str, Any]]:
                return list(self.rows)

        current_revision = int(service.situation_detail(ids[0], user_id=owner, session_id=session)["situation"]["revision"])
        paged_rows = [
            {"owner_id": owner, "session_id": session, "situation_id": ids[0], "situation_revision": current_revision - 1, "reaction_id": "stale-suggestion-1", "disposition": "suggest", "category": "test", "created_at": "2026-08-17T15:03:00+00:00", "what_happened": "stale", "why_it_matters": "stale", "why_now": "stale", "suggested_next_step": "stale", "rank": 0.9},
            {"owner_id": owner, "session_id": session, "situation_id": ids[0], "situation_revision": current_revision - 1, "reaction_id": "stale-suggestion-2", "disposition": "suggest", "category": "test", "created_at": "2026-08-17T15:02:00+00:00", "what_happened": "stale", "why_it_matters": "stale", "why_now": "stale", "suggested_next_step": "stale", "rank": 0.8},
            {"owner_id": owner, "session_id": session, "situation_id": ids[0], "situation_revision": current_revision, "reaction_id": "current-suggestion", "disposition": "suggest", "category": "test", "created_at": "2026-08-17T15:01:00+00:00", "what_happened": "current", "why_it_matters": "current", "why_now": "current", "suggested_next_step": "current", "rank": 0.7},
        ]
        paged_service = ProductExperienceService(store, living_context_runtime=living, reaction_runtime=PagedReactionRuntime(paged_rows))
        paged_suggestions = paged_service.reactions(user_id=owner, session_id=session, situation_id=ids[0], limit=1, visible_dispositions={"suggest"})
        expect(len(paged_suggestions["items"]) == 1 and paged_suggestions["items"][0]["reaction_id"] == "current-suggestion", "suggestions filter current revision before limiting")

        before = bytes_snapshot(store)
        for path in (
            f"/product/today?user_id={owner}&session_id={session}",
            f"/product/matters?user_id={owner}&session_id={session}",
            f"/product/situations?user_id={owner}&session_id={session}",
            f"/product/situations/{ids[0]}?user_id={owner}&session_id={session}",
            f"/product/questions?user_id={owner}&session_id={session}",
            f"/product/reactions?user_id={owner}&session_id={session}",
            f"/product/suggestions?user_id={owner}&session_id={session}",
            f"/product/sources?user_id={owner}&session_id={session}",
        ):
            expect(client.get(path).status_code == 200, f"GET {path.split('?')[0]} succeeds")
        expect(bytes_snapshot(store) == before, "all product GETs are byte-pure")

        valid_state = store.read_json("situation_state.json")
        store.mutate_json("situation_state.json", lambda state: {**state, "situations": [{**item, "semantic": {**item["semantic"], "material_change": ""}} if item.get("record_kind") == "semantic_situation" else item for item in state["situations"]]})
        no_change_today = client.get(f"/product/today?user_id={owner}&session_id={session}")
        expect(no_change_today.status_code == 200 and no_change_today.json()["recent_changes"] == [], "Recent changes only exposes material changes")
        store.mutate_json("situation_state.json", lambda state: valid_state)

        def corrupt(state: dict[str, Any]) -> dict[str, Any]:
            for item in state["situations"]:
                if item.get("situation_id") == ids[0]:
                    item["semantic"] = "malformed"
                    break
            return state
        store.mutate_json("situation_state.json", corrupt)
        malformed = client.get(f"/product/situations?user_id={owner}&session_id={session}")
        expect(malformed.status_code == 200 and malformed.json()["status"] == "degraded" and malformed.json()["degraded"]["status"] == "degraded", "malformed semantic state is typed degraded")
        malformed_detail = client.get(f"/product/situations/{ids[0]}?user_id={owner}&session_id={session}")
        expect(malformed_detail.status_code == 200 and malformed_detail.json()["status"] == "degraded", "malformed Situation detail is typed degraded")
        degraded_status = client.get("/product/status")
        expect(degraded_status.status_code == 200 and degraded_status.json()["status"] == "degraded" and degraded_status.json()["integrity"]["status"] == "degraded", "status reports corrupted product state without repair")
        degraded_before = bytes_snapshot(store)
        malformed_today = client.get(f"/product/today?user_id={owner}&session_id={session}&first_meeting=true")
        expect(malformed_today.status_code == 200 and malformed_today.json()["first_meeting"] is False, "malformed Situation state never claims first meeting")
        expect(bytes_snapshot(store) == degraded_before, "malformed reads remain read-only")
        store.mutate_json("situation_state.json", lambda state: valid_state)
        empty_service = ProductExperienceService(WorldStateStore(Path(temporary) / "empty-state"))
        empty_today = empty_service.today(user_id=owner, session_id=session, first_meeting=True)
        expect(empty_today["status"] == "empty" and empty_today["first_meeting"] is True, "explicit empty Situation source may claim first meeting")

    print("RESULT product experience V1 smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
