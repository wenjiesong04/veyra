#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

GATE_SMOKES = [
    "agency_single_source_smoke.py",
    "runtime_config_honesty_smoke.py",
    "user_world_multitenant_smoke.py",
    "persona_deep_binding_smoke.py",
    "capability_registry_unified_smoke.py",
    "local_setup_smoke.py",
    "start_local_runtime_smoke.py",
    "runtime_build_identity_smoke.py",
    "proactive_review_execution_smoke.py",
    "agency_state_source_smoke.py",
    "user_profile_isolation_smoke.py",
    "user_profile_generalization_smoke.py",
    "attention_scope_semantic_smoke.py",
    "context_anchor_binding_smoke.py",
    "read_only_cognitive_loop_smoke.py",
    "general_situation_suggestion_smoke.py",
    "attention_hypothesis_smoke.py",
    "attention_lifecycle_smoke.py",
    "suggestion_sandbox_smoke.py",
    "structured_observation_control_plane_smoke.py",
    "component_health_producer_smoke.py",
    "strategic_discussion_boundary_smoke.py",
    "final_route_projection_smoke.py",
    "commitment_smoke.py",
    "commitment_scope_smoke.py",
    "cognition_pipeline_smoke.py",
    "tool_proxy_guard_smoke.py",
    "tool_governance_runtime_smoke.py",
    "authoritative_tool_evidence_smoke.py",
    "safe_tool_sandbox_smoke.py",
    "openclaw_tool_broker_smoke.py",
    "openclaw_canary_bootstrap_smoke.py",
    "openclaw_authoritative_projection_smoke.py",
    "openclaw_authoritative_cancellation_smoke.py",
    "openclaw_device_store_security_smoke.py",
    "review_rollback_bypass_smoke.py",
    "review_execution_claim_smoke.py",
    "event_inbox_smoke.py",
    "situation_evaluator_smoke.py",
    "event_driven_awareness_smoke.py",
    "project_guardian_smoke.py",
    "project_guardian_producer_smoke.py",
    "project_guardian_github_ci_smoke.py",
    "project_guardian_deployment_intent_smoke.py",
    "project_guardian_attention_smoke.py",
    "project_guardian_attention_runtime_smoke.py",
    "project_guardian_http_contract_smoke.py",
    "project_guardian_replay_smoke.py",
    "belief_read_purity_smoke.py",
    "belief_scope_retention_smoke.py",
    "probe_observation_loop_smoke.py",
    "conservatism_monitor_smoke.py",
    "epistemic_hygiene_smoke.py",
    "state_integrity_smoke.py",
    "state_truth_isolation_smoke.py",
    "runtime_truth_smoke.py",
    "model_transport_truth_smoke.py",
    "local_control_guard_smoke.py",
    "state_refresh_fairness_smoke.py",
    "openclaw_capability_identity_smoke.py",
    "agent_governance_transaction_smoke.py",
    "agent_session_isolation_smoke.py",
    "agent_dialogue_contract_smoke.py",
    "durable_case_smoke.py",
    "bounded_agent_negotiation_smoke.py",
    "durable_case_http_contract_smoke.py",
    "phase4_agent_callback_smoke.py",
    "verifier_caller_execution_result_smoke.py",
    "phase6_collaboration_contract_smoke.py",
    "phase6_no_tool_execution_profile_smoke.py",
    "phase6_agent_capability_catalog_smoke.py",
    "bounded_multi_agent_collaboration_smoke.py",
    "multi_agent_recovery_cancellation_smoke.py",
    "phase6_control_plane_smoke.py",
    "phase6_extension_spec_contract_smoke.py",
    "phase6_extension_spec_lifecycle_smoke.py",
    "phase6_extension_control_plane_smoke.py",
    "phase6_extension_artifact_contract_smoke.py",
    "phase6_extension_artifact_lifecycle_smoke.py",
    "phase6_extension_artifact_control_plane_smoke.py",
    "phase6_extension_source_check_contract_smoke.py",
    "phase6_extension_source_gate_lifecycle_smoke.py",
    "phase6_extension_source_check_control_plane_smoke.py",
    "phase6_extension_isolated_runner_contract_smoke.py",
    "phase6_extension_isolated_runner_backend_smoke.py",
    "phase6_extension_isolated_runner_lifecycle_smoke.py",
    "phase6_extension_isolated_runner_control_plane_smoke.py",
    "phase6_extension_generation_contract_smoke.py",
    "phase6_extension_generation_lifecycle_smoke.py",
    "phase6_extension_generation_control_plane_smoke.py",
    "phase6_extension_dynamic_validation_contract_smoke.py",
    "phase6_extension_dynamic_validation_lifecycle_smoke.py",
    "phase6_extension_dynamic_validation_backend_smoke.py",
    "phase6_extension_dynamic_validation_control_plane_smoke.py",
    "phase6_extension_release_contract_smoke.py",
    "phase6_extension_release_lifecycle_smoke.py",
    "phase6_extension_release_control_plane_smoke.py",
    "phase6_extension_release_startup_smoke.py",
    "phase6_extension_deployment_contract_smoke.py",
    "phase6_extension_deployment_lifecycle_smoke.py",
    "phase6_extension_deployment_backend_smoke.py",
    "phase6_extension_deployment_breaker_smoke.py",
    "phase6_extension_deployment_control_plane_smoke.py",
    "phase6_extension_deployment_security_smoke.py",
    "phase6_promoted_extension_directory_smoke.py",
    "phase6_extension_pipeline_contract_smoke.py",
    "phase6_extension_pipeline_lifecycle_smoke.py",
    "phase6_extension_pipeline_control_plane_smoke.py",
    "phase6_extension_pipeline_recovery_smoke.py",
    "phase6_capability_gap_contract_smoke.py",
    "phase6_capability_gap_lifecycle_smoke.py",
    "phase6_capability_gap_control_plane_smoke.py",
    "phase6_route_non_regression_smoke.py",
    "autonomy_policy_smoke.py",
    "foresight_effect_contract_smoke.py",
    "foresight_sandbox_promotion_smoke.py",
    "prediction_residual_smoke.py",
    "learning_calibration_smoke.py",
    "performance_portfolio_smoke.py",
    "provider_certification_smoke.py",
    "capability_registry_provider_truth_smoke.py",
    "provider_dispatch_fail_closed_smoke.py",
    "phase5_control_plane_smoke.py",
    "playbook_registry_smoke.py",
    "sandbox_repair_playbook_smoke.py",
    "openclaw_reconnect_playbook_smoke.py",
    "openclaw_reconnect_http_contract_smoke.py",
    "self_heal_route_non_regression_smoke.py",
    "memory_integrity_smoke.py",
    "memory_quality_smoke.py",
    "openclaw_memory_contract_smoke.py",
    "openclaw_workspace_memory_smoke.py",
    "memory_api_isolation_smoke.py",
    "memory_context_scope_smoke.py",
    "proactive_planner_scope_smoke.py",
    "state_mutation_concurrency_smoke.py",
]

