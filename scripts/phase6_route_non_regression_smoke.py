#!/usr/bin/env python3
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from interface.event_normalizer import EventNormalizer  # noqa: E402
from interface.event_schema import Route  # noqa: E402
from routers.debug_audit import _public_state  # noqa: E402
from scripts.event_driven_awareness_smoke import (  # noqa: E402
    OFFLINE_ROUTE_CASES,
    build_offline_route_loop,
    offline_public_outputs_equivalent,
)


MODES = ("disabled", "record_only", "shadow")
SCENARIOS = ("populated", "corrupt")


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"PASS {label}")


def seed_phase6(loop: Any, scenario: str) -> None:
    path = loop.state_store.path_for(
        "phase6_collaboration_state.json"
    )
    if scenario == "corrupt":
        path.write_text("{invalid-phase6-state", encoding="utf-8")
        return

    def mutate(state: dict[str, Any]) -> None:
        state.update(
            {
                "schema_version": (
                    "veyra.phase6.collaboration_state.v1"
                ),
                "collaborations": {
                    "case_phase6_route_non_regression": {
                        "case_id": (
                            "case_phase6_route_non_regression"
                        ),
                        "status": "PROPOSED",
                        "participants": [
                            {
                                "participant_id": "p6_primary",
                                "role": "primary_analyst",
                            },
                            {
                                "participant_id": "p6_critic",
                                "role": "critic",
                            },
                        ],
                        "dispatches": {},
                        "budgets": {
                            "agent_calls_claimed": 2,
                            "handoffs_claimed": 1,
                            "evidence_patches_claimed": 0,
                        },
                    }
                },
                "event_index": {
                    "event_phase6_route_non_regression": (
                        "case_phase6_route_non_regression"
                    )
                },
                "collaboration_count": 1,
            }
        )

    loop.state_store.mutate_json(
        "phase6_collaboration_state.json", mutate
    )


def route_event(case: Any, mode: str, scenario: str) -> Any:
    return EventNormalizer().user_message(
        case.text,
        "phase6-route-smoke",
        "matrix-user",
        f"phase6-{mode}-{scenario}-{case.case_id}",
        event_id=f"evt_phase6_{mode}_{scenario}_{case.case_id}",
        correlation_id=(
            f"corr-phase6-{mode}-{scenario}-{case.case_id}"
        ),
    )


def main() -> int:
    public_state = _public_state(
        {
            "local_world": {"current_project": "public-safe"},
            "phase6_collaboration_state": {
                "collaborations": {
                    "private-case": {
                        "goal": "private goal",
                        "workspace_id": "private-workspace",
                        "session_id": "private-session",
                    }
                }
            },
        }
    )
    expect(
        "phase6_collaboration_state" not in public_state
        and public_state.get("local_world", {}).get(
            "current_project"
        )
        == "public-safe",
        "generic state projection omits the private Phase 6 graph",
        public_state,
    )
    expect(
        len(OFFLINE_ROUTE_CASES) == len(Route) == 9
        and {case.route for case in OFFLINE_ROUTE_CASES} == set(Route),
        "fixtures cover all nine public routes",
    )
    failures: dict[str, Any] = {}
    comparisons = 0
    with TemporaryDirectory(
        prefix="veyra-phase6-route-non-regression-"
    ) as raw:
        root = Path(raw)
        for case in OFFLINE_ROUTE_CASES:
            for mode in MODES:
                for scenario in SCENARIOS:
                    event = route_event(case, mode, scenario)
                    baseline_started = datetime.now(timezone.utc)
                    baseline = build_offline_route_loop(
                        root
                        / case.case_id
                        / mode
                        / scenario
                        / "baseline",
                        mode=mode,
                        case=case,
                    ).handle_event(event)
                    baseline_window = (
                        baseline_started,
                        datetime.now(timezone.utc),
                    )
                    candidate_started = datetime.now(timezone.utc)
                    candidate_loop = build_offline_route_loop(
                        root
                        / case.case_id
                        / mode
                        / scenario
                        / "candidate",
                        mode=mode,
                        case=case,
                    )
                    seed_phase6(candidate_loop, scenario)
                    candidate = candidate_loop.handle_event(event)
                    candidate_window = (
                        candidate_started,
                        datetime.now(timezone.utc),
                    )
                    equivalent, differences = (
                        offline_public_outputs_equivalent(
                            baseline,
                            candidate,
                            require_distinct_generated_ids=True,
                            left_runtime_window=baseline_window,
                            right_runtime_window=candidate_window,
                        )
                    )
                    comparisons += 1
                    if (
                        not equivalent
                        or candidate.route != case.route
                        or candidate.status != case.expected_status
                        or candidate.risk_level != case.risk_level
                    ):
                        failures[
                            f"{case.case_id}:{mode}:{scenario}"
                        ] = {
                            "differences": differences,
                            "baseline": baseline.to_dict(),
                            "candidate": candidate.to_dict(),
                        }
    expect(
        comparisons == 9 * len(MODES) * len(SCENARIOS)
        and not failures,
        (
            "populated or corrupt Phase 6 state cannot weaken complete "
            "disabled, record-only, or shadow Route output/status/risk"
        ),
        failures,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
