from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
from typing import Any, Callable

from core.world_state import WorldStateStore
from interface.extension_artifact import artifact_owner_scope_digest
from interface.extension_dynamic_validation import (
    DYNAMIC_VALIDATION_BACKEND_STATUS_SCHEMA_VERSION,
    DYNAMIC_VALIDATION_BINDING_SCHEMA_VERSION,
    DYNAMIC_VALIDATION_FUZZ_CASES,
    DYNAMIC_VALIDATION_HARNESS_REVISION,
    DYNAMIC_VALIDATION_POLICY_DIGEST,
    DYNAMIC_VALIDATION_POLICY_REVISION,
    DYNAMIC_VALIDATION_REPORT_SCHEMA_VERSION,
    DYNAMIC_VALIDATION_TEST_BUNDLE_SCHEMA_VERSION,
    DynamicValidationAuthority,
    DynamicValidationBackendStatus,
    DynamicValidationBinding,
    DynamicValidationReport,
    DynamicValidationTestBundle,
    dynamic_build_identity_digest,
    dynamic_request_provenance_digest,
    parse_dynamic_validation_test_bundle,
)
from interface.extension_isolated_runner import (
    ISOLATED_RUNNER_HARNESS_REVISION,
    ISOLATED_RUNNER_POLICY_REVISION,
)
from interface.extension_generation import (
    authenticated_local_principal_digest,
    initiating_session_digest,
)
from interface.extension_spec import (
    EXTENSION_POLICY_REVISION,
    JSON_SCHEMA_URI,
    REQUIRED_FUTURE_CHECKS,
    TCB_FORBIDDEN_PATHS,
    parse_extension_spec,
)


BASE_TIME = datetime(2026, 8, 2, 8, 0, tzinfo=timezone.utc)
USER = "phase6-dynamic-validation-user"
WORKSPACE = str(Path(__file__).resolve().parents[1])
CONTROL_TOKEN = "phase6-dynamic-validation-control-token"
SESSION = "phase6-dynamic-validation-session"
REQUEST = "phase6-dynamic-validation-request"
SOURCE = (
    b'def run_extension(payload):\n'
    b'    return {"label": payload["name"]}\n'
)


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


def valid_spec() -> dict[str, Any]:
    return {
        "schema_version": "veyra.extension_spec.v1",
        "extension_kind": "pure_function",
        "extension_id": "example.dynamic_projection",
        "version": 1,
        "purpose": "Project one bounded caller field in isolated validation.",
        "expires_at": (BASE_TIME + timedelta(days=7)).isoformat(),
        "input_schema": {
            "$schema": JSON_SCHEMA_URI,
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 120,
                },
                "count": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 100,
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        "output_schema": {
            "$schema": JSON_SCHEMA_URI,
            "type": "object",
            "properties": {
                "label": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 160,
                }
            },
            "required": ["label"],
            "additionalProperties": False,
        },
        "permissions": {
            "files": [],
            "network_hosts": [],
            "secret_ids": [],
            "external_account_ids": [],
            "max_cost_usd_cents": 0,
        },
        "side_effects": [],
        "risk_floor": "R0",
        "budgets": {
            "timeout_ms": 100,
            "cpu_ms": 50,
            "memory_bytes": 1024 * 1024,
            "output_bytes": 4 * 1024,
            "max_retries": 0,
        },
        "idempotency": {
            "mode": "pure",
            "key": "canonical_input_sha256",
        },
        "verification": {
            "strategy": "future_isolated_contract_tests",
            "required_checks": list(REQUIRED_FUTURE_CHECKS),
            "independent_verifier_required": True,
        },
        "compensation": {
            "strategy": "not_applicable_no_side_effects"
        },
        "dependencies": [],
        "artifact": {
            "status": "not_generated",
            "artifact_kind": "none",
            "code_sha256": None,
        },
        "tcb_policy": {
            "policy_revision": EXTENSION_POLICY_REVISION,
            "forbidden_paths": list(TCB_FORBIDDEN_PATHS),
            "workspace_access_allowed": False,
            "state_access_allowed": False,
            "environment_access_allowed": False,
        },
    }


def valid_test_bundle() -> dict[str, Any]:
    return {
        "schema_version": DYNAMIC_VALIDATION_TEST_BUNDLE_SCHEMA_VERSION,
        "bundle_id": "caller.behavior.v1",
        "origin": "caller_frozen_outside_generator",
        "vectors": [
            {
                "case_id": "minimum-name",
                "input_payload": {"name": "A", "count": 0},
                "expected_output": {"label": "A"},
            },
            {
                "case_id": "normal-name",
                "input_payload": {"name": "Veyra", "count": 100},
                "expected_output": {"label": "Veyra"},
            },
        ],
    }


