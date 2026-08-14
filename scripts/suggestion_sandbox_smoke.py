#!/usr/bin/env python3
from __future__ import annotations

import copy
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, Callable

from fastapi import FastAPI
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


from core.context_scope import tenant_scope_storage_key  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402
from routers.debug_audit import build_debug_audit_router  # noqa: E402
from runtime.learning_calibration_runtime import (  # noqa: E402
    LearningCalibrationConflict,
    LearningCalibrationRuntime,
)
from runtime.event_awareness_runtime import ShadowAwarenessRuntime  # noqa: E402
from runtime.suggestion_outbox import (  # noqa: E402
    SuggestionOutbox,
    SuggestionOutboxConflict,
)
from runtime.attention_hypothesis_runtime import AttentionHypothesisRuntime  # noqa: E402
from scripts.attention_hypothesis_smoke import (  # noqa: E402
    CONFIRMING_VALUES,
    LOW_VALUES,
    MutableClock,
    assessment_for,
    parent_for,
    persist_parent,
)


USER = "suggestion-owner"
SESSION = "suggestion-session"
NOW = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"PASS {label}")


def expect_raises(
    error_type: type[BaseException],
    label: str,
    call: Callable[[], Any],
) -> None:
    try:
        call()
    except error_type:
        print(f"PASS {label}")
        return
    raise AssertionError(f"{label} failed: {error_type.__name__} not raised")


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def proposal_inputs(
    store: WorldStateStore,
    attention: AttentionHypothesisRuntime,
    clock: MutableClock,
    index: int = 1,
) -> tuple[dict[str, Any], dict[str, Any]]:
    parent = parent_for(
        user_id=USER,
        session_id=SESSION,
        revision=1,
        child_count=2,
        clock=clock,
        anchor=f"goal:suggestion-{index}",
        general_id=f"gsit_suggestion_{index}",
    )
    persist_parent(store, parent)
    result = attention.observe(
        parent,
        assessment_for(
            parent,
            values=CONFIRMING_VALUES,
            store=store,
            clock=clock,
        ),
    )
    surface = result.get("surface_assessment")
    if result.get("status") != "confirmed" or not isinstance(surface, dict):
        raise AssertionError(f"proposal fixture did not confirm: {result}")
    return parent, surface


def feedback_kwargs(
    proposal: dict[str, Any],
    *,
    feedback_id: str,
    label: str,
    outbox_revision: int,
    supersedes_learning_id: str | None = None,
) -> dict[str, Any]:
    return {
        "feedback_id": feedback_id,
        "user_id": USER,
        "session_id": SESSION,
        "proposal_id": str(proposal["proposal_id"]),
        "proposal_revision": str(proposal["proposal_revision"]),
        "general_situation_id": str(proposal["general_situation_id"]),
        "parent_revision": int(proposal["parent_revision"]),
        "label": label,
        "expected_outbox_state_revision": outbox_revision,
        "supersedes_learning_id": supersedes_learning_id,
    }


