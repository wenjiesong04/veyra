from __future__ import annotations

import hashlib
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from interface.extension_isolated_runner import (
    ISOLATED_RUNNER_BACKEND_KIND,
    ISOLATED_RUNNER_BACKEND_STATUS_SCHEMA_VERSION,
    ISOLATED_RUNNER_HARNESS_REVISION,
    ISOLATED_RUNNER_POLICY_DIGEST,
    ISOLATED_RUNNER_POLICY_REVISION,
    IsolatedRunnerAuthority,
    IsolatedRunnerBackendStatus,
)
from interface.extension_spec import parse_extension_spec
import runtime.extension_invocation_harness as harness
from runtime.trusted_extension_invocation_runner import (
    TrustedExtensionInvocationRunner,
)
from runtime.trusted_isolated_runner import TrustedIsolatedRunnerBackend
from scripts.phase6_extension_deployment_test_support import (
    TOKEN,
    build_gate,
    expect,
    invoke,
    propose,
    transition,
)
from scripts.phase6_extension_dynamic_validation_test_support import (
    SOURCE,
    valid_spec,
)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="veyra-phase6-harness-") as tmp:
        root = Path(tmp)
        gate, release, fake_runner, _ = build_gate(root / "state")
        deployment = propose(gate)
        deployment = transition(
            gate,
            deployment,
            "shadow",
            operation="backend-shadow",
            state_revision=gate.status(control_token=TOKEN)["state_revision"],
        )
        invoke(
            gate,
            deployment,
            operation="backend-invoke",
            state_revision=gate.status(control_token=TOKEN)["state_revision"],
        )
        binding = fake_runner.last_binding
        expect(binding is not None, "lifecycle emits exact invocation binding")
        assert binding is not None

        input_root = root / "input"
        input_root.mkdir()
        artifact = input_root / "artifact.py"
        spec_path = input_root / "spec.json"
        binding_path = input_root / "binding.json"
        payload_path = input_root / "payload.json"
        artifact.write_bytes(SOURCE)
        spec = parse_extension_spec(
            release.subjects[deployment["release_id"]]["source_check_spec"]
        )
        spec_path.write_bytes(spec.canonical_bytes())
        binding_path.write_bytes(binding.canonical_bytes())
        payload_path.write_bytes(b'{"name":"Veyra"}')

        original_paths = (
            harness.INPUT_ROOT,
            harness.ARTIFACT_PATH,
            harness.SPEC_PATH,
            harness.BINDING_PATH,
            harness.PAYLOAD_PATH,
            harness._isolation_checks,
        )
        harness.INPUT_ROOT = input_root
        harness.ARTIFACT_PATH = artifact
        harness.SPEC_PATH = spec_path
        harness.BINDING_PATH = binding_path
        harness.PAYLOAD_PATH = payload_path
        harness._isolation_checks = lambda: True
        try:
            result = harness.run_harness()
            expect(result["invocation_status"] == "passed", "fixed harness executes exact signed source")
            expect(result["output_payload"] == {"label": "Veyra"}, "fixed harness returns schema-valid output")
            expect(
                result["binding_digest"] == binding.binding_digest(),
                "fixed harness binds exact invocation identity",
            )

            payload_path.write_bytes(b'{"name":"Changed"}')
            changed = harness.run_harness()
            expect(
                changed["invocation_status"] == "failed"
                and changed["issue_code"] == "input_identity_failed",
                "fixed harness rejects input rebinding",
            )
            payload_path.write_bytes(b'{"name":"Veyra"}')
            artifact.write_bytes(
                b"def run_extension(payload):\n    return {'label': 'changed'}\n"
            )
            changed = harness.run_harness()
            expect(
                changed["invocation_status"] == "failed"
                and changed["issue_code"] == "artifact_identity_failed",
                "fixed harness rejects source rebinding",
            )
        finally:
            (
                harness.INPUT_ROOT,
                harness.ARTIFACT_PATH,
                harness.SPEC_PATH,
                harness.BINDING_PATH,
                harness.PAYLOAD_PATH,
                harness._isolation_checks,
            ) = original_paths

    isolation = IsolatedRunnerBackendStatus(
        schema_version=ISOLATED_RUNNER_BACKEND_STATUS_SCHEMA_VERSION,
        backend_kind=ISOLATED_RUNNER_BACKEND_KIND,
        availability="available",
        reason_code="ready",
        runner_policy_revision=ISOLATED_RUNNER_POLICY_REVISION,
        runner_policy_digest=ISOLATED_RUNNER_POLICY_DIGEST,
        harness_revision=ISOLATED_RUNNER_HARNESS_REVISION,
        harness_digest="1" * 64,
        image_id="sha256:" + "2" * 64,
        image_conformance_digest="3" * 64,
        engine_identity_digest="4" * 64,
        conformance_certified=True,
        authority=IsolatedRunnerAuthority(),
    )
    isolation_backend = TrustedIsolatedRunnerBackend(
        docker_binary="/bin/false",
        docker_context="colima-veyra-runner",
    )
    isolation_backend.status = lambda: isolation  # type: ignore[method-assign]
    runner = TrustedExtensionInvocationRunner(
        isolation_backend=isolation_backend,
    )
    conformance = runner.conformance_identity_digest(
        isolation=isolation,
        harness_digest=runner.harness_digest(),
    )
    runner.expected_invocation_conformance_digest = conformance
    runner.invocation_conformance_certified = True
    ready = runner.status()
    expect(
        ready.availability == "available"
        and ready.engine_identity_digest == isolation.engine_identity_digest
        and ready.image_id == isolation.image_id,
        "invocation runner reuses exact certified isolation TCB",
    )
    runner.expected_invocation_conformance_digest = hashlib.sha256(b"drift").hexdigest()
    drift = runner.status()
    expect(
        drift.availability == "unavailable"
        and drift.reason_code == "conformance_identity_mismatch",
        "invocation runner fails closed on conformance drift",
    )

    print("Phase 6 signed extension invocation backend smoke passed")


if __name__ == "__main__":
    main()
