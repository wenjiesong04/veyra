from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)


EXTENSION_SPEC_SCHEMA_VERSION = "veyra.extension_spec.v1"
EXTENSION_POLICY_REVISION = (
    "veyra.phase6.extension_spec_policy.v1"
)
JSON_SCHEMA_URI = (
    "https://json-schema.org/draft/2020-12/schema"
)
TCB_FORBIDDEN_PATHS = (
    ".env",
    ".git",
    "core/",
    "execution/",
    "guardian/",
    "interface/",
    "rollback_audit/",
    "routers/",
    "runtime/",
    "state/",
    "tool_proxy/",
)
REQUIRED_FUTURE_CHECKS = (
    "static",
    "unit",
    "contract",
    "security",
    "fuzz",
)
MAX_CANONICAL_SPEC_BYTES = 64 * 1024

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}$")
_FIELD_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_VERSION_PIN = re.compile(
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)(?:-[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)*)?"
    r"(?:\+[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)*)?$"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ExtensionPrimitiveSchema(BaseModel):
    """A deliberately small, non-recursive JSON Schema subset."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    type: Literal["string", "integer", "number", "boolean", "null"]
    minLength: StrictInt | None = Field(default=None, ge=0, le=16_384)
    maxLength: StrictInt | None = Field(default=None, ge=0, le=16_384)
    minimum: StrictInt | None = None
    maximum: StrictInt | None = None

    @model_validator(mode="after")
    def validate_constraints(self) -> "ExtensionPrimitiveSchema":
        if self.type == "string":
            if self.minimum is not None or self.maximum is not None:
                raise ValueError(
                    "string schemas cannot declare numeric bounds"
                )
            if self.maxLength is None:
                raise ValueError(
                    "string schemas must declare a bounded maxLength"
                )
            if (
                self.minLength is not None
                and self.maxLength is not None
                and self.minLength > self.maxLength
            ):
                raise ValueError(
                    "string minLength cannot exceed maxLength"
                )
            return self
        if self.minLength is not None or self.maxLength is not None:
            raise ValueError(
                "non-string schemas cannot declare length bounds"
            )
        if self.type not in {"integer", "number"} and (
            self.minimum is not None or self.maximum is not None
        ):
            raise ValueError(
                "non-numeric schemas cannot declare numeric bounds"
            )
        if self.type in {"integer", "number"} and (
            self.minimum is None or self.maximum is None
        ):
            raise ValueError(
                "numeric schemas must declare bounded minimum and maximum"
            )
        if (
            self.minimum is not None
            and self.maximum is not None
            and self.minimum > self.maximum
        ):
            raise ValueError(
                "numeric minimum cannot exceed maximum"
            )
        return self


class ExtensionObjectSchema(BaseModel):
    """The only JSON Schema shape accepted by the Phase 6.2a gate."""

    model_config = ConfigDict(
        strict=True,
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )

    schema_uri: Literal[JSON_SCHEMA_URI] = Field(alias="$schema")
    type: Literal["object"]
    properties: dict[str, ExtensionPrimitiveSchema] = Field(
        min_length=1,
        max_length=32,
    )
    required: list[str] = Field(default_factory=list, max_length=32)
    additionalProperties: Literal[False]

    @field_validator("properties")
    @classmethod
    def validate_property_names(
        cls,
        value: dict[str, ExtensionPrimitiveSchema],
    ) -> dict[str, ExtensionPrimitiveSchema]:
        casefolded: set[str] = set()
        for name in value:
            if not _FIELD_NAME.fullmatch(name):
                raise ValueError(
                    "JSON Schema property names must be bounded identifiers"
                )
            folded = name.casefold()
            if folded in casefolded:
                raise ValueError(
                    "JSON Schema property names cannot collide by case"
                )
            casefolded.add(folded)
        return value

    @model_validator(mode="after")
    def validate_required(self) -> "ExtensionObjectSchema":
        if len(set(self.required)) != len(self.required):
            raise ValueError("required properties must be unique")
        if any(name not in self.properties for name in self.required):
            raise ValueError(
                "required properties must exist in properties"
            )
        return self


class ExtensionPermissionDeclaration(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    files: list[str] = Field(default_factory=list, max_length=0)
    network_hosts: list[str] = Field(default_factory=list, max_length=0)
    secret_ids: list[str] = Field(default_factory=list, max_length=0)
    external_account_ids: list[str] = Field(
        default_factory=list,
        max_length=0,
    )
    max_cost_usd_cents: Literal[0]


class ExtensionSideEffect(BaseModel):
    """Future contract placeholder; Phase 6.2a rejects every instance."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    effect_type: str = Field(min_length=1, max_length=80)
    target: str = Field(min_length=1, max_length=240)


