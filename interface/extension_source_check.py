from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
import sys
from typing import Any, Literal, TypeAlias

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


EXTENSION_SOURCE_CHECK_BINDING_SCHEMA_VERSION = (
    "veyra.phase6.extension_source_check_binding.v1"
)
EXTENSION_SOURCE_CHECK_SCHEMA_VERSION = (
    "veyra.phase6.extension_source_check_report.v1"
)
EXTENSION_SOURCE_CHECK_POLICY_REVISION = (
    "veyra.phase6.extension_source_check_policy.v1"
)
EXTENSION_SOURCE_CHECKER_REVISION = (
    "veyra.phase6.nonexecuting_ast_checker.v1"
)
EXTENSION_SOURCE_ENTRYPOINT = "run_extension"
MAX_EXTENSION_SOURCE_CHECK_REPORT_BYTES = 16 * 1024
MAX_EXTENSION_SOURCE_AST_NODES = 128
MAX_EXTENSION_SOURCE_AST_DEPTH = 16
MAX_EXTENSION_SOURCE_LITERAL_BYTES = 16 * 1024
EXTENSION_SOURCE_PARSER_IDENTITY = (
    f"{sys.implementation.name}-{sys.version_info.major}."
    f"{sys.version_info.minor}-ast-feature-3.11"
)
EXTENSION_SOURCE_RULESET_DIGEST = hashlib.sha256(
    json.dumps(
        {
            "entrypoint": EXTENSION_SOURCE_ENTRYPOINT,
            "function_body": "single_return_dict",
            "return_values": (
                "json_primitive_constant_or_required_input_projection"
            ),
            "calls": False,
            "attributes": False,
            "imports": False,
            "dependencies": False,
            "max_ast_nodes": MAX_EXTENSION_SOURCE_AST_NODES,
            "max_ast_depth": MAX_EXTENSION_SOURCE_AST_DEPTH,
            "max_literal_bytes": MAX_EXTENSION_SOURCE_LITERAL_BYTES,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
).hexdigest()

SOURCE_CHECK_ISSUE_CODES = (
    "spec_invalid",
    "spec_binding_invalid",
    "dependencies_not_empty",
    "source_size_invalid",
    "source_identity_mismatch",
    "source_encoding_invalid",
    "source_canonicalization_invalid",
    "source_syntax_invalid",
    "source_ast_budget_exceeded",
    "top_level_shape_invalid",
    "entrypoint_signature_invalid",
    "function_body_invalid",
    "return_mapping_invalid",
    "output_field_invalid",
    "input_projection_invalid",
    "schema_type_incompatible",
    "schema_bounds_incompatible",
)

SourceCheckIssueCode: TypeAlias = Literal[
    "spec_invalid",
    "spec_binding_invalid",
    "dependencies_not_empty",
    "source_size_invalid",
    "source_identity_mismatch",
    "source_encoding_invalid",
    "source_canonicalization_invalid",
    "source_syntax_invalid",
    "source_ast_budget_exceeded",
    "top_level_shape_invalid",
    "entrypoint_signature_invalid",
    "function_body_invalid",
    "return_mapping_invalid",
    "output_field_invalid",
    "input_projection_invalid",
    "schema_type_incompatible",
    "schema_bounds_incompatible",
]

_CANDIDATE_ID = re.compile(r"^extspec_[0-9a-f]{24}$")
_ARTIFACT_ID = re.compile(r"^extart_[0-9a-f]{24}$")
_EXTENSION_ID = re.compile(r"^[a-z][a-z0-9_.-]{0,119}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_ISSUE_ORDER = {
    code: index for index, code in enumerate(SOURCE_CHECK_ISSUE_CODES)
}


class ExtensionSourceCheckBinding(BaseModel):
    """Exact private identity supplied to the non-executing checker."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal[
        EXTENSION_SOURCE_CHECK_BINDING_SCHEMA_VERSION
    ]
    candidate_id: str = Field(min_length=32, max_length=32)
    candidate_revision: StrictInt = Field(ge=1, le=2_147_483_647)
    artifact_id: str = Field(min_length=31, max_length=31)
    artifact_revision: StrictInt = Field(ge=1, le=2_147_483_647)
    owner_scope_digest: str = Field(min_length=64, max_length=64)
    extension_id: str = Field(min_length=1, max_length=120)
    extension_version: StrictInt = Field(ge=1, le=2_147_483_647)
    spec_digest: str = Field(min_length=64, max_length=64)
    artifact_sha256: str = Field(min_length=64, max_length=64)
    source_size_bytes: StrictInt = Field(
        ge=1,
        le=MAX_EXTENSION_ARTIFACT_BYTES,
    )
    extension_policy_revision: Literal[EXTENSION_POLICY_REVISION]
    artifact_policy_revision: Literal[
        EXTENSION_ARTIFACT_POLICY_REVISION
    ]
    source_check_policy_revision: Literal[
        EXTENSION_SOURCE_CHECK_POLICY_REVISION
    ]
    parser_identity: Literal[EXTENSION_SOURCE_PARSER_IDENTITY]
    ruleset_digest: Literal[EXTENSION_SOURCE_RULESET_DIGEST]

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
        "owner_scope_digest",
        "spec_digest",
        "artifact_sha256",
    )
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if not _DIGEST.fullmatch(value):
            raise ValueError("source-check binding digest is invalid")
        return value

    def canonical_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.canonical_dict())

    def binding_digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


class ExtensionSourceCheckAuthority(BaseModel):
    """The static report grants no candidate or runtime authority."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    code_generation: Literal[False] = False
    artifact_write: Literal[False] = False
    artifact_write_outside_quarantine: Literal[False] = False
    workspace_access: Literal[False] = False
    state_access: Literal[False] = False
    environment_access: Literal[False] = False
    network_access: Literal[False] = False
    secret_access: Literal[False] = False
    tool_access: Literal[False] = False
    agent_dispatch: Literal[False] = False
    signing: Literal[False] = False
    installation: Literal[False] = False
    activation: Literal[False] = False
    capability_registration: Literal[False] = False
    execution: Literal[False] = False
    behavior_verification: Literal[False] = False
    promotion: Literal[False] = False
    provider_switch: Literal[False] = False


class ExtensionSourceCheckReport(BaseModel):
    """Canonical, source-free output from the fixed AST policy checker."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal[EXTENSION_SOURCE_CHECK_SCHEMA_VERSION]
    checker_revision: Literal[EXTENSION_SOURCE_CHECKER_REVISION]
    candidate_id: str = Field(min_length=32, max_length=32)
    candidate_revision: StrictInt = Field(ge=1, le=2_147_483_647)
    artifact_id: str = Field(min_length=31, max_length=31)
    artifact_revision: StrictInt = Field(ge=1, le=2_147_483_647)
    owner_scope_digest: str = Field(min_length=64, max_length=64)
    extension_id: str = Field(min_length=1, max_length=120)
    extension_version: StrictInt = Field(ge=1, le=2_147_483_647)
    spec_digest: str = Field(min_length=64, max_length=64)
    artifact_sha256: str = Field(min_length=64, max_length=64)
    source_size_bytes: StrictInt = Field(
        ge=1,
        le=MAX_EXTENSION_ARTIFACT_BYTES,
    )
    extension_policy_revision: Literal[EXTENSION_POLICY_REVISION]
    artifact_policy_revision: Literal[
        EXTENSION_ARTIFACT_POLICY_REVISION
    ]
    source_check_policy_revision: Literal[
        EXTENSION_SOURCE_CHECK_POLICY_REVISION
    ]
    parser_identity: Literal[EXTENSION_SOURCE_PARSER_IDENTITY]
    ruleset_digest: Literal[EXTENSION_SOURCE_RULESET_DIGEST]
    check_status: Literal["passed", "failed"]
    source_syntax_status: Literal["passed", "failed", "not_checked"]
    static_checks_status: Literal["passed", "failed"]
    static_security_policy_status: Literal["passed", "failed"]
    unit_checks_status: Literal["not_started"] = "not_started"
    contract_checks_status: Literal["not_started"] = "not_started"
    security_runtime_checks_status: Literal["not_started"] = "not_started"
    fuzz_checks_status: Literal["not_started"] = "not_started"
    behavior_verification_status: Literal["not_started"] = "not_started"
    execution_status: Literal["not_started"] = "not_started"
    isolated_generation_status: Literal["not_started"] = "not_started"
    test_execution_status: Literal["not_started"] = "not_started"
    signature_status: Literal["not_implemented"] = "not_implemented"
    activation_status: Literal["not_installed"] = "not_installed"
    capability_registry_visible: Literal[False] = False
    promotion_authorized: Literal[False] = False
    policy_effect: Literal["none"] = "none"
    issue_codes: list[SourceCheckIssueCode] = Field(
        default_factory=list,
        max_length=len(SOURCE_CHECK_ISSUE_CODES),
    )
    checked_at: str = Field(min_length=20, max_length=32)
    authority: ExtensionSourceCheckAuthority = Field(
        default_factory=ExtensionSourceCheckAuthority,
    )

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
        "owner_scope_digest",
        "spec_digest",
        "artifact_sha256",
    )
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if not _DIGEST.fullmatch(value):
            raise ValueError("source-check report digest is invalid")
        return value

    @field_validator("checked_at")
    @classmethod
    def validate_checked_at(cls, value: str) -> str:
        parsed = _parse_canonical_utc(value)
        if _canonical_utc(parsed) != value:
            raise ValueError("checked_at must be canonical UTC")
        return value

    @field_validator("issue_codes")
    @classmethod
    def validate_issue_codes(
        cls,
        value: list[SourceCheckIssueCode],
    ) -> list[SourceCheckIssueCode]:
        if len(set(value)) != len(value):
            raise ValueError("source-check issue codes must be unique")
        expected = sorted(value, key=lambda item: _ISSUE_ORDER[item])
        if value != expected:
            raise ValueError(
                "source-check issue codes must use canonical policy order"
            )
        return value

    @model_validator(mode="after")
    def validate_status_consistency(self) -> "ExtensionSourceCheckReport":
        passed = self.check_status == "passed"
        if passed != (
            not self.issue_codes
            and self.source_syntax_status == "passed"
            and self.static_checks_status == "passed"
            and self.static_security_policy_status == "passed"
        ):
            raise ValueError("source-check status is inconsistent")
        if not passed and (
            not self.issue_codes
            or self.static_checks_status != "failed"
            or self.static_security_policy_status != "failed"
        ):
            raise ValueError("failed source-check report is inconsistent")
        return self

    def canonical_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.canonical_dict())

    def report_digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


def parse_extension_source_check_binding(
    value: Any,
) -> ExtensionSourceCheckBinding:
    if isinstance(value, ExtensionSourceCheckBinding):
        value = value.model_dump(mode="python", by_alias=True)
    binding = ExtensionSourceCheckBinding.model_validate(
        value,
        strict=True,
    )
    binding.canonical_bytes()
    return binding


def parse_extension_source_check_report(
    value: Any,
) -> ExtensionSourceCheckReport:
    if isinstance(value, ExtensionSourceCheckReport):
        value = value.model_dump(mode="python", by_alias=True)
    report = ExtensionSourceCheckReport.model_validate(
        value,
        strict=True,
    )
    report.canonical_bytes()
    return report


def _canonical_json_bytes(value: Any) -> bytes:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(payload) > MAX_EXTENSION_SOURCE_CHECK_REPORT_BYTES:
        raise ValueError("canonical source-check document exceeds budget")
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
    "EXTENSION_SOURCE_CHECK_BINDING_SCHEMA_VERSION",
    "EXTENSION_SOURCE_CHECK_POLICY_REVISION",
    "EXTENSION_SOURCE_CHECK_SCHEMA_VERSION",
    "EXTENSION_SOURCE_CHECKER_REVISION",
    "EXTENSION_SOURCE_ENTRYPOINT",
    "EXTENSION_SOURCE_PARSER_IDENTITY",
    "EXTENSION_SOURCE_RULESET_DIGEST",
    "MAX_EXTENSION_SOURCE_CHECK_REPORT_BYTES",
    "MAX_EXTENSION_SOURCE_AST_DEPTH",
    "MAX_EXTENSION_SOURCE_AST_NODES",
    "MAX_EXTENSION_SOURCE_LITERAL_BYTES",
    "SOURCE_CHECK_ISSUE_CODES",
    "ExtensionSourceCheckAuthority",
    "ExtensionSourceCheckBinding",
    "ExtensionSourceCheckReport",
    "SourceCheckIssueCode",
    "parse_extension_source_check_binding",
    "parse_extension_source_check_report",
]
