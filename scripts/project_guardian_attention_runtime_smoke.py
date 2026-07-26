#!/usr/bin/env python3
from __future__ import annotations

import copy
import tempfile
import threading
from datetime import timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

import sys

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.world_state import WorldStateStore
from awareness.project_guardian_attention import (
    ProjectGuardianAttentionScheduler,
)
from runtime.project_guardian import ProjectGuardianRuntime
from runtime.project_guardian_attention_runtime import (
    ProjectGuardianAttentionConflict,
    ProjectGuardianAttentionRuntime,
)
from scripts.event_driven_awareness_smoke import (
    OFFLINE_ROUTE_CASES,
    build_offline_route_loop,
    offline_public_outputs_equivalent,
)
from scripts.project_guardian_smoke import (
    NOW,
    SCOPE,
    configure_mode,
    enqueue_signal,
    goal,
)


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"PASS {label}")


def expect_raises(
    error_type: type[BaseException],
    label: str,
    call: Any,
) -> None:
    try:
        call()
    except error_type:
        print(f"PASS {label}")
        return
    raise AssertionError(f"{label}: expected {error_type.__name__}")


class BlockingAttentionScheduler(ProjectGuardianAttentionScheduler):
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def evaluate(self, **kwargs: Any) -> dict[str, Any]:
        self.started.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("blocking Attention scheduler timed out")
        return super().evaluate(**kwargs)


def fixture(
    root: Path,
) -> tuple[
    WorldStateStore,
    ProjectGuardianAttentionRuntime,
    list[dict[str, Any]],
]:
    store = WorldStateStore(root / "state")
    scopes = [
        copy.deepcopy(SCOPE),
        {
            **SCOPE,
            "workspace_id": "ws_attention_02",
            "repo_id": "wenjiesong04/veyra-second",
        },
        {
            **SCOPE,
            "workspace_id": "ws_attention_foreign",
            "repo_id": "wenjiesong04/veyra-foreign",
        },
    ]
    goals = [
        goal(
            goal_id="goal_attention_a",
            revision="attn-a1",
            scope=scopes[0],
        ),
        goal(
            goal_id="goal_attention_b",
            revision="attn-b1",
            scope=scopes[1],
        ),
        goal(
            goal_id="goal_attention_foreign",
            user_id="user-b",
            revision="attn-f1",
            scope=scopes[2],
        ),
    ]
    store.write_json(
        "user_goals.json",
        {"goals": copy.deepcopy(goals), "updated_at": NOW.isoformat()},
    )
    configure_mode(store, "record_only")
    for index, item in enumerate(goals):
        for kind in ("git_dirty", "ci_failed"):
            enqueue_signal(
                store,
                kind=kind,
                event_id=f"evt_attention_{index}_{kind}",
                user_id=str(item["user_id"]),
                goal_id=str(item["goal_id"]),
                goal_revision=str(item["revision"]),
                scope=copy.deepcopy(item["scope"]),
            )
    guardian = ProjectGuardianRuntime(
        state_store=store,
        publish_event=lambda event: {"status": "disabled"},
        event_fabric_mode=lambda: "record_only",
        clock=lambda: NOW,
    )
    guardian_result = guardian.run_once(reason="attention_runtime_smoke")
    expect(
        guardian_result.get("status") == "success"
        and guardian_result.get("candidate_count") == 3,
        "real Guardian evaluation persists three independent candidates",
        guardian_result,
    )
    return (
        store,
        ProjectGuardianAttentionRuntime(
            state_store=store,
            clock=lambda: NOW,
        ),
        goals,
    )


def set_policy(
    runtime: ProjectGuardianAttentionRuntime,
    item: dict[str, Any],
    *,
    group: str,
    expected_policy_revision: str | None = None,
    notifications_paused: bool = False,
    quiet_hours: dict[str, Any] | None = None,
    daily_budget: int = 3,
) -> dict[str, Any]:
    return runtime.set_policy(
        user_id=str(item["user_id"]),
        goal_id=str(item["goal_id"]),
        goal_revision=str(item["revision"]),
        expected_goal_state_revision=int(item["state_revision"]),
        attention_group_id=group,
        goal_priority=0.9,
        deadline_at=(NOW + timedelta(hours=2)).isoformat(),
        timezone_name="Asia/Shanghai",
        notifications_paused=notifications_paused,
        quiet_hours=copy.deepcopy(
            quiet_hours
            if quiet_hours is not None
            else {"enabled": False}
        ),
        daily_notification_budget=daily_budget,
        expected_policy_revision=expected_policy_revision,
    )


