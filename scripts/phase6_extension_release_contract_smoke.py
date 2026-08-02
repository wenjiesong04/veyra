from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.world_state import WorldStateStore
from interface.extension_artifact import EXTENSION_ARTIFACT_POLICY_REVISION
from interface.extension_generation import (
    EXTENSION_GENERATION_BINDING_SCHEMA_VERSION,
    EXTENSION_GENERATION_POLICY_DIGEST,
    EXTENSION_GENERATION_POLICY_REVISION,
    EXTENSION_GENERATION_PROMPT_DIGEST,
    EXTENSION_GENERATION_REPORT_SCHEMA_VERSION,
    EXTENSION_GENERATOR_REVISION,
    ExtensionGenerationAuthority,
    ExtensionGenerationBinding,
    ExtensionGenerationReport,
    authenticated_local_principal_digest,
    initiating_session_digest,
)
from interface.extension_dynamic_validation import (
    DYNAMIC_VALIDATION_BINDING_SCHEMA_VERSION,
    DYNAMIC_VALIDATION_HARNESS_REVISION,
    DYNAMIC_VALIDATION_POLICY_DIGEST,
    DYNAMIC_VALIDATION_POLICY_REVISION,
    DYNAMIC_VALIDATION_REPORT_SCHEMA_VERSION,
    DYNAMIC_VALIDATION_FUZZ_CASES,
    DynamicValidationAuthority,
    DynamicValidationBinding,
    DynamicValidationReport,
    dynamic_build_identity_digest,
    dynamic_request_provenance_digest,
    parse_dynamic_validation_test_bundle,
)
from interface.extension_isolated_runner import (
    ISOLATED_RUNNER_HARNESS_REVISION,
    ISOLATED_RUNNER_POLICY_REVISION,
)
from interface.extension_release import (
    ExtensionReleaseAuthority,
    parse_extension_release_attestation,
)
from interface.extension_spec import EXTENSION_POLICY_REVISION, parse_extension_spec
from runtime.extension_release_registry import (
    ExtensionReleaseConflictError,
    ExtensionReleaseRegistry,
)
from runtime.extension_release_signer import (
    Ed25519ReleaseSigner,
    Ed25519ReleaseTrustStore,
    ExtensionReleaseKeyConfigurationError,
    ExtensionReleaseSignatureError,
)
from scripts.phase6_extension_dynamic_validation_test_support import (
    SOURCE,
    USER,
    WORKSPACE,
    available_backend_status,
    make_subject,
    valid_test_bundle,
    valid_spec,
)


BASE_TIME = datetime(2026, 8, 2, 8, 1, tzinfo=timezone.utc)
SESSION = "phase6-release-session"
CONTROL_TOKEN = "phase6-release-control-token"
SIGNING_SERVICE_ID = "veyra.release.signer"


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


