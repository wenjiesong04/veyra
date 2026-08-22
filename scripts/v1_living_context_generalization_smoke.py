#!/usr/bin/env python3
"""Deterministic generalization proof for the V1 Living Context path.

The fixture rows below are typed test inputs only.  All three rows use one
``LivingContextComposition`` and one ``LivingContextOrchestrator``; production
code does not branch on their titles, scenario keys, or wording.
"""

from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.world_state import StateRevisionConflictError, WorldStateStore  # noqa: E402
from interface.event_schema import EventSource, EventType, VeyraEvent  # noqa: E402
from interface.living_context_contract import (  # noqa: E402
    CandidateKnown,
    CandidateNeed,
    LivingContextCandidate,
    SituationProgress,
)
from runtime.living_context_composition import build_living_context_composition  # noqa: E402


NOW = datetime(2026, 8, 20, 12, tzinfo=timezone.utc)
OWNER = "generalization-owner"
SESSION = "generalization-session"
OTHER_OWNER = "generalization-other-owner"
OTHER_SESSION = "generalization-other-session"

AUTHORITY_KEYS = (
    "route",
    "risk",
    "tool",
    "agent",
    "execution",
    "delivery",
    "permission_expansion",
)

# These markers are deliberately unique to this smoke.  They let the AST
# guard reject a future fixture-specific production lookup without banning
# legitimate generic category names such as ``health`` or ``finance``.
FIXTURES: tuple[dict[str, Any], ...] = (
    {
        "scenario_key": "health_follow_up_generalization_marker",
        "category": "health",
        "title": "Post-viral follow-up plan",
        "create_text": "I need to keep a post-viral follow-up plan organized.",
        "update_text": "The clinic sent a different follow-up window, so I adjusted the plan.",
        "goal": "Keep the follow-up steps organized.",
        "known": "A follow-up plan is being coordinated.",
        "update_known": "The follow-up window changed after a clinic message.",
        "unknown": "the next follow-up window",
        "question": "When is the next follow-up window?",
        "deadline": "2026-09-01T12:00:00+00:00",
        "progress": 0.25,
        "updated_progress": 0.55,
        "evidence_kind": "user",
        "allowed_source": "user",
        "fallback_reaction": "ask",
        "expected_reaction": "ask",
    },
    {
        "scenario_key": "education_exam_preparation_generalization_marker",
        "category": "education",
        "title": "Calculus qualifying-exam sprint",
        "create_text": "I am organizing a calculus qualifying-exam preparation sprint.",
        "update_text": "The study group moved one review block, and I recorded the new sequence.",
        "goal": "Prepare steadily for the qualifying exam.",
        "known": "A qualifying-exam preparation sprint is underway.",
        "update_known": "One review block moved in the study sequence.",
        "unknown": "the next review block",
        "question": "Which review block should happen next?",
        "deadline": "2026-09-10T12:00:00+00:00",
        "progress": 0.30,
        "updated_progress": 0.60,
        "evidence_kind": "other",
        "allowed_source": "other",
        "fallback_reaction": "wait",
        "expected_reaction": "wait",
    },
    {
        "scenario_key": "household_finance_admin_generalization_marker",
        "category": "finance",
        "title": "Household tax-reconciliation file",
        "create_text": "I am assembling a household tax-reconciliation file before its cutoff.",
        "update_text": "A receipt arrived from a different account, and I added it to the reconciliation.",
        "goal": "Finish the household reconciliation before the cutoff.",
        "known": "A household reconciliation file is being assembled.",
        "update_known": "A receipt from another account was added to the file.",
        "unknown": "the final receipt set",
        "question": "Which receipt is still missing from the file?",
        "deadline": "2026-08-20T15:00:00+00:00",
        "progress": 0.40,
        "updated_progress": 0.75,
        "evidence_kind": "other",
        "allowed_source": "other",
        "fallback_reaction": "wait",
        "expected_reaction": "suggest",
    },
)