class ExtensionBudget(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    timeout_ms: StrictInt = Field(ge=1, le=1_000)
    cpu_ms: StrictInt = Field(ge=1, le=1_000)
    memory_bytes: StrictInt = Field(
        ge=1_024,
        le=16 * 1024 * 1024,
    )
    output_bytes: StrictInt = Field(ge=1, le=64 * 1024)
    max_retries: Literal[0]


class ExtensionIdempotency(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    mode: Literal["pure"]
    key: Literal["canonical_input_sha256"]


class ExtensionVerificationPlan(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    strategy: Literal["future_isolated_contract_tests"]
    required_checks: list[
        Literal["static", "unit", "contract", "security", "fuzz"]
    ] = Field(min_length=5, max_length=5)
    independent_verifier_required: Literal[True]

    @model_validator(mode="after")
    def validate_checks(self) -> "ExtensionVerificationPlan":
        if tuple(self.required_checks) != REQUIRED_FUTURE_CHECKS:
            raise ValueError(
                "required_checks must match the fixed Phase 6.2a gate"
            )
        return self


class ExtensionCompensationPlan(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    strategy: Literal["not_applicable_no_side_effects"]


class ExtensionDependencyPin(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    dependency_id: str = Field(min_length=1, max_length=120)
    version: str = Field(min_length=1, max_length=80)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_pin(self) -> "ExtensionDependencyPin":
        if not _IDENTIFIER.fullmatch(self.dependency_id):
            raise ValueError(
                "dependency_id must be a bounded identifier"
            )
        if not _VERSION_PIN.fullmatch(self.version):
            raise ValueError(
                "dependency version must be an exact immutable pin"
            )
        if not _SHA256.fullmatch(self.sha256):
            raise ValueError("dependency sha256 must be exact")
        return self


class ExtensionArtifactDeclaration(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    status: Literal["not_generated"]
    artifact_kind: Literal["none"]
    code_sha256: None


class ExtensionTcbPolicy(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    policy_revision: Literal[EXTENSION_POLICY_REVISION]
    forbidden_paths: list[str] = Field(
        min_length=len(TCB_FORBIDDEN_PATHS),
        max_length=len(TCB_FORBIDDEN_PATHS),
    )
    workspace_access_allowed: Literal[False]
    state_access_allowed: Literal[False]
    environment_access_allowed: Literal[False]

    @model_validator(mode="after")
    def validate_policy(self) -> "ExtensionTcbPolicy":
        if tuple(self.forbidden_paths) != TCB_FORBIDDEN_PATHS:
            raise ValueError(
                "TCB forbidden paths must match the fixed Veyra policy"
            )
        return self


class ExtensionSpec(BaseModel):
    """Strict, non-executable manifest admitted by Phase 6.2a."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal[EXTENSION_SPEC_SCHEMA_VERSION]
    extension_kind: Literal["pure_function"]
    extension_id: str = Field(min_length=1, max_length=120)
    version: StrictInt = Field(ge=1, le=2_147_483_647)
    purpose: str = Field(min_length=1, max_length=512)
    expires_at: str = Field(min_length=20, max_length=40)
    input_schema: ExtensionObjectSchema
    output_schema: ExtensionObjectSchema
    permissions: ExtensionPermissionDeclaration
    side_effects: list[ExtensionSideEffect] = Field(
        default_factory=list,
        max_length=0,
    )
    risk_floor: Literal["R0"]
    budgets: ExtensionBudget
    idempotency: ExtensionIdempotency
    verification: ExtensionVerificationPlan
    compensation: ExtensionCompensationPlan
    dependencies: list[ExtensionDependencyPin] = Field(
        default_factory=list,
        max_length=16,
    )
    artifact: ExtensionArtifactDeclaration
    tcb_policy: ExtensionTcbPolicy

    @model_validator(mode="after")
    def validate_spec(self) -> "ExtensionSpec":
        if not _IDENTIFIER.fullmatch(self.extension_id):
            raise ValueError(
                "extension_id must be a bounded identifier"
            )
        if unicodedata.normalize("NFC", self.purpose) != self.purpose:
            raise ValueError("purpose must use NFC Unicode")
        dependency_ids = [
            item.dependency_id.casefold() for item in self.dependencies
        ]
        if len(set(dependency_ids)) != len(dependency_ids):
            raise ValueError("dependency pins must be unique")
        return self

    def canonical_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.canonical_dict())

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(payload) > MAX_CANONICAL_SPEC_BYTES:
        raise ValueError(
            "canonical ExtensionSpec exceeds the byte budget"
        )
    return payload


def parse_extension_spec(value: Any) -> ExtensionSpec:
    if isinstance(value, ExtensionSpec):
        # Pydantic's frozen models are shallow: nested lists and dictionaries
        # can still be mutated by an in-process caller.  Never trust an
        # existing model instance as already validated.
        value = value.model_dump(mode="python", by_alias=True)
    spec = ExtensionSpec.model_validate(value, strict=True)
    spec.canonical_bytes()
    return spec


__all__ = [
    "EXTENSION_POLICY_REVISION",
    "EXTENSION_SPEC_SCHEMA_VERSION",
    "ExtensionArtifactDeclaration",
    "ExtensionBudget",
    "ExtensionCompensationPlan",
    "ExtensionDependencyPin",
    "ExtensionIdempotency",
    "ExtensionObjectSchema",
    "ExtensionPermissionDeclaration",
    "ExtensionPrimitiveSchema",
    "ExtensionSideEffect",
    "ExtensionSpec",
    "ExtensionTcbPolicy",
    "ExtensionVerificationPlan",
    "JSON_SCHEMA_URI",
    "MAX_CANONICAL_SPEC_BYTES",
    "REQUIRED_FUTURE_CHECKS",
    "TCB_FORBIDDEN_PATHS",
    "canonical_json_bytes",
    "parse_extension_spec",
]
