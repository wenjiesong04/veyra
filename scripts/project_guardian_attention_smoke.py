#!/usr/bin/env python3
from __future__ import annotations

import copy
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from awareness.project_guardian import ProjectGuardianEvaluator
from awareness.project_guardian_attention import (
    ProjectGuardianAttentionScheduler,
)


NOW = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
SCOPE = {
    "workspace_id": "ws_veyra_01",
    "repo_id": "wenjiesong04/veyra",
    "target_ref": "refs/heads/main",
    "target_environment": "production",
    "release_cycle": "release_2026_07_26",
}


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"PASS {label}")


def candidate(
    *,
    user_id: str = "user-a",
    goal_id: str = "goal-release-a",
    goal_revision: str = "goal-rev-1",
    signal_kinds: list[str] | None = None,
    unknowns: list[str] | None = None,
) -> dict[str, Any]:
    kinds = signal_kinds or [
        "ci_failed",
        "deployment_intent",
        "git_dirty",
    ]
    value: dict[str, Any] = {
        "schema_version": ProjectGuardianEvaluator.CANDIDATE_SCHEMA,
        "record_kind": "situation_candidate",
        "candidate_kind": ProjectGuardianEvaluator.CANDIDATE_KIND,
        "user_id": user_id,
        "goal_id": goal_id,
        "goal_revision": goal_revision,
        "scope": copy.deepcopy(SCOPE),
        "signal_kinds": sorted(kinds),
        "signals": [
            {
                "kind": kind,
                "source_component": ProjectGuardianEvaluator.SIGNAL_COMPONENTS[
                    kind
                ],
                "event_id": f"event-{kind}",
                "occurred_at": (NOW - timedelta(minutes=5)).isoformat(),
            }
            for kind in sorted(kinds)
        ],
        "signal_frontier": [],
        "evidence_refs": [],
        "source_sessions": ["session-a"],
        "why_now": {
            "reason_codes": [
                "active_project_release_goal",
                "multiple_independent_release_risk_signals",
                "deterministic_scope_and_time_match",
            ],
            "independent_signal_count": len(kinds),
            "correlation_window_seconds": 1800,
        },
        "unknowns": list(
            unknowns or ["release_approval", "rollback_readiness"]
        ),
        "candidate_advice": {
            "recommendation": "review_release_readiness",
            "checks": [
                {
                    "git_dirty": "review_uncommitted_changes",
                    "ci_failed": "inspect_and_resolve_ci_failure",
                    "deployment_intent": (
                        "confirm_release_target_and_rollback_plan"
                    ),
                }[kind]
                for kind in sorted(kinds)
            ],
        },
        "analysis_mode": "deterministic_read_only",
        "agent_invoked": False,
        "shadow_only": True,
        "notification_allowed": False,
        "execution_allowed": False,
        "interrupt_eligible": False,
        "qualified_at": (NOW - timedelta(minutes=5)).isoformat(),
        "transitioned_at": (NOW - timedelta(minutes=5)).isoformat(),
        "evaluated_at": NOW.isoformat(),
    }
    value["candidate_id"] = ProjectGuardianEvaluator.candidate_id_for(
        user_id=user_id,
        goal_id=goal_id,
        goal_revision=goal_revision,
        scope=value["scope"],
    )
    value["candidate_revision"] = (
        ProjectGuardianEvaluator.candidate_revision_for(value)
    )
    return value


