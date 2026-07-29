from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)


CASE_SCHEMA_VERSION = "veyra.durable_case.v1"
CASE_DOCUMENT_SCHEMA_VERSION = "veyra.durable_case_state.v1"
CASE_TRACE_SCHEMA_VERSION = "veyra.durable_case_trace.v1"

MAX_DIALOGUE_CONTENT_BYTES = 32 * 1024
MAX_REASON_LENGTH = 1200
MAX_USER_GOAL_BYTES = 4 * 1024


class CaseStatus(str, Enum):
    OBSERVING = "OBSERVING"
    QUALIFIED = "QUALIFIED"
    DELIBERATING = "DELIBERATING"
    AWAITING_EVIDENCE = "AWAITING_EVIDENCE"
    PROPOSED = "PROPOSED"
    PAUSED = "PAUSED"
    CANCELLING = "CANCELLING"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    INDETERMINATE = "INDETERMINATE"
    CLOSED = "CLOSED"


class CasePriority(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class DialogueMessageType(str, Enum):
    TASK_REQUEST = "TASK_REQUEST"
    CONTEXT_PATCH = "CONTEXT_PATCH"
    EVIDENCE_REQUEST = "EVIDENCE_REQUEST"
    CHALLENGE = "CHALLENGE"
    OPTION_SET = "OPTION_SET"
    PLAN_SELECTION = "PLAN_SELECTION"


class CheckpointEffectState(str, Enum):
    NOT_STARTED = "not_started"
    STARTED = "started"
    OBSERVED = "observed"
    UNKNOWN = "unknown"


TERMINAL_CASE_STATUSES = frozenset(
    {
        CaseStatus.CANCELLED,
        CaseStatus.FAILED,
        CaseStatus.INDETERMINATE,
        CaseStatus.CLOSED,
    }
)

# Phase 4's first slice is analysis-only. In particular, this table deliberately
# has no authorization or execution state.
ALLOWED_CASE_TRANSITIONS: dict[CaseStatus, frozenset[CaseStatus]] = {
    CaseStatus.OBSERVING: frozenset(
        {
            CaseStatus.QUALIFIED,
            CaseStatus.PAUSED,
            CaseStatus.FAILED,
            CaseStatus.INDETERMINATE,
            CaseStatus.CLOSED,
        }
    ),
    CaseStatus.QUALIFIED: frozenset(
        {
            CaseStatus.DELIBERATING,
            CaseStatus.PAUSED,
            CaseStatus.CANCELLING,
            CaseStatus.FAILED,
            CaseStatus.INDETERMINATE,
            CaseStatus.CLOSED,
        }
    ),
    CaseStatus.DELIBERATING: frozenset(
        {
            CaseStatus.AWAITING_EVIDENCE,
            CaseStatus.PROPOSED,
            CaseStatus.PAUSED,
            CaseStatus.CANCELLING,
            CaseStatus.FAILED,
            CaseStatus.INDETERMINATE,
            CaseStatus.CLOSED,
        }
    ),
    CaseStatus.AWAITING_EVIDENCE: frozenset(
        {
            CaseStatus.DELIBERATING,
            CaseStatus.PROPOSED,
            CaseStatus.PAUSED,
            CaseStatus.CANCELLING,
            CaseStatus.FAILED,
            CaseStatus.INDETERMINATE,
            CaseStatus.CLOSED,
        }
    ),
    CaseStatus.PROPOSED: frozenset(
        {
            CaseStatus.DELIBERATING,
            CaseStatus.AWAITING_EVIDENCE,
            CaseStatus.PAUSED,
            CaseStatus.CANCELLING,
            CaseStatus.FAILED,
            CaseStatus.INDETERMINATE,
            CaseStatus.CLOSED,
        }
    ),
    CaseStatus.PAUSED: frozenset(
        {
            CaseStatus.QUALIFIED,
            CaseStatus.DELIBERATING,
            CaseStatus.AWAITING_EVIDENCE,
            CaseStatus.PROPOSED,
            CaseStatus.CANCELLING,
            CaseStatus.FAILED,
            CaseStatus.INDETERMINATE,
            CaseStatus.CLOSED,
        }
    ),
    CaseStatus.CANCELLING: frozenset(
        {
            CaseStatus.CANCELLED,
            CaseStatus.FAILED,
            CaseStatus.INDETERMINATE,
        }
    ),
    CaseStatus.CANCELLED: frozenset(),
    CaseStatus.FAILED: frozenset(),
    CaseStatus.INDETERMINATE: frozenset(),
    CaseStatus.CLOSED: frozenset(),
}


class StrictCaseModel(BaseModel):
    model_config = ConfigDict(
        strict=True,
        extra="forbid",
        frozen=True,
        validate_assignment=True,
        populate_by_name=True,
    )


def _validate_normalized_text(
    value: str,
    *,
    field_name: str,
    min_length: int = 1,
    max_length: int = 240,
) -> str:
    if value != value.strip():
        raise ValueError(f"{field_name} cannot have leading or trailing whitespace")
    if len(value) < min_length or len(value) > max_length:
        raise ValueError(
            f"{field_name} length must be between {min_length} and {max_length}"
        )
    if any(ord(character) < 32 for character in value):
        raise ValueError(f"{field_name} cannot contain control characters")
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def sha256_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def deterministic_case_id(*, user_id: str, workspace_id: str, event_id: str) -> str:
    identity = {
        "namespace": "veyra.durable-case.v1",
        "user_id": _validate_normalized_text(user_id, field_name="user_id"),
        "workspace_id": _validate_normalized_text(
            workspace_id, field_name="workspace_id"
        ),
        "event_id": _validate_normalized_text(event_id, field_name="event_id"),
    }
    return f"case_{sha256_digest(identity)[:32]}"


def operation_id_digest(
    *, operation_id: str, case_id: str, user_id: str, workspace_id: str
) -> str:
    normalized_operation_id = _validate_normalized_text(
        operation_id, field_name="operation_id", max_length=320
    )
    return sha256_digest(
        {
            "namespace": "veyra.durable-case-operation.v1",
            "operation_id": normalized_operation_id,
            "case_id": case_id,
            "user_id": user_id,
            "workspace_id": workspace_id,
        }
    )


class CaseScope(StrictCaseModel):
    user_id: str
    workspace_id: str

    @field_validator("user_id", "workspace_id")
    @classmethod
    def validate_identifier(cls, value: str, info: Any) -> str:
        return _validate_normalized_text(value, field_name=info.field_name)


class CaseCheckpoint(StrictCaseModel):
    schema_version: Literal["veyra.durable_case_checkpoint.v1"] = (
        "veyra.durable_case_checkpoint.v1"
    )
    checkpoint_id: str
    phase: str = Field(min_length=1, max_length=120)
    operation_id: str | None = Field(default=None, min_length=1, max_length=320)
    step_id: str | None = Field(default=None, min_length=1, max_length=240)
    task_id: str | None = Field(default=None, min_length=1, max_length=240)
    run_id: str | None = Field(default=None, min_length=1, max_length=240)
    session_key: str | None = Field(default=None, min_length=1, max_length=500)
    binding_digest: str | None = Field(default=None, min_length=16, max_length=128)
    executor: str | None = Field(default=None, min_length=1, max_length=120)
    target_agent: str | None = Field(default=None, min_length=1, max_length=120)
    dialogue_message_id: str | None = Field(
        default=None, min_length=1, max_length=240
    )
    result_status: str | None = Field(default=None, min_length=1, max_length=120)
    effect_state: CheckpointEffectState = CheckpointEffectState.NOT_STARTED
    evidence_refs: list[str] = Field(default_factory=list, max_length=32)
    recorded_at: AwareDatetime

    @field_validator("checkpoint_id", "phase")
    @classmethod
    def validate_required_text(cls, value: str, info: Any) -> str:
        return _validate_normalized_text(
            value,
            field_name=info.field_name,
            max_length=240 if info.field_name == "checkpoint_id" else 120,
        )

    @field_validator(
        "operation_id",
        "step_id",
        "task_id",
        "run_id",
        "session_key",
        "binding_digest",
        "executor",
        "target_agent",
        "dialogue_message_id",
        "result_status",
    )
    @classmethod
    def validate_optional_text(cls, value: str | None, info: Any) -> str | None:
        if value is None:
            return None
        return _validate_normalized_text(
            value,
            field_name=info.field_name,
            max_length=(
                500
                if info.field_name == "session_key"
                else 320
                if info.field_name == "operation_id"
                else 240
            ),
        )

    @field_validator("evidence_refs")
    @classmethod
    def validate_evidence_refs(cls, value: list[str]) -> list[str]:
        normalized = [
            _validate_normalized_text(
                item, field_name="evidence_ref", max_length=500
            )
            for item in value
        ]
        if len(set(normalized)) != len(normalized):
            raise ValueError("evidence_refs cannot contain duplicates")
        return normalized


class DialogueRecord(StrictCaseModel):
    schema_version: Literal["veyra.durable_case_dialogue.v1"] = (
        "veyra.durable_case_dialogue.v1"
    )
    message_id: str
    message_type: DialogueMessageType
    sender: Literal["veyra", "agent"]
    direction: Literal["veyra_to_agent", "agent_to_veyra"]
    case_revision: int = Field(ge=1)
    turn_index: int = Field(ge=0)
    in_reply_to: str | None = Field(default=None, min_length=1, max_length=240)
    content: dict[str, JsonValue]
    authority_granted: Literal[False] = False
    evidence_verified: Literal[False] = False
    recorded_at: AwareDatetime

    @field_validator("message_id")
    @classmethod
    def validate_message_id(cls, value: str) -> str:
        return _validate_normalized_text(
            value, field_name="message_id", max_length=240
        )

    @field_validator("in_reply_to")
    @classmethod
    def validate_reply_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_normalized_text(
            value, field_name="in_reply_to", max_length=240
        )

    @field_validator("content")
    @classmethod
    def validate_content_size(
        cls, value: dict[str, JsonValue]
    ) -> dict[str, JsonValue]:
        if len(canonical_json(value).encode("utf-8")) > MAX_DIALOGUE_CONTENT_BYTES:
            raise ValueError(
                f"dialogue content exceeds {MAX_DIALOGUE_CONTENT_BYTES} bytes"
            )
        return value

    @model_validator(mode="after")
    def validate_direction(self) -> "DialogueRecord":
        expected = (
            "veyra_to_agent" if self.sender == "veyra" else "agent_to_veyra"
        )
        if self.direction != expected:
            raise ValueError("dialogue direction does not match sender")
        veyra_message_types = {
            DialogueMessageType.TASK_REQUEST,
            DialogueMessageType.CONTEXT_PATCH,
            DialogueMessageType.PLAN_SELECTION,
        }
        if self.message_type in veyra_message_types:
            if self.sender != "veyra":
                raise ValueError(
                    f"{self.message_type.value} must be sent by Veyra"
                )
            if (
                self.message_type
                in {
                    DialogueMessageType.CONTEXT_PATCH,
                    DialogueMessageType.PLAN_SELECTION,
                }
                and self.in_reply_to is None
            ):
                raise ValueError(
                    f"{self.message_type.value} must identify its parent"
                )
        else:
            if self.sender != "agent":
                raise ValueError(
                    f"{self.message_type.value} must be sent by the Agent"
                )
            if not self.in_reply_to:
                raise ValueError(
                    f"{self.message_type.value} must identify its TASK_REQUEST"
                )
        envelope = {
            "message_id": self.message_id,
            "message_type": self.message_type.value,
            "sender": self.sender,
            "case_revision": self.case_revision,
            "turn_index": self.turn_index,
            "in_reply_to": self.in_reply_to,
        }
        mismatched = [
            key
            for key, expected in envelope.items()
            if self.content.get(key) != expected
        ]
        if mismatched:
            raise ValueError(
                "dialogue record metadata does not match its content "
                f"envelope: {mismatched}"
            )
        return self


class CaseOperation(StrictCaseModel):
    operation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    semantic_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    kind: str = Field(min_length=1, max_length=120)
    resulting_revision: int = Field(ge=1)
    resulting_status: CaseStatus
    applied_at: AwareDatetime


class DurableCase(StrictCaseModel):
    schema_version: Literal["veyra.durable_case.v1"] = CASE_SCHEMA_VERSION
    case_id: str = Field(min_length=16, max_length=80)
    case_type: str = Field(default="agent_task", min_length=1, max_length=120)
    scope: CaseScope
    source_event_id: str = Field(min_length=1, max_length=240)
    user_goal: str = Field(min_length=1, max_length=4000)
    situation_id: str | None = Field(default=None, min_length=1, max_length=240)
    goal_ids: list[str] = Field(default_factory=list, max_length=32)
    commitment_ids: list[str] = Field(default_factory=list, max_length=32)
    status: CaseStatus
    paused_from_status: CaseStatus | None = None
    priority: CasePriority = CasePriority.NORMAL
    revision: int = Field(ge=1)
    checkpoints: list[CaseCheckpoint] = Field(default_factory=list, max_length=32)
    dialogue: list[DialogueRecord] = Field(default_factory=list, max_length=32)
    operations: dict[str, CaseOperation] = Field(default_factory=dict, max_length=64)
    next_wakeup_at: AwareDatetime | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @field_validator(
        "case_id", "case_type", "source_event_id", "situation_id"
    )
    @classmethod
    def validate_text(cls, value: str | None, info: Any) -> str | None:
        if value is None:
            return None
        return _validate_normalized_text(
            value,
            field_name=info.field_name,
            max_length=240 if info.field_name != "case_id" else 80,
        )

    @field_validator("user_goal")
    @classmethod
    def validate_user_goal(cls, value: str) -> str:
        normalized = _validate_normalized_text(
            value, field_name="user_goal", max_length=4000
        )
        if len(normalized.encode("utf-8")) > MAX_USER_GOAL_BYTES:
            raise ValueError(f"user_goal exceeds {MAX_USER_GOAL_BYTES} bytes")
        return normalized

    @field_validator("goal_ids", "commitment_ids")
    @classmethod
    def validate_refs(cls, value: list[str], info: Any) -> list[str]:
        normalized = [
            _validate_normalized_text(
                item, field_name=info.field_name, max_length=240
            )
            for item in value
        ]
        if len(set(normalized)) != len(normalized):
            raise ValueError(f"{info.field_name} cannot contain duplicates")
        return normalized

    @model_validator(mode="after")
    def validate_integrity(self) -> "DurableCase":
        expected_id = deterministic_case_id(
            user_id=self.scope.user_id,
            workspace_id=self.scope.workspace_id,
            event_id=self.source_event_id,
        )
        if self.case_id != expected_id:
            raise ValueError("case_id does not match the scoped source event")
        if self.paused_from_status is not None:
            if self.status != CaseStatus.PAUSED:
                raise ValueError(
                    "paused_from_status is only valid while status is PAUSED"
                )
            if self.paused_from_status in TERMINAL_CASE_STATUSES:
                raise ValueError("paused_from_status cannot be terminal")
            if self.paused_from_status == CaseStatus.PAUSED:
                raise ValueError(
                    "paused_from_status cannot refer to PAUSED itself"
                )
        for digest, operation in self.operations.items():
            if digest != operation.operation_digest:
                raise ValueError(
                    "operation registry key does not match operation digest"
                )
            if operation.resulting_revision > self.revision:
                raise ValueError(
                    "operation result cannot refer to a future case revision"
                )
        return self


class DurableCaseDocument(StrictCaseModel):
    schema_version: Literal["veyra.durable_case_state.v1"] = (
        CASE_DOCUMENT_SCHEMA_VERSION
    )
    cases: dict[str, DurableCase] = Field(default_factory=dict)
    event_index: dict[str, str] = Field(default_factory=dict)
    trace_outbox: list[dict[str, JsonValue]] = Field(
        default_factory=list, max_length=512
    )
    trace_sequence: int = Field(default=0, ge=0)
    trace_outbox_count: int = Field(default=0, ge=0)
    recovery_cursor: int = Field(default=0, ge=0)
    source: Literal["durable_case_store"] = "durable_case_store"
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    ttl_seconds: Literal[0] = 0
    status: Literal["fresh"] = "fresh"
    state_revision: int = Field(default=0, ge=0, alias="_state_revision")
    updated_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_indexes(self) -> "DurableCaseDocument":
        for case_id, case in self.cases.items():
            if case_id != case.case_id:
                raise ValueError("case registry key does not match case_id")
        for event_key, case_id in self.event_index.items():
            if not event_key or case_id not in self.cases:
                raise ValueError("event index references an unknown case")
        if self.trace_outbox_count != len(self.trace_outbox):
            raise ValueError("trace_outbox_count does not match trace_outbox")
        return self


def now_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value
