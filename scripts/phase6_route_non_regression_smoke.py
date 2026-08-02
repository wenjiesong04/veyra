#!/usr/bin/env python3
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
import hashlib
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
from interface.extension_artifact import (  # noqa: E402
    EXTENSION_ARTIFACT_POLICY_REVISION,
    EXTENSION_ARTIFACT_SCHEMA_VERSION,
    artifact_owner_scope_digest,
    encode_artifact_content,
)
from runtime.extension_artifact_quarantine import (  # noqa: E402
    ExtensionArtifactQuarantine,
)
from runtime.extension_spec_quarantine import (  # noqa: E402
    ExtensionSpecQuarantine,
)
from runtime.extension_source_policy_gate import (  # noqa: E402
    ExtensionSourcePolicyGate,
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
    "extension_artifact_populated",
    "extension_artifact_corrupt",
    "extension_source_check_populated",
    "extension_source_check_corrupt",
    "extension_isolated_runner_populated",
    "extension_isolated_runner_corrupt",
    "general_situation_populated",
    "general_situation_corrupt",
    "suggestion_outbox_populated",
    "suggestion_outbox_corrupt",
    "extension_generation_populated",
    "extension_generation_corrupt",
    "extension_dynamic_validation_populated",
    "extension_dynamic_validation_corrupt",
    "extension_release_populated",
    "extension_release_corrupt",
    "extension_deployment_populated",
    "extension_deployment_corrupt",
    "extension_pipeline_populated",
    "extension_pipeline_corrupt",
    "capability_gap_populated",
    "capability_gap_corrupt",
)

