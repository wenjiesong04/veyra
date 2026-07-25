#!/usr/bin/env python3
from __future__ import annotations

import copy
import json
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from itertools import combinations
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]

import sys

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from awareness.project_guardian import ProjectGuardianEvaluator
from core.agency_core import AgencyCore
from core.definitions import RiskLevel
from core.world_state import STATE_FILE_LAYOUT, WorldStateStore
from interface.event_normalizer import EventNormalizer
from interface.event_schema import (
    EventSource,
    EventType,
    LoopResult,
    Route,
    VeyraEvent,
)
from routers.debug_audit import _public_state, build_debug_audit_router
from runtime.active_loop import ActiveRuntimeLoop
from runtime.event_awareness_runtime import ShadowAwarenessRuntime
from runtime.event_inbox import EventInbox
from runtime.project_guardian import ProjectGuardianRuntime
from runtime.project_guardian_signal_ledger import ProjectGuardianSignalLedger
from scripts.event_driven_awareness_smoke import (
    OFFLINE_ROUTE_CASES,
    build_offline_route_loop,
    offline_public_outputs_equivalent,
)


NOW = datetime.now(timezone.utc).replace(microsecond=0)
SCOPE = {
    "workspace_id": "ws_veyra_01",
    "repo_id": "wenjiesong04/veyra",
    "target_ref": "refs/heads/main",
    "target_environment": "production",
    "release_cycle": "release_2026_07_26",
}
COMPONENTS = ProjectGuardianEvaluator.SIGNAL_COMPONENTS


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"PASS {label}")


def goal(
    *,
    goal_id: str = "goal_release_1",
    user_id: str = "user-a",
    status: str = "active",
    kind: str = "project_release",
    scope: dict[str, str] | None = None,
    revision: str = "7",
    active_from: datetime | None = None,
    active_until: datetime | None = None,
) -> dict[str, Any]:
    return {
        "goal_id": goal_id,
        "kind": kind,
        "status": status,
        "user_id": user_id,
        "revision": revision,
        "scope": copy.deepcopy(scope or SCOPE),
        "active_from": (active_from or NOW - timedelta(hours=1)).isoformat(),
        "active_until": (active_until or NOW + timedelta(hours=1)).isoformat(),
    }


def seed_goal(store: WorldStateStore, value: dict[str, Any] | None = None) -> None:
    store.write_json(
        "user_goals.json",
        {"goals": [copy.deepcopy(value or goal())], "updated_at": NOW.isoformat()},
    )


def configure_mode(store: WorldStateStore, mode: str) -> None:
    def update(config: dict[str, Any]) -> None:
        current = (
            config.get("project_guardian")
            if isinstance(config.get("project_guardian"), dict)
            else {}
        )
        previous = str(current.get("mode") or "disabled")
        try:
            previous_epoch = max(0, int(current.get("mode_epoch") or 0))
        except (TypeError, ValueError):
            previous_epoch = 0
        config["project_guardian"] = {
            **copy.deepcopy(current),
            "mode": mode,
            "mode_epoch": previous_epoch + int(previous != mode),
            "allowed_modes": sorted(ProjectGuardianRuntime.MODES),
        }

    store.mutate_json("ops_config.json", update)


def enqueue_signal(
    store: WorldStateStore,
    *,
    kind: str,
    event_id: str,
    state: str = "present",
    user_id: str = "user-a",
    session_id: str = "session-a",
    goal_id: str = "goal_release_1",
    scope: dict[str, str] | None = None,
    occurred_at: datetime | None = None,
    valid_until: datetime | None = None,
    source_component: str | None = None,
    provenance_root: str | None = None,
    evidence_id: str | None = None,
    evidence_ref_id: str | None = None,
    evidence_source: str | None = None,
    evidence_is_fact: bool = True,
    goal_revision: str = "7",
    source_channel: str = ProjectGuardianEvaluator.SIGNAL_CHANNEL,
    privacy_scope: str = "user",
    received_at: datetime | None = None,
    include_evidence: bool = True,
    enqueue: bool = True,
) -> VeyraEvent:
    observed = occurred_at or NOW - timedelta(minutes=5)
    evidence = evidence_id or f"evidence:{event_id}"
    event = VeyraEvent(
        type=EventType.OBSERVATION,
        source=EventSource(
            channel=source_channel,
            user_id=user_id,
            session_id=session_id,
        ),
        payload={
            "schema_version": ProjectGuardianEvaluator.SIGNAL_SCHEMA,
            "project_guardian_signal": {
                "kind": kind,
                "state": state,
                "source_component": source_component or COMPONENTS[kind],
                "provenance_root": provenance_root or f"{COMPONENTS[kind]}:{event_id}",
                "evidence_id": evidence,
                "goal_id": goal_id,
                "goal_revision": goal_revision,
                "scope": copy.deepcopy(scope or SCOPE),
                "valid_until": (
                    valid_until or NOW + timedelta(minutes=10)
                ).isoformat(),
                # Deliberately ignored by the evaluator/runtime.
                "details": {
                    "short_status": [" M /private/repo/secret.py"],
                    "raw_log": "must-not-enter-guardian-candidate",
                    "user_text": "deploy this now",
                },
            },
        },
        event_id=event_id,
        timestamp=observed.isoformat(),
        occurred_at=observed.isoformat(),
        received_at=(received_at or observed).isoformat(),
        evidence_refs=(
            [
                {
                    "ref_id": evidence_ref_id or evidence,
                    "source": evidence_source or source_component or COMPONENTS[kind],
                    "is_fact": evidence_is_fact,
                }
            ]
            if include_evidence
            else []
        ),
        privacy_scope=privacy_scope,
    )
    if enqueue:
        admission = EventInbox(store).enqueue(event)
        envelope = (
            admission.get("envelope")
            if isinstance(admission.get("envelope"), dict)
            else {}
        )
        ProjectGuardianSignalLedger(store).record_envelope(envelope)
    return event


def evaluate(store: WorldStateStore) -> dict[str, Any]:
    return ProjectGuardianEvaluator().evaluate(
        goals_state=store.read_json("user_goals.json"),
        event_inbox_state=ProjectGuardianSignalLedger(store).evaluation_state(),
        now=NOW,
    )


def guardian_projection_envelopes(
    store: WorldStateStore,
) -> list[dict[str, Any]]:
    inbox = store.read_json("event_inbox.json")
    return [
        copy.deepcopy(record["envelope"])
        for record in (inbox.get("events") or {}).values()
        if isinstance(record, dict)
        and isinstance(record.get("envelope"), dict)
        and str(
            (record["envelope"].get("source") or {}).get("channel") or ""
        )
        == "project_guardian"
    ]


def populated_store(path: Path) -> WorldStateStore:
    store = WorldStateStore(path)
    seed_goal(store)
    enqueue_signal(
        store,
        kind="git_dirty",
        event_id="evt_git",
        occurred_at=NOW - timedelta(minutes=8),
    )
    enqueue_signal(
        store,
        kind="ci_failed",
        event_id="evt_ci",
        session_id="runtime-ci",
        occurred_at=NOW - timedelta(minutes=3),
    )
    return store


def test_qualification_matrix(root: Path) -> None:
    positive_pairs = (
        ("git_dirty", "ci_failed"),
        ("git_dirty", "deployment_intent"),
        ("ci_failed", "deployment_intent"),
    )
    candidate_ids: set[str] = set()
    for index, pair in enumerate(positive_pairs):
        store = WorldStateStore(root / f"positive-{index}")
        seed_goal(store)
        for signal_index, kind in enumerate(pair):
            enqueue_signal(
                store,
                kind=kind,
                event_id=f"evt_{index}_{signal_index}",
                session_id=f"session-{signal_index}",
                occurred_at=NOW - timedelta(minutes=5 - signal_index),
            )
        result = evaluate(store)
        expect(
            result["candidate_count"] == 1,
            f"active release goal plus {pair[0]} and {pair[1]} qualifies once",
            result,
        )
        candidate = result["candidates"][0]
        candidate_ids.add(candidate["candidate_id"])
        expect(
            candidate["signal_kinds"] == sorted(pair)
            and candidate["shadow_only"] is True
            and candidate["notification_allowed"] is False
            and candidate["execution_allowed"] is False
            and candidate["agent_invoked"] is False,
            "candidate is deterministic, shadow-only, and authority-free",
            candidate,
        )
        expect(
            candidate["why_now"]
            and candidate["evidence_refs"]
            and candidate["unknowns"]
            and candidate["candidate_advice"],
            "candidate records why-now, evidence, unknowns, and advice",
            candidate,
        )
        expect(
            not any(ref.get("is_fact") for ref in candidate["evidence_refs"]),
            "signal references cannot self-certify as facts",
            candidate["evidence_refs"],
        )
    expect(
        len(candidate_ids) == 1,
        "candidate identity depends on release Goal scope, not signal order or pair",
        candidate_ids,
    )