def available_backend_status(
    *,
    harness_digest: str = "7" * 64,
) -> DynamicValidationBackendStatus:
    return DynamicValidationBackendStatus(
        schema_version=DYNAMIC_VALIDATION_BACKEND_STATUS_SCHEMA_VERSION,
        backend_kind="docker_cli",
        availability="available",
        reason_code="ready",
        engine_identity_digest="1" * 64,
        image_id="sha256:" + "2" * 64,
        isolation_conformance_digest="3" * 64,
        validation_conformance_digest="4" * 64,
        harness_revision=DYNAMIC_VALIDATION_HARNESS_REVISION,
        harness_digest=harness_digest,
        validation_policy_revision=DYNAMIC_VALIDATION_POLICY_REVISION,
        validation_policy_digest=DYNAMIC_VALIDATION_POLICY_DIGEST,
        conformance_certified=True,
        authority=DynamicValidationAuthority(),
    )


def make_subject() -> dict[str, Any]:
    spec = parse_extension_spec(valid_spec())
    return {
        "schema_version": "veyra.phase6.extension_dynamic_validation_subject.v1",
        "run_id": "extrun_" + "a" * 24,
        "isolated_runner_binding_digest": "b" * 64,
        "isolated_runner_report_digest": "c" * 64,
        "isolated_runner_stage": "RUNNER_JOB_PASSED",
        "source_check_id": "extcheck_" + "d" * 24,
        "source_check_binding_digest": "e" * 64,
        "source_check_report_digest": "f" * 64,
        "source_check_stage": "SOURCE_CHECK_PASSED",
        "candidate_id": "extspec_" + "1" * 24,
        "candidate_revision": 1,
        "artifact_id": "extart_" + "2" * 24,
        "artifact_revision": 1,
        "artifact_envelope_digest": "3" * 64,
        "artifact_sha256": hashlib.sha256(SOURCE).hexdigest(),
        "artifact_size_bytes": len(SOURCE),
        "owner_scope_digest": artifact_owner_scope_digest(USER, WORKSPACE),
        "extension_id": spec.extension_id,
        "extension_version": spec.version,
        "spec_digest": spec.digest(),
        "parser_identity": "cpython_ast_feature_3_11",
        "ruleset_digest": "5" * 64,
        "source_check_spec": spec.canonical_dict(),
        "source_bytes": SOURCE,
        "user_id": USER,
        "workspace_id": WORKSPACE,
        "authority": {
            "artifact_mutation": False,
            "artifact_export": False,
            "workspace_access": False,
            "host_filesystem_access": False,
            "state_access": False,
            "environment_access": False,
            "network_access": False,
            "secret_access": False,
            "candidate_execution": False,
            "tests": False,
            "signing": False,
            "installation": False,
            "activation": False,
            "capability_registration": False,
            "canary": False,
            "promotion": False,
        },
    }


def make_binding(
    *,
    backend: DynamicValidationBackendStatus | None = None,
    test_bundle: DynamicValidationTestBundle | None = None,
    harness_digest: str | None = None,
) -> DynamicValidationBinding:
    subject = make_subject()
    selected_backend = backend or available_backend_status(
        harness_digest=harness_digest or "7" * 64
    )
    selected_tests = test_bundle or parse_dynamic_validation_test_bundle(
        valid_test_bundle()
    )
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
        "source_check_binding_digest": subject[
            "source_check_binding_digest"
        ],
        "source_check_report_digest": subject[
            "source_check_report_digest"
        ],
        "source_check_stage": "SOURCE_CHECK_PASSED",
        "candidate_id": subject["candidate_id"],
        "candidate_revision": subject["candidate_revision"],
        "artifact_id": subject["artifact_id"],
        "artifact_revision": subject["artifact_revision"],
        "artifact_envelope_digest": subject["artifact_envelope_digest"],
        "artifact_sha256": subject["artifact_sha256"],
        "artifact_size_bytes": subject["artifact_size_bytes"],
        "owner_scope_digest": subject["owner_scope_digest"],
        "authenticated_principal_digest": (
            authenticated_local_principal_digest(CONTROL_TOKEN)
        ),
        "initiating_session_digest": initiating_session_digest(SESSION),
        "request_id": REQUEST,
        "request_provenance_digest": "0" * 64,
        "extension_id": subject["extension_id"],
        "extension_version": subject["extension_version"],
        "spec_digest": subject["spec_digest"],
        "source_parser_identity": subject["parser_identity"],
        "source_ruleset_digest": subject["ruleset_digest"],
        "engine_identity_digest": selected_backend.engine_identity_digest,
        "image_id": selected_backend.image_id,
        "isolation_conformance_digest": (
            selected_backend.isolation_conformance_digest
        ),
        "validation_conformance_digest": (
            selected_backend.validation_conformance_digest
        ),
        "validation_policy_revision": DYNAMIC_VALIDATION_POLICY_REVISION,
        "validation_policy_digest": DYNAMIC_VALIDATION_POLICY_DIGEST,
        "validation_harness_revision": DYNAMIC_VALIDATION_HARNESS_REVISION,
        "validation_harness_digest": selected_backend.harness_digest,
        "test_bundle_digest": selected_tests.bundle_digest(),
    }
    values["request_provenance_digest"] = (
        dynamic_request_provenance_digest(values)
    )
    values["build_identity_digest"] = dynamic_build_identity_digest(values)
    values["validation_id"] = "extval_" + values["build_identity_digest"][:24]
    return DynamicValidationBinding.model_validate(values, strict=True)


