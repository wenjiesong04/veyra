#!/usr/bin/env python3
from __future__ import annotations

from datetime import timedelta
import hashlib
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.world_state import WorldStateStore  # noqa: E402
from interface.extension_artifact import artifact_owner_scope_digest  # noqa: E402
from interface.extension_deployment import canonical_utc  # noqa: E402
from interface.extension_generation import (  # noqa: E402
    authenticated_local_principal_digest,
    initiating_session_digest,
)
from interface.extension_release import ExtensionReleaseAuthority  # noqa: E402
from interface.extension_spec import parse_extension_spec  # noqa: E402
from runtime.agent_capability_directory import (  # noqa: E402
    AgentCapabilityDirectory,
    ExtensionCapabilitySelectionError,
)
from runtime.extension_deployment_gate import ExtensionDeploymentGate  # noqa: E402
from scripts.phase6_extension_deployment_test_support import (  # noqa: E402
    APPROVER_TOKEN,
    ATTESTATION_DIGEST,
    BASE_TIME,
    Clock,
    FakeInvocationRunner,
    FakeReleaseSnapshotVerifier,
    GENERATOR_IDENTITY,
    MANIFEST_DIGEST,
    SESSION,
    SIGNER_IDENTITY,
    SOURCE,
    TOKEN,
    USER,
    VERIFIER_IDENTITY,
    WORKSPACE,
    expect,
    expect_raises,
)
from scripts.phase6_extension_dynamic_validation_test_support import (  # noqa: E402
    valid_spec,
)


SESSION_B = "phase6-deployment-session-b"
RELEASE_B = "extrel_" + "2" * 24
ATTESTATION_B = "3" * 64