def test_negative_and_correlation_cases(root: Path) -> None:
    no_goal = WorldStateStore(root / "no-goal")
    enqueue_signal(no_goal, kind="git_dirty", event_id="evt_ng_git")
    enqueue_signal(no_goal, kind="ci_failed", event_id="evt_ng_ci")
    expect(evaluate(no_goal)["candidate_count"] == 0, "missing Goal never qualifies")

    inactive = WorldStateStore(root / "inactive")
    seed_goal(inactive, goal(status="paused"))
    enqueue_signal(inactive, kind="git_dirty", event_id="evt_i_git")
    enqueue_signal(inactive, kind="ci_failed", event_id="evt_i_ci")
    expect(evaluate(inactive)["candidate_count"] == 0, "inactive Goal never qualifies")

    single = WorldStateStore(root / "single")
    seed_goal(single)
    enqueue_signal(single, kind="git_dirty", event_id="evt_single")
    expect(evaluate(single)["candidate_count"] == 0, "one signal class never qualifies")

    repeated = WorldStateStore(root / "repeated")
    seed_goal(repeated)
    enqueue_signal(
        repeated,
        kind="git_dirty",
        event_id="evt_repeat_a",
        provenance_root="git_probe:one",
    )
    enqueue_signal(
        repeated,
        kind="git_dirty",
        event_id="evt_repeat_b",
        provenance_root="git_probe:two",
    )
    expect(
        evaluate(repeated)["candidate_count"] == 0,
        "repeating one signal class does not create independence",
    )

    shared_lineage = WorldStateStore(root / "shared-provenance-lineage")
    seed_goal(shared_lineage)
    enqueue_signal(
        shared_lineage,
        kind="git_dirty",
        event_id="evt_shared_lineage_git",
        provenance_root="git_probe:shared-release-probe",
    )
    enqueue_signal(
        shared_lineage,
        kind="ci_failed",
        event_id="evt_shared_lineage_ci",
        provenance_root="ci_provider:shared-release-probe",
    )
    expect(
        evaluate(shared_lineage)["candidate_count"] == 0,
        "different components with one provenance lineage are not independent",
    )

    wrong_user = WorldStateStore(root / "wrong-user")
    seed_goal(wrong_user)
    enqueue_signal(
        wrong_user,
        kind="git_dirty",
        event_id="evt_wu_git",
        user_id="user-b",
    )
    enqueue_signal(
        wrong_user,
        kind="ci_failed",
        event_id="evt_wu_ci",
        user_id="user-b",
    )
    expect(
        evaluate(wrong_user)["candidate_count"] == 0,
        "cross-user signals never join a Goal",
    )

    wrong_scope = WorldStateStore(root / "wrong-scope")
    seed_goal(wrong_scope)
    other_scope = {**SCOPE, "target_ref": "refs/heads/feature"}
    enqueue_signal(wrong_scope, kind="git_dirty", event_id="evt_ws_git")
    enqueue_signal(
        wrong_scope,
        kind="ci_failed",
        event_id="evt_ws_ci",
        scope=other_scope,
    )
    expect(
        evaluate(wrong_scope)["candidate_count"] == 0,
        "cross-ref signals never join",
    )

    outside_window = WorldStateStore(root / "outside-window")
    seed_goal(outside_window)
    enqueue_signal(
        outside_window,
        kind="git_dirty",
        event_id="evt_ow_git",
        occurred_at=NOW - timedelta(minutes=40),
        valid_until=NOW + timedelta(minutes=5),
    )
    enqueue_signal(
        outside_window,
        kind="ci_failed",
        event_id="evt_ow_ci",
        occurred_at=NOW - timedelta(minutes=5),
    )
    expect(
        evaluate(outside_window)["candidate_count"] == 0,
        "signals outside the correlation window never join",
    )

    old_received_now = WorldStateStore(root / "old-received-now")
    seed_goal(old_received_now)
    enqueue_signal(
        old_received_now,
        kind="git_dirty",
        event_id="evt_old_git",
        occurred_at=NOW - timedelta(hours=2),
        received_at=NOW,
        valid_until=NOW + timedelta(minutes=5),
    )
    enqueue_signal(
        old_received_now,
        kind="ci_failed",
        event_id="evt_old_ci",
    )
    expect(
        evaluate(old_received_now)["candidate_count"] == 0,
        "new received_at cannot refresh stale occurred_at evidence",
    )

    missing_evidence = WorldStateStore(root / "missing-evidence")
    seed_goal(missing_evidence)
    enqueue_signal(
        missing_evidence,
        kind="git_dirty",
        event_id="evt_me_git",
        include_evidence=False,
    )
    enqueue_signal(
        missing_evidence,
        kind="ci_failed",
        event_id="evt_me_ci",
    )
    expect(
        evaluate(missing_evidence)["candidate_count"] == 0,
        "unknown or unreferenced evidence cannot count positive",
    )

    reversed_state = WorldStateStore(root / "state-reversal")
    seed_goal(reversed_state)
    enqueue_signal(
        reversed_state,
        kind="git_dirty",
        event_id="evt_sr_dirty",
        occurred_at=NOW - timedelta(minutes=8),
    )
    enqueue_signal(
        reversed_state,
        kind="git_dirty",
        state="clear",
        event_id="evt_sr_clean",
        occurred_at=NOW - timedelta(minutes=2),
    )
    enqueue_signal(
        reversed_state,
        kind="ci_failed",
        event_id="evt_sr_ci",
        occurred_at=NOW - timedelta(minutes=3),
    )
    expect(
        evaluate(reversed_state)["candidate_count"] == 0,
        "newer clean state supersedes older dirty state",
    )

    expired_clear = WorldStateStore(root / "expired-clear")
    seed_goal(expired_clear)
    enqueue_signal(
        expired_clear,
        kind="git_dirty",
        event_id="evt_ec_dirty",
        occurred_at=NOW - timedelta(minutes=8),
        valid_until=NOW + timedelta(minutes=5),
    )
    enqueue_signal(
        expired_clear,
        kind="git_dirty",
        state="clear",
        event_id="evt_ec_clean",
        occurred_at=NOW - timedelta(minutes=2),
        valid_until=NOW - timedelta(seconds=1),
    )
    enqueue_signal(
        expired_clear,
        kind="ci_failed",
        event_id="evt_ec_ci",
        occurred_at=NOW - timedelta(minutes=3),
    )
    expect(
        evaluate(expired_clear)["candidate_count"] == 0,
        "expired newer clear remains a tombstone and cannot resurrect older risk",
    )

    over_window = WorldStateStore(root / "window-plus-one-ms")
    seed_goal(over_window)
    first_signal_at = NOW - timedelta(minutes=40)
    enqueue_signal(
        over_window,
        kind="git_dirty",
        event_id="evt_w1_git",
        occurred_at=first_signal_at,
        valid_until=NOW + timedelta(minutes=5),
    )
    enqueue_signal(
        over_window,
        kind="ci_failed",
        event_id="evt_w1_ci",
        occurred_at=first_signal_at
        + timedelta(minutes=30, milliseconds=1),
    )
    expect(
        evaluate(over_window)["candidate_count"] == 0,
        "correlation window plus one millisecond never qualifies",
    )

    exact_goal_boundary = WorldStateStore(root / "goal-boundary")
    boundary_start = NOW - timedelta(minutes=10)
    seed_goal(
        exact_goal_boundary,
        goal(active_from=boundary_start, active_until=NOW),
    )
    enqueue_signal(
        exact_goal_boundary,
        kind="git_dirty",
        event_id="evt_gb_git",
        occurred_at=boundary_start,
    )
    enqueue_signal(
        exact_goal_boundary,
        kind="ci_failed",
        event_id="evt_gb_ci",
        occurred_at=NOW,
    )
    expect(
        evaluate(exact_goal_boundary)["candidate_count"] == 1,
        "signals exactly on active Goal interval boundaries qualify",
    )

    untrusted_channel = WorldStateStore(root / "untrusted-channel")
    seed_goal(untrusted_channel)
    enqueue_signal(
        untrusted_channel,
        kind="git_dirty",
        event_id="evt_uc_git",
        source_channel="api",
    )
    enqueue_signal(
        untrusted_channel,
        kind="ci_failed",
        event_id="evt_uc_ci",
        source_channel="api",
    )
    expect(
        evaluate(untrusted_channel)["candidate_count"] == 0,
        "user or model channels cannot forge Guardian producer signals",
    )

    mismatched_evidence = WorldStateStore(root / "mismatched-evidence")
    seed_goal(mismatched_evidence)
    enqueue_signal(
        mismatched_evidence,
        kind="git_dirty",
        event_id="evt_mismatch_git",
        evidence_ref_id="evidence:unrelated",
    )
    enqueue_signal(
        mismatched_evidence,
        kind="ci_failed",
        event_id="evt_mismatch_ci",
    )
    expect(
        evaluate(mismatched_evidence)["candidate_count"] == 0,
        "signal evidence id must bind to the envelope reference",
    )

    mismatched_source = WorldStateStore(root / "mismatched-evidence-source")
    seed_goal(mismatched_source)
    enqueue_signal(
        mismatched_source,
        kind="git_dirty",
        event_id="evt_ms_git",
        evidence_source="model",
    )
    enqueue_signal(
        mismatched_source,
        kind="ci_failed",
        event_id="evt_ms_ci",
    )
    expect(
        evaluate(mismatched_source)["candidate_count"] == 0,
        "signal evidence source must bind to the declared producer",
    )

    unverified_evidence = WorldStateStore(root / "unverified-evidence")
    seed_goal(unverified_evidence)
    enqueue_signal(
        unverified_evidence,
        kind="git_dirty",
        event_id="evt_ue_git",
        evidence_is_fact=False,
    )
    enqueue_signal(
        unverified_evidence,
        kind="ci_failed",
        event_id="evt_ue_ci",
    )
    expect(
        evaluate(unverified_evidence)["candidate_count"] == 0,
        "non-factual producer evidence cannot qualify a risk signal",
    )

    wrong_revision = WorldStateStore(root / "wrong-goal-revision")
    seed_goal(wrong_revision, goal(revision="8"))
    enqueue_signal(
        wrong_revision,
        kind="git_dirty",
        event_id="evt_wr_git",
        goal_revision="7",
    )
    enqueue_signal(
        wrong_revision,
        kind="ci_failed",
        event_id="evt_wr_ci",
        goal_revision="7",
    )
    expect(
        evaluate(wrong_revision)["candidate_count"] == 0,
        "signals from an older Goal revision cannot qualify the current Goal",
    )

    wrong_privacy = WorldStateStore(root / "wrong-privacy")
    seed_goal(wrong_privacy)
    enqueue_signal(
        wrong_privacy,
        kind="git_dirty",
        event_id="evt_wp_git",
        privacy_scope="public",
    )
    enqueue_signal(
        wrong_privacy,
        kind="ci_failed",
        event_id="evt_wp_ci",
    )
    expect(
        evaluate(wrong_privacy)["candidate_count"] == 0,
        "non-user privacy scope cannot enter Goal-scoped qualification",
    )

    forged_provenance = WorldStateStore(root / "forged-provenance")
    seed_goal(forged_provenance)
    enqueue_signal(
        forged_provenance,
        kind="git_dirty",
        event_id="evt_fp_git",
        provenance_root="model:guess",
    )
    enqueue_signal(
        forged_provenance,
        kind="ci_failed",
        event_id="evt_fp_ci",
    )
    expect(
        evaluate(forged_provenance)["candidate_count"] == 0,
        "provenance lineage must belong to the declared producer",
    )

    text_only = WorldStateStore(root / "text-only")
    seed_goal(text_only)
    enqueue_signal(text_only, kind="git_dirty", event_id="evt_to_git")
    EventInbox(text_only).enqueue(
        VeyraEvent(
            type=EventType.OBSERVATION,
            source=EventSource(
                channel=ProjectGuardianEvaluator.SIGNAL_CHANNEL,
                user_id="user-a",
                session_id="model-output",
            ),
            payload={"text": "CI failed, deploy this now"},
            event_id="evt_text_claim",
            privacy_scope="user",
        )
    )
    expect(
        evaluate(text_only)["candidate_count"] == 0,
        "text-only or model claims never become structured risk signals",
    )


