from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import tempfile
from typing import Any, Callable

from core.world_state import WorldStateStore
from interface.extension_artifact import artifact_owner_scope_digest
from interface.extension_deployment import (
    EXTENSION_DEPLOYMENT_POLICY_DIGEST,
    EXTENSION_DEPLOYMENT_POLICY_REVISION,
    EXTENSION_INVOCATION_BACKEND_STATUS_SCHEMA_VERSION,
    EXTENSION_INVOCATION_HARNESS_REVISION,
    EXTENSION_INVOCATION_RESULT_SCHEMA_VERSION,
    ExtensionDeploymentAuthority,
    ExtensionInvocationBackendStatus,
    ExtensionInvocationBinding,
    ExtensionInvocationResult,
    canonical_utc,
    digest_canonical_value,
)
from interface.extension_generation import (
    authenticated_local_principal_digest,
    initiating_session_digest,
)
from interface.extension_release import ExtensionReleaseAuthority
from interface.extension_spec import parse_extension_spec
from runtime.extension_deployment_gate import ExtensionDeploymentGate
from scripts.phase6_extension_dynamic_validation_test_support import (
    SOURCE,
    valid_spec,
)


ROOT = Path(__file__).resolve().parents[1]
BASE_TIME = datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc)
USER = "phase6-deployment-user"
OTHER_USER = "phase6-deployment-other-user"
WORKSPACE = str(Path(__file__).resolve().parents[1])
SESSION = "phase6-deployment-session"
OTHER_SESSION = "phase6-deployment-other-session"
TOKEN = "phase6-deployment-control-token"
RELEASE_ID = "extrel_" + "a" * 24
ATTESTATION_DIGEST = "b" * 64
MANIFEST_DIGEST = "c" * 64
GENERATOR_IDENTITY = "d" * 64
VERIFIER_IDENTITY = "e" * 64
SIGNER_IDENTITY = "f" * 64
APPROVER_IDENTITY = "1" * 64
APPROVER_TOKEN = "phase6-extension-distinct-approver-token"


class Clock:
    def __init__(self, current: datetime = BASE_TIME) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"PASS {label}")


def expect_raises(
    errors: tuple[type[BaseException], ...],
    call: Callable[[], Any],
    label: str,
) -> BaseException:
    try:
        call()
    except errors as exc:
        print(f"PASS {label}")
        return exc
    raise AssertionError(f"{label}: call did not fail closed")


def available_backend_status() -> ExtensionInvocationBackendStatus:
    harness_digest = hashlib.sha256(
        (ROOT / "runtime" / "extension_invocation_harness.py").read_bytes()
    ).hexdigest()
    return ExtensionInvocationBackendStatus(
        schema_version=EXTENSION_INVOCATION_BACKEND_STATUS_SCHEMA_VERSION,
        availability="available",
        reason_code="ready",
        engine_identity_digest="2" * 64,
        image_id="sha256:" + "3" * 64,
        isolation_conformance_digest="4" * 64,
        invocation_conformance_digest="5" * 64,
        harness_revision=EXTENSION_INVOCATION_HARNESS_REVISION,
        harness_digest=harness_digest,
        deployment_policy_revision=EXTENSION_DEPLOYMENT_POLICY_REVISION,
        deployment_policy_digest=EXTENSION_DEPLOYMENT_POLICY_DIGEST,
        conformance_certified=True,
        authority=ExtensionDeploymentAuthority(),
    )


