from __future__ import annotations

import hashlib
import json
import re
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
)


CAPABILITY_GAP_SCHEMA_VERSION = "veyra.phase6.capability_gap.v1"
CAPABILITY_GAP_POLICY_REVISION = (
    "veyra.phase6.capability_gap_policy.v1"
)
CAPABILITY_GAP_RECEIPT_SOURCE = (
    "explicit_local_control_plane_projection"
)

_GAP_ID = re.compile(r"^capgap_[0-9a-f]{24}$")
_CANDIDATE_ID = re.compile(r"^extspec_[0-9a-f]{24}$")
_GENERATION_ID = re.compile(r"^extgen_[0-9a-f]{24}$")
_VALIDATION_ID = re.compile(r"^extval_[0-9a-f]{24}$")
_RELEASE_ID = re.compile(r"^extrel_[0-9a-f]{24}$")
_DEPLOYMENT_ID = re.compile(r"^extdep_[0-9a-f]{24}$")
_ARTIFACT_ID = re.compile(r"^extart_[0-9a-f]{24}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class CapabilityGapAuthority(BaseModel):
    """Authority explicitly *not* conveyed by a gap or observation."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    capability_gap_recording: Literal[True] = True
    spec_link_observation: Literal[True] = True
    lifecycle_observation: Literal[True] = True
    schema_inference: Literal[False] = False
    code_generation: Literal[False] = False
    artifact_write: Literal[False] = False
    model_call: Literal[False] = False
    agent_dispatch: Literal[False] = False
    tool_call: Literal[False] = False
    execution: Literal[False] = False
    signing: Literal[False] = False
    installation: Literal[False] = False
    activation: Literal[False] = False
    canary: Literal[False] = False
    capability_registration: Literal[False] = False
    promotion: Literal[False] = False
    background_advancement: Literal[False] = False


class _ReceiptBase(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    candidate_id: str = Field(min_length=32, max_length=32)
    candidate_revision: StrictInt = Field(ge=1, le=2_147_483_647)
    spec_digest: str = Field(min_length=64, max_length=64)
    receipt_source: Literal[
        CAPABILITY_GAP_RECEIPT_SOURCE
    ] = CAPABILITY_GAP_RECEIPT_SOURCE
    authority_granted: Literal[False] = False

    @field_validator("candidate_id")
    @classmethod
    def validate_candidate_id(cls, value: str) -> str:
        if not _CANDIDATE_ID.fullmatch(value):
            raise ValueError("candidate_id is invalid")
        return value

    @field_validator("spec_digest")
    @classmethod
    def validate_spec_digest(cls, value: str) -> str:
        if not _DIGEST.fullmatch(value):
            raise ValueError("spec_digest is invalid")
        return value

    def canonical_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def digest(self) -> str:
        return canonical_digest(self.canonical_dict())


class CapabilityGapGenerationReceipt(_ReceiptBase):
    schema_version: Literal[
        "veyra.phase6.capability_gap_generation_receipt.v1"
    ]
    receipt_kind: Literal["generation"]
    generation_id: str = Field(min_length=31, max_length=31)
    generation_status: Literal[
        "GENERATION_QUARANTINED",
        "GENERATION_REJECTED",
        "GENERATION_INDETERMINATE",
    ]
    generation_report_digest: str = Field(min_length=64, max_length=64)

    @field_validator("generation_id")
    @classmethod
    def validate_generation_id(cls, value: str) -> str:
        if not _GENERATION_ID.fullmatch(value):
            raise ValueError("generation_id is invalid")
        return value

    @field_validator("generation_report_digest")
    @classmethod
    def validate_report_digest(cls, value: str) -> str:
        if not _DIGEST.fullmatch(value):
            raise ValueError("generation_report_digest is invalid")
        return value


class CapabilityGapValidationReceipt(_ReceiptBase):
    schema_version: Literal[
        "veyra.phase6.capability_gap_validation_receipt.v1"
    ]
    receipt_kind: Literal["validation"]
    generation_id: str = Field(min_length=31, max_length=31)
    validation_id: str = Field(min_length=31, max_length=31)
    artifact_id: str = Field(min_length=31, max_length=31)
    artifact_sha256: str = Field(min_length=64, max_length=64)
    validation_status: Literal[
        "DYNAMIC_VALIDATION_PASSED",
        "DYNAMIC_VALIDATION_FAILED",
        "DYNAMIC_VALIDATION_INDETERMINATE",
    ]
    validation_report_digest: str = Field(min_length=64, max_length=64)

    @field_validator("generation_id")
    @classmethod
    def validate_generation_id(cls, value: str) -> str:
        if not _GENERATION_ID.fullmatch(value):
            raise ValueError("generation_id is invalid")
        return value

    @field_validator("validation_id")
    @classmethod
    def validate_validation_id(cls, value: str) -> str:
        if not _VALIDATION_ID.fullmatch(value):
            raise ValueError("validation_id is invalid")
        return value

    @field_validator("artifact_id")
    @classmethod
    def validate_artifact_id(cls, value: str) -> str:
        if not _ARTIFACT_ID.fullmatch(value):
            raise ValueError("artifact_id is invalid")
        return value

    @field_validator("artifact_sha256", "validation_report_digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if not _DIGEST.fullmatch(value):
            raise ValueError("validation receipt digest is invalid")
        return value


class CapabilityGapReleaseReceipt(_ReceiptBase):
    schema_version: Literal[
        "veyra.phase6.capability_gap_release_receipt.v1"
    ]
    receipt_kind: Literal["release"]
    generation_id: str = Field(min_length=31, max_length=31)
    validation_id: str = Field(min_length=31, max_length=31)
    release_id: str = Field(min_length=31, max_length=31)
    release_status: Literal["RELEASE_SIGNED", "RELEASE_REVOKED"]
    release_attestation_digest: str = Field(min_length=64, max_length=64)

    @field_validator("generation_id")
    @classmethod
    def validate_generation_id(cls, value: str) -> str:
        if not _GENERATION_ID.fullmatch(value):
            raise ValueError("generation_id is invalid")
        return value

    @field_validator("validation_id")
    @classmethod
    def validate_validation_id(cls, value: str) -> str:
        if not _VALIDATION_ID.fullmatch(value):
            raise ValueError("validation_id is invalid")
        return value

    @field_validator("release_id")
    @classmethod
    def validate_release_id(cls, value: str) -> str:
        if not _RELEASE_ID.fullmatch(value):
            raise ValueError("release_id is invalid")
        return value

    @field_validator("release_attestation_digest")
    @classmethod
    def validate_attestation_digest(cls, value: str) -> str:
        if not _DIGEST.fullmatch(value):
            raise ValueError("release_attestation_digest is invalid")
        return value


class CapabilityGapDeploymentReceipt(_ReceiptBase):
    schema_version: Literal[
        "veyra.phase6.capability_gap_deployment_receipt.v1"
    ]
    receipt_kind: Literal["deployment"]
    release_id: str = Field(min_length=31, max_length=31)
    deployment_id: str = Field(min_length=31, max_length=31)
    deployment_status: Literal[
        "SHADOW_OBSERVED",
        "READ_ONLY_CANARY_OBSERVED",
        "SCOPED_CANARY_OBSERVED",
        "PROMOTED_OBSERVED",
        "ROLLED_BACK_OBSERVED",
        "DISABLED_OBSERVED",
    ]
    deployment_receipt_digest: str = Field(min_length=64, max_length=64)
    output_used_for_policy: Literal[False] = False

    @field_validator("release_id")
    @classmethod
    def validate_release_id(cls, value: str) -> str:
        if not _RELEASE_ID.fullmatch(value):
            raise ValueError("release_id is invalid")
        return value

    @field_validator("deployment_id")
    @classmethod
    def validate_deployment_id(cls, value: str) -> str:
        if not _DEPLOYMENT_ID.fullmatch(value):
            raise ValueError("deployment_id is invalid")
        return value

    @field_validator("deployment_receipt_digest")
    @classmethod
    def validate_receipt_digest(cls, value: str) -> str:
        if not _DIGEST.fullmatch(value):
            raise ValueError("deployment_receipt_digest is invalid")
        return value


CapabilityGapLifecycleReceipt: TypeAlias = Annotated[
    CapabilityGapGenerationReceipt
    | CapabilityGapValidationReceipt
    | CapabilityGapReleaseReceipt
    | CapabilityGapDeploymentReceipt,
    Field(discriminator="receipt_kind"),
]


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def deterministic_capability_gap_id(
    *,
    proposal_id: str,
    intent_id: str,
    user_id: str,
    workspace_id: str,
    session_id: str,
    source_code: str,
) -> str:
    for field, value in {
        "proposal_id": proposal_id,
        "intent_id": intent_id,
        "user_id": user_id,
        "workspace_id": workspace_id,
        "session_id": session_id,
        "source_code": source_code,
    }.items():
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            raise ValueError(f"{field} is invalid")
    digest = canonical_digest(
        {
            "schema_version": CAPABILITY_GAP_SCHEMA_VERSION,
            "proposal_id": proposal_id.strip(),
            "intent_id": intent_id.strip(),
            "user_id": user_id.strip(),
            "workspace_id": workspace_id.strip(),
            "session_id": session_id.strip(),
            "source_code": source_code.strip(),
        }
    )
    return f"capgap_{digest[:24]}"


def validate_gap_id(value: str) -> str:
    if not isinstance(value, str) or not _GAP_ID.fullmatch(value):
        raise ValueError("gap_id is invalid")
    return value


__all__ = [
    "CAPABILITY_GAP_POLICY_REVISION",
    "CAPABILITY_GAP_RECEIPT_SOURCE",
    "CAPABILITY_GAP_SCHEMA_VERSION",
    "CapabilityGapAuthority",
    "CapabilityGapDeploymentReceipt",
    "CapabilityGapGenerationReceipt",
    "CapabilityGapLifecycleReceipt",
    "CapabilityGapReleaseReceipt",
    "CapabilityGapValidationReceipt",
    "canonical_digest",
    "deterministic_capability_gap_id",
    "validate_gap_id",
]