def forbidden_effects(store: WorldStateStore) -> dict[str, Any]:
    return {
        "event_inbox": store.read_json("event_inbox.json"),
        "situation": store.read_json("situation_state.json"),
        "agent": store.read_json("agent_config.json"),
        "task": store.read_json("task_state.json"),
        "review": store.read_json("review_queue.json"),
        "executor": store.read_json("executor_state.json"),
        "commitments": store.read_json("user_commitments.json"),
        "authorizations": store.read_json(
            "proactive_authorizations.json"
        ),
        "tool_calls": store.read_jsonl("tool_call_log.jsonl", limit=1000),
        "model_calls": store.read_jsonl(
            "core_model_trace.jsonl",
            limit=1000,
        ),
        "decisions": store.read_jsonl(
            "decision_trace.jsonl",
            limit=1000,
        ),
    }


def test_grouping_modes_and_authority(root: Path) -> None:
    store, runtime, goals = fixture(root)
    for item in goals[:2]:
        set_policy(runtime, item, group="release-group-main")
    set_policy(runtime, goals[2], group="release-group-main")
    before = forbidden_effects(store)
    result = runtime.run_once(reason="attention_grouping")
    after = forbidden_effects(store)
    user_rows = runtime.list_assessments(user_id="user-a")
    foreign_rows = runtime.list_assessments(user_id="user-b")
    user_groups = runtime.list_general_situations(user_id="user-a")
    foreign_groups = runtime.list_general_situations(user_id="user-b")

    expect(
        result.get("assessment_count") == 3
        and result.get("general_situation_count") == 1
        and len(user_rows) == 2
        and len(foreign_rows) == 1
        and len(user_groups) == 1
        and len(user_groups[0].get("candidate_refs") or []) == 2
        and foreign_groups == [],
        "explicit same-user group aggregates two candidates without cross-user association",
        {
            "result": result,
            "user_groups": user_groups,
            "foreign_groups": foreign_groups,
        },
    )
    expect(
        before == after
        and all(
            row.get("agent_invoked") is False
            and row.get("notification_allowed") is False
            and row.get("execution_allowed") is False
            and row.get("interrupt_eligible") is False
            for row in [*user_rows, *foreign_rows, *user_groups]
        ),
        "Attention writes only private shadow telemetry and keeps every authority lock closed",
    )

    state_before_disable = store.read_json(runtime.STATE_FILE)
    configure_mode(store, "disabled")
    disabled = runtime.run_once(reason="disabled_check")
    expect(
        disabled.get("status") == "disabled"
        and store.read_json(runtime.STATE_FILE) == state_before_disable,
        "disabled Attention performs no assessment persistence",
        disabled,
    )