class MultiSessionReleaseRegistry:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.calls = 0
        self.revoked: set[str] = set()
        self.subjects: dict[str, dict[str, Any]] = {}
        self.add_release(
            release_id="extrel_" + "a" * 24,
            attestation=ATTESTATION_DIGEST,
            version=1,
            session_id=SESSION,
        )
        self.add_release(
            release_id=RELEASE_B,
            attestation=ATTESTATION_B,
            version=2,
            session_id=SESSION_B,
        )

    def add_release(
        self,
        *,
        release_id: str,
        attestation: str,
        version: int,
        session_id: str,
    ) -> None:
        raw = valid_spec()
        raw["version"] = version
        raw["expires_at"] = (BASE_TIME + timedelta(days=7)).isoformat()
        spec = parse_extension_spec(raw)
        self.subjects[release_id] = {
            "schema_version": (
                "veyra.phase6.extension_release_deployment_subject.v1"
            ),
            "release_id": release_id,
            "release_revision": 1,
            "owner_scope_digest": artifact_owner_scope_digest(
                USER, WORKSPACE
            ),
            "principal_digest": authenticated_local_principal_digest(TOKEN),
            "session_digest": initiating_session_digest(session_id),
            "manifest_digest": MANIFEST_DIGEST,
            "attestation_digest": attestation,
            "signature_algorithm": "ed25519",
            "signing_key_id": "ed25519_" + "7" * 64,
            "signing_service_identity_digest": SIGNER_IDENTITY,
            "generator_identity_digest": GENERATOR_IDENTITY,
            "verifier_identity_digest": VERIFIER_IDENTITY,
            "validation_build_identity_digest": "8" * 64,
            "generation_report_digest": "9" * 64,
            "validation_report_digest": "0" * 64,
            "artifact_id": "extart_" + str(version) * 24,
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
        expected_session = (
            SESSION if release_id != RELEASE_B else SESSION_B
        )
        if (
            kwargs.get("user_id") != USER
            or kwargs.get("workspace_id") != WORKSPACE
            or kwargs.get("session_id") != expected_session
            or kwargs.get("control_token") != TOKEN
            or kwargs.get("expected_release_revision") != 1
            or kwargs.get("expected_attestation_digest")
            != subject["attestation_digest"]
        ):
            raise RuntimeError("release subject mismatch")
        return dict(subject)


class EmptyAgentRegistry:
    def config(self) -> dict[str, Any]:
        return {}

    def names(self) -> list[str]:
        return []


class CountingDeploymentGate:
    def __init__(self, gate: ExtensionDeploymentGate) -> None:
        self.gate = gate
        self.public_reads = 0
        self.invocations = 0

    def public_registry(self, **kwargs: Any) -> dict[str, Any]:
        self.public_reads += 1
        return self.gate.public_registry(**kwargs)

    def get(self, **kwargs: Any) -> dict[str, Any]:
        return self.gate.get(**kwargs)

    def invoke(self, **kwargs: Any) -> dict[str, Any]:
        self.invocations += 1
        return self.gate.invoke(**kwargs)


def _state_revision(gate: ExtensionDeploymentGate) -> int:
    return gate.status(control_token=TOKEN)["state_revision"]


def _transition(
    gate: ExtensionDeploymentGate,
    deployment: dict[str, Any],
    *,
    session_id: str,
    target_mode: str,
    operation_id: str,
    review_id: str | None = None,
) -> dict[str, Any]:
    return gate.transition(
        operation_id=operation_id,
        request_id="request-" + operation_id,
        deployment_id=deployment["deployment_id"],
        target_mode=target_mode,
        user_id=USER,
        workspace_id=WORKSPACE,
        session_id=session_id,
        expected_state_revision=_state_revision(gate),
        expected_deployment_revision=deployment["revision"],
        expected_mode_epoch=deployment["mode_epoch"],
        max_invocations=10,
        expires_at=canonical_utc(BASE_TIME + timedelta(days=2)),
        review_id=review_id,
        control_token=TOKEN,
    )


def _invoke(
    gate: ExtensionDeploymentGate,
    deployment: dict[str, Any],
    *,
    session_id: str,
    operation_id: str,
) -> dict[str, Any]:
    return gate.invoke(
        operation_id=operation_id,
        request_id="request-" + operation_id,
        deployment_id=deployment["deployment_id"],
        user_id=USER,
        workspace_id=WORKSPACE,
        session_id=session_id,
        expected_state_revision=_state_revision(gate),
        expected_deployment_revision=deployment["revision"],
        expected_mode_epoch=deployment["mode_epoch"],
        input_payload={"name": session_id},
        control_token=TOKEN,
    )["deployment"]


def _review(
    gate: ExtensionDeploymentGate,
    deployment: dict[str, Any],
    *,
    session_id: str,
    target_mode: str,
    suffix: str,
) -> str:
    requested = gate.request_transition_review(
        operation_id="review-request-" + suffix,
        request_id="review-request-id-" + suffix,
        deployment_id=deployment["deployment_id"],
        target_mode=target_mode,
        user_id=USER,
        workspace_id=WORKSPACE,
        session_id=session_id,
        expected_state_revision=_state_revision(gate),
        expected_deployment_revision=deployment["revision"],
        expected_mode_epoch=deployment["mode_epoch"],
        control_token=TOKEN,
    )
    approved = gate.approve_transition_review(
        review_id=requested["review_id"],
        expected_review_revision=requested["review_revision"],
        reason_digest="6" * 64,
        approver_token=APPROVER_TOKEN,
    )
    return approved["review_id"]


def _promote(
    gate: ExtensionDeploymentGate,
    *,
    release_id: str,
    attestation: str,
    session_id: str,
    suffix: str,
) -> dict[str, Any]:
    deployment = gate.propose(
        operation_id="propose-" + suffix,
        request_id="request-propose-" + suffix,
        release_id=release_id,
        user_id=USER,
        workspace_id=WORKSPACE,
        session_id=session_id,
        expected_state_revision=_state_revision(gate),
        expected_release_revision=1,
        expected_attestation_digest=attestation,
        expires_at=canonical_utc(BASE_TIME + timedelta(days=2)),
        control_token=TOKEN,
    )
    deployment = _transition(
        gate,
        deployment,
        session_id=session_id,
        target_mode="shadow",
        operation_id="shadow-" + suffix,
    )
    deployment = _invoke(
        gate,
        deployment,
        session_id=session_id,
        operation_id="invoke-shadow-" + suffix,
    )
    deployment = _transition(
        gate,
        deployment,
        session_id=session_id,
        target_mode="read_only_canary",
        operation_id="readonly-" + suffix,
    )
    deployment = _invoke(
        gate,
        deployment,
        session_id=session_id,
        operation_id="invoke-readonly-" + suffix,
    )
    review = _review(
        gate,
        deployment,
        session_id=session_id,
        target_mode="scoped_canary",
        suffix="scoped-" + suffix,
    )
    deployment = _transition(
        gate,
        deployment,
        session_id=session_id,
        target_mode="scoped_canary",
        operation_id="scoped-" + suffix,
        review_id=review,
    )
    deployment = _invoke(
        gate,
        deployment,
        session_id=session_id,
        operation_id="invoke-scoped-" + suffix,
    )
    review = _review(
        gate,
        deployment,
        session_id=session_id,
        target_mode="promoted",
        suffix="promote-" + suffix,
    )
    return _transition(
        gate,
        deployment,
        session_id=session_id,
        target_mode="promoted",
        operation_id="promote-" + suffix,
        review_id=review,
    )


def main() -> int:
    with TemporaryDirectory(prefix="veyra-phase6-directory-") as raw:
        store = WorldStateStore(Path(raw) / "state")
        clock = Clock()
        releases = MultiSessionReleaseRegistry(clock)
        runner = FakeInvocationRunner(clock)
        gate = ExtensionDeploymentGate(
            state_store=store,
            release_registry=releases,
            invocation_runner=runner,  # type: ignore[arg-type]
            release_snapshot_verifier=FakeReleaseSnapshotVerifier(
                releases, clock  # type: ignore[arg-type]
            ),
            control_token=TOKEN,
            approver_token=APPROVER_TOKEN,
            lifecycle_mode="record_only",
            now=clock,
        )

        first = _promote(
            gate,
            release_id="extrel_" + "a" * 24,
            attestation=ATTESTATION_DIGEST,
            session_id=SESSION,
            suffix="a",
        )
        second = _promote(
            gate,
            release_id=RELEASE_B,
            attestation=ATTESTATION_B,
            session_id=SESSION_B,
            suffix="b",
        )
        state = store.read_json("phase6_extension_deployment_state.json")
        expect(
            len(state["active_pointers"]) == 2
            and len(state["public_registry"]) == 2
            and first["deployment_id"] != second["deployment_id"],
            "same owner and workspace retain independent session pointers",
            {
                "pointers": len(state["active_pointers"]),
                "public": len(state["public_registry"]),
            },
        )

        proxy = CountingDeploymentGate(gate)
        directory = AgentCapabilityDirectory(
            state_store=store,
            registry=EmptyAgentRegistry(),
            extension_deployment_gate=proxy,
        )
        baseline_reads = proxy.public_reads
        runtime_snapshot = directory.snapshot()
        expect(
            proxy.public_reads == baseline_reads
            and "extensions" not in runtime_snapshot,
            "ordinary Agent runtime snapshot never auto-discovers extensions",
            runtime_snapshot,
        )

        view_a = directory.discover_promoted_extensions(
            user_id=USER,
            workspace_id=WORKSPACE,
            session_id=SESSION,
            control_token=TOKEN,
        )
        view_b = directory.discover_promoted_extensions(
            user_id=USER,
            workspace_id=WORKSPACE,
            session_id=SESSION_B,
            control_token=TOKEN,
        )
        expect(
            len(view_a["items"]) == 1
            and len(view_b["items"]) == 1
            and view_a["items"][0]["deployment_id"]
            == first["deployment_id"]
            and view_b["items"][0]["deployment_id"]
            == second["deployment_id"]
            and view_a["natural_language_routing_allowed"] is False
            and view_a["agent_dispatch_allowed"] is False,
            "directory discovery is exact-session and non-automatic",
            {"a": view_a, "b": view_b},
        )
        expect_raises(
            (ExtensionCapabilitySelectionError,),
            lambda: directory.select_promoted_extension(
                capability_id="请自动选择合适工具",
                user_id=USER,
                workspace_id=WORKSPACE,
                session_id=SESSION,
                control_token=TOKEN,
            ),
            "natural-language capability selection is rejected",
        )

        selection = directory.select_promoted_extension(
            capability_id=view_a["items"][0]["capability_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
            session_id=SESSION,
            control_token=TOKEN,
        )
        before_cross_session = runner.run_calls
        expect_raises(
            (ExtensionCapabilitySelectionError,),
            lambda: directory.invoke_promoted_extension(
                selection,
                operation_id="directory-cross-session",
                request_id="directory-cross-session-request",
                user_id=USER,
                workspace_id=WORKSPACE,
                session_id=SESSION_B,
                input_payload={"name": "must-not-run"},
                control_token=TOKEN,
            ),
            "a promoted selection cannot cross its initiating session",
        )
        expect(
            proxy.invocations == 0
            and runner.run_calls == before_cross_session,
            "cross-session selection mismatch reaches no invocation gate",
            {
                "gate_invocations": proxy.invocations,
                "runner_calls": runner.run_calls,
            },
        )
        before_invocations = runner.run_calls
        response = directory.invoke_promoted_extension(
            selection,
            operation_id="directory-invoke-a",
            request_id="directory-request-a",
            user_id=USER,
            workspace_id=WORKSPACE,
            session_id=SESSION,
            input_payload={"name": "directory"},
            control_token=TOKEN,
        )
        expect(
            proxy.invocations == 1
            and runner.run_calls == before_invocations + 1
            and response["result"]["invocation_status"] == "passed",
            "directory invocation reaches only the deployment invocation gate",
            response,
        )

        releases.revoked.add(RELEASE_B)
        revoked = directory.discover_promoted_extensions(
            user_id=USER,
            workspace_id=WORKSPACE,
            session_id=SESSION_B,
            control_token=TOKEN,
        )
        still_valid = directory.discover_promoted_extensions(
            user_id=USER,
            workspace_id=WORKSPACE,
            session_id=SESSION,
            control_token=TOKEN,
        )
        expect(
            revoked["items"] == []
            and revoked["invalid_or_revoked_count"] == 1
            and len(still_valid["items"]) == 1,
            "revocation removes only the exact session capability",
            {"revoked": revoked, "still_valid": still_valid},
        )

    print("Phase 6 promoted extension directory smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