SMOKE_TIMEOUT_OVERRIDES = {
    # This intentionally exercises eight sequential turns against the live
    # configured model and evidence providers.
    "runtime_e2e_dialogue_smoke.py": 360.0,
    # This builds 1,512 isolated loops to compare all nine public Routes
    # across three modes and 28 populated/corrupt private-state scenarios.
    # GitHub-hosted runners can exceed the generic 120-second script budget;
    # keep the complete 756-comparison matrix and give only this smoke a
    # bounded, CI-tolerant allowance.
    "phase6_route_non_regression_smoke.py": 300.0,
}


def smoke_files(group: str) -> list[Path]:
    scripts_dir = ROOT / "scripts"
    if group == "all":
        return sorted(path for path in scripts_dir.glob("*_smoke.py") if path.name != "run_smokes.py")
    if group != "gate":
        raise ValueError(f"unknown smoke group: {group}")
    return [scripts_dir / name for name in GATE_SMOKES]


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Veyra smoke scripts with timeouts.")
    parser.add_argument("--group", choices=["gate", "all"], default="gate")
    parser.add_argument("--all", action="store_true", help="Run every scripts/*_smoke.py file.")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--list", action="store_true", help="List selected smoke scripts without running them.")
    args = parser.parse_args()

    group = "all" if args.all else args.group
    selected = smoke_files(group)
    missing = [path for path in selected if not path.exists()]
    if missing:
        for path in missing:
            print(f"missing smoke: {path.relative_to(ROOT)}", file=sys.stderr)
        return 2
    if args.list:
        for path in selected:
            print(path.relative_to(ROOT))
        return 0

    failures: list[tuple[Path, str]] = []
    for index, path in enumerate(selected, start=1):
        label = str(path.relative_to(ROOT))
        timeout = max(args.timeout, SMOKE_TIMEOUT_OVERRIDES.get(path.name, 0.0))
        print(f"[{index}/{len(selected)}] {label} (timeout={timeout:.0f}s)")
        try:
            result = subprocess.run(
                [sys.executable, str(path)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            failures.append((path, f"timeout after {timeout:.1f}s\n{exc.stdout or ''}\n{exc.stderr or ''}"))
            print(f"FAIL {label}: timeout", file=sys.stderr)
            continue
        if result.stdout:
            print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
        if result.stderr:
            print(result.stderr, file=sys.stderr, end="" if result.stderr.endswith("\n") else "\n")
        if result.returncode != 0:
            failures.append((path, f"exit {result.returncode}"))
            print(f"FAIL {label}: exit {result.returncode}", file=sys.stderr)
        else:
            print(f"PASS {label}")

    if failures:
        print("\nSmoke failures:", file=sys.stderr)
        for path, reason in failures:
            print(f"- {path.relative_to(ROOT)}: {reason}", file=sys.stderr)
        return 1
    print(f"All {len(selected)} {group} smoke scripts passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