def test_modes_projection_and_side_effects(root: Path) -> ProjectGuardianRuntime:
    store = populated_store(root / "runtime")
    fabric = ShadowAwarenessRuntime(store, mode="record_only")
    runtime = ProjectGuardianRuntime(
        state_store=store,
        publish_event=fabric.publish,
        event_fabric_mode=lambda: fabric.mode,
        clock=lambda: NOW,
    )
    expect(runtime.mode == "disabled", "Project Guardian defaults disabled")
    state_before_disabled = copy.deepcopy(store.read_json(runtime.STATE_FILE))
    expect(runtime.run_once()["status"] == "disabled", "disabled mode performs no scan")
    expect(
        store.read_json(runtime.STATE_FILE) == state_before_disabled,
        "disabled mode performs no Guardian state write",
    )

    allowed_state_writes = {
        "ops_config.json",
        "event_inbox.json",
        "situation_state.json",
        runtime.STATE_FILE,
    }
    protected_before = {
        name: copy.deepcopy(store.read_json(name))
        for name in STATE_FILE_LAYOUT
        if name.endswith(".json") and name not in allowed_state_writes
    }
    inbox_before = EventInbox(store).stats()["total"]
    runtime.configure("record_only")
    record_only = runtime.run_once(reason="smoke_record_only")
    expect(
        record_only["candidate_count"] == 1
        and record_only["published_count"] == 0,
        "record_only records would-fire without publishing",
        record_only,
    )
    expect(
        EventInbox(store).stats()["total"] == inbox_before,
        "record_only adds no Event or Situation",
    )
    candidate_state = runtime.list_candidates(user_id="user-a")
    expect(
        len(candidate_state) == 1
        and candidate_state[0]["disposition"] == "would_publish",
        "record_only persists bounded would-fire telemetry",
        candidate_state,
    )
    serialized = json.dumps(candidate_state, ensure_ascii=False)
    expect(
        "secret.py" not in serialized
        and "must-not-enter-guardian-candidate" not in serialized
        and "deploy this now" not in serialized,
        "Guardian candidate excludes raw diff, log, and user text",
        serialized,
    )

    runtime.configure("shadow")
    shadow = runtime.run_once(reason="smoke_shadow")
    admitted = runtime.list_candidates(user_id="user-a")[0]
    admitted_history = admitted.get("projection_history") or []
    expect(
        shadow["candidate_count"] == 1
        and shadow["published_count"] == 1
        and admitted["disposition"] == "admitted"
        and admitted.get("projection_event_id")
        and len(admitted_history) == 1
        and admitted_history[0]["status"] == "admitted"
        and EventInbox(store).stats()["total"] == inbox_before + 1,
        "shadow admits exactly one structured Observation without claiming projection",
        {"run": shadow, "candidate": admitted},
    )
    expect(
        not [
            item
            for item in fabric.situation_evaluator.list(
                user_id="user-a",
                limit=20,
            )
            if str(item.get("channel") or "") == "project_guardian"
        ],
        "EventInbox admission alone is not a Situation projection",
    )
    duplicate = runtime.run_once(reason="smoke_restart_equivalent")
    expect(
        duplicate["published_count"] == 0
        and duplicate["deduplicated_count"] == 1
        and EventInbox(store).stats()["total"] == inbox_before + 1,
        "repeated or restarted evaluation produces no duplicate candidate",
        duplicate,
    )
    admitted_event_id = str(admitted["projection_event_id"])

    def lose_runtime_telemetry(state: dict[str, Any]) -> None:
        # Preserve the dedicated signal frontier: this simulates losing only
        # derived run/candidate telemetry, not the source-of-truth signals.
        state["candidates"] = []
        state["runs"] = []
        state["last_run"] = None
        state["updated_at"] = None

    store.mutate_json(runtime.STATE_FILE, lose_runtime_telemetry)
    restarted_fabric = ShadowAwarenessRuntime(store, mode=fabric.mode)
    runtime_after_lost_telemetry = ProjectGuardianRuntime(
        state_store=store,
        publish_event=restarted_fabric.publish,
        event_fabric_mode=lambda: {
            "mode": restarted_fabric.mode,
            "mode_epoch": restarted_fabric.mode_epoch,
        },
        clock=lambda: NOW + timedelta(minutes=1),
    )
    repaired = runtime_after_lost_telemetry.run_once(reason="lost_telemetry_recovery")
    repaired_candidate = runtime_after_lost_telemetry.list_candidates(
        user_id="user-a"
    )[0]
    repaired_history = repaired_candidate.get("projection_history") or []
    expect(
        repaired["published_count"] == 0
        and repaired["deduplicated_count"] == 1
        and repaired_candidate["disposition"] == "admitted"
        and repaired_candidate.get("projection_event_id") == admitted_event_id
        and len(repaired_history) == 1
        and repaired_history[0]["event_id"] == admitted_event_id
        and repaired_history[0]["status"] == "admitted"
        and EventInbox(store).stats()["total"] == inbox_before + 1,
        "restart reconciles admitted-not-projected telemetry from EventInbox",
        {"run": repaired, "candidate": repaired_candidate},
    )
    projection = restarted_fabric.process_pending(limit=10)
    projected_candidate = runtime_after_lost_telemetry.list_candidates(
        user_id="user-a"
    )[0]
    projected_history = projected_candidate.get("projection_history") or []
    situation = restarted_fabric.situation_evaluator.get(
        ProjectGuardianRuntime.situation_id_for(
            str(projected_candidate["candidate_id"])
        ),
        user_id="user-a",
    )
    materialized_observation = (
        situation.get("observations", [{}])[-1]
        if isinstance(situation, dict)
        and isinstance(situation.get("observations"), list)
        and situation.get("observations")
        else {}
    )
    expect(
        projection["status"] == "success"
        and projected_candidate["disposition"] == "projected"
        and len(projected_history) == 1
        and projected_history[0]["event_id"] == admitted_event_id
        and projected_history[0]["status"] == "projected"
        and isinstance(situation, dict)
        and situation["decision"] is None
        and situation["outcome"] is None
        and situation["goal_refs"]
        and materialized_observation.get("is_fact") is False
        and materialized_observation.get("value", {}).get("candidate_kind")
        == "project_release_risk"
        and materialized_observation.get("value", {}).get("why_now"),
        "consumer acknowledgement advances admitted to projected exactly once",
        {
            "projection": projection,
            "candidate": projected_candidate,
            "situation": situation,
        },
    )
    expect(
        all(store.read_json(name) == value for name, value in protected_before.items()),
        "Guardian changes no business, authority, execution, review, or channel state",
    )
    return runtime


def test_projection_authority_locks(root: Path) -> None:
    store = populated_store(root / "projection-authority-locks")
    fabric = ShadowAwarenessRuntime(store, mode="record_only")
    fabric.configure("shadow")
    runtime = ProjectGuardianRuntime(
        state_store=store,
        publish_event=fabric.publish,
        event_fabric_mode=lambda: {
            "mode": fabric.mode,
            "mode_epoch": fabric.mode_epoch,
        },
        clock=lambda: NOW,
    )
    runtime.configure("shadow")
    candidate = evaluate(store)["candidates"][0]
    valid_event = runtime._candidate_event(
        candidate,
        projection_sequence=1,
        projection_attempt=0,
        guardian_mode_epoch=runtime.mode_epoch,
        event_fabric_mode_epoch=fabric.mode_epoch,
    )
    expect(
        ProjectGuardianRuntime.validate_projection_event(valid_event)
        is not None,
        "untampered Guardian projection satisfies the authority contract",
    )
    before_total = EventInbox(store).stats()["total"]
    tampered_results: dict[str, dict[str, Any]] = {}
    authority_tampering = {
        "agent_invoked": True,
        "shadow_only": False,
        "notification_allowed": True,
        "execution_allowed": True,
        "interrupt_eligible": True,
    }
    for field, value in authority_tampering.items():
        raw = valid_event.to_dict()
        payload = raw["payload"]
        payload[field] = value
        payload["observation"][field] = value
        tampered = VeyraEvent.from_dict(raw)
        validation = ProjectGuardianRuntime.validate_projection_event(
            tampered
        )
        admission = fabric.publish(tampered)
        tampered_results[field] = {
            "validation": validation,
            "admission": admission,
        }
    expect(
        all(
            item["validation"] is None
            and item["admission"].get("status") == "disabled"
            and item["admission"].get("reason")
            == "invalid_project_guardian_projection"
            for item in tampered_results.values()
        )
        and EventInbox(store).stats()["total"] == before_total,
        "each synchronized authority-lock tamper is rejected by validation and publish",
        tampered_results,
    )


