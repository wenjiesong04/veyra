from __future__ import annotations

from datetime import timedelta
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from interface.extension_deployment import canonical_utc
from runtime.extension_deployment_gate import ExtensionDeploymentConflictError
from scripts.phase6_extension_deployment_test_support import (
    BASE_TIME,
    SESSION,
    TOKEN,
    USER,
    WORKSPACE,
    Clock,
    admit_review,
    build_gate,
    expect,
    expect_raises,
    invoke,
    propose,
    transition,
)


def revision(gate: object) -> int:
    return gate.status(control_token=TOKEN)["state_revision"]


def advance_with_receipt(
    gate: object,
    deployment: dict[str, object],
    target: str,
    prefix: str,
) -> dict[str, object]:
    review_id = None
    if target in {"scoped_canary", "promoted"}:
        review_id = admit_review(
            gate,
            deployment,
            target,
            review_id=f"review-{prefix}-{target}",
        )
    selected = transition(
        gate,
        deployment,
        target,
        operation=f"{prefix}-transition-{target}",
        state_revision=revision(gate),
        review_id=review_id,
    )
    if target != "promoted":
        response = invoke(
            gate,
            selected,
            operation=f"{prefix}-invoke-{target}",
            state_revision=revision(gate),
        )
        return response["deployment"]
    return selected


def promote(gate: object, deployment: dict[str, object], prefix: str) -> dict[str, object]:
    for target in (
        "shadow",
        "read_only_canary",
        "scoped_canary",
        "promoted",
    ):
        deployment = advance_with_receipt(gate, deployment, target, prefix)
    return deployment


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="veyra-phase6-breaker-") as tmp:
        clock = Clock()
        gate, _, runner, _ = build_gate(
            Path(tmp), clock=clock
        )
        deployment = propose(gate)
        deployment = advance_with_receipt(gate, deployment, "shadow", "failure")
        deployment = transition(
            gate,
            deployment,
            "read_only_canary",
            operation="failure-transition-read",
            state_revision=revision(gate),
        )
        runner.next_status = "failed"
        first = invoke(
            gate,
            deployment,
            operation="failure-one",
            state_revision=revision(gate),
        )
        expect(
            first["deployment"]["breaker_open"] is False,
            "one deterministic failure does not trip repeated-failure breaker",
        )
        second = invoke(
            gate,
            first["deployment"],
            operation="failure-two",
            state_revision=revision(gate),
        )
        expect(
            second["deployment"]["breaker_open"] is True
            and second["deployment"]["mode"] == "disabled",
            "repeated deterministic failures disable without retry",
        )
        expect_raises(
            (ExtensionDeploymentConflictError,),
            lambda: invoke(
                gate,
                second["deployment"],
                operation="failure-three",
                state_revision=revision(gate),
            ),
            "open breaker blocks further invocation",
        )

    with tempfile.TemporaryDirectory(prefix="veyra-phase6-rollback-") as tmp:
        clock = Clock()
        gate, release, runner, _ = build_gate(
            Path(tmp), clock=clock
        )
        release_a = promote(gate, propose(gate), "release-a")
        expect(release_a["mode"] == "promoted", "first release promoted")

        release_b_id = "extrel_" + "b" * 24
        release_b_attestation = "c" * 64
        release.add_release(release_b_id, release_b_attestation, version=2)
        release_b = gate.propose(
            operation_id="release-b-propose",
            request_id="release-b-request",
            release_id=release_b_id,
            user_id=USER,
            workspace_id=WORKSPACE,
            session_id=SESSION,
            expected_state_revision=revision(gate),
            expected_release_revision=1,
            expected_attestation_digest=release_b_attestation,
            expires_at=canonical_utc(BASE_TIME + timedelta(days=2)),
            control_token=TOKEN,
        )
        release_b = promote(gate, release_b, "release-b")
        expect(
            release_b["previous_deployment_id"] == release_a["deployment_id"],
            "second promotion binds exact prior active deployment",
        )
        registry = gate.public_registry(
            user_id=USER,
            workspace_id=WORKSPACE,
            session_id=SESSION,
            control_token=TOKEN,
        )
        expect(
            registry["items"][0]["deployment_id"] == release_b["deployment_id"],
            "second promotion atomically replaces active pointer",
        )

        runner.next_status = "raise"
        indeterminate = invoke(
            gate,
            release_b,
            operation="release-b-indeterminate",
            state_revision=revision(gate),
        )
        expect(
            indeterminate["result"]["invocation_status"] == "indeterminate",
            "unknown runner failure is durable indeterminate",
        )
        expect(
            indeterminate["deployment"]["mode"] == "disabled"
            and indeterminate["deployment"]["breaker_open"] is True,
            "any indeterminate result opens breaker",
        )
        registry = gate.public_registry(
            user_id=USER,
            workspace_id=WORKSPACE,
            session_id=SESSION,
            control_token=TOKEN,
        )
        expect(
            len(registry["items"]) == 1
            and registry["items"][0]["deployment_id"]
            == release_a["deployment_id"],
            "breaker atomically rolls back to prior still-signed release",
        )
        runs = runner.run_calls
        replay = invoke(
            gate,
            release_b,
            operation="release-b-indeterminate",
            state_revision=revision(gate) - 2,
        )
        expect(
            replay["result_digest"] == indeterminate["result_digest"]
            and runner.run_calls == runs,
            "indeterminate receipt replays without a retry",
        )

        release.revoked.add(release_a["release_id"])
        rollback_result = gate.reconcile_release(
            operation_id="reconcile-revoked-old",
            deployment_id=release_a["deployment_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
            session_id=SESSION,
            expected_state_revision=revision(gate),
            expected_deployment_revision=release_a["revision"],
            expected_mode_epoch=release_a["mode_epoch"],
            reason_digest="d" * 64,
            control_token=TOKEN,
        )
        expect(
            rollback_result["deployment"]["mode"] == "disabled",
            "revoked active rollback target is explicitly disabled",
        )
        expect(
            gate.public_registry(
                user_id=USER,
                workspace_id=WORKSPACE,
                session_id=SESSION,
                control_token=TOKEN,
            )["items"]
            == [],
            "revoked release is removed from public registry",
        )

    print("Phase 6 extension deployment breaker and rollback smoke passed")


if __name__ == "__main__":
    main()
