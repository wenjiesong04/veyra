#!/usr/bin/env python3
from __future__ import annotations

import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.foresight_contract import UnknownCapabilityEffectContract
from core.world_state import WorldStateStore
from runtime import foresight_runtime as foresight_runtime_module
from runtime.foresight_runtime import (
    ForesightRuntime,
    STATE_FILE,
    STATE_SCHEMA_VERSION,
)
from tool_proxy.governance_contract import (
    AuthoritativeToolReceipt,
    GovernedSessionBinding,
    ToolInvocation,
    VerifiedToolEffect,
    canonical_sha256,
)


NOW = datetime(2026, 7, 28, 8, 0, tzinfo=timezone.utc)
EXECUTOR = "veyra.openclaw.scoped_executor.v1"


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail}")
    print(f"ok - {label}")


def make_invocation(workspace: Path, suffix: str) -> ToolInvocation:
    run_id = f"run_residual_{suffix}"
    binding = GovernedSessionBinding.create(
        user_id="local-user",
        workspace_id=str(workspace),
        agent_id="openclaw",
        session_id=f"session_residual_{suffix}",
        channel_id="api",
        case_id=f"case_residual_{suffix}",
        step_id=f"step_residual_{suffix}",
        run_id=run_id,
    )
    target = str(workspace / f"{suffix}.txt")
    return ToolInvocation.create(
        binding=binding,
        tool_call_id=f"call_residual_{suffix}",
        tool_name="file.write",
        tool_kind="file",
        arguments={"path": target, "content": f"content-{suffix}"},
        derived_targets=[target],
        environment={
            "executor": EXECUTOR,
            "scope_digest": canonical_sha256(
                {"sandbox_root": str(workspace)}
            ),
            "policy_revision": "veyra.phase3.scoped_sandbox.v1",
            "registry_revision": "veyra.openclaw.tool_registry.v1",
        },
        requested_at=NOW,
    )