def test_corrupt_guardian_inputs_freeze_lifecycle(root: Path) -> None:
    scenarios = (
        ("project_guardian_signal_state.json", False),
        ("user_goals.json", False),
        ("project_guardian_state.json", True),
    )
    for target_file, close_before_corruption in scenarios:
        store = populated_store(
            root / target_file.removesuffix(".json")
        )
        fabric = ShadowAwarenessRuntime(store, mode="record_only")
        fabric.configure("shadow")
        clock_now = [NOW]
        runtime = ProjectGuardianRuntime(
            state_store=store,
            publish_event=fabric.publish,
            event_fabric_mode=lambda fabric=fabric: {
                "mode": fabric.mode,
                "mode_epoch": fabric.mode_epoch,
            },
            clock=lambda clock_now=clock_now: clock_now[0],
        )
        runtime.configure("shadow")
        initial = runtime.run_once(reason="corruption_fixture_candidate")
        fabric.process_pending(limit=20)
        expect(
            initial["published_count"] == 1,
            f"{target_file} corruption fixture materializes one candidate",
            initial,
        )
        if close_before_corruption:
            seed_goal(store, goal(status="paused"))
            clock_now[0] = NOW + timedelta(seconds=1)
            close = runtime.run_once(
                reason="corruption_fixture_close"
            )
            fabric.process_pending(limit=20)
            seed_goal(store, goal(status="active"))
            expect(
                close["published_count"] == 1,
                "Guardian-state corruption fixture materializes one closure",
                close,
            )

        event_ids_before = {
            envelope["event_id"]
            for envelope in guardian_projection_envelopes(store)
        }
        guardian_state_before = copy.deepcopy(
            store.read_json(ProjectGuardianRuntime.STATE_FILE)
        )
        situation_before = copy.deepcopy(
            store.read_json("situation_state.json")
        )
        corrupt_payload = (
            '{"schema_version":"broken",'
            f'"target":"{target_file}"'
        )
        store.write_text(target_file, corrupt_payload)
        corrupt_before = store.read_text(target_file)
        try:
            result = runtime.run_once(
                reason=f"corrupt_{target_file}"
            )
        except Exception as exc:
            result = {
                "status": "exception",
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        event_ids_after = {
            envelope["event_id"]
            for envelope in guardian_projection_envelopes(store)
        }
        expect(
            result.get("status") == "degraded"
            and int(result.get("published_count") or 0) == 0
            and event_ids_after == event_ids_before
            and store.read_json("situation_state.json")
            == situation_before
            and (
                target_file == ProjectGuardianRuntime.STATE_FILE
                or store.read_json(ProjectGuardianRuntime.STATE_FILE)
                == guardian_state_before
            )
            and store.read_text(target_file) == corrupt_before,
            (
                f"corrupt {target_file} freezes lifecycle without "
                "closure, reopen, or corrupt-state overwrite"
            ),
            {
                "result": result,
                "events_before": event_ids_before,
                "events_after": event_ids_after,
                "corrupt_before": corrupt_before,
                "corrupt_after": store.read_text(target_file),
            },
        )


def test_admitted_recovery_uses_situation_evidence(root: Path) -> None:
    observed_store = populated_store(root / "admitted-observed")
    observed_fabric = ShadowAwarenessRuntime(
        observed_store,
        mode="record_only",
    )
    observed_fabric.configure("shadow")
    observed_runtime = ProjectGuardianRuntime(
        state_store=observed_store,
        publish_event=observed_fabric.publish,
        event_fabric_mode=lambda: {
            "mode": observed_fabric.mode,
            "mode_epoch": observed_fabric.mode_epoch,
        },
        clock=lambda: NOW,
    )
    observed_runtime.configure("shadow")
    observed_admission = observed_runtime.run_once(
        reason="admitted_observation_fixture"
    )
    observed_candidate = observed_runtime.list_candidates(
        user_id="user-a"
    )[0]
    admitted_event_id = str(
        observed_candidate["projection_event_id"]
    )
    admitted_record = (
        observed_store.read_json("event_inbox.json")
        .get("events", {})
        .get(admitted_event_id)
    )
    admitted_event = VeyraEvent.from_dict(
        admitted_record["envelope"]
    )
    admitted_payload = admitted_event.payload
    observed_fabric.situation_evaluator.observe(
        admitted_event,
        situation_id=str(admitted_payload["situation_id"]),
        salience_components=(
            admitted_payload.get("salience_components")
            if isinstance(
                admitted_payload.get("salience_components"),
                dict,
            )
            else {}
        ),
        observation=(
            admitted_payload.get("observation")
            if isinstance(admitted_payload.get("observation"), dict)
            else None
        ),
        status=str(
            admitted_payload.get("situation_status") or "observed"
        ),
        observation_sequence=int(
            admitted_payload.get("projection_sequence") or 0
        ),
        observation_id=(
            f"{admitted_payload['candidate_id']}:"
            f"{admitted_payload['candidate_revision']}"
        ),
    )

    def lose_candidate_telemetry(state: dict[str, Any]) -> None:
        state["candidates"] = []
        state["runs"] = []
        state["last_run"] = None

    observed_store.mutate_json(
        ProjectGuardianRuntime.STATE_FILE,
        lose_candidate_telemetry,
    )
    observed_restarted = ProjectGuardianRuntime(
        state_store=observed_store,
        publish_event=observed_fabric.publish,
        event_fabric_mode=lambda: {
            "mode": observed_fabric.mode,
            "mode_epoch": observed_fabric.mode_epoch,
        },
        clock=lambda: NOW + timedelta(seconds=1),
    )
    observed_recovery = observed_restarted.run_once(
        reason="admitted_observation_recovery"
    )
    recovered_observed = observed_restarted.list_candidates(
        user_id="user-a"
    )[0]
    observed_situation = observed_fabric.situation_evaluator.get(
        str(admitted_payload["situation_id"]),
        user_id="user-a",
    )
    expect(
        observed_admission["published_count"] == 1
        and observed_recovery["published_count"] == 0
        and recovered_observed["disposition"] == "projected"
        and len(
            guardian_projection_envelopes(observed_store)
        )
        == 1
        and isinstance(observed_situation, dict)
        and observed_situation["status"] == "observed",
        "Situation observation promotes admitted delivery after candidate telemetry loss",
        {
            "run": observed_recovery,
            "candidate": recovered_observed,
            "situation": observed_situation,
        },
    )

    retry_store = populated_store(root / "admitted-missing-inbox")
    retry_fabric = ShadowAwarenessRuntime(
        retry_store,
        mode="record_only",
    )
    retry_fabric.configure("shadow")
    retry_runtime = ProjectGuardianRuntime(
        state_store=retry_store,
        publish_event=retry_fabric.publish,
        event_fabric_mode=lambda: {
            "mode": retry_fabric.mode,
            "mode_epoch": retry_fabric.mode_epoch,
        },
        clock=lambda: NOW,
    )
    retry_runtime.configure("shadow")
    first_attempt = retry_runtime.run_once(
        reason="admitted_retry_fixture"
    )
    first_candidate = retry_runtime.list_candidates(
        user_id="user-a"
    )[0]
    first_event_id = str(first_candidate["projection_event_id"])

    def lose_inbox_delivery(state: dict[str, Any]) -> None:
        events = (
            state.get("events")
            if isinstance(state.get("events"), dict)
            else {}
        )
        events.pop(first_event_id, None)
        dedupe_index = (
            state.get("dedupe_index")
            if isinstance(state.get("dedupe_index"), dict)
            else {}
        )
        for key, event_id in list(dedupe_index.items()):
            if str(event_id) == first_event_id:
                dedupe_index.pop(key, None)

    retry_store.mutate_json(
        "event_inbox.json",
        lose_inbox_delivery,
    )
    retry_restarted = ProjectGuardianRuntime(
        state_store=retry_store,
        publish_event=retry_fabric.publish,
        event_fabric_mode=lambda: {
            "mode": retry_fabric.mode,
            "mode_epoch": retry_fabric.mode_epoch,
        },
        clock=lambda: NOW + timedelta(seconds=1),
    )
    retry_run = retry_restarted.run_once(
        reason="admitted_missing_inbox_retry"
    )
    retried_candidate = retry_restarted.list_candidates(
        user_id="user-a"
    )[0]
    retry_history = [
        item
        for item in (retried_candidate.get("projection_history") or [])
        if item.get("projection_kind") == "candidate"
    ]
    retried_event_id = str(
        retried_candidate.get("projection_event_id") or ""
    )
    retry_situations = [
        item
        for item in retry_fabric.situation_evaluator.list(
            user_id="user-a",
            limit=20,
        )
        if str(item.get("channel") or "") == "project_guardian"
    ]
    expect(
        first_attempt["published_count"] == 1
        and retry_run["published_count"] == 1
        and retry_run["projected_count"] == 0
        and retried_candidate["disposition"] == "admitted"
        and retried_event_id
        and retried_event_id != first_event_id
        and retry_history[-1]["projection_attempt"] == 1
        and len(guardian_projection_envelopes(retry_store)) == 1
        and not retry_situations,
        "missing Inbox delivery without Situation creates a new transport attempt",
        {
            "run": retry_run,
            "candidate": retried_candidate,
            "history": retry_history,
            "situations": retry_situations,
        },
    )


def test_revision_lifecycle_and_situation_merge(root: Path) -> None:
    store = populated_store(root / "revision-lifecycle")
    fabric = ShadowAwarenessRuntime(store, mode="record_only")
    fabric.configure("shadow")
    clock_now = [NOW]
    runtime = ProjectGuardianRuntime(
        state_store=store,
        publish_event=fabric.publish,
        event_fabric_mode=lambda: {
            "mode": fabric.mode,
            "mode_epoch": fabric.mode_epoch,
        },
        clock=lambda: clock_now[0],
    )

    def projection_envelopes() -> list[dict[str, Any]]:
        inbox = store.read_json("event_inbox.json")
        return [
            record["envelope"]
            for record in (inbox.get("events") or {}).values()
            if isinstance(record, dict)
            and isinstance(record.get("envelope"), dict)
            and str(
                (record["envelope"].get("source") or {}).get("channel")
                or ""
            )
            == "project_guardian"
        ]

    runtime.configure("shadow")
    first = runtime.run_once(reason="initial_revision")
    initial = runtime.list_candidates(user_id="user-a")[0]
    first_revision = initial["candidate_revision"]

    enqueue_signal(
        store,
        kind="deployment_intent",
        event_id="evt_revision_deploy",
        session_id="semantic-runtime",
        occurred_at=NOW - timedelta(minutes=1),
    )
    second = runtime.run_once(reason="new_evidence_revision")
    updated = runtime.list_candidates(user_id="user-a")[0]
    second_revision = updated["candidate_revision"]

    enqueue_signal(
        store,
        kind="deployment_intent",
        state="clear",
        event_id="evt_revision_deploy_clear",
        session_id="semantic-runtime",
        occurred_at=NOW,
    )
    third = runtime.run_once(reason="risk_signal_cleared")
    returned = runtime.list_candidates(user_id="user-a")[0]
    third_revision = returned["candidate_revision"]
    candidate_history = [
        item
        for item in (returned.get("projection_history") or [])
        if item.get("projection_kind") == "candidate"
    ]
    expect(
        first["published_count"] == 1
        and second["published_count"] == 1
        and third["published_count"] == 1
        and first_revision != second_revision
        and second_revision != third_revision
        and first_revision != third_revision
        and updated["signal_kinds"]
        == ["ci_failed", "deployment_intent", "git_dirty"]
        and returned["signal_kinds"] == ["ci_failed", "git_dirty"]
        and [item["candidate_revision"] for item in candidate_history]
        == [first_revision, second_revision, third_revision]
        and all(item["status"] == "admitted" for item in candidate_history),
        "2-to-3-to-2 signal transitions create three monotonic admitted revisions",
        {
            "first": first,
            "second": second,
            "third": third,
            "candidate": returned,
        },
    )
    replay = runtime.run_once(reason="same_revision_replay")
    expect(
        replay["published_count"] == 0
        and replay["deduplicated_count"] == 1,
        "the same candidate revision projects at most once",
        replay,
    )

    guardian_envelopes = projection_envelopes()
    situation_ids = {
        str((envelope.get("payload") or {}).get("situation_id") or "")
        for envelope in guardian_envelopes
        if isinstance(envelope, dict)
    }
    expect(
        len(guardian_envelopes) == 3
        and len(situation_ids) == 1
        and "" not in situation_ids,
        "candidate revisions share one deterministic Situation identity",
        guardian_envelopes,
    )
    projection = fabric.process_pending(limit=20)
    situation = fabric.situation_evaluator.get(
        next(iter(situation_ids)),
        user_id="user-a",
    )
    projected = runtime.list_candidates(user_id="user-a")[0]
    projected_candidates = [
        item
        for item in (projected.get("projection_history") or [])
        if item.get("projection_kind") == "candidate"
    ]
    expect(
        projection["status"] == "success"
        and isinstance(situation, dict)
        and situation.get("observation_revision") == 3
        and len(situation.get("observations") or []) == 3
        and [item["status"] for item in projected_candidates]
        == ["projected", "projected", "projected"],
        "three candidate revisions project once into one non-factual Situation",
        {
            "projection": projection,
            "candidate": projected,
            "situation": situation,
        },
    )

    seed_goal(store, goal(status="completed"))
    clock_now[0] = NOW + timedelta(seconds=1)
    close_run = runtime.run_once(reason="goal_completed")
    close_admitted = runtime.list_candidates(user_id="user-a")[0]
    projection_count_after_close_admission = len(projection_envelopes())
    admitted_repeat = runtime.run_once(reason="closure_admitted_repeat")
    projection_count_after_admitted_repeat = len(projection_envelopes())
    close_projection = fabric.process_pending(limit=20)
    closed = runtime.list_candidates(user_id="user-a")[0]
    closed_situation = fabric.situation_evaluator.get(
        next(iter(situation_ids)),
        user_id="user-a",
    )
    projected_repeat = runtime.run_once(reason="closure_projected_repeat")
    restarted_runtime = ProjectGuardianRuntime(
        state_store=store,
        publish_event=fabric.publish,
        event_fabric_mode=lambda: {
            "mode": fabric.mode,
            "mode_epoch": fabric.mode_epoch,
        },
        clock=lambda: clock_now[0],
    )
    restarted_repeat = restarted_runtime.run_once(
        reason="closure_projected_restart_repeat"
    )
    guardian_after_close = projection_envelopes()
    closure_envelopes = [
        envelope
        for envelope in guardian_after_close
        if str((envelope.get("payload") or {}).get("projection_kind") or "")
        == "closure"
    ]
    closure_payload = (
        closure_envelopes[0].get("payload")
        if len(closure_envelopes) == 1
        and isinstance(closure_envelopes[0].get("payload"), dict)
        else {}
    )
    closed_parent_envelopes = [
        envelope
        for envelope in guardian_after_close
        if str((envelope.get("payload") or {}).get("projection_kind") or "")
        == "candidate"
        and str(
            (envelope.get("payload") or {}).get("candidate_revision")
            or ""
        )
        == str(closure_payload.get("closure_of_revision") or "")
    ]
    closure_occurred_at = (
        datetime.fromisoformat(
            str(closure_envelopes[0].get("occurred_at") or "").replace(
                "Z",
                "+00:00",
            )
        )
        if len(closure_envelopes) == 1
        else datetime.min.replace(tzinfo=timezone.utc)
    )
    parent_occurred_at = (
        datetime.fromisoformat(
            str(closed_parent_envelopes[0].get("occurred_at") or "").replace(
                "Z",
                "+00:00",
            )
        )
        if len(closed_parent_envelopes) == 1
        else datetime.max.replace(tzinfo=timezone.utc)
    )
    expect(
        close_run["candidate_count"] == 0
        and close_run["published_count"] == 1
        and close_admitted["disposition"] == "inactive"
        and close_admitted["closure_status"] == "admitted"
        and admitted_repeat["published_count"] == 0
        and projected_repeat["published_count"] == 0
        and restarted_repeat["published_count"] == 0
        and projection_count_after_close_admission == 4
        and projection_count_after_admitted_repeat == 4
        and len(guardian_after_close) == 4
        and len(closure_envelopes) == 1
        and closure_payload.get("closure_of_revision") == third_revision
        and not str(
            closure_payload.get("closure_of_revision") or ""
        ).startswith("pgrc_")
        and closure_occurred_at > parent_occurred_at
        and close_projection["status"] == "success"
        and closed["closure_status"] == "projected"
        and isinstance(closed_situation, dict)
        and closed_situation["status"] == "closed",
        "Goal completion emits one forward-timed closure across repeats and restart",
        {
            "run": close_run,
            "candidate": closed,
            "projection": close_projection,
            "situation": closed_situation,
            "admitted_repeat": admitted_repeat,
            "projected_repeat": projected_repeat,
            "restarted_repeat": restarted_repeat,
            "guardian_envelopes": guardian_after_close,
        },
    )
    runtime = restarted_runtime

    seed_goal(store, goal(status="active"))
    reopen_run = runtime.run_once(reason="same_revision_reopened")
    reopen_admitted = runtime.list_candidates(user_id="user-a")[0]
    projection_count_after_reopen_admission = len(projection_envelopes())
    reopen_repeat = runtime.run_once(reason="reopen_admitted_repeat")
    reopen_still_admitted = runtime.list_candidates(user_id="user-a")[0]
    reopen_history_before_projection = [
        item
        for item in (
            reopen_still_admitted.get("projection_history") or []
        )
        if item.get("projection_kind") == "reopen"
    ]
    projection_count_after_reopen_repeat = len(projection_envelopes())
    reopen_projection = fabric.process_pending(limit=20)
    reopened = runtime.list_candidates(user_id="user-a")[0]
    reopened_situation = fabric.situation_evaluator.get(
        next(iter(situation_ids)),
        user_id="user-a",
    )
    lifecycle_history = [
        item
        for item in (reopened.get("projection_history") or [])
        if item.get("projection_kind") in {"candidate", "closure", "reopen"}
    ]
    sequences = [int(item["projection_sequence"]) for item in lifecycle_history]
    observation_sequences = [
        int((item.get("value") or {}).get("projection_sequence") or 0)
        for item in (
            reopened_situation.get("observations") or []
            if isinstance(reopened_situation, dict)
            else []
        )
    ]
    expect(
        reopen_run["candidate_count"] == 1
        and reopen_run["published_count"] == 1
        and reopen_admitted["disposition"] == "admitted"
        and reopen_repeat["published_count"] == 0
        and reopen_repeat["projected_count"] == 0
        and reopen_repeat["deduplicated_count"] == 1
        and reopen_still_admitted["disposition"] == "admitted"
        and len(reopen_history_before_projection) == 1
        and reopen_history_before_projection[0]["status"] == "admitted"
        and projection_count_after_reopen_admission == 5
        and projection_count_after_reopen_repeat == 5
        and reopen_projection["status"] == "success"
        and reopened["candidate_revision"] == third_revision
        and reopened["disposition"] == "projected"
        and isinstance(reopened_situation, dict)
        and reopened_situation["situation_id"] == next(iter(situation_ids))
        and reopened_situation["status"] == "observed"
        and [item["projection_kind"] for item in lifecycle_history]
        == ["candidate", "candidate", "candidate", "closure", "reopen"]
        and all(item["status"] == "projected" for item in lifecycle_history)
        and sequences == [1, 2, 3, 4, 5]
        and observation_sequences == [1, 2, 3, 4, 5],
        "same Goal revision reopens the closed Situation with monotonic lifecycle sequence",
        {
            "run": reopen_run,
            "repeat": reopen_repeat,
            "candidate": reopened,
            "projection": reopen_projection,
            "situation": reopened_situation,
        },
    )


def test_record_only_revision_does_not_inherit_delivery(root: Path) -> None:
    store = populated_store(root / "record-only-revision")
    fabric = ShadowAwarenessRuntime(store, mode="record_only")
    fabric.configure("shadow")
    runtime = ProjectGuardianRuntime(
        state_store=store,
        publish_event=fabric.publish,
        event_fabric_mode=lambda: {
            "mode": fabric.mode,
            "mode_epoch": fabric.mode_epoch,
        },
        clock=lambda: NOW,
    )
    runtime.configure("shadow")
    first_run = runtime.run_once(reason="revision_one_shadow")
    fabric.process_pending(limit=10)
    first = runtime.list_candidates(user_id="user-a")[0]
    first_revision = str(first["candidate_revision"])

    runtime.configure("record_only")
    enqueue_signal(
        store,
        kind="deployment_intent",
        event_id="evt_record_only_revision",
        occurred_at=NOW - timedelta(minutes=1),
    )
    record_only_run = runtime.run_once(reason="revision_two_record_only")
    record_only = runtime.list_candidates(user_id="user-a")[0]
    second_revision = str(record_only["candidate_revision"])
    expect(
        first_run["published_count"] == 1
        and first["disposition"] == "projected"
        and first_revision != second_revision
        and record_only_run["published_count"] == 0
        and record_only["disposition"] == "would_publish",
        "record-only revision cannot inherit an earlier revision's projected disposition",
        {
            "first": first,
            "record_only_run": record_only_run,
            "record_only": record_only,
        },
    )

    runtime.configure("shadow")
    second_run = runtime.run_once(reason="revision_two_shadow")
    second_admitted = runtime.list_candidates(user_id="user-a")[0]
    fabric.process_pending(limit=10)
    second_projected = runtime.list_candidates(user_id="user-a")[0]
    projected_revisions = [
        str(item.get("candidate_revision") or "")
        for item in (second_projected.get("projection_history") or [])
        if item.get("projection_kind") == "candidate"
        and item.get("status") == "projected"
    ]
    expect(
        second_run["published_count"] == 1
        and second_admitted["disposition"] == "admitted"
        and second_projected["disposition"] == "projected"
        and projected_revisions == [first_revision, second_revision],
        "returning to shadow admits and projects the new revision exactly once",
        {
            "run": second_run,
            "admitted": second_admitted,
            "projected": second_projected,
        },
    )


def test_candidate_capacity_has_no_orphan_lifecycle(root: Path) -> None:
    store = WorldStateStore(root / "candidate-capacity")
    retained_limit = 3
    qualified_count = retained_limit + 2
    goals: list[dict[str, Any]] = []
    fixtures: list[tuple[str, str, dict[str, str]]] = []
    for index in range(qualified_count):
        user_id = f"capacity-user-{index:02d}"
        goal_id = f"capacity-goal-{index:02d}"
        scope = {
            **SCOPE,
            "workspace_id": f"capacity-workspace-{index:02d}",
            "repo_id": f"capacity/repo-{index:02d}",
            "release_cycle": f"capacity-cycle-{index:02d}",
        }
        goals.append(
            goal(
                goal_id=goal_id,
                user_id=user_id,
                scope=scope,
                revision="1",
            )
        )
        fixtures.append((user_id, goal_id, scope))
    store.write_json(
        "user_goals.json",
        {"goals": goals, "updated_at": NOW.isoformat()},
    )
    for index, (user_id, goal_id, scope) in enumerate(fixtures):
        enqueue_signal(
            store,
            kind="git_dirty",
            event_id=f"evt_capacity_git_{index:02d}",
            user_id=user_id,
            goal_id=goal_id,
            goal_revision="1",
            scope=scope,
            occurred_at=NOW - timedelta(minutes=5),
        )
        enqueue_signal(
            store,
            kind="ci_failed",
            event_id=f"evt_capacity_ci_{index:02d}",
            user_id=user_id,
            goal_id=goal_id,
            goal_revision="1",
            scope=scope,
            occurred_at=NOW - timedelta(minutes=2),
        )

    fabric = ShadowAwarenessRuntime(store, mode="record_only")
    fabric.configure("shadow")
    clock_now = [NOW]
    runtime = ProjectGuardianRuntime(
        state_store=store,
        publish_event=fabric.publish,
        event_fabric_mode=lambda: {
            "mode": fabric.mode,
            "mode_epoch": fabric.mode_epoch,
        },
        clock=lambda: clock_now[0],
    )
    runtime.MAX_CANDIDATES = retained_limit
    runtime.configure("shadow")
    admitted_run = runtime.run_once(reason="candidate_capacity")
    admitted_envelopes = guardian_projection_envelopes(store)
    admitted_state = store.read_json(runtime.STATE_FILE)
    admitted_candidates = [
        item
        for item in (admitted_state.get("candidates") or [])
        if isinstance(item, dict)
    ]
    persisted_ids = {
        str(item.get("candidate_id") or "")
        for item in admitted_candidates
    }
    published_ids = {
        str((envelope.get("payload") or {}).get("candidate_id") or "")
        for envelope in admitted_envelopes
    }
    fabric.process_pending(limit=100)
    materialized = [
        item
        for item in fabric.situation_evaluator.list(limit=100)
        if str(item.get("channel") or "") == "project_guardian"
    ]
    expect(
        admitted_run["evaluation"]["candidate_count"]
        == qualified_count
        and admitted_run["published_count"] == retained_limit
        and len(admitted_candidates) == retained_limit
        and len(admitted_envelopes) == retained_limit
        and published_ids == persisted_ids
        and len(materialized) == retained_limit,
        "capacity admits only candidates whose lifecycle state can be retained",
        {
            "run": admitted_run,
            "published_ids": published_ids,
            "persisted_ids": persisted_ids,
            "situations": materialized,
        },
    )

    def pause_goals(state: dict[str, Any]) -> None:
        for item in state.get("goals") or []:
            if isinstance(item, dict):
                item["status"] = "paused"

    store.mutate_json("user_goals.json", pause_goals)
    clock_now[0] = NOW + timedelta(seconds=1)
    close_run = runtime.run_once(reason="capacity_close")
    fabric.process_pending(limit=100)
    closed_situations = [
        item
        for item in fabric.situation_evaluator.list(limit=100)
        if str(item.get("channel") or "") == "project_guardian"
    ]
    expect(
        close_run["published_count"] == retained_limit
        and len(closed_situations) == retained_limit
        and all(
            item.get("status") == "closed"
            for item in closed_situations
        ),
        "all capacity-admitted Situations retain state and receive closure",
        {
            "run": close_run,
            "situations": closed_situations,
        },
    )


def test_projection_history_overflow_preserves_lifecycle_head(
    root: Path,
) -> None:
    original_limit = ProjectGuardianRuntime.MAX_PROJECTION_HISTORY
    bounded_limit = 4
    ProjectGuardianRuntime.MAX_PROJECTION_HISTORY = bounded_limit
    try:
        store = populated_store(root / "projection-history-overflow")
        fabric = ShadowAwarenessRuntime(store, mode="record_only")
        fabric.configure("shadow")
        clock_now = [NOW]
        runtime = ProjectGuardianRuntime(
            state_store=store,
            publish_event=fabric.publish,
            event_fabric_mode=lambda: {
                "mode": fabric.mode,
                "mode_epoch": fabric.mode_epoch,
            },
            clock=lambda: clock_now[0],
        )
        runtime.configure("shadow")
        runtime.run_once(reason="history_head_candidate")
        fabric.process_pending(limit=20)
        projected = runtime.list_candidates(user_id="user-a")[0]
        situation_id = ProjectGuardianRuntime.situation_id_for(
            str(projected["candidate_id"])
        )

        noise_count = bounded_limit + 2
        for attempt in range(1, noise_count + 1):
            noisy_event = runtime._candidate_event(
                projected,
                projection_sequence=1,
                projection_attempt=attempt,
                guardian_mode_epoch=runtime.mode_epoch,
                event_fabric_mode_epoch=fabric.mode_epoch,
            )
            ProjectGuardianRuntime.record_projection_result(
                store,
                noisy_event,
                status=(
                    "failed"
                    if attempt % 2
                    else "suppressed"
                ),
            )
        noisy_candidate = runtime.list_candidates(
            user_id="user-a"
        )[0]
        noisy_history = noisy_candidate.get("projection_history") or []

        seed_goal(store, goal(status="paused"))
        clock_now[0] = NOW + timedelta(seconds=1)
        close_run = runtime.run_once(reason="history_head_close")
        close_projection = fabric.process_pending(limit=20)
        closed_situation = fabric.situation_evaluator.get(
            situation_id,
            user_id="user-a",
        )
    finally:
        ProjectGuardianRuntime.MAX_PROJECTION_HISTORY = original_limit

    expect(
        noise_count > bounded_limit
        and len(noisy_history) <= bounded_limit
        and close_run["published_count"] == 1
        and close_projection["status"] == "success"
        and isinstance(closed_situation, dict)
        and closed_situation["status"] == "closed",
        "bounded failed or suppressed history cannot evict the projected lifecycle head",
        {
            "history": noisy_history,
            "close_run": close_run,
            "close_projection": close_projection,
            "situation": closed_situation,
        },
    )


def test_projection_order_and_replay_safety(root: Path) -> None:
    store = populated_store(root / "projection-order")
    seed_inbox = EventInbox(store)
    while True:
        claimed = seed_inbox.claim(
            "projection-order-signal-drain",
            channel=ProjectGuardianEvaluator.SIGNAL_CHANNEL,
        )
        if not isinstance(claimed, dict):
            break
        seed_inbox.complete(
            str(claimed["event_id"]),
            "projection-order-signal-drain",
            {"status": "signal_recorded"},
        )

    fabric = ShadowAwarenessRuntime(store, mode="record_only")
    fabric.configure("shadow")
    runtime = ProjectGuardianRuntime(
        state_store=store,
        publish_event=fabric.publish,
        event_fabric_mode=lambda: {
            "mode": fabric.mode,
            "mode_epoch": fabric.mode_epoch,
        },
        clock=lambda: NOW,
    )
    runtime.configure("shadow")
    runtime.run_once(reason="ordered_p1")
    enqueue_signal(
        store,
        kind="deployment_intent",
        event_id="evt_order_deploy",
        occurred_at=NOW - timedelta(minutes=1),
    )
    runtime.run_once(reason="ordered_p2")
    enqueue_signal(
        store,
        kind="deployment_intent",
        state="clear",
        event_id="evt_order_deploy_clear",
        occurred_at=NOW,
    )
    runtime.run_once(reason="ordered_p3")
    seed_goal(store, goal(status="paused"))
    runtime.run_once(reason="ordered_p4_close")
    seed_goal(store, goal(status="active"))
    runtime.run_once(reason="ordered_p5_reopen")
    while True:
        claimed = seed_inbox.claim(
            "projection-order-signal-drain",
            channel=ProjectGuardianEvaluator.SIGNAL_CHANNEL,
        )
        if not isinstance(claimed, dict):
            break
        seed_inbox.complete(
            str(claimed["event_id"]),
            "projection-order-signal-drain",
            {"status": "signal_recorded"},
        )

    state = store.read_json("event_inbox.json")
    projection_records = [
        record
        for record in (state.get("events") or {}).values()
        if isinstance(record, dict)
        and isinstance(record.get("envelope"), dict)
        and str(
            ((record["envelope"].get("source") or {}).get("channel") or "")
        )
        == "project_guardian"
    ]
    projection_records.sort(
        key=lambda record: int(
            ((record["envelope"].get("payload") or {}).get(
                "projection_sequence"
            )
            or 0)
        )
    )
    expect(
        len(projection_records) == 5
        and [
            int(
                ((record["envelope"].get("payload") or {}).get(
                    "projection_sequence"
                )
                or 0)
            )
            for record in projection_records
        ]
        == [1, 2, 3, 4, 5],
        "P1-P5 lifecycle projections are admitted before disorder injection",
        projection_records,
    )

    def reverse_delivery(document: dict[str, Any]) -> None:
        for record in (document.get("events") or {}).values():
            if not isinstance(record, dict):
                continue
            envelope = (
                record.get("envelope")
                if isinstance(record.get("envelope"), dict)
                else {}
            )
            if str(((envelope.get("source") or {}).get("channel") or "")) != (
                "project_guardian"
            ):
                continue
            payload = (
                envelope.get("payload")
                if isinstance(envelope.get("payload"), dict)
                else {}
            )
            sequence = int(payload.get("projection_sequence") or 0)
            delivery_order = 5 - sequence
            timestamp = (
                f"2000-01-01T00:00:0{delivery_order}+00:00"
            )
            record["available_at"] = timestamp
            record["enqueued_at"] = timestamp

    store.mutate_json("event_inbox.json", reverse_delivery)
    projection = fabric.process_pending(limit=10)
    candidate = runtime.list_candidates(user_id="user-a")[0]
    situation_id = ProjectGuardianRuntime.situation_id_for(
        str(candidate["candidate_id"])
    )
    situation = fabric.situation_evaluator.get(
        situation_id,
        user_id="user-a",
    )
    observations = (
        situation.get("observations") or []
        if isinstance(situation, dict)
        else []
    )
    observation_sequences = [
        int((item.get("value") or {}).get("projection_sequence") or 0)
        for item in observations
    ]
    p5_envelope = projection_records[-1]["envelope"]
    expect(
        projection["status"] == "success"
        and projection["processed_count"] == 5
        and projection["suppressed_count"] == 0
        and candidate.get("situation_status") == "observed"
        and isinstance(situation, dict)
        and situation["status"] == "observed"
        and situation["source_event_id"] == p5_envelope["event_id"]
        and situation["observation_revision"] == 5
        and observation_sequences == [1, 2, 3, 4, 5],
        "reverse P5-to-P1 delivery preserves the P5 head and ordered P1-P5 history",
        {
            "projection": projection,
            "candidate": candidate,
            "situation": situation,
        },
    )

    replay_envelope = projection_records[2]["envelope"]
    replay_event = VeyraEvent.from_dict(replay_envelope)
    replay_payload = replay_event.payload
    before_replay = copy.deepcopy(situation)
    replayed = fabric.situation_evaluator.observe(
        replay_event,
        situation_id=str(replay_payload["situation_id"]),
        salience_components=(
            replay_payload.get("salience_components")
            if isinstance(replay_payload.get("salience_components"), dict)
            else {}
        ),
        observation=(
            replay_payload.get("observation")
            if isinstance(replay_payload.get("observation"), dict)
            else None
        ),
        status=str(replay_payload.get("situation_status") or "observed"),
        observation_sequence=int(
            replay_payload.get("projection_sequence") or 0
        ),
        observation_id=(
            f"{replay_payload['candidate_id']}:"
            f"{replay_payload['candidate_revision']}"
        ),
        allow_terminal_reopen=(
            str(replay_payload.get("projection_kind") or "") == "reopen"
        ),
    )
    expect(
        replayed["source_event_id"] == p5_envelope["event_id"]
        and replayed["status"] == "observed"
        and replayed["observation_revision"]
        == before_replay["observation_revision"]
        and replayed.get("observations") == before_replay.get("observations"),
        "exact replay of an older projection is idempotent and cannot replace the head",
        {"before": before_replay, "after": replayed},
    )


def test_signal_frontier_survives_queue_eviction(root: Path) -> None:
    store = populated_store(root / "signal-frontier")
    expect(
        evaluate(store)["candidate_count"] == 1,
        "signal frontier initially qualifies independently of delivery status",
    )
    bounded_inbox = EventInbox(store, max_records=2)
    for _ in range(2):
        claimed = bounded_inbox.claim(
            "signal-frontier-eviction",
            channel=ProjectGuardianEvaluator.SIGNAL_CHANNEL,
        )
        expect(
            isinstance(claimed, dict),
            "seed signal can be completed before queue eviction",
            claimed,
        )
        bounded_inbox.complete(
            str(claimed["event_id"]),
            "signal-frontier-eviction",
            {"status": "signal_recorded"},
        )
    for index in range(2):
        bounded_inbox.enqueue(
            VeyraEvent(
                type=EventType.OBSERVATION,
                source=EventSource(
                    channel="api",
                    user_id="queue-user",
                    session_id="queue-session",
                ),
                payload={"kind": "queue_retention_fixture", "index": index},
                event_id=f"evt_queue_retention_{index}",
                timestamp=(NOW + timedelta(seconds=index)).isoformat(),
                occurred_at=(NOW + timedelta(seconds=index)).isoformat(),
                privacy_scope="user",
            )
        )
    retained_ids = set(
        (store.read_json("event_inbox.json").get("events") or {}).keys()
    )
    expect(
        {"evt_git", "evt_ci"}.isdisjoint(retained_ids)
        and evaluate(store)["candidate_count"] == 1,
        "completed EventInbox eviction cannot erase the Guardian signal frontier",
        retained_ids,
    )

    enqueue_signal(
        store,
        kind="git_dirty",
        state="clear",
        event_id="evt_frontier_newer_clear",
        occurred_at=NOW - timedelta(minutes=1),
    )
    enqueue_signal(
        store,
        kind="git_dirty",
        state="present",
        event_id="evt_frontier_late_old_present",
        occurred_at=NOW - timedelta(minutes=7),
    )
    frontier_state = ProjectGuardianSignalLedger(store).evaluation_state()
    frontier_envelopes = [
        record.get("envelope")
        for record in (frontier_state.get("events") or {}).values()
        if isinstance(record, dict)
        and isinstance(record.get("envelope"), dict)
    ]
    git_frontier = [
        envelope
        for envelope in frontier_envelopes
        if str(
            (
                (envelope.get("payload") or {})
                .get("project_guardian_signal", {})
                .get("kind")
                or ""
            )
        )
        == "git_dirty"
    ]
    expect(
        evaluate(store)["candidate_count"] == 0
        and len(git_frontier) == 1
        and git_frontier[0]["event_id"] == "evt_frontier_newer_clear"
        and (
            (git_frontier[0].get("payload") or {})
            .get("project_guardian_signal", {})
            .get("state")
            == "clear"
        ),
        "a late older present cannot override a newer clear frontier tombstone",
        git_frontier,
    )


def test_signal_frontier_rejects_malformed_replacements(root: Path) -> None:
    store = populated_store(root / "malformed-signal-frontier")
    initial_frontier = ProjectGuardianSignalLedger(store).evaluation_state()
    compact_serialized = json.dumps(
        store.read_json(ProjectGuardianSignalLedger.STATE_FILE),
        ensure_ascii=False,
        sort_keys=True,
    )
    expect(
        '"details"' not in compact_serialized
        and '"raw_log"' not in compact_serialized
        and '"user_text"' not in compact_serialized
        and "secret.py" not in compact_serialized
        and "must-not-enter-guardian-candidate" not in compact_serialized
        and "deploy this now" not in compact_serialized,
        "signal frontier stores only compact qualification fields",
        compact_serialized,
    )

    enqueue_signal(
        store,
        kind="git_dirty",
        event_id="evt_invalid_later_source",
        occurred_at=NOW - timedelta(minutes=2),
        source_component="ci_provider",
        provenance_root="ci_provider:wrong-component",
        evidence_source="ci_provider",
    )
    enqueue_signal(
        store,
        kind="git_dirty",
        event_id="evt_invalid_later_evidence",
        occurred_at=NOW - timedelta(minutes=1),
        evidence_ref_id="evidence:not-the-declared-id",
    )
    enqueue_signal(
        store,
        kind="git_dirty",
        event_id="evt_invalid_future_skew",
        occurred_at=NOW + timedelta(minutes=3),
        valid_until=NOW + timedelta(minutes=10),
    )
    after_invalid = ProjectGuardianSignalLedger(store).evaluation_state()
    git_envelopes = [
        record["envelope"]
        for record in (after_invalid.get("events") or {}).values()
        if isinstance(record, dict)
        and isinstance(record.get("envelope"), dict)
        and str(
            (
                (record["envelope"].get("payload") or {})
                .get("project_guardian_signal", {})
                .get("kind")
                or ""
            )
        )
        == "git_dirty"
    ]
    expect(
        after_invalid == initial_frontier
        and len(git_envelopes) == 1
        and git_envelopes[0]["event_id"] == "evt_git"
        and evaluate(store)["candidate_count"] == 1,
        "malformed later source, evidence, or future time cannot replace a valid frontier",
        {
            "initial": initial_frontier,
            "after_invalid": after_invalid,
            "git_envelopes": git_envelopes,
        },
    )


def test_equal_time_clear_precedence(root: Path) -> None:
    occurred_at = NOW - timedelta(minutes=1)
    for label, delivery_order in (
        ("present-then-clear", ("present", "clear")),
        ("clear-then-present", ("clear", "present")),
    ):
        store = WorldStateStore(root / label)
        seed_goal(store)
        enqueue_signal(
            store,
            kind="ci_failed",
            event_id=f"evt_equal_time_ci_{label}",
            occurred_at=NOW - timedelta(minutes=2),
        )
        events = {
            "present": "evt_z_equal_time_present",
            "clear": "evt_a_equal_time_clear",
        }
        for state in delivery_order:
            enqueue_signal(
                store,
                kind="git_dirty",
                state=state,
                event_id=events[state],
                occurred_at=occurred_at,
            )
        frontier = ProjectGuardianSignalLedger(store).evaluation_state()
        git_frontier = [
            record["envelope"]
            for record in (frontier.get("events") or {}).values()
            if isinstance(record, dict)
            and isinstance(record.get("envelope"), dict)
            and str(
                (
                    (record["envelope"].get("payload") or {})
                    .get("project_guardian_signal", {})
                    .get("kind")
                    or ""
                )
            )
            == "git_dirty"
        ]
        expect(
            evaluate(store)["candidate_count"] == 0
            and len(git_frontier) == 1
            and git_frontier[0]["event_id"]
            == "evt_a_equal_time_clear"
            and (
                (git_frontier[0].get("payload") or {})
                .get("project_guardian_signal", {})
                .get("state")
                == "clear"
            ),
            (
                "equal-time clear dominates present independent of "
                f"event-id order and {label} delivery"
            ),
            git_frontier,
        )


def test_signal_ledger_recovers_after_partial_publish(root: Path) -> None:
    store = WorldStateStore(root / "signal-ledger-partial-publish")
    seed_goal(store)
    fabric = ShadowAwarenessRuntime(store, mode="record_only")

    def fail_ledger_write(envelope: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("injected signal ledger failure")

    fabric.project_guardian_signals.record_envelope = fail_ledger_write
    signal_events = [
        enqueue_signal(
            store,
            kind="git_dirty",
            event_id="evt_partial_ledger_git",
            occurred_at=NOW - timedelta(minutes=5),
            enqueue=False,
        ),
        enqueue_signal(
            store,
            kind="ci_failed",
            event_id="evt_partial_ledger_ci",
            occurred_at=NOW - timedelta(minutes=2),
            enqueue=False,
        ),
    ]
    admissions = [fabric.publish(event) for event in signal_events]
    before_repair = ProjectGuardianSignalLedger(store).evaluation_state()
    expect(
        all(
            admission.get("status") == "enqueued"
            and admission.get("signal_ledger_status") == "degraded"
            for admission in admissions
        )
        and EventInbox(store).stats()["total"] == 2
        and not (before_repair.get("events") or {}),
        "signal publish reports degraded ledger after durable Inbox admission",
        {
            "admissions": admissions,
            "inbox": EventInbox(store).stats(),
            "frontier": before_repair,
        },
    )

    recovered_fabric = ShadowAwarenessRuntime(store, mode="record_only")
    first_repair = recovered_fabric.process_pending(limit=0)
    repaired_frontier = ProjectGuardianSignalLedger(
        store
    ).evaluation_state()
    second_repair = recovered_fabric.process_pending(limit=0)
    guardian = ProjectGuardianRuntime(
        state_store=store,
        publish_event=recovered_fabric.publish,
        event_fabric_mode=lambda: {
            "mode": recovered_fabric.mode,
            "mode_epoch": recovered_fabric.mode_epoch,
        },
        clock=lambda: NOW,
    )
    guardian.configure("record_only")
    guardian_run = guardian.run_once(reason="repaired_signal_qualification")
    candidates = guardian.list_candidates(user_id="user-a")
    expect(
        first_repair["signal_reconciliation"]["recorded_count"] == 2
        and first_repair["signal_reconciliation"]["accepted_signal_count"]
        == 2
        and len(repaired_frontier.get("events") or {}) == 2
        and second_repair["signal_reconciliation"]["recorded_count"] == 0
        and second_repair["signal_reconciliation"]["stale_count"] == 2
        and guardian_run["candidate_count"] == 1
        and guardian_run["evaluation"]["accepted_signal_count"] == 2
        and len(candidates) == 1
        and candidates[0]["disposition"] == "would_publish"
        and EventInbox(store).stats()["total"] == 2,
        "restart reconciles the Inbox once and repaired signals qualify without duplication",
        {
            "first_repair": first_repair,
            "second_repair": second_repair,
            "run": guardian_run,
            "candidates": candidates,
            "frontier": repaired_frontier,
        },
    )


def test_missing_goal_signals_cannot_evict_active_frontier(
    root: Path,
) -> None:
    store = WorldStateStore(root / "missing-goal-pressure")
    seed_goal(store)
    fabric = ShadowAwarenessRuntime(store, mode="record_only")
    fabric.project_guardian_signals.MAX_RECORDS_PER_USER = 4
    fabric.configure("shadow")
    true_signals = [
        enqueue_signal(
            store,
            kind="git_dirty",
            event_id="evt_pressure_true_git",
            occurred_at=NOW - timedelta(minutes=8),
            enqueue=False,
        ),
        enqueue_signal(
            store,
            kind="ci_failed",
            event_id="evt_pressure_true_ci",
            occurred_at=NOW - timedelta(minutes=5),
            enqueue=False,
        ),
    ]
    for event in true_signals:
        admission = fabric.publish(event)
        expect(
            admission.get("signal_ledger_status") == "recorded",
            "active Goal signal enters the bounded frontier",
            admission,
        )
    runtime = ProjectGuardianRuntime(
        state_store=store,
        publish_event=fabric.publish,
        event_fabric_mode=lambda: {
            "mode": fabric.mode,
            "mode_epoch": fabric.mode_epoch,
        },
        clock=lambda: NOW,
    )
    runtime.signal_ledger.MAX_RECORDS_PER_USER = 4
    runtime.configure("shadow")
    first_run = runtime.run_once(reason="pressure_fixture")
    fabric.process_pending(limit=20)
    candidate = runtime.list_candidates(user_id="user-a")[0]
    situation_id = ProjectGuardianRuntime.situation_id_for(
        str(candidate["candidate_id"])
    )

    for index in range(8):
        missing_scope = {
            **SCOPE,
            "repo_id": f"missing/repo-{index}",
            "release_cycle": f"missing-cycle-{index}",
        }
        junk = enqueue_signal(
            store,
            kind="git_dirty",
            event_id=f"evt_pressure_missing_goal_{index:02d}",
            goal_id=f"missing-goal-{index:02d}",
            goal_revision="1",
            scope=missing_scope,
            occurred_at=NOW - timedelta(minutes=1)
            + timedelta(milliseconds=index),
            enqueue=False,
        )
        fabric.publish(junk)

    pressure_run = runtime.run_once(reason="missing_goal_pressure")
    signal_state = ProjectGuardianSignalLedger(store).evaluation_state()
    retained_signal_ids = {
        str((record.get("envelope") or {}).get("event_id") or "")
        for record in (signal_state.get("events") or {}).values()
        if isinstance(record, dict)
    }
    situation = fabric.situation_evaluator.get(
        situation_id,
        user_id="user-a",
    )
    expect(
        first_run["published_count"] == 1
        and pressure_run["candidate_count"] == 1
        and pressure_run["published_count"] == 0
        and len(signal_state.get("events") or {}) <= 4
        and {
            "evt_pressure_true_git",
            "evt_pressure_true_ci",
        }
        <= retained_signal_ids
        and len(guardian_projection_envelopes(store)) == 1
        and isinstance(situation, dict)
        and situation["status"] == "observed",
        "same-user signals for missing Goals cannot evict or close the active Goal frontier",
        {
            "run": pressure_run,
            "retained_signal_ids": retained_signal_ids,
            "signal_state": signal_state,
            "situation": situation,
        },
    )


def test_dependency_kill_switch_and_fail_open(root: Path) -> None:
    guardian_store = populated_store(root / "guardian-kill-switch")
    guardian_fabric = ShadowAwarenessRuntime(guardian_store, mode="record_only")
    guardian_holder: dict[str, ProjectGuardianRuntime] = {}

    def disable_before_admission(event: VeyraEvent) -> dict[str, Any]:
        guardian_holder["runtime"].configure("disabled")
        return guardian_fabric.publish(event)

    guardian_runtime = ProjectGuardianRuntime(
        state_store=guardian_store,
        publish_event=disable_before_admission,
        event_fabric_mode=lambda: guardian_fabric.mode,
        clock=lambda: NOW,
    )
    guardian_holder["runtime"] = guardian_runtime
    guardian_runtime.configure("shadow")
    guardian_race = guardian_runtime.run_once(reason="guardian_disable_race")
    guardian_events = [
        record
        for record in (guardian_store.read_json("event_inbox.json").get("events") or {}).values()
        if isinstance(record, dict)
        and str(((record.get("envelope") or {}).get("source") or {}).get("channel") or "")
        == "project_guardian"
    ]
    expect(
        guardian_race["status"] == "skipped"
        and guardian_race["published_count"] == 0
        and not guardian_events,
        "Guardian disable is linearized with EventInbox admission",
        {"result": guardian_race, "events": guardian_events},
    )

    queued_store = populated_store(root / "queued-then-disabled")
    queued_inbox = EventInbox(queued_store)
    while True:
        claimed = queued_inbox.claim(
            "seed-signal-drain",
            channel=ProjectGuardianEvaluator.SIGNAL_CHANNEL,
        )
        if not isinstance(claimed, dict):
            break
        queued_inbox.complete(
            str(claimed["event_id"]),
            "seed-signal-drain",
            {"status": "seeded"},
        )
    queued_fabric = ShadowAwarenessRuntime(queued_store, mode="record_only")
    queued_fabric.configure("shadow")
    queued_runtime = ProjectGuardianRuntime(
        state_store=queued_store,
        publish_event=queued_fabric.publish,
        event_fabric_mode=lambda: {
            "mode": queued_fabric.mode,
            "mode_epoch": queued_fabric.mode_epoch,
        },
        clock=lambda: NOW,
    )
    queued_runtime.configure("shadow")
    queued = queued_runtime.run_once(reason="enqueue_before_disable")
    queued_candidate = queued_runtime.list_candidates(user_id="user-a")[0]
    stale_event_id = str(queued_candidate["projection_event_id"])

    queued_fabric.configure("disabled")
    queued_fabric.configure("shadow")
    restarted_fabric = ShadowAwarenessRuntime(queued_store, mode="shadow")
    suppressed = restarted_fabric.process_pending(limit=10)
    guardian_situations_after_stale = [
        item
        for item in restarted_fabric.situation_evaluator.list(
            user_id="user-a",
            limit=20,
        )
        if str(item.get("channel") or "") == "project_guardian"
    ]
    expect(
        queued["published_count"] == 1
        and suppressed["processed_count"] == 0
        and suppressed["suppressed_count"] == 1
        and not guardian_situations_after_stale,
        "disable-reenable plus restart fences the old Event Fabric epoch",
        {
            "queued": queued,
            "projection": suppressed,
            "situations": guardian_situations_after_stale,
        },
    )

    restarted_runtime = ProjectGuardianRuntime(
        state_store=queued_store,
        publish_event=restarted_fabric.publish,
        event_fabric_mode=lambda: {
            "mode": restarted_fabric.mode,
            "mode_epoch": restarted_fabric.mode_epoch,
        },
        clock=lambda: NOW + timedelta(minutes=1),
    )
    retried = restarted_runtime.run_once(reason="retry_after_epoch_fence")
    retry_candidate = restarted_runtime.list_candidates(user_id="user-a")[0]
    retry_event_id = str(retry_candidate["projection_event_id"])
    retry_projection = restarted_fabric.process_pending(limit=10)
    projected_candidate = restarted_runtime.list_candidates(user_id="user-a")[0]
    projection_history = projected_candidate.get("projection_history") or []
    guardian_situations = [
        item
        for item in restarted_fabric.situation_evaluator.list(
            user_id="user-a",
            limit=20,
        )
        if str(item.get("channel") or "") == "project_guardian"
    ]
    expect(
        retried["published_count"] == 1
        and retry_event_id
        and retry_event_id != stale_event_id
        and retry_projection["processed_count"] == 1
        and len(guardian_situations) == 1
        and [item["status"] for item in projection_history]
        == ["suppressed", "projected"]
        and [int(item["projection_sequence"]) for item in projection_history]
        == [1, 1]
        and [int(item["projection_attempt"]) for item in projection_history]
        == [0, 1],
        "a fresh transport attempt projects after the stale epoch is suppressed",
        {
            "run": retried,
            "projection": retry_projection,
            "candidate": projected_candidate,
            "situations": guardian_situations,
        },
    )

    store = populated_store(root / "kill-switch")
    configure_mode(store, "shadow")
    publish_calls: list[str] = []
    modes = iter(("record_only", "disabled"))
    runtime = ProjectGuardianRuntime(
        state_store=store,
        publish_event=lambda event: publish_calls.append(event.event_id) or {"status": "enqueued"},
        event_fabric_mode=lambda: next(modes, "disabled"),
        clock=lambda: NOW,
    )
    result = runtime.run_once(reason="kill_switch_race")
    expect(
        result["status"] == "skipped" and not publish_calls,
        "event-fabric disable is rechecked immediately before publish",
        result,
    )

    store_disabled = populated_store(root / "fabric-disabled")
    configure_mode(store_disabled, "shadow")
    disabled_calls: list[str] = []
    blocked = ProjectGuardianRuntime(
        state_store=store_disabled,
        publish_event=lambda event: disabled_calls.append(event.event_id) or {"status": "enqueued"},
        event_fabric_mode=lambda: "disabled",
        clock=lambda: NOW,
    ).run_once()
    expect(
        blocked["status"] == "skipped" and not disabled_calls,
        "Guardian never bypasses a disabled event fabric",
        blocked,
    )

    active_store = WorldStateStore(root / "active-loop")
    calls: list[str] = []

    class RuntimeEntity:
        lifecycle = SimpleNamespace(status="idle", last_heartbeat_at=None)

        def set_status(self, value: str) -> None:
            self.lifecycle.last_heartbeat_at = NOW.isoformat()

    class Retention:
        def summary(self) -> dict[str, Any]:
            calls.append("retention")
            return {"policy": "bounded", "files": []}

        def enforce(self) -> dict[str, Any]:
            return {"policy": "bounded", "changed": 0, "files": []}

    loop = ActiveRuntimeLoop(
        state_store=active_store,
        runtime_entity=RuntimeEntity(),
        proactive_checks=SimpleNamespace(
            run_read_only=lambda **kwargs: calls.append("proactive")
            or {"status": "success"}
        ),
        state_refresh=SimpleNamespace(
            refresh_stale=lambda **kwargs: calls.append("stale_state")
            or {"status": "success"}
        ),
        external_world_refresh=SimpleNamespace(
            refresh_watchlist=lambda **kwargs: calls.append("external_world")
            or {"status": "success"}
        ),
        runtime_matrix=SimpleNamespace(run=lambda **kwargs: {"status": "success"}),
        retention_policy=Retention(),
        task_tracker=SimpleNamespace(
            refresh_pending=lambda *args, **kwargs: calls.append("pending_tasks")
            or {"status": "success"}
        ),
        adapter_resolver=lambda: object(),
        verifier=object(),
        event_consumer=lambda **kwargs: {"status": "success"},
        project_guardian=lambda **kwargs: (_ for _ in ()).throw(
            RuntimeError("shadow evaluator unavailable")
        ),
    )
    tick = loop.tick(reason="guardian_failure")
    guardian_step = next(
        step for step in tick["steps"] if step["name"] == "project_guardian"
    )
    expect(
        tick["status"] == "degraded"
        and guardian_step["status"] == "error"
        and {"pending_tasks", "proactive", "external_world", "retention"}
        <= set(calls),
        "Guardian failure degrades only its step and later work continues",
        tick,
    )


def test_shared_fabric_disable_blocks_stale_instance(root: Path) -> None:
    store = WorldStateStore(root / "shared-fabric-disable")
    runtime_a = ShadowAwarenessRuntime(store, mode="record_only")
    runtime_a.configure("shadow")
    runtime_b = ShadowAwarenessRuntime(store, mode="shadow")
    runtime_b.configure("disabled")

    event = EventNormalizer().user_message(
        "status check",
        "api",
        "shared-mode-user",
        "shared-mode-session",
        event_id="evt_shared_mode_disabled",
    )
    result = LoopResult(
        event_id=event.event_id,
        route=Route.DIRECT_ANSWER,
        status="success",
        response="ok",
        risk_level=RiskLevel.R0,
    )
    json_before = copy.deepcopy(store.read_all())
    jsonl_before = {
        name: store.read_jsonl(name, limit=10_000)
        for name in (
            "action_record.jsonl",
            "alert_log.jsonl",
            "situation_trace.jsonl",
        )
    }
    begin_result = runtime_a.begin(event)
    finalize_result = runtime_a.finalize(
        event,
        result,
        {"trace_id": "trace_shared_mode_disabled"},
    )
    json_after = store.read_all()
    jsonl_after = {
        name: store.read_jsonl(name, limit=10_000)
        for name in jsonl_before
    }
    expect(
        begin_result["status"] == "disabled"
        and finalize_result is None
        and json_after == json_before
        and jsonl_after == jsonl_before,
        "a persisted disable from instance B blocks stale instance A begin and finalize writes",
        {
            "begin": begin_result,
            "finalize": finalize_result,
            "json_changed": json_after != json_before,
            "jsonl_changed": jsonl_after != jsonl_before,
        },
    )


def test_shadow_telemetry_health_isolation(root: Path) -> None:
    store = populated_store(root / "telemetry-health")
    runtime = ProjectGuardianRuntime(
        state_store=store,
        publish_event=lambda event: {"status": "unexpected"},
        event_fabric_mode=lambda: "record_only",
        clock=lambda: NOW,
    )
    runtime.configure("record_only")
    runtime.run_once(reason="telemetry_health")
    guardian_state = store.read_json(runtime.STATE_FILE)
    guardian_health = next(
        (
            item
            for item in store.state_health().get("items", [])
            if isinstance(item, dict) and item.get("name") == runtime.STATE_FILE
        ),
        {},
    )
    agency = AgencyCore(
        store,
        agency_root=root / "agency",
        model_assist_enabled=False,
    )
    gaps = agency.detect_state_gap(goals={}, world_state=store.read_all())
    expect(
        guardian_state.get("ttl_seconds") == 0
        and guardian_health.get("ttl_seconds") == 0
        and guardian_health.get("health_status") == "fresh"
        and not any(
            str(gap.get("gap_id") or "")
            == f"state_ttl:{runtime.STATE_FILE}"
            for gap in gaps
            if isinstance(gap, dict)
        ),
        "shadow telemetry cannot become a stale-state business intention",
        {
            "guardian_state": guardian_state,
            "health": guardian_health,
            "gaps": gaps,
        },
    )


def test_nine_route_public_output_equivalence(root: Path) -> None:
    normalizer = EventNormalizer()
    scenarios = ("disabled", "record_only", "shadow", "shadow_fault")
    for case in OFFLINE_ROUTE_CASES:
        event = normalizer.user_message(
            case.text,
            "guardian-route-matrix",
            "matrix-user",
            f"guardian-{case.case_id}",
            event_id=f"evt_guardian_matrix_{case.case_id}",
            correlation_id=f"corr-guardian-matrix-{case.case_id}",
        )
        results: dict[str, Any] = {}
        run_results: dict[str, dict[str, Any]] = {}
        for scenario in scenarios:
            loop = build_offline_route_loop(
                root / case.case_id / scenario,
                mode="shadow",
                case=case,
            )
            seed_goal(loop.state_store)
            enqueue_signal(
                loop.state_store,
                kind="git_dirty",
                event_id=f"evt_{case.case_id}_{scenario}_git",
                occurred_at=NOW - timedelta(minutes=5),
            )
            enqueue_signal(
                loop.state_store,
                kind="ci_failed",
                event_id=f"evt_{case.case_id}_{scenario}_ci",
                occurred_at=NOW - timedelta(minutes=3),
            )
            selected_mode = "shadow" if scenario == "shadow_fault" else scenario
            configure_mode(loop.state_store, selected_mode)

            def publish(event: VeyraEvent) -> dict[str, Any]:
                if scenario == "shadow_fault":
                    raise RuntimeError("injected Guardian publisher fault")
                return loop.publish_event(event)

            guardian = ProjectGuardianRuntime(
                state_store=loop.state_store,
                publish_event=publish,
                event_fabric_mode=lambda loop=loop: loop.event_awareness.mode,
                clock=lambda: NOW,
            )
            run_results[scenario] = guardian.run_once(
                reason=f"route_matrix_{scenario}"
            )
            results[scenario] = loop.handle_event(event)

        expect(
            run_results["disabled"]["status"] == "disabled"
            and run_results["record_only"]["published_count"] == 0
            and run_results["shadow"]["published_count"] == 1
            and run_results["shadow_fault"]["status"] == "degraded"
            and run_results["shadow_fault"]["published_count"] == 0,
            f"{case.case_id} exercises all Guardian observation modes and fault isolation",
            run_results,
        )
        differences: dict[str, list[str]] = {}
        for left, right in combinations(scenarios, 2):
            equivalent, pair_differences = offline_public_outputs_equivalent(
                results[left],
                results[right],
                require_distinct_generated_ids=True,
            )
            if not equivalent:
                differences[f"{left}:{right}"] = pair_differences
        expect(
            not differences,
            (
                f"{case.case_id} complete public output, status, and risk are "
                "identical with Guardian disabled, record-only, shadow, or failed"
            ),
            differences,
        )
    expect(
        len(OFFLINE_ROUTE_CASES) == 9,
        "Guardian non-interference matrix covers all nine Route branches",
    )


def test_debug_scope_and_config(root: Path, runtime: ProjectGuardianRuntime) -> None:
    app = FastAPI()
    app.include_router(
        build_debug_audit_router(
            {
                "project_guardian": runtime,
                "state_store": runtime.state_store,
            }
        )
    )
    client = TestClient(app)
    expect(
        client.get("/awareness/project-guardian/candidates").status_code == 422,
        "candidate inspection requires explicit user scope",
    )
    own = client.get(
        "/awareness/project-guardian/candidates",
        params={"user_id": "user-a"},
    ).json()
    other = client.get(
        "/awareness/project-guardian/candidates",
        params={"user_id": "user-b"},
    ).json()
    expect(
        own["count"] == 1 and other["count"] == 0,
        "candidate inspection is logically user-scoped",
        {"own": own, "other": other},
    )
    status = client.get("/awareness/project-guardian/status").json()
    expect(
        status["contracts"]["notifications"] == "disabled_by_contract"
        and status["contracts"]["agent_execution"] == "disabled_by_contract"
        and status["contracts"]["read_only"] is True,
        "status exposes immutable authority locks",
        status,
    )
    expect(
        client.post(
            "/awareness/project-guardian/config",
            json={"mode": "advise_only"},
        ).status_code
        == 422,
        "config rejects authority-expanding modes",
    )
    runtime.state_store.mutate_json(
        "ops_config.json",
        lambda config: config["project_guardian"].update(
            {
                "correlation_window_seconds": 1800,
                "producer_allowlist": ["git_probe", "ci_provider"],
            }
        ),
    )
    before = runtime.state_store.read_json("ops_config.json")
    updated = client.post(
        "/awareness/project-guardian/config",
        json={"mode": "record_only"},
    )
    after = runtime.state_store.read_json("ops_config.json")
    expect(
        updated.status_code == 200
        and after["event_awareness"] == before["event_awareness"]
        and after["tool_proxy"] == before["tool_proxy"]
        and after["active_loop"] == before["active_loop"]
        and after["project_guardian"]["correlation_window_seconds"] == 1800
        and after["project_guardian"]["producer_allowlist"]
        == ["git_probe", "ci_provider"],
        "Guardian config is independent of fabric, tools, and scheduler",
        updated.json(),
    )
    generic_public = _public_state(
        {
            "project_guardian_state": {
                "candidates": [{"user_id": "user-a"}]
            },
            "project_guardian_signal_state": {
                "signals": {"secret": {"user_id": "user-a"}}
            },
            "event_inbox": {},
            "situation_state": {},
        }
    )
    expect(
        "project_guardian_state" not in generic_public
        and "project_guardian_signal_state" not in generic_public,
        "generic public state omits candidate and signal Guardian telemetry",
        generic_public,
    )


def main() -> int:
    git_commands = {
        "status": ["status", "--porcelain=v1"],
        "head": ["rev-parse", "HEAD"],
        "refs": ["show-ref"],
        "index": ["diff", "--cached", "--binary"],
    }

    def git_snapshot() -> dict[str, str]:
        return {
            key: subprocess.run(
                ["git", *arguments],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            for key, arguments in git_commands.items()
        }

    git_before = git_snapshot()
    with tempfile.TemporaryDirectory(prefix="veyra-project-guardian-") as tmp:
        root = Path(tmp)
        test_qualification_matrix(root)
        test_negative_and_correlation_cases(root)
        runtime = test_modes_projection_and_side_effects(root)
        test_projection_authority_locks(root)
        test_corrupt_guardian_inputs_freeze_lifecycle(root)
        test_admitted_recovery_uses_situation_evidence(root)
        test_revision_lifecycle_and_situation_merge(root)
        test_record_only_revision_does_not_inherit_delivery(root)
        test_candidate_capacity_has_no_orphan_lifecycle(root)
        test_projection_history_overflow_preserves_lifecycle_head(root)
        test_projection_order_and_replay_safety(root)
        test_signal_frontier_survives_queue_eviction(root)
        test_signal_frontier_rejects_malformed_replacements(root)
        test_equal_time_clear_precedence(root)
        test_signal_ledger_recovers_after_partial_publish(root)
        test_missing_goal_signals_cannot_evict_active_frontier(root)
        test_dependency_kill_switch_and_fail_open(root)
        test_shared_fabric_disable_blocks_stale_instance(root)
        test_shadow_telemetry_health_isolation(root)
        test_nine_route_public_output_equivalence(root)
        test_debug_scope_and_config(root, runtime)
    git_after = git_snapshot()
    expect(
        git_before == git_after,
        "Guardian evaluation changes no Git worktree, index, HEAD, or refs",
        {"before": git_before, "after": git_after},
    )
    print("Project Guardian smoke passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
