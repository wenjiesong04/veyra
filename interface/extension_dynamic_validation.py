from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import re
from typing import Any, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

from interface.extension_artifact import MAX_EXTENSION_ARTIFACT_BYTES
from interface.extension_isolated_runner import (
    ISOLATED_RUNNER_BACKEND_KIND,
    ISOLATED_RUNNER_HARNESS_REVISION,
    ISOLATED_RUNNER_POLICY_REVISION,
)


DYNAMIC_VALIDATION_TEST_BUNDLE_SCHEMA_VERSION = (
    "veyra.phase6.dynamic_validation_test_bundle.v1"
)
DYNAMIC_VALIDATION_BINDING_SCHEMA_VERSION = (
    "veyra.phase6.dynamic_validation_binding.v1"
)
DYNAMIC_VALIDATION_REPORT_SCHEMA_VERSION = (
    "veyra.phase6.dynamic_validation_report.v1"
)
DYNAMIC_VALIDATION_HARNESS_RESULT_SCHEMA_VERSION = (
    "veyra.phase6.dynamic_validation_harness_result.v1"
)
DYNAMIC_VALIDATION_BACKEND_STATUS_SCHEMA_VERSION = (
    "veyra.phase6.dynamic_validation_backend_status.v1"
)
DYNAMIC_VALIDATION_POLICY_REVISION = (
    "veyra.phase6.dynamic_validation_policy.v1"
)
DYNAMIC_VALIDATION_HARNESS_REVISION = (
    "veyra.phase6.fixed_dynamic_validation_harness.v1"
)
DYNAMIC_VALIDATION_BACKEND_REVISION = (
    "veyra.phase6.trusted_extension_validation_runner.v1"
)

MAX_DYNAMIC_VALIDATION_VECTORS = 32
MAX_DYNAMIC_VALIDATION_BUNDLE_BYTES = 64 * 1024
MAX_DYNAMIC_VALIDATION_DOCUMENT_BYTES = 96 * 1024
DYNAMIC_VALIDATION_FUZZ_CASES = 32
DYNAMIC_VALIDATION_WALL_TIMEOUT_SECONDS = 12
MAX_DYNAMIC_VALIDATION_STDOUT_BYTES = 24 * 1024
DYNAMIC_VALIDATION_PIDS_LIMIT = 16
DYNAMIC_VALIDATION_MEMORY_BYTES = 48 * 1024 * 1024
DYNAMIC_VALIDATION_CPU_MILLIS = 500
DYNAMIC_VALIDATION_NOFILE_LIMIT = 64
DYNAMIC_VALIDATION_TMPFS_BYTES = 2 * 1024 * 1024