def evidence(
    invocation: ToolInvocation,
    *,
    receipt_run_id: str | None = None,
    changed_files: list[str] | None = None,
    include_effect: bool = True,
    reserved_at: datetime | None = None,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    receipt_run = receipt_run_id or invocation.binding.run_id
    receipt_observed_at = observed_at or NOW + timedelta(seconds=2)
    result_digest = canonical_sha256(
        {
            "status": "success",
            "run_id": receipt_run,
            "tool_call_id": invocation.tool_call_id,
        }
    )
    receipt = AuthoritativeToolReceipt.create(
        receipt_id=f"receipt_{receipt_run}_{invocation.tool_call_id}",
        run_id=receipt_run,
        tool_call_id=invocation.tool_call_id,
        grant_id=f"grant_{invocation.tool_call_id}",
        reservation_id=f"reservation_{invocation.tool_call_id}",
        tool_name=invocation.tool_name,
        tool_kind=invocation.tool_kind,
        risk_level="R2",
        args_digest=invocation.args_digest,
        targets_digest=invocation.targets_digest,
        environment_digest=invocation.environment_digest,
        invocation_digest=invocation.invocation_digest,
        grant_digest="b" * 64,
        ledger_state="observed_success",
        approval_verified=False,
        approval_id=f"approval_{invocation.tool_call_id}",
        approval_revision="approval.revision.v1",
        policy_revision="veyra.phase3.scoped_sandbox.v1",
        registry_revision="veyra.openclaw.tool_registry.v1",
        reserved_at=reserved_at or NOW + timedelta(seconds=1),
        observed_at=receipt_observed_at,
        outcome="success",
        result_digest=result_digest,
    )
    projection = receipt.model_dump(mode="json")
    if not include_effect:
        return projection
    effect = VerifiedToolEffect.create(
        source=EXECUTOR,
        observed_at=receipt_observed_at,
        receipt_id=receipt.receipt_id,
        run_id=receipt.run_id,
        tool_call_id=receipt.tool_call_id,
        tool_name=receipt.tool_name,
        invocation_digest=receipt.invocation_digest,
        result_digest=result_digest,
        targets_digest=receipt.targets_digest,
        authorized_targets=list(invocation.derived_targets),
        summary="Verified bounded sandbox observation.",
        changed_files=(
            list(invocation.derived_targets)
            if changed_files is None
            else changed_files
        ),
    )
    projection["effect_evidence"] = effect.model_dump(mode="json")
    return projection


def no_authority(projection: dict[str, Any]) -> bool:
    return (
        projection.get("promotion_applied") is False
        and projection.get("mode_changed") is False
        and projection.get("grant_issued") is False
        and projection.get("grant_signed") is False
        and projection.get("review_approved") is False
        and projection.get("execution_authority_enabled") is False
        and projection.get("promotion_authority_enabled") is False
        and projection.get("autonomy_level_changed") is False
    )


def registered_runtime(
    state_root: Path,
    invocation: ToolInvocation,
    resolved: dict[str, Any] | None,
    clock: dict[str, datetime],
    *,
    valid_for_seconds: int = 900,
) -> tuple[ForesightRuntime, str, list[tuple[str, str]]]:
    calls: list[tuple[str, str]] = []

    def resolver(run_id: str, tool_call_id: str) -> dict[str, Any] | None:
        calls.append((run_id, tool_call_id))
        return dict(resolved) if isinstance(resolved, dict) else None

    runtime = ForesightRuntime(
        WorldStateStore(state_root),
        tool_receipt_resolver=resolver,
        now=lambda: clock["value"],
    )
    registered = runtime.register_sandbox_trial(
        invocation,
        valid_for_seconds=valid_for_seconds,
    )
    return runtime, str(registered["assessment_id"]), calls


def main() -> int:
    with TemporaryDirectory(prefix="veyra-prediction-residual-") as temp:
        root = Path(temp)

        cross_workspace = root / "cross-workspace"
        cross_workspace.mkdir()
        cross_invocation = make_invocation(cross_workspace, "cross")
        cross_clock = {"value": NOW}
        cross_runtime, cross_id, _ = registered_runtime(
            root / "cross-state",
            cross_invocation,
            evidence(
                cross_invocation,
                receipt_run_id="run_residual_other",
            ),
            cross_clock,
        )
        cross_clock["value"] = NOW + timedelta(seconds=3)
        cross = cross_runtime.reconcile(cross_id)
        expect(
            cross["status"] == "indeterminate"
            and "cross_run_receipt_rejected"
            in cross["residual"]["reasons"]
            and cross["promotion"]["status"] == "blocked"
            and no_authority(cross["promotion"]),
            "cross-run authoritative evidence is indeterminate and cannot promote",
        )

        mismatch_workspace = root / "mismatch-workspace"
        mismatch_workspace.mkdir()
        mismatch_invocation = make_invocation(
            mismatch_workspace,
            "mismatch",
        )
        mismatch_clock = {"value": NOW}
        mismatch_runtime, mismatch_id, _ = registered_runtime(
            root / "mismatch-state",
            mismatch_invocation,
            evidence(mismatch_invocation, changed_files=[]),
            mismatch_clock,
        )
        mismatch_clock["value"] = NOW + timedelta(seconds=3)
        mismatch = mismatch_runtime.reconcile(mismatch_id)
        expect(
            mismatch["status"] == "mismatch"
            and mismatch["residual"]["missing_changed_files"]
            == list(mismatch_invocation.derived_targets)
            and mismatch["residual"]["unexpected_changed_files"] == []
            and mismatch["promotion"]["status"] == "blocked",
            "trusted observed effect difference is a categorical mismatch",
        )

        missing_workspace = root / "missing-workspace"
        missing_workspace.mkdir()
        missing_invocation = make_invocation(missing_workspace, "missing")
        missing_clock = {"value": NOW}
        missing_runtime, missing_id, _ = registered_runtime(
            root / "missing-state",
            missing_invocation,
            evidence(missing_invocation, include_effect=False),
            missing_clock,
        )
        missing_clock["value"] = NOW + timedelta(seconds=3)
        missing = missing_runtime.reconcile(missing_id)
        expect(
            missing["status"] == "pending"
            and missing["residual"]["reasons"]
            == ["authoritative_effect_missing"]
            and missing["promotion"]["status"] == "not_ready"
            and missing["persisted"] is False
            and missing["reevaluable"] is True
            and missing_runtime.status()["residual_counts"]["pending"] == 1,
            "missing Veyra-owned effect remains pending and reevaluable",
        )

        retry_workspace = root / "retry-workspace"
        retry_workspace.mkdir()
        retry_invocation = make_invocation(retry_workspace, "retry")
        retry_clock = {"value": NOW}
        retry_evidence: dict[str, Any] = {}
        retry_calls: list[tuple[str, str]] = []

        def retry_resolver(
            run_id: str,
            tool_call_id: str,
        ) -> dict[str, Any] | None:
            retry_calls.append((run_id, tool_call_id))
            return (
                dict(retry_evidence)
                if retry_evidence
                else None
            )

        retry_runtime = ForesightRuntime(
            WorldStateStore(root / "retry-state"),
            tool_receipt_resolver=retry_resolver,
            now=lambda: retry_clock["value"],
        )
        retry_id = str(
            retry_runtime.register_sandbox_trial(
                retry_invocation
            )["assessment_id"]
        )
        retry_clock["value"] = NOW + timedelta(seconds=1)
        first_retry = retry_runtime.reconcile(retry_id)
        retry_evidence.update(evidence(retry_invocation))
        retry_clock["value"] = NOW + timedelta(seconds=3)
        second_retry = retry_runtime.reconcile(retry_id)
        expect(
            first_retry["status"] == "pending"
            and first_retry["residual"]["reasons"]
            == ["authoritative_receipt_missing"]
            and first_retry["persisted"] is False
            and first_retry["reevaluable"] is True
            and second_retry["status"] == "exact"
            and second_retry["persisted"] is True
            and second_retry["reevaluable"] is False
            and len(retry_calls) == 2,
            "missing receipt cannot close a prediction before later evidence arrives",
        )

        retired_workspace = root / "retired-workspace"
        retired_workspace.mkdir()
        retired_invocation = make_invocation(retired_workspace, "retired")
        retired_clock = {"value": NOW}
        retired_runtime, retired_id, retired_calls = registered_runtime(
            root / "retired-state",
            retired_invocation,
            evidence(retired_invocation),
            retired_clock,
        )
        retired_clock["value"] = NOW + timedelta(seconds=3)
        original_resolver = (
            foresight_runtime_module.resolve_capability_effect_contract
        )

        def retired_contract(_: str) -> Any:
            raise UnknownCapabilityEffectContract("retired capability")

        foresight_runtime_module.resolve_capability_effect_contract = (
            retired_contract
        )
        try:
            retired = retired_runtime.reconcile(retired_id)
        finally:
            foresight_runtime_module.resolve_capability_effect_contract = (
                original_resolver
            )
        expect(
            retired["status"] == "indeterminate"
            and retired["residual"]["reasons"]
            == ["capability_effect_contract_unknown"]
            and retired_calls == []
            and retired["promotion"]["status"] == "blocked",
            "retired or unknown capability contract fails closed before evidence lookup",
        )

        prior_workspace = root / "prior-workspace"
        prior_workspace.mkdir()
        prior_invocation = make_invocation(prior_workspace, "prior")
        prior_clock = {"value": NOW}
        prior_runtime, prior_id, _ = registered_runtime(
            root / "prior-state",
            prior_invocation,
            evidence(
                prior_invocation,
                reserved_at=NOW - timedelta(seconds=2),
                observed_at=NOW - timedelta(seconds=1),
            ),
            prior_clock,
        )
        prior_clock["value"] = NOW + timedelta(seconds=1)
        prior = prior_runtime.reconcile(prior_id)
        expect(
            prior["status"] == "indeterminate"
            and prior["residual"]["reasons"]
            == [
                "execution_reserved_before_prediction",
                "authoritative_effect_precedes_prediction",
            ],
            "pre-existing reservation and outcome cannot be relabeled as a prediction",
        )

        stale_workspace = root / "stale-workspace"
        stale_workspace.mkdir()
        stale_invocation = make_invocation(stale_workspace, "stale")
        stale_clock = {"value": NOW}
        stale_runtime, stale_id, stale_calls = registered_runtime(
            root / "stale-state",
            stale_invocation,
            evidence(stale_invocation),
            stale_clock,
            valid_for_seconds=1,
        )
        stale_clock["value"] = NOW + timedelta(seconds=2)
        stale = stale_runtime.reconcile(stale_id)
        expect(
            stale["status"] == "indeterminate"
            and stale["residual"]["reasons"] == ["assessment_expired"]
            and stale_calls == []
            and stale["promotion"]["status"] == "blocked",
            "stale prediction fails before authoritative evidence lookup",
        )

        concurrent_workspace = root / "concurrent-workspace"
        concurrent_workspace.mkdir()
        concurrent_invocation = make_invocation(
            concurrent_workspace,
            "concurrent",
        )
        concurrent_clock_state = {
            "phase": "register",
            "counter": 0,
        }
        clock_lock = threading.Lock()

        def concurrent_clock() -> datetime:
            with clock_lock:
                if concurrent_clock_state["phase"] == "register":
                    return NOW
                concurrent_clock_state["counter"] += 1
                return (
                    NOW
                    + timedelta(seconds=3)
                    + timedelta(
                        microseconds=concurrent_clock_state["counter"]
                    )
                )

        barrier = threading.Barrier(2)
        concurrent_evidence = evidence(concurrent_invocation)

        def concurrent_resolver(
            _: str,
            __: str,
        ) -> dict[str, Any]:
            barrier.wait(timeout=5)
            return dict(concurrent_evidence)

        concurrent_runtime = ForesightRuntime(
            WorldStateStore(root / "concurrent-state"),
            tool_receipt_resolver=concurrent_resolver,
            now=concurrent_clock,
        )
        concurrent_id = str(
            concurrent_runtime.register_sandbox_trial(
                concurrent_invocation
            )["assessment_id"]
        )
        concurrent_clock_state["phase"] = "reconcile"
        with ThreadPoolExecutor(max_workers=2) as executor:
            concurrent_results = list(
                executor.map(
                    lambda _: concurrent_runtime.reconcile(concurrent_id),
                    range(2),
                )
            )
        expect(
            all(item["status"] == "exact" for item in concurrent_results)
            and len(
                {
                    item["residual"]["residual_digest"]
                    for item in concurrent_results
                }
            )
            == 1
            and concurrent_runtime.status()["residual_counts"]["exact"] == 1,
            "concurrent reconciliation converges on one authoritative residual",
        )

        capacity_workspace = root / "capacity-workspace"
        capacity_workspace.mkdir()
        capacity_clock = {"value": NOW}
        capacity_runtime = ForesightRuntime(
            WorldStateStore(root / "capacity-state"),
            now=lambda: capacity_clock["value"],
        )
        original_capacity = foresight_runtime_module.MAX_ASSESSMENTS
        foresight_runtime_module.MAX_ASSESSMENTS = 3
        try:
            expired_ids = [
                str(
                    capacity_runtime.register_sandbox_trial(
                        make_invocation(
                            capacity_workspace,
                            f"capacity-{index}",
                        ),
                        valid_for_seconds=1,
                    )["assessment_id"]
                )
                for index in range(3)
            ]
            capacity_clock["value"] = NOW + timedelta(seconds=2)
            admitted_after_expiry = (
                capacity_runtime.register_sandbox_trial(
                    make_invocation(
                        capacity_workspace,
                        "capacity-new",
                    ),
                    valid_for_seconds=30,
                )
            )
        finally:
            foresight_runtime_module.MAX_ASSESSMENTS = original_capacity
        capacity_status = capacity_runtime.status()
        expect(
            admitted_after_expiry["status"] == "registered"
            and capacity_status["assessment_count"] == 3
            and capacity_status["residual_counts"]["pending"] == 3
            and any(
                capacity_runtime.promotion_eligibility(assessment_id)[
                    "reason"
                ].startswith("foresight_state_untrusted:")
                for assessment_id in expired_ids
            ),
            "expired unresolved predictions cannot permanently exhaust capacity",
        )

        corrupt_store = WorldStateStore(root / "corrupt-state")
        corrupt_store.write_json(
            STATE_FILE,
            {
                "schema_version": STATE_SCHEMA_VERSION,
                "assessments": [],
                "invocation_index": {},
            },
        )
        corrupt_runtime = ForesightRuntime(corrupt_store)
        blocked = corrupt_runtime.promotion_eligibility("unknown_assessment")
        expect(
            corrupt_runtime.status()["status"] == "degraded"
            and blocked["status"] == "blocked"
            and blocked["reason"].startswith("foresight_state_untrusted:")
            and no_authority(blocked),
            "unknown or malformed durable state fails closed without authority",
        )

    print("prediction residual smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
