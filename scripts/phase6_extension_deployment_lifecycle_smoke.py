from __future__ import annotations

import hashlib
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime.extension_deployment_gate import (
    DEPLOYMENT_STATE_FILE,
    ExtensionDeploymentConflictError,
    ExtensionDeploymentNotFoundError,
    ExtensionDeploymentUnavailableError,
)
from scripts.phase6_extension_deployment_test_support import (
    GENERATOR_IDENTITY,
    OTHER_SESSION,
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


def state_revision(gate: object) -> int:
    return gate.status(control_token=TOKEN)["state_revision"]


def current(gate: object, deployment_id: str) -> dict[str, object]:
    return gate.get(
        deployment_id=deployment_id,
        user_id=USER,
        workspace_id=WORKSPACE,
        session_id=SESSION,
        control_token=TOKEN,
    )


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="veyra-phase6-deployment-") as tmp:
        root = Path(tmp)
        clock = Clock()
        gate, release, runner, _ = build_gate(
            root, clock=clock
        )

        deployment = propose(gate)
        expect(deployment["mode"] == "record_only", "proposal defaults record_only")
        expect(runner.run_calls == 0, "record_only proposal performs no execution")
        expect(
            gate.public_registry(
                user_id=USER,
                workspace_id=WORKSPACE,
                session_id=SESSION,
                control_token=TOKEN,
            )["items"]
            == [],
            "proposal is absent from public registry",
        )
        expect_raises(
            (ExtensionDeploymentConflictError,),
            lambda: gate.transition(
                operation_id="skip-to-canary",
                request_id="request-skip",
                deployment_id=deployment["deployment_id"],
                target_mode="read_only_canary",
                user_id=USER,
                workspace_id=WORKSPACE,
                session_id=SESSION,
                expected_state_revision=state_revision(gate),
                expected_deployment_revision=deployment["revision"],
                expected_mode_epoch=deployment["mode_epoch"],
                max_invocations=10,
                expires_at=deployment["expires_at"],
                control_token=TOKEN,
            ),
            "lifecycle cannot skip shadow",
        )

        deployment = transition(
            gate,
            deployment,
            "shadow",
            operation="transition-shadow",
            state_revision=state_revision(gate),
        )
        response = invoke(
            gate,
            deployment,
            operation="invoke-shadow",
            state_revision=state_revision(gate),
        )
        expect(
            response["result"]["invocation_status"] == "discarded",
            "shadow executes in isolation",
        )
        expect(
            response["result"]["output_payload"] is None
            and response["result"]["output_discarded"] is True,
            "shadow output is irreversibly discarded",
        )
        state_text = gate.state_store.path_for(DEPLOYMENT_STATE_FILE).read_text(
            encoding="utf-8"
        )
        expect("Veyra" not in state_text, "shadow input and output are not persisted")
        calls_before = (release.calls, runner.status_calls, runner.run_calls)
        replay = invoke(
            gate,
            deployment,
            operation="invoke-shadow",
            state_revision=2,
        )
        expect(
            replay["result_digest"] == response["result_digest"],
            "exact invocation replay returns the durable receipt",
        )
        expect(
            calls_before == (release.calls, runner.status_calls, runner.run_calls),
            "exact replay does not touch release or runner",
        )
        deployment = response["deployment"]

        expect_raises(
            (ExtensionDeploymentNotFoundError,),
            lambda: gate.get(
                deployment_id=deployment["deployment_id"],
                user_id=USER,
                workspace_id=WORKSPACE,
                session_id=OTHER_SESSION,
                control_token=TOKEN,
            ),
            "deployment is isolated by initiating session",
        )

        deployment = transition(
            gate,
            deployment,
            "read_only_canary",
            operation="transition-read-only",
            state_revision=state_revision(gate),
        )
        response = invoke(
            gate,
            deployment,
            operation="invoke-read-only",
            state_revision=state_revision(gate),
        )
        expect(
            response["result"]["output_payload"] == {"label": "Veyra"},
            "read-only canary returns only schema-valid pure data",
        )
        deployment = response["deployment"]

        expect_raises(
            (ExtensionDeploymentUnavailableError,),
            lambda: transition(
                gate,
                deployment,
                "scoped_canary",
                operation="scoped-no-review",
                state_revision=state_revision(gate),
            ),
            "scoped canary blocks without an identified ReviewQueue receipt",
        )
        scoped_review = admit_review(
            gate,
            deployment,
            "scoped_canary",
            review_id="review-scoped",
        )
        deployment = transition(
            gate,
            deployment,
            "scoped_canary",
            operation="transition-scoped",
            state_revision=state_revision(gate),
            review_id=scoped_review,
        )
        response = invoke(
            gate,
            deployment,
            operation="invoke-scoped",
            state_revision=state_revision(gate),
        )
        deployment = response["deployment"]
        expect("scoped_canary" in deployment["successful_modes"], "scoped canary receipt recorded")

        expect_raises(
            (ExtensionDeploymentUnavailableError,),
            lambda: transition(
                gate,
                deployment,
                "promoted",
                operation="promote-no-review",
                state_revision=state_revision(gate),
            ),
            "promotion fails closed without review callback",
        )
        self_review = admit_review(
            gate,
            deployment,
            "promoted",
            review_id="review-generator",
            approver_identity_digest=GENERATOR_IDENTITY,
        )
        expect_raises(
            (ExtensionDeploymentConflictError,),
            lambda: transition(
                gate,
                deployment,
                "promoted",
                operation="promote-generator-review",
                state_revision=state_revision(gate),
                review_id=self_review,
            ),
            "generator cannot approve its own promotion",
        )
        promotion_review = admit_review(
            gate,
            deployment,
            "promoted",
            review_id="review-promotion",
        )
        deployment = transition(
            gate,
            deployment,
            "promoted",
            operation="promote-approved",
            state_revision=state_revision(gate),
            review_id=promotion_review,
        )
        expect(deployment["mode"] == "promoted", "separate explicit review promotes")
        public = gate.public_registry(
            user_id=USER,
            workspace_id=WORKSPACE,
            session_id=SESSION,
            control_token=TOKEN,
        )
        expect(len(public["items"]) == 1, "promotion atomically creates public pointer")
        expect(
            public["items"][0]["execution_boundary"]
            == "trusted_isolated_runner_only",
            "public capability retains isolated execution boundary",
        )
        expect(
            not any(public["items"][0]["authority"].values()),
            "public capability delegates no ambient authority",
        )

        file_path = gate.state_store.path_for(DEPLOYMENT_STATE_FILE)
        before_bytes = file_path.read_bytes()
        before_hash = hashlib.sha256(before_bytes).hexdigest()
        calls_before = (release.calls, runner.status_calls, runner.run_calls)
        gate.status(control_token=TOKEN)
        gate.list(
            user_id=USER,
            workspace_id=WORKSPACE,
            session_id=SESSION,
            control_token=TOKEN,
        )
        gate.get(
            deployment_id=deployment["deployment_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
            session_id=SESSION,
            control_token=TOKEN,
        )
        gate.integrity(
            deployment_id=deployment["deployment_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
            session_id=SESSION,
            control_token=TOKEN,
        )
        gate.public_registry(
            user_id=USER,
            workspace_id=WORKSPACE,
            session_id=SESSION,
            control_token=TOKEN,
        )
        after_bytes = file_path.read_bytes()
        expect(before_bytes == after_bytes, "all deployment GETs are byte-invariant")
        expect(
            before_hash == hashlib.sha256(after_bytes).hexdigest(),
            "all deployment GETs are hash-invariant",
        )
        expect(runner.status_calls == calls_before[1] and runner.run_calls == calls_before[2], "deployment GETs avoid runner calls")
        expect(release.calls == calls_before[0], "public GET never resolves private release subject")
        expect(
            gate.release_snapshot_verifier.calls >= 1,
            "public projection verifies the durable source-free signed snapshot",
        )

    with tempfile.TemporaryDirectory(prefix="veyra-phase6-deployment-disabled-") as tmp:
        disabled, _, disabled_runner, _ = build_gate(
            Path(tmp), lifecycle_mode="disabled"
        )
        expect_raises(
            (ExtensionDeploymentUnavailableError,),
            lambda: propose(disabled),
            "disabled mode blocks proposal",
        )
        expect(disabled_runner.run_calls == 0, "disabled mode cannot execute")

    print("Phase 6 signed extension deployment lifecycle smoke passed")


if __name__ == "__main__":
    main()