DYNAMIC_VALIDATION_POLICY_DIGEST = hashlib.sha256(
    json.dumps(
        {
            "candidate_operation": "fixed_dynamic_validation_only",
            "entrypoint": "run_extension",
            "candidate_shape": "single_return_dict_projection_or_constant",
            "candidate_globals": "empty_builtins",
            "candidate_mount": "private_vm_volume_read_only",
            "harness_mount": "private_vm_volume_read_only",
            "spec_mount": "private_vm_volume_read_only",
            "test_bundle_origin": "caller_frozen_outside_generator",
            "unit": "exact_frozen_vectors",
            "contract": "schema_determinism_and_input_immutability",
            "security": "fixed_ast_empty_builtins_and_isolation",
            "fuzz": DYNAMIC_VALIDATION_FUZZ_CASES,
            "behavior": "exact_frozen_vectors",
            "network": "none",
            "rootfs": "read_only",
            "capabilities": "drop_all",
            "seccomp": "builtin",
            "container_uid_gid": 65_532,
            "pids_limit": DYNAMIC_VALIDATION_PIDS_LIMIT,
            "memory_bytes": DYNAMIC_VALIDATION_MEMORY_BYTES,
            "cpu_millis": DYNAMIC_VALIDATION_CPU_MILLIS,
            "nofile_limit": DYNAMIC_VALIDATION_NOFILE_LIMIT,
            "tmpfs_bytes": DYNAMIC_VALIDATION_TMPFS_BYTES,
            "wall_timeout_seconds": DYNAMIC_VALIDATION_WALL_TIMEOUT_SECONDS,
            "stdout_bytes": MAX_DYNAMIC_VALIDATION_STDOUT_BYTES,
            "log_driver": "none",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
).hexdigest()

DYNAMIC_VALIDATION_ISSUE_CODES = (
    "artifact_identity_failed",
    "binding_identity_failed",
    "spec_identity_failed",
    "test_bundle_identity_failed",
    "source_policy_failed",
    "candidate_compile_failed",
    "candidate_load_failed",
    "unit_checks_failed",
    "contract_checks_failed",
    "security_runtime_checks_failed",
    "fuzz_checks_failed",
    "behavior_verification_failed",
    "validation_timeout",
    "validation_output_budget_exceeded",
    "validation_exit_nonzero",
    "validation_output_invalid",
    "validation_binding_mismatch",
    "validation_internal_error",
)

DynamicValidationIssueCode: TypeAlias = Literal[
    "artifact_identity_failed",
    "binding_identity_failed",
    "spec_identity_failed",
    "test_bundle_identity_failed",
    "source_policy_failed",
    "candidate_compile_failed",
    "candidate_load_failed",
    "unit_checks_failed",
    "contract_checks_failed",
    "security_runtime_checks_failed",
    "fuzz_checks_failed",
    "behavior_verification_failed",
    "validation_timeout",
    "validation_output_budget_exceeded",
    "validation_exit_nonzero",
    "validation_output_invalid",
    "validation_binding_mismatch",
    "validation_internal_error",
]

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,239}$")
_FIELD = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_VALIDATION_ID = re.compile(r"^extval_[0-9a-f]{24}$")
_RUN_ID = re.compile(r"^extrun_[0-9a-f]{24}$")
_CHECK_ID = re.compile(r"^extcheck_[0-9a-f]{24}$")
_CANDIDATE_ID = re.compile(r"^extspec_[0-9a-f]{24}$")
_ARTIFACT_ID = re.compile(r"^extart_[0-9a-f]{24}$")
_EXTENSION_ID = re.compile(r"^[a-z][a-z0-9_.-]{0,119}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_ISSUE_ORDER = {
    code: index for index, code in enumerate(DYNAMIC_VALIDATION_ISSUE_CODES)
}
_CHECK_FIELDS = (
    "unit_checks_status",
    "contract_checks_status",
    "security_runtime_checks_status",
    "fuzz_checks_status",
    "behavior_verification_status",
)


class DynamicValidationAuthority(BaseModel):
    """A validation result can never grant later lifecycle authority."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    artifact_mutation: Literal[False] = False
    artifact_export: Literal[False] = False
    workspace_access: Literal[False] = False
    host_filesystem_access: Literal[False] = False
    state_access: Literal[False] = False
    environment_inheritance: Literal[False] = False
    network_access: Literal[False] = False
    secret_access: Literal[False] = False
    tool_access: Literal[False] = False
    agent_dispatch: Literal[False] = False
    execution_outside_validation: Literal[False] = False
    signing: Literal[False] = False
    installation: Literal[False] = False
    activation: Literal[False] = False
    capability_registration: Literal[False] = False
    canary: Literal[False] = False
    promotion: Literal[False] = False
    provider_switch: Literal[False] = False


class DynamicValidationVector(BaseModel):
    """One caller-frozen behavior vector; no code or executable oracle."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    case_id: str = Field(min_length=1, max_length=120)
    input_payload: dict[str, Any] = Field(max_length=32)
    expected_output: dict[str, Any] = Field(max_length=32)

    @field_validator("case_id")
    @classmethod
    def validate_case_id(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("case_id must be a bounded opaque identifier")
        return value

    @field_validator("input_payload", "expected_output")
    @classmethod
    def validate_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        _validate_flat_json_object(value)
        return value


class DynamicValidationTestBundle(BaseModel):
    """Canonical tests frozen outside the untrusted generator."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal[DYNAMIC_VALIDATION_TEST_BUNDLE_SCHEMA_VERSION]
    bundle_id: str = Field(min_length=1, max_length=120)
    origin: Literal["caller_frozen_outside_generator"]
    vectors: list[DynamicValidationVector] = Field(
        min_length=1,
        max_length=MAX_DYNAMIC_VALIDATION_VECTORS,
    )

    @field_validator("bundle_id")
    @classmethod
    def validate_bundle_id(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("bundle_id must be a bounded opaque identifier")
        return value

    @model_validator(mode="after")
    def validate_vectors(self) -> "DynamicValidationTestBundle":
        case_ids = [item.case_id for item in self.vectors]
        if len(set(case_ids)) != len(case_ids):
            raise ValueError("dynamic validation case ids must be unique")
        self.canonical_bytes()
        return self

    def canonical_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(
            self.canonical_dict(),
            limit=MAX_DYNAMIC_VALIDATION_BUNDLE_BYTES,
        )

    def bundle_digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


class DynamicValidationBackendStatus(BaseModel):
    """Private-data-free readiness for the executable validation profile."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal[DYNAMIC_VALIDATION_BACKEND_STATUS_SCHEMA_VERSION]
    backend_kind: Literal[ISOLATED_RUNNER_BACKEND_KIND]
    availability: Literal["available", "unavailable"]
    reason_code: Literal[
        "ready",
        "isolation_backend_unavailable",
        "configuration_missing",
        "conformance_not_certified",
        "conformance_identity_mismatch",
        "harness_invalid",
    ]
    engine_identity_digest: str | None = Field(default=None, min_length=64, max_length=64)
    image_id: str | None = Field(default=None, min_length=71, max_length=71)
    isolation_conformance_digest: str | None = Field(default=None, min_length=64, max_length=64)
    validation_conformance_digest: str | None = Field(default=None, min_length=64, max_length=64)
    harness_revision: Literal[DYNAMIC_VALIDATION_HARNESS_REVISION]
    harness_digest: str = Field(min_length=64, max_length=64)
    validation_policy_revision: Literal[DYNAMIC_VALIDATION_POLICY_REVISION]
    validation_policy_digest: Literal[DYNAMIC_VALIDATION_POLICY_DIGEST]
    conformance_certified: StrictBool
    candidate_operation: Literal["fixed_dynamic_validation_only"] = (
        "fixed_dynamic_validation_only"
    )
    authority: DynamicValidationAuthority = Field(default_factory=DynamicValidationAuthority)

    @field_validator(
        "engine_identity_digest",
        "isolation_conformance_digest",
        "validation_conformance_digest",
        "harness_digest",
    )
    @classmethod
    def validate_digest(cls, value: str | None) -> str | None:
        if value is not None and not _DIGEST.fullmatch(value):
            raise ValueError("dynamic validation backend digest is invalid")
        return value

    @field_validator("image_id")
    @classmethod
    def validate_image_id(cls, value: str | None) -> str | None:
        if value is not None and not _IMAGE_ID.fullmatch(value):
            raise ValueError("dynamic validation backend image is invalid")
        return value

    @model_validator(mode="after")
    def validate_readiness(self) -> "DynamicValidationBackendStatus":
        ready = self.availability == "available"
        if ready != (self.reason_code == "ready"):
            raise ValueError("dynamic validation backend readiness is inconsistent")
        if ready and (
            not self.conformance_certified
            or self.engine_identity_digest is None
            or self.image_id is None
            or self.isolation_conformance_digest is None
            or self.validation_conformance_digest is None
        ):
            raise ValueError("available validation backend lacks exact trust identity")
        if not ready and self.conformance_certified:
            raise ValueError("unavailable validation backend cannot be certified")
        if any(self.authority.model_dump(mode="python").values()):
            raise ValueError("dynamic validation backend cannot grant authority")
        return self

    def canonical_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.canonical_dict())


class DynamicValidationBinding(BaseModel):
    """Exact immutable subject executed by the fixed validation harness."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal[DYNAMIC_VALIDATION_BINDING_SCHEMA_VERSION]
    validation_id: str = Field(min_length=31, max_length=31)
    isolated_run_id: str = Field(min_length=31, max_length=31)
    isolated_runner_binding_digest: str = Field(min_length=64, max_length=64)
    isolated_runner_report_digest: str = Field(min_length=64, max_length=64)
    isolated_runner_stage: Literal["RUNNER_JOB_PASSED"]
    isolated_runner_policy_revision: Literal[ISOLATED_RUNNER_POLICY_REVISION]
    isolated_runner_harness_revision: Literal[ISOLATED_RUNNER_HARNESS_REVISION]
    source_check_id: str = Field(min_length=33, max_length=33)
    source_check_binding_digest: str = Field(min_length=64, max_length=64)
    source_check_report_digest: str = Field(min_length=64, max_length=64)
    source_check_stage: Literal["SOURCE_CHECK_PASSED"]
    candidate_id: str = Field(min_length=32, max_length=32)
    candidate_revision: StrictInt = Field(ge=1, le=2_147_483_647)
    artifact_id: str = Field(min_length=31, max_length=31)
    artifact_revision: StrictInt = Field(ge=1, le=2_147_483_647)
    artifact_envelope_digest: str = Field(min_length=64, max_length=64)
    artifact_sha256: str = Field(min_length=64, max_length=64)
    artifact_size_bytes: StrictInt = Field(ge=1, le=MAX_EXTENSION_ARTIFACT_BYTES)
    owner_scope_digest: str = Field(min_length=64, max_length=64)
    authenticated_principal_digest: str = Field(min_length=64, max_length=64)
    initiating_session_digest: str = Field(min_length=64, max_length=64)
    request_id: str = Field(min_length=1, max_length=240)
    request_provenance_digest: str = Field(min_length=64, max_length=64)
    extension_id: str = Field(min_length=1, max_length=120)
    extension_version: StrictInt = Field(ge=1, le=2_147_483_647)
    spec_digest: str = Field(min_length=64, max_length=64)
    source_parser_identity: str = Field(min_length=1, max_length=120)
    source_ruleset_digest: str = Field(min_length=64, max_length=64)
    engine_identity_digest: str = Field(min_length=64, max_length=64)
    image_id: str = Field(min_length=71, max_length=71)
    isolation_conformance_digest: str = Field(min_length=64, max_length=64)
    validation_conformance_digest: str = Field(min_length=64, max_length=64)
    validation_policy_revision: Literal[DYNAMIC_VALIDATION_POLICY_REVISION]
    validation_policy_digest: Literal[DYNAMIC_VALIDATION_POLICY_DIGEST]
    validation_harness_revision: Literal[DYNAMIC_VALIDATION_HARNESS_REVISION]
    validation_harness_digest: str = Field(min_length=64, max_length=64)
    test_bundle_digest: str = Field(min_length=64, max_length=64)
    build_identity_digest: str = Field(min_length=64, max_length=64)

    @field_validator("validation_id")
    @classmethod
    def validate_validation_id(cls, value: str) -> str:
        if not _VALIDATION_ID.fullmatch(value):
            raise ValueError("validation_id is invalid")
        return value

    @field_validator("isolated_run_id")
    @classmethod
    def validate_run_id(cls, value: str) -> str:
        if not _RUN_ID.fullmatch(value):
            raise ValueError("isolated_run_id is invalid")
        return value

    @field_validator("source_check_id")
    @classmethod
    def validate_check_id(cls, value: str) -> str:
        if not _CHECK_ID.fullmatch(value):
            raise ValueError("source_check_id is invalid")
        return value

    @field_validator("candidate_id")
    @classmethod
    def validate_candidate_id(cls, value: str) -> str:
        if not _CANDIDATE_ID.fullmatch(value):
            raise ValueError("candidate_id is invalid")
        return value

    @field_validator("artifact_id")
    @classmethod
    def validate_artifact_id(cls, value: str) -> str:
        if not _ARTIFACT_ID.fullmatch(value):
            raise ValueError("artifact_id is invalid")
        return value

    @field_validator("extension_id")
    @classmethod
    def validate_extension_id(cls, value: str) -> str:
        if not _EXTENSION_ID.fullmatch(value):
            raise ValueError("extension_id is invalid")
        return value

    @field_validator("request_id")
    @classmethod
    def validate_request_id(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("dynamic validation request_id is invalid")
        return value

    @field_validator(
        "isolated_runner_binding_digest",
        "isolated_runner_report_digest",
        "source_check_binding_digest",
        "source_check_report_digest",
        "artifact_envelope_digest",
        "artifact_sha256",
        "owner_scope_digest",
        "authenticated_principal_digest",
        "initiating_session_digest",
        "request_provenance_digest",
        "spec_digest",
        "source_ruleset_digest",
        "engine_identity_digest",
        "isolation_conformance_digest",
        "validation_conformance_digest",
        "validation_harness_digest",
        "test_bundle_digest",
        "build_identity_digest",
    )
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if not _DIGEST.fullmatch(value):
            raise ValueError("dynamic validation binding digest is invalid")
        return value

    @field_validator("image_id")
    @classmethod
    def validate_image(cls, value: str) -> str:
        if not _IMAGE_ID.fullmatch(value):
            raise ValueError("dynamic validation image identity is invalid")
        return value

    @model_validator(mode="after")
    def validate_build_identity(self) -> "DynamicValidationBinding":
        if self.request_provenance_digest != dynamic_request_provenance_digest(
            self.model_dump(mode="json", by_alias=True)
        ):
            raise ValueError("dynamic validation request provenance mismatch")
        if self.build_identity_digest != dynamic_build_identity_digest(
            self.model_dump(mode="json", by_alias=True)
        ):
            raise ValueError("dynamic validation build identity mismatch")
        return self

    def canonical_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.canonical_dict())

    def binding_digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


class DynamicValidationHarnessResult(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal[DYNAMIC_VALIDATION_HARNESS_RESULT_SCHEMA_VERSION]
    binding_digest: str = Field(min_length=64, max_length=64)
    build_identity_digest: str = Field(min_length=64, max_length=64)
    artifact_sha256: str = Field(min_length=64, max_length=64)
    test_bundle_digest: str = Field(min_length=64, max_length=64)
    validation_status: Literal["passed", "failed"]
    candidate_execution_status: Literal["passed", "failed"]
    unit_checks_status: Literal["passed", "failed", "not_checked"]
    contract_checks_status: Literal["passed", "failed", "not_checked"]
    security_runtime_checks_status: Literal["passed", "failed", "not_checked"]
    fuzz_checks_status: Literal["passed", "failed", "not_checked"]
    behavior_verification_status: Literal["passed", "failed", "not_checked"]
    vector_count: StrictInt = Field(ge=0, le=MAX_DYNAMIC_VALIDATION_VECTORS)
    fuzz_case_count: StrictInt = Field(ge=0, le=DYNAMIC_VALIDATION_FUZZ_CASES)
    issue_codes: list[DynamicValidationIssueCode] = Field(
        default_factory=list,
        max_length=len(DYNAMIC_VALIDATION_ISSUE_CODES),
    )

    @field_validator(
        "binding_digest",
        "build_identity_digest",
        "artifact_sha256",
        "test_bundle_digest",
    )
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if not _DIGEST.fullmatch(value):
            raise ValueError("dynamic validation harness digest is invalid")
        return value

    @field_validator("issue_codes")
    @classmethod
    def validate_issues(
        cls,
        value: list[DynamicValidationIssueCode],
    ) -> list[DynamicValidationIssueCode]:
        if len(set(value)) != len(value):
            raise ValueError("dynamic validation issue codes must be unique")
        if value != sorted(value, key=lambda item: _ISSUE_ORDER[item]):
            raise ValueError("dynamic validation issue codes are out of order")
        return value

    @model_validator(mode="after")
    def validate_result(self) -> "DynamicValidationHarnessResult":
        passed = self.validation_status == "passed"
        all_checks = all(getattr(self, field) == "passed" for field in _CHECK_FIELDS)
        if passed != (
            self.candidate_execution_status == "passed"
            and all_checks
            and not self.issue_codes
            and self.vector_count > 0
            and self.fuzz_case_count == DYNAMIC_VALIDATION_FUZZ_CASES
        ):
            raise ValueError("dynamic validation harness result is inconsistent")
        if not passed and not self.issue_codes:
            raise ValueError("failed dynamic validation must include an issue")
        return self

    def canonical_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


class DynamicValidationReport(BaseModel):
    """Canonical source- and vector-free durable validation evidence."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal[DYNAMIC_VALIDATION_REPORT_SCHEMA_VERSION]
    binding: DynamicValidationBinding
    binding_digest: str = Field(min_length=64, max_length=64)
    validation_status: Literal["passed", "failed"]
    candidate_execution_status: Literal["passed", "failed"]
    unit_checks_status: Literal["passed", "failed", "not_checked"]
    contract_checks_status: Literal["passed", "failed", "not_checked"]
    security_runtime_checks_status: Literal["passed", "failed", "not_checked"]
    fuzz_checks_status: Literal["passed", "failed", "not_checked"]
    behavior_verification_status: Literal["passed", "failed", "not_checked"]
    vector_count: StrictInt = Field(ge=0, le=MAX_DYNAMIC_VALIDATION_VECTORS)
    fuzz_case_count: StrictInt = Field(ge=0, le=DYNAMIC_VALIDATION_FUZZ_CASES)
    issue_codes: list[DynamicValidationIssueCode] = Field(
        default_factory=list,
        max_length=len(DYNAMIC_VALIDATION_ISSUE_CODES),
    )
    signature_status: Literal["not_implemented"] = "not_implemented"
    activation_status: Literal["not_installed"] = "not_installed"
    capability_registry_visible: Literal[False] = False
    canary_status: Literal["not_started"] = "not_started"
    promotion_authorized: Literal[False] = False
    policy_effect: Literal["none"] = "none"
    completed_at: str = Field(min_length=20, max_length=32)
    authority: DynamicValidationAuthority = Field(default_factory=DynamicValidationAuthority)

    @field_validator("binding_digest")
    @classmethod
    def validate_binding_digest(cls, value: str) -> str:
        if not _DIGEST.fullmatch(value):
            raise ValueError("dynamic validation report digest is invalid")
        return value

    @field_validator("completed_at")
    @classmethod
    def validate_completed_at(cls, value: str) -> str:
        parsed = _parse_canonical_utc(value)
        if _canonical_utc(parsed) != value:
            raise ValueError("completed_at must be canonical UTC")
        return value

    @field_validator("issue_codes")
    @classmethod
    def validate_issues(
        cls,
        value: list[DynamicValidationIssueCode],
    ) -> list[DynamicValidationIssueCode]:
        if len(set(value)) != len(value):
            raise ValueError("dynamic validation issue codes must be unique")
        if value != sorted(value, key=lambda item: _ISSUE_ORDER[item]):
            raise ValueError("dynamic validation issue codes are out of order")
        return value

    @model_validator(mode="after")
    def validate_report(self) -> "DynamicValidationReport":
        if self.binding_digest != self.binding.binding_digest():
            raise ValueError("dynamic validation report binding mismatch")
        harness = DynamicValidationHarnessResult(
            schema_version=DYNAMIC_VALIDATION_HARNESS_RESULT_SCHEMA_VERSION,
            binding_digest=self.binding_digest,
            build_identity_digest=self.binding.build_identity_digest,
            artifact_sha256=self.binding.artifact_sha256,
            test_bundle_digest=self.binding.test_bundle_digest,
            validation_status=self.validation_status,
            candidate_execution_status=self.candidate_execution_status,
            unit_checks_status=self.unit_checks_status,
            contract_checks_status=self.contract_checks_status,
            security_runtime_checks_status=self.security_runtime_checks_status,
            fuzz_checks_status=self.fuzz_checks_status,
            behavior_verification_status=self.behavior_verification_status,
            vector_count=self.vector_count,
            fuzz_case_count=self.fuzz_case_count,
            issue_codes=self.issue_codes,
        )
        harness.canonical_dict()
        return self

    def canonical_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.canonical_dict())

    def report_digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


