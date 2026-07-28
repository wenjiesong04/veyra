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

from core.world_state import WorldStateStore  # noqa: E402
from interface.event_normalizer import EventNormalizer  # noqa: E402
from interface.event_schema import Route  # noqa: E402
from runtime.self_heal_playbook import (  # noqa: E402
    PLAYBOOK_ID,
    STATE_SCHEMA_VERSION,
    OpenClawReconnectPlaybook,
)
from scripts.event_driven_awareness_smoke import (  # noqa: E402
    OFFLINE_ROUTE_CASES,
    build_offline_route_loop,
    offline_public_outputs_equivalent,
)


SCENARIOS: dict[str, tuple[str, str]] = {
    "disabled": ("disabled", "disabled"),
    "record_only": ("record_only", "record_only"),
    "shadow": ("shadow", "shadow_qualified"),
    "cooldown": ("scoped_canary", "cooldown"),
    "breaker": ("scoped_canary", "breaker_open"),
    "fault": ("shadow", "fault"),
}
OBSERVED_AT = "2026-07-28T01:00:00+00:00"


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail!r}")
    print(f"ok - {label}")


def configure_mode(store: WorldStateStore, mode: str) -> None:
    def update(config: dict[str, Any]) -> None:
        self_heal = config.setdefault("self_heal", {})
        if not isinstance(self_heal, dict):
            config["self_heal"] = self_heal = {}
        self_heal["openclaw_reconnect"] = {
            "mode": mode,
            "mode_epoch": 7,
            "allowed_modes": [
                "disabled",
                "record_only",
                "shadow",
                "scoped_canary",
            ],
        }

    store.mutate_json("ops_config.json", update)


def seed_scenario(
    store: WorldStateStore,
    scenario: str,
) -> OpenClawReconnectPlaybook:
    mode, expected_status = SCENARIOS[scenario]
    configure_mode(store, mode)
    playbook = OpenClawReconnectPlaybook(
        state_store=store,
        adapter_resolver=None,
    )
    state_path = store.path_for("self_heal_state.json")
    if scenario == "fault":
        state_path.write_text("{invalid-self-heal-state", encoding="utf-8")
        expect(
            playbook.status().get("status") == expected_status,
            "fault fixture is recognized by the controller",
        )
        return playbook

    record = playbook._new_record(  # noqa: SLF001 - canonical state fixture
        target_binding_digest="a" * 64,
        status=expected_status,
        mode=mode,
        mode_epoch=7,
    )
    record["identity_scope_digest"] = "b" * 64
    if scenario in {"cooldown", "breaker"}:
        attempt_count = 1 if scenario == "cooldown" else 2
        confirmation_times = (
            "2026-07-28T00:59:59+00:00",
            OBSERVED_AT,
        )
        confirmations: list[dict[str, Any]] = []
        for index, observed_at in enumerate(confirmation_times, start=1):
            round_id = f"obs_aaaaaaaaaaaaaaa{index}"
            confirmations.append(
                {
                    "qualifies": True,
                    "sources": [
                        {
                            "source": "openclaw_tcp_probe",
                            "status": "unavailable",
                            "passed": False,
                            "observed_at": observed_at,
                            "round_id": round_id,
                            "evidence_kind": "real_probe",
                            "error_code": None,
                        },
                        {
                            "source": "openclaw_gateway_protocol",
                            "status": "unavailable",
                            "passed": False,
                            "observed_at": observed_at,
                            "round_id": round_id,
                            "evidence_kind": "real_force_refresh",
                            "error_code": None,
                            "active_task_count": None,
                            "active_task_count_malformed": False,
                            "capability_snapshot": None,
                        },
                    ],
                    "round_id": round_id,
                    "observed_at": observed_at,
                }
            )
        last_confirmation = confirmations[-1]
        record.update(
            {
                "incident_id": "heal_aaaaaaaaaaaaaaaa",
                "attempt_count": attempt_count,
                "failure_confirmation_count": 2,
                "failure_confirmations": confirmations,
                "cooldown_until": (
                    "2099-01-01T00:00:00+00:00"
                    if scenario == "cooldown"
                    else None
                ),
                "breaker_open": scenario == "breaker",
                "operation": {
                    "operation_id": "healop_aaaaaaaaaaaaaaaa",
                    "state": "completed",
                    "attempt_number": attempt_count,
                    "claimed_at": OBSERVED_AT,
                    "completed_at": OBSERVED_AT,
                    "outcome": "failed",
                },
                "review_id": (
                    "rev_aaaaaaaaaaaa" if scenario == "breaker" else None
                ),
                "last_observation": last_confirmation,
                "last_verification": {
                    "passed": False,
                    "sources": [],
                    "round_id": "verify_aaaaaaaaaaaaaaaa",
                    "observed_at": OBSERVED_AT,
                },
                "last_outcome": "failed",
            }
        )

    def seed(state: dict[str, Any]) -> None:
        state["schema_version"] = STATE_SCHEMA_VERSION
        state["playbooks"] = {PLAYBOOK_ID: record}
        state["updated_at"] = OBSERVED_AT

    store.mutate_json("self_heal_state.json", seed)
    expect(
        playbook.status().get("status") == expected_status,
        f"{scenario} fixture is recognized by the controller",
    )
    return playbook