PRIVATE_STATE_SCENARIOS: dict[str, tuple[str, str, str]] = {
    "general_situation": (
        "general_situation_state.json",
        "general_situations",
        "veyra.general_situation_state.v1",
    ),
    "suggestion_outbox": (
        "suggestion_outbox.json",
        "proposals",
        "veyra.suggestion_outbox.v1",
    ),
    "extension_generation": (
        "phase6_extension_generation_state.json",
        "generations",
        "veyra.phase6.extension_generation_state.v1",
    ),
    "extension_dynamic_validation": (
        "phase6_extension_dynamic_validation_state.json",
        "validations",
        "veyra.phase6.extension_dynamic_validation_state.v1",
    ),
    "extension_release": (
        "phase6_extension_release_state.json",
        "releases",
        "veyra.phase6.extension_release_state.v1",
    ),
    "extension_deployment": (
        "phase6_extension_deployment_state.json",
        "deployments",
        "veyra.phase6.extension_deployment_state.v1",
    ),
    "extension_pipeline": (
        "phase6_extension_pipeline_state.json",
        "pipelines",
        "veyra.phase6.extension_pipeline_state.v1",
    ),
    "capability_gap": (
        "phase6_capability_gap_state.json",
        "gaps",
        "veyra.phase6.capability_gap_state.v1",
    ),
}


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
    artifact_path = loop.state_store.path_for(
        "phase6_extension_artifact_state.json"
    )
    source_check_path = loop.state_store.path_for(
        "phase6_extension_source_check_state.json"
    )
    isolated_runner_path = loop.state_store.path_for(
        "phase6_extension_isolated_runner_state.json"
    )
    for scenario_prefix, (state_file, collection, schema) in (
        PRIVATE_STATE_SCENARIOS.items()
    ):
        if scenario == f"{scenario_prefix}_corrupt":
            loop.state_store.path_for(state_file).write_text(
                f"{{invalid-{scenario_prefix.replace('_', '-')}-state",
                encoding="utf-8",
            )
            return
        if scenario == f"{scenario_prefix}_populated":
            recorded_at = datetime.now(timezone.utc).isoformat(
                timespec="microseconds"
            ).replace("+00:00", "Z")

            def populate_private_state(
                state: dict[str, Any],
                *,
                selected_collection: str = collection,
                selected_schema: str = schema,
                selected_prefix: str = scenario_prefix,
            ) -> None:
                # This is deliberately inert private data. The route matrix
                # tests isolation from pre-existing lifecycle records; it
                # must not construct a model, runner, signer, or deployment
                # gate merely to seed their durable stores.
                records = (
                    state.get(selected_collection)
                    if isinstance(state.get(selected_collection), dict)
                    else {}
                )
                records["private-route-fixture"] = {
                    "schema_version": (
                        f"veyra.phase6.{selected_prefix}.private_fixture.v1"
                    ),
                    "user_id": "private-user",
                    "workspace_id": "private-workspace",
                    "session_id": "private-session",
                    "status": "private_fixture_only",
                    "created_at": recorded_at,
                }
                state["schema_version"] = selected_schema
                state[selected_collection] = records
                for count_field in (
                    "general_situation_count",
                    "proposal_count",
                    "generation_count",
                    "validation_count",
                ):
                    if count_field in state:
                        state[count_field] = len(records)
                if "registry_revision" in state:
                    state["registry_revision"] = 1
                if "revision" in state:
                    state["revision"] = 1
                if "updated_at" in state:
                    state["updated_at"] = recorded_at

            loop.state_store.mutate_json(
                state_file,
                populate_private_state,
            )
            return
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
    if scenario == "extension_artifact_corrupt":
        artifact_path.write_text(
            "{invalid-phase6-extension-artifact-state",
            encoding="utf-8",
        )
        return
    if scenario == "extension_source_check_corrupt":
        source_check_path.write_text(
            "{invalid-phase6-extension-source-check-state",
            encoding="utf-8",
        )
        return
    if scenario == "extension_isolated_runner_corrupt":
        isolated_runner_path.write_text(
            "{invalid-phase6-extension-isolated-runner-state",
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
    if scenario == "extension_artifact_populated":
        now = datetime.now(timezone.utc)
        user_id = "private-artifact-user"
        workspace_id = str(
            loop.state_store.read_json("local_world.json").get(
                "current_project"
            )
            or ""
        )
        payload = valid_spec(now=now)
        payload["extension_id"] = "example.route_artifact"
        spec = parse_extension_spec(payload)
        spec_quarantine = ExtensionSpecQuarantine(
            state_store=loop.state_store
        )
        candidate = spec_quarantine.quarantine(
            spec=spec,
            expected_spec_digest=spec.digest(),
            user_id=user_id,
            workspace_id=workspace_id,
            operation_id="route-artifact-spec",
        )
        candidate = spec_quarantine.review(
            candidate_id=candidate["candidate_id"],
            user_id=user_id,
            workspace_id=workspace_id,
            expected_revision=candidate["candidate_revision"],
            operation_id="route-artifact-review",
            decision="accept_for_future_isolated_generation",
            reason="route isolation fixture only",
        )
        source = (
            b"def route_isolation_fixture(value: str) -> str:\n"
            b"    return value\n"
        )
        artifact = ExtensionArtifactQuarantine(
            state_store=loop.state_store,
            spec_quarantine=spec_quarantine,
        )
        artifact.submit(
            envelope={
                "schema_version": EXTENSION_ARTIFACT_SCHEMA_VERSION,
                "artifact_kind": "python_source_utf8",
                "candidate_id": candidate["candidate_id"],
                "candidate_revision": candidate["candidate_revision"],
                "owner_scope_digest": artifact_owner_scope_digest(
                    user_id,
                    workspace_id,
                ),
                "extension_id": candidate["extension_id"],
                "extension_version": candidate["extension_version"],
                "spec_digest": candidate["spec_digest"],
                "extension_policy_revision": (
                    spec.tcb_policy.policy_revision
                ),
                "artifact_policy_revision": (
                    EXTENSION_ARTIFACT_POLICY_REVISION
                ),
                "artifact_sha256": hashlib.sha256(source).hexdigest(),
                "size_bytes": len(source),
                "content_b64url": encode_artifact_content(source),
                "expires_at": (
                    now + timedelta(days=3)
                ).isoformat(timespec="microseconds").replace(
                    "+00:00",
                    "Z",
                ),
            },
            expected_artifact_sha256=hashlib.sha256(source).hexdigest(),
            user_id=user_id,
            workspace_id=workspace_id,
            operation_id="route-artifact-submit",
        )
        status = artifact.status()
        if status["operational_health"] != "available":
            raise AssertionError(
                f"invalid artifact route fixture: {status!r}"
            )
        return
    if scenario == "extension_source_check_populated":
        now = datetime.now(timezone.utc)
        user_id = "private-source-check-user"
        workspace_id = str(
            loop.state_store.read_json("local_world.json").get(
                "current_project"
            )
            or ""
        )
        payload = valid_spec(now=now)
        payload["extension_id"] = "example.route_source_check"
        payload["dependencies"] = []
        spec = parse_extension_spec(payload)
        spec_quarantine = ExtensionSpecQuarantine(
            state_store=loop.state_store
        )
        candidate = spec_quarantine.quarantine(
            spec=spec,
            expected_spec_digest=spec.digest(),
            user_id=user_id,
            workspace_id=workspace_id,
            operation_id="route-source-check-spec",
        )
        candidate = spec_quarantine.review(
            candidate_id=candidate["candidate_id"],
            user_id=user_id,
            workspace_id=workspace_id,
            expected_revision=candidate["candidate_revision"],
            operation_id="route-source-check-review",
            decision="accept_for_future_isolated_generation",
            reason="route isolation source-check fixture only",
        )
        source = (
            b"def run_extension(payload):\n"
            b'    return {"label": payload["name"]}\n'
        )
        source_sha256 = hashlib.sha256(source).hexdigest()
        artifact_quarantine = ExtensionArtifactQuarantine(
            state_store=loop.state_store,
            spec_quarantine=spec_quarantine,
        )
        artifact = artifact_quarantine.submit(
            envelope={
                "schema_version": EXTENSION_ARTIFACT_SCHEMA_VERSION,
                "artifact_kind": "python_source_utf8",
                "candidate_id": candidate["candidate_id"],
                "candidate_revision": candidate["candidate_revision"],
                "owner_scope_digest": artifact_owner_scope_digest(
                    user_id,
                    workspace_id,
                ),
                "extension_id": candidate["extension_id"],
                "extension_version": candidate["extension_version"],
                "spec_digest": candidate["spec_digest"],
                "extension_policy_revision": (
                    spec.tcb_policy.policy_revision
                ),
                "artifact_policy_revision": (
                    EXTENSION_ARTIFACT_POLICY_REVISION
                ),
                "artifact_sha256": source_sha256,
                "size_bytes": len(source),
                "content_b64url": encode_artifact_content(source),
                "expires_at": (
                    now + timedelta(days=3)
                ).isoformat(timespec="microseconds").replace(
                    "+00:00",
                    "Z",
                ),
            },
            expected_artifact_sha256=source_sha256,
            user_id=user_id,
            workspace_id=workspace_id,
            operation_id="route-source-check-artifact",
        )
        gate = ExtensionSourcePolicyGate(
            state_store=loop.state_store,
            artifact_quarantine=artifact_quarantine,
        )
        result = gate.start(
            artifact_id=artifact["artifact_id"],
            user_id=user_id,
            workspace_id=workspace_id,
            expected_artifact_revision=artifact[
                "artifact_revision"
            ],
            expected_artifact_sha256=artifact[
                "artifact_sha256"
            ],
            operation_id="route-source-check-start",
        )
        status = gate.status()
        if (
            result["source_check_status"] != "passed"
            or status["operational_health"] != "available"
            or status["storage"]["check_count"] != 1
        ):
            raise AssertionError(
                "invalid source-check route fixture: "
                f"{result!r} {status!r}"
            )
        return
    if scenario == "extension_isolated_runner_populated":
        recorded_at = datetime.now(timezone.utc).isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z")
        run_id = "extrun_0123456789abcdef01234567"
        check_id = "extcheck_0123456789abcdef01234567"
        binding_digest = hashlib.sha256(
            b"private-isolated-runner-binding"
        ).hexdigest()
        report_digest = hashlib.sha256(
            b"private-isolated-runner-report"
        ).hexdigest()
        operation_key = hashlib.sha256(
            b"private-user\0private-workspace\0route-isolated-runner"
        ).hexdigest()

        def mutate_isolated_runner(state: dict[str, Any]) -> None:
            state.update(
                {
                    "schema_version": (
                        "veyra.phase6.extension_isolated_runner_state.v1"
                    ),
                    "runs": {
                        run_id: {
                            "schema_version": (
                                "veyra.phase6.extension_isolated_runner_record.v1"
                            ),
                            "run_id": run_id,
                            "check_id": check_id,
                            "user_id": "private-user",
                            "workspace_id": "private-workspace",
                            "stage": "ISOLATED_RUN_PASSED",
                            "revision": 2,
                            "binding_digest": binding_digest,
                            "report_digest": report_digest,
                            "candidate_execution_status": "not_started",
                            "behavior_verification_status": "not_started",
                            "promotion_authorized": False,
                            "created_at": recorded_at,
                            "updated_at": recorded_at,
                        }
                    },
                    "binding_index": {binding_digest: run_id},
                    "operation_index": {
                        operation_key: {
                            "request_digest": hashlib.sha256(
                                b"private-isolated-runner-request"
                            ).hexdigest(),
                            "kind": "start_isolated_run",
                            "run_id": run_id,
                            "result_revision": 2,
                            "result_stage": "ISOLATED_RUN_PASSED",
                            "owner_scope_digest": hashlib.sha256(
                                b"private-user\0private-workspace"
                            ).hexdigest(),
                            "recorded_at": recorded_at,
                        }
                    },
                    "run_count": 1,
                    "updated_at": recorded_at,
                }
            )

        loop.state_store.mutate_json(
            "phase6_extension_isolated_runner_state.json",
            mutate_isolated_runner,
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
            "phase6_extension_artifact_state": {
                "artifacts": {
                    "private-artifact": {
                        "content_b64url": "private-bytes",
                        "user_id": "private-user",
                        "workspace_id": "private-workspace",
                    }
                }
            },
            "phase6_extension_source_check_state": {
                "checks": {
                    "private-source-check": {
                        "source_sha256": "private-source-digest",
                        "user_id": "private-user",
                        "workspace_id": "private-workspace",
                    }
                }
            },
            "phase6_extension_isolated_runner_state": {
                "runs": {
                    "private-isolated-run": {
                        "binding_digest": "private-binding-digest",
                        "report_digest": "private-report-digest",
                        "user_id": "private-user",
                        "workspace_id": "private-workspace",
                    }
                }
            },
            "general_situation_state": {
                "general_situations": {
                    "private-general-situation": {
                        "user_id": "private-user",
                        "session_scope_keys": ["private-session"],
                        "child_refs": ["private-child-reference"],
                    }
                }
            },
            "suggestion_outbox": {
                "proposals": {
                    "private-suggestion": {
                        "user_id": "private-user",
                        "session_id": "private-session",
                        "summary": "private suggestion",
                    }
                }
            },
            "phase6_extension_generation_state": {
                "generations": {
                    "private-generation": {
                        "user_id": "private-user",
                        "workspace_id": "private-workspace",
                        "binding": "private-generation-binding",
                    }
                }
            },
            "phase6_extension_dynamic_validation_state": {
                "validations": {
                    "private-validation": {
                        "user_id": "private-user",
                        "workspace_id": "private-workspace",
                        "report": "private-validation-report",
                    }
                }
            },
            "phase6_extension_release_state": {
                "releases": {
                    "private-release": {
                        "owner_scope_digest": "private-owner-scope",
                        "attestation": "private-attestation",
                    }
                }
            },
            "phase6_extension_deployment_state": {
                "deployments": {
                    "private-deployment": {
                        "user_id": "private-user",
                        "workspace_id": "private-workspace",
                        "release_id": "private-release",
                    }
                }
            },
            "phase6_extension_pipeline_state": {
                "pipelines": {
                    "private-pipeline": {
                        "user_id": "private-user",
                        "workspace_id": "private-workspace",
                        "session_id": "private-session",
                        "receipts": "private-pipeline-receipts",
                    }
                }
            },
            "phase6_capability_gap_state": {
                "gaps": {
                    "private-gap": {
                        "user_id": "private-user",
                        "workspace_id": "private-workspace",
                        "reason_code": "private-reason",
                    }
                }
            },
        }
    )
    expect(
        "phase6_collaboration_state" not in public_state
        and "phase6_extension_spec_state" not in public_state
        and "phase6_extension_artifact_state" not in public_state
        and "phase6_extension_source_check_state" not in public_state
        and "phase6_extension_isolated_runner_state" not in public_state
        and "general_situation_state" not in public_state
        and "suggestion_outbox" not in public_state
        and "phase6_extension_generation_state" not in public_state
        and "phase6_extension_dynamic_validation_state" not in public_state
        and "phase6_extension_release_state" not in public_state
        and "phase6_extension_deployment_state" not in public_state
        and "phase6_extension_pipeline_state" not in public_state
        and "phase6_capability_gap_state" not in public_state
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
        len(SCENARIOS) == 26
        and comparisons == 9 * len(MODES) * len(SCENARIOS) == 702
        and not failures,
        (
            "702 isolated populated or corrupt collaboration, Situation, "
            "suggestion, extension lifecycle, governed-pipeline, and "
            "capability-gap states "
            "cannot weaken complete disabled, "
            "record-only, or shadow Route output/status/risk"
        ),
        failures,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