def dynamic_build_identity_digest(value: dict[str, Any]) -> str:
    """Build identity for the exact source/spec/test/runtime validation input."""

    fields = (
        "isolated_run_id",
        "isolated_runner_binding_digest",
        "isolated_runner_report_digest",
        "source_check_id",
        "source_check_binding_digest",
        "source_check_report_digest",
        "candidate_id",
        "candidate_revision",
        "artifact_id",
        "artifact_revision",
        "artifact_envelope_digest",
        "artifact_sha256",
        "artifact_size_bytes",
        "owner_scope_digest",
        "authenticated_principal_digest",
        "initiating_session_digest",
        "request_id",
        "request_provenance_digest",
        "extension_id",
        "extension_version",
        "spec_digest",
        "source_parser_identity",
        "source_ruleset_digest",
        "engine_identity_digest",
        "image_id",
        "isolation_conformance_digest",
        "validation_conformance_digest",
        "validation_policy_revision",
        "validation_policy_digest",
        "validation_harness_revision",
        "validation_harness_digest",
        "test_bundle_digest",
    )
    try:
        payload = {field: value[field] for field in fields}
    except KeyError as exc:
        raise ValueError("dynamic build identity input is incomplete") from exc
    return hashlib.sha256(
        _canonical_json_bytes(
            {
                "schema_version": "veyra.phase6.extension_build_identity.v1",
                **payload,
            }
        )
    ).hexdigest()