class Clock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def expect(condition: bool, label: str, detail: object | None = None) -> None:
    if not condition:
        suffix = f": {detail!r}" if detail is not None else ""
        raise AssertionError(f"{label}{suffix}")
    print(f"PASS {label}")


def event(
    event_id: str,
    text: str,
    *,
    owner_id: str = OWNER,
    session_id: str = SESSION,
) -> VeyraEvent:
    timestamp = NOW.isoformat()
    return VeyraEvent(
        type=EventType.USER_MESSAGE,
        source=EventSource(channel="api", user_id=owner_id, session_id=session_id),
        payload={"text": text},
        event_id=event_id,
        timestamp=timestamp,
        occurred_at=timestamp,
        received_at=timestamp,
    )


def candidate(
    fixture: Mapping[str, Any],
    *,
    disposition: str,
    catalog_row: Mapping[str, Any] | None = None,
    update: bool = False,
) -> LivingContextCandidate:
    """Build one strict typed candidate; no raw candidate dictionaries enter production."""

    is_update = disposition != "create"
    row = catalog_row or {}
    return LivingContextCandidate(
        schema_version="veyra.living_context_candidate.v1",
        disposition=disposition,  # type: ignore[arg-type]
        situation_token=(str(row["situation_token"]) if is_update else None),
        situation_revision=(int(row["observation_revision"]) if is_update else None),
        catalog_token=(str(row["catalog_token"]) if is_update else None),
        create_subject=(str(fixture["title"]) if not is_update else ""),
        category=str(fixture["category"]),  # type: ignore[arg-type]
        label=str(fixture["title"]),
        title=str(fixture["title"]),
        summary=str(fixture["update_text"] if update else fixture["create_text"]),
        goal=str(fixture["goal"]),
        deadline_at=str(fixture["deadline"]),
        progress=SituationProgress(
            status="in_progress",
            value=float(fixture["updated_progress"] if update else fixture["progress"]),
        ),
        lifecycle="active",
        known=[
            CandidateKnown(
                statement=str(fixture["update_known"] if update else fixture["known"]),
                epistemic_status="reported",
            )
        ],
        unknown=[] if update else [str(fixture["unknown"])],
        material_change=("A differently worded progress update was recorded." if update else ""),
        next_step=("Recheck the remaining bounded information." if update else "Confirm the next bounded step."),
        next_step_epistemic_status="inferred",
        needs=(
            []
            if update
            else [
                CandidateNeed(
                    blocked_judgment=str(fixture["unknown"]),
                    evidence_kind=str(fixture["evidence_kind"]),  # type: ignore[arg-type]
                    why_now="This missing detail changes the next bounded judgment.",
                    urgency=0.7,
                    allowed_source_classes=[str(fixture["allowed_source"])],
                    fallback_reaction=str(fixture["fallback_reaction"]),  # type: ignore[arg-type]
                    question=str(fixture["question"]),
                )
            ]
        ),
        requested_reaction=("ask" if fixture["expected_reaction"] == "ask" else "wait"),
        source="model",
    )


def row_for(catalog: list[dict[str, Any]], situation_id: str) -> dict[str, Any]:
    rows = [
        row
        for row in catalog
        if isinstance(row, dict) and str(row.get("situation_token") or "") == situation_id
    ]
    if len(rows) != 1:
        raise AssertionError(f"expected one catalog row for {situation_id}, got {len(rows)}")
    return rows[0]


def assert_authority(result: Mapping[str, Any], label: str) -> None:
    authority = result.get("authority")
    expect(isinstance(authority, dict), f"{label} has an authority projection")
    for key in AUTHORITY_KEYS:
        expect(authority.get(key) is False, f"{label} keeps {key} authority disabled", authority)
    decision = result.get("decision")
    if not isinstance(decision, dict):
        decision = result.get("reaction", {}).get("decision") if isinstance(result.get("reaction"), dict) else None
    expect(isinstance(decision, dict), f"{label} has one bounded reaction decision")
    decision_authority = decision.get("authority")
    expect(isinstance(decision_authority, dict), f"{label} reaction authority is explicit")
    expect(not any(bool(value) for value in decision_authority.values()), f"{label} reaction has no effect authority")
    expect(decision.get("external_delivery") is False, f"{label} reaction has no delivery authority")
    expect(decision.get("record_only") is True, f"{label} reaction is record-only")