def corruption_freeze_case(
    *,
    label: str,
    corrupt: Callable[[dict[str, Any]], None],
    forbidden_value: str | None = None,
) -> None:
    with TemporaryDirectory(prefix=f"veyra-suggestion-corrupt-{label}-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        clock = MutableClock(NOW)
        outbox = SuggestionOutbox(store, clock=clock)
        attention = AttentionHypothesisRuntime(store, clock=clock)
        outbox.configure_mode(
            "advise_only",
            expected_state_revision=int(
                store.read_json("ops_config.json").get("_state_revision") or 0
            ),
        )
        outbox.configure_policy(
            user_id=USER,
            session_id=SESSION,
            sandbox_enabled=True,
            daily_budget=1,
            quiet_hours=None,
            cooldown_seconds=3600,
            dismiss_cooldown_seconds=86400,
            expected_state_revision=int(
                store.read_json(SuggestionOutbox.STATE_FILE).get(
                    "_state_revision"
                )
                or 0
            ),
        )
        parent, assessment = proposal_inputs(store, attention, clock, 90)
        surfaced = outbox.consider(
            parent,
            assessment,
            user_id=USER,
            session_id=SESSION,
        )
        proposal = surfaced.get("proposal") or {}
        expect(
            surfaced.get("status") == "pending",
            f"{label} fixture surfaces before corruption",
            surfaced,
        )

        def inject(state: dict[str, Any]) -> dict[str, Any]:
            corrupt(state)
            return state

        store.mutate_json(SuggestionOutbox.STATE_FILE, inject)
        outbox_path = store.path_for(SuggestionOutbox.STATE_FILE)
        before = file_digest(outbox_path)
        ops_path = store.path_for("ops_config.json")
        ops_before = file_digest(ops_path)
        revision = int(
            store.read_json(SuggestionOutbox.STATE_FILE).get("_state_revision")
            or 0
        )
        status = outbox.status()
        inbox = outbox.list_inbox(user_id=USER, session_id=SESSION)
        reconsidered = outbox.consider(
            parent,
            assessment,
            user_id=USER,
            session_id=SESSION,
        )
        mode_update = outbox.configure_mode(
            "shadow",
            expected_state_revision=int(
                store.read_json("ops_config.json").get("_state_revision") or 0
            ),
        )
        expect_raises(
            SuggestionOutboxConflict,
            f"{label} freezes policy writes",
            lambda: outbox.configure_policy(
                user_id=USER,
                session_id=SESSION,
                sandbox_enabled=True,
                daily_budget=1,
                quiet_hours=None,
                cooldown_seconds=3600,
                dismiss_cooldown_seconds=86400,
                expected_state_revision=revision,
            ),
        )
        expect_raises(
            SuggestionOutboxConflict,
            f"{label} freezes proposal transitions",
            lambda: outbox.dismiss(
                str(proposal.get("proposal_id") or ""),
                user_id=USER,
                session_id=SESSION,
                expected_state_revision=revision,
            ),
        )
        after = file_digest(outbox_path)
        ops_after = file_digest(ops_path)
        public_text = json.dumps(
            {
                "status": status,
                "inbox": inbox,
                "reconsidered": reconsidered,
                "mode_update": mode_update,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        expect(
            status.get("status") == "fail_closed"
            and inbox.get("status") == "fail_closed"
            and reconsidered.get("status") == "fail_closed"
            and mode_update.get("status") == "fail_closed"
            and before == after
            and ops_before == ops_after
            and (
                forbidden_value is None
                or (
                    forbidden_value not in public_text
                    and "raw_user_text" not in public_text
                )
            ),
            f"{label} freezes all outbox reads and writes byte-pure",
            {
                "status": status,
                "inbox": inbox,
                "consider": reconsidered,
                "mode_update": mode_update,
            },
        )


def legacy_proposal_isolation_case() -> None:
    with TemporaryDirectory(prefix="veyra-suggestion-legacy-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        clock = MutableClock(NOW)
        outbox = SuggestionOutbox(store, clock=clock)
        attention = AttentionHypothesisRuntime(store, clock=clock)
        outbox.configure_mode(
            "advise_only",
            expected_state_revision=int(
                store.read_json("ops_config.json").get("_state_revision") or 0
            ),
        )
        outbox.configure_policy(
            user_id=USER,
            session_id=SESSION,
            sandbox_enabled=True,
            daily_budget=1,
            quiet_hours=None,
            cooldown_seconds=3600,
            dismiss_cooldown_seconds=86400,
            expected_state_revision=int(
                store.read_json(SuggestionOutbox.STATE_FILE).get(
                    "_state_revision"
                )
                or 0
            ),
        )
        parent, assessment = proposal_inputs(store, attention, clock, 91)
        surfaced = outbox.consider(
            parent,
            assessment,
            user_id=USER,
            session_id=SESSION,
        )
        proposal_id = str(
            (surfaced.get("proposal") or {}).get("proposal_id") or ""
        )

        def downgrade(state: dict[str, Any]) -> dict[str, Any]:
            proposal = state["proposals"][proposal_id]
            proposal["schema_version"] = "veyra.informational_suggestion.v1"
            for key in (
                "proposal_revision",
                "attention_hypothesis_ref",
                "attention_readiness",
                "evidence_diversity",
                "assessment_binding",
                "source_expires_at",
                "upstream_scorer_version",
            ):
                proposal.pop(key, None)
            return state

        store.mutate_json(SuggestionOutbox.STATE_FILE, downgrade)
        path = store.path_for(SuggestionOutbox.STATE_FILE)
        before = file_digest(path)
        inbox = outbox.list_inbox(user_id=USER, session_id=SESSION)
        status = outbox.status()
        expect_raises(
            SuggestionOutboxConflict,
            "legacy v1 proposal rejects new lifecycle feedback",
            lambda: outbox.dismiss(
                proposal_id,
                user_id=USER,
                session_id=SESSION,
                expected_state_revision=int(
                    store.read_json(SuggestionOutbox.STATE_FILE).get(
                        "_state_revision"
                    )
                    or 0
                ),
            ),
        )
        expect(
            status.get("status") == "success"
            and inbox.get("count") == 0
            and inbox.get("legacy_hidden_count") == 1
            and before == file_digest(path),
            "legacy v1 proposal is structurally isolated and byte-pure",
            inbox,
        )


def real_clock_currentness_case() -> None:
    with TemporaryDirectory(prefix="veyra-suggestion-real-clock-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        outbox = SuggestionOutbox(store)
        attention = AttentionHypothesisRuntime(store)
        fixture_clock = MutableClock(datetime.now(timezone.utc))
        outbox.configure_mode(
            "advise_only",
            expected_state_revision=int(
                store.read_json("ops_config.json").get("_state_revision") or 0
            ),
        )
        outbox.configure_policy(
            user_id=USER,
            session_id=SESSION,
            sandbox_enabled=True,
            daily_budget=1,
            quiet_hours=None,
            timezone="UTC",
            cooldown_seconds=3600,
            dismiss_cooldown_seconds=86400,
            expected_state_revision=int(
                store.read_json(SuggestionOutbox.STATE_FILE).get(
                    "_state_revision"
                )
                or 0
            ),
        )
        parent, assessment = proposal_inputs(
            store,
            attention,
            fixture_clock,
            92,
        )
        surfaced = outbox.consider(
            parent,
            assessment,
            user_id=USER,
            session_id=SESSION,
        )
        count = int(
            store.read_json(SuggestionOutbox.STATE_FILE).get(
                "proposal_count"
            )
            or 0
        )
        later = SuggestionOutbox(
            store,
            clock=lambda: datetime.now(timezone.utc) + timedelta(seconds=1),
        )
        inbox = later.list_inbox(user_id=USER, session_id=SESSION)
        replayed = later.consider(
            parent,
            assessment,
            user_id=USER,
            session_id=SESSION,
        )
        expect(
            surfaced.get("status") == "pending"
            and inbox.get("count") == 1
            and replayed.get("status") == "replayed"
            and int(
                store.read_json(SuggestionOutbox.STATE_FILE).get(
                    "proposal_count"
                )
                or 0
            )
            == count,
            "real-clock micro-drift and t+1 freshness keep current proposal visible",
            {
                "surfaced": surfaced,
                "inbox": inbox,
                "replayed": replayed,
            },
        )


def interaction_disposition_case() -> None:
    """Decision economics is typed and never creates a delivery side effect."""

    with TemporaryDirectory(prefix="veyra-interaction-disposition-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        clock = MutableClock(NOW)
        outbox = SuggestionOutbox(store, clock=clock)
        attention = AttentionHypothesisRuntime(store, clock=clock)

        def persisted_surface(
            index: int,
            *,
            user_id: str = USER,
            session_id: str = SESSION,
            accumulating: bool = False,
        ) -> tuple[dict[str, Any], dict[str, Any]]:
            general_id = f"gsit-interaction-disposition-{index}"
            parent = parent_for(
                user_id=user_id,
                session_id=session_id,
                revision=1,
                child_count=2,
                clock=clock,
                anchor=f"goal:interaction-{index}",
                general_id=general_id,
            )
            persist_parent(store, parent)
            first_assessment = assessment_for(
                parent,
                values=LOW_VALUES,
                store=store,
                clock=clock,
            )
            first = attention.observe(parent, first_assessment)
            if not accumulating:
                selected_parent = parent
                selected = first
            else:
                selected_parent = parent_for(
                    user_id=user_id,
                    session_id=session_id,
                    revision=2,
                    child_count=3,
                    clock=clock,
                    anchor=f"goal:interaction-{index}",
                    general_id=general_id,
                )
                second_assessment = assessment_for(
                    selected_parent,
                    values=LOW_VALUES,
                    store=store,
                    clock=clock,
                )
                selected = attention.observe(
                    selected_parent,
                    second_assessment,
                )
            surface = selected.get("surface_assessment")
            expect(
                selected.get("status") in {"candidate", "accumulating"}
                and isinstance(surface, dict)
                and isinstance(surface.get("attention_hypothesis_ref"), dict),
                f"{general_id} persists a current {selected.get('status')} binding",
                selected,
            )
            return selected_parent, copy.deepcopy(surface)

        candidate_parent, candidate_surface = persisted_surface(1)
        accumulating_parent, accumulating_surface = persisted_surface(
            2,
            accumulating=True,
        )
        ask_parent, ask_surface = persisted_surface(3)
        ask_surface["interaction_gap"] = {
            "kind": "owner_question",
            "gap_id": "gap_interaction_missing_input",
            "answerable": True,
        }
        silent_parent, silent_surface = persisted_surface(4)
        silent_surface["hypothesis_status"] = candidate_surface["hypothesis_status"]
        silent_surface["status"] = "awaiting_evidence"
        silent_surface["eligible"] = False
        positive_cases = (
            (
                "candidate",
                candidate_parent,
                candidate_surface,
                "wait",
                "attention_evidence_accumulating",
            ),
            (
                "accumulating",
                accumulating_parent,
                accumulating_surface,
                "wait",
                "attention_evidence_accumulating",
            ),
            (
                "owner_question_dormant",
                ask_parent,
                ask_surface,
                "wait",
                "attention_evidence_accumulating",
            ),
            (
                "noneligible_caller_override_dormant",
                silent_parent,
                silent_surface,
                "wait",
                "attention_evidence_accumulating",
            ),
        )
        for (
            label,
            parent,
            surface,
            expected_decision,
            expected_reason,
        ) in positive_cases:
            result = outbox.consider(
                parent,
                surface,
                user_id=USER,
                session_id=SESSION,
            )
            expect(
                result.get("status") == "not_proposed"
                and result.get("decision_disposition") == expected_decision
                and result.get("delivery_disposition") == "none"
                and result.get("reason") == expected_reason
                and result.get("proposal") is None
                and isinstance(result.get("interaction_decision"), dict)
                and result["interaction_decision"].get(
                    "attention_hypothesis_ref"
                )
                == surface.get("attention_hypothesis_ref"),
                f"{label} produces {expected_decision} with a persisted exact binding",
                result,
            )

        ledger_path = store.path_for(SuggestionOutbox.STATE_FILE)
        ledger = store.read_json(SuggestionOutbox.STATE_FILE)
        decisions = ledger.get("interaction_decisions") or {}
        hypothesis_ref = candidate_surface["attention_hypothesis_ref"]
        general_state = store.read_json("general_situation_state.json")
        attention_state = store.read_json("attention_hypothesis_state.json")
        persisted_parent = (
            general_state.get("general_situations", {})
            .get(candidate_parent["general_situation_id"])
        )
        persisted_hypothesis = (
            attention_state.get("hypotheses", {})
            .get(hypothesis_ref["hypothesis_id"])
        )
        expect(
            ledger.get("interaction_decision_count") == 4
            and len(decisions) == 4
            and isinstance(persisted_parent, dict)
            and persisted_parent.get("user_id") == USER
            and tenant_scope_storage_key(USER, SESSION)
            in set(persisted_parent.get("session_scope_keys") or [])
            and persisted_parent.get("parent_revision")
            == candidate_parent.get("parent_revision")
            and isinstance(persisted_hypothesis, dict)
            and persisted_hypothesis.get("user_id") == USER
            and tenant_scope_storage_key(USER, SESSION)
            in set(persisted_hypothesis.get("session_scope_keys") or [])
            and AttentionHypothesisRuntime._parent_binding(persisted_parent)
            == persisted_hypothesis.get("parent_binding")
            and all(
                item.get("attention_hypothesis_ref") is not None
                and item.get("user_id") == USER
                and item.get("session_id") == SESSION
                for item in decisions.values()
            ),
            "positive decisions are parent-derived and exact-owner/session scoped",
            {
                "decision_count": ledger.get("interaction_decision_count"),
                "parent": persisted_parent,
                "hypothesis": persisted_hypothesis,
            },
        )

        _, foreign_owner_surface = persisted_surface(
            5,
            user_id="foreign-owner",
        )
        _, foreign_session_surface = persisted_surface(
            6,
            session_id="foreign-session",
        )
        missing_surface = copy.deepcopy(candidate_surface)
        missing_surface.pop("attention_hypothesis_ref", None)
        stale_surface = copy.deepcopy(candidate_surface)
        stale_surface["attention_hypothesis_ref"]["hypothesis_revision"] += 1
        cross_owner_surface = copy.deepcopy(candidate_surface)
        cross_owner_surface["attention_hypothesis_ref"] = copy.deepcopy(
            foreign_owner_surface["attention_hypothesis_ref"]
        )
        cross_session_surface = copy.deepcopy(candidate_surface)
        cross_session_surface["attention_hypothesis_ref"] = copy.deepcopy(
            foreign_session_surface["attention_hypothesis_ref"]
        )
        rejection_cases = (
            (
                "missing binding",
                missing_surface,
                "current_attention_hypothesis_required",
            ),
            (
                "stale binding",
                stale_surface,
                "attention_hypothesis_binding_not_current",
            ),
            (
                "cross-owner binding",
                cross_owner_surface,
                "attention_hypothesis_binding_not_current",
            ),
            (
                "cross-session binding",
                cross_session_surface,
                "attention_hypothesis_binding_not_current",
            ),
        )
        for label, forged_surface, expected_reason in rejection_cases:
            before = ledger_path.read_bytes()
            before_count = int(
                store.read_json(SuggestionOutbox.STATE_FILE).get(
                    "interaction_decision_count"
                )
                or 0
            )
            rejected = outbox.consider(
                candidate_parent,
                forged_surface,
                user_id=USER,
                session_id=SESSION,
            )
            expect(
                rejected.get("status") == "fail_closed"
                and rejected.get("reason") == expected_reason
                and rejected.get("interaction_decision") is None
                and before == ledger_path.read_bytes()
                and int(
                    store.read_json(SuggestionOutbox.STATE_FILE).get(
                        "interaction_decision_count"
                    )
                    or 0
                )
                == before_count,
                f"{label} fails closed without ledger mutation",
                rejected,
            )


def pipeline_fail_closed_projection_case() -> None:
    parent = {
        "general_situation_id": "gsit_pipeline_fail_closed",
        "parent_revision": 1,
    }
    runtime = SimpleNamespace(
        general_situations=SimpleNamespace(
            ingest_child=lambda situation, event: {
                "status": "replayed",
                "general_situation": parent,
            }
        ),
        general_attention=SimpleNamespace(
            assess=lambda value: {"status": "eligible"}
        ),
        attention_hypotheses=SimpleNamespace(
            observe=lambda value, assessment: {
                "status": "confirmed",
                "surface_assessment": {"status": "eligible"},
            }
        ),
        suggestion_outbox=SimpleNamespace(
            consider=lambda *args, **kwargs: {
                "status": "fail_closed",
                "reason": "suggestion_outbox_state_corrupt",
            }
        ),
    )
    event = SimpleNamespace(
        source=SimpleNamespace(user_id=USER, session_id=SESSION)
    )
    projected = ShadowAwarenessRuntime._project_general_situation(
        runtime,
        {},
        event,
    )
    expect(
        projected.get("status") == "degraded"
        and projected.get("suggestion_status") == "fail_closed"
        and projected.get("reason") == "suggestion_outbox_state_corrupt"
        and projected.get("route_change_allowed") is False,
        "event pipeline propagates SuggestionOutbox fail-closed as degraded",
        projected,
    )


def main() -> int:
    with TemporaryDirectory(prefix="veyra-suggestion-sandbox-") as tmp:
        store = WorldStateStore(Path(tmp) / "state")
        clock = MutableClock(NOW)
        outbox = SuggestionOutbox(store, clock=clock)
        attention = AttentionHypothesisRuntime(store, clock=clock)
        learning = LearningCalibrationRuntime(
            state_store=store,
            clock=clock,
        )
        config_revision = int(
            store.read_json("ops_config.json").get("_state_revision") or 0
        )
        outbox.configure_mode(
            "advise_only",
            expected_state_revision=config_revision,
        )

        parent, assessment = proposal_inputs(store, attention, clock)
        not_opted_in = outbox.consider(
            parent,
            assessment,
            user_id=USER,
            session_id=SESSION,
        )
        expect(
            not_opted_in.get("status") == "suppressed"
            and not_opted_in.get("reason")
            == "suggestion_sandbox_not_enabled"
            and not_opted_in.get("decision_disposition") == "say"
            and not_opted_in.get("delivery_disposition") == "suppressed"
            and outbox.list_inbox(user_id=USER, session_id=SESSION)["count"]
            == 0,
            "advise_only cannot surface without exact-scope sandbox opt-in",
            not_opted_in,
        )

        outbox.configure_policy(
            user_id=USER,
            session_id=SESSION,
            sandbox_enabled=True,
            daily_budget=1,
            quiet_hours=None,
            cooldown_seconds=3600,
            dismiss_cooldown_seconds=86400,
            expected_state_revision=int(
                store.read_json("suggestion_outbox.json").get(
                    "_state_revision"
                )
                or 0
            ),
        )
        forged_surface = copy.deepcopy(assessment)
        forged_surface["score"] = 0.999999
        forged_surface["attention_readiness"]["value"] = 0.999999
        forged_surface["components"]["freshness"]["value"] = 0.0
        forged_surface["components"]["freshness"]["weighted_score"] = 0.0
        forged = outbox.consider(
            parent,
            forged_surface,
            user_id=USER,
            session_id=SESSION,
        )
        expect(
            forged.get("status") == "fail_closed"
            and forged.get("reason")
            == "attention_hypothesis_surface_not_current"
            and store.read_json(SuggestionOutbox.STATE_FILE).get(
                "proposal_count"
            )
            == 0,
            "forged score and freshness cannot enter the suggestion ledger",
            forged,
        )
        stale_wait_surface = copy.deepcopy(assessment)
        stale_wait_surface["eligible"] = False
        stale_wait_surface["status"] = "not_eligible"
        stale_wait_surface["hypothesis_status"] = "candidate"
        stale_ledger_path = store.path_for(SuggestionOutbox.STATE_FILE)
        stale_ledger_before = stale_ledger_path.read_bytes()
        stale_decision_count_before = int(
            store.read_json(SuggestionOutbox.STATE_FILE).get(
                "interaction_decision_count"
            )
            or 0
        )
        stale_wait = outbox.consider(
            parent,
            stale_wait_surface,
            user_id=USER,
            session_id=SESSION,
        )
        expect(
            stale_wait.get("status") == "fail_closed"
            and stale_wait.get("reason") == "attention_hypothesis_binding_not_current"
            and stale_wait.get("decision_disposition") == "wait"
            and stale_wait.get("delivery_disposition") == "suppressed"
            and stale_wait.get("interaction_decision") is None
            and store.read_json(SuggestionOutbox.STATE_FILE).get("proposal_count") == 0
            and store.read_json(SuggestionOutbox.STATE_FILE).get(
                "interaction_decision_count", 0
            )
            == stale_decision_count_before
            and stale_ledger_before == stale_ledger_path.read_bytes(),
            "non-say decisions reject a stale Attention surface without ledger mutation",
            stale_wait,
        )
        surfaced = outbox.consider(
            parent,
            assessment,
            user_id=USER,
            session_id=SESSION,
        )
        proposal = surfaced.get("proposal") or {}
        raw_proposal = store.read_json("suggestion_outbox.json")[
            "proposals"
        ][proposal["proposal_id"]]
        changed_hypothesis_revision = copy.deepcopy(raw_proposal)
        changed_hypothesis_revision["attention_hypothesis_ref"][
            "hypothesis_revision"
        ] += 1
        expect(
            surfaced.get("status") == "pending"
            and proposal.get("decision_disposition") == "say"
            and proposal.get("delivery_disposition") == "owner_scoped_console"
            and proposal.get("proposal_revision")
            == SuggestionOutbox.proposal_revision_for(raw_proposal)
            and proposal.get("attention_hypothesis_ref")
            == assessment["attention_hypothesis_ref"]
            and proposal.get("proposal_revision")
            != SuggestionOutbox.proposal_revision_for(
                changed_hypothesis_revision
            )
            and outbox.list_inbox(user_id=USER, session_id=SESSION)["count"]
            == 1
            and outbox.list_inbox(
                user_id=USER,
                session_id="foreign-session",
            )["count"]
            == 0,
            "surface is exact-scope and carries a stable proposal revision",
            surfaced,
        )
        before_replay_count = int(
            store.read_json(SuggestionOutbox.STATE_FILE).get(
                "proposal_count"
            )
            or 0
        )
        clock.advance(seconds=1)
        replayed = outbox.consider(
            parent,
            assessment,
            user_id=USER,
            session_id=SESSION,
        )
        replayed_inbox = outbox.list_inbox(
            user_id=USER,
            session_id=SESSION,
        )
        expect(
            replayed.get("status") == "replayed"
            and replayed_inbox["count"] == 1
            and int(
                store.read_json(SuggestionOutbox.STATE_FILE).get(
                    "proposal_count"
                )
                or 0
            )
            == before_replay_count
            and len(
                {
                    item.get("proposal_id")
                    for item in replayed_inbox.get("items", [])
                }
            )
            == 1,
            "t+1 proposal replay keeps one stable owner inbox entry",
            replayed_inbox,
        )
        current_policy_revision = int(
            store.read_json(SuggestionOutbox.STATE_FILE).get(
                "_state_revision"
            )
            or 0
        )
        expect_raises(
            SuggestionOutboxConflict,
            "owner budget timezone is frozen after its first daily use",
            lambda: outbox.configure_policy(
                user_id=USER,
                session_id=SESSION,
                sandbox_enabled=True,
                daily_budget=1,
                quiet_hours=None,
                timezone="America/Los_Angeles",
                cooldown_seconds=3600,
                dismiss_cooldown_seconds=86400,
                expected_state_revision=current_policy_revision,
            ),
        )
        expect_raises(
            SuggestionOutboxConflict,
            "one owner cannot use different daily-budget timezones across sessions",
            lambda: outbox.configure_policy(
                user_id=USER,
                session_id="timezone-bypass-session",
                sandbox_enabled=True,
                daily_budget=1,
                quiet_hours=None,
                timezone="America/Los_Angeles",
                cooldown_seconds=3600,
                dismiss_cooldown_seconds=86400,
                expected_state_revision=current_policy_revision,
            ),
        )

        outbox_revision = int(
            store.read_json("suggestion_outbox.json").get("_state_revision")
            or 0
        )
        first_kwargs = feedback_kwargs(
            proposal,
            feedback_id="suggestion-feedback-1",
            label="useful",
            outbox_revision=outbox_revision,
        )
        first = learning.record_suggestion_feedback(**first_kwargs)
        before_duplicate = copy.deepcopy(
            store.read_json("learning_calibration_state.json")
        )
        duplicate = learning.record_suggestion_feedback(**first_kwargs)
        expect(
            first["status"] == "recorded"
            and duplicate["status"] == "duplicate"
            and before_duplicate
            == store.read_json("learning_calibration_state.json"),
            "exact suggestion feedback replay is idempotent",
            duplicate,
        )
        expect_raises(
            LearningCalibrationConflict,
            "a changed label requires the exact active correction target",
            lambda: learning.record_suggestion_feedback(
                **feedback_kwargs(
                    proposal,
                    feedback_id="suggestion-feedback-2",
                    label="not_useful",
                    outbox_revision=outbox_revision,
                )
            ),
        )
        corrected = learning.record_suggestion_feedback(
            **feedback_kwargs(
                proposal,
                feedback_id="suggestion-feedback-2",
                label="not_useful",
                outbox_revision=outbox_revision,
                supersedes_learning_id=first["record"]["learning_id"],
            )
        )
        expect(
            corrected["status"] == "corrected"
            and corrected["record"]["session_id"] == SESSION,
            "correction remains bound to exact owner, session, and proposal revision",
            corrected,
        )
        expect_raises(
            LearningCalibrationConflict,
            "feedback cannot cross session identity",
            lambda: learning.record_suggestion_feedback(
                **{
                    **feedback_kwargs(
                        proposal,
                        feedback_id="suggestion-feedback-cross-session",
                        label="wrong_timing",
                        outbox_revision=outbox_revision,
                    ),
                    "session_id": "foreign-session",
                }
            ),
        )
        expect_raises(
            LearningCalibrationConflict,
            "feedback cannot bind a forged proposal revision",
            lambda: learning.record_suggestion_feedback(
                **{
                    **feedback_kwargs(
                        proposal,
                        feedback_id="suggestion-feedback-forged",
                        label="wrong_evidence",
                        outbox_revision=outbox_revision,
                    ),
                    "proposal_revision": "sugr_" + "0" * 24,
                }
            ),
        )

        outbox.configure_policy(
            user_id=USER,
            session_id=SESSION,
            sandbox_enabled=True,
            daily_budget=1,
            quiet_hours=None,
            cooldown_seconds=3600,
            dismiss_cooldown_seconds=86400,
            expected_state_revision=int(
                store.read_json("suggestion_outbox.json").get(
                    "_state_revision"
                )
                or 0
            ),
        )
        for index, diagnostic_label in enumerate(
            ("too_frequent", "wrong_timing", "wrong_evidence"),
            start=3,
        ):
            clock.advance(days=1)
            diagnostic_parent, diagnostic_assessment = proposal_inputs(
                store,
                attention,
                clock,
                index,
            )
            diagnostic_proposal = outbox.consider(
                diagnostic_parent,
                diagnostic_assessment,
                user_id=USER,
                session_id=SESSION,
            )["proposal"]
            diagnostic_result = learning.record_suggestion_feedback(
                **feedback_kwargs(
                    diagnostic_proposal,
                    feedback_id=f"suggestion-feedback-{diagnostic_label}",
                    label=diagnostic_label,
                    outbox_revision=int(
                        store.read_json("suggestion_outbox.json").get(
                            "_state_revision"
                        )
                        or 0
                    ),
                )
            )
            expect(
                diagnostic_result["status"] == "recorded",
                f"{diagnostic_label} is accepted as explicit categorical feedback",
                diagnostic_result,
            )
        current_outbox_revision = int(
            store.read_json("suggestion_outbox.json").get("_state_revision")
            or 0
        )
        expect_raises(
            LearningCalibrationConflict,
            "new feedback rejects a stale outbox CAS",
            lambda: learning.record_suggestion_feedback(
                **feedback_kwargs(
                    proposal,
                    feedback_id="suggestion-feedback-stale-cas",
                    label="useful",
                    outbox_revision=current_outbox_revision - 1,
                )
            ),
        )

        summary_before_dismissal = learning.suggestion_summary(
            user_id=USER,
            session_id=SESSION,
        )
        outbox.dismiss(
            str(proposal["proposal_id"]),
            user_id=USER,
            session_id=SESSION,
            expected_state_revision=int(
                store.read_json("suggestion_outbox.json").get(
                    "_state_revision"
                )
                or 0
            ),
            reason="console_dismiss",
        )
        summary_after_dismissal = learning.suggestion_summary(
            user_id=USER,
            session_id=SESSION,
        )
        expect(
            summary_before_dismissal == summary_after_dismissal
            and summary_after_dismissal["active_feedback_count"] == 4
            and summary_after_dismissal["counts"]["not_useful"] == 1
            and summary_after_dismissal["diagnostic_feedback_count"] == 3
            and summary_after_dismissal[
                "dismissal_inferred_as_usefulness"
            ]
            is False
            and summary_after_dismissal["accuracy"]
            == (
                "unavailable_without_falsifiable_prediction_and_verified_outcome"
            )
            and summary_after_dismissal["policy_effect"] == "none",
            "dismissal is not inferred, accuracy is unavailable, and calibration has no policy effect",
            summary_after_dismissal,
        )
        expect(
            learning.suggestion_summary(
                user_id=USER,
                session_id="foreign-session",
            )["active_feedback_count"]
            == 0,
            "calibration summary is exact-session scoped",
        )

        second_parent, second_assessment = proposal_inputs(
            store,
            attention,
            clock,
            2,
        )
        outbox.configure_mode(
            "record_only",
            expected_state_revision=int(
                store.read_json("ops_config.json").get("_state_revision") or 0
            ),
        )
        recorded = outbox.consider(
            second_parent,
            second_assessment,
            user_id=USER,
            session_id=SESSION,
        )["proposal"]
        record_only_preview = outbox.list_preview(
            user_id=USER,
            session_id=SESSION,
        )
        preview_item = (record_only_preview.get("items") or [None])[0]
        preview_authority = (
            preview_item.get("authority") if isinstance(preview_item, dict) else None
        )
        expect(
            record_only_preview.get("status") == "success"
            and record_only_preview.get("count") == 1
            and isinstance(preview_item, dict)
            and preview_item.get("proposal_id") == recorded.get("proposal_id")
            and preview_item.get("delivery_disposition") == "none"
            and preview_item.get("delivery") == {
                "channel": "none",
                "external_delivery": False,
                "feishu_delivery": False,
                "agent_delivery": False,
            }
            and isinstance(preview_authority, dict)
            and all(value is False for value in preview_authority.values()),
            "current record-only proposal is exact-owner preview with no delivery authority",
            record_only_preview,
        )
        recorded_count = int(
            store.read_json(SuggestionOutbox.STATE_FILE).get(
                "proposal_count"
            )
            or 0
        )
        clock.advance(seconds=1)
        record_only_replay = outbox.consider(
            second_parent,
            second_assessment,
            user_id=USER,
            session_id=SESSION,
        )
        expect(
            record_only_replay.get("status") == "replayed"
            and recorded.get("decision_disposition") == "say"
            and recorded.get("delivery_disposition") == "none"
            and int(
                store.read_json(SuggestionOutbox.STATE_FILE).get(
                    "proposal_count"
                )
                or 0
            )
            == recorded_count,
            "record-only t+1 replay does not create freshness identities",
            record_only_replay,
        )
        expect_raises(
            LearningCalibrationConflict,
            "record-only proposals cannot receive surfaced-user feedback",
            lambda: learning.record_suggestion_feedback(
                **feedback_kwargs(
                    recorded,
                    feedback_id="suggestion-feedback-record-only",
                    label="useful",
                    outbox_revision=int(
                        store.read_json("suggestion_outbox.json").get(
                            "_state_revision"
                        )
                        or 0
                    ),
                )
            ),
        )
        attention_id = str(
            (recorded.get("attention_hypothesis_ref") or {}).get("hypothesis_id") or ""
        )
        attention_before_expiry = copy.deepcopy(
            store.read_json(AttentionHypothesisRuntime.STATE_FILE)
        )

        def expire_record(state: dict[str, Any]) -> dict[str, Any]:
            record = (state.get("hypotheses") or {}).get(attention_id)
            if not isinstance(record, dict):
                raise AssertionError("record-only attention fixture was not persisted")
            record["expires_at"] = (clock() - timedelta(seconds=1)).isoformat()
            return state

        store.mutate_json(AttentionHypothesisRuntime.STATE_FILE, expire_record)
        expired_preview = outbox.list_preview(
            user_id=USER,
            session_id=SESSION,
        )
        expect(
            expired_preview.get("status") == "success"
            and expired_preview.get("count") == 0
            and expired_preview.get("items") == []
            and int(expired_preview.get("stale_hidden_count") or 0) >= 1,
            "expired record-only binding is hidden from Product Preview",
            expired_preview,
        )

        def restore_record(state: dict[str, Any]) -> dict[str, Any]:
            return attention_before_expiry

        store.mutate_json(AttentionHypothesisRuntime.STATE_FILE, restore_record)

        awareness = SimpleNamespace(
            event_awareness=SimpleNamespace(suggestion_outbox=outbox)
        )
        app = FastAPI()
        app.include_router(
            build_debug_audit_router(
                {
                    "awareness_loop": awareness,
                    "learning_calibration": learning,
                }
            )
        )
        client = TestClient(app)
        query = {"user_id": USER, "session_id": SESSION}
        before_get = file_digest(
            store.path_for("learning_calibration_state.json")
        )
        feedback_http = client.get(
            "/awareness/suggestions/feedback",
            params=query,
        )
        summary_http = client.get(
            "/awareness/suggestions/calibration",
            params=query,
        )
        after_get = file_digest(
            store.path_for("learning_calibration_state.json")
        )
        expect(
            feedback_http.status_code == 200
            and feedback_http.json()["count"] == 5
            and summary_http.status_code == 200
            and summary_http.json()["active_feedback_count"] == 4
            and before_get == after_get,
            "suggestion feedback and calibration GETs are scoped and byte-pure",
            {
                "feedback": feedback_http.json(),
                "summary": summary_http.json(),
            },
        )
        api_duplicate = client.post(
            f"/awareness/suggestions/{proposal['proposal_id']}/feedback",
            json={
                "schema_version": "veyra.suggestion_feedback_command.v1",
                **{
                    key: value
                    for key, value in feedback_kwargs(
                        proposal,
                        feedback_id="suggestion-feedback-2",
                        label="not_useful",
                        outbox_revision=int(
                            store.read_json("suggestion_outbox.json").get(
                                "_state_revision"
                            )
                            or 0
                        ),
                        supersedes_learning_id=first["record"]["learning_id"],
                    ).items()
                    if key != "proposal_id"
                },
            },
        )
        invalid_extra = client.post(
            f"/awareness/suggestions/{proposal['proposal_id']}/feedback",
            json={
                "schema_version": "veyra.suggestion_feedback_command.v1",
                **{
                    key: value
                    for key, value in feedback_kwargs(
                        proposal,
                        feedback_id="suggestion-feedback-extra",
                        label="too_frequent",
                        outbox_revision=int(
                            store.read_json("suggestion_outbox.json").get(
                                "_state_revision"
                            )
                            or 0
                        ),
                    ).items()
                    if key != "proposal_id"
                },
                "note": "free text must not be accepted",
            },
        )
        expect(
            api_duplicate.status_code == 200
            and api_duplicate.json()["status"] == "duplicate"
            and invalid_extra.status_code == 422,
            "HTTP feedback is idempotent and rejects free-text extras",
            {
                "duplicate": api_duplicate.json(),
                "invalid": invalid_extra.json(),
            },
        )

        persisted_text = json.dumps(
            store.read_json("learning_calibration_state.json"),
            ensure_ascii=False,
            sort_keys=True,
        )
        expect(
            '"suggestion_policy_effect": "none"' in persisted_text
            and '"note"' not in persisted_text
            and '"prompt"' not in persisted_text
            and '"raw"' not in persisted_text,
            "suggestion calibration is privacy-minimal and non-authoritative",
        )

    def corrupt_proposal_count(state: dict[str, Any]) -> None:
        state["proposal_count"] = int(state.get("proposal_count") or 0) + 1

    def corrupt_daily_counter(state: dict[str, Any]) -> None:
        counters = state.get("daily_counters")
        if not isinstance(counters, dict) or not counters:
            raise AssertionError("daily counter fixture was not persisted")
        counter = next(iter(counters.values()))
        if not isinstance(counter, dict):
            raise AssertionError("daily counter fixture is invalid")
        counter["count"] = "1"

    raw_private_sentinel = "RAW-PRIVATE-SUGGESTION-SENTINEL"

    def inject_unknown_proposal_field(state: dict[str, Any]) -> None:
        proposals = state.get("proposals")
        if not isinstance(proposals, dict) or not proposals:
            raise AssertionError("proposal fixture was not persisted")
        proposal = next(iter(proposals.values()))
        if not isinstance(proposal, dict):
            raise AssertionError("proposal fixture is invalid")
        proposal["raw_user_text"] = raw_private_sentinel

    corruption_freeze_case(
        label="corrupt proposal_count",
        corrupt=corrupt_proposal_count,
    )
    corruption_freeze_case(
        label="corrupt daily counter",
        corrupt=corrupt_daily_counter,
    )
    corruption_freeze_case(
        label="unknown v2 proposal field",
        corrupt=inject_unknown_proposal_field,
        forbidden_value=raw_private_sentinel,
    )
    legacy_proposal_isolation_case()
    real_clock_currentness_case()
    interaction_disposition_case()
    pipeline_fail_closed_projection_case()

    print("suggestion sandbox smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
