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


EXTENSION_DEPLOYMENT_BINDING_SCHEMA_VERSION = (
    "veyra.phase6.extension_deployment_binding.v1"
)
EXTENSION_INVOCATION_BINDING_SCHEMA_VERSION = (
    "veyra.phase6.extension_invocation_binding.v1"
)
EXTENSION_INVOCATION_RESULT_SCHEMA_VERSION = (
    "veyra.phase6.extension_invocation_result.v1"
)
EXTENSION_INVOCATION_HARNESS_RESULT_SCHEMA_VERSION = (
    "veyra.phase6.extension_invocation_harness_result.v1"
)
EXTENSION_INVOCATION_BACKEND_STATUS_SCHEMA_VERSION = (
    "veyra.phase6.extension_invocation_backend_status.v1"
)
EXTENSION_REVIEW_APPROVAL_SCHEMA_VERSION = (
    "veyra.phase6.extension_review_approval.v1"
)
EXTENSION_PUBLIC_CAPABILITY_SCHEMA_VERSION = (
    "veyra.phase6.public_extension_capability.v1"
)
EXTENSION_DEPLOYMENT_POLICY_REVISION = (
    "veyra.phase6.signed_extension_deployment_policy.v1"
)
EXTENSION_INVOCATION_HARNESS_REVISION = (
    "veyra.phase6.fixed_signed_extension_invocation_harness.v1"
)
EXTENSION_INVOCATION_RUNNER_REVISION = (
    "veyra.phase6.trusted_signed_extension_invocation_runner.v1"
)

EXTENSION_INVOCATION_WALL_TIMEOUT_SECONDS = 5
EXTENSION_INVOCATION_PIDS_LIMIT = 16
EXTENSION_INVOCATION_MEMORY_BYTES = 32 * 1024 * 1024
EXTENSION_INVOCATION_CPU_MILLIS = 250
EXTENSION_INVOCATION_NOFILE_LIMIT = 64
EXTENSION_INVOCATION_TMPFS_BYTES = 1024 * 1024
MAX_EXTENSION_INVOCATION_STDOUT_BYTES = 16 * 1024
MAX_EXTENSION_INVOCATION_DOCUMENT_BYTES = 64 * 1024
MAX_EXTENSION_DEPLOYMENT_INVOCATIONS = 100
MAX_EXTENSION_DEPLOYMENT_RECEIPTS = 128