def test_policy_cas_suppression_and_restart(root: Path) -> None:
    store, runtime, goals = fixture(root)
    item = goals[0]
    policy = set_policy(runtime, item, group="release-group-one")
    expect_raises(
        ValueError,
        "Attention policy rejects coerced quiet-hour clock values",
        lambda: set_policy(
            runtime,
            item,
            group="release-group-one",
            expected_policy_revision=str(policy["policy_revision"]),
            quiet_hours={
                "enabled": True,
                "start": 22,
                "end": "08:00",
            },
        ),
    )
    expect_raises(
        ProjectGuardianAttentionConflict,
        "stale Attention policy CAS cannot overwrite current policy",
        lambda: set_policy(
            runtime,
            item,
            group="release-group-one",
            expected_policy_revision="stale-policy",
        ),
    )
    paused_policy = set_policy(
        runtime,
        item,
        group="release-group-one",
        expected_policy_revision=str(policy["policy_revision"]),
        notifications_paused=True,
        quiet_hours={
            "enabled": True,
            "start": "00:00",
            "end": "23:59",
        },
        daily_budget=0,
    )
    paused = runtime.run_once(reason="pause_precedence")
    row = runtime.list_assessments(user_id="user-a")[0]
    expect(
        paused.get("status") == "success"
        and row.get("would_disposition") == "suppressed"
        and (row.get("suppression") or {}).get("primary_reason")
        == "user_paused",
        "runtime preserves pause before quiet-hours and budget precedence",
        row,
    )

    normal_policy = set_policy(
        runtime,
        item,
        group="release-group-one",
        expected_policy_revision=str(paused_policy["policy_revision"]),
    )
    del normal_policy
    runtime.run_once(reason="dismissal_seed")
    seeded = runtime.list_assessments(user_id="user-a")[0]
    suppression_key = str((seeded.get("suppression") or {})["key"])
    runtime.set_dismissal(
        user_id="user-a",
        suppression_key=suppression_key,
        dismissed=True,
    )
    runtime.run_once(reason="dismissed")
    dismissed = runtime.list_assessments(user_id="user-a")[0]
    expect(
        dismissed.get("would_disposition") == "suppressed"
        and (dismissed.get("suppression") or {}).get("primary_reason")
        == "user_dismissed",
        "explicit dismiss suppresses the exact candidate key",
        dismissed,
    )
    runtime.set_dismissal(
        user_id="user-a",
        suppression_key=suppression_key,
        dismissed=False,
    )
    runtime.run_once(reason="restored")
    restored = runtime.list_assessments(user_id="user-a")[0]
    expect(
        (restored.get("suppression") or {}).get("primary_reason")
        != "user_dismissed",
        "restore removes dismissal without granting authority",
        restored,
    )

    restarted = ProjectGuardianAttentionRuntime(
        state_store=store,
        clock=lambda: NOW,
    )
    restarted.run_once(reason="restart_one")
    stable_one = restarted.list_assessments(user_id="user-a")[0]
    restarted.run_once(reason="restart_two")
    stable_two = restarted.list_assessments(user_id="user-a")[0]
    expect(
        stable_one.get("assessment_revision")
        == stable_two.get("assessment_revision")
        and stable_one.get("assessment_id")
        == stable_two.get("assessment_id"),
        "restart and repeated fixed-time ticks converge idempotently",
    )


def test_corrupt_state_freezes(root: Path) -> None:
    store, runtime, _ = fixture(root)
    path = store.path_for(runtime.STATE_FILE)
    path.write_text("{broken", encoding="utf-8")
    before = path.read_bytes()
    result = runtime.run_once(reason="corrupt_attention")
    expect(
        result.get("status") == "degraded"
        and result.get("state_frozen") is True
        and path.read_bytes() == before,
        "corrupt Attention state freezes instead of inferring empty policy",
        result,
    )


def test_semantically_corrupt_policy_is_rejected(root: Path) -> None:
    store, runtime, goals = fixture(root)
    policy = set_policy(
        runtime,
        goals[0],
        group="release-group-semantic-corruption",
    )
    state = store.read_json(runtime.STATE_FILE)
    state["policies"][goals[0]["goal_id"]][
        "daily_notification_budget"
    ] = True
    store.write_json(runtime.STATE_FILE, state)
    before = forbidden_effects(store)
    result = runtime.run_once(reason="semantic_policy_corruption")
    after = forbidden_effects(store)
    expect(
        result.get("status") == "success"
        and result.get("rejected_policy_count") == 1
        and result.get("assessment_count") == 0
        and before == after,
        (
            "valid JSON with a coerced or digest-mismatched Attention policy "
            "is rejected without producing authority or an assessment"
        ),
        {"policy": policy, "result": result},
    )


def test_disable_epoch_fences_inflight_attention(root: Path) -> None:
    store, runtime, goals = fixture(root)
    set_policy(runtime, goals[0], group="release-group-mode-fence")
    blocking = BlockingAttentionScheduler()
    runtime.scheduler = blocking
    state_before = store.read_json(runtime.STATE_FILE)
    holder: dict[str, Any] = {}

    def run_attention() -> None:
        try:
            holder["result"] = runtime.run_once(
                reason="inflight_disable_fence"
            )
        except Exception as exc:
            holder["error"] = exc

    worker = threading.Thread(target=run_attention)
    worker.start()
    expect(
        blocking.started.wait(timeout=5),
        "in-flight Attention run reaches the controlled scheduler barrier",
    )
    configure_mode(store, "disabled")
    blocking.release.set()
    worker.join(timeout=5)
    result = holder.get("result")
    expect(
        not worker.is_alive()
        and "error" not in holder
        and isinstance(result, dict)
        and result.get("status") == "disabled"
        and result.get("reason") == "attention_mode_changed_during_run"
        and store.read_json(runtime.STATE_FILE) == state_before,
        (
            "disable mode_epoch fences an in-flight Attention tick before "
            "any assessment or run telemetry persists"
        ),
        {"result": result, "error": holder.get("error")},
    )