def assert_no_fixture_knowledge_in_production() -> None:
    """Reject fixture literals and multi-scenario dispatch tables in production AST."""

    production_modules = (
        ROOT / "core" / "living_context_candidate_extractor.py",
        ROOT / "core" / "understanding_core.py",
        ROOT / "interface" / "living_context_contract.py",
        ROOT / "runtime" / "living_context_composition.py",
        ROOT / "runtime" / "living_context_orchestrator.py",
        ROOT / "runtime" / "living_context_runtime.py",
        ROOT / "runtime" / "living_reaction_policy.py",
    )
    forbidden_literals = {
        str(value)
        for fixture in FIXTURES
        for key in ("scenario_key", "title", "create_text", "update_text", "goal", "known", "update_known")
        for value in (fixture[key],)
    }
    scenario_markers = {str(fixture["scenario_key"]) for fixture in FIXTURES}
    for path in production_modules:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        literals = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        overlap = forbidden_literals.intersection(literals)
        expect(not overlap, "production AST contains no fixture-specific literals", {"path": str(path), "overlap": sorted(overlap)})
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            table_literals = {
                item.value
                for item in (*node.keys, *node.values)
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            }
            dispatch_overlap = scenario_markers.intersection(table_literals)
            expect(
                len(dispatch_overlap) < 2,
                "production AST contains no multi-scenario fixture dispatch table",
                {"path": str(path), "markers": sorted(dispatch_overlap)},
            )


