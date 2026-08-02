from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
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

from interface.extension_artifact import (
    EXTENSION_ARTIFACT_POLICY_REVISION,
    MAX_EXTENSION_ARTIFACT_BYTES,
)
from interface.extension_source_check import (
    EXTENSION_SOURCE_CHECK_POLICY_REVISION,
    EXTENSION_SOURCE_CHECKER_REVISION,
    EXTENSION_SOURCE_PARSER_IDENTITY,
    EXTENSION_SOURCE_RULESET_DIGEST,
)
from interface.extension_spec import EXTENSION_POLICY_REVISION


ISOLATED_RUNNER_BINDING_SCHEMA_VERSION = (
    "veyra.phase6.isolated_runner_binding.v1"
)
ISOLATED_RUNNER_REPORT_SCHEMA_VERSION = (
    "veyra.phase6.isolated_runner_report.v1"
)
ISOLATED_RUNNER_BACKEND_STATUS_SCHEMA_VERSION = (
    "veyra.phase6.isolated_runner_backend_status.v1"
)
ISOLATED_RUNNER_BACKEND_SNAPSHOT_SCHEMA_VERSION = (
    "veyra.phase6.isolated_runner_backend_snapshot.v1"
)
ISOLATED_RUNNER_POLICY_REVISION = (
    "veyra.phase6.trusted_isolated_runner_policy.v1"
)
ISOLATED_RUNNER_HARNESS_REVISION = (
    "veyra.phase6.fixed_isolation_probe_harness.v1"
)
ISOLATED_RUNNER_BACKEND_KIND = "docker_cli"
ISOLATED_RUNNER_CONTAINER_USER = 65_532
ISOLATED_RUNNER_PIDS_LIMIT = 16
ISOLATED_RUNNER_MEMORY_BYTES = 32 * 1024 * 1024
ISOLATED_RUNNER_CPU_MILLIS = 500
ISOLATED_RUNNER_NOFILE_LIMIT = 64
ISOLATED_RUNNER_TMPFS_BYTES = 1024 * 1024
ISOLATED_RUNNER_WALL_TIMEOUT_SECONDS = 10
ISOLATED_RUNNER_BACKEND_SNAPSHOT_TTL_SECONDS = 300
MAX_ISOLATED_RUNNER_STDOUT_BYTES = 8 * 1024
MAX_ISOLATED_RUNNER_DOCUMENT_BYTES = 32 * 1024