class FakeReleaseRegistry:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.calls = 0
        self.revoked: set[str] = set()
        self.subjects: dict[str, dict[str, Any]] = {}
        self.add_release(RELEASE_ID, ATTESTATION_DIGEST, version=1)

    def add_release(self, release_id: str, attestation: str, *, version: int) -> None:
        raw = valid_spec()
        raw["version"] = version
        raw["expires_at"] = (BASE_TIME + timedelta(days=7)).isoformat()
        spec = parse_extension_spec(raw)
        self.subjects[release_id] = {
            "schema_version": "veyra.phase6.extension_release_deployment_subject.v1",
            "release_id": release_id,
            "release_revision": 1,
            "owner_scope_digest": artifact_owner_scope_digest(USER, WORKSPACE),
            "principal_digest": authenticated_local_principal_digest(TOKEN),
            "session_digest": initiating_session_digest(SESSION),
            "manifest_digest": hashlib.sha256(release_id.encode()).hexdigest(),
            "attestation_digest": attestation,
            "signature_algorithm": "ed25519",
            "signing_key_id": "ed25519_" + "7" * 64,
            "signing_service_identity_digest": SIGNER_IDENTITY,
            "generator_identity_digest": GENERATOR_IDENTITY,
            "verifier_identity_digest": VERIFIER_IDENTITY,
            "validation_build_identity_digest": "8" * 64,
            "generation_report_digest": "9" * 64,
            "validation_report_digest": "0" * 64,
            "artifact_id": "extart_" + "a" * 24,
            "artifact_revision": 1,
            "artifact_sha256": hashlib.sha256(SOURCE).hexdigest(),
            "spec_digest": spec.digest(),
            "source_bytes": SOURCE,
            "source_check_spec": spec.canonical_dict(),
            "expires_at": canonical_utc(BASE_TIME + timedelta(days=5)),
            "authority": ExtensionReleaseAuthority().model_dump(mode="json"),
        }

    def deployment_subject(self, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        release_id = kwargs.get("release_id")
        subject = self.subjects.get(release_id)
        if subject is None or release_id in self.revoked:
            raise RuntimeError("release unavailable")
        if (
            kwargs.get("user_id") != USER
            or kwargs.get("workspace_id") != WORKSPACE
            or kwargs.get("session_id") != SESSION
            or kwargs.get("control_token") != TOKEN
            or kwargs.get("expected_release_revision") != subject["release_revision"]
            or kwargs.get("expected_attestation_digest")
            != subject["attestation_digest"]
            or self.clock.current >= datetime.fromisoformat(
                subject["expires_at"].replace("Z", "+00:00")
            )
        ):
            raise RuntimeError("release subject mismatch")
        return dict(subject)


class FakeReleaseSnapshotVerifier:
    def __init__(self, release: FakeReleaseRegistry, clock: Clock) -> None:
        self.release = release
        self.clock = clock
        self.calls = 0

    def verify(self, **kwargs: Any) -> None:
        self.calls += 1
        subject = self.release.subjects.get(kwargs.get("release_id"))
        if (
            subject is None
            or kwargs.get("release_id") in self.release.revoked
            or kwargs.get("release_revision") != subject["release_revision"]
            or kwargs.get("attestation_digest") != subject["attestation_digest"]
            or kwargs.get("owner_scope_digest") != subject["owner_scope_digest"]
            or kwargs.get("principal_digest") != subject["principal_digest"]
            or kwargs.get("session_digest") != subject["session_digest"]
            or kwargs.get("artifact_sha256") != subject["artifact_sha256"]
            or kwargs.get("spec_digest") != subject["spec_digest"]
            or self.clock.current
            >= datetime.fromisoformat(subject["expires_at"].replace("Z", "+00:00"))
        ):
            raise RuntimeError("durable signed release snapshot is invalid")
        spec = parse_extension_spec(subject["source_check_spec"])
        if (
            kwargs.get("extension_id") != spec.extension_id
            or kwargs.get("extension_version") != spec.version
        ):
            raise RuntimeError("durable signed release snapshot rebound")


class FakeInvocationRunner:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.status_calls = 0
        self.run_calls = 0
        self.next_status = "passed"
        self.last_binding: ExtensionInvocationBinding | None = None

    def status(self) -> ExtensionInvocationBackendStatus:
        self.status_calls += 1
        return available_backend_status()

    def run(
        self,
        *,
        source_bytes: bytes,
        spec: Any,
        input_payload: dict[str, Any],
        binding: ExtensionInvocationBinding,
    ) -> ExtensionInvocationResult:
        self.run_calls += 1
        self.last_binding = binding
        if hashlib.sha256(source_bytes).hexdigest() != binding.artifact_sha256:
            raise RuntimeError("source mismatch")
        status = self.next_status
        if status == "raise":
            raise RuntimeError("unknown backend failure")
        if status == "failed":
            return ExtensionInvocationResult(
                schema_version=EXTENSION_INVOCATION_RESULT_SCHEMA_VERSION,
                binding=binding,
                binding_digest=binding.binding_digest(),
                invocation_status="failed",
                output_payload=None,
                output_digest=None,
                output_discarded=False,
                issue_code="deterministic_failure",
                completed_at=canonical_utc(self.clock.current),
                authority=ExtensionDeploymentAuthority(),
            )
        output = {"label": input_payload["name"]}
        digest = digest_canonical_value(output)
        shadow = binding.mode == "shadow"
        return ExtensionInvocationResult(
            schema_version=EXTENSION_INVOCATION_RESULT_SCHEMA_VERSION,
            binding=binding,
            binding_digest=binding.binding_digest(),
            invocation_status="discarded" if shadow else "passed",
            output_payload=None if shadow else output,
            output_digest=digest,
            output_discarded=shadow,
            issue_code=None,
            completed_at=canonical_utc(self.clock.current),
            authority=ExtensionDeploymentAuthority(),
        )


def build_gate(
    root: Path,
    *,
    clock: Clock | None = None,
    lifecycle_mode: str = "record_only",
) -> tuple[
    ExtensionDeploymentGate,
    FakeReleaseRegistry,
    FakeInvocationRunner,
    Clock,
]:
    selected_clock = clock or Clock()
    release = FakeReleaseRegistry(selected_clock)
    snapshot_verifier = FakeReleaseSnapshotVerifier(release, selected_clock)
    runner = FakeInvocationRunner(selected_clock)
    gate = ExtensionDeploymentGate(
        state_store=WorldStateStore(root=root),
        release_registry=release,
        invocation_runner=runner,  # type: ignore[arg-type]
        release_snapshot_verifier=snapshot_verifier,
        control_token=TOKEN,
        approver_token=APPROVER_TOKEN,
        lifecycle_mode=lifecycle_mode,
        now=selected_clock,
    )
    return gate, release, runner, selected_clock


def propose(gate: ExtensionDeploymentGate, *, operation: str = "propose-1") -> dict[str, Any]:
    return gate.propose(
        operation_id=operation,
        request_id="request-propose",
        release_id=RELEASE_ID,
        user_id=USER,
        workspace_id=WORKSPACE,
        session_id=SESSION,
        expected_state_revision=0,
        expected_release_revision=1,
        expected_attestation_digest=ATTESTATION_DIGEST,
        expires_at=canonical_utc(BASE_TIME + timedelta(days=2)),
        control_token=TOKEN,
    )


def transition(
    gate: ExtensionDeploymentGate,
    deployment: dict[str, Any],
    target: str,
    *,
    operation: str,
    state_revision: int,
    review_id: str | None = None,
) -> dict[str, Any]:
    return gate.transition(
        operation_id=operation,
        request_id=f"request-{operation}",
        deployment_id=deployment["deployment_id"],
        target_mode=target,
        user_id=USER,
        workspace_id=WORKSPACE,
        session_id=SESSION,
        expected_state_revision=state_revision,
        expected_deployment_revision=deployment["revision"],
        expected_mode_epoch=deployment["mode_epoch"],
        max_invocations=10,
        expires_at=canonical_utc(BASE_TIME + timedelta(days=2)),
        review_id=review_id,
        control_token=TOKEN,
    )


def admit_review(
    gate: ExtensionDeploymentGate,
    deployment: dict[str, Any],
    target_mode: str,
    *,
    review_id: str,
    approver_identity_digest: str | None = None,
) -> str:
    requested = gate.request_transition_review(
        operation_id=f"request-{review_id}",
        request_id=f"request-id-{review_id}",
        deployment_id=deployment["deployment_id"],
        target_mode=target_mode,
        user_id=USER,
        workspace_id=WORKSPACE,
        session_id=SESSION,
        expected_state_revision=gate.status(control_token=TOKEN)["state_revision"],
        expected_deployment_revision=deployment["revision"],
        expected_mode_epoch=deployment["mode_epoch"],
        control_token=TOKEN,
    )
    approved = gate.approve_transition_review(
        review_id=requested["review_id"],
        expected_review_revision=requested["review_revision"],
        reason_digest="a" * 64,
        approver_token=APPROVER_TOKEN,
    )
    if (
        approver_identity_digest is not None
        and approver_identity_digest
        != gate._approver_identity_digest(APPROVER_TOKEN)
    ):
        # Negative-only corruption fixture: a forged same-service identity is
        # made internally self-consistent so transition separation must catch it.
        def forge(state: dict[str, Any]) -> None:
            for item in state.get("items", []):
                if item.get("review_id") != approved["review_id"]:
                    continue
                item["approver_identity_digest"] = approver_identity_digest
                item["approval_receipt_digest"] = gate._review_receipt_digest(
                    review_id=item["review_id"],
                    decided_at=item["decided_at"],
                    expires_at=item["approval_expires_at"],
                    approver_identity_digest=approver_identity_digest,
                    proposal=item["proposal"],
                )

        gate.state_store.mutate_json("review_queue.json", forge)
    return approved["review_id"]


def invoke(
    gate: ExtensionDeploymentGate,
    deployment: dict[str, Any],
    *,
    operation: str,
    state_revision: int,
    name: str = "Veyra",
) -> dict[str, Any]:
    return gate.invoke(
        operation_id=operation,
        request_id=f"request-{operation}",
        deployment_id=deployment["deployment_id"],
        user_id=USER,
        workspace_id=WORKSPACE,
        session_id=SESSION,
        expected_state_revision=state_revision,
        expected_deployment_revision=deployment["revision"],
        expected_mode_epoch=deployment["mode_epoch"],
        input_payload={"name": name},
        control_token=TOKEN,
    )


__all__ = [
    "APPROVER_IDENTITY",
    "APPROVER_TOKEN",
    "ATTESTATION_DIGEST",
    "BASE_TIME",
    "Clock",
    "FakeInvocationRunner",
    "FakeReleaseRegistry",
    "FakeReleaseSnapshotVerifier",
    "GENERATOR_IDENTITY",
    "OTHER_SESSION",
    "OTHER_USER",
    "RELEASE_ID",
    "SESSION",
    "SIGNER_IDENTITY",
    "TOKEN",
    "USER",
    "VERIFIER_IDENTITY",
    "WORKSPACE",
    "admit_review",
    "build_gate",
    "expect",
    "expect_raises",
    "invoke",
    "propose",
    "transition",
]