def test_policy_revision_fences_inflight_attention(root: Path) -> None:
    store, runtime, goals = fixture(root)
    policy = set_policy(
        runtime,
        goals[0],
        group="release-group-policy-fence",
    )
    blocking = BlockingAttentionScheduler()
    runtime.scheduler = blocking
    holder: dict[str, Any] = {}

    def run_attention() -> None:
        try:
            holder["result"] = runtime.run_once(
                reason="inflight_policy_fence"
            )
        except Exception as exc:
            holder["error"] = exc

    worker = threading.Thread(target=run_attention)
    worker.start()
    expect(
        blocking.started.wait(timeout=5),
        "in-flight Attention run reaches the policy-race barrier",
    )
    updated = set_policy(
        runtime,
        goals[0],
        group="release-group-policy-fence",
        expected_policy_revision=str(policy["policy_revision"]),
        notifications_paused=True,
    )
    state_after_policy = store.read_json(runtime.STATE_FILE)
    blocking.release.set()
    worker.join(timeout=5)
    result = holder.get("result")
    final_state = store.read_json(runtime.STATE_FILE)
    expect(
        not worker.is_alive()
        and "error" not in holder
        and isinstance(result, dict)
        and result.get("status") == "stale"
        and result.get("reason") == "attention_input_changed_during_run"
        and updated.get("notifications_paused") is True
        and final_state == state_after_policy
        and final_state.get("assessments") == {},
        (
            "a concurrent policy revision fences the old assessment before "
            "it can overwrite the new policy snapshot"
        ),
        {"result": result, "error": holder.get("error")},
    )


def test_all_routes_are_unchanged(root: Path) -> None:
    failures: dict[str, Any] = {}
    for case in OFFLINE_ROUTE_CASES:
        baseline = build_offline_route_loop(
            root / case.case_id / "baseline",
            mode="disabled",
            case=case,
        )
        fault = build_offline_route_loop(
            root / case.case_id / "fault",
            mode="disabled",
            case=case,
        )
        runtime = ProjectGuardianAttentionRuntime(
            state_store=fault.state_store,
            clock=lambda: NOW,
        )
        configure_mode(fault.state_store, "record_only")
        state_path = fault.state_store.path_for(runtime.STATE_FILE)
        state_path.write_text("{broken", encoding="utf-8")
        attention_result = runtime.run_once(reason="route_fault")
        event_id = f"evt_attention_route_{case.case_id}"
        event = case_event(case, event_id=event_id)
        baseline_result = baseline.handle_event(event)
        fault_result = fault.handle_event(event)
        equivalent, differences = offline_public_outputs_equivalent(
            baseline_result,
            fault_result,
            require_distinct_generated_ids=True,
        )
        if (
            attention_result.get("status") != "degraded"
            or not equivalent
            or baseline_result.route != case.route
            or fault_result.route != case.route
            or baseline_result.status != case.expected_status
            or fault_result.status != case.expected_status
            or baseline_result.risk_level != case.risk_level
            or fault_result.risk_level != case.risk_level
        ):
            failures[case.case_id] = {
                "attention": attention_result,
                "differences": differences,
                "baseline": baseline_result.to_dict(),
                "fault": fault_result.to_dict(),
            }
    expect(
        len(OFFLINE_ROUTE_CASES) == 9 and not failures,
        "Attention faults preserve all nine Route outputs, status, and risk",
        failures,
    )


def case_event(case: Any, *, event_id: str) -> Any:
    from interface.event_normalizer import EventNormalizer

    return EventNormalizer().user_message(
        case.text,
        "attention-route-smoke",
        "matrix-user",
        f"attention-{case.case_id}",
        event_id=event_id,
        correlation_id=f"corr-{event_id}",
    )


def main() -> int:
    with tempfile.TemporaryDirectory(
        prefix="veyra-project-guardian-attention-runtime-"
    ) as temporary:
        root = Path(temporary)
        test_grouping_modes_and_authority(root / "grouping")
        test_policy_cas_suppression_and_restart(root / "suppression")
        test_corrupt_state_freezes(root / "corrupt")
        test_semantically_corrupt_policy_is_rejected(
            root / "semantic-corruption"
        )
        test_disable_epoch_fences_inflight_attention(
            root / "mode-fence"
        )
        test_policy_revision_fences_inflight_attention(
            root / "policy-fence"
        )
        test_all_routes_are_unchanged(root / "routes")
    print("Project Guardian Attention runtime smoke checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
