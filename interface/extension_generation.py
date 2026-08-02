from __future__ import annotations

from datetime import datetime, timezone
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

from interface.extension_artifact import (
    EXTENSION_ARTIFACT_POLICY_REVISION,
    MAX_EXTENSION_ARTIFACT_BYTES,
)
from interface.extension_spec import EXTENSION_POLICY_REVISION


EXTENSION_GENERATION_BINDING_SCHEMA_VERSION = (
    "veyra.phase6.extension_generation_binding.v1"
)
EXTENSION_GENERATION_REPORT_SCHEMA_VERSION = (
    "veyra.phase6.extension_generation_report.v1"
)
EXTENSION_GENERATOR_REVISION = (
    "veyra.phase6.bounded_model_source_generator.v1"
)
EXTENSION_GENERATION_POLICY_REVISION = (
    "veyra.phase6.extension_generation_policy.v1"
)
MAX_EXTENSION_GENERATION_REPORT_BYTES = 16 * 1024

_GENERATION_ID = re.compile(r"^extgen_[0-9a-f]{24}$")
_CANDIDATE_ID = re.compile(r"^extspec_[0-9a-f]{24}$")
_ARTIFACT_ID = re.compile(r"^extart_[0-9a-f]{24}$")
_EXTENSION_ID = re.compile(r"^[a-z][a-z0-9_.-]{0,119}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,239}$")


EXTENSION_GENERATION_PROMPT = """You are a bounded source candidate generator.
Return strict JSON with exactly one field named source. The source must be UTF-8
Python defining exactly one function named run_extension(payload). The function
must contain exactly one return statement returning a dictionary. Every output
value must be either a JSON primitive constant or a direct payload[\"field\"]
projection. Do not use imports, calls, attributes, annotations, decorators,
defaults, loops, comprehensions, conditions, exceptions, assignments, globals,
comments, Markdown fences, files, network, environment, tools, or side effects.
Use only the supplied immutable ExtensionSpec. Do not claim the candidate was
tested, verified, signed, installed, activated, or promoted."""

EXTENSION_GENERATION_PROMPT_DIGEST = hashlib.sha256(
    EXTENSION_GENERATION_PROMPT.encode("utf-8")
).hexdigest()