def dynamic_report(
    *,
    session_id: str = SESSION,
    control_token: str = CONTROL_TOKEN,
) -> DynamicValidationReport:
    subject = make_subject()
    backend = available_backend_status()
    tests = parse_dynamic_validation_test_bundle(valid_test_bundle())
    values: dict[str, Any] = {
        "schema_version": DYNAMIC_VALIDATION_BINDING_SCHEMA_VERSION,
        "validation_id": "extval_" + "0" * 24,
        "isolated_run_id": subject["run_id"],
        "isolated_runner_binding_digest": subject[
            "isolated_runner_binding_digest"
        ],
        "isolated_runner_report_digest": subject[
            "isolated_runner_report_digest"
        ],
        "isolated_runner_stage": "RUNNER_JOB_PASSED",
        "isolated_runner_policy_revision": ISOLATED_RUNNER_POLICY_REVISION,
        "isolated_runner_harness_revision": ISOLATED_RUNNER_HARNESS_REVISION,
        "source_check_id": subject["source_check_id"],
        "source_check_binding_digest": subject["source_check_binding_digest"],
        "source_check_report_digest": subject["source_check_report_digest"],
        "source_check_stage": "SOURCE_CHECK_PASSED",
        "candidate_id": subject["candidate_id"],
        "candidate_revision": subject["candidate_revision"],
        "artifact_id": subject["artifact_id"],
        "artifact_revision": subject["artifact_revision"],
        "artifact_envelope_digest": subject["artifact_envelope_digest"],
        "artifact_sha256": subject["artifact_sha256"],
        "artifact_size_bytes": subject["artifact_size_bytes"],
        "owner_scope_digest": subject["owner_scope_digest"],
        "authenticated_principal_digest": authenticated_local_principal_digest(
            control_token
        ),
        "initiating_session_digest": initiating_session_digest(session_id),
        "request_id": "dynamic-validation-release-test",
        "request_provenance_digest": "0" * 64,
        "extension_id": subject["extension_id"],
        "extension_version": subject["extension_version"],
        "spec_digest": subject["spec_digest"],
        "source_parser_identity": subject["parser_identity"],
        "source_ruleset_digest": subject["ruleset_digest"],
        "engine_identity_digest": backend.engine_identity_digest,
        "image_id": backend.image_id,
        "isolation_conformance_digest": backend.isolation_conformance_digest,
        "validation_conformance_digest": backend.validation_conformance_digest,
        "validation_policy_revision": DYNAMIC_VALIDATION_POLICY_REVISION,
        "validation_policy_digest": DYNAMIC_VALIDATION_POLICY_DIGEST,
        "validation_harness_revision": DYNAMIC_VALIDATION_HARNESS_REVISION,
        "validation_harness_digest": backend.harness_digest,
        "test_bundle_digest": tests.bundle_digest(),
    }
    values["request_provenance_digest"] = dynamic_request_provenance_digest(
        values
    )
    values["build_identity_digest"] = dynamic_build_identity_digest(values)
    values["validation_id"] = "extval_" + values[
        "build_identity_digest"
    ][:24]
    binding = DynamicValidationBinding.model_validate(values, strict=True)
    return DynamicValidationReport(
        schema_version=DYNAMIC_VALIDATION_REPORT_SCHEMA_VERSION,
        binding=binding,
        binding_digest=binding.binding_digest(),
        validation_status="passed",
        candidate_execution_status="passed",
        unit_checks_status="passed",
        contract_checks_status="passed",
        security_runtime_checks_status="passed",
        fuzz_checks_status="passed",
        behavior_verification_status="passed",
        vector_count=len(tests.vectors),
        fuzz_case_count=DYNAMIC_VALIDATION_FUZZ_CASES,
        issue_codes=[],
        completed_at="2026-08-02T08:00:00Z",
        authority=DynamicValidationAuthority(),
    )


def generation_report() -> ExtensionGenerationReport:
    dynamic = dynamic_report().binding
    generation_id = "extgen_" + "9" * 24
    binding = ExtensionGenerationBinding(
        schema_version=EXTENSION_GENERATION_BINDING_SCHEMA_VERSION,
        generation_id=generation_id,
        candidate_id=dynamic.candidate_id,
        candidate_revision=dynamic.candidate_revision,
        owner_scope_digest=dynamic.owner_scope_digest,
        authenticated_principal_digest=authenticated_local_principal_digest(
            CONTROL_TOKEN
        ),
        initiating_session_digest=initiating_session_digest(SESSION),
        request_provenance_digest="8" * 64,
        extension_id=dynamic.extension_id,
        extension_version=dynamic.extension_version,
        spec_digest=dynamic.spec_digest,
        candidate_expires_at=(BASE_TIME + timedelta(days=6)).isoformat(),
        extension_policy_revision=EXTENSION_POLICY_REVISION,
        artifact_policy_revision=EXTENSION_ARTIFACT_POLICY_REVISION,
        generation_policy_revision=EXTENSION_GENERATION_POLICY_REVISION,
        generation_policy_digest=EXTENSION_GENERATION_POLICY_DIGEST,
        generator_revision=EXTENSION_GENERATOR_REVISION,
        prompt_digest=EXTENSION_GENERATION_PROMPT_DIGEST,
        provider_id="provider.test",
        model_id="model.test",
        model_config_digest="7" * 64,
    )
    return ExtensionGenerationReport(
        schema_version=EXTENSION_GENERATION_REPORT_SCHEMA_VERSION,
        binding=binding,
        binding_digest=binding.binding_digest(),
        generation_status="quarantined",
        artifact_id=dynamic.artifact_id,
        artifact_sha256=dynamic.artifact_sha256,
        artifact_size_bytes=dynamic.artifact_size_bytes,
        artifact_envelope_digest=dynamic.artifact_envelope_digest,
        failure_code=None,
        generated_at="2026-08-02T07:59:00Z",
        authority=ExtensionGenerationAuthority(),
    )


