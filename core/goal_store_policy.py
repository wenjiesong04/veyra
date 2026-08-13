"""Shared retention policy for the heterogeneous durable Goal store."""

from __future__ import annotations

from typing import Any


MAX_SHARED_GOALS = 100
WORKSPACE_GOAL_SCHEMA = "veyra.workspace_goal.v1"
WORKSPACE_GOAL_KIND = "workspace_observation"
WORKSPACE_GOAL_SOURCE = "workspace_goal_control"
PROJECT_RELEASE_GOAL_SCHEMA = "veyra.project_guardian_release_goal.v1"
PROJECT_RELEASE_GOAL_KIND = "project_release"
PROJECT_RELEASE_GOAL_SOURCE = "project_guardian_release_goal_registry"
PROTECTED_GOAL_STATUSES = frozenset({"active", "paused"})
_RESERVED_GOAL_CONTRACTS = {
    WORKSPACE_GOAL_SOURCE: {
        "schema": WORKSPACE_GOAL_SCHEMA,
        "kind": WORKSPACE_GOAL_KIND,
        "statuses": frozenset({"active"}),
    },
    PROJECT_RELEASE_GOAL_SOURCE: {
        "schema": PROJECT_RELEASE_GOAL_SCHEMA,
        "kind": PROJECT_RELEASE_GOAL_KIND,
        "statuses": frozenset({"active", "paused", "completed"}),
    },
}


class GoalStoreCapacityError(RuntimeError):
    """Raised when controlled Goals alone exceed the shared store budget."""


class GoalStoreIntegrityError(RuntimeError):
    """Raised rather than silently normalizing malformed shared Goal state."""


def goal_records(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(
        not isinstance(goal, dict) for goal in value
    ):
        raise GoalStoreIntegrityError("shared Goal records are invalid")
    records = list(value)
    for goal in records:
        source = str(goal.get("source") or "")
        contract = _RESERVED_GOAL_CONTRACTS.get(source)
        if contract is None:
            continue
        if (
            str(goal.get("schema_version") or "") != contract["schema"]
            or str(goal.get("kind") or "") != contract["kind"]
            or str(goal.get("status") or "").lower()
            not in contract["statuses"]
        ):
            raise GoalStoreIntegrityError(
                "reserved Goal record violates its durable contract"
            )
    return records


def is_protected_goal(goal: dict[str, Any]) -> bool:
    descriptor = (
        str(goal.get("schema_version") or ""),
        str(goal.get("kind") or ""),
        str(goal.get("source") or ""),
    )
    return (
        descriptor
        in {
            (
                WORKSPACE_GOAL_SCHEMA,
                WORKSPACE_GOAL_KIND,
                WORKSPACE_GOAL_SOURCE,
            ),
            (
                PROJECT_RELEASE_GOAL_SCHEMA,
                PROJECT_RELEASE_GOAL_KIND,
                PROJECT_RELEASE_GOAL_SOURCE,
            ),
        }
        and str(goal.get("status") or "").lower() in PROTECTED_GOAL_STATUSES
    )


def retain_shared_goals(
    goals: Any,
    *,
    maximum: int = MAX_SHARED_GOALS,
    touched_goal_id: str | None = None,
) -> list[dict[str, Any]]:
    """Keep controlled Goals and bound the remaining ordinary history.

    Controlled Goals carry durable bindings consumed by background runtimes.
    Silently evicting one would invalidate those bindings, so capacity fails
    closed when controlled records alone exceed the store budget. Ordinary
    Goal history retains its most recent records within the remaining budget.
    """

    if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 1:
        raise ValueError("shared Goal capacity must be a positive integer")
    records = goal_records(goals)
    selected_touched = str(touched_goal_id or "").strip()
    if selected_touched:
        matches = [
            index
            for index, goal in enumerate(records)
            if str(goal.get("goal_id") or "") == selected_touched
        ]
        if len(matches) != 1:
            raise GoalStoreIntegrityError(
                "touched Goal identity is missing or ambiguous"
            )
        touched = records.pop(matches[0])
        records.append(touched)
    indexed = list(enumerate(records))
    controlled_indexes = [
        index
        for index, goal in indexed
        if is_protected_goal(goal)
    ]
    if len(controlled_indexes) > maximum:
        raise GoalStoreCapacityError(
            "controlled Goal capacity exceeds shared store limit"
        )
    ordinary_indexes = [
        index
        for index, goal in indexed
        if not is_protected_goal(goal)
    ]
    remaining = maximum - len(controlled_indexes)
    retained_indexes = set(controlled_indexes)
    if remaining:
        retained_indexes.update(ordinary_indexes[-remaining:])
    retained = [goal for index, goal in indexed if index in retained_indexes]
    if selected_touched and not any(
        str(goal.get("goal_id") or "") == selected_touched
        for goal in retained
    ):
        raise GoalStoreCapacityError(
            "touched Goal could not be retained within capacity"
        )
    return retained


__all__ = [
    "GoalStoreCapacityError",
    "GoalStoreIntegrityError",
    "MAX_SHARED_GOALS",
    "PROJECT_RELEASE_GOAL_KIND",
    "PROJECT_RELEASE_GOAL_SCHEMA",
    "PROJECT_RELEASE_GOAL_SOURCE",
    "PROTECTED_GOAL_STATUSES",
    "WORKSPACE_GOAL_KIND",
    "WORKSPACE_GOAL_SCHEMA",
    "WORKSPACE_GOAL_SOURCE",
    "goal_records",
    "is_protected_goal",
    "retain_shared_goals",
]