def dynamic_request_provenance_digest(value: dict[str, Any]) -> str:
    fields = (
        "request_id",
        "owner_scope_digest",
        "authenticated_principal_digest",
        "initiating_session_digest",
        "isolated_run_id",
        "test_bundle_digest",
    )
    try:
        payload = {field: value[field] for field in fields}
    except KeyError as exc:
        raise ValueError(
            "dynamic validation request provenance input is incomplete"
        ) from exc
    return hashlib.sha256(
        _canonical_json_bytes(
            {
                "schema_version": (
                    "veyra.phase6.dynamic_validation_request_provenance.v1"
                ),
                **payload,
            }
        )
    ).hexdigest()


def parse_dynamic_validation_test_bundle(value: Any) -> DynamicValidationTestBundle:
    if isinstance(value, DynamicValidationTestBundle):
        value = value.model_dump(mode="python", by_alias=True)
    parsed = DynamicValidationTestBundle.model_validate(value, strict=True)
    parsed.canonical_bytes()
    return parsed


def parse_dynamic_validation_binding(value: Any) -> DynamicValidationBinding:
    if isinstance(value, DynamicValidationBinding):
        value = value.model_dump(mode="python", by_alias=True)
    parsed = DynamicValidationBinding.model_validate(value, strict=True)
    parsed.canonical_bytes()
    return parsed


