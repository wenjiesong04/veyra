#!/usr/bin/env python3
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.world_state import WorldStateStore
from runtime.foresight_runtime import ForesightRuntime, STATE_FILE
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


def snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


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


def make_invocation(workspace: Path) -> ToolInvocation:
    binding = GovernedSessionBinding.create(
        user_id="local-user",
        workspace_id=str(workspace),
        agent_id="openclaw",
        session_id="session_foresight_trial",
        channel_id="api",
        case_id="case_foresight_trial",
        step_id="step_foresight_trial",
        run_id="run_foresight_trial",
    )
    target = str(workspace / "effect.txt")
    return ToolInvocation.create(
        binding=binding,
        tool_call_id="call_foresight_trial",
        tool_name="file.write",
        tool_kind="file",
        arguments={
            "path": target,
            "content": "SANDBOX_CONTENT_MUST_NOT_BE_STORED_OR_EXECUTED",
        },
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


def authoritative_evidence(
    invocation: ToolInvocation,
) -> dict[str, Any]:
    result_digest = canonical_sha256(
        {"status": "success", "tool_call_id": invocation.tool_call_id}
    )
    receipt = AuthoritativeToolReceipt.create(
        receipt_id="receipt_foresight_trial",
        run_id=invocation.binding.run_id,
        tool_call_id=invocation.tool_call_id,
        grant_id="grant_foresight_trial",
        reservation_id="reservation_foresight_trial",
        tool_name=invocation.tool_name,
        tool_kind=invocation.tool_kind,
        risk_level="R2",
        args_digest=invocation.args_digest,
        targets_digest=invocation.targets_digest,
        environment_digest=invocation.environment_digest,
        invocation_digest=invocation.invocation_digest,
        grant_digest="a" * 64,
        ledger_state="observed_success",
        approval_verified=False,
        approval_id="approval_foresight_trial",
        approval_revision="approval.revision.v1",
        policy_revision="veyra.phase3.scoped_sandbox.v1",
        registry_revision="veyra.openclaw.tool_registry.v1",
        reserved_at=NOW + timedelta(seconds=1),
        observed_at=NOW + timedelta(seconds=2),
        outcome="success",
        result_digest=result_digest,
    )
    effect = VerifiedToolEffect.create(
        source=EXECUTOR,
        observed_at=NOW + timedelta(seconds=2),
        receipt_id=receipt.receipt_id,
        run_id=receipt.run_id,
        tool_call_id=receipt.tool_call_id,
        tool_name=receipt.tool_name,
        invocation_digest=receipt.invocation_digest,
        result_digest=result_digest,
        targets_digest=receipt.targets_digest,
        authorized_targets=list(invocation.derived_targets),
        summary="Verified one exact sandbox file write.",
        changed_files=list(invocation.derived_targets),
    )
    return {
        **receipt.model_dump(mode="json"),
        "effect_evidence": effect.model_dump(mode="json"),
    }


def main() -> int:
    with TemporaryDirectory(prefix="veyra-foresight-trial-") as temp:
        root = Path(temp)
        workspace = root / "sandbox"
        workspace.mkdir()
        state_store = WorldStateStore(root / "state")
        invocation = make_invocation(workspace)
        clock = {"value": NOW}
        evidence = {"value": None}
        resolver_calls: list[tuple[str, str]] = []

        def resolver(run_id: str, tool_call_id: str) -> dict[str, Any] | None:
            resolver_calls.append((run_id, tool_call_id))
            value = evidence["value"]
            return dict(value) if isinstance(value, dict) else None

        runtime = ForesightRuntime(
            state_store,
            tool_receipt_resolver=resolver,
            now=lambda: clock["value"],
        )
        before_preview = snapshot(state_store.root)
        preview = runtime.preview(invocation)
        after_preview = snapshot(state_store.root)
        expect(
            preview["status"] == "previewed"
            and preview["dry_run"] is True
            and preview["persisted"] is False
            and preview["execution_attempted"] is False
            and before_preview == after_preview
            and not (workspace / "effect.txt").exists(),
            "dry-run assessment has zero persistence and zero execution",
        )
        expect(
            no_authority(preview),
            "dry-run output carries no execution or promotion authority",
        )

        registered = runtime.register_sandbox_trial(invocation)
        replayed = runtime.register_sandbox_trial(invocation)
        expect(
            registered["status"] == "registered"
            and registered["replayed"] is False
            and replayed["assessment_id"] == registered["assessment_id"]
            and replayed["replayed"] is True
            and resolver_calls == [],
            "sandbox prediction registration is durable and exact-replay idempotent",
        )
        expect(
            not (workspace / "effect.txt").exists()
            and no_authority(registered)
            and no_authority(replayed),
            "registration neither executes the tool nor creates authority",
        )

        not_ready = runtime.promotion_eligibility(
            registered["assessment_id"]
        )
        expect(
            not_ready["status"] == "not_ready"
            and not_ready["reason"]
            == "authoritative_sandbox_effect_not_reconciled"
            and not_ready["eligibility_only"] is True
            and no_authority(not_ready),
            "unreconciled prediction is not promotion eligible",
        )

        evidence["value"] = authoritative_evidence(invocation)
        clock["value"] = NOW + timedelta(seconds=3)
        reconciled = runtime.reconcile(registered["assessment_id"])
        expect(
            resolver_calls
            == [(invocation.binding.run_id, invocation.tool_call_id)]
            and reconciled["status"] == "exact"
            and reconciled["residual"]["status"] == "exact"
            and no_authority(reconciled),
            "exact run/call receipt plus Veyra-owned effect closes an exact residual",
        )
        promotion = reconciled["promotion"]
        expect(
            promotion["status"] == "eligible"
            and promotion["eligibility_only"] is True
            and promotion["evidence"]["residual_status"] == "exact"
            and no_authority(promotion),
            "exact sandbox evidence yields eligibility output only",
        )
        replay = runtime.reconcile(registered["assessment_id"])
        expect(
            replay["replayed"] is True
            and replay["residual"]["residual_digest"]
            == reconciled["residual"]["residual_digest"]
            and resolver_calls
            == [(invocation.binding.run_id, invocation.tool_call_id)],
            "terminal residual replay is stable and does not re-resolve evidence",
        )
        expect(
            not (workspace / "effect.txt").exists()
            and "SANDBOX_CONTENT_MUST_NOT_BE_STORED_OR_EXECUTED"
            not in state_store.path_for(STATE_FILE).read_text(encoding="utf-8")
            and "foresight_runtime_state" not in state_store.read_all()
            and runtime.status()["residual_counts"]["exact"] == 1,
            "Foresight keeps private digest evidence out of generic state without side effects",
        )

    print("foresight sandbox promotion smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
