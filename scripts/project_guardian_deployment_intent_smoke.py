#!/usr/bin/env python3
from __future__ import annotations

import copy
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]

import sys

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from awareness.project_guardian import ProjectGuardianEvaluator
from core.world_state import JSONL_FILES, STATE_FILE_LAYOUT, WorldStateStore
from interface.event_normalizer import EventNormalizer
from interface.event_schema import EventSource, EventType, VeyraEvent
from runtime.event_awareness_runtime import ShadowAwarenessRuntime
from runtime.project_guardian import ProjectGuardianRuntime
from runtime.project_guardian_producers import (
    ProjectGuardianGoalConflict,
    ProjectGuardianProducerRuntime,
)
from runtime.project_guardian_signal_ledger import ProjectGuardianSignalLedger
from scripts.event_driven_awareness_smoke import (
    OFFLINE_ROUTE_CASES,
    build_offline_route_loop,
    offline_public_outputs_equivalent,
)
from scripts.project_guardian_producer_smoke import (
    REPO_ID,
    TARGET_REF,
    create_git_fixture,
    frontier_signals,
    git,
    git_snapshot,
    signal_envelopes,
)
from scripts.project_guardian_smoke import configure_mode


NOW = datetime.now(timezone.utc).replace(microsecond=0)
USER_ID = "intent-user"
SESSION_ID = "intent-session"
GOAL_ID = "goal_release_deployment_intent"
TARGET_ENVIRONMENT = "production"


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"PASS {label}")


def expect_raises(
    error_types: type[BaseException] | tuple[type[BaseException], ...],
    label: str,
    action: Callable[[], Any],
) -> BaseException:
    try:
        action()
    except error_types as exc:
        print(f"PASS {label}")
        return exc
    except Exception as exc:
        raise AssertionError(
            f"{label}: unexpected {type(exc).__name__}: {exc}"
        ) from exc
    raise AssertionError(f"{label}: expected an exception")


