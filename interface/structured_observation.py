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


STRUCTURED_OBSERVATION_COMMAND_SCHEMA = (
    "veyra.structured_observation.command.v1"
)
STRUCTURED_OBSERVATION_EVENT_SCHEMA = (
    "veyra.structured_observation.event.v1"
)
STRUCTURED_OBSERVATION_CHANNEL = "structured_observation"

StructuredObservationProducer = Literal[
    "commitment_runtime",
    "component_health",
    "local_operator",
    "task_runtime",
    "workspace_observer",
]
StructuredObservationAnchorKind = Literal[
    "goal",
    "commitment",
    "case",
    "task",
    "trace",
    "entity",
]
StructuredObservationFactKind = Literal[
    "availability_signal",
    "change_signal",
    "deadline_signal",
    "progress_signal",
    "risk_signal",
]
StructuredObservationFactState = Literal[
    "blocked",
    "changed",
    "completed",
    "degraded",
    "present",
    "recovered",
]
StructuredObservationEvidenceSource = Literal[
    "derived_rule",
    "direct_tool_observation",
    "external_primary_source",
    "human_verified",
]


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,239}$")


def parse_aware_utc(value: str) -> datetime:
    selected = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if selected.tzinfo is None or selected.utcoffset() is None:
        raise ValueError("structured observation time must be timezone-aware")
    return selected.astimezone(timezone.utc)


def canonical_utc(value: datetime) -> str:
    selected = value.astimezone(timezone.utc)
    return selected.isoformat(timespec="microseconds").replace("+00:00", "Z")


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


class StructuredObservationAuthority(BaseModel):
    """Authority explicitly absent from this informational ingress."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    route_change: Literal[False] = False
    agent_dispatch: Literal[False] = False
    tool_call: Literal[False] = False
    capability_grant: Literal[False] = False
    external_delivery: Literal[False] = False
    execution: Literal[False] = False
    fact_certification: Literal[False] = False


class StructuredObservationAnchor(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    kind: StructuredObservationAnchorKind
    ref_id: str = Field(min_length=1, max_length=240)

    @field_validator("ref_id")
    @classmethod
    def validate_ref_id(cls, value: str) -> str:
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError("structured anchor id is invalid")
        return value


class StructuredObservationEvidence(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    evidence_id: str = Field(min_length=1, max_length=240)
    source: StructuredObservationEvidenceSource

    @field_validator("evidence_id")
    @classmethod
    def validate_evidence_id(cls, value: str) -> str:
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError("structured evidence id is invalid")
        return value


class StructuredObservationFacts(BaseModel):
    """Bounded categorical inputs; callers never provide numeric salience."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    kind: StructuredObservationFactKind
    state: StructuredObservationFactState
    severity: Literal["info", "low", "moderate", "high", "critical"]
    urgency: Literal["none", "routine", "soon", "immediate"]
    novelty: Literal["known", "changed", "new"]
    uncertainty: Literal["low", "medium", "high"]
    evidence_quality: Literal["partial", "corroborated", "direct"]
    #: How the producer came to know this. Required, with no default: an
    #: unstated epistemic status would silently read as an observation, which
    #: is exactly what lets a model inference qualify as confirming evidence.
    #: `is_fact` is never accepted from the caller; the server derives it.
    epistemic_status: Literal["observed", "inference", "prediction"]


