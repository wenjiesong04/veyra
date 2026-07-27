from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Mapping, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)


BINDING_SCHEMA_VERSION = "veyra.governed_session_binding.v1"
INVOCATION_SCHEMA_VERSION = "veyra.tool_invocation.v1"
GRANT_SCHEMA_VERSION = "veyra.capability_grant.v1"
PREFLIGHT_SCHEMA_VERSION = "veyra.tool_preflight_decision.v1"
OBSERVATION_SCHEMA_VERSION = "veyra.tool_observation.v1"
EFFECT_EVIDENCE_SCHEMA_VERSION = "veyra.verified_tool_effect.v1"
RECEIPT_SCHEMA_VERSION = "veyra.authoritative_tool_receipt.v1"

_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MAX_JSON_BYTES = 256 * 1024

LedgerState = Literal[
    "blocked",
    "authorized_not_observed",
    "observed_success",
    "observed_failure",
    "revoked",
    "expired",
]
PreflightOutcome = Literal["would_allow", "block", "require_approval"]
ObservationOutcome = Literal["success", "failure"]
RiskLevelValue = Literal["R0", "R1", "R2", "R3", "R4", "R5"]


def _json_ready(value: Any) -> Any:
    """Return the deterministic JSON representation used by all digests."""

    if isinstance(value, BaseModel):
        return _json_ready(value.model_dump(mode="python"))
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("canonical JSON cannot encode a naive datetime")
        normalized = value.astimezone(timezone.utc)
        return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")
    if isinstance(value, Enum):
        return _json_ready(value.value)
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("canonical JSON object keys must be strings")
            normalized[key] = _json_ready(item)
        return normalized
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("canonical JSON cannot encode NaN or infinity")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"value of type {type(value).__name__} is not JSON serializable")