def producer(
    store: WorldStateStore,
    fabric: ShadowAwarenessRuntime,
    *,
    intent_publisher: Callable[..., dict[str, Any]] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> ProjectGuardianProducerRuntime:
    return ProjectGuardianProducerRuntime(
        state_store=store,
        publish_git_observation=(
            fabric.issue_project_guardian_git_publisher()
        ),
        publish_deployment_intent=(
            intent_publisher
            or fabric.issue_project_guardian_deployment_intent_publisher()
        ),
        clock=clock or (lambda: NOW),
    )


def register_goal(
    runtime: ProjectGuardianProducerRuntime,
    repo: Path,
    *,
    user_id: str = USER_ID,
    goal_id: str = GOAL_ID,
    release_cycle: str = "deployment_intent_smoke",
    active_from: datetime | None = None,
    active_until: datetime | None = None,
    expected_state_revision: int | None = None,
) -> dict[str, Any]:
    return runtime.register_release_goal(
        user_id=user_id,
        workspace_id="workspace-deployment-intent-smoke",
        repo_id=REPO_ID,
        target_ref=TARGET_REF,
        target_environment=TARGET_ENVIRONMENT,
        release_cycle=release_cycle,
        workspace_path=str(repo),
        active_from=(active_from or NOW - timedelta(hours=1)).isoformat(),
        active_until=(active_until or NOW + timedelta(hours=1)).isoformat(),
        goal_id=goal_id,
        expected_state_revision=expected_state_revision,
    )


def record_intent(
    runtime: ProjectGuardianProducerRuntime,
    goal: dict[str, Any],
    *,
    transition: str = "declare",
    operation_id: str = "intent-operation-1",
    occurred_at: datetime = NOW,
    user_id: str | None = None,
    goal_id: str | None = None,
    goal_revision: str | None = None,
    expected_goal_state_revision: int | None = None,
    target_sha: str | None = None,
    target_environment: str | None = None,
    session_id: str = SESSION_ID,
) -> dict[str, Any]:
    return runtime.record_deployment_intent(
        user_id=user_id or str(goal["user_id"]),
        goal_id=goal_id or str(goal["goal_id"]),
        goal_revision=goal_revision or str(goal["revision"]),
        expected_goal_state_revision=(
            int(goal["state_revision"])
            if expected_goal_state_revision is None
            else expected_goal_state_revision
        ),
        target_sha=target_sha or str(goal["target_sha"]),
        target_environment=(
            target_environment
            or str((goal.get("scope") or {})["target_environment"])
        ),
        transition=transition,
        operation_id=operation_id,
        occurred_at=occurred_at.isoformat(),
        session_id=session_id,
    )


def deployment_signals(store: WorldStateStore) -> list[dict[str, Any]]:
    return [
        signal
        for signal in frontier_signals(store)
        if str(signal.get("kind") or "") == "deployment_intent"
    ]


def evaluate(store: WorldStateStore, *, now: datetime) -> dict[str, Any]:
    return ProjectGuardianEvaluator().evaluate(
        goals_state=store.read_json("user_goals.json"),
        event_inbox_state=ProjectGuardianSignalLedger(
            store
        ).evaluation_state(),
        now=now,
    )


def all_state_snapshot(store: WorldStateStore) -> dict[str, Any]:
    return {
        "json": {
            name: store.read_json(name)
            for name in STATE_FILE_LAYOUT
            if name.endswith(".json")
        },
        "jsonl": {
            name: store.read_jsonl(name, limit=100_000)
            for name in JSONL_FILES
        },
    }


def nonproducer_state_snapshot(store: WorldStateStore) -> dict[str, Any]:
    return {
        "json": {
            name: store.read_json(name)
            for name in STATE_FILE_LAYOUT
            if name.endswith(".json")
            and name != ProjectGuardianProducerRuntime.STATE_FILE
        },
        "jsonl": {
            name: store.read_jsonl(name, limit=100_000)
            for name in JSONL_FILES
        },
    }


def forbidden_effect_snapshot(store: WorldStateStore) -> dict[str, Any]:
    return {
        "agent_config": store.read_json("agent_config.json"),
        "agent_memory": store.read_json("agent_memory.json"),
        "task_state": store.read_json("task_state.json"),
        "executor": store.read_json("executor_state.json"),
        "review": store.read_json("review_queue.json"),
        "authorization": store.read_json("proactive_authorizations.json"),
        "proactive_intents": store.read_json("proactive_intents.json"),
        "rollback": store.read_json("rollback_state.json"),
        "state_changes": store.read_json("state_change_proposals.json"),
        "self_improvement": store.read_json(
            "self_improvement_proposals.json"
        ),
        "tool_calls": store.read_jsonl("tool_call_log.jsonl", limit=100_000),
        "model_calls": store.read_jsonl(
            "core_model_trace.jsonl",
            limit=100_000,
        ),
        "decisions": store.read_jsonl(
            "decision_trace.jsonl",
            limit=100_000,
        ),
        "policies": store.read_jsonl(
            "policy_trace.jsonl",
            limit=100_000,
        ),
        "execution": store.read_jsonl(
            "execution_trace.jsonl",
            limit=100_000,
        ),
        "alerts": store.read_jsonl("alert_log.jsonl", limit=100_000),
        "rollback_log": store.read_jsonl(
            "rollback_log.jsonl",
            limit=100_000,
        ),
    }


def event_count(store: WorldStateStore) -> int:
    events = store.read_json("event_inbox.json").get("events")
    return len(events) if isinstance(events, dict) else 0


def test_no_implicit_intent_from_registration_tick_or_rebind(
    root: Path,
) -> None:
    repo, _ = create_git_fixture(root / "git")
    store = WorldStateStore(root / "state")
    configure_mode(store, "record_only")
    fabric = ShadowAwarenessRuntime(store, mode="record_only")
    runtime = producer(store, fabric)

    created = register_goal(runtime, repo)
    after_register = deployment_signals(store)
    tick = runtime.run_once(reason="no_implicit_deployment_intent")
    after_tick = deployment_signals(store)
    rebound = register_goal(
        runtime,
        repo,
        expected_state_revision=int(created["state_revision"]),
        release_cycle="deployment_intent_smoke_rebound",
    )
    after_rebind = deployment_signals(store)

    expect(
        after_register == []
        and after_tick == []
        and after_rebind == []
        and tick["status"] == "success"
        and rebound["revision"] != created["revision"],
        (
            "Goal registration, producer tick, and semantic rebind never "
            "invent deployment intent"
        ),
        {
            "tick": tick,
            "created_revision": created["revision"],
            "rebound_revision": rebound["revision"],
        },
    )


def test_real_declare_dirty_shadow_candidate_and_withdraw(
    root: Path,
) -> None:
    repo, _ = create_git_fixture(root / "git")
    store = WorldStateStore(root / "state")
    configure_mode(store, "shadow")
    fabric = ShadowAwarenessRuntime(store, mode="shadow")
    fabric.configure("shadow")
    runtime = producer(store, fabric)
    goal = register_goal(runtime, repo)
    git_before = git_snapshot(repo)
    forbidden_before = forbidden_effect_snapshot(store)

    declared = record_intent(
        runtime,
        goal,
        transition="declare",
        operation_id="intent-real-declare",
        occurred_at=NOW,
    )
    intent_only = evaluate(store, now=NOW + timedelta(seconds=1))
    declared_signals = deployment_signals(store)
    expect(
        declared.get("signal_state") == "present"
        and declared.get("event_id")
        and len(declared_signals) == 1
        and declared_signals[0]["state"] == "present"
        and intent_only["candidate_count"] == 0,
        "an exact declaration records present intent but cannot qualify alone",
        {
            "record": declared,
            "signals": declared_signals,
            "evaluation": intent_only,
        },
    )

    (repo / "tracked.txt").write_text(
        "initial\nreal deployment-risk fixture\n",
        encoding="utf-8",
    )
    dirty = runtime.run_once(reason="real_dirty_plus_intent")
    guardian = ProjectGuardianRuntime(
        state_store=store,
        publish_event=fabric.publish,
        event_fabric_mode=lambda: {
            "mode": fabric.mode,
            "mode_epoch": fabric.mode_epoch,
        },
        clock=lambda: NOW + timedelta(minutes=1),
    )
    shadow = guardian.run_once(reason="real_deployment_intent_candidate")
    candidates = guardian.list_candidates(user_id=USER_ID)
    expect(
        dirty["status"] == "success"
        and dirty["published_count"] >= 1
        and any(
            observation.get("producer") == "git_dirty"
            and observation.get("signal_state") == "present"
            for observation in dirty.get("observations", [])
        )
        and shadow["status"] == "success"
        and shadow["candidate_count"] == 1
        and shadow["published_count"] == 1
        and len(candidates) == 1
        and candidates[0]["disposition"] == "admitted"
        and candidates[0]["shadow_only"] is True
        and candidates[0]["agent_invoked"] is False
        and candidates[0]["notification_allowed"] is False
        and candidates[0]["execution_allowed"] is False,
        "real dirty Git evidence plus declared intent produces one shadow-only candidate",
        {"dirty": dirty, "shadow": shadow, "candidates": candidates},
    )

    withdrawn = record_intent(
        runtime,
        goal,
        transition="withdraw",
        operation_id="intent-real-withdraw",
        occurred_at=NOW + timedelta(minutes=2),
    )
    after_withdraw = evaluate(store, now=NOW + timedelta(minutes=3))
    guardian._clock = lambda: NOW + timedelta(minutes=3)
    closed = guardian.run_once(reason="withdraw_closes_candidate")
    final_candidates = guardian.list_candidates(user_id=USER_ID)
    expect(
        withdrawn.get("signal_state") == "clear"
        and len(deployment_signals(store)) == 1
        and deployment_signals(store)[0]["state"] == "clear"
        and after_withdraw["candidate_count"] == 0
        and closed["candidate_count"] == 0
        and len(final_candidates) == 1
        and final_candidates[0]["disposition"] == "inactive",
        "withdraw records a clear frontier and closes the shadow candidate",
        {
            "withdraw": withdrawn,
            "evaluation": after_withdraw,
            "guardian": closed,
            "candidates": final_candidates,
        },
    )
    git_after = git_snapshot(repo)
    forbidden_after = forbidden_effect_snapshot(store)
    expect(
        git_after["head"] == git_before["head"]
        and git_after["ref"] == git_before["ref"]
        and git_after["index_sha256"] == git_before["index_sha256"]
        and forbidden_after == forbidden_before,
        (
            "intent correlation never invokes Agent, Review, notification, "
            "tool, execution, rollback, or Git mutation"
        ),
        {
            "git_before": git_before,
            "git_after": git_after,
            "forbidden_changed": forbidden_after != forbidden_before,
        },
    )


def test_operation_idempotency_and_semantic_conflict(root: Path) -> None:
    repo, _ = create_git_fixture(root / "git")
    store = WorldStateStore(root / "state")
    configure_mode(store, "record_only")
    fabric = ShadowAwarenessRuntime(store, mode="record_only")
    runtime = producer(store, fabric)
    goal = register_goal(runtime, repo)

    first = record_intent(
        runtime,
        goal,
        operation_id="intent-idempotent-operation",
        occurred_at=NOW,
    )
    event_total = event_count(store)
    frontier = copy.deepcopy(deployment_signals(store))
    replay = record_intent(
        runtime,
        goal,
        operation_id="intent-idempotent-operation",
        occurred_at=NOW,
    )
    persisted = json.dumps(
        all_state_snapshot(store),
        ensure_ascii=False,
        sort_keys=True,
    )
    expect(
        replay.get("event_id") == first.get("event_id")
        and replay.get("occurred_at") == first.get("occurred_at")
        and event_count(store) == event_total
        and deployment_signals(store) == frontier,
        "replaying one operation returns the same event and observation time",
        {"first": first, "replay": replay},
    )
    expect(
        "intent-idempotent-operation" not in persisted
        and SESSION_ID not in persisted,
        "raw operation and caller session identifiers are never persisted",
    )

    before_conflict = all_state_snapshot(store)
    git_before = git_snapshot(repo)
    expect_raises(
        ProjectGuardianGoalConflict,
        "one operation id cannot be reused for different intent semantics",
        lambda: record_intent(
            runtime,
            goal,
            transition="withdraw",
            operation_id="intent-idempotent-operation",
            occurred_at=NOW,
        ),
    )
    expect(
        all_state_snapshot(store) == before_conflict
        and git_snapshot(repo) == git_before,
        "operation-id conflict fails closed without state or Git mutation",
    )


def test_binding_status_time_and_mode_fail_closed(root: Path) -> None:
    def fixture(
        name: str,
        *,
        mode: str = "record_only",
        active_from: datetime | None = None,
        active_until: datetime | None = None,
    ) -> tuple[
        Path,
        WorldStateStore,
        ShadowAwarenessRuntime,
        ProjectGuardianProducerRuntime,
        dict[str, Any],
    ]:
        repo, _ = create_git_fixture(root / name / "git")
        store = WorldStateStore(root / name / "state")
        configure_mode(store, mode)
        fabric = ShadowAwarenessRuntime(store, mode=mode)
        runtime = producer(store, fabric)
        goal = register_goal(
            runtime,
            repo,
            active_from=active_from,
            active_until=active_until,
        )
        return repo, store, fabric, runtime, goal

    invalid_cases: list[
        tuple[str, Callable[[ProjectGuardianProducerRuntime, dict[str, Any]], Any]]
    ] = [
        (
            "wrong user",
            lambda runtime, goal: record_intent(
                runtime,
                goal,
                user_id="other-user",
                operation_id="intent-wrong-user",
            ),
        ),
        (
            "wrong Goal revision",
            lambda runtime, goal: record_intent(
                runtime,
                goal,
                goal_revision="wrong-revision",
                operation_id="intent-wrong-revision",
            ),
        ),
        (
            "stale Goal state revision",
            lambda runtime, goal: record_intent(
                runtime,
                goal,
                expected_goal_state_revision=(
                    int(goal["state_revision"]) + 1
                ),
                operation_id="intent-stale-state-revision",
            ),
        ),
        (
            "wrong target SHA",
            lambda runtime, goal: record_intent(
                runtime,
                goal,
                target_sha="f" * len(str(goal["target_sha"])),
                operation_id="intent-wrong-sha",
            ),
        ),
        (
            "wrong target environment",
            lambda runtime, goal: record_intent(
                runtime,
                goal,
                target_environment="staging",
                operation_id="intent-wrong-environment",
            ),
        ),
    ]
    failures: dict[str, Any] = {}
    for index, (label, action) in enumerate(invalid_cases):
        repo, store, _, runtime, goal = fixture(f"binding-{index}")
        before = all_state_snapshot(store)
        before_git = git_snapshot(repo)
        try:
            action(runtime, goal)
        except (ProjectGuardianGoalConflict, ValueError, KeyError):
            pass
        except Exception as exc:
            failures[label] = {
                "unexpected_error": f"{type(exc).__name__}: {exc}",
            }
        else:
            failures[label] = {"error": "request was accepted"}
        if all_state_snapshot(store) != before:
            failures.setdefault(label, {})["state_changed"] = True
        if git_snapshot(repo) != before_git:
            failures.setdefault(label, {})["git_changed"] = True

    paused_repo, paused_store, _, paused_runtime, paused_goal = fixture(
        "paused"
    )
    paused_goal = paused_runtime.set_release_goal_status(
        user_id=USER_ID,
        goal_id=GOAL_ID,
        expected_state_revision=int(paused_goal["state_revision"]),
        status="paused",
    )
    paused_before = all_state_snapshot(paused_store)
    paused_git = git_snapshot(paused_repo)
    try:
        record_intent(
            paused_runtime,
            paused_goal,
            operation_id="intent-paused-goal",
        )
    except (ProjectGuardianGoalConflict, ValueError, KeyError):
        pass
    else:
        failures["paused Goal"] = {"error": "request was accepted"}
    if all_state_snapshot(paused_store) != paused_before:
        failures.setdefault("paused Goal", {})["state_changed"] = True
    if git_snapshot(paused_repo) != paused_git:
        failures.setdefault("paused Goal", {})["git_changed"] = True

    expired_repo, expired_store, _, expired_runtime, expired_goal = fixture(
        "expired",
        active_from=NOW - timedelta(hours=2),
        active_until=NOW - timedelta(hours=1),
    )
    expired_before = all_state_snapshot(expired_store)
    expired_git = git_snapshot(expired_repo)
    try:
        record_intent(
            expired_runtime,
            expired_goal,
            operation_id="intent-expired-goal",
        )
    except (ProjectGuardianGoalConflict, ValueError, KeyError):
        pass
    else:
        failures["expired Goal"] = {"error": "request was accepted"}
    if all_state_snapshot(expired_store) != expired_before:
        failures.setdefault("expired Goal", {})["state_changed"] = True
    if git_snapshot(expired_repo) != expired_git:
        failures.setdefault("expired Goal", {})["git_changed"] = True

    bad_repo, bad_store, _, bad_runtime, bad_goal = fixture("bad-binding")

    def corrupt_binding(state: dict[str, Any]) -> None:
        binding = state["bindings"][GOAL_ID]
        binding["target_sha"] = "0" * len(str(bad_goal["target_sha"]))

    bad_store.mutate_json(bad_runtime.STATE_FILE, corrupt_binding)
    bad_before = all_state_snapshot(bad_store)
    bad_git = git_snapshot(bad_repo)
    try:
        record_intent(
            bad_runtime,
            bad_goal,
            operation_id="intent-bad-private-binding",
        )
    except (ProjectGuardianGoalConflict, ValueError, KeyError):
        pass
    else:
        failures["bad private binding"] = {"error": "request was accepted"}
    if all_state_snapshot(bad_store) != bad_before:
        failures.setdefault("bad private binding", {})[
            "state_changed"
        ] = True
    if git_snapshot(bad_repo) != bad_git:
        failures.setdefault("bad private binding", {})["git_changed"] = True

    expect(
        not failures,
        (
            "wrong identity, revision, SHA, environment, status, time, and "
            "private binding all fail closed"
        ),
        failures,
    )


def test_disabled_has_zero_intent_side_effects(root: Path) -> None:
    repo, _ = create_git_fixture(root / "git")
    store = WorldStateStore(root / "state")
    fabric = ShadowAwarenessRuntime(store, mode="disabled")
    runtime = producer(store, fabric)
    goal = register_goal(runtime, repo)
    state_before = all_state_snapshot(store)
    git_before = git_snapshot(repo)

    result = record_intent(
        runtime,
        goal,
        operation_id="intent-disabled",
    )
    expect(
        result.get("status") in {"disabled", "ignored"}
        and all_state_snapshot(store) == state_before
        and git_snapshot(repo) == git_before
        and signal_envelopes(store) == []
        and deployment_signals(store) == [],
        (
            "disabled mode records no operation, Inbox event, signal ledger "
            "entry, telemetry, or Git mutation"
        ),
        result,
    )


def test_ledger_failure_replay_repairs_same_operation(root: Path) -> None:
    repo, _ = create_git_fixture(root / "git")
    store = WorldStateStore(root / "state")
    configure_mode(store, "record_only")
    fabric = ShadowAwarenessRuntime(store, mode="record_only")
    runtime = producer(store, fabric)
    goal = register_goal(runtime, repo)

    original_record = fabric.project_guardian_signals.record_envelope
    failures = 0

    def fail_once(envelope: dict[str, Any]) -> dict[str, Any]:
        nonlocal failures
        failures += 1
        if failures == 1:
            raise OSError("injected intent ledger interruption")
        return original_record(envelope)

    fabric.project_guardian_signals.record_envelope = fail_once  # type: ignore[method-assign]
    try:
        first = record_intent(
            runtime,
            goal,
            operation_id="intent-ledger-repair",
        )
        before_replay_events = event_count(store)
        before_replay_frontier = deployment_signals(store)
        replay = record_intent(
            runtime,
            goal,
            operation_id="intent-ledger-repair",
        )
    finally:
        fabric.project_guardian_signals.record_envelope = original_record  # type: ignore[method-assign]

    expect(
        failures == 2
        and first.get("event_id") == replay.get("event_id")
        and first.get("occurred_at") == replay.get("occurred_at")
        and first.get("signal_ledger_status") == "degraded"
        and replay.get("signal_ledger_status") in {"recorded", "stale"}
        and before_replay_events == 1
        and event_count(store) == 1
        and before_replay_frontier == []
        and len(deployment_signals(store)) == 1
        and deployment_signals(store)[0]["state"] == "present",
        "replaying the same operation repairs a prior ledger-side interruption",
        {
            "first": first,
            "replay": replay,
            "signals": deployment_signals(store),
        },
    )


def test_reserved_intent_ingress_rejects_forgery(root: Path) -> None:
    repo, _ = create_git_fixture(root / "git")
    store = WorldStateStore(root / "state")
    configure_mode(store, "record_only")
    fabric = ShadowAwarenessRuntime(store, mode="record_only")
    runtime = producer(store, fabric)
    goal = register_goal(runtime, repo)
    state_before = all_state_snapshot(store)
    untrusted = fabric._publish_project_guardian_deployment_intent(
        ingress_capability=object(),
        user_id=USER_ID,
        goal_id=GOAL_ID,
        goal_revision=str(goal["revision"]),
        expected_goal_state_revision=int(goal["state_revision"]),
        target_sha=str(goal["target_sha"]),
        target_environment=TARGET_ENVIRONMENT,
        transition="declare",
        operation_digest="0" * 64,
        occurred_at=NOW.isoformat(),
        session_id=SESSION_ID,
    )
    generic = VeyraEvent(
        type=EventType.OBSERVATION,
        source=EventSource(
            channel=ProjectGuardianEvaluator.SIGNAL_CHANNEL,
            user_id=USER_ID,
            session_id=SESSION_ID,
        ),
        payload={
            "schema_version": ProjectGuardianEvaluator.SIGNAL_SCHEMA,
            "project_guardian_signal": {
                "kind": "deployment_intent",
                "state": "present",
            },
        },
        event_id="evt_generic_deployment_intent_forgery",
        timestamp=NOW.isoformat(),
        occurred_at=NOW.isoformat(),
        received_at=NOW.isoformat(),
        evidence_refs=[],
        privacy_scope="user",
    )
    generic_result = fabric.publish(generic)
    expect(
        untrusted.get("status") == "ignored"
        and untrusted.get("reason")
        == "untrusted_project_guardian_intent_ingress"
        and generic_result
        == {
            "status": "disabled",
            "event_id": generic.event_id,
            "reason": "reserved_project_guardian_signal_ingress",
        }
        and all_state_snapshot(store) == state_before,
        (
            "wrong capability and generic Event publish cannot enter the "
            "reserved deployment-intent channel"
        ),
        {"untrusted": untrusted, "generic": generic_result},
    )


def test_intent_ingress_fault_preserves_all_routes(root: Path) -> None:
    repo, _ = create_git_fixture(root / "git")
    normalizer = EventNormalizer()
    failures: dict[str, Any] = {}
    for case in OFFLINE_ROUTE_CASES:
        baseline_started_at = datetime.now(timezone.utc)
        baseline_loop = build_offline_route_loop(
            root / "routes" / case.case_id / "baseline",
            mode="record_only",
            case=case,
        )
        fault_started_at = datetime.now(timezone.utc)
        fault_loop = build_offline_route_loop(
            root / "routes" / case.case_id / "fault",
            mode="record_only",
            case=case,
        )
        configure_mode(baseline_loop.state_store, "record_only")
        configure_mode(fault_loop.state_store, "record_only")
        baseline_fabric = baseline_loop.event_awareness
        fault_fabric = fault_loop.event_awareness
        baseline_runtime = producer(
            baseline_loop.state_store,
            baseline_fabric,
            intent_publisher=lambda **kwargs: {
                "status": "ignored",
                "reason": "baseline_fixture",
                "request": copy.deepcopy(kwargs),
            },
        )
        baseline_goal = register_goal(
            baseline_runtime,
            repo,
            user_id="matrix-user",
            goal_id=f"goal_{case.case_id}",
        )
        callback_calls: list[dict[str, Any]] = []

        def failing_ingress(**kwargs: Any) -> dict[str, Any]:
            callback_calls.append(copy.deepcopy(kwargs))
            raise RuntimeError("injected deployment-intent ingress fault")

        fault_runtime = producer(
            fault_loop.state_store,
            fault_fabric,
            intent_publisher=failing_ingress,
        )
        fault_goal = register_goal(
            fault_runtime,
            repo,
            user_id="matrix-user",
            goal_id=f"goal_{case.case_id}",
        )
        del baseline_goal

        state_before = nonproducer_state_snapshot(fault_loop.state_store)
        git_before = git_snapshot(repo)
        fault_admission = record_intent(
            fault_runtime,
            fault_goal,
            operation_id=f"intent-route-fault-{case.case_id}",
            session_id=f"intent-route-{case.case_id}",
        )
        if not (
            fault_admission.get("status") == "degraded"
            and fault_admission.get("committed") is False
            and fault_admission.get("reason") == "signal_ingress_failed"
        ):
            failures[f"{case.case_id}:fault"] = {
                "admission": fault_admission,
            }
        if (
            len(callback_calls) != 1
            or nonproducer_state_snapshot(fault_loop.state_store)
            != state_before
            or git_snapshot(repo) != git_before
        ):
            failures[f"{case.case_id}:side_effects"] = {
                "callback_count": len(callback_calls),
                "state_changed": (
                    nonproducer_state_snapshot(fault_loop.state_store)
                    != state_before
                ),
                "git_changed": git_snapshot(repo) != git_before,
            }

        event = normalizer.user_message(
            case.text,
            "guardian-intent-route-matrix",
            "matrix-user",
            f"intent-{case.case_id}",
            event_id=f"evt_intent_matrix_{case.case_id}",
            correlation_id=f"corr-intent-matrix-{case.case_id}",
        )
        baseline_result = baseline_loop.handle_event(event)
        baseline_runtime_window = (
            baseline_started_at,
            datetime.now(timezone.utc),
        )
        fault_result = fault_loop.handle_event(event)
        fault_runtime_window = (
            fault_started_at,
            datetime.now(timezone.utc),
        )
        equivalent, differences = offline_public_outputs_equivalent(
            baseline_result,
            fault_result,
            require_distinct_generated_ids=True,
            left_runtime_window=baseline_runtime_window,
            right_runtime_window=fault_runtime_window,
        )
        if (
            not equivalent
            or baseline_result.route != case.route
            or fault_result.route != case.route
            or baseline_result.status != case.expected_status
            or fault_result.status != case.expected_status
            or baseline_result.risk_level != case.risk_level
            or fault_result.risk_level != case.risk_level
        ):
            failures[f"{case.case_id}:public_output"] = {
                "differences": differences,
                "baseline": baseline_result.to_dict(),
                "fault": fault_result.to_dict(),
            }

    expect(
        len(OFFLINE_ROUTE_CASES) == 9 and not failures,
        (
            "deployment-intent ingress faults preserve all nine Route "
            "outputs, status, risk, Agent/Review/notification/tool state, "
            "and Git HEAD/index/ref"
        ),
        failures,
    )


def main() -> int:
    with tempfile.TemporaryDirectory(
        prefix="veyra-project-guardian-deployment-intent-"
    ) as temporary:
        root = Path(temporary)
        test_no_implicit_intent_from_registration_tick_or_rebind(
            root / "no-implicit-intent"
        )
        test_real_declare_dirty_shadow_candidate_and_withdraw(
            root / "real-shadow-scenario"
        )
        test_operation_idempotency_and_semantic_conflict(
            root / "idempotency"
        )
        test_binding_status_time_and_mode_fail_closed(
            root / "binding-fail-closed"
        )
        test_disabled_has_zero_intent_side_effects(
            root / "disabled"
        )
        test_ledger_failure_replay_repairs_same_operation(
            root / "ledger-repair"
        )
        test_reserved_intent_ingress_rejects_forgery(
            root / "reserved-ingress"
        )
        test_intent_ingress_fault_preserves_all_routes(
            root / "route-non-interference"
        )

    print("Project Guardian deployment-intent smoke checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