EXTENSION_GENERATION_POLICY_DIGEST = hashlib.sha256(
    json.dumps(
        {
            "policy_revision": EXTENSION_GENERATION_POLICY_REVISION,
            "generator_revision": EXTENSION_GENERATOR_REVISION,
            "prompt_digest": EXTENSION_GENERATION_PROMPT_DIGEST,
            "input": "exact_passed_extension_spec_only",
            "output": "strict_json_source_data_only",
            "artifact_destination": "private_immutable_quarantine",
            "candidate_execution": False,
            "tests": False,
            "signing": False,
            "activation": False,
            "promotion": False,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
).hexdigest()


class ExtensionGenerationAuthority(BaseModel):
    """The exact authority granted by the generation-to-quarantine stage."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    code_generation: Literal[True] = True
    private_artifact_quarantine_write: Literal[True] = True
    workspace_access: Literal[False] = False
    host_filesystem_access: Literal[False] = False
    state_access_by_candidate: Literal[False] = False
    environment_access_by_candidate: Literal[False] = False
    network_access_by_candidate: Literal[False] = False
    secret_access_by_candidate: Literal[False] = False
    tool_access: Literal[False] = False
    agent_dispatch: Literal[False] = False
    candidate_execution: Literal[False] = False
    behavior_verification: Literal[False] = False
    signing: Literal[False] = False
    installation: Literal[False] = False
    activation: Literal[False] = False
    capability_registration: Literal[False] = False
    canary: Literal[False] = False
    promotion: Literal[False] = False
    provider_switch: Literal[False] = False


class ExtensionGenerationBinding(BaseModel):
    """Exact, source-free identity admitted to one bounded generation call."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal[
        EXTENSION_GENERATION_BINDING_SCHEMA_VERSION
    ]
    generation_id: str = Field(min_length=31, max_length=31)
    candidate_id: str = Field(min_length=32, max_length=32)
    candidate_revision: StrictInt = Field(ge=1, le=2_147_483_647)
    owner_scope_digest: str = Field(min_length=64, max_length=64)
    authenticated_principal_digest: str = Field(
        min_length=64,
        max_length=64,
    )
    initiating_session_digest: str = Field(min_length=64, max_length=64)
    request_provenance_digest: str = Field(min_length=64, max_length=64)
    extension_id: str = Field(min_length=1, max_length=120)
    extension_version: StrictInt = Field(ge=1, le=2_147_483_647)
    spec_digest: str = Field(min_length=64, max_length=64)
    candidate_expires_at: str = Field(min_length=20, max_length=40)
    extension_policy_revision: Literal[EXTENSION_POLICY_REVISION]
    artifact_policy_revision: Literal[
        EXTENSION_ARTIFACT_POLICY_REVISION
    ]
    generation_policy_revision: Literal[
        EXTENSION_GENERATION_POLICY_REVISION
    ]
    generation_policy_digest: Literal[
        EXTENSION_GENERATION_POLICY_DIGEST
    ]
    generator_revision: Literal[EXTENSION_GENERATOR_REVISION]
    prompt_digest: Literal[EXTENSION_GENERATION_PROMPT_DIGEST]
    provider_id: str = Field(min_length=1, max_length=120)
    model_id: str = Field(min_length=1, max_length=240)
    model_config_digest: str = Field(min_length=64, max_length=64)

    @field_validator("generation_id")
    @classmethod
    def validate_generation_id(cls, value: str) -> str:
        if not _GENERATION_ID.fullmatch(value):
            raise ValueError("generation_id is invalid")
        return value

    @field_validator("candidate_id")
    @classmethod
    def validate_candidate_id(cls, value: str) -> str:
        if not _CANDIDATE_ID.fullmatch(value):
            raise ValueError("candidate_id is invalid")
        return value

    @field_validator("extension_id")
    @classmethod
    def validate_extension_id(cls, value: str) -> str:
        if not _EXTENSION_ID.fullmatch(value):
            raise ValueError("extension_id is invalid")
        return value

    @field_validator("provider_id", "model_id")
    @classmethod
    def validate_model_identity(cls, value: str) -> str:
        if not _TOKEN.fullmatch(value):
            raise ValueError("provider/model identity is invalid")
        return value

    @field_validator(
        "owner_scope_digest",
        "authenticated_principal_digest",
        "initiating_session_digest",
        "request_provenance_digest",
        "spec_digest",
        "generation_policy_digest",
        "prompt_digest",
        "model_config_digest",
    )
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if not _DIGEST.fullmatch(value):
            raise ValueError("generation binding digest is invalid")
        return value

    @field_validator("candidate_expires_at")
    @classmethod
    def validate_expiry(cls, value: str) -> str:
        _parse_utc(value)
        return value

    def canonical_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def canonical_bytes(self) -> bytes:
        return _canonical_bytes(self.canonical_dict())

    def binding_digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


class ExtensionGenerationReport(BaseModel):
    """Canonical source-free terminal report for one generation attempt."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal[
        EXTENSION_GENERATION_REPORT_SCHEMA_VERSION
    ]
    binding: ExtensionGenerationBinding
    binding_digest: str = Field(min_length=64, max_length=64)
    generation_status: Literal["quarantined", "rejected"]
    artifact_id: str | None = Field(default=None, max_length=31)
    artifact_sha256: str | None = Field(default=None, max_length=64)
    artifact_size_bytes: StrictInt | None = Field(
        default=None,
        ge=1,
        le=MAX_EXTENSION_ARTIFACT_BYTES,
    )
    artifact_envelope_digest: str | None = Field(
        default=None,
        max_length=64,
    )
    failure_code: Literal[
        "model_unavailable",
        "model_identity_drift",
        "model_output_invalid",
        "artifact_admission_failed",
        "prerequisite_changed",
    ] | None = None
    generated_at: str = Field(min_length=20, max_length=32)
    authority: ExtensionGenerationAuthority = Field(
        default_factory=ExtensionGenerationAuthority,
    )

    @field_validator("binding_digest")
    @classmethod
    def validate_binding_digest(cls, value: str) -> str:
        if not _DIGEST.fullmatch(value):
            raise ValueError("generation report digest is invalid")
        return value

    @field_validator(
        "artifact_sha256",
        "artifact_envelope_digest",
    )
    @classmethod
    def validate_optional_digest(cls, value: str | None) -> str | None:
        if value is not None and not _DIGEST.fullmatch(value):
            raise ValueError("artifact digest is invalid")
        return value

    @field_validator("artifact_id")
    @classmethod
    def validate_artifact_id(cls, value: str | None) -> str | None:
        if value is not None and not _ARTIFACT_ID.fullmatch(value):
            raise ValueError("artifact_id is invalid")
        return value

    @field_validator("generated_at")
    @classmethod
    def validate_generated_at(cls, value: str) -> str:
        _parse_utc(value)
        return value

    @model_validator(mode="after")
    def validate_terminal_shape(self) -> "ExtensionGenerationReport":
        if self.binding.binding_digest() != self.binding_digest:
            raise ValueError("generation report binding digest mismatch")
        artifact_fields = (
            self.artifact_id,
            self.artifact_sha256,
            self.artifact_size_bytes,
            self.artifact_envelope_digest,
        )
        if self.generation_status == "quarantined":
            if any(value is None for value in artifact_fields):
                raise ValueError("quarantined report requires artifact identity")
            if self.failure_code is not None:
                raise ValueError("quarantined report cannot include failure")
        else:
            if any(value is not None for value in artifact_fields):
                raise ValueError("rejected report cannot include artifact identity")
            if self.failure_code is None:
                raise ValueError("rejected report requires failure code")
        self.canonical_bytes()
        return self

    def canonical_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def canonical_bytes(self) -> bytes:
        payload = _canonical_bytes(self.canonical_dict())
        if len(payload) > MAX_EXTENSION_GENERATION_REPORT_BYTES:
            raise ValueError("generation report exceeds byte budget")
        return payload

    def report_digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


def authenticated_local_principal_digest(control_token: str) -> str:
    if not isinstance(control_token, str) or len(control_token) < 16:
        raise ValueError("control token is invalid")
    return hashlib.sha256(
        b"veyra.phase6.local_control_principal.v1\x00"
        + control_token.encode("utf-8")
    ).hexdigest()


def initiating_session_digest(session_id: str) -> str:
    if not isinstance(session_id, str):
        raise TypeError("session_id must be a string")
    selected = session_id.strip()
    if not selected or len(selected.encode("utf-8")) > 240:
        raise ValueError("session_id is invalid")
    return hashlib.sha256(
        b"veyra.phase6.initiating_session.v1\x00"
        + selected.encode("utf-8")
    ).hexdigest()


def parse_extension_generation_binding(
    value: Any,
) -> ExtensionGenerationBinding:
    if isinstance(value, ExtensionGenerationBinding):
        value = value.model_dump(mode="python")
    parsed = ExtensionGenerationBinding.model_validate(value, strict=True)
    parsed.canonical_bytes()
    return parsed


def parse_extension_generation_report(
    value: Any,
) -> ExtensionGenerationReport:
    if isinstance(value, ExtensionGenerationReport):
        value = value.model_dump(mode="python")
    parsed = ExtensionGenerationReport.model_validate(value, strict=True)
    parsed.canonical_bytes()
    return parsed


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _parse_utc(value: str) -> datetime:
    if not isinstance(value, str):
        raise TypeError("timestamp must be a string")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc)


__all__ = [
    "EXTENSION_GENERATION_BINDING_SCHEMA_VERSION",
    "EXTENSION_GENERATION_POLICY_DIGEST",
    "EXTENSION_GENERATION_POLICY_REVISION",
    "EXTENSION_GENERATION_PROMPT",
    "EXTENSION_GENERATION_PROMPT_DIGEST",
    "EXTENSION_GENERATION_REPORT_SCHEMA_VERSION",
    "EXTENSION_GENERATOR_REVISION",
    "ExtensionGenerationAuthority",
    "ExtensionGenerationBinding",
    "ExtensionGenerationReport",
    "authenticated_local_principal_digest",
    "initiating_session_digest",
    "parse_extension_generation_binding",
    "parse_extension_generation_report",
]