def parse_dynamic_validation_harness_result(value: Any) -> DynamicValidationHarnessResult:
    if isinstance(value, DynamicValidationHarnessResult):
        value = value.model_dump(mode="python", by_alias=True)
    parsed = DynamicValidationHarnessResult.model_validate(value, strict=True)
    _canonical_json_bytes(parsed.canonical_dict())
    return parsed


def parse_dynamic_validation_report(value: Any) -> DynamicValidationReport:
    if isinstance(value, DynamicValidationReport):
        value = value.model_dump(mode="python", by_alias=True)
    parsed = DynamicValidationReport.model_validate(value, strict=True)
    parsed.canonical_bytes()
    return parsed


def parse_dynamic_validation_backend_status(value: Any) -> DynamicValidationBackendStatus:
    if isinstance(value, DynamicValidationBackendStatus):
        value = value.model_dump(mode="python", by_alias=True)
    parsed = DynamicValidationBackendStatus.model_validate(value, strict=True)
    _canonical_json_bytes(parsed.canonical_dict())
    return parsed


def _validate_flat_json_object(value: Any) -> None:
    if not isinstance(value, dict) or len(value) > 32:
        raise ValueError("dynamic validation payload must be a bounded object")
    for key, item in value.items():
        if not isinstance(key, str) or not _FIELD.fullmatch(key):
            raise ValueError("dynamic validation payload key is invalid")
        if item is None or type(item) in {str, int, bool}:
            continue
        if type(item) is float and math.isfinite(item):
            continue
        raise ValueError("dynamic validation payload values must be JSON primitives")


