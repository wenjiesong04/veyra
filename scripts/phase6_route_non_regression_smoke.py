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
from interface.extension_spec import parse_extension_spec  # noqa: E402
from runtime.extension_spec_quarantine import (  # noqa: E402
    ExtensionSpecQuarantine,
)
from scripts.event_driven_awareness_smoke import (  # noqa: E402
    OFFLINE_ROUTE_CASES,
    build_offline_route_loop,
    offline_public_outputs_equivalent,
)
from scripts.phase6_extension_spec_contract_smoke import (  # noqa: E402
    valid_spec,
)


MODES = ("disabled", "record_only", "shadow")
SCENARIOS = (
    "collaboration_populated",
    "collaboration_corrupt",
    "extension_populated",
    "extension_corrupt",
)


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"PASS {label}")


def seed_phase6(loop: Any, scenario: str) -> None:
    collaboration_path = loop.state_store.path_for(
        "phase6_collaboration_state.json"
    )
    extension_path = loop.state_store.path_for(
        "phase6_extension_spec_state.json"
    )
    if scenario == "collaboration_corrupt":
        collaboration_path.write_text(
            "{invalid-phase6-collaboration-state",
            encoding="utf-8",
        )
        return
    if scenario == "extension_corrupt":
        extension_path.write_text(
            "{invalid-phase6-extension-state",
            encoding="utf-8",
        )
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

    if scenario == "collaboration_populated":
        loop.state_store.mutate_json(
            "phase6_collaboration_state.json", mutate
        )
        return

    def mutate_extension(state: dict[str, Any]) -> None:
        created = datetime.now(timezone.utc)
        payload = valid_spec(now=created)
        payload["extension_id"] = "example.route_non_regression"
        spec = parse_extension_spec(payload)
        user_id = "private-user"
        workspace_id = "private-workspace"
        identity_key = ExtensionSpecQuarantine._identity_key(
            user_id,
            workspace_id,
            spec.extension_id,
            spec.version,
        )
        candidate_id = ExtensionSpecQuarantine._candidate_id(
            identity_key,
            spec.digest(),
        )
        operation_key = ExtensionSpecQuarantine._operation_key(
            user_id,
            workspace_id,
            "private-route-fixture",
        )
        recorded_at = created.isoformat()
        state.update(
            {
                "schema_version": (
                    "veyra.phase6.extension_spec_state.v1"
                ),
                "candidates": {
                    candidate_id: {
                        "schema_version": (
                            "veyra.phase6.extension_spec_record.v1"
                        ),
                        "candidate_id": candidate_id,
                        "spec": spec.canonical_dict(),
                        "spec_digest": spec.digest(),
                        "user_id": user_id,
                        "workspace_id": workspace_id,
                        "extension_id": spec.extension_id,
                        "extension_version": spec.version,
                        "stage": "SPEC_QUARANTINED",
                        "revision": 1,
                        "expires_at": spec.expires_at,
                        "review": None,
                        "revocation": None,
                        "history": [
                            {
                                "revision": 1,
                                "transition": "spec_quarantined",
                                "recorded_at": recorded_at,
                            }
                        ],
                        "created_at": recorded_at,
                        "updated_at": recorded_at,
                    }
                },
                "identity_index": {
                    identity_key: candidate_id,
                },
                "operation_index": {
                    operation_key: {
                        "request_digest": (
                            ExtensionSpecQuarantine._digest(
                                {"fixture": "route"}
                            )
                        ),
                        "kind": "quarantine",
                        "candidate_id": candidate_id,
                        "result_revision": 1,
                        "result_stage": "SPEC_QUARANTINED",
                        "owner_scope_digest": (
                            ExtensionSpecQuarantine._owner_scope_digest(
                                user_id,
                                workspace_id,
                            )
                        ),
                        "terminal_control": False,
                        "recorded_at": recorded_at,
                    }
                },
                "candidate_count": 1,
            }
        )

    if scenario == "extension_populated":
        loop.state_store.mutate_json(
            "phase6_extension_spec_state.json",
            mutate_extension,
        )
        status = ExtensionSpecQuarantine(
            state_store=loop.state_store
        ).status()
        if status["operational_health"] != "available":
            raise AssertionError(
                f"invalid extension route fixture: {status!r}"
            )
        return
    raise ValueError(f"unknown Phase 6 scenario: {scenario}")


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
            "phase6_extension_spec_state": {
                "candidates": {
                    "private-extension": {
                        "purpose": "private extension purpose",
                        "user_id": "private-user",
                        "workspace_id": "private-workspace",
                    }
                }
            },
        }
    )
    expect(
        "phase6_collaboration_state" not in public_state
        and "phase6_extension_spec_state" not in public_state
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
            "isolated populated or corrupt collaboration/extension state "
            "cannot weaken complete "
            "disabled, record-only, or shadow Route output/status/risk"
        ),
        failures,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
