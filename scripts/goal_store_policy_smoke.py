#!/usr/bin/env python3
"""Deterministic contract checks for the shared durable Goal store policy."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.goal_store_policy import (  # noqa: E402
    GoalStoreCapacityError,
    GoalStoreIntegrityError,
    MAX_SHARED_GOALS,
    PROJECT_RELEASE_GOAL_KIND,
    PROJECT_RELEASE_GOAL_SCHEMA,
    PROJECT_RELEASE_GOAL_SOURCE,
    WORKSPACE_GOAL_KIND,
    WORKSPACE_GOAL_SCHEMA,
    WORKSPACE_GOAL_SOURCE,
    retain_shared_goals,
)


def expect(condition: bool, label: str) -> None:
    if not condition:
        raise AssertionError(label)
    print(f"PASS {label}")


def workspace_goal(index: int) -> dict:
    return {
        "schema_version": WORKSPACE_GOAL_SCHEMA,
        "goal_id": f"workspace-{index}",
        "kind": WORKSPACE_GOAL_KIND,
        "source": WORKSPACE_GOAL_SOURCE,
        "status": "active",
    }


def release_goal(index: int, status: str = "paused") -> dict:
    return {
        "schema_version": PROJECT_RELEASE_GOAL_SCHEMA,
        "goal_id": f"release-{index}",
        "kind": PROJECT_RELEASE_GOAL_KIND,
        "source": PROJECT_RELEASE_GOAL_SOURCE,
        "status": status,
    }


def main() -> int:
    mixed = [workspace_goal(1), release_goal(1)] + [
        {"goal_id": f"ordinary-{index}", "status": "active"}
        for index in range(MAX_SHARED_GOALS + 10)
    ]
    retained = retain_shared_goals(mixed)
    retained_ids = {str(item.get("goal_id") or "") for item in retained}
    expect(
        len(retained) == MAX_SHARED_GOALS
        and {"workspace-1", "release-1"} <= retained_ids,
        "active workspace and paused release Goals survive bounded retention",
    )
    expect(
        "ordinary-0" not in retained_ids
        and f"ordinary-{MAX_SHARED_GOALS + 9}" in retained_ids,
        "only the oldest ordinary Goal history is evicted",
    )
    expect(
        retain_shared_goals(retained) == retained,
        "Goal retention is deterministic and idempotent",
    )

    try:
        retain_shared_goals([workspace_goal(index) for index in range(101)])
    except GoalStoreCapacityError:
        pass
    else:
        raise AssertionError("protected Goal capacity failed open")
    expect(True, "protected Goal overflow fails closed")

    malformed = workspace_goal(1)
    malformed["schema_version"] = "forged"
    try:
        retain_shared_goals([malformed])
    except GoalStoreIntegrityError:
        pass
    else:
        raise AssertionError("reserved Goal descriptor failed open")
    expect(True, "reserved Goal descriptor mismatch fails closed")

    try:
        retain_shared_goals([workspace_goal(1), "not-a-goal"])
    except GoalStoreIntegrityError:
        pass
    else:
        raise AssertionError("malformed Goal collection failed open")
    expect(True, "malformed Goal collection fails closed")

    completed = release_goal(2, status="completed")
    crowded = [completed] + [workspace_goal(index) for index in range(100)]
    retained_crowded = retain_shared_goals(crowded)
    expect(
        completed not in retained_crowded
        and len(retained_crowded) == MAX_SHARED_GOALS,
        "completed release history yields capacity to active controlled Goals",
    )
    print("Shared Goal store policy smoke passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