def route_event(case: Any) -> Any:
    return EventNormalizer().user_message(
        case.text,
        "self-heal-route-smoke",
        "matrix-user",
        f"self-heal-{case.case_id}",
        event_id=f"evt_self_heal_route_{case.case_id}",
        correlation_id=f"corr-self-heal-route-{case.case_id}",
    )


def run_matrix(root: Path) -> None:
    expect(
        len(OFFLINE_ROUTE_CASES) == len(Route) == 9
        and {case.route for case in OFFLINE_ROUTE_CASES} == set(Route),
        "offline fixtures cover all nine public Route branches",
    )
    failures: dict[str, Any] = {}
    comparisons = 0
    for case in OFFLINE_ROUTE_CASES:
        event = route_event(case)
        baseline_started = datetime.now(timezone.utc)
        baseline_loop = build_offline_route_loop(
            root / case.case_id / "baseline",
            mode="disabled",
            case=case,
        )
        baseline_result = baseline_loop.handle_event(event)
        baseline_window = (
            baseline_started,
            datetime.now(timezone.utc),
        )
        for scenario, (_mode, expected_self_heal_status) in SCENARIOS.items():
            candidate_started = datetime.now(timezone.utc)
            candidate_loop = build_offline_route_loop(
                root / case.case_id / scenario,
                mode="disabled",
                case=case,
            )
            controller = seed_scenario(
                candidate_loop.state_store,
                scenario,
            )
            state_path = candidate_loop.state_store.path_for(
                "self_heal_state.json"
            )
            state_before = state_path.read_bytes()
            status_before = controller.status()
            candidate_result = candidate_loop.handle_event(event)
            candidate_window = (
                candidate_started,
                datetime.now(timezone.utc),
            )
            status_after = controller.status()
            state_after = state_path.read_bytes()
            equivalent, differences = offline_public_outputs_equivalent(
                baseline_result,
                candidate_result,
                require_distinct_generated_ids=True,
                left_runtime_window=baseline_window,
                right_runtime_window=candidate_window,
            )
            comparisons += 1
            if (
                not equivalent
                or candidate_result.route != case.route
                or candidate_result.status != case.expected_status
                or candidate_result.risk_level != case.risk_level
                or status_before.get("status") != expected_self_heal_status
                or status_after != status_before
                or state_after != state_before
            ):
                failures[f"{case.case_id}:{scenario}"] = {
                    "differences": differences,
                    "baseline": baseline_result.to_dict(),
                    "candidate": candidate_result.to_dict(),
                    "self_heal_before": status_before,
                    "self_heal_after": status_after,
                    "state_unchanged": state_after == state_before,
                }

    expect(
        comparisons == 9 * len(SCENARIOS) and not failures,
        (
            "disabled, record-only, shadow, cooldown, breaker, and fault "
            "preserve every complete Route output, status, and risk"
        ),
        failures,
    )


def main() -> None:
    with TemporaryDirectory(prefix="veyra-self-heal-route-") as tmp:
        run_matrix(Path(tmp))
    print("self_heal_route_non_regression_smoke: ok")


if __name__ == "__main__":
    main()