def _canonical_json_bytes(
    value: Any,
    *,
    limit: int = MAX_DYNAMIC_VALIDATION_DOCUMENT_BYTES,
) -> bytes:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(payload) > limit:
        raise ValueError("dynamic validation document exceeds byte budget")
    return payload


def _parse_canonical_utc(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("timestamp must be canonical UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("timestamp must be canonical UTC") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("timestamp must be canonical UTC")
    return parsed.astimezone(timezone.utc)


def _canonical_utc(value: datetime) -> str:
    selected = value.astimezone(timezone.utc)
    if selected.microsecond:
        return selected.isoformat(timespec="microseconds").replace("+00:00", "Z")
    return selected.isoformat(timespec="seconds").replace("+00:00", "Z")


__all__ = [
    "DYNAMIC_VALIDATION_BACKEND_REVISION",
    "DYNAMIC_VALIDATION_BACKEND_STATUS_SCHEMA_VERSION",
    "DYNAMIC_VALIDATION_BINDING_SCHEMA_VERSION",
    "DYNAMIC_VALIDATION_CPU_MILLIS",
    "DYNAMIC_VALIDATION_FUZZ_CASES",
    "DYNAMIC_VALIDATION_HARNESS_RESULT_SCHEMA_VERSION",
    "DYNAMIC_VALIDATION_HARNESS_REVISION",
    "DYNAMIC_VALIDATION_ISSUE_CODES",
    "DYNAMIC_VALIDATION_MEMORY_BYTES",
    "DYNAMIC_VALIDATION_NOFILE_LIMIT",
    "DYNAMIC_VALIDATION_PIDS_LIMIT",
    "DYNAMIC_VALIDATION_POLICY_DIGEST",
    "DYNAMIC_VALIDATION_POLICY_REVISION",
    "DYNAMIC_VALIDATION_REPORT_SCHEMA_VERSION",
    "DYNAMIC_VALIDATION_TEST_BUNDLE_SCHEMA_VERSION",
    "DYNAMIC_VALIDATION_TMPFS_BYTES",
    "DYNAMIC_VALIDATION_WALL_TIMEOUT_SECONDS",
    "MAX_DYNAMIC_VALIDATION_BUNDLE_BYTES",
    "MAX_DYNAMIC_VALIDATION_DOCUMENT_BYTES",
    "MAX_DYNAMIC_VALIDATION_STDOUT_BYTES",
    "MAX_DYNAMIC_VALIDATION_VECTORS",
    "DynamicValidationAuthority",
    "DynamicValidationBackendStatus",
    "DynamicValidationBinding",
    "DynamicValidationHarnessResult",
    "DynamicValidationIssueCode",
    "DynamicValidationReport",
    "DynamicValidationTestBundle",
    "DynamicValidationVector",
    "dynamic_build_identity_digest",
    "dynamic_request_provenance_digest",
    "parse_dynamic_validation_backend_status",
    "parse_dynamic_validation_binding",
    "parse_dynamic_validation_harness_result",
    "parse_dynamic_validation_report",
    "parse_dynamic_validation_test_bundle",
]