EXTENSION_DEPLOYMENT_POLICY_DIGEST = hashlib.sha256(
    json.dumps(
        {
            "release_handoff": "exact_fresh_signed_unrevoked_private_subject",
            "candidate_kind": "pure_function_only",
            "execution": "fixed_trusted_isolated_runner_only",
            "network": "none",
            "host_binds": "none",
            "secrets": "none",
            "workspace": "none",
            "environment": "fixed_image_only",
            "rootfs": "read_only",
            "user": 65_532,
            "capabilities": "drop_all",
            "seccomp": "builtin",
            "wall_timeout_seconds": EXTENSION_INVOCATION_WALL_TIMEOUT_SECONDS,
            "pids_limit": EXTENSION_INVOCATION_PIDS_LIMIT,
            "memory_bytes": EXTENSION_INVOCATION_MEMORY_BYTES,
            "cpu_millis": EXTENSION_INVOCATION_CPU_MILLIS,
            "nofile_limit": EXTENSION_INVOCATION_NOFILE_LIMIT,
            "tmpfs_bytes": EXTENSION_INVOCATION_TMPFS_BYTES,
            "stdout_bytes": MAX_EXTENSION_INVOCATION_STDOUT_BYTES,
            "sequence": [
                "record_only",
                "shadow",
                "read_only_canary",
                "scoped_canary",
                "promoted",
            ],
            "promotion": "explicit_separate_human_review_only",
            "failure": "indeterminate_opens_breaker;repeated_failure_rolls_back",
            "automatic_promotion": False,
            "agent_dispatch": False,
            "tool_access": False,
            "provider_switch": False,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
).hexdigest()


DeploymentMode: TypeAlias = Literal[
    "disabled",
    "record_only",
    "shadow",
    "read_only_canary",
    "scoped_canary",
    "promoted",
]
InvocationStatus: TypeAlias = Literal[
    "passed", "failed", "indeterminate", "discarded"
]

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_DEPLOYMENT_ID = re.compile(r"^extdep_[0-9a-f]{24}$")
_INVOCATION_ID = re.compile(r"^extinv_[0-9a-f]{24}$")
_RELEASE_ID = re.compile(r"^extrel_[0-9a-f]{24}$")
_EXTENSION_ID = re.compile(r"^[a-z][a-z0-9_.-]{0,119}$")
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,239}$")
_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")


class ExtensionDeploymentAuthority(BaseModel):
    """Lifecycle evidence never delegates ambient Veyra authority."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    workspace_access: Literal[False] = False
    host_filesystem_access: Literal[False] = False
    state_access_by_candidate: Literal[False] = False
    environment_inheritance: Literal[False] = False
    network_access: Literal[False] = False
    secret_access: Literal[False] = False
    tool_access: Literal[False] = False
    agent_dispatch: Literal[False] = False
    provider_switch: Literal[False] = False
    self_modify: Literal[False] = False
    self_sign: Literal[False] = False
    self_promote: Literal[False] = False
    automatic_promotion: Literal[False] = False
    direct_host_execution: Literal[False] = False


class ExtensionDeploymentBinding(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal[EXTENSION_DEPLOYMENT_BINDING_SCHEMA_VERSION]
    deployment_id: str = Field(min_length=31, max_length=31)
    release_id: str = Field(min_length=31, max_length=31)
    release_revision: StrictInt = Field(ge=1, le=2_147_483_647)
    manifest_digest: str = Field(min_length=64, max_length=64)
    attestation_digest: str = Field(min_length=64, max_length=64)
    signing_key_id: str = Field(min_length=1, max_length=240)
    owner_scope_digest: str = Field(min_length=64, max_length=64)
    authenticated_principal_digest: str = Field(min_length=64, max_length=64)
    initiating_session_digest: str = Field(min_length=64, max_length=64)
    request_provenance_digest: str = Field(min_length=64, max_length=64)
    extension_id: str = Field(min_length=1, max_length=120)
    extension_version: StrictInt = Field(ge=1, le=2_147_483_647)
    artifact_sha256: str = Field(min_length=64, max_length=64)
    spec_digest: str = Field(min_length=64, max_length=64)
    mode: DeploymentMode
    mode_epoch: StrictInt = Field(ge=0, le=2_147_483_647)
    scope_digest: str = Field(min_length=64, max_length=64)
    max_invocations: StrictInt = Field(
        ge=0, le=MAX_EXTENSION_DEPLOYMENT_INVOCATIONS
    )
    expires_at: str = Field(min_length=20, max_length=32)
    runner_engine_identity_digest: str = Field(min_length=64, max_length=64)
    runner_image_id: str = Field(min_length=71, max_length=71)
    isolation_conformance_digest: str = Field(min_length=64, max_length=64)
    invocation_conformance_digest: str = Field(min_length=64, max_length=64)
    invocation_harness_digest: str = Field(min_length=64, max_length=64)
    deployment_policy_revision: Literal[EXTENSION_DEPLOYMENT_POLICY_REVISION]
    deployment_policy_digest: Literal[EXTENSION_DEPLOYMENT_POLICY_DIGEST]

    @field_validator("deployment_id")
    @classmethod
    def validate_deployment_id(cls, value: str) -> str:
        if not _DEPLOYMENT_ID.fullmatch(value):
            raise ValueError("deployment_id is invalid")
        return value

    @field_validator("release_id")
    @classmethod
    def validate_release_id(cls, value: str) -> str:
        if not _RELEASE_ID.fullmatch(value):
            raise ValueError("release_id is invalid")
        return value

    @field_validator("extension_id")
    @classmethod
    def validate_extension_id(cls, value: str) -> str:
        if not _EXTENSION_ID.fullmatch(value):
            raise ValueError("extension_id is invalid")
        return value

    @field_validator(
        "manifest_digest",
        "attestation_digest",
        "owner_scope_digest",
        "authenticated_principal_digest",
        "initiating_session_digest",
        "request_provenance_digest",
        "artifact_sha256",
        "spec_digest",
        "scope_digest",
        "runner_engine_identity_digest",
        "isolation_conformance_digest",
        "invocation_conformance_digest",
        "invocation_harness_digest",
        "deployment_policy_digest",
    )
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if not _DIGEST.fullmatch(value):
            raise ValueError("deployment digest is invalid")
        return value

    @field_validator("runner_image_id")
    @classmethod
    def validate_image_id(cls, value: str) -> str:
        if not _IMAGE_ID.fullmatch(value):
            raise ValueError("runner image id is invalid")
        return value

    @field_validator("signing_key_id")
    @classmethod
    def validate_signing_key(cls, value: str) -> str:
        if not _IDENTITY.fullmatch(value):
            raise ValueError("signing key id is invalid")
        return value

    @field_validator("expires_at")
    @classmethod
    def validate_expires_at(cls, value: str) -> str:
        if canonical_utc(parse_canonical_utc(value)) != value:
            raise ValueError("deployment expiry must be canonical UTC")
        return value

    @model_validator(mode="after")
    def validate_budget_for_mode(self) -> "ExtensionDeploymentBinding":
        if self.mode in {"disabled", "record_only"} and self.max_invocations != 0:
            raise ValueError("non-executing modes must have zero invocation budget")
        if self.mode not in {"disabled", "record_only"} and self.max_invocations < 1:
            raise ValueError("executing modes require a positive invocation budget")
        self.canonical_bytes()
        return self

    def canonical_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def canonical_bytes(self) -> bytes:
        return canonical_deployment_bytes(self.canonical_dict())

    def binding_digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


class ExtensionInvocationBinding(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal[EXTENSION_INVOCATION_BINDING_SCHEMA_VERSION]
    invocation_id: str = Field(min_length=31, max_length=31)
    deployment_id: str = Field(min_length=31, max_length=31)
    deployment_revision: StrictInt = Field(ge=1, le=2_147_483_647)
    deployment_binding_digest: str = Field(min_length=64, max_length=64)
    release_id: str = Field(min_length=31, max_length=31)
    release_revision: StrictInt = Field(ge=1, le=2_147_483_647)
    attestation_digest: str = Field(min_length=64, max_length=64)
    owner_scope_digest: str = Field(min_length=64, max_length=64)
    authenticated_principal_digest: str = Field(min_length=64, max_length=64)
    initiating_session_digest: str = Field(min_length=64, max_length=64)
    request_provenance_digest: str = Field(min_length=64, max_length=64)
    extension_id: str = Field(min_length=1, max_length=120)
    extension_version: StrictInt = Field(ge=1, le=2_147_483_647)
    artifact_sha256: str = Field(min_length=64, max_length=64)
    spec_digest: str = Field(min_length=64, max_length=64)
    input_digest: str = Field(min_length=64, max_length=64)
    mode: Literal["shadow", "read_only_canary", "scoped_canary", "promoted"]
    mode_epoch: StrictInt = Field(ge=1, le=2_147_483_647)
    scope_digest: str = Field(min_length=64, max_length=64)
    runner_engine_identity_digest: str = Field(min_length=64, max_length=64)
    runner_image_id: str = Field(min_length=71, max_length=71)
    isolation_conformance_digest: str = Field(min_length=64, max_length=64)
    invocation_conformance_digest: str = Field(min_length=64, max_length=64)
    invocation_harness_revision: Literal[EXTENSION_INVOCATION_HARNESS_REVISION]
    invocation_harness_digest: str = Field(min_length=64, max_length=64)
    deployment_policy_revision: Literal[EXTENSION_DEPLOYMENT_POLICY_REVISION]
    deployment_policy_digest: Literal[EXTENSION_DEPLOYMENT_POLICY_DIGEST]

    @field_validator("invocation_id")
    @classmethod
    def validate_invocation_id(cls, value: str) -> str:
        if not _INVOCATION_ID.fullmatch(value):
            raise ValueError("invocation_id is invalid")
        return value

    @field_validator("deployment_id")
    @classmethod
    def validate_deployment_id(cls, value: str) -> str:
        if not _DEPLOYMENT_ID.fullmatch(value):
            raise ValueError("deployment_id is invalid")
        return value

    @field_validator("release_id")
    @classmethod
    def validate_release_id(cls, value: str) -> str:
        if not _RELEASE_ID.fullmatch(value):
            raise ValueError("release_id is invalid")
        return value

    @field_validator("extension_id")
    @classmethod
    def validate_extension_id(cls, value: str) -> str:
        if not _EXTENSION_ID.fullmatch(value):
            raise ValueError("extension_id is invalid")
        return value

    @field_validator(
        "deployment_binding_digest",
        "attestation_digest",
        "owner_scope_digest",
        "authenticated_principal_digest",
        "initiating_session_digest",
        "request_provenance_digest",
        "artifact_sha256",
        "spec_digest",
        "input_digest",
        "scope_digest",
        "runner_engine_identity_digest",
        "isolation_conformance_digest",
        "invocation_conformance_digest",
        "invocation_harness_digest",
        "deployment_policy_digest",
    )
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if not _DIGEST.fullmatch(value):
            raise ValueError("invocation digest is invalid")
        return value

    @field_validator("runner_image_id")
    @classmethod
    def validate_image_id(cls, value: str) -> str:
        if not _IMAGE_ID.fullmatch(value):
            raise ValueError("runner image id is invalid")
        return value

    def canonical_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def canonical_bytes(self) -> bytes:
        return canonical_deployment_bytes(self.canonical_dict())

    def binding_digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


class ExtensionInvocationHarnessResult(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal[EXTENSION_INVOCATION_HARNESS_RESULT_SCHEMA_VERSION]
    binding_digest: str = Field(min_length=64, max_length=64)
    artifact_sha256: str = Field(min_length=64, max_length=64)
    spec_digest: str = Field(min_length=64, max_length=64)
    input_digest: str = Field(min_length=64, max_length=64)
    invocation_status: Literal["passed", "failed"]
    source_policy_status: Literal["passed", "failed"]
    contract_status: Literal["passed", "failed"]
    determinism_status: Literal["passed", "failed"]
    input_immutability_status: Literal["passed", "failed"]
    isolation_status: Literal["passed", "failed"]
    output_payload: dict[str, Any] | None = None
    output_digest: str | None = Field(default=None, min_length=64, max_length=64)
    issue_code: str | None = Field(default=None, min_length=1, max_length=120)

    @field_validator(
        "binding_digest", "artifact_sha256", "spec_digest", "input_digest"
    )
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if not _DIGEST.fullmatch(value):
            raise ValueError("harness digest is invalid")
        return value

    @field_validator("output_digest")
    @classmethod
    def validate_optional_digest(cls, value: str | None) -> str | None:
        if value is not None and not _DIGEST.fullmatch(value):
            raise ValueError("output digest is invalid")
        return value

    @model_validator(mode="after")
    def validate_result(self) -> "ExtensionInvocationHarnessResult":
        checks = (
            self.source_policy_status,
            self.contract_status,
            self.determinism_status,
            self.input_immutability_status,
            self.isolation_status,
        )
        if self.invocation_status == "passed":
            if (
                any(value != "passed" for value in checks)
                or self.output_payload is None
                or self.output_digest
                != digest_canonical_value(self.output_payload)
                or self.issue_code is not None
            ):
                raise ValueError("passed harness result is inconsistent")
        elif (
            self.output_payload is not None
            or self.output_digest is not None
            or self.issue_code is None
        ):
            raise ValueError("failed harness result is inconsistent")
        return self

    def canonical_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class ExtensionInvocationResult(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal[EXTENSION_INVOCATION_RESULT_SCHEMA_VERSION]
    binding: ExtensionInvocationBinding
    binding_digest: str = Field(min_length=64, max_length=64)
    invocation_status: InvocationStatus
    output_payload: dict[str, Any] | None = None
    output_digest: str | None = Field(default=None, min_length=64, max_length=64)
    output_discarded: StrictBool
    issue_code: str | None = Field(default=None, min_length=1, max_length=120)
    completed_at: str = Field(min_length=20, max_length=32)
    authority: ExtensionDeploymentAuthority = Field(
        default_factory=ExtensionDeploymentAuthority
    )

    @field_validator("binding_digest")
    @classmethod
    def validate_binding_digest(cls, value: str) -> str:
        if not _DIGEST.fullmatch(value):
            raise ValueError("result binding digest is invalid")
        return value

    @field_validator("output_digest")
    @classmethod
    def validate_output_digest(cls, value: str | None) -> str | None:
        if value is not None and not _DIGEST.fullmatch(value):
            raise ValueError("result output digest is invalid")
        return value

    @field_validator("completed_at")
    @classmethod
    def validate_completed_at(cls, value: str) -> str:
        if canonical_utc(parse_canonical_utc(value)) != value:
            raise ValueError("result time must be canonical UTC")
        return value

    @model_validator(mode="after")
    def validate_result(self) -> "ExtensionInvocationResult":
        if self.binding_digest != self.binding.binding_digest():
            raise ValueError("invocation result binding changed")
        if any(self.authority.model_dump(mode="python").values()):
            raise ValueError("invocation result cannot delegate authority")
        if self.invocation_status in {"passed", "discarded"}:
            if self.output_digest is None or self.issue_code is not None:
                raise ValueError("successful invocation result is inconsistent")
            if self.invocation_status == "discarded":
                if not self.output_discarded or self.output_payload is not None:
                    raise ValueError("shadow output must be discarded")
            elif (
                self.output_discarded
                or self.output_payload is None
                or digest_canonical_value(self.output_payload)
                != self.output_digest
            ):
                raise ValueError("returned invocation output is inconsistent")
        elif (
            self.output_payload is not None
            or self.output_digest is not None
            or self.output_discarded
            or self.issue_code is None
        ):
            raise ValueError("failed invocation result is inconsistent")
        return self

    def canonical_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def canonical_bytes(self) -> bytes:
        return canonical_deployment_bytes(self.canonical_dict())

    def result_digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


class ExtensionInvocationBackendStatus(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal[
        EXTENSION_INVOCATION_BACKEND_STATUS_SCHEMA_VERSION
    ]
    availability: Literal["available", "unavailable"]
    reason_code: str = Field(min_length=1, max_length=120)
    engine_identity_digest: str | None = Field(default=None, min_length=64, max_length=64)
    image_id: str | None = Field(default=None, min_length=71, max_length=71)
    isolation_conformance_digest: str | None = Field(
        default=None, min_length=64, max_length=64
    )
    invocation_conformance_digest: str | None = Field(
        default=None, min_length=64, max_length=64
    )
    harness_revision: Literal[EXTENSION_INVOCATION_HARNESS_REVISION]
    harness_digest: str = Field(min_length=64, max_length=64)
    deployment_policy_revision: Literal[EXTENSION_DEPLOYMENT_POLICY_REVISION]
    deployment_policy_digest: Literal[EXTENSION_DEPLOYMENT_POLICY_DIGEST]
    conformance_certified: StrictBool
    authority: ExtensionDeploymentAuthority = Field(
        default_factory=ExtensionDeploymentAuthority
    )

    @field_validator(
        "engine_identity_digest",
        "isolation_conformance_digest",
        "invocation_conformance_digest",
    )
    @classmethod
    def validate_optional_digest(cls, value: str | None) -> str | None:
        if value is not None and not _DIGEST.fullmatch(value):
            raise ValueError("backend digest is invalid")
        return value

    @field_validator("harness_digest", "deployment_policy_digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if not _DIGEST.fullmatch(value):
            raise ValueError("backend digest is invalid")
        return value

    @field_validator("image_id")
    @classmethod
    def validate_image_id(cls, value: str | None) -> str | None:
        if value is not None and not _IMAGE_ID.fullmatch(value):
            raise ValueError("backend image id is invalid")
        return value

    @model_validator(mode="after")
    def validate_status(self) -> "ExtensionInvocationBackendStatus":
        complete = all(
            value is not None
            for value in (
                self.engine_identity_digest,
                self.image_id,
                self.isolation_conformance_digest,
                self.invocation_conformance_digest,
            )
        )
        if self.availability == "available" and (
            not complete or not self.conformance_certified
        ):
            raise ValueError("available invocation backend is incomplete")
        if any(self.authority.model_dump(mode="python").values()):
            raise ValueError("backend status cannot delegate authority")
        return self


class ExtensionReviewApproval(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal[EXTENSION_REVIEW_APPROVAL_SCHEMA_VERSION]
    review_id: str = Field(min_length=1, max_length=240)
    status: Literal["approved", "rejected"]
    receipt_origin: Literal["veyra_review_queue_approved_identity"]
    receipt_digest: str = Field(min_length=64, max_length=64)
    deployment_id: str = Field(min_length=31, max_length=31)
    deployment_revision: StrictInt = Field(ge=1, le=2_147_483_647)
    release_id: str = Field(min_length=31, max_length=31)
    attestation_digest: str = Field(min_length=64, max_length=64)
    current_mode: Literal["read_only_canary", "scoped_canary"]
    target_mode: Literal["scoped_canary", "promoted"]
    mode_epoch: StrictInt = Field(ge=1, le=2_147_483_647)
    approver_identity_digest: str = Field(min_length=64, max_length=64)
    approved_at: str = Field(min_length=20, max_length=32)
    expires_at: str = Field(min_length=20, max_length=32)

    @field_validator("deployment_id")
    @classmethod
    def validate_deployment_id(cls, value: str) -> str:
        if not _DEPLOYMENT_ID.fullmatch(value):
            raise ValueError("review deployment id is invalid")
        return value

    @field_validator("release_id")
    @classmethod
    def validate_release_id(cls, value: str) -> str:
        if not _RELEASE_ID.fullmatch(value):
            raise ValueError("review release id is invalid")
        return value

    @field_validator(
        "attestation_digest", "approver_identity_digest", "receipt_digest"
    )
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if not _DIGEST.fullmatch(value):
            raise ValueError("review digest is invalid")
        return value

    @field_validator("approved_at", "expires_at")
    @classmethod
    def validate_time(cls, value: str) -> str:
        if canonical_utc(parse_canonical_utc(value)) != value:
            raise ValueError("review time must be canonical UTC")
        return value

    @model_validator(mode="after")
    def validate_window(self) -> "ExtensionReviewApproval":
        if parse_canonical_utc(self.approved_at) >= parse_canonical_utc(self.expires_at):
            raise ValueError("review expiry must follow approval")
        if (
            self.current_mode == "read_only_canary"
            and self.target_mode != "scoped_canary"
        ) or (
            self.current_mode == "scoped_canary"
            and self.target_mode != "promoted"
        ):
            raise ValueError("review transition binding is invalid")
        return self


class PublicExtensionCapability(BaseModel):
    """Source-free public registry row produced only by promotion."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal[EXTENSION_PUBLIC_CAPABILITY_SCHEMA_VERSION]
    capability_id: str = Field(min_length=1, max_length=120)
    extension_id: str = Field(min_length=1, max_length=120)
    extension_version: StrictInt = Field(ge=1, le=2_147_483_647)
    deployment_id: str = Field(min_length=31, max_length=31)
    release_id: str = Field(min_length=31, max_length=31)
    attestation_digest: str = Field(min_length=64, max_length=64)
    owner_scope_digest: str = Field(min_length=64, max_length=64)
    workspace_identity_digest: str = Field(min_length=64, max_length=64)
    initiating_session_digest: str = Field(min_length=64, max_length=64)
    scope_digest: str = Field(min_length=64, max_length=64)
    mode: Literal["promoted"]
    mode_epoch: StrictInt = Field(ge=1, le=2_147_483_647)
    active_pointer_revision: StrictInt = Field(ge=1, le=2_147_483_647)
    input_schema_digest: str = Field(min_length=64, max_length=64)
    output_schema_digest: str = Field(min_length=64, max_length=64)
    expires_at: str = Field(min_length=20, max_length=32)
    execution_boundary: Literal["trusted_isolated_runner_only"]
    authority: ExtensionDeploymentAuthority = Field(
        default_factory=ExtensionDeploymentAuthority
    )

    @field_validator("extension_id")
    @classmethod
    def validate_extension_id(cls, value: str) -> str:
        if not _EXTENSION_ID.fullmatch(value):
            raise ValueError("public extension id is invalid")
        return value

    @field_validator("deployment_id")
    @classmethod
    def validate_deployment_id(cls, value: str) -> str:
        if not _DEPLOYMENT_ID.fullmatch(value):
            raise ValueError("public deployment id is invalid")
        return value

    @field_validator("release_id")
    @classmethod
    def validate_release_id(cls, value: str) -> str:
        if not _RELEASE_ID.fullmatch(value):
            raise ValueError("public release id is invalid")
        return value

    @field_validator(
        "attestation_digest",
        "owner_scope_digest",
        "workspace_identity_digest",
        "initiating_session_digest",
        "scope_digest",
        "input_schema_digest",
        "output_schema_digest",
    )
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if not _DIGEST.fullmatch(value):
            raise ValueError("public capability digest is invalid")
        return value

    @field_validator("expires_at")
    @classmethod
    def validate_expires_at(cls, value: str) -> str:
        if canonical_utc(parse_canonical_utc(value)) != value:
            raise ValueError("public capability expiry must be canonical UTC")
        return value

    @model_validator(mode="after")
    def validate_authority(self) -> "PublicExtensionCapability":
        if any(self.authority.model_dump(mode="python").values()):
            raise ValueError("public capability cannot delegate ambient authority")
        return self


def parse_deployment_binding(value: Any) -> ExtensionDeploymentBinding:
    if isinstance(value, ExtensionDeploymentBinding):
        value = value.model_dump(mode="python")
    return ExtensionDeploymentBinding.model_validate(value, strict=True)


def parse_invocation_binding(value: Any) -> ExtensionInvocationBinding:
    if isinstance(value, ExtensionInvocationBinding):
        value = value.model_dump(mode="python")
    return ExtensionInvocationBinding.model_validate(value, strict=True)


def parse_invocation_harness_result(value: Any) -> ExtensionInvocationHarnessResult:
    if isinstance(value, ExtensionInvocationHarnessResult):
        value = value.model_dump(mode="python")
    return ExtensionInvocationHarnessResult.model_validate(value, strict=True)


def parse_invocation_result(value: Any) -> ExtensionInvocationResult:
    if isinstance(value, ExtensionInvocationResult):
        value = value.model_dump(mode="python")
    return ExtensionInvocationResult.model_validate(value, strict=True)


def parse_review_approval(value: Any) -> ExtensionReviewApproval:
    if isinstance(value, ExtensionReviewApproval):
        value = value.model_dump(mode="python")
    return ExtensionReviewApproval.model_validate(value, strict=True)


def parse_public_capability(value: Any) -> PublicExtensionCapability:
    if isinstance(value, PublicExtensionCapability):
        value = value.model_dump(mode="python")
    return PublicExtensionCapability.model_validate(value, strict=True)


def canonical_deployment_bytes(value: Any) -> bytes:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if not payload or len(payload) > MAX_EXTENSION_INVOCATION_DOCUMENT_BYTES:
        raise ValueError("deployment document exceeds byte budget")
    return payload


def digest_canonical_value(value: Any) -> str:
    _validate_json_value(value)
    return hashlib.sha256(canonical_deployment_bytes(value)).hexdigest()


def _validate_json_value(value: Any, *, depth: int = 0) -> None:
    if depth > 8:
        raise ValueError("JSON value exceeds depth budget")
    if value is None or type(value) in {str, bool, int}:
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("JSON number must be finite")
        return
    if isinstance(value, list):
        if len(value) > 64:
            raise ValueError("JSON list exceeds item budget")
        for item in value:
            _validate_json_value(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > 64 or any(type(key) is not str for key in value):
            raise ValueError("JSON object exceeds field budget")
        for item in value.values():
            _validate_json_value(item, depth=depth + 1)
        return
    raise ValueError("value is not canonical JSON")


def parse_canonical_utc(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("timestamp must be canonical UTC")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    if parsed.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def canonical_utc(value: datetime) -> str:
    selected = value.astimezone(timezone.utc)
    if selected.microsecond:
        return selected.isoformat(timespec="microseconds").replace("+00:00", "Z")
    return selected.isoformat(timespec="seconds").replace("+00:00", "Z")


__all__ = [
    "EXTENSION_DEPLOYMENT_BINDING_SCHEMA_VERSION",
    "EXTENSION_DEPLOYMENT_POLICY_DIGEST",
    "EXTENSION_DEPLOYMENT_POLICY_REVISION",
    "EXTENSION_INVOCATION_BACKEND_STATUS_SCHEMA_VERSION",
    "EXTENSION_INVOCATION_BINDING_SCHEMA_VERSION",
    "EXTENSION_INVOCATION_CPU_MILLIS",
    "EXTENSION_INVOCATION_HARNESS_RESULT_SCHEMA_VERSION",
    "EXTENSION_INVOCATION_HARNESS_REVISION",
    "EXTENSION_INVOCATION_MEMORY_BYTES",
    "EXTENSION_INVOCATION_NOFILE_LIMIT",
    "EXTENSION_INVOCATION_PIDS_LIMIT",
    "EXTENSION_INVOCATION_RESULT_SCHEMA_VERSION",
    "EXTENSION_INVOCATION_RUNNER_REVISION",
    "EXTENSION_INVOCATION_TMPFS_BYTES",
    "EXTENSION_INVOCATION_WALL_TIMEOUT_SECONDS",
    "EXTENSION_PUBLIC_CAPABILITY_SCHEMA_VERSION",
    "EXTENSION_REVIEW_APPROVAL_SCHEMA_VERSION",
    "MAX_EXTENSION_DEPLOYMENT_INVOCATIONS",
    "MAX_EXTENSION_DEPLOYMENT_RECEIPTS",
    "MAX_EXTENSION_INVOCATION_DOCUMENT_BYTES",
    "MAX_EXTENSION_INVOCATION_STDOUT_BYTES",
    "DeploymentMode",
    "ExtensionDeploymentAuthority",
    "ExtensionDeploymentBinding",
    "ExtensionInvocationBackendStatus",
    "ExtensionInvocationBinding",
    "ExtensionInvocationHarnessResult",
    "ExtensionInvocationResult",
    "ExtensionReviewApproval",
    "PublicExtensionCapability",
    "canonical_deployment_bytes",
    "canonical_utc",
    "digest_canonical_value",
    "parse_canonical_utc",
    "parse_deployment_binding",
    "parse_invocation_binding",
    "parse_invocation_harness_result",
    "parse_invocation_result",
    "parse_public_capability",
    "parse_review_approval",
]