class StructuredObservationCommand(BaseModel):
    """Strict private command for one evidence-linked observation.

    There is intentionally no text, metadata, title, summary, numeric score,
    route, tool, Agent, grant, notification, or execution field.
    """

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal[STRUCTURED_OBSERVATION_COMMAND_SCHEMA]
    operation_id: str = Field(min_length=1, max_length=240)
    producer_id: StructuredObservationProducer
    producer_receipt_id: str = Field(min_length=1, max_length=240)
    user_id: str = Field(min_length=1, max_length=240)
    workspace_id: str = Field(min_length=1, max_length=1024)
    session_id: str = Field(min_length=1, max_length=240)
    expected_event_inbox_revision: StrictInt = Field(
        ge=0,
        le=2_147_483_647,
    )
    occurred_at: str = Field(min_length=20, max_length=40)
    valid_until: str = Field(min_length=20, max_length=40)
    anchors: list[StructuredObservationAnchor] = Field(
        min_length=1,
        max_length=8,
    )
    evidence: list[StructuredObservationEvidence] = Field(
        min_length=1,
        max_length=16,
    )
    facts: StructuredObservationFacts

    @field_validator("operation_id", "producer_receipt_id")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError("structured observation identifier is invalid")
        return value

    @field_validator("occurred_at", "valid_until")
    @classmethod
    def validate_time(cls, value: str) -> str:
        # Require a canonical representation so the same command has one
        # immutable digest and replay cannot exploit equivalent timestamps.
        parsed = parse_aware_utc(value)
        if canonical_utc(parsed) != value:
            raise ValueError("structured observation time must be canonical UTC")
        return value

    @model_validator(mode="after")
    def validate_command(self) -> "StructuredObservationCommand":
        if parse_aware_utc(self.valid_until) < parse_aware_utc(self.occurred_at):
            raise ValueError("structured observation interval is invalid")
        anchor_keys = [(item.kind, item.ref_id) for item in self.anchors]
        if len(anchor_keys) != len(set(anchor_keys)):
            raise ValueError("structured observation anchors must be unique")
        evidence_keys = [item.evidence_id for item in self.evidence]
        if len(evidence_keys) != len(set(evidence_keys)):
            raise ValueError("structured observation evidence must be unique")
        return self

    def canonical_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def command_digest(self) -> str:
        # CAS is transport concurrency, not semantic identity. A retry may
        # carry the original CAS value after the first successful mutation.
        payload = self.canonical_dict()
        payload.pop("expected_event_inbox_revision", None)
        return canonical_digest(payload)


class ComponentHealthObservationRequest(BaseModel):
    """Scope and CAS only; health facts are always derived server-side."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal["veyra.component_health_observation.request.v1"]
    operation_id: str = Field(min_length=1, max_length=240)
    user_id: str = Field(min_length=1, max_length=240)
    workspace_id: str = Field(min_length=1, max_length=1024)
    session_id: str = Field(min_length=1, max_length=240)
    expected_event_inbox_revision: StrictInt = Field(
        ge=0,
        le=2_147_483_647,
    )
    occurred_at: str = Field(min_length=20, max_length=40)

    @field_validator("operation_id")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError("component health operation identifier is invalid")
        return value

    @field_validator("occurred_at")
    @classmethod
    def validate_time(cls, value: str) -> str:
        parsed = parse_aware_utc(value)
        if canonical_utc(parsed) != value:
            raise ValueError("component health time must be canonical UTC")
        return value


class TrustedWorkspaceObserverConfigRequest(BaseModel):
    """Strict token-authenticated private observer binding request."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal["veyra.trusted_workspace_observer.config.v1"]
    expected_state_revision: StrictInt = Field(ge=0, le=2_147_483_647)
    mode: Literal["disabled", "record_only"]
    user_id: str = Field(min_length=1, max_length=240)
    session_id: str = Field(min_length=1, max_length=240)
    workspace_id: str = Field(min_length=1, max_length=4096)
    goal_id: str = Field(min_length=1, max_length=240)
    github_repo_id: str | None = Field(default=None, max_length=240)
    github_workflow_path: str | None = Field(default=None, max_length=512)
    github_required_jobs: list[str] | None = Field(default=None, max_length=20)
    github_expected_app_id: StrictInt | None = Field(default=None, ge=1)


class TrustedWorkspaceObserverRunRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal["veyra.trusted_workspace_observer.run.v1"]
    reason: str = Field(default="private_control", min_length=1, max_length=120)


__all__ = [
    "STRUCTURED_OBSERVATION_CHANNEL",
    "STRUCTURED_OBSERVATION_COMMAND_SCHEMA",
    "STRUCTURED_OBSERVATION_EVENT_SCHEMA",
    "StructuredObservationAnchor",
    "StructuredObservationAuthority",
    "StructuredObservationCommand",
    "ComponentHealthObservationRequest",
    "TrustedWorkspaceObserverConfigRequest",
    "TrustedWorkspaceObserverRunRequest",
    "StructuredObservationEvidence",
    "StructuredObservationFacts",
    "canonical_digest",
    "canonical_utc",
    "parse_aware_utc",
]