def main() -> int:
    assert_no_fixture_knowledge_in_production()
    with TemporaryDirectory(prefix="veyra-v1-generalization-") as temp:
        clock = Clock()
        store = WorldStateStore(Path(temp) / "state")
        composition = build_living_context_composition(store, clock=clock)
        orchestrator = composition.orchestrator
        situation_ids: list[str] = []
        update_pairs: list[tuple[VeyraEvent, LivingContextCandidate, list[dict[str, Any]], int]] = []
        expected_reactions: dict[str, str] = {}
        update_reaction_dispositions: list[str] = []

        for index, fixture in enumerate(FIXTURES, start=1):
            before = orchestrator.model_catalog(owner_id=OWNER, session_id=SESSION)
            create_event = event(f"evt_generalization_create_{index}", str(fixture["create_text"]))
            create_candidate = candidate(fixture, disposition="create")
            create_result = orchestrator.process_user_turn(
                create_event,
                SimpleNamespace(living_context_candidate=create_candidate, living_reaction_feedback=None),
                catalog=before,
            )
            expect(create_result.get("status") == "recorded", f"{fixture['category']} create is recorded", create_result)
            situation = create_result.get("situation")
            expect(isinstance(situation, dict), f"{fixture['category']} create has a Situation")
            situation_id = str(situation["situation_id"])
            situation_ids.append(situation_id)
            expected_reaction = str(fixture["expected_reaction"])
            create_reaction = create_result.get("reaction") if isinstance(create_result.get("reaction"), dict) else {}
            create_decision = create_reaction.get("decision") if isinstance(create_reaction.get("decision"), dict) else {}
            expect(create_decision.get("disposition") == expected_reaction, f"{fixture['category']} reaches {expected_reaction}", create_decision)
            expected_reactions[str(fixture["category"])] = expected_reaction
            assert_authority(create_result, f"{fixture['category']} create")

            semantic = situation.get("semantic") if isinstance(situation.get("semantic"), dict) else {}
            expect(semantic.get("category") == fixture["category"], f"{fixture['category']} category persists")
            expect(semantic.get("goal") == fixture["goal"], f"{fixture['category']} goal persists")
            progress = semantic.get("progress") if isinstance(semantic.get("progress"), dict) else {}
            expect(progress.get("value") == fixture["progress"], f"{fixture['category']} progress persists")
            expect(semantic.get("deadline_at") == fixture["deadline"], f"{fixture['category']} deadline persists")
            expect(any(item.get("statement") == fixture["known"] for item in semantic.get("known", []) if isinstance(item, dict)), f"{fixture['category']} Known persists")
            expect(str(fixture["unknown"]) in semantic.get("unknown", []), f"{fixture['category']} Unknown persists")
            needs = create_result.get("information_needs")
            expect(isinstance(needs, list) and len(needs) == 1, f"{fixture['category']} has one InformationNeed", needs)
            need = needs[0]
            expect(
                need.get("blocked_judgment") == fixture["unknown"]
                and need.get("owner_id") == OWNER
                and need.get("session_id") == SESSION
                and need.get("situation_id") == situation_id,
                f"{fixture['category']} Need is persisted in exact scope",
                need,
            )

            current_catalog = orchestrator.model_catalog(owner_id=OWNER, session_id=SESSION)
            current_row = row_for(current_catalog, situation_id)
            expect(
                current_row.get("situation_token") == situation_id
                and int(current_row.get("observation_revision") or 0) == int(situation["observation_revision"])
                and str(current_row.get("catalog_token") or ""),
                f"{fixture['category']} catalog carries exact server binding",
                current_row,
            )

            # An exact token cannot cross either owner or session boundary.
            update_probe = candidate(fixture, disposition="update", catalog_row=current_row, update=True)
            for wrong_owner, wrong_session, scope_label in (
                (OTHER_OWNER, SESSION, "owner"),
                (OWNER, OTHER_SESSION, "session"),
            ):
                try:
                    orchestrator.process_user_turn(
                        event(f"evt_generalization_wrong_{index}_{scope_label}", str(fixture["update_text"]), owner_id=wrong_owner, session_id=wrong_session),
                        SimpleNamespace(living_context_candidate=update_probe, living_reaction_feedback=None),
                        catalog=current_catalog,
                    )
                except (KeyError, PermissionError, StateRevisionConflictError):
                    expect(True, f"{fixture['category']} token rejects cross-{scope_label} reuse")
                else:  # pragma: no cover - defensive assertion
                    expect(False, f"{fixture['category']} token rejects cross-{scope_label} reuse")
            expect(
                not orchestrator.model_catalog(owner_id=OTHER_OWNER, session_id=SESSION)
                and not orchestrator.model_catalog(owner_id=OWNER, session_id=OTHER_SESSION),
                f"{fixture['category']} cross-scope catalogs stay empty",
            )

            update_event = event(f"evt_generalization_update_{index}", str(fixture["update_text"]))
            update_result = orchestrator.process_user_turn(
                update_event,
                SimpleNamespace(living_context_candidate=update_probe, living_reaction_feedback=None),
                catalog=current_catalog,
                expected_revision=int(situation["observation_revision"]),
            )
            expect(update_result.get("status") == "recorded", f"{fixture['category']} differently worded update is recorded", update_result)
            expect(update_result.get("operation") == "update", f"{fixture['category']} update uses shared update operation")
            updated = update_result.get("situation")
            expect(isinstance(updated, dict), f"{fixture['category']} update has a Situation")
            expect(updated.get("situation_id") == situation_id, f"{fixture['category']} update keeps stable Situation identity")
            expect(int(updated.get("observation_revision") or 0) == int(situation["observation_revision"]) + 1, f"{fixture['category']} update advances exact revision")
            updated_semantic = updated.get("semantic") if isinstance(updated.get("semantic"), dict) else {}
            updated_progress = updated_semantic.get("progress") if isinstance(updated_semantic.get("progress"), dict) else {}
            expect(updated_progress.get("value") == fixture["updated_progress"], f"{fixture['category']} update persists progress")
            expect(updated_semantic.get("deadline_at") == fixture["deadline"], f"{fixture['category']} update preserves deadline")
            expect(any(item.get("statement") == fixture["update_known"] for item in updated_semantic.get("known", []) if isinstance(item, dict)), f"{fixture['category']} update persists differently worded Known")
            update_reaction = update_result.get("reaction") if isinstance(update_result.get("reaction"), dict) else {}
            expect(isinstance(update_reaction.get("decision"), dict), f"{fixture['category']} update has one bounded reaction")
            update_decision = update_reaction.get("decision") if isinstance(update_reaction.get("decision"), dict) else {}
            update_reaction_dispositions.append(str(update_decision.get("disposition") or ""))
            assert_authority(update_result, f"{fixture['category']} update")
            update_pairs.append((update_event, update_probe, current_catalog, int(situation["observation_revision"])))

            reaction_count_before = int(composition.reaction.status().get("reaction_count") or 0)
            replay = orchestrator.process_user_turn(
                update_event,
                SimpleNamespace(living_context_candidate=update_probe, living_reaction_feedback=None),
                catalog=current_catalog,
                expected_revision=int(situation["observation_revision"]),
            )
            expect(replay.get("status") == "replayed" and replay.get("semantic_replayed") is True, f"{fixture['category']} exact update replay is idempotent", replay)
            replayed_situation = replay.get("situation")
            expect(isinstance(replayed_situation, dict) and int(replayed_situation.get("observation_revision") or 0) == int(updated["observation_revision"]), f"{fixture['category']} replay does not advance revision")
            expect(int(composition.reaction.status().get("reaction_count") or 0) == reaction_count_before, f"{fixture['category']} replay does not grow reaction ledger")

        expect(set(expected_reactions.values()) == {"ask", "wait", "suggest"}, "three unfamiliar categories reach ask, wait, and suggest")
        expect(
            len(update_reaction_dispositions) == len(FIXTURES)
            and set(update_reaction_dispositions).issubset({"ask", "read", "wait", "silent", "suggest"}),
            "differently worded updates remain inside the shared bounded reaction vocabulary",
        )

        restarted = build_living_context_composition(WorldStateStore(Path(temp) / "state"), clock=clock).orchestrator
        restored = restarted.core.list_situations(owner_id=OWNER, session_id=SESSION)
        expect({str(item["situation_id"]) for item in restored} == set(situation_ids), "restart restores all three Situations")
        expect(not restarted.core.list_situations(owner_id=OTHER_OWNER, session_id=SESSION), "restart preserves owner isolation")
        expect(not restarted.core.list_situations(owner_id=OWNER, session_id=OTHER_SESSION), "restart preserves session isolation")
        restarted_catalog = restarted.model_catalog(owner_id=OWNER, session_id=SESSION, limit=8)
        expect(len(restarted_catalog) == len(FIXTURES), "restart restores one catalog row per Situation")
        for fixture in FIXTURES:
            restored_row = next(row for row in restarted_catalog if row.get("category") == fixture["category"])
            expect(restored_row.get("title") == fixture["title"], f"restart preserves {fixture['category']} title")
            expect(isinstance(restored_row.get("reaction"), dict), f"restart restores {fixture['category']} reaction projection")

        # Replay once more through a fresh orchestrator to cover restart plus
        # admission-ledger idempotency, still using the original exact catalog.
        for index, (update_event, update_candidate, update_catalog, initial_revision) in enumerate(update_pairs, start=1):
            replay = restarted.process_user_turn(
                update_event,
                SimpleNamespace(living_context_candidate=update_candidate, living_reaction_feedback=None),
                catalog=update_catalog,
                expected_revision=initial_revision,
            )
            expect(replay.get("status") == "replayed", f"restart replay {index} remains idempotent")
            assert_authority(replay, f"restart replay {index}")

    print("V1_LIVING_CONTEXT_GENERALIZATION_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