def canonical_json(value: Any) -> str:
    """Serialize a JSON-compatible value with stable keys and no whitespace."""

    return json.dumps(
        _json_ready(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_sha256(value: Any) -> str:
    """Return a lowercase SHA-256 digest over :func:`canonical_json`."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def secret_sha256(secret: str) -> str:
    """Digest an in-memory capability or reservation token for persistence."""

    if not isinstance(secret, str) or not secret or secret != secret.strip():
        raise ValueError("secret token must be a non-empty normalized string")
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _without(payload: Mapping[str, Any], *fields: str) -> dict[str, Any]:
    excluded = set(fields)
    return {key: value for key, value in payload.items() if key not in excluded}


def _validate_digest(value: str, *, field_name: str) -> str:
    if not _DIGEST_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def _validate_identifier(value: str, *, field_name: str) -> str:
    if value != value.strip():
        raise ValueError(f"{field_name} cannot have leading or trailing whitespace")
    if "\x00" in value:
        raise ValueError(f"{field_name} cannot contain NUL")
    return value


def _validate_json_size(value: Any, *, field_name: str) -> Any:
    if len(canonical_json(value).encode("utf-8")) > _MAX_JSON_BYTES:
        raise ValueError(f"{field_name} exceeds {_MAX_JSON_BYTES} bytes")
    return value


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        strict=True,
        extra="forbid",
        frozen=True,
        validate_assignment=True,
    )


class GovernedSessionBinding(_StrictFrozenModel):
    """Immutable identity boundary for a governed Agent execution."""

    schema_version: Literal["veyra.governed_session_binding.v1"] = (
        BINDING_SCHEMA_VERSION
    )
    user_id: str = Field(min_length=1, max_length=240)
    workspace_id: str = Field(min_length=1, max_length=600)
    agent_id: str = Field(min_length=1, max_length=240)
    session_id: str = Field(min_length=1, max_length=240)
    channel_id: str = Field(min_length=1, max_length=240)
    case_id: str = Field(min_length=1, max_length=240)
    step_id: str = Field(min_length=1, max_length=240)
    run_id: str = Field(min_length=1, max_length=240)
    binding_digest: str

    @field_validator(
        "user_id",
        "workspace_id",
        "agent_id",
        "session_id",
        "channel_id",
        "case_id",
        "step_id",
        "run_id",
    )
    @classmethod
    def validate_identifiers(cls, value: str, info: Any) -> str:
        return _validate_identifier(value, field_name=info.field_name)

    @field_validator("binding_digest")
    @classmethod
    def validate_digest_shape(cls, value: str) -> str:
        return _validate_digest(value, field_name="binding_digest")

    @model_validator(mode="after")
    def validate_integrity(self) -> Self:
        expected = canonical_sha256(
            _without(self.model_dump(mode="python"), "binding_digest")
        )
        if self.binding_digest != expected:
            raise ValueError("binding_digest does not match the governed session")
        return self

    @classmethod
    def create(
        cls,
        *,
        user_id: str,
        workspace_id: str,
        agent_id: str,
        session_id: str,
        channel_id: str,
        case_id: str,
        step_id: str,
        run_id: str,
    ) -> Self:
        payload = {
            "schema_version": BINDING_SCHEMA_VERSION,
            "user_id": user_id,
            "workspace_id": workspace_id,
            "agent_id": agent_id,
            "session_id": session_id,
            "channel_id": channel_id,
            "case_id": case_id,
            "step_id": step_id,
            "run_id": run_id,
        }
        return cls.model_validate(
            {**payload, "binding_digest": canonical_sha256(payload)}, strict=True
        )


class ToolInvocation(_StrictFrozenModel):
    """The exact tool call presented to the authoritative preflight boundary."""

    schema_version: Literal["veyra.tool_invocation.v1"] = INVOCATION_SCHEMA_VERSION
    binding: GovernedSessionBinding
    tool_call_id: str = Field(min_length=1, max_length=240)
    tool_name: str = Field(min_length=1, max_length=240)
    tool_kind: str = Field(min_length=1, max_length=120)
    arguments: dict[str, JsonValue]
    derived_targets: list[str] = Field(default_factory=list, max_length=256)
    environment: dict[str, JsonValue]
    requested_at: AwareDatetime
    args_digest: str
    targets_digest: str
    environment_digest: str
    invocation_digest: str

    @field_validator("tool_call_id", "tool_name", "tool_kind")
    @classmethod
    def validate_identifiers(cls, value: str, info: Any) -> str:
        return _validate_identifier(value, field_name=info.field_name)

    @field_validator("derived_targets")
    @classmethod
    def validate_targets(cls, value: list[str]) -> list[str]:
        for target in value:
            if not target or target != target.strip() or "\x00" in target:
                raise ValueError(
                    "derived_targets must contain non-empty normalized strings"
                )
        if len(value) != len(set(value)):
            raise ValueError("derived_targets cannot contain duplicates")
        _validate_json_size(value, field_name="derived_targets")
        return value

    @field_validator("arguments", "environment")
    @classmethod
    def validate_json_maps(
        cls, value: dict[str, JsonValue], info: Any
    ) -> dict[str, JsonValue]:
        return _validate_json_size(value, field_name=info.field_name)

    @field_validator(
        "args_digest",
        "targets_digest",
        "environment_digest",
        "invocation_digest",
    )
    @classmethod
    def validate_digest_shape(cls, value: str, info: Any) -> str:
        return _validate_digest(value, field_name=info.field_name)

    @model_validator(mode="after")
    def validate_integrity(self) -> Self:
        if self.binding.run_id == self.tool_call_id:
            raise ValueError("tool_call_id must be distinct from run_id")
        if self.args_digest != canonical_sha256(self.arguments):
            raise ValueError("args_digest does not match arguments")
        if self.targets_digest != canonical_sha256(self.derived_targets):
            raise ValueError("targets_digest does not match derived_targets")
        if self.environment_digest != canonical_sha256(self.environment):
            raise ValueError("environment_digest does not match environment")
        expected = canonical_sha256(
            _without(self.model_dump(mode="python"), "invocation_digest")
        )
        if self.invocation_digest != expected:
            raise ValueError("invocation_digest does not match the invocation")
        return self

    @classmethod
    def create(
        cls,
        *,
        binding: GovernedSessionBinding,
        tool_call_id: str,
        tool_name: str,
        tool_kind: str,
        arguments: dict[str, JsonValue],
        derived_targets: list[str],
        environment: dict[str, JsonValue],
        requested_at: datetime,
    ) -> Self:
        payload = {
            "schema_version": INVOCATION_SCHEMA_VERSION,
            "binding": binding,
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "tool_kind": tool_kind,
            "arguments": arguments,
            "derived_targets": derived_targets,
            "environment": environment,
            "requested_at": requested_at,
            "args_digest": canonical_sha256(arguments),
            "targets_digest": canonical_sha256(derived_targets),
            "environment_digest": canonical_sha256(environment),
        }
        return cls.model_validate(
            {**payload, "invocation_digest": canonical_sha256(payload)}, strict=True
        )


class CapabilityGrant(_StrictFrozenModel):
    """A digest-only authorization bound to one exact invocation scope."""

    schema_version: Literal["veyra.capability_grant.v1"] = GRANT_SCHEMA_VERSION
    grant_id: str = Field(min_length=1, max_length=240)
    capability_token_digest: str
    binding: GovernedSessionBinding
    tool_call_id: str = Field(min_length=1, max_length=240)
    tool_name: str = Field(min_length=1, max_length=240)
    tool_kind: str = Field(min_length=1, max_length=120)
    risk_level: RiskLevelValue
    args_digest: str
    targets_digest: str
    environment_digest: str
    invocation_digest: str
    issued_at: AwareDatetime
    not_before: AwareDatetime
    expires_at: AwareDatetime
    max_uses: int = Field(ge=1, le=32)
    approval_id: str = Field(min_length=1, max_length=240)
    approval_revision: str = Field(min_length=1, max_length=240)
    policy_revision: str = Field(min_length=1, max_length=240)
    registry_revision: str = Field(min_length=1, max_length=240)
    grant_digest: str

    @field_validator(
        "grant_id",
        "tool_call_id",
        "tool_name",
        "tool_kind",
        "approval_id",
        "approval_revision",
        "policy_revision",
        "registry_revision",
    )
    @classmethod
    def validate_identifiers(cls, value: str, info: Any) -> str:
        return _validate_identifier(value, field_name=info.field_name)

    @field_validator(
        "capability_token_digest",
        "args_digest",
        "targets_digest",
        "environment_digest",
        "invocation_digest",
        "grant_digest",
    )
    @classmethod
    def validate_digest_shape(cls, value: str, info: Any) -> str:
        return _validate_digest(value, field_name=info.field_name)

    @model_validator(mode="after")
    def validate_integrity(self) -> Self:
        if self.not_before < self.issued_at:
            raise ValueError("not_before cannot precede issued_at")
        if self.expires_at <= self.not_before:
            raise ValueError("expires_at must be later than not_before")
        expected = canonical_sha256(
            _without(self.model_dump(mode="python"), "grant_digest")
        )
        if self.grant_digest != expected:
            raise ValueError("grant_digest does not match the capability grant")
        return self

    @classmethod
    def issue(
        cls,
        *,
        grant_id: str,
        capability_token: str,
        invocation: ToolInvocation,
        issued_at: datetime,
        not_before: datetime,
        expires_at: datetime,
        max_uses: int,
        risk_level: RiskLevelValue,
        approval_id: str,
        approval_revision: str,
        policy_revision: str,
        registry_revision: str,
    ) -> Self:
        payload = {
            "schema_version": GRANT_SCHEMA_VERSION,
            "grant_id": grant_id,
            "capability_token_digest": secret_sha256(capability_token),
            "binding": invocation.binding,
            "tool_call_id": invocation.tool_call_id,
            "tool_name": invocation.tool_name,
            "tool_kind": invocation.tool_kind,
            "risk_level": risk_level,
            "args_digest": invocation.args_digest,
            "targets_digest": invocation.targets_digest,
            "environment_digest": invocation.environment_digest,
            "invocation_digest": invocation.invocation_digest,
            "issued_at": issued_at,
            "not_before": not_before,
            "expires_at": expires_at,
            "max_uses": max_uses,
            "approval_id": approval_id,
            "approval_revision": approval_revision,
            "policy_revision": policy_revision,
            "registry_revision": registry_revision,
        }
        return cls.model_validate(
            {**payload, "grant_digest": canonical_sha256(payload)}, strict=True
        )

    def matches(
        self,
        binding: GovernedSessionBinding,
        invocation: ToolInvocation,
    ) -> bool:
        return (
            self.binding == binding
            and invocation.binding == binding
            and self.tool_call_id == invocation.tool_call_id
            and self.tool_name == invocation.tool_name
            and self.tool_kind == invocation.tool_kind
            and self.args_digest == invocation.args_digest
            and self.targets_digest == invocation.targets_digest
            and self.environment_digest == invocation.environment_digest
            and self.invocation_digest == invocation.invocation_digest
        )

    def is_time_valid(self, at: datetime) -> bool:
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("grant evaluation time must be timezone-aware")
        return self.not_before <= at < self.expires_at


class PreflightDecision(_StrictFrozenModel):
    """Persistable deny-only first-slice decision.

    A runtime may return an in-memory reservation token in a separate response
    envelope.  This contract stores only its digest, so normal model dumps
    cannot leak a bearer credential.
    """

    schema_version: Literal["veyra.tool_preflight_decision.v1"] = (
        PREFLIGHT_SCHEMA_VERSION
    )
    decision_id: str = Field(min_length=1, max_length=240)
    outcome: PreflightOutcome
    execute_allowed: Literal[False] = False
    reason: str = Field(min_length=1, max_length=1200)
    ledger_state: LedgerState
    grant_id: str | None = Field(default=None, min_length=1, max_length=240)
    reservation_id: str | None = Field(default=None, min_length=1, max_length=240)
    reservation_token_digest: str | None = None
    invocation_digest: str
    decided_at: AwareDatetime
    decision_digest: str

    @field_validator("decision_id")
    @classmethod
    def validate_decision_id(cls, value: str) -> str:
        return _validate_identifier(value, field_name="decision_id")

    @field_validator("grant_id", "reservation_id")
    @classmethod
    def validate_optional_identifiers(cls, value: str | None, info: Any) -> str | None:
        if value is None:
            return None
        return _validate_identifier(value, field_name=info.field_name)

    @field_validator(
        "reservation_token_digest",
        "invocation_digest",
        "decision_digest",
    )
    @classmethod
    def validate_digests(cls, value: str | None, info: Any) -> str | None:
        if value is None:
            return None
        return _validate_digest(value, field_name=info.field_name)

    @model_validator(mode="after")
    def validate_integrity(self) -> Self:
        reservation_values = (
            self.grant_id,
            self.reservation_id,
            self.reservation_token_digest,
        )
        if self.outcome == "would_allow":
            if self.ledger_state != "authorized_not_observed":
                raise ValueError(
                    "would_allow must enter authorized_not_observed ledger state"
                )
            if any(value is None for value in reservation_values):
                raise ValueError(
                    "would_allow requires grant and reservation digest bindings"
                )
        elif any(value is not None for value in reservation_values):
            raise ValueError("non-authorizing decisions cannot carry a reservation")
        if self.outcome == "block" and self.ledger_state not in {
            "blocked",
            "revoked",
            "expired",
        }:
            raise ValueError("block requires a non-authorizing ledger state")
        if self.outcome == "require_approval" and self.ledger_state != "blocked":
            raise ValueError("require_approval must remain blocked")
        expected = canonical_sha256(
            _without(self.model_dump(mode="python"), "decision_digest")
        )
        if self.decision_digest != expected:
            raise ValueError("decision_digest does not match the preflight decision")
        return self

    @classmethod
    def create(cls, **values: Any) -> Self:
        payload = {"schema_version": PREFLIGHT_SCHEMA_VERSION, **values}
        payload.setdefault("execute_allowed", False)
        return cls.model_validate(
            {**payload, "decision_digest": canonical_sha256(payload)}, strict=True
        )


class ToolObservation(_StrictFrozenModel):
    """Authoritative after-tool observation; bearer tokens stay out-of-band."""

    schema_version: Literal["veyra.tool_observation.v1"] = OBSERVATION_SCHEMA_VERSION
    grant_id: str = Field(min_length=1, max_length=240)
    reservation_id: str = Field(min_length=1, max_length=240)
    reservation_token_digest: str
    invocation_digest: str
    run_id: str = Field(min_length=1, max_length=240)
    tool_call_id: str = Field(min_length=1, max_length=240)
    outcome: ObservationOutcome
    result_digest: str
    observed_at: AwareDatetime
    duration_ms: int = Field(ge=0, le=86_400_000)
    observation_digest: str

    @field_validator("grant_id", "reservation_id", "run_id", "tool_call_id")
    @classmethod
    def validate_identifiers(cls, value: str, info: Any) -> str:
        return _validate_identifier(value, field_name=info.field_name)

    @field_validator(
        "reservation_token_digest",
        "invocation_digest",
        "result_digest",
        "observation_digest",
    )
    @classmethod
    def validate_digest_shape(cls, value: str, info: Any) -> str:
        return _validate_digest(value, field_name=info.field_name)

    @model_validator(mode="after")
    def validate_integrity(self) -> Self:
        expected = canonical_sha256(
            _without(self.model_dump(mode="python"), "observation_digest")
        )
        if self.observation_digest != expected:
            raise ValueError("observation_digest does not match the tool observation")
        return self

    @classmethod
    def create(cls, **values: Any) -> Self:
        payload = {"schema_version": OBSERVATION_SCHEMA_VERSION, **values}
        return cls.model_validate(
            {**payload, "observation_digest": canonical_sha256(payload)}, strict=True
        )


class VerifiedToolEffect(_StrictFrozenModel):
    """Veyra-owned projection of an independently verified tool effect.

    Caller/Agent prose is intentionally absent. The projection is bound to the
    exact invocation, observed result, target set, and canonical tool name so a
    harmless receipt cannot certify a different claimed outcome.
    """

    schema_version: Literal["veyra.verified_tool_effect.v1"] = (
        EFFECT_EVIDENCE_SCHEMA_VERSION
    )
    status: Literal["verified_success"] = "verified_success"
    source: str = Field(min_length=1, max_length=240)
    observed_at: AwareDatetime
    receipt_id: str = Field(min_length=1, max_length=240)
    run_id: str = Field(min_length=1, max_length=240)
    tool_call_id: str = Field(min_length=1, max_length=240)
    tool_name: str = Field(min_length=1, max_length=240)
    invocation_digest: str
    result_digest: str
    targets_digest: str
    authorized_targets: list[str] = Field(default_factory=list, max_length=256)
    summary: str = Field(min_length=1, max_length=2000)
    changed_files: list[str] = Field(default_factory=list, max_length=256)
    evidence_digest: str

    @field_validator(
        "source",
        "receipt_id",
        "run_id",
        "tool_call_id",
        "tool_name",
        "summary",
    )
    @classmethod
    def validate_text(cls, value: str, info: Any) -> str:
        return _validate_identifier(value, field_name=info.field_name)

    @field_validator("authorized_targets", "changed_files")
    @classmethod
    def validate_paths(cls, value: list[str], info: Any) -> list[str]:
        for path in value:
            if not path or path != path.strip() or "\x00" in path:
                raise ValueError(
                    f"{info.field_name} must contain non-empty normalized strings"
                )
        if len(value) != len(set(value)):
            raise ValueError(f"{info.field_name} cannot contain duplicates")
        _validate_json_size(value, field_name=info.field_name)
        return value

    @field_validator(
        "invocation_digest",
        "result_digest",
        "targets_digest",
        "evidence_digest",
    )
    @classmethod
    def validate_digest_shape(cls, value: str, info: Any) -> str:
        return _validate_digest(value, field_name=info.field_name)

    @model_validator(mode="after")
    def validate_integrity(self) -> Self:
        if self.targets_digest != canonical_sha256(self.authorized_targets):
            raise ValueError(
                "targets_digest does not match authorized_targets"
            )
        authorized = set(self.authorized_targets)
        if any(path not in authorized for path in self.changed_files):
            raise ValueError(
                "changed_files must be exact members of authorized_targets"
            )
        expected = canonical_sha256(
            _without(self.model_dump(mode="python"), "evidence_digest")
        )
        if self.evidence_digest != expected:
            raise ValueError(
                "evidence_digest does not match the verified tool effect"
            )
        return self

    @classmethod
    def create(cls, **values: Any) -> Self:
        payload = {"schema_version": EFFECT_EVIDENCE_SCHEMA_VERSION, **values}
        payload.setdefault("status", "verified_success")
        return cls.model_validate(
            {**payload, "evidence_digest": canonical_sha256(payload)},
            strict=True,
        )


class AuthoritativeToolReceipt(_StrictFrozenModel):
    """Verifier-facing receipt resolved from the private tool ledger."""

    schema_version: Literal["veyra.authoritative_tool_receipt.v1"] = (
        RECEIPT_SCHEMA_VERSION
    )
    receipt_id: str = Field(min_length=1, max_length=240)
    run_id: str = Field(min_length=1, max_length=240)
    tool_call_id: str = Field(min_length=1, max_length=240)
    grant_id: str = Field(min_length=1, max_length=240)
    reservation_id: str = Field(min_length=1, max_length=240)
    tool_name: str = Field(min_length=1, max_length=240)
    tool_kind: str = Field(min_length=1, max_length=120)
    risk_level: RiskLevelValue
    args_digest: str
    targets_digest: str
    environment_digest: str
    invocation_digest: str
    grant_digest: str
    ledger_state: LedgerState
    approval_verified: bool = False
    approval_id: str = Field(min_length=1, max_length=240)
    approval_revision: str = Field(min_length=1, max_length=240)
    policy_revision: str = Field(min_length=1, max_length=240)
    registry_revision: str = Field(min_length=1, max_length=240)
    reserved_at: AwareDatetime
    observed_at: AwareDatetime | None = None
    outcome: ObservationOutcome | None = None
    result_digest: str | None = None
    receipt_digest: str

    @field_validator(
        "receipt_id",
        "run_id",
        "tool_call_id",
        "grant_id",
        "reservation_id",
        "tool_name",
        "tool_kind",
        "approval_id",
        "approval_revision",
        "policy_revision",
        "registry_revision",
    )
    @classmethod
    def validate_identifiers(cls, value: str, info: Any) -> str:
        return _validate_identifier(value, field_name=info.field_name)

    @field_validator(
        "invocation_digest",
        "grant_digest",
        "args_digest",
        "targets_digest",
        "environment_digest",
        "result_digest",
        "receipt_digest",
    )
    @classmethod
    def validate_digests(cls, value: str | None, info: Any) -> str | None:
        if value is None:
            return None
        return _validate_digest(value, field_name=info.field_name)

    @model_validator(mode="after")
    def validate_integrity(self) -> Self:
        observed = self.ledger_state in {"observed_success", "observed_failure"}
        if observed:
            if self.observed_at is None or self.outcome is None or self.result_digest is None:
                raise ValueError(
                    "observed receipts require time, outcome, and result digest"
                )
            if self.observed_at < self.reserved_at:
                raise ValueError("observed_at cannot precede reserved_at")
            expected_state = (
                "observed_success"
                if self.outcome == "success"
                else "observed_failure"
            )
            if self.ledger_state != expected_state:
                raise ValueError("ledger_state does not match observation outcome")
        elif (
            self.observed_at is not None
            or self.outcome is not None
            or self.result_digest is not None
        ):
            raise ValueError("unobserved receipts cannot carry observation results")
        expected = canonical_sha256(
            _without(self.model_dump(mode="python"), "receipt_digest")
        )
        if self.receipt_digest != expected:
            raise ValueError("receipt_digest does not match the authoritative receipt")
        return self

    @classmethod
    def create(cls, **values: Any) -> Self:
        payload = {"schema_version": RECEIPT_SCHEMA_VERSION, **values}
        return cls.model_validate(
            {**payload, "receipt_digest": canonical_sha256(payload)}, strict=True
        )


__all__ = [
    "AuthoritativeToolReceipt",
    "CapabilityGrant",
    "GovernedSessionBinding",
    "LedgerState",
    "ObservationOutcome",
    "PreflightDecision",
    "PreflightOutcome",
    "RiskLevelValue",
    "ToolInvocation",
    "ToolObservation",
    "VerifiedToolEffect",
    "canonical_json",
    "canonical_sha256",
    "secret_sha256",
]
