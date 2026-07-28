#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from awareness.project_guardian import ProjectGuardianEvaluator
from awareness.project_guardian_attention import (
    ProjectGuardianAttentionScheduler,
)
from core.world_state import WorldStateStore
from core.autonomy_policy import JSON_SANDBOX_REPAIR_PROFILE
from interface.agent_contract import AGENT_CONTRACT_VERSION
from routers.phase5 import build_phase5_router
from runtime.foresight_runtime import ForesightRuntime
from runtime.learning_calibration_runtime import (
    LearningCalibrationRuntime,
)
from runtime.performance_portfolio import PerformancePortfolio
from runtime.playbook_registry import (
    PlaybookRegistration,
    PlaybookRegistry,
)
from runtime.sandbox_repair_playbook import (
    IMPLEMENTATION_REVISION,
    PLAYBOOK_ID,
    JsonSandboxRepairPlaybook,
)


SECRET = "SECRET_PHASE5_CONTROL_PLANE_MUST_NOT_LEAK"
NOW = datetime.now(timezone.utc).replace(microsecond=0)


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"PASS {label}")


def snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def seed_attention(store: WorldStateStore) -> dict[str, Any]:
    scope = {
        "workspace_id": "ws-phase5",
        "repo_id": "veyra/phase5-control-plane",
        "target_ref": "refs/heads/main",
        "target_environment": "test",
        "release_cycle": "phase5-control-plane",
    }
    signal_kinds = [
        "ci_failed",
        "deployment_intent",
        "git_dirty",
    ]
    candidate: dict[str, Any] = {
        "schema_version": ProjectGuardianEvaluator.CANDIDATE_SCHEMA,
        "record_kind": "situation_candidate",
        "candidate_kind": ProjectGuardianEvaluator.CANDIDATE_KIND,
        "user_id": "user-phase5",
        "goal_id": "goal-phase5",
        "goal_revision": "goal-revision-phase5",
        "scope": scope,
        "signal_kinds": signal_kinds,
        "signals": [
            {
                "kind": kind,
                "source_component": (
                    ProjectGuardianEvaluator.SIGNAL_COMPONENTS[kind]
                ),
                "event_id": f"event-phase5-{kind}",
                "occurred_at": (
                    NOW - timedelta(minutes=5)
                ).isoformat(),
            }
            for kind in signal_kinds
        ],
        "signal_frontier": [],
        "evidence_refs": [],
        "source_sessions": ["session-phase5"],
        "why_now": {
            "reason_codes": ["phase5_control_plane_fixture"],
            "independent_signal_count": len(signal_kinds),
            "correlation_window_seconds": 1800,
        },
        "unknowns": ["release_approval", "rollback_readiness"],
        "candidate_advice": {
            "recommendation": "review_release_readiness",
            "checks": [
                "inspect_and_resolve_ci_failure",
                "confirm_release_target_and_rollback_plan",
                "review_uncommitted_changes",
            ],
        },
        "analysis_mode": "deterministic_read_only",
        "qualified_at": (NOW - timedelta(minutes=5)).isoformat(),
        "transitioned_at": (NOW - timedelta(minutes=5)).isoformat(),
        "evaluated_at": NOW.isoformat(),
        "shadow_only": True,
        "agent_invoked": False,
        "notification_allowed": False,
        "execution_allowed": False,
        "interrupt_eligible": False,
    }
    candidate["candidate_id"] = (
        ProjectGuardianEvaluator.candidate_id_for(
            user_id=candidate["user_id"],
            goal_id=candidate["goal_id"],
            goal_revision=candidate["goal_revision"],
            scope=scope,
        )
    )
    candidate["candidate_revision"] = (
        ProjectGuardianEvaluator.candidate_revision_for(candidate)
    )
    policy_context = {
        "schema_version": (
            ProjectGuardianAttentionScheduler.CONTEXT_SCHEMA_VERSION
        ),
        "user_id": candidate["user_id"],
        "goal_id": candidate["goal_id"],
        "goal_revision": candidate["goal_revision"],
        "scope": scope,
        "goal_priority": 0.95,
        "deadline_at": (NOW + timedelta(hours=1)).isoformat(),
        "timezone": "UTC",
        "notifications_paused": True,
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
    evaluation = ProjectGuardianAttentionScheduler().evaluate(
        candidates=[candidate],
        policy_context=policy_context,
        now=NOW,
    )
    assessment = dict(evaluation["assessments"][0])
    assessment.update(
        {
            "runtime_mode": "shadow",
            "attention_group_id": "phase5-control-plane",
            "recorded_at": NOW.isoformat(),
        }
    )

    def update(state: dict[str, Any]) -> None:
        state["schema_version"] = (
            "veyra.project_guardian_attention_state.v1"
        )
        state["assessments"] = {
            candidate["candidate_id"]: assessment
        }
        state["assessment_count"] = 1

    store.mutate_json(
        "project_guardian_attention_state.json",
        update,
    )
    return assessment


def seed_public_inputs(store: WorldStateStore) -> None:
    store.append_jsonl(
        "decision_trace.jsonl",
        {
            "trace_id": "phase5-control-route",
            "final_route": "direct",
            "outcome_category": "success",
            "probe_used": ["system_probe"],
            "latency_ms": 12,
            "completed_at": (NOW - timedelta(seconds=1)).isoformat(),
            "message_preview": SECRET,
        },
    )

    def matrix(state: dict[str, Any]) -> None:
        state.update(
            {
                "status": "degraded",
                "observed_at": NOW.isoformat(),
                "checked_at": NOW.isoformat(),
                "ttl_seconds": 300,
                "runtimes": [
                    {
                        "name": "openclaw",
                        "status": "ready",
                        "validation": {
                            "validated": True,
                            "status": "validated",
                        },
                        "capabilities": {
                            "runtime": "openclaw",
                            "status": "available",
                            "connected": True,
                            "contract_version": (
                                AGENT_CONTRACT_VERSION
                            ),
                            "features": {
                                "structured_task_packet": True,
                                "rendered_prompt_fallback": True,
                                "task_status": True,
                                "stop_task": True,
                            },
                            "compatibility": {
                                "status": "compatible",
                                "native_adapter": True,
                            },
                            "private_token": SECRET,
                        },
                    },
                    {
                        "name": "hermes",
                        "status": "not_configured",
                        "validation": {
                            "validated": False,
                            "status": "not_configured",
                        },
                        "capabilities": {
                            "runtime": "hermes",
                            "status": "adapter_unconfigured",
                            "connected": False,
                            "features": {},
                            "compatibility": {
                                "status": "unconfigured",
                                "native_adapter": False,
                            },
                        },
                    },
                    {
                        "name": "malicious-fixture",
                        "status": "ready",
                        "validation": {
                            "validated": False,
                            "status": SECRET,
                        },
                        "capabilities": {
                            "runtime": "malicious-fixture",
                            "status": SECRET,
                            "connected": True,
                            "contract_version": SECRET,
                            "features": {
                                "structured_task_packet": True,
                                "rendered_prompt_fallback": True,
                                "task_status": True,
                                "stop_task": True,
                            },
                            "compatibility": {
                                "status": SECRET,
                                "native_adapter": True,
                            },
                            "provider_diagnostic": SECRET,
                        },
                    },
                ],
            }
        )

    store.mutate_json("ops_runtime_matrix.json", matrix)
    store.mutate_json(
        "tool_governance_state.json",
        lambda state: state.update({"private_diagnostic": SECRET}),
    )


def feedback_body(
    assessment: dict[str, Any],
) -> dict[str, Any]:
    candidate_ref = assessment["candidate_ref"]
    return {
        "schema_version": "veyra.phase5.feedback_command.v1",
        "feedback_id": "feedback-control-plane-1",
        "user_id": "user-phase5",
        "assessment_id": assessment["assessment_id"],
        "assessment_revision": assessment["assessment_revision"],
        "candidate_id": candidate_ref["candidate_id"],
        "candidate_revision": candidate_ref["candidate_revision"],
        "label": "wrong_timing",
        "supersedes_learning_id": None,
    }


def sandbox_body(
    *,
    operation_id: str,
    candidate_json: str,
) -> dict[str, Any]:
    return {
        "schema_version": (
            "veyra.phase5.json_sandbox_candidate_command.v1"
        ),
        "playbook_id": PLAYBOOK_ID,
        "version": 1,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "operation_id": operation_id,
        "candidate_json": candidate_json,
        "expected_digest": hashlib.sha256(
            candidate_json.encode("utf-8")
        ).hexdigest(),
    }


def main() -> int:
    with tempfile.TemporaryDirectory(
        prefix="veyra-phase5-control-plane-"
    ) as tmp:
        root = Path(tmp)
        state_root = root / "state"
        workspace = root / "workspace"
        workspace.mkdir()
        sentinel = workspace / "sentinel.txt"
        sentinel.write_text("unchanged", encoding="utf-8")

        store = WorldStateStore(state_root)
        attention_assessment = seed_attention(store)
        seed_public_inputs(store)
        learning = LearningCalibrationRuntime(
            state_store=store,
            clock=lambda: NOW,
        )
        portfolio = PerformancePortfolio(
            state_store=store,
            now_fn=lambda: NOW,
        )
        foresight = ForesightRuntime(
            store,
            now=lambda: NOW,
        )
        sandbox = JsonSandboxRepairPlaybook(
            state_store=store,
            now=lambda: NOW,
        )
        app = FastAPI()
        app.include_router(
            build_phase5_router(
                state_store=store,
                foresight_runtime=foresight,
                learning_runtime=learning,
                performance_portfolio=portfolio,
                sandbox_playbook=sandbox,
            )
        )
        public_route_keys = [
            (route.path, method)
            for route in app.routes
            for method in getattr(route, "methods", set())
            if route.path.startswith("/phase5")
        ]
        expect(
            len(public_route_keys) == len(set(public_route_keys)) == 7,
            "each Phase 5 public method and path is registered exactly once",
            public_route_keys,
        )

        with TestClient(app) as client:
            before_get = snapshot(state_root)
            responses = {
                path: client.get(path)
                for path in (
                    "/phase5/status",
                    "/phase5/portfolio",
                    "/phase5/foresight/contracts",
                    "/phase5/foresight/status",
                    "/phase5/providers/certification",
                )
            }
            after_get = snapshot(state_root)
            expect(
                all(response.status_code == 200 for response in responses.values())
                and before_get == after_get,
                "all Phase 5 GET endpoints are persistence-free",
                {
                    path: response.status_code
                    for path, response in responses.items()
                },
            )

            status = responses["/phase5/status"].json()
            expect(
                status["status"]
                == "technical_complete_shadow_calibration"
                and status["operational_health"] == "available"
                and status["autonomy"]["A3"]["status"]
                == "private_sandbox_only"
                and status["autonomy"]["A3"][
                    "workspace_authority"
                ]
                is False
                and status["autonomy"]["A3"][
                    "production_authority"
                ]
                is False
                and status["autonomy"]["A4"] == "not_certified"
                and status["autonomy"]["A5"] == "not_certified"
                and status["components"]["playbook_registry"][
                    "kind"
                ]
                == "builtin_immutable"
                and status["components"]["playbook_registry"][
                    "dynamic_registration"
                ]
                is False
                and status["components"]["provider_certification"][
                    "status"
                ]
                == "available"
                and status["components"]["provider_certification"][
                    "selected"
                ]["runtime"]
                == "openclaw",
                "status exposes the bounded Phase 5 completion without degrading on an unconfigured non-selected provider",
                status,
            )
            authority = status["authority"]
            expect(
                authority["provider_auto_switch_allowed"] is False
                and authority["notification_allowed"] is False
                and authority["workspace_read_allowed"] is False
                and authority["workspace_mutation_allowed"] is False
                and authority["production_effect_allowed"] is False
                and authority["promotion_allowed"] is False
                and authority["capability_grant_allowed"] is False
                and authority["autonomy_raise_allowed"] is False,
                "status grants no provider, notification, workspace, production, promotion, or autonomy authority",
                authority,
            )
            expect(
                status["components"]["performance_portfolio"][
                    "status"
                ]
                == "success"
                and status["components"]["performance_portfolio"][
                    "authority"
                ]["policy_effect"]
                == "none",
                "Phase 5 health includes the read-only portfolio component",
                status["components"]["performance_portfolio"],
            )

            contracts = responses[
                "/phase5/foresight/contracts"
            ].json()
            foresight_status = responses[
                "/phase5/foresight/status"
            ].json()
            expect(
                contracts["registry_kind"] == "fixed_immutable"
                and contracts["contract_count"] == 6
                and contracts["authority"][
                    "prediction_is_authorization"
                ]
                is False
                and contracts["authority"][
                    "eligibility_applies_promotion"
                ]
                is False
                and foresight_status["status"] == "available"
                and foresight_status["execution_authority_enabled"]
                is False
                and foresight_status["promotion_authority_enabled"]
                is False,
                "Foresight GETs expose fixed contracts and read-only status without promotion authority",
                {
                    "contracts": contracts,
                    "status": foresight_status,
                },
            )

            portfolio_payload = responses[
                "/phase5/portfolio"
            ].json()
            providers = responses[
                "/phase5/providers/certification"
            ].json()
            certifications = {
                item["runtime"]: item
                for item in providers["certifications"]
            }
            expect(
                portfolio_payload["status"] == "success"
                and portfolio_payload[
                    "unknown_excluded_from_success_denominator"
                ]
                is True
                and portfolio_payload["authority"][
                    "route_selection_allowed"
                ]
                is False
                and certifications["openclaw"]["validated"] is True
                and certifications["hermes"]["validated"] is False
                and providers["selected"]["runtime"] == "openclaw"
                and providers["selected"]["certification"]["runtime"]
                == "openclaw"
                and certifications["malicious-fixture"]["evidence"][
                    "runtime_status"
                ]
                == "unknown"
                and certifications["malicious-fixture"]["evidence"][
                    "compatibility_status"
                ]
                == "unknown"
                and "reported"
                not in certifications["malicious-fixture"]["evidence"][
                    "contract"
                ]
                and providers["authority"][
                    "automatic_selection_allowed"
                ]
                is False
                and providers["authority"][
                    "provider_switch_allowed"
                ]
                is False,
                "portfolio and provider projections remain descriptive and non-selecting",
                {
                    "portfolio": portfolio_payload,
                    "providers": providers,
                },
            )
            serialized_gets = json.dumps(
                {
                    path: response.json()
                    for path, response in responses.items()
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            expect(
                SECRET not in serialized_gets
                and str(state_root) not in serialized_gets
                and str(workspace) not in serialized_gets,
                "GET projections do not expose sensitive state, tokens, or local paths",
            )

            original_matrix = store.read_json(
                "ops_runtime_matrix.json"
            )
            store.write_json(
                "ops_runtime_matrix.json",
                {
                    "status": "corrupt_fixture",
                    "runtimes": {"not": "a list"},
                },
            )
            provider_fault_status = client.get(
                "/phase5/status"
            ).json()
            expect(
                provider_fault_status["operational_health"]
                == "degraded"
                and provider_fault_status["components"][
                    "provider_certification"
                ]["status"]
                == "degraded",
                "runtime matrix structure faults degrade the provider component and Phase 5 health",
                provider_fault_status["components"][
                    "provider_certification"
                ],
            )
            def restore_matrix(state: dict[str, Any]) -> None:
                state.clear()
                state.update(
                    {
                        key: value
                        for key, value in original_matrix.items()
                        if key != "_state_revision"
                    }
                )

            store.mutate_json(
                "ops_runtime_matrix.json",
                restore_matrix,
            )

            def faulting_status() -> dict[str, Any]:
                raise RuntimeError("fixture status reader fault")

            fault_registry = PlaybookRegistry(
                (
                    PlaybookRegistration(
                        playbook_id=PLAYBOOK_ID,
                        version=sandbox.spec.version,
                        implementation_revision=IMPLEMENTATION_REVISION,
                        domain=JSON_SANDBOX_REPAIR_PROFILE.domain,
                        profile_id=(
                            JSON_SANDBOX_REPAIR_PROFILE.profile_id
                        ),
                        maximum_level=(
                            JSON_SANDBOX_REPAIR_PROFILE.level.value
                        ),
                        risk_floor=sandbox.spec.risk_floor,
                        allowed_modes=(
                            "disabled",
                            "record_only",
                            "shadow",
                            "scoped_canary",
                        ),
                        runner=sandbox.run,
                        status_reader=faulting_status,
                    ),
                )
            )
            fault_app = FastAPI()
            fault_app.include_router(
                build_phase5_router(
                    state_store=store,
                    foresight_runtime=foresight,
                    learning_runtime=learning,
                    performance_portfolio=portfolio,
                    sandbox_playbook=sandbox,
                    playbook_registry=fault_registry,
                )
            )
            with TestClient(fault_app) as fault_client:
                fault_response = fault_client.get("/phase5/status")
            fault_status = fault_response.json()
            expect(
                fault_response.status_code == 200
                and fault_status["operational_health"] == "degraded"
                and fault_status["components"]["playbook_registry"][
                    "status"
                ]
                == "degraded"
                and fault_status["components"]["sandbox_repair"][
                    "status"
                ]
                == "fault",
                "playbook status faults fail closed and degrade Phase 5 health",
                fault_status,
            )

            malicious_runner_called = False

            def malicious_runner(
                _request: Any,
            ) -> dict[str, Any]:
                nonlocal malicious_runner_called
                malicious_runner_called = True
                return {
                    "playbook_id": PLAYBOOK_ID,
                    "status": "malicious_runner_executed",
                }

            malicious_registry = PlaybookRegistry(
                (
                    PlaybookRegistration(
                        playbook_id=PLAYBOOK_ID,
                        version=sandbox.spec.version,
                        implementation_revision=IMPLEMENTATION_REVISION,
                        domain=JSON_SANDBOX_REPAIR_PROFILE.domain,
                        profile_id=(
                            JSON_SANDBOX_REPAIR_PROFILE.profile_id
                        ),
                        maximum_level=(
                            JSON_SANDBOX_REPAIR_PROFILE.level.value
                        ),
                        risk_floor=sandbox.spec.risk_floor,
                        allowed_modes=(
                            "disabled",
                            "record_only",
                            "shadow",
                            "scoped_canary",
                        ),
                        runner=malicious_runner,
                        status_reader=sandbox.status,
                    ),
                )
            )
            malicious_app = FastAPI()
            malicious_app.include_router(
                build_phase5_router(
                    state_store=store,
                    foresight_runtime=foresight,
                    learning_runtime=learning,
                    performance_portfolio=portfolio,
                    sandbox_playbook=sandbox,
                    playbook_registry=malicious_registry,
                )
            )
            malicious_candidate = json.dumps(
                {"fixture": "registry-runner-must-not-run"},
                sort_keys=True,
                separators=(",", ":"),
            )
            with TestClient(malicious_app) as malicious_client:
                malicious_response = malicious_client.post(
                    "/phase5/sandbox/json-candidate",
                    json=sandbox_body(
                        operation_id="sandbox-registry-binding",
                        candidate_json=malicious_candidate,
                    ),
                )
            expect(
                malicious_response.status_code == 200
                and malicious_response.json()["status"]
                == "shadow_candidate_valid"
                and malicious_runner_called is False,
                "sandbox POST invokes the exact type-validated built-in and never a metadata-equivalent registry runner",
                malicious_response.json(),
            )

            feedback = client.post(
                "/phase5/feedback",
                json=feedback_body(attention_assessment),
            )
            learning_after_first = store.read_json(
                "learning_calibration_state.json"
            )
            replay = client.post(
                "/phase5/feedback",
                json=feedback_body(attention_assessment),
            )
            learning_after_replay = store.read_json(
                "learning_calibration_state.json"
            )
            expect(
                feedback.status_code == 200
                and feedback.json()["status"] == "recorded"
                and feedback.json()["record"]["label"]
                == "wrong_timing"
                and "user_id" not in feedback.json()["record"]
                and replay.status_code == 200
                and replay.json()["status"] == "duplicate"
                and learning_after_first == learning_after_replay,
                "feedback POST is exact-bound, categorical, privacy-minimal, and replay-idempotent",
                {
                    "feedback": feedback.json(),
                    "replay": replay.json(),
                },
            )
            wrong_candidate = {
                **feedback_body(attention_assessment),
                "feedback_id": "feedback-control-plane-wrong",
                "candidate_revision": "candidate_revision_wrong",
            }
            extra_field = {
                **feedback_body(attention_assessment),
                "feedback_id": "feedback-control-plane-extra",
                "free_text": SECRET,
            }
            wrong_label = {
                **feedback_body(attention_assessment),
                "feedback_id": "feedback-control-plane-label",
                "label": "dismissed",
            }
            expect(
                client.post(
                    "/phase5/feedback",
                    json=wrong_candidate,
                ).status_code
                == 409
                and client.post(
                    "/phase5/feedback",
                    json=extra_field,
                ).status_code
                == 422
                and client.post(
                    "/phase5/feedback",
                    json=wrong_label,
                ).status_code
                == 422,
                "feedback rejects candidate drift, free text, and unsupported labels",
            )

            shadow_candidate = json.dumps(
                {"candidate": SECRET, "value": 1},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            before_shadow = snapshot(state_root)
            shadow = client.post(
                "/phase5/sandbox/json-candidate",
                json=sandbox_body(
                    operation_id="sandbox-shadow-1",
                    candidate_json=shadow_candidate,
                ),
            )
            after_shadow = snapshot(state_root)
            shadow_payload = shadow.json()
            expect(
                shadow.status_code == 200
                and shadow_payload["status"]
                == "shadow_candidate_valid"
                and shadow_payload["mode"] == "shadow"
                and shadow_payload["automatic_effects"] == []
                and shadow_payload["promotion_authorized"] is False
                and shadow_payload["production_effect_status"]
                == "not_started"
                and before_shadow == after_shadow,
                "default shadow sandbox candidate performs zero persistence and zero effect",
                shadow_payload,
            )
            expect(
                SECRET
                not in json.dumps(
                    shadow_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                )
                and str(state_root)
                not in json.dumps(
                    shadow_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                )
                and client.post(
                    "/phase5/sandbox/json-candidate",
                    json={
                        **sandbox_body(
                            operation_id="sandbox-strict-version",
                            candidate_json=shadow_candidate,
                        ),
                        "version": "1",
                    },
                ).status_code
                == 422,
                "sandbox response omits candidate/path data and strict Pydantic rejects coercion",
            )

            def enable_scoped_canary(config: dict[str, Any]) -> None:
                playbooks = config.setdefault("playbooks", {})
                playbooks["sandbox_repair_json"] = {
                    "mode": "scoped_canary",
                    "mode_epoch": 1,
                    "allowed_modes": [
                        "disabled",
                        "record_only",
                        "shadow",
                        "scoped_canary",
                    ],
                }

            store.mutate_json("ops_config.json", enable_scoped_canary)
            workspace_before = snapshot(workspace)
            private_candidate = json.dumps(
                {"candidate": SECRET, "value": 2},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            canary = client.post(
                "/phase5/sandbox/json-candidate",
                json=sandbox_body(
                    operation_id="sandbox-canary-1",
                    candidate_json=private_candidate,
                ),
            )
            canary_payload = canary.json()
            sandbox_root = (
                state_root / "runtime" / "sandbox_repairs"
            )
            private_files = [
                path
                for path in sandbox_root.rglob("*")
                if path.is_file()
            ]
            expect(
                canary.status_code == 200
                and canary_payload["status"]
                == "sandbox_verified_candidate"
                and canary_payload["mode"] == "scoped_canary"
                and canary_payload["effective_autonomy_level"] == "A3"
                and canary_payload["promotion_authorized"] is False
                and canary_payload["production_effect_status"]
                == "not_started"
                and len(private_files) == 2
                and all(
                    path.is_relative_to(state_root)
                    for path in private_files
                )
                and snapshot(workspace) == workspace_before
                and sentinel.read_text(encoding="utf-8") == "unchanged",
                "scoped canary writes only its Veyra-private state sandbox and leaves workspace untouched",
                {
                    "response": canary_payload,
                    "private_files": [
                        str(path.relative_to(state_root))
                        for path in private_files
                    ],
                },
            )
            public_canary = json.dumps(
                canary_payload,
                ensure_ascii=False,
                sort_keys=True,
            )
            durable_canary = store.path_for(
                "sandbox_repair_state.json"
            ).read_text(encoding="utf-8")
            expect(
                SECRET not in public_canary
                and str(state_root) not in public_canary
                and str(workspace) not in public_canary
                and SECRET not in durable_canary,
                "scoped canary keeps candidate content out of public and durable ledger projections",
            )

    print("phase5 control plane smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
