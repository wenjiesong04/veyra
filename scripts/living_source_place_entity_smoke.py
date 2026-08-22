#!/usr/bin/env python3
"""Acceptance for the place-entity seam that binds a Need to a weather source.

A weather source target is derived only from a typed, reported ``place`` entity
carried by the Situation.  Without that entity the source policy cannot build a
binding, so a weather Need can never leave ``ask``/``wait`` no matter how the
question is worded.  This smoke pins the seam end to end and asserts that the
server still refuses an inferred place, a foreign scope and a revoked consent.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.world_state import WorldStateStore  # noqa: E402
from interface.event_schema import EventSource, EventType, VeyraEvent  # noqa: E402
from interface.living_context_contract import (  # noqa: E402
    SCHEMA_VERSION,
    CandidateEntity,
    CandidateKnown,
    CandidateNeed,
    ContextQuote,
    LivingContextCandidate,
    SituationProgress,
)
from runtime.living_context_composition import build_living_context_composition  # noqa: E402
from runtime.living_context_source_policy import LivingContextSourcePolicy  # noqa: E402

OWNER = "place-entity-owner"
SESSION = "place-entity-session"
MESSAGE = "下周六在上海有一场户外团建，需要确认当天天气。"
PLACE = "上海"


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 22, 9, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: int) -> None:
        self.value += timedelta(**kwargs)


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"PASS {label}")


def event(clock: Clock, text: str, event_id: str) -> VeyraEvent:
    timestamp = clock().isoformat()
    return VeyraEvent(
        type=EventType.USER_MESSAGE,
        source=EventSource(channel="api", user_id=OWNER, session_id=SESSION),
        payload={"text": text},
        event_id=event_id,
        timestamp=timestamp,
        occurred_at=timestamp,
        received_at=timestamp,
    )


def quote(text: str) -> ContextQuote:
    start = MESSAGE.index(text)
    return ContextQuote(text=text, start=start, end=start + len(text))


def candidate(*, place: str, epistemic_status: str) -> LivingContextCandidate:
    return LivingContextCandidate(
        schema_version=SCHEMA_VERSION,
        disposition="create",
        create_subject="上海户外团建天气",
        category="travel",
        summary="下周六上海户外团建，需要确认天气。",
        goal="确认团建当天的天气是否可行",
        progress=SituationProgress(status="in_progress"),
        known=[CandidateKnown(statement="团建在上海举行", epistemic_status="reported", source_quote=quote(PLACE))],
        unknown=["当天天气未确认"],
        entities=[
            CandidateEntity(
                kind="place",
                value=place,
                epistemic_status=epistemic_status,  # type: ignore[arg-type]
                source_quote=quote(PLACE) if place == PLACE else None,
            )
        ],
        needs=[
            CandidateNeed(
                blocked_judgment="当天天气未确认",
                evidence_kind="weather",
                why_now="团建前需要确认天气是否影响户外安排",
                urgency=0.7,
                allowed_source_classes=["weather"],
                fallback_reaction="read",
                question="上海下周六的天气如何？",
            )
        ],
        source="model",
    )


def authoritative(composition: Any, need: Any) -> dict[str, Any]:
    """Return the projection the orchestrator binds against, digest included."""

    if not isinstance(need, dict):
        return {}
    projection = composition.core.needs.authoritative_projection(
        str(need.get("need_id") or ""), owner_id=OWNER, session_id=SESSION
    )
    return {**need, **(projection or {})}


def seeded(store: WorldStateStore, clock: Clock, *, place: str = PLACE, epistemic_status: str = "reported"):
    composition = build_living_context_composition(store, clock=clock)
    result = composition.orchestrator.process_user_turn(
        event(clock, MESSAGE, f"place-seed-{epistemic_status}-{place}"),
        SimpleNamespace(
            living_context_candidate=candidate(place=place, epistemic_status=epistemic_status),
            living_reaction_feedback=None,
        ),
        catalog=[],
    )
    return composition, result


def main() -> int:
    with TemporaryDirectory() as tmp:
        store = WorldStateStore(Path(tmp))
        clock = Clock()
        composition, result = seeded(store, clock)
        situation = result["situation"]
        semantic = situation.get("semantic") or {}
        entities = semantic.get("entities") or []
        expect(
            any(
                str(item.get("kind")) == "place"
                and str(item.get("value")) == PLACE
                and str(item.get("epistemic_status")) == "reported"
                for item in entities
            ),
            "a reported place entity is admitted into the durable Situation",
            entities,
        )

        needs = composition.core.needs.list(
            owner_id=OWNER,
            session_id=SESSION,
            situation_id=str(situation.get("situation_id") or ""),
            limit=8,
        )
        weather_need = next((item for item in needs if str(item.get("evidence_kind")) == "weather"), None)
        expect(weather_need is not None, "a weather Need is persisted", needs)

        # Mirror the orchestrator: only the authoritative projection carries the
        # record digest the source policy binds against.
        policy = LivingContextSourcePolicy()
        binding = policy.derive_binding(
            situation=situation,
            need=authoritative(composition, weather_need),
            now=clock(),
        )
        expect(binding is not None, "the source policy derives a weather binding from the place entity", binding)
        expect(
            binding is not None and binding.source == "weather" and binding.parameters.get("location") == PLACE,
            "the derived binding targets the exact reported place",
            binding.parameters if binding else None,
        )

        # An inferred place is a model guess, not an external source target.
        inferred_store = WorldStateStore(Path(tmp) / "inferred")
        inferred_clock = Clock()
        inferred_composition, inferred_result = seeded(
            inferred_store, inferred_clock, epistemic_status="inferred"
        )
        inferred_needs = inferred_composition.core.needs.list(
            owner_id=OWNER,
            session_id=SESSION,
            situation_id=str(inferred_result["situation"].get("situation_id") or ""),
            limit=8,
        )
        inferred_need = next((item for item in inferred_needs if str(item.get("evidence_kind")) == "weather"), None)
        inferred_projection = inferred_composition.core.needs.authoritative_projection(
            str((inferred_need or {}).get("need_id") or ""), owner_id=OWNER, session_id=SESSION
        )
        expect(
            policy.derive_binding(
                situation=inferred_result["situation"],
                need={**(inferred_need or {}), **(inferred_projection or {})},
                now=inferred_clock(),
            )
            is None,
            "an inferred place never becomes a source target",
        )

        # Consent is required before the reaction may choose a background read.
        status = composition.orchestrator._source_status(OWNER, SESSION)
        expect(
            not status.get("consent", {}).get("weather", {}).get("granted"),
            "weather starts unconsented",
            status.get("consent"),
        )
        before = composition.orchestrator.tick(owner_id=OWNER, session_id=SESSION, limit=4)
        dispositions = [
            str(((item.get("reaction") or {}).get("decision") or {}).get("disposition") or "")
            for item in before.get("items", [])
        ]
        expect("read" not in dispositions, "an unconsented weather Need never reads", dispositions)

        granted = composition.orchestrator.grant_source_consent(
            "weather", owner_id=OWNER, session_id=SESSION, expected_generation=0
        )
        expect(granted["status"] == "granted", "weather consent is granted", granted)

        # The decision itself is deterministic; only the receipt status depends
        # on whether the provider is reachable from this host.
        after = composition.orchestrator.tick(owner_id=OWNER, session_id=SESSION, limit=4)
        read_items = [
            item
            for item in after.get("items", [])
            if str(((item.get("reaction") or {}).get("decision") or {}).get("disposition") or "") == "read"
        ]
        expect(bool(read_items), "a consented weather Need reaches the read disposition", after.get("items"))
        source_result = read_items[0].get("source") if read_items else None
        expect(
            isinstance(source_result, dict) and bool(str(source_result.get("status") or "").strip()),
            "the read attempt records a bounded source outcome",
            source_result,
        )
        expect(
            isinstance(source_result, dict) and source_result.get("reason") != "no_safe_source_binding",
            "the place entity removes the missing-binding block",
            source_result,
        )
        print(f"NOTE weather read outcome: {(source_result or {}).get('status')}")

        expect(
            composition.orchestrator.grant_source_consent(
                "weather", owner_id=OWNER, session_id=SESSION, expected_generation=1
            )["consent"]["generation"]
            == 2,
            "a CAS-approved renewal advances exactly one generation",
        )

        foreign = composition.orchestrator._source_status("other-owner", SESSION)
        expect(
            not foreign.get("consent", {}).get("weather", {}).get("granted"),
            "consent does not leak to another owner",
            foreign.get("consent"),
        )

        composition.orchestrator.revoke_source_consent(
            "weather", owner_id=OWNER, session_id=SESSION, expected_generation=2
        )
        revoked = composition.orchestrator._source_status(OWNER, SESSION)
        expect(
            not revoked.get("consent", {}).get("weather", {}).get("granted"),
            "revocation removes the read permission",
            revoked.get("consent"),
        )
        print("RESULT living source place entity smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
