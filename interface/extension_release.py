from __future__ import annotations

from datetime import datetime, timezone
import base64
import hashlib
import json
import re
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)

from interface.extension_artifact import MAX_EXTENSION_ARTIFACT_BYTES


EXTENSION_RELEASE_MANIFEST_SCHEMA_VERSION = (
    "veyra.phase6.extension_release_manifest.v1"
)
EXTENSION_RELEASE_ATTESTATION_SCHEMA_VERSION = (
    "veyra.phase6.extension_release_attestation.v1"
)
EXTENSION_RELEASE_REVOCATION_SCHEMA_VERSION = (
    "veyra.phase6.extension_release_revocation.v1"
)
EXTENSION_RELEASE_POLICY_REVISION = (
    "veyra.phase6.extension_signed_release_policy.v1"
)
EXTENSION_RELEASE_SIGNATURE_DOMAIN = (
    b"veyra.phase6.extension_release.ed25519.v1\x00"
)
MAX_EXTENSION_RELEASE_DOCUMENT_BYTES = 64 * 1024

EXTENSION_RELEASE_POLICY_DIGEST = hashlib.sha256(
    json.dumps(
        {
            "policy_revision": EXTENSION_RELEASE_POLICY_REVISION,
            "prerequisite": (
                "exact durable generation plus exact passed dynamic validation"
            ),
            "signature_algorithm": "ed25519",
            "private_key_location": "explicit_external_0600_file",
            "registry": "private_content_addressed_immutable",
            "lifecycle": "append_only_signed_or_revoked",
            "candidate_execution": False,
            "installation": False,
            "activation": False,
            "capability_registration": False,
            "canary": False,
            "promotion": False,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
).hexdigest()

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_RELEASE_ID = re.compile(r"^extrel_[0-9a-f]{24}$")
_GENERATION_ID = re.compile(r"^extgen_[0-9a-f]{24}$")
_VALIDATION_ID = re.compile(r"^extval_[0-9a-f]{24}$")
_CANDIDATE_ID = re.compile(r"^extspec_[0-9a-f]{24}$")
_ARTIFACT_ID = re.compile(r"^extart_[0-9a-f]{24}$")
_RUN_ID = re.compile(r"^extrun_[0-9a-f]{24}$")
_CHECK_ID = re.compile(r"^extcheck_[0-9a-f]{24}$")
_EXTENSION_ID = re.compile(r"^[a-z][a-z0-9_.-]{0,119}$")
_KEY_ID = re.compile(r"^ed25519_[0-9a-f]{64}$")
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,239}$")


class ExtensionReleaseAuthority(BaseModel):
    """A signed attestation is evidence, never delegated runtime authority."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    artifact_export: Literal[False] = False
    workspace_access: Literal[False] = False
    host_filesystem_access: Literal[False] = False
    state_access_by_candidate: Literal[False] = False
    environment_inheritance: Literal[False] = False
    network_access: Literal[False] = False
    secret_access: Literal[False] = False
    tool_access: Literal[False] = False
    agent_dispatch: Literal[False] = False
    candidate_execution: Literal[False] = False
    behavior_verification: Literal[False] = False
    signing_authority: Literal[False] = False
    installation: Literal[False] = False
    activation: Literal[False] = False
    capability_registration: Literal[False] = False
    canary: Literal[False] = False
    promotion: Literal[False] = False
    provider_switch: Literal[False] = False


class ExtensionReleaseManifest(BaseModel):
    """Canonical source-free identity for one independently signed release."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal[EXTENSION_RELEASE_MANIFEST_SCHEMA_VERSION]

    owner_scope_digest: str = Field(min_length=64, max_length=64)
    workspace_identity_digest: str = Field(min_length=64, max_length=64)
    authenticated_principal_digest: str = Field(min_length=64, max_length=64)
    initiating_session_digest: str = Field(min_length=64, max_length=64)
    request_provenance_digest: str = Field(min_length=64, max_length=64)

    generation_id: str = Field(min_length=31, max_length=31)
    generation_binding_digest: str = Field(min_length=64, max_length=64)
    generation_report_digest: str = Field(min_length=64, max_length=64)
    generator_revision: str = Field(min_length=1, max_length=240)
    generation_policy_revision: str = Field(min_length=1, max_length=240)
    generation_policy_digest: str = Field(min_length=64, max_length=64)
    generation_prompt_digest: str = Field(min_length=64, max_length=64)
    generator_provider_id: str = Field(min_length=1, max_length=240)
    generator_model_id: str = Field(min_length=1, max_length=240)
    generator_model_config_digest: str = Field(min_length=64, max_length=64)
    generator_identity_digest: str = Field(min_length=64, max_length=64)

    validation_id: str = Field(min_length=31, max_length=31)
    validation_binding_digest: str = Field(min_length=64, max_length=64)
    validation_report_digest: str = Field(min_length=64, max_length=64)
    validation_authenticated_principal_digest: str = Field(
        min_length=64,
        max_length=64,
    )
    validation_initiating_session_digest: str = Field(
        min_length=64,
        max_length=64,
    )
    validation_request_id: str = Field(min_length=1, max_length=240)
    validation_request_provenance_digest: str = Field(
        min_length=64,
        max_length=64,
    )
    validation_build_identity_digest: str = Field(min_length=64, max_length=64)
    validation_test_bundle_digest: str = Field(min_length=64, max_length=64)
    validation_policy_revision: str = Field(min_length=1, max_length=240)
    validation_policy_digest: str = Field(min_length=64, max_length=64)
    validation_harness_revision: str = Field(min_length=1, max_length=240)
    validation_harness_digest: str = Field(min_length=64, max_length=64)
    validation_engine_identity_digest: str = Field(min_length=64, max_length=64)
    validation_image_id: str = Field(min_length=71, max_length=71)
    isolation_conformance_digest: str = Field(min_length=64, max_length=64)
    validation_conformance_digest: str = Field(min_length=64, max_length=64)
    verifier_identity_digest: str = Field(min_length=64, max_length=64)

    isolated_run_id: str = Field(min_length=31, max_length=31)
    isolated_runner_binding_digest: str = Field(min_length=64, max_length=64)
    isolated_runner_report_digest: str = Field(min_length=64, max_length=64)
    isolated_runner_policy_revision: str = Field(min_length=1, max_length=240)
    isolated_runner_harness_revision: str = Field(min_length=1, max_length=240)

    source_check_id: str = Field(min_length=33, max_length=33)
    source_check_binding_digest: str = Field(min_length=64, max_length=64)
    source_check_report_digest: str = Field(min_length=64, max_length=64)
    source_parser_identity: str = Field(min_length=1, max_length=240)
    source_ruleset_digest: str = Field(min_length=64, max_length=64)

    candidate_id: str = Field(min_length=32, max_length=32)
    candidate_revision: StrictInt = Field(ge=1, le=2_147_483_647)
    extension_id: str = Field(min_length=1, max_length=120)
    extension_version: StrictInt = Field(ge=1, le=2_147_483_647)
    spec_digest: str = Field(min_length=64, max_length=64)
    artifact_id: str = Field(min_length=31, max_length=31)
    artifact_revision: StrictInt = Field(ge=1, le=2_147_483_647)
    artifact_envelope_digest: str = Field(min_length=64, max_length=64)
    artifact_sha256: str = Field(min_length=64, max_length=64)
    artifact_size_bytes: StrictInt = Field(
        ge=1,
        le=MAX_EXTENSION_ARTIFACT_BYTES,
    )

    signing_service_id: str = Field(min_length=1, max_length=240)
    signing_service_identity_digest: str = Field(min_length=64, max_length=64)
    signing_key_id: str = Field(min_length=72, max_length=72)
    release_policy_revision: Literal[EXTENSION_RELEASE_POLICY_REVISION]
    release_policy_digest: Literal[EXTENSION_RELEASE_POLICY_DIGEST]
    created_at: str = Field(min_length=20, max_length=32)
    expires_at: str = Field(min_length=20, max_length=32)

    @field_validator(
        "owner_scope_digest",
        "workspace_identity_digest",
        "authenticated_principal_digest",
        "initiating_session_digest",
        "request_provenance_digest",
        "generation_binding_digest",
        "generation_report_digest",
        "generation_policy_digest",
        "generation_prompt_digest",
        "generator_model_config_digest",
        "generator_identity_digest",
        "validation_binding_digest",
        "validation_report_digest",
        "validation_authenticated_principal_digest",
        "validation_initiating_session_digest",
        "validation_request_provenance_digest",
        "validation_build_identity_digest",
        "validation_test_bundle_digest",
        "validation_policy_digest",
        "validation_harness_digest",
        "validation_engine_identity_digest",
        "isolation_conformance_digest",
        "validation_conformance_digest",
        "verifier_identity_digest",
        "isolated_runner_binding_digest",
        "isolated_runner_report_digest",
        "source_check_binding_digest",
        "source_check_report_digest",
        "source_ruleset_digest",
        "spec_digest",
        "artifact_envelope_digest",
        "artifact_sha256",
        "signing_service_identity_digest",
        "release_policy_digest",
    )
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if not _DIGEST.fullmatch(value):
            raise ValueError("release manifest digest is invalid")
        return value

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

    @field_validator("extension_id")
    @classmethod
    def validate_extension_id(cls, value: str) -> str:
        if not _EXTENSION_ID.fullmatch(value):
            raise ValueError("extension_id is invalid")
        return value

    @field_validator(
        "generator_revision",
        "generation_policy_revision",
        "generator_provider_id",
        "generator_model_id",
        "validation_policy_revision",
        "validation_harness_revision",
        "validation_request_id",
        "isolated_runner_policy_revision",
        "isolated_runner_harness_revision",
        "source_parser_identity",
        "signing_service_id",
    )
    @classmethod
    def validate_identity(cls, value: str) -> str:
        if not _IDENTITY.fullmatch(value):
            raise ValueError("release identity is invalid")
        return value

    @field_validator("signing_key_id")
    @classmethod
    def validate_key_id(cls, value: str) -> str:
        if not _KEY_ID.fullmatch(value):
            raise ValueError("signing key id is invalid")
        return value

    @field_validator("validation_image_id")
    @classmethod
    def validate_image_id(cls, value: str) -> str:
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
            raise ValueError("validation image id is invalid")
        return value

    @field_validator("created_at", "expires_at")
    @classmethod
    def validate_timestamp(cls, value: str) -> str:
        if canonical_utc(parse_canonical_utc(value)) != value:
            raise ValueError("release timestamp must be canonical UTC")
        return value

    @model_validator(mode="after")
    def validate_lifetime_and_identities(self) -> "ExtensionReleaseManifest":
        if parse_canonical_utc(self.expires_at) <= parse_canonical_utc(
            self.created_at
        ):
            raise ValueError("release must expire after creation")
        identities = {
            self.generator_identity_digest,
            self.verifier_identity_digest,
            self.signing_service_identity_digest,
        }
        if len(identities) != 3:
            raise ValueError("generator verifier and signer identities must differ")
        self.canonical_bytes()
        return self

    def canonical_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def canonical_bytes(self) -> bytes:
        return canonical_release_bytes(self.canonical_dict())

    def manifest_digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    def release_id(self) -> str:
        return "extrel_" + self.manifest_digest()[:24]


class ExtensionReleaseAttestation(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal[EXTENSION_RELEASE_ATTESTATION_SCHEMA_VERSION]
    release_id: str = Field(min_length=31, max_length=31)
    manifest: ExtensionReleaseManifest
    manifest_digest: str = Field(min_length=64, max_length=64)
    signature_algorithm: Literal["ed25519"] = "ed25519"
    signing_key_id: str = Field(min_length=72, max_length=72)
    signature_b64url: str = Field(min_length=86, max_length=86)
    authority: ExtensionReleaseAuthority = Field(
        default_factory=ExtensionReleaseAuthority
    )

    @field_validator("release_id")
    @classmethod
    def validate_release_id(cls, value: str) -> str:
        if not _RELEASE_ID.fullmatch(value):
            raise ValueError("release_id is invalid")
        return value

    @field_validator("manifest_digest")
    @classmethod
    def validate_manifest_digest(cls, value: str) -> str:
        if not _DIGEST.fullmatch(value):
            raise ValueError("manifest digest is invalid")
        return value

    @field_validator("signing_key_id")
    @classmethod
    def validate_key_id(cls, value: str) -> str:
        if not _KEY_ID.fullmatch(value):
            raise ValueError("signing key id is invalid")
        return value

    @field_validator("signature_b64url")
    @classmethod
    def validate_signature(cls, value: str) -> str:
        try:
            raw = base64.urlsafe_b64decode(value + "==")
        except Exception as exc:
            raise ValueError("release signature encoding is invalid") from exc
        if len(raw) != 64 or base64.urlsafe_b64encode(raw).decode("ascii").rstrip(
            "="
        ) != value:
            raise ValueError("release signature encoding is invalid")
        return value

    @model_validator(mode="after")
    def validate_binding(self) -> "ExtensionReleaseAttestation":
        if (
            self.manifest.manifest_digest() != self.manifest_digest
            or self.manifest.release_id() != self.release_id
            or self.manifest.signing_key_id != self.signing_key_id
            or any(self.authority.model_dump(mode="python").values())
        ):
            raise ValueError("release attestation binding is invalid")
        self.canonical_bytes()
        return self

    def signing_bytes(self) -> bytes:
        return EXTENSION_RELEASE_SIGNATURE_DOMAIN + self.manifest.canonical_bytes()

    def canonical_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def canonical_bytes(self) -> bytes:
        return canonical_release_bytes(self.canonical_dict())

    def attestation_digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


class ExtensionReleaseRevocation(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal[EXTENSION_RELEASE_REVOCATION_SCHEMA_VERSION]
    release_id: str = Field(min_length=31, max_length=31)
    reason_digest: str = Field(min_length=64, max_length=64)
    source: Literal["explicit_local_control_plane"]
    recorded_at: str = Field(min_length=20, max_length=32)

    @field_validator("release_id")
    @classmethod
    def validate_release_id(cls, value: str) -> str:
        if not _RELEASE_ID.fullmatch(value):
            raise ValueError("release_id is invalid")
        return value

    @field_validator("reason_digest")
    @classmethod
    def validate_reason_digest(cls, value: str) -> str:
        if not _DIGEST.fullmatch(value):
            raise ValueError("revocation reason digest is invalid")
        return value

    @field_validator("recorded_at")
    @classmethod
    def validate_recorded_at(cls, value: str) -> str:
        if canonical_utc(parse_canonical_utc(value)) != value:
            raise ValueError("revocation time must be canonical UTC")
        return value


def parse_extension_release_manifest(value: Any) -> ExtensionReleaseManifest:
    if isinstance(value, ExtensionReleaseManifest):
        value = value.model_dump(mode="python")
    parsed = ExtensionReleaseManifest.model_validate(value, strict=True)
    parsed.canonical_bytes()
    return parsed


def parse_extension_release_attestation(
    value: Any,
) -> ExtensionReleaseAttestation:
    if isinstance(value, ExtensionReleaseAttestation):
        value = value.model_dump(mode="python")
    parsed = ExtensionReleaseAttestation.model_validate(value, strict=True)
    parsed.canonical_bytes()
    return parsed


def parse_extension_release_revocation(
    value: Any,
) -> ExtensionReleaseRevocation:
    if isinstance(value, ExtensionReleaseRevocation):
        value = value.model_dump(mode="python")
    return ExtensionReleaseRevocation.model_validate(value, strict=True)


def canonical_release_bytes(value: Any) -> bytes:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(payload) > MAX_EXTENSION_RELEASE_DOCUMENT_BYTES:
        raise ValueError("release document exceeds byte budget")
    return payload


def parse_canonical_utc(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("timestamp must be canonical UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def canonical_utc(value: datetime) -> str:
    selected = value.astimezone(timezone.utc)
    if selected.microsecond:
        return selected.isoformat(timespec="microseconds").replace(
            "+00:00", "Z"
        )
    return selected.isoformat(timespec="seconds").replace("+00:00", "Z")


__all__ = [
    "EXTENSION_RELEASE_ATTESTATION_SCHEMA_VERSION",
    "EXTENSION_RELEASE_MANIFEST_SCHEMA_VERSION",
    "EXTENSION_RELEASE_POLICY_DIGEST",
    "EXTENSION_RELEASE_POLICY_REVISION",
    "EXTENSION_RELEASE_REVOCATION_SCHEMA_VERSION",
    "EXTENSION_RELEASE_SIGNATURE_DOMAIN",
    "ExtensionReleaseAttestation",
    "ExtensionReleaseAuthority",
    "ExtensionReleaseManifest",
    "ExtensionReleaseRevocation",
    "canonical_release_bytes",
    "canonical_utc",
    "parse_canonical_utc",
    "parse_extension_release_attestation",
    "parse_extension_release_manifest",
    "parse_extension_release_revocation",
]