def passing_report(
    binding: DynamicValidationBinding,
    *,
    completed_at: str = "2026-08-02T08:00:00Z",
) -> DynamicValidationReport:
    bundle = parse_dynamic_validation_test_bundle(valid_test_bundle())
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
        vector_count=len(bundle.vectors),
        fuzz_case_count=DYNAMIC_VALIDATION_FUZZ_CASES,
        issue_codes=[],
        completed_at=completed_at,
        authority=DynamicValidationAuthority(),
    )


class FakeIsolatedRunnerGate:
    def __init__(self, subject: dict[str, Any] | None = None) -> None:
        self.subject = dict(subject or make_subject())
        self.calls = 0
        self.crash: BaseException | None = None

    def dynamic_validation_subject(self, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        if self.crash is not None:
            raise self.crash
        expected = {
            "run_id": self.subject["run_id"],
            "user_id": self.subject["user_id"],
            "workspace_id": self.subject["workspace_id"],
            "expected_artifact_revision": self.subject["artifact_revision"],
            "expected_artifact_sha256": self.subject["artifact_sha256"],
            "expected_source_check_report_digest": self.subject[
                "source_check_report_digest"
            ],
            "expected_isolated_runner_report_digest": self.subject[
                "isolated_runner_report_digest"
            ],
        }
        if kwargs != expected:
            raise RuntimeError("subject CAS mismatch")
        return dict(self.subject)


class FakeValidationBackend:
    def __init__(
        self,
        *,
        status_value: DynamicValidationBackendStatus | None = None,
        before_return: Callable[[DynamicValidationBinding], None] | None = None,
        crash: BaseException | None = None,
    ) -> None:
        self.status_value = status_value or available_backend_status()
        self.before_return = before_return
        self.crash = crash
        self.status_calls = 0
        self.run_calls = 0

    def status(self) -> DynamicValidationBackendStatus:
        self.status_calls += 1
        return self.status_value

    def run(
        self,
        *,
        artifact_bytes: bytes,
        spec: Any,
        test_bundle: Any,
        binding: DynamicValidationBinding,
    ) -> DynamicValidationReport:
        self.run_calls += 1
        if hashlib.sha256(artifact_bytes).hexdigest() != binding.artifact_sha256:
            raise RuntimeError("artifact mismatch")
        if spec.digest() != binding.spec_digest:
            raise RuntimeError("spec mismatch")
        if test_bundle.bundle_digest() != binding.test_bundle_digest:
            raise RuntimeError("test mismatch")
        if self.before_return is not None:
            self.before_return(binding)
        if self.crash is not None:
            raise self.crash
        return passing_report(binding)


def initialize_store(root: Path) -> WorldStateStore:
    store = WorldStateStore(root=root)

    def bind_workspace(local: dict[str, Any]) -> None:
        local["current_project"] = WORKSPACE

    store.mutate_json("local_world.json", bind_workspace)
    return store


__all__ = [
    "BASE_TIME",
    "CONTROL_TOKEN",
    "Clock",
    "FakeIsolatedRunnerGate",
    "FakeValidationBackend",
    "SOURCE",
    "SESSION",
    "REQUEST",
    "USER",
    "WORKSPACE",
    "available_backend_status",
    "expect",
    "expect_raises",
    "initialize_store",
    "make_binding",
    "make_subject",
    "passing_report",
    "valid_spec",
    "valid_test_bundle",
]