class FakeGenerationGate:
    def __init__(self, report: ExtensionGenerationReport) -> None:
        self.report = report
        self.calls = 0

    def signed_release_subject(self, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        expected = {
            "generation_id": self.report.binding.generation_id,
            "user_id": USER,
            "workspace_id": WORKSPACE,
            "session_id": SESSION,
            "control_token": CONTROL_TOKEN,
        }
        if kwargs != expected:
            raise RuntimeError("generation release subject mismatch")
        return {
            "stage": "GENERATION_QUARANTINED",
            "report": self.report.canonical_dict(),
            "report_digest": self.report.report_digest(),
        }


class FakeDynamicValidationGate:
    def __init__(self, report: Any) -> None:
        self.report = report
        self.release_calls = 0
        self.deployment_calls = 0

    def signed_release_subject(self, **kwargs: Any) -> dict[str, Any]:
        self.release_calls += 1
        expected = {
            "validation_id": self.report.binding.validation_id,
            "user_id": USER,
            "workspace_id": WORKSPACE,
            "session_id": SESSION,
            "control_token": CONTROL_TOKEN,
        }
        if kwargs != expected:
            raise RuntimeError("validation release subject mismatch")
        return {
            "stage": "DYNAMIC_VALIDATION_PASSED",
            "report": self.report.canonical_dict(),
            "report_digest": self.report.report_digest(),
        }

    def deployment_subject(self, **kwargs: Any) -> dict[str, Any]:
        self.deployment_calls += 1
        expected = {
            "validation_id": self.report.binding.validation_id,
            "user_id": USER,
            "workspace_id": WORKSPACE,
            "expected_validation_report_digest": self.report.report_digest(),
        }
        if kwargs != expected:
            raise RuntimeError("validation deployment subject mismatch")
        return {
            "source_bytes": SOURCE,
            "source_check_spec": valid_spec(),
            "authority": ExtensionReleaseAuthority().model_dump(mode="json"),
        }


def write_test_private_key(path: Path, *, mode: int = 0o600) -> bytes:
    private = Ed25519PrivateKey.generate()
    raw_private = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        os.write(descriptor, raw_private)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return public


def initialize_store(root: Path) -> WorldStateStore:
    store = WorldStateStore(root=root)

    def bind(local: dict[str, Any]) -> None:
        local["current_project"] = WORKSPACE

    store.mutate_json("local_world.json", bind)
    return store


def build_registry(
    state_root: Path,
    key_root: Path,
    *,
    clock: Clock | None = None,
    signing_service_id: str = SIGNING_SERVICE_ID,
    validation_override: DynamicValidationReport | None = None,
) -> tuple[
    ExtensionReleaseRegistry,
    FakeGenerationGate,
    FakeDynamicValidationGate,
]:
    store = initialize_store(state_root)
    key_path = key_root / "release-ed25519.key"
    public = write_test_private_key(key_path)
    signer = Ed25519ReleaseSigner(
        private_key_path=key_path,
        signing_service_id=signing_service_id,
        forbidden_roots=[Path(__file__).resolve().parents[1], state_root],
    )
    trust = Ed25519ReleaseTrustStore({signer.key_id: public})
    generation = generation_report()
    validation = validation_override or dynamic_report()
    generation_gate = FakeGenerationGate(generation)
    validation_gate = FakeDynamicValidationGate(validation)
    registry = ExtensionReleaseRegistry(
        state_store=store,
        generation_gate=generation_gate,
        dynamic_validation_gate=validation_gate,
        signer=signer,
        trust_store=trust,
        enabled=True,
        control_token=CONTROL_TOKEN,
        now=clock or Clock(),
    )
    return registry, generation_gate, validation_gate


def create_kwargs(registry: ExtensionReleaseRegistry) -> dict[str, Any]:
    generation = registry.generation_gate.report
    validation = registry.dynamic_validation_gate.report
    return {
        "generation_id": generation.binding.generation_id,
        "validation_id": validation.binding.validation_id,
        "user_id": USER,
        "workspace_id": WORKSPACE,
        "session_id": SESSION,
        "operation_id": "release-create-1",
        "expected_registry_revision": 0,
        "expected_generation_report_digest": generation.report_digest(),
        "expected_validation_report_digest": validation.report_digest(),
        "expected_generator_identity_digest": registry._generator_identity(
            generation
        ),
        "expected_verifier_identity_digest": registry._verifier_identity(
            validation
        ),
        "expected_signing_identity_digest": registry.signer.identity_digest,
        "expires_at": "2026-08-03T08:01:00Z",
        "control_token": CONTROL_TOKEN,
    }


def snapshot_bytes(store: WorldStateStore) -> bytes:
    path = store.path_for("phase6_extension_release_state.json")
    return path.read_bytes() if path.exists() else b""


def run() -> None:
    expect(
        not any(ExtensionReleaseAuthority().model_dump(mode="python").values()),
        "release attestation grants no downstream authority",
    )
    with tempfile.TemporaryDirectory() as state_dir, tempfile.TemporaryDirectory() as key_dir:
        state_root = Path(state_dir)
        key_root = Path(key_dir)
        bad_key = key_root / "bad-mode.key"
        write_test_private_key(bad_key, mode=0o644)
        expect_raises(
            (ExtensionReleaseKeyConfigurationError,),
            lambda: Ed25519ReleaseSigner(
                private_key_path=bad_key,
                signing_service_id=SIGNING_SERVICE_ID,
                forbidden_roots=[Path(__file__).resolve().parents[1], state_root],
            ),
            "signer rejects non-0600 private key",
        )
        state_key = state_root / "state-owned.key"
        write_test_private_key(state_key)
        expect_raises(
            (ExtensionReleaseKeyConfigurationError,),
            lambda: Ed25519ReleaseSigner(
                private_key_path=state_key,
                signing_service_id=SIGNING_SERVICE_ID,
                forbidden_roots=[Path(__file__).resolve().parents[1], state_root],
            ),
            "signer rejects private keys under Veyra state",
        )

    with tempfile.TemporaryDirectory() as state_dir, tempfile.TemporaryDirectory() as key_dir:
        registry, generation_gate, validation_gate = build_registry(
            Path(state_dir), Path(key_dir)
        )
        kwargs = create_kwargs(registry)
        created = registry.create(**kwargs)
        expect(
            created["effective_status"] == "RELEASE_SIGNED"
            and created["signature_verification_status"] == "verified",
            "exact generation and dynamic validation produce one verified release",
            created,
        )
        expect(
            generation_gate.calls == 2 and validation_gate.release_calls == 2,
            "release prerequisites are checked before and after signing",
            (generation_gate.calls, validation_gate.release_calls),
        )
        attestation = parse_extension_release_attestation(
            created["attestation"]
        )
        registry.trust_store.verify_attestation(attestation)
        expect(True, "persisted Ed25519 signature verifies against raw public trust root")
        tampered = bytearray(attestation.signing_bytes())
        tampered[-1] ^= 1
        expect_raises(
            (ExtensionReleaseSignatureError,),
            lambda: registry.trust_store.verify(
                key_id=attestation.signing_key_id,
                payload=bytes(tampered),
                signature_b64url=attestation.signature_b64url,
            ),
            "Ed25519 verification rejects a one-byte manifest tamper",
        )
        mismatch = dict(kwargs)
        mismatch["operation_id"] = "release-create-identity-mismatch"
        mismatch["expected_generator_identity_digest"] = "0" * 64
        expect_raises(
            (ExtensionReleaseConflictError,),
            lambda: registry.create(**mismatch),
            "release signing rejects caller identity mismatch",
        )
        serialized = json.dumps(created, sort_keys=True)
        expect(
            SOURCE.decode("utf-8") not in serialized
            and str(registry.signer._private_key_path) not in serialized
            and not any(created["authority"].values()),
            "public release record contains no source private path or authority",
        )
        deployment = registry.deployment_subject(
            release_id=created["release_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
            session_id=SESSION,
            expected_release_revision=created["release_revision"],
            expected_attestation_digest=created["attestation_digest"],
            control_token=CONTROL_TOKEN,
        )
        expect(
            deployment["source_bytes"] == SOURCE
            and deployment["source_check_spec"]
            == parse_extension_spec(valid_spec()).canonical_dict()
            and deployment["release_manifest"]
            == attestation.manifest.canonical_dict()
            and deployment["validation_binding"]
            == dynamic_report().binding.canonical_dict()
            and not any(deployment["authority"].values()),
            "internal deployment subject returns exact source/spec/manifest/build without authority",
        )

    with tempfile.TemporaryDirectory() as state_dir, tempfile.TemporaryDirectory() as key_dir:
        collapsed, _, _ = build_registry(
            Path(state_dir),
            Path(key_dir),
            signing_service_id="provider.test",
        )
        expect_raises(
            (ExtensionReleaseConflictError,),
            lambda: collapsed.create(**create_kwargs(collapsed)),
            "signing identity cannot collapse into generator identity",
        )

    with tempfile.TemporaryDirectory() as state_dir, tempfile.TemporaryDirectory() as key_dir:
        mismatched, _, _ = build_registry(
            Path(state_dir),
            Path(key_dir),
            validation_override=dynamic_report(session_id="other-session"),
        )
        expect_raises(
            (ExtensionReleaseConflictError,),
            lambda: mismatched.create(**create_kwargs(mismatched)),
            "generation and verifier session identities must match exactly",
        )

    print("Phase 6 extension release contract smoke passed")


if __name__ == "__main__":
    run()


__all__ = [
    "BASE_TIME",
    "CONTROL_TOKEN",
    "SESSION",
    "Clock",
    "FakeDynamicValidationGate",
    "FakeGenerationGate",
    "build_registry",
    "create_kwargs",
    "dynamic_report",
    "expect",
    "expect_raises",
    "generation_report",
    "snapshot_bytes",
]