ISOLATED_RUNNER_POLICY_DIGEST = hashlib.sha256(
    json.dumps(
        {
            "backend": ISOLATED_RUNNER_BACKEND_KIND,
            "candidate_operation": "identity_probe_only_nonexecuting",
            "candidate_mount": "private_copy_read_only",
            "harness": ISOLATED_RUNNER_HARNESS_REVISION,
            "harness_mount": "private_copy_read_only",
            "image": "exact_immutable_image_id_and_conformance_digest",
            "network": "none",
            "rootfs": "read_only",
            "capabilities": "drop_all",
            "no_new_privileges": True,
            "container_uid_gid": ISOLATED_RUNNER_CONTAINER_USER,
            "pids_limit": ISOLATED_RUNNER_PIDS_LIMIT,
            "memory_bytes": ISOLATED_RUNNER_MEMORY_BYTES,
            "memory_swap_bytes": ISOLATED_RUNNER_MEMORY_BYTES,
            "cpu_millis": ISOLATED_RUNNER_CPU_MILLIS,
            "nofile_limit": ISOLATED_RUNNER_NOFILE_LIMIT,
            "tmpfs_bytes": ISOLATED_RUNNER_TMPFS_BYTES,
            "wall_timeout_seconds": (
                ISOLATED_RUNNER_WALL_TIMEOUT_SECONDS
            ),
            "stdout_bytes": MAX_ISOLATED_RUNNER_STDOUT_BYTES,
            "log_driver": "none",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
).hexdigest()

ISOLATED_RUNNER_ISSUE_CODES = (
    "artifact_identity_failed",
    "harness_identity_failed",
    "non_root_failed",
    "rootfs_read_only_failed",
    "input_read_only_failed",
    "network_isolation_failed",
    "secret_isolation_failed",
    "host_surface_isolation_failed",
    "resource_limits_failed",
    "runner_timeout",
    "runner_output_budget_exceeded",
    "runner_exit_nonzero",
    "runner_output_invalid",
    "runner_binding_mismatch",
    "runner_internal_error",
)

IsolatedRunnerIssueCode: TypeAlias = Literal[
    "artifact_identity_failed",
    "harness_identity_failed",
    "non_root_failed",
    "rootfs_read_only_failed",
    "input_read_only_failed",
    "network_isolation_failed",
    "secret_isolation_failed",
    "host_surface_isolation_failed",
    "resource_limits_failed",
    "runner_timeout",
    "runner_output_budget_exceeded",
    "runner_exit_nonzero",
    "runner_output_invalid",
    "runner_binding_mismatch",
    "runner_internal_error",
]

_RUN_ID = re.compile(r"^extrun_[0-9a-f]{24}$")
_CHECK_ID = re.compile(r"^extcheck_[0-9a-f]{24}$")
_CANDIDATE_ID = re.compile(r"^extspec_[0-9a-f]{24}$")
_ARTIFACT_ID = re.compile(r"^extart_[0-9a-f]{24}$")
_EXTENSION_ID = re.compile(r"^[a-z][a-z0-9_.-]{0,119}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_ISSUE_ORDER = {
    code: index for index, code in enumerate(ISOLATED_RUNNER_ISSUE_CODES)
}
_PROBE_STATUS_FIELDS = (
    "artifact_identity_status",
    "harness_identity_status",
    "non_root_status",
    "rootfs_read_only_status",
    "input_read_only_status",
    "network_isolation_status",
    "secret_isolation_status",
    "host_surface_isolation_status",
    "resource_limits_status",
)


class IsolatedRunnerAuthority(BaseModel):
    """The isolation probe grants no production or candidate authority."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    generation: Literal[False] = False
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
    signing: Literal[False] = False
    installation: Literal[False] = False
    activation: Literal[False] = False
    capability_registration: Literal[False] = False
    candidate_execution: Literal[False] = False
    behavior_verification: Literal[False] = False
    canary: Literal[False] = False
    promotion: Literal[False] = False
    provider_switch: Literal[False] = False


class IsolatedRunnerBinding(BaseModel):
    """Exact private identity admitted to the fixed isolation probe."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal[ISOLATED_RUNNER_BINDING_SCHEMA_VERSION]
    run_id: str = Field(min_length=31, max_length=31)
    check_id: str = Field(min_length=33, max_length=33)
    source_check_binding_digest: str = Field(min_length=64, max_length=64)
    source_check_report_digest: str = Field(min_length=64, max_length=64)
    source_check_stage: Literal["SOURCE_CHECK_PASSED"]
    candidate_id: str = Field(min_length=32, max_length=32)
    candidate_revision: StrictInt = Field(ge=1, le=2_147_483_647)
    artifact_id: str = Field(min_length=31, max_length=31)
    artifact_revision: StrictInt = Field(ge=1, le=2_147_483_647)
    artifact_envelope_digest: str = Field(min_length=64, max_length=64)
    owner_scope_digest: str = Field(min_length=64, max_length=64)
    extension_id: str = Field(min_length=1, max_length=120)
    extension_version: StrictInt = Field(ge=1, le=2_147_483_647)
    spec_digest: str = Field(min_length=64, max_length=64)
    artifact_sha256: str = Field(min_length=64, max_length=64)
    artifact_size_bytes: StrictInt = Field(
        ge=1,
        le=MAX_EXTENSION_ARTIFACT_BYTES,
    )
    extension_policy_revision: Literal[EXTENSION_POLICY_REVISION]
    artifact_policy_revision: Literal[EXTENSION_ARTIFACT_POLICY_REVISION]
    source_check_policy_revision: Literal[
        EXTENSION_SOURCE_CHECK_POLICY_REVISION
    ]
    source_checker_revision: Literal[EXTENSION_SOURCE_CHECKER_REVISION]
    source_parser_identity: Literal[EXTENSION_SOURCE_PARSER_IDENTITY]
    source_ruleset_digest: Literal[EXTENSION_SOURCE_RULESET_DIGEST]
    runner_policy_revision: Literal[ISOLATED_RUNNER_POLICY_REVISION]
    runner_policy_digest: Literal[ISOLATED_RUNNER_POLICY_DIGEST]
    backend_kind: Literal[ISOLATED_RUNNER_BACKEND_KIND]
    engine_identity_digest: str = Field(min_length=64, max_length=64)
    image_id: str = Field(min_length=71, max_length=71)
    image_conformance_digest: str = Field(min_length=64, max_length=64)
    harness_revision: Literal[ISOLATED_RUNNER_HARNESS_REVISION]
    harness_digest: str = Field(min_length=64, max_length=64)

    @field_validator("run_id")
    @classmethod
    def validate_run_id(cls, value: str) -> str:
        if not _RUN_ID.fullmatch(value):
            raise ValueError("run_id is invalid")
        return value

    @field_validator("check_id")
    @classmethod
    def validate_check_id(cls, value: str) -> str:
        if not _CHECK_ID.fullmatch(value):
            raise ValueError("check_id is invalid")
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

    @field_validator(
        "source_check_binding_digest",
        "source_check_report_digest",
        "source_ruleset_digest",
        "engine_identity_digest",
        "artifact_envelope_digest",
        "owner_scope_digest",
        "spec_digest",
        "artifact_sha256",
        "image_conformance_digest",
        "harness_digest",
    )
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if not _DIGEST.fullmatch(value):
            raise ValueError("isolated-runner binding digest is invalid")
        return value

    @field_validator("image_id")
    @classmethod
    def validate_image_id(cls, value: str) -> str:
        if not _IMAGE_ID.fullmatch(value):
            raise ValueError("image_id must be an immutable image ID")
        return value

    def canonical_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.canonical_dict())

    def binding_digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


class IsolatedRunnerBackendStatus(BaseModel):
    """Source-, log-, path-, and environment-free backend readiness."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal[
        ISOLATED_RUNNER_BACKEND_STATUS_SCHEMA_VERSION
    ]
    backend_kind: Literal[ISOLATED_RUNNER_BACKEND_KIND]
    availability: Literal["available", "unavailable"]
    reason_code: Literal[
        "ready",
        "configuration_missing",
        "docker_unavailable",
        "engine_unavailable",
        "engine_identity_mismatch",
        "image_unavailable",
        "image_identity_mismatch",
        "conformance_not_certified",
        "conformance_identity_mismatch",
        "harness_invalid",
    ]
    runner_policy_revision: Literal[ISOLATED_RUNNER_POLICY_REVISION]
    runner_policy_digest: Literal[ISOLATED_RUNNER_POLICY_DIGEST]
    harness_revision: Literal[ISOLATED_RUNNER_HARNESS_REVISION]
    harness_digest: str = Field(min_length=64, max_length=64)
    image_id: str | None = Field(default=None, min_length=71, max_length=71)
    image_conformance_digest: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
    )
    engine_identity_digest: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
    )
    conformance_certified: StrictBool
    candidate_operation: Literal[
        "identity_probe_only_nonexecuting"
    ] = "identity_probe_only_nonexecuting"
    network_mode: Literal["none"] = "none"
    rootfs_mode: Literal["read_only"] = "read_only"
    authority: IsolatedRunnerAuthority = Field(
        default_factory=IsolatedRunnerAuthority,
    )

    @field_validator(
        "harness_digest",
        "image_conformance_digest",
        "engine_identity_digest",
    )
    @classmethod
    def validate_digest(cls, value: str | None) -> str | None:
        if value is not None and not _DIGEST.fullmatch(value):
            raise ValueError("backend status digest is invalid")
        return value

    @field_validator("image_id")
    @classmethod
    def validate_image_id(cls, value: str | None) -> str | None:
        if value is not None and not _IMAGE_ID.fullmatch(value):
            raise ValueError("backend image_id is invalid")
        return value

    @model_validator(mode="after")
    def validate_readiness(self) -> "IsolatedRunnerBackendStatus":
        available = self.availability == "available"
        if available != (self.reason_code == "ready"):
            raise ValueError("backend availability is inconsistent")
        if available and (
            not self.conformance_certified
            or self.image_id is None
            or self.image_conformance_digest is None
            or self.engine_identity_digest is None
        ):
            raise ValueError("available backend lacks exact trust binding")
        return self

    def canonical_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.canonical_dict())


class IsolatedRunnerBackendSnapshot(BaseModel):
    """Durable, sanitized result of an explicit backend observation.

    The nested backend status binds the engine, immutable image,
    conformance certificate, fixed harness and runner policy.  It contains no
    endpoint, path, command, environment or secret material and is therefore
    safe for status reads without contacting the backend again.
    """

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal[
        ISOLATED_RUNNER_BACKEND_SNAPSHOT_SCHEMA_VERSION
    ]
    observed_at: str = Field(min_length=20, max_length=32)
    backend_status: IsolatedRunnerBackendStatus
    backend_status_digest: str = Field(min_length=64, max_length=64)

    @field_validator("observed_at")
    @classmethod
    def validate_observed_at(cls, value: str) -> str:
        parsed = _parse_canonical_utc(value)
        if _canonical_utc(parsed) != value:
            raise ValueError("observed_at must be canonical UTC")
        return value

    @field_validator("backend_status_digest")
    @classmethod
    def validate_backend_status_digest(cls, value: str) -> str:
        if not _DIGEST.fullmatch(value):
            raise ValueError("backend snapshot digest is invalid")
        return value

    @model_validator(mode="after")
    def validate_status_digest(self) -> "IsolatedRunnerBackendSnapshot":
        expected = hashlib.sha256(
            self.backend_status.canonical_bytes()
        ).hexdigest()
        if self.backend_status_digest != expected:
            raise ValueError("backend snapshot status digest mismatch")
        return self

    def canonical_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.canonical_dict())


class IsolatedRunnerReport(BaseModel):
    """Canonical public result of only the fixed isolation probe."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal[ISOLATED_RUNNER_REPORT_SCHEMA_VERSION]
    binding: IsolatedRunnerBinding
    binding_digest: str = Field(min_length=64, max_length=64)
    probe_status: Literal["passed", "failed"]
    result_semantics: Literal[
        "fixed_isolation_probe_only"
    ] = "fixed_isolation_probe_only"
    artifact_identity_status: Literal[
        "passed", "failed", "not_checked"
    ]
    harness_identity_status: Literal[
        "passed", "failed", "not_checked"
    ]
    non_root_status: Literal["passed", "failed", "not_checked"]
    rootfs_read_only_status: Literal[
        "passed", "failed", "not_checked"
    ]
    input_read_only_status: Literal[
        "passed", "failed", "not_checked"
    ]
    network_isolation_status: Literal[
        "passed", "failed", "not_checked"
    ]
    secret_isolation_status: Literal[
        "passed", "failed", "not_checked"
    ]
    host_surface_isolation_status: Literal[
        "passed", "failed", "not_checked"
    ]
    resource_limits_status: Literal[
        "passed", "failed", "not_checked"
    ]
    isolated_runner_status: Literal["passed", "failed"]
    candidate_execution_status: Literal["not_started"] = "not_started"
    unit_checks_status: Literal["not_started"] = "not_started"
    contract_checks_status: Literal["not_started"] = "not_started"
    security_runtime_checks_status: Literal[
        "not_started"
    ] = "not_started"
    fuzz_checks_status: Literal["not_started"] = "not_started"
    behavior_verification_status: Literal[
        "not_started"
    ] = "not_started"
    signature_status: Literal["not_implemented"] = "not_implemented"
    activation_status: Literal["not_installed"] = "not_installed"
    capability_registry_visible: Literal[False] = False
    promotion_authorized: Literal[False] = False
    policy_effect: Literal["none"] = "none"
    issue_codes: list[IsolatedRunnerIssueCode] = Field(
        default_factory=list,
        max_length=len(ISOLATED_RUNNER_ISSUE_CODES),
    )
    completed_at: str = Field(min_length=20, max_length=32)
    authority: IsolatedRunnerAuthority = Field(
        default_factory=IsolatedRunnerAuthority,
    )

    @field_validator("binding_digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if not _DIGEST.fullmatch(value):
            raise ValueError("isolated-runner report digest is invalid")
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
    def validate_issue_codes(
        cls,
        value: list[IsolatedRunnerIssueCode],
    ) -> list[IsolatedRunnerIssueCode]:
        if len(set(value)) != len(value):
            raise ValueError("isolated-runner issue codes must be unique")
        expected = sorted(value, key=lambda item: _ISSUE_ORDER[item])
        if value != expected:
            raise ValueError(
                "isolated-runner issue codes must use canonical order"
            )
        return value

    @model_validator(mode="after")
    def validate_result(self) -> "IsolatedRunnerReport":
        if self.binding_digest != self.binding.binding_digest():
            raise ValueError("isolated-runner binding digest mismatch")
        statuses = [getattr(self, name) for name in _PROBE_STATUS_FIELDS]
        passed = self.probe_status == "passed"
        if passed != (
            self.isolated_runner_status == "passed"
            and not self.issue_codes
            and all(status == "passed" for status in statuses)
        ):
            raise ValueError("isolated-runner report status is inconsistent")
        if not passed and (
            self.isolated_runner_status != "failed"
            or not self.issue_codes
            or all(status == "passed" for status in statuses)
        ):
            raise ValueError("failed isolated-runner report is inconsistent")
        return self

    def canonical_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.canonical_dict())

    def report_digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


def parse_isolated_runner_binding(value: Any) -> IsolatedRunnerBinding:
    if isinstance(value, IsolatedRunnerBinding):
        value = value.model_dump(mode="python", by_alias=True)
    binding = IsolatedRunnerBinding.model_validate(value, strict=True)
    binding.canonical_bytes()
    return binding


def parse_isolated_runner_report(value: Any) -> IsolatedRunnerReport:
    if isinstance(value, IsolatedRunnerReport):
        value = value.model_dump(mode="python", by_alias=True)
    report = IsolatedRunnerReport.model_validate(value, strict=True)
    report.canonical_bytes()
    return report


def parse_isolated_runner_backend_status(
    value: Any,
) -> IsolatedRunnerBackendStatus:
    if isinstance(value, IsolatedRunnerBackendStatus):
        value = value.model_dump(mode="python", by_alias=True)
    status = IsolatedRunnerBackendStatus.model_validate(value, strict=True)
    status.canonical_bytes()
    return status


def parse_isolated_runner_backend_snapshot(
    value: Any,
) -> IsolatedRunnerBackendSnapshot:
    if isinstance(value, IsolatedRunnerBackendSnapshot):
        value = value.model_dump(mode="python", by_alias=True)
    snapshot = IsolatedRunnerBackendSnapshot.model_validate(
        value,
        strict=True,
    )
    snapshot.canonical_bytes()
    return snapshot


def _canonical_json_bytes(value: Any) -> bytes:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(payload) > MAX_ISOLATED_RUNNER_DOCUMENT_BYTES:
        raise ValueError("canonical isolated-runner document exceeds budget")
    return payload


def _parse_canonical_utc(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("timestamp must be canonical UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("timestamp must be canonical UTC") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(
        parsed
    ):
        raise ValueError("timestamp must be canonical UTC")
    return parsed.astimezone(timezone.utc)


def _canonical_utc(value: datetime) -> str:
    selected = value.astimezone(timezone.utc)
    if selected.microsecond:
        return selected.isoformat(
            timespec="microseconds",
        ).replace("+00:00", "Z")
    return selected.isoformat(timespec="seconds").replace("+00:00", "Z")


__all__ = [
    "ISOLATED_RUNNER_BACKEND_KIND",
    "ISOLATED_RUNNER_BACKEND_SNAPSHOT_SCHEMA_VERSION",
    "ISOLATED_RUNNER_BACKEND_SNAPSHOT_TTL_SECONDS",
    "ISOLATED_RUNNER_BACKEND_STATUS_SCHEMA_VERSION",
    "ISOLATED_RUNNER_BINDING_SCHEMA_VERSION",
    "ISOLATED_RUNNER_CONTAINER_USER",
    "ISOLATED_RUNNER_CPU_MILLIS",
    "ISOLATED_RUNNER_HARNESS_REVISION",
    "ISOLATED_RUNNER_ISSUE_CODES",
    "ISOLATED_RUNNER_MEMORY_BYTES",
    "ISOLATED_RUNNER_NOFILE_LIMIT",
    "ISOLATED_RUNNER_PIDS_LIMIT",
    "ISOLATED_RUNNER_POLICY_DIGEST",
    "ISOLATED_RUNNER_POLICY_REVISION",
    "ISOLATED_RUNNER_REPORT_SCHEMA_VERSION",
    "ISOLATED_RUNNER_TMPFS_BYTES",
    "ISOLATED_RUNNER_WALL_TIMEOUT_SECONDS",
    "MAX_ISOLATED_RUNNER_DOCUMENT_BYTES",
    "MAX_ISOLATED_RUNNER_STDOUT_BYTES",
    "IsolatedRunnerAuthority",
    "IsolatedRunnerBackendSnapshot",
    "IsolatedRunnerBackendStatus",
    "IsolatedRunnerBinding",
    "IsolatedRunnerIssueCode",
    "IsolatedRunnerReport",
    "parse_isolated_runner_backend_snapshot",
    "parse_isolated_runner_backend_status",
    "parse_isolated_runner_binding",
    "parse_isolated_runner_report",
]