def policy(
    value: dict[str, Any],
    *,
    attention_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    context = {
        "schema_version": (
            ProjectGuardianAttentionScheduler.CONTEXT_SCHEMA_VERSION
        ),
        "user_id": value["user_id"],
        "goal_id": value["goal_id"],
        "goal_revision": value["goal_revision"],
        "scope": copy.deepcopy(value["scope"]),
        "goal_priority": 0.95,
        "deadline_at": (NOW + timedelta(hours=1)).isoformat(),
        "timezone": "UTC",
        "notifications_paused": False,
        "quiet_hours": {
            "enabled": False,
            "start": "22:00",
            "end": "08:00",
        },
        "notification_budget": {
            "local_date": NOW.date().isoformat(),
            "limit": 4,
            "used": 0,
        },
        "dismissal": {"active": False},
        "cooldown": {"until": None},
        "novelty": {"last_seen_candidate_revision": None},
        "capabilities": {"read_only_investigation": True},
    }
    if attention_policy is not None:
        context["attention_policy"] = copy.deepcopy(attention_policy)
    return context


def assessment(
    scheduler: ProjectGuardianAttentionScheduler,
    value: dict[str, Any],
    context: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    result = scheduler.evaluate(
        candidates=[value],
        policy_context=context,
        now=NOW,
    )
    expect(result["assessment_count"] == 1, "candidate is assessed", result)
    return result, result["assessments"][0]


def threshold_policy(
    *,
    observe: float,
    investigate: float,
    suggest: float,
    act: float,
) -> dict[str, Any]:
    return {
        "observe_threshold": round(observe, 6),
        "investigate_threshold": round(investigate, 6),
        "suggest_threshold": round(suggest, 6),
        "act_threshold": round(act, 6),
        "default_cooldown_seconds": 3600,
    }


def main() -> None:
    scheduler = ProjectGuardianAttentionScheduler()
    stable_candidate = candidate()
    stable_policy = policy(stable_candidate)
    candidate_before = copy.deepcopy(stable_candidate)
    policy_before = copy.deepcopy(stable_policy)

    result, row = assessment(
        scheduler,
        stable_candidate,
        stable_policy,
    )
    expect(
        row["would_disposition"] == "would_suggest",
        "high release risk reaches counterfactual suggest",
        row,
    )
    expect(
        row["score"] is not None
        and row["score"] >= row["thresholds"]["suggest"],
        "score and threshold are explicit",
        row,
    )
    expect(
        set(row["components"])
        == set(ProjectGuardianAttentionScheduler.COMPONENT_NAMES)
        and all(
            item["status"] in {"known", "unknown"}
            for item in row["components"].values()
        ),
        "all attention components expose known or unknown",
        row["components"],
    )
    expect(
        row["notification_allowed"] is False
        and row["execution_allowed"] is False
        and row["interrupt_eligible"] is False
        and row["agent_invoked"] is False
        and row["shadow_only"] is True,
        "shadow authority locks remain closed",
        row,
    )
    expect(
        stable_candidate == candidate_before and stable_policy == policy_before,
        "pure evaluation does not mutate inputs",
    )
    expect(
        result["policy_version"]
        == ProjectGuardianAttentionScheduler.POLICY_VERSION
        and row["suppression"]["cooldown_seconds"]
        == ProjectGuardianAttentionScheduler.DEFAULT_COOLDOWN_SECONDS,
        "versioned deterministic policy defaults are visible",
        row,
    )

    repeat = scheduler.evaluate(
        candidates=[copy.deepcopy(stable_candidate)],
        policy_context=copy.deepcopy(stable_policy),
        now=NOW,
    )
    expect(
        repeat == result
        and repeat["assessments"][0]["assessment_revision"]
        == row["assessment_revision"],
        "identical inputs produce identical assessment and revision",
        repeat,
    )
    try:
        scheduler.evaluate(  # type: ignore[call-arg]
            candidates=[stable_candidate],
            policy_context=stable_policy,
        )
    except TypeError:
        missing_now_rejected = True
    else:
        missing_now_rejected = False
    expect(
        missing_now_rejected,
        "evaluation time is a required deterministic input",
    )

    revision_contexts: list[dict[str, Any]] = []
    changed_priority = copy.deepcopy(stable_policy)
    changed_priority["goal_priority"] = 0.9
    revision_contexts.append(changed_priority)
    changed_deadline = copy.deepcopy(stable_policy)
    changed_deadline["deadline_at"] = (NOW + timedelta(hours=2)).isoformat()
    revision_contexts.append(changed_deadline)
    changed_timezone = copy.deepcopy(stable_policy)
    changed_timezone["timezone"] = "Asia/Shanghai"
    changed_timezone["notification_budget"]["local_date"] = (
        NOW.astimezone(timezone(timedelta(hours=8))).date().isoformat()
    )
    revision_contexts.append(changed_timezone)
    changed_quiet = copy.deepcopy(stable_policy)
    changed_quiet["quiet_hours"] = {
        "enabled": False,
        "start": "21:00",
        "end": "07:00",
    }
    revision_contexts.append(changed_quiet)
    changed_budget = copy.deepcopy(stable_policy)
    changed_budget["notification_budget"]["used"] = 1
    revision_contexts.append(changed_budget)
    changed_dismissal = copy.deepcopy(stable_policy)
    changed_dismissal["dismissal"] = {
        "active": True,
        "suppression_key": scheduler.suppression_key_for(stable_candidate),
    }
    revision_contexts.append(changed_dismissal)
    changed_cooldown = copy.deepcopy(stable_policy)
    changed_cooldown["cooldown"] = {
        "until": (NOW + timedelta(hours=1)).isoformat(),
        "suppression_key": scheduler.suppression_key_for(stable_candidate),
    }
    revision_contexts.append(changed_cooldown)
    for changed_context in revision_contexts:
        _, changed_row = assessment(
            scheduler,
            stable_candidate,
            changed_context,
        )
        expect(
            changed_row["policy_revision"] != row["policy_revision"]
            and changed_row["assessment_id"] != row["assessment_id"],
            "decision context is bound into policy and assessment identity",
            changed_row,
        )

    for missing_key, component_name, blocker in (
        (
            "goal_priority",
            "goal_relevance",
            "missing_or_invalid_goal_priority",
        ),
        ("deadline_at", "urgency", "missing_or_invalid_deadline"),
        ("timezone", "urgency", "missing_or_invalid_timezone"),
        (
            "notification_budget",
            "interruption_cost",
            "notification_budget_unknown",
        ),
    ):
        incomplete = copy.deepcopy(stable_policy)
        incomplete.pop(missing_key)
        _, incomplete_row = assessment(
            scheduler,
            stable_candidate,
            incomplete,
        )
        expect(
            incomplete_row["components"][component_name]["status"] == "unknown"
            and incomplete_row["components"][component_name]["score"] is None
            and incomplete_row["score"] is None
            and incomplete_row["would_disposition"] == "suppressed"
            and blocker in incomplete_row["blockers"],
            f"missing {missing_key} remains unknown and blocks scoring",
            incomplete_row,
        )

    coerced_priority = copy.deepcopy(stable_policy)
    coerced_priority["goal_priority"] = "0.95"
    _, coerced_priority_row = assessment(
        scheduler,
        stable_candidate,
        coerced_priority,
    )
    expect(
        coerced_priority_row["components"]["goal_relevance"]["status"]
        == "unknown"
        and "missing_or_invalid_goal_priority"
        in coerced_priority_row["blockers"],
        "numeric strings are not coerced into Attention authority context",
        coerced_priority_row,
    )

    score = float(row["score"])
    boundary_cases = (
        (
            "observe",
            threshold_policy(
                observe=score,
                investigate=score + 0.02,
                suggest=score + 0.04,
                act=min(1.0, score + 0.06),
            ),
        ),
        (
            "investigate_read_only",
            threshold_policy(
                observe=score - 0.02,
                investigate=score,
                suggest=score + 0.02,
                act=min(1.0, score + 0.04),
            ),
        ),
        (
            "would_suggest",
            threshold_policy(
                observe=score - 0.04,
                investigate=score - 0.02,
                suggest=score,
                act=min(1.0, score + 0.02),
            ),
        ),
    )
    for expected, selected_policy in boundary_cases:
        boundary_context = policy(
            stable_candidate,
            attention_policy=selected_policy,
        )
        _, boundary_row = assessment(
            scheduler,
            stable_candidate,
            boundary_context,
        )
        expect(
            boundary_row["would_disposition"] == expected,
            f"{expected} threshold is inclusive",
            boundary_row,
        )
    act_context = policy(
        stable_candidate,
        attention_policy=threshold_policy(
            observe=score - 0.06,
            investigate=score - 0.04,
            suggest=score - 0.02,
            act=score,
        ),
    )
    _, act_row = assessment(scheduler, stable_candidate, act_context)
    expect(
        act_row["would_disposition"] == "would_suggest"
        and "act_threshold_has_no_authority_in_shadow"
        in act_row["reason_codes"]
        and act_row["execution_allowed"] is False,
        "act threshold remains a shadow suggestion with no authority",
        act_row,
    )
    below_context = policy(
        stable_candidate,
        attention_policy=threshold_policy(
            observe=score + 0.01,
            investigate=score + 0.03,
            suggest=score + 0.05,
            act=min(1.0, score + 0.07),
        ),
    )
    _, below_row = assessment(scheduler, stable_candidate, below_context)
    expect(
        below_row["would_disposition"] == "suppressed"
        and below_row["suppression"]["primary_reason"]
        == "below_observe_threshold",
        "below observe threshold is suppressed",
        below_row,
    )

    suppression_key = scheduler.suppression_key_for(stable_candidate)
    precedence = policy(stable_candidate)
    precedence.update(
        {
            "notifications_paused": True,
            "quiet_hours": {
                "enabled": True,
                "start": "00:00",
                "end": "23:59",
            },
            "notification_budget": {
                "local_date": NOW.date().isoformat(),
                "limit": 1,
                "used": 1,
            },
            "dismissal": {
                "active": True,
                "suppression_key": suppression_key,
            },
            "cooldown": {
                "until": (NOW + timedelta(hours=2)).isoformat(),
                "suppression_key": suppression_key,
            },
        }
    )
    expected_precedence = (
        ("user_paused", None),
        ("quiet_hours_active", "notifications_paused"),
        ("notification_budget_exhausted", "quiet_hours"),
        ("user_dismissed", "notification_budget"),
        ("cooldown_active", "dismissal"),
    )
    current = copy.deepcopy(precedence)
    for expected_reason, field_to_disable in expected_precedence:
        if field_to_disable == "notifications_paused":
            current["notifications_paused"] = False
        elif field_to_disable == "quiet_hours":
            current["quiet_hours"]["enabled"] = False
        elif field_to_disable == "notification_budget":
            current["notification_budget"]["used"] = 0
        elif field_to_disable == "dismissal":
            current["dismissal"] = {"active": False}
        _, suppressed_row = assessment(
            scheduler,
            stable_candidate,
            current,
        )
        expect(
            suppressed_row["would_disposition"] == "suppressed"
            and suppressed_row["suppression"]["primary_reason"]
            == expected_reason,
            f"suppression precedence selects {expected_reason}",
            suppressed_row,
        )

    foreign_candidates = [
        candidate(
            user_id=f"user-{index + 1}",
            goal_id=f"goal-release-{index + 1}",
            goal_revision=f"goal-rev-{index + 1}",
        )
        for index in range(
            ProjectGuardianAttentionScheduler.MAX_CANDIDATES + 8
        )
    ]
    isolated = scheduler.evaluate(
        candidates=[*foreign_candidates, stable_candidate],
        policy_context=stable_policy,
        now=NOW,
    )
    isolated_text = json.dumps(isolated, ensure_ascii=False, sort_keys=True)
    expect(
        isolated == result
        and isolated["assessment_count"] == 1
        and isolated["assessments"][0]["user_id"] == "user-a"
        and "user-1" not in isolated_text
        and "goal-release-1" not in isolated_text,
        "foreign-first candidates cannot starve or alter scoped output",
        isolated,
    )

    malicious_policy = copy.deepcopy(stable_policy)
    malicious_policy.update(
        {
            "notification_allowed": True,
            "execution_allowed": True,
            "interrupt_eligible": True,
        }
    )
    _, locked = assessment(
        scheduler,
        stable_candidate,
        malicious_policy,
    )
    expect(
        locked["notification_allowed"] is False
        and locked["execution_allowed"] is False
        and locked["interrupt_eligible"] is False,
        "policy input cannot override authority locks",
        locked,
    )

    unsafe_candidate = copy.deepcopy(stable_candidate)
    unsafe_candidate["execution_allowed"] = True
    unsafe_candidate["candidate_revision"] = (
        ProjectGuardianEvaluator.candidate_revision_for(unsafe_candidate)
    )
    rejected_unsafe = scheduler.evaluate(
        candidates=[unsafe_candidate],
        policy_context=stable_policy,
        now=NOW,
    )
    expect(
        rejected_unsafe["assessment_count"] == 0,
        "candidate with weakened authority lock is rejected",
        rejected_unsafe,
    )

    numeric_identity = copy.deepcopy(stable_candidate)
    numeric_identity["user_id"] = 1
    numeric_identity["candidate_id"] = (
        ProjectGuardianEvaluator.candidate_id_for(
            user_id=1,
            goal_id=numeric_identity["goal_id"],
            goal_revision=numeric_identity["goal_revision"],
            scope=numeric_identity["scope"],
        )
    )
    numeric_identity["candidate_revision"] = (
        ProjectGuardianEvaluator.candidate_revision_for(numeric_identity)
    )
    strict_identity = scheduler.evaluate(
        candidates=[numeric_identity],
        policy_context=stable_policy,
        now=NOW,
    )
    expect(
        strict_identity["assessment_count"] == 0,
        "non-string security identity is rejected instead of aliased",
        strict_identity,
    )

    mismatched_cooldown = copy.deepcopy(stable_policy)
    mismatched_cooldown["cooldown"] = {
        "until": (NOW + timedelta(hours=1)).isoformat(),
        "suppression_key": "pgas_other_user_or_situation",
    }
    _, mismatch_row = assessment(
        scheduler,
        stable_candidate,
        mismatched_cooldown,
    )
    expect(
        mismatch_row["components"]["cooldown"]["status"] == "unknown"
        and "cooldown_binding_mismatch" in mismatch_row["blockers"]
        and mismatch_row["score"] is None,
        "cooldown cannot cross suppression-key boundary",
        mismatch_row,
    )

    print(
        json.dumps(
            {
                "status": "pass",
                "schema_version": result["schema_version"],
                "ruleset_version": result["ruleset_version"],
                "base_disposition": row["would_disposition"],
                "base_score": row["score"],
                "authority": {
                    "notification_allowed": row["notification_allowed"],
                    "execution_allowed": row["execution_allowed"],
                    "interrupt_eligible": row["interrupt_eligible"],
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
