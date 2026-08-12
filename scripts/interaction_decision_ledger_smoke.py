#!/usr/bin/env python3
from __future__ import annotations

import copy
import hashlib
from datetime import datetime, timezone
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.context_scope import tenant_scope_storage_key  # noqa: E402
from core.world_state import WorldStateStore  # noqa: E402
from runtime.attention_hypothesis_runtime import (  # noqa: E402
    AttentionHypothesisRuntime,
)
from runtime.suggestion_outbox import SuggestionOutbox  # noqa: E402
from scripts.attention_hypothesis_smoke import (  # noqa: E402
    CONFIRMING_VALUES,
    LOW_VALUES,
    MutableClock,
    assessment_for,
    parent_for,
    persist_parent,
)


USER = "ledger-owner"
SESSION = "ledger-session"
NOW = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"PASS {label}")


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.name.endswith(".lock"):
            digest.update(str(path.relative_to(root)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def current_surface(
    store: WorldStateStore,
    attention: AttentionHypothesisRuntime,
    clock: MutableClock,
    *,
    user_id: str,
    session_id: str,
    general_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Create a persisted parent and return its current candidate surface."""

    parent = parent_for(
        user_id=user_id,
        session_id=session_id,
        revision=1,
        child_count=2,
        clock=clock,
        general_id=general_id,
        anchor=f"goal:{general_id}",
    )
    persist_parent(store, parent)
    assessment = assessment_for(
        parent,
        values=LOW_VALUES,
        store=store,
        clock=clock,
    )
    observed = attention.observe(parent, assessment)
    expect(
        observed.get("status") == "candidate"
        and isinstance(observed.get("surface_assessment"), dict)
        and isinstance(
            observed.get("surface_assessment", {}).get(
                "attention_hypothesis_ref"
            ),
            dict,
        ),
        "fixture persists a current candidate AttentionHypothesis",
        observed,
    )
    return parent, copy.deepcopy(observed["surface_assessment"])


def main() -> int:
    with TemporaryDirectory(prefix="veyra-interaction-ledger-") as raw:
        root = Path(raw) / "state"
        store = WorldStateStore(root)
        clock = MutableClock(NOW)
        outbox = SuggestionOutbox(store, clock=clock)
        attention = AttentionHypothesisRuntime(store, clock=clock)
        parent, candidate_surface = current_surface(
            store,
            attention,
            clock,
            user_id=USER,
            session_id=SESSION,
            general_id="gsit-interaction-ledger",
        )
        wait_surface = copy.deepcopy(candidate_surface)
        ask_surface = copy.deepcopy(candidate_surface)
        ask_surface["interaction_gap"] = {
            "kind": "owner_question",
            "gap_id": "gap_ledger_missing_input",
            "answerable": True,
        }
        # A current, non-eligible surface without a lifecycle status is the
        # typed silent fallback.  Its parent/ref/evidence remain untouched and
        # therefore still prove the exact persisted owner/session binding.
        silent_surface = copy.deepcopy(candidate_surface)
        silent_surface["hypothesis_status"] = candidate_surface["hypothesis_status"]
        silent_surface["status"] = "awaiting_evidence"
        silent_surface["eligible"] = False
        wait = outbox.consider(
            parent,
            wait_surface,
            user_id=USER,
            session_id=SESSION,
        )
        ask = outbox.consider(
            parent,
            ask_surface,
            user_id=USER,
            session_id=SESSION,
        )
        silent = outbox.consider(
            parent,
            silent_surface,
            user_id=USER,
            session_id=SESSION,
        )
        expect(
            wait.get("status") == "not_proposed"
            and ask.get("status") == "not_proposed"
            and silent.get("status") == "not_proposed"
            and wait.get("decision_disposition") == "wait"
            # Caller-provided interaction_gap is intentionally dormant until
            # a trusted owner-question producer exists; it must not create
            # an ask decision.
            and ask.get("decision_disposition") == "wait"
            and silent.get("decision_disposition") == "wait"
            and all(
                item.get("delivery_disposition") == "none"
                for item in (wait, ask, silent)
            )
            and all(
                isinstance(item.get("interaction_decision"), dict)
                for item in (wait, ask, silent)
            ),
            "wait/ask-dormant/silent persist only with a current AttentionHypothesis binding",
            {"wait": wait, "ask": ask, "silent": silent},
        )
        state = store.read_json(SuggestionOutbox.STATE_FILE)
        decisions = state.get("interaction_decisions") or {}
        refs = {
            tuple(
                sorted(
                    (str(key), str(value))
                    for key, value in (
                        item.get("attention_hypothesis_ref") or {}
                    ).items()
                )
            )
            for item in decisions.values()
        }
        hypothesis_ref = candidate_surface["attention_hypothesis_ref"]
        parent_state = store.read_json("general_situation_state.json")
        hypothesis_state = store.read_json("attention_hypothesis_state.json")
        persisted_hypothesis = hypothesis_state.get("hypotheses", {}).get(
            hypothesis_ref["hypothesis_id"]
        )
        expect(
            state.get("interaction_decision_count") == 1
            and len(decisions) == 1
            and len(refs) == 1
            and all(
                item.get("attention_hypothesis_ref") == hypothesis_ref
                for item in decisions.values()
            )
            and parent_state.get("general_situations", {})
            .get(parent["general_situation_id"], {})
            .get("parent_revision")
            == parent["parent_revision"]
            and parent_state.get("general_situations", {})
            .get(parent["general_situation_id"], {})
            .get("user_id")
            == USER
            and tenant_scope_storage_key(USER, SESSION)
            in set(
                parent_state.get("general_situations", {})
                .get(parent["general_situation_id"], {})
                .get("session_scope_keys")
                or []
            )
            and isinstance(persisted_hypothesis, dict)
            and persisted_hypothesis.get("user_id") == USER
            and tenant_scope_storage_key(USER, SESSION)
            in set(persisted_hypothesis.get("session_scope_keys") or [])
            and persisted_hypothesis.get("general_situation_id")
            == parent["general_situation_id"]
            and AttentionHypothesisRuntime._parent_binding(
                parent_state.get("general_situations", {})
                .get(parent["general_situation_id"], {})
            )
            == persisted_hypothesis.get("parent_binding")
            and all(
                item.get("schema_version") == SuggestionOutbox.DECISION_SCHEMA_VERSION
                and item.get("user_id") == USER
                and item.get("session_id") == SESSION
                and item.get("proposal_id") is None
                and item.get("delivery_disposition") == "none"
                and not any(item.get("authority", {}).values())
                for item in decisions.values()
            ),
            "decision ledger is exact-owner, parent-derived, and non-authorizing",
            decisions,
        )

        # Missing, stale, and cross-scope references are rejected before the
        # writer can append an unreviewable row.  Foreign fixtures are real
        # persisted hypotheses; only their references are forged onto the
        # current owner's assessment.
        _, foreign_owner_surface = current_surface(
            store,
            attention,
            clock,
            user_id="foreign-owner",
            session_id=SESSION,
            general_id="gsit-interaction-ledger-foreign-owner",
        )
        _, foreign_session_surface = current_surface(
            store,
            attention,
            clock,
            user_id=USER,
            session_id="foreign-session",
            general_id="gsit-interaction-ledger-foreign-session",
        )
        missing_surface = copy.deepcopy(candidate_surface)
        missing_surface.pop("attention_hypothesis_ref", None)
        stale_surface = copy.deepcopy(candidate_surface)
        stale_surface["attention_hypothesis_ref"]["hypothesis_revision"] += 1
        forged_owner_surface = copy.deepcopy(candidate_surface)
        forged_owner_surface["attention_hypothesis_ref"] = copy.deepcopy(
            foreign_owner_surface["attention_hypothesis_ref"]
        )
        forged_session_surface = copy.deepcopy(candidate_surface)
        forged_session_surface["attention_hypothesis_ref"] = copy.deepcopy(
            foreign_session_surface["attention_hypothesis_ref"]
        )
        state_path = store.path_for(SuggestionOutbox.STATE_FILE)
        rejection_cases = (
            (
                "missing AttentionHypothesis reference",
                missing_surface,
                "current_attention_hypothesis_required",
            ),
            (
                "stale AttentionHypothesis revision",
                stale_surface,
                "attention_hypothesis_binding_not_current",
            ),
            (
                "cross-owner AttentionHypothesis reference",
                forged_owner_surface,
                "attention_hypothesis_binding_not_current",
            ),
            (
                "cross-session AttentionHypothesis reference",
                forged_session_surface,
                "attention_hypothesis_binding_not_current",
            ),
        )
        for label, forged_surface, reason in rejection_cases:
            before = state_path.read_bytes()
            before_count = int(
                store.read_json(SuggestionOutbox.STATE_FILE).get(
                    "interaction_decision_count"
                )
                or 0
            )
            rejected = outbox.consider(
                parent,
                forged_surface,
                user_id=USER,
                session_id=SESSION,
            )
            after = state_path.read_bytes()
            expect(
                rejected.get("status") == "fail_closed"
                and rejected.get("reason") == reason
                and rejected.get("interaction_decision") is None
                and before == after
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

        # A mode/epoch flip is a commit-time suppression, not permission to
        # persist a stale or missing Attention binding.  Exercise both the
        # non-say ledger path and the say/proposal writer path with a synthetic
        # two-snapshot mode race.
        original_mode_snapshot = outbox._mode_snapshot  # noqa: SLF001

        def race_result(
            selected_parent: dict[str, Any],
            selected_surface: dict[str, Any],
        ) -> tuple[dict[str, Any], bool]:
            snapshots = iter(
                (
                    {"status": "success", "mode": "record_only", "mode_epoch": 0},
                    {"status": "success", "mode": "shadow", "mode_epoch": 1},
                )
            )
            outbox._mode_snapshot = lambda: next(snapshots)  # type: ignore[method-assign]  # noqa: SLF001
            before = state_path.read_bytes()
            try:
                selected = outbox.consider(
                    selected_parent,
                    selected_surface,
                    user_id=USER,
                    session_id=SESSION,
                )
            finally:
                outbox._mode_snapshot = original_mode_snapshot  # type: ignore[method-assign]  # noqa: SLF001
            return selected, before == state_path.read_bytes()

        race_stale = copy.deepcopy(candidate_surface)
        race_stale["attention_hypothesis_ref"]["hypothesis_revision"] += 100
        stale_race_result, stale_race_pure = race_result(parent, race_stale)
        race_missing = copy.deepcopy(candidate_surface)
        race_missing.pop("attention_hypothesis_ref", None)
        missing_race_result, missing_race_pure = race_result(parent, race_missing)

        confirmed_parent = parent_for(
            user_id=USER,
            session_id=SESSION,
            revision=1,
            child_count=2,
            clock=clock,
            general_id="gsit-interaction-ledger-mode-race-say",
            anchor="goal:interaction-ledger-mode-race-say",
        )
        persist_parent(store, confirmed_parent)
        confirmed = attention.observe(
            confirmed_parent,
            assessment_for(
                confirmed_parent,
                values=CONFIRMING_VALUES,
                store=store,
                clock=clock,
            ),
        )
        confirmed_surface = copy.deepcopy(confirmed.get("surface_assessment"))
        if not isinstance(confirmed_surface, dict):
            raise AssertionError(f"confirmed race fixture missing surface: {confirmed!r}")
        confirmed_surface["attention_hypothesis_ref"][
            "hypothesis_revision"
        ] += 100
        say_race_result, say_race_pure = race_result(
            confirmed_parent,
            confirmed_surface,
        )
        expect(
            stale_race_result.get("status") == "fail_closed"
            and stale_race_result.get("reason")
            == "attention_hypothesis_binding_not_current"
            and stale_race_result.get("interaction_decision") is None
            and stale_race_pure
            and missing_race_result.get("status") == "fail_closed"
            and missing_race_result.get("reason")
            == "current_attention_hypothesis_required"
            and missing_race_result.get("interaction_decision") is None
            and missing_race_pure
            and say_race_result.get("status") == "fail_closed"
            and say_race_result.get("reason")
            == "attention_hypothesis_binding_not_current"
            and say_race_pure,
            "mode races cannot persist stale or missing Attention bindings",
            {
                "stale_wait": stale_race_result,
                "missing_wait": missing_race_result,
                "stale_say": say_race_result,
            },
        )

        before_get = tree_digest(root)
        status = outbox.status()
        status_again = outbox.status()
        inbox = outbox.list_inbox(user_id=USER, session_id=SESSION)
        owner_decisions = outbox.list_decisions(
            user_id=USER,
            session_id=SESSION,
        )
        foreign_decisions = outbox.list_decisions(
            user_id=USER,
            session_id="foreign-session",
        )
        after_get = tree_digest(root)
        expect(
            status["interaction_decision_count"] == 1
            and status == status_again
            and inbox["count"] == 0
            and owner_decisions.get("count") == 1
            and foreign_decisions.get("count") == 0
            and before_get == after_get,
            "status, owner ledger, and inbox GETs remain byte-pure",
            {
                "status": status,
                "owner_decisions": owner_decisions,
                "inbox": inbox,
            },
        )

        def tamper_decision(state: dict[str, Any]) -> dict[str, Any]:
            records = state.get("interaction_decisions")
            if not isinstance(records, dict) or not records:
                raise AssertionError("decision fixture was not persisted")
            first_id = next(iter(records))
            records[first_id]["reason"] = "tampered-ledger-reason"
            return state

        store.mutate_json(SuggestionOutbox.STATE_FILE, tamper_decision)
        tampered_state = store.read_json(SuggestionOutbox.STATE_FILE)
        tampered_id, tampered_record = next(
            iter((tampered_state.get("interaction_decisions") or {}).items())
        )
        tamper_before_reads = state_path.read_bytes()
        tampered_status = outbox.status()
        tampered_list = outbox.list_decisions(
            user_id=USER,
            session_id=SESSION,
        )
        tampered_consider = outbox.consider(
            parent,
            candidate_surface,
            user_id=USER,
            session_id=SESSION,
        )
        expect(
            SuggestionOutbox.decision_id_for(tampered_record) != tampered_id
            and tampered_status.get("status") == "fail_closed"
            and tampered_status.get("reason") == "suggestion_outbox_state_corrupt"
            and tampered_list.get("status") == "fail_closed"
            and tampered_consider.get("status") == "fail_closed"
            and tamper_before_reads == state_path.read_bytes(),
            "canonical decision identity recomputation detects ledger tampering",
            {
                "stored_id": tampered_id,
                "recomputed_id": SuggestionOutbox.decision_id_for(tampered_record),
                "status": tampered_status,
                "list": tampered_list,
                "consider": tampered_consider,
            },
        )
        print(
            "Interaction decision ledger smoke passed: "
            "3 positive / 4 fail-closed / tamper"
        )

    with TemporaryDirectory(prefix="veyra-interaction-terminal-") as raw:
        root = Path(raw) / "state"
        store = WorldStateStore(root)
        clock = MutableClock(NOW)
        outbox = SuggestionOutbox(store, clock=clock)
        attention = AttentionHypothesisRuntime(store, clock=clock)
        parent = parent_for(
            user_id=USER,
            session_id=SESSION,
            revision=1,
            child_count=2,
            clock=clock,
            general_id="gsit-interaction-terminal",
            anchor="goal:interaction-terminal",
        )
        persist_parent(store, parent)
        assessment = assessment_for(
            parent,
            values=CONFIRMING_VALUES,
            store=store,
            clock=clock,
        )
        confirmed = attention.observe(parent, assessment)
        confirmed_hypothesis = confirmed["hypothesis"]
        terminal = attention.observe(
            parent,
            assessment,
            lifecycle_signal={
                "schema_version": AttentionHypothesisRuntime.LIFECYCLE_SIGNAL_SCHEMA_VERSION,
                "signal_id": "als_interaction_terminal",
                "kind": "contradiction",
                "target_hypothesis_id": confirmed_hypothesis["hypothesis_id"],
                "target_hypothesis_revision": confirmed_hypothesis[
                    "hypothesis_revision"
                ],
                "evidence_id": "evidence:interaction_terminal",
                "reason_code": "direct_counter_observation",
                "producer_id": "local_operator",
                "observed_at": clock().isoformat(),
            },
        )
        terminal_surface = copy.deepcopy(terminal["surface_assessment"])
        canonical = outbox.consider(
            parent,
            terminal_surface,
            user_id=USER,
            session_id=SESSION,
        )
        forged = copy.deepcopy(terminal_surface)
        forged["assessment_binding"] = {
            **forged.get("assessment_binding", {}),
            "assessed_at": "2026-08-03T11:59:59+00:00",
        }
        forged["evidence"] = []
        forged["components"] = {}
        forged["unknowns"] = ["caller-forged-terminal-surface"]
        state_path = store.path_for(SuggestionOutbox.STATE_FILE)
        before_forged = state_path.read_bytes()
        forged_result = outbox.consider(
            parent,
            forged,
            user_id=USER,
            session_id=SESSION,
        )
        expect(
            terminal.get("status") == "contradicted"
            and canonical.get("status") == "not_proposed"
            and canonical.get("decision_disposition") == "silent"
            and isinstance(canonical.get("interaction_decision"), dict)
            and forged_result.get("status") == "fail_closed"
            and forged_result.get("reason")
            == "attention_hypothesis_surface_not_current"
            and forged_result.get("interaction_decision") is None
            and before_forged == state_path.read_bytes(),
            "terminal silent decisions require the canonical durable surface",
            {"canonical": canonical, "forged": forged_result},
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
