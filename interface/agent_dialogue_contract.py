from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)


DIALOGUE_CONTRACT_VERSION = "veyra.agent_dialogue.v1"
MAX_DIALOGUE_BYTES = 32 * 1024
MAX_CONTEXT_BYTES = 12 * 1024
MAX_TURNS = 16

BoundedId = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    ),
]
BoundedText = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=4_000)]
ShortText = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=500)]
EvidenceRef = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=256)]
CapabilityName = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    ),
]
ScopeDigest = Annotated[
    str,
    StringConstraints(strict=True, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"),
]


class DialogueType(str, Enum):
    TASK_REQUEST = "TASK_REQUEST"
    EVIDENCE_REQUEST = "EVIDENCE_REQUEST"
    CHALLENGE = "CHALLENGE"
    OPTION_SET = "OPTION_SET"


class DialogueContractError(ValueError):
    """A stable, caller-safe validation error for the dialogue boundary."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        strict=True,
        extra="forbid",
        validate_assignment=True,
        frozen=True,
    )


class AuthorityBoundary(_StrictModel):
    mode: Literal["read_only", "sandbox"]
    side_effects_require_governance: Literal[True] = True
    capability_expansion_authorized: Literal[False] = False
    verification_authority: Literal[False] = False


class TaskRequestPayload(_StrictModel):
    user_goal: BoundedText
    constraints: Annotated[list[ShortText], Field(default_factory=list, max_length=16)]
    evidence_refs: Annotated[list[EvidenceRef], Field(default_factory=list, max_length=32)]
    context: dict[str, Any] = Field(default_factory=dict)
    authority: AuthorityBoundary

    @field_validator("context")
    @classmethod
    def _context_is_bounded_json(cls, value: dict[str, Any]) -> dict[str, Any]:
        try:
            encoded = _canonical_json(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("context must contain JSON values") from exc
        if len(encoded) > MAX_CONTEXT_BYTES:
            raise ValueError(f"context exceeds {MAX_CONTEXT_BYTES} bytes")
        return value


class EvidenceNeed(_StrictModel):
    question: ShortText
    reason: ShortText
    claim_ref: EvidenceRef | None = None
    freshness_required: bool = False


class EvidenceRequestPayload(_StrictModel):
    requested_evidence: Annotated[list[EvidenceNeed], Field(min_length=1, max_length=8)]
    requested_capabilities: Annotated[
        list[CapabilityName],
        Field(default_factory=list, max_length=8),
    ]


class ChallengePayload(_StrictModel):
    challenged_claim_refs: Annotated[list[EvidenceRef], Field(min_length=1, max_length=8)]
    reason: BoundedText
    alternative: BoundedText
    evidence_refs: Annotated[list[EvidenceRef], Field(default_factory=list, max_length=16)]
    requested_capabilities: Annotated[
        list[CapabilityName],
        Field(default_factory=list, max_length=8),
    ]


class Option(_StrictModel):
    option_id: BoundedId
    summary: BoundedText
    assumptions: Annotated[list[ShortText], Field(default_factory=list, max_length=12)]
    expected_outcome: BoundedText
    costs: Annotated[list[ShortText], Field(default_factory=list, max_length=8)]
    risks: Annotated[list[ShortText], Field(default_factory=list, max_length=12)]
    evidence_refs: Annotated[list[EvidenceRef], Field(default_factory=list, max_length=16)]
    required_capabilities: Annotated[
        list[CapabilityName],
        Field(default_factory=list, max_length=8),
    ]


class OptionSetPayload(_StrictModel):
    options: Annotated[list[Option], Field(min_length=2, max_length=5)]
    recommended_option_id: BoundedId | None = None

    @model_validator(mode="after")
    def _recommendation_references_an_option(self) -> OptionSetPayload:
        option_ids = [option.option_id for option in self.options]
        if len(option_ids) != len(set(option_ids)):
            raise ValueError("option_id values must be unique")
        if self.recommended_option_id and self.recommended_option_id not in set(option_ids):
            raise ValueError("recommended_option_id must reference an option")
        return self


class _DialogueEnvelope(_StrictModel):
    contract_version: Literal[DIALOGUE_CONTRACT_VERSION]
    message_id: BoundedId
    case_id: BoundedId
    case_revision: Annotated[int, Field(strict=True, ge=0)]
    turn_index: Annotated[int, Field(strict=True, ge=0, le=MAX_TURNS)]
    task_packet_id: BoundedId
    operation_id: BoundedId
    scope_digest: ScopeDigest

    @model_validator(mode="after")
    def _message_is_bounded(self) -> _DialogueEnvelope:
        if len(_canonical_json(self.model_dump(mode="json"))) > MAX_DIALOGUE_BYTES:
            raise ValueError(f"dialogue message exceeds {MAX_DIALOGUE_BYTES} bytes")
        return self


class TaskRequestMessage(_DialogueEnvelope):
    message_type: Literal["TASK_REQUEST"]
    sender: Literal["veyra"]
    in_reply_to: None = None
    payload: TaskRequestPayload


class EvidenceRequestMessage(_DialogueEnvelope):
    message_type: Literal["EVIDENCE_REQUEST"]
    sender: Literal["agent"]
    in_reply_to: BoundedId
    payload: EvidenceRequestPayload


class ChallengeMessage(_DialogueEnvelope):
    message_type: Literal["CHALLENGE"]
    sender: Literal["agent"]
    in_reply_to: BoundedId
    payload: ChallengePayload


class OptionSetMessage(_DialogueEnvelope):
    message_type: Literal["OPTION_SET"]
    sender: Literal["agent"]
    in_reply_to: BoundedId
    payload: OptionSetPayload


DialogueMessage: TypeAlias = Annotated[
    TaskRequestMessage | EvidenceRequestMessage | ChallengeMessage | OptionSetMessage,
    Field(discriminator="message_type"),
]
_DIALOGUE_ADAPTER = TypeAdapter(DialogueMessage)


def build_task_request(
    *,
    case_id: str,
    case_revision: int,
    turn_index: int,
    message_id: str,
    task_packet_id: str,
    operation_id: str,
    scope_digest: str,
    user_goal: str,
    constraints: list[str] | None = None,
    evidence_refs: list[str] | None = None,
    context: dict[str, Any] | None = None,
    authority: dict[str, Any] | AuthorityBoundary | None = None,
) -> dict[str, Any]:
    """Build the only Veyra-to-Agent Phase 4 message.

    The default boundary is read-only. Choosing ``sandbox`` changes where work
    may occur, not who owns authorization or verification.
    """

    message = TaskRequestMessage.model_validate(
        {
            "contract_version": DIALOGUE_CONTRACT_VERSION,
            "message_id": message_id,
            "message_type": DialogueType.TASK_REQUEST.value,
            "case_id": case_id,
            "case_revision": case_revision,
            "turn_index": turn_index,
            "task_packet_id": task_packet_id,
            "operation_id": operation_id,
            "scope_digest": scope_digest,
            "sender": "veyra",
            "in_reply_to": None,
            "payload": {
                "user_goal": user_goal,
                "constraints": list(constraints or []),
                "evidence_refs": list(evidence_refs or []),
                "context": dict(context or {}),
                "authority": authority
                or {
                    "mode": "read_only",
                    "side_effects_require_governance": True,
                    "capability_expansion_authorized": False,
                    "verification_authority": False,
                },
            },
        },
        strict=True,
    )
    return message.model_dump(mode="json")


def parse_dialogue_message(
    raw: Any,
    *,
    expected_case_id: str | None = None,
    expected_case_revision: int | None = None,
    expected_turn_index: int | None = None,
    expected_in_reply_to: str | None = None,
    expected_sender: Literal["veyra", "agent"] | None = None,
    expected_task_packet_id: str | None = None,
    expected_operation_id: str | None = None,
    expected_scope_digest: str | None = None,
    allowed_types: set[DialogueType | str] | None = None,
) -> DialogueMessage:
    """Parse one exact message and enforce its Case-turn binding.

    Validation never derives a message from prose and never treats Agent
    fields as authority, evidence, or a verified outcome.
    """

    try:
        message = _DIALOGUE_ADAPTER.validate_python(raw, strict=True)
    except ValidationError as exc:
        raise DialogueContractError(_validation_errors(exc)) from exc

    errors: list[str] = []
    _expect(errors, "case_id", message.case_id, expected_case_id)
    _expect(errors, "case_revision", message.case_revision, expected_case_revision)
    _expect(errors, "turn_index", message.turn_index, expected_turn_index)
    _expect(errors, "in_reply_to", message.in_reply_to, expected_in_reply_to)
    _expect(errors, "sender", message.sender, expected_sender)
    _expect(errors, "task_packet_id", message.task_packet_id, expected_task_packet_id)
    _expect(errors, "operation_id", message.operation_id, expected_operation_id)
    _expect(errors, "scope_digest", message.scope_digest, expected_scope_digest)

    if allowed_types is not None:
        normalized_allowed = {
            item if isinstance(item, DialogueType) else DialogueType(item)
            for item in allowed_types
        }
        message_type = (
            message.message_type
            if isinstance(message.message_type, DialogueType)
            else DialogueType(message.message_type)
        )
        if message_type not in normalized_allowed:
            errors.append(f"message_type mismatch: {message_type.value} is not allowed")
    if errors:
        raise DialogueContractError(errors)
    return message


def validate_dialogue_message(raw: Any, **expected: Any) -> list[str]:
    try:
        parse_dialogue_message(raw, **expected)
    except (DialogueContractError, ValueError) as exc:
        if isinstance(exc, DialogueContractError):
            return list(exc.errors)
        return [str(exc)]
    return []


def extract_agent_dialogue(
    agent_response: Any,
    *,
    expected_case_id: str,
    expected_case_revision: int,
    expected_turn_index: int,
    expected_in_reply_to: str,
    expected_task_packet_id: str,
    expected_operation_id: str,
    expected_scope_digest: str,
) -> dict[str, Any] | None:
    """Read only the explicit Agent envelope; free text is never promoted."""

    if not isinstance(agent_response, dict) or "dialogue_message" not in agent_response:
        return None
    message = parse_dialogue_message(
        agent_response["dialogue_message"],
        expected_sender="agent",
        expected_case_id=expected_case_id,
        expected_case_revision=expected_case_revision,
        expected_turn_index=expected_turn_index,
        expected_in_reply_to=expected_in_reply_to,
        expected_task_packet_id=expected_task_packet_id,
        expected_operation_id=expected_operation_id,
        expected_scope_digest=expected_scope_digest,
        allowed_types={
            DialogueType.EVIDENCE_REQUEST,
            DialogueType.CHALLENGE,
            DialogueType.OPTION_SET,
        },
    )
    return message.model_dump(mode="json")


def dialogue_contract_summary() -> dict[str, Any]:
    return {
        "contract_version": DIALOGUE_CONTRACT_VERSION,
        "message_types": [item.value for item in DialogueType],
        "max_message_bytes": MAX_DIALOGUE_BYTES,
        "max_context_bytes": MAX_CONTEXT_BYTES,
        "max_turns": MAX_TURNS,
        "binding": [
            "case_id",
            "case_revision",
            "turn_index",
            "task_packet_id",
            "operation_id",
            "scope_digest",
            "in_reply_to",
        ],
        "authority": {
            "agent_messages_are_proposals": True,
            "agent_can_verify": False,
            "agent_can_expand_capabilities": False,
            "side_effects_require_governance": True,
        },
    }


def scope_digest(*, user_id: str, workspace_id: str) -> str:
    """Return the provider-neutral binding digest used by the Case runtime."""

    for field_name, value in (
        ("user_id", user_id),
        ("workspace_id", workspace_id),
    ):
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or len(value) > 240
            or any(ord(character) < 32 for character in value)
        ):
            raise ValueError(f"{field_name} must be normalized bounded text")
    return hashlib.sha256(
        _canonical_json(
            {
                "user_id": user_id,
                "workspace_id": workspace_id,
            }
        )
    ).hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _expect(errors: list[str], field: str, actual: Any, expected: Any) -> None:
    if expected is not None and actual != expected:
        errors.append(f"{field} mismatch")


def _validation_errors(exc: ValidationError) -> list[str]:
    return [
        f"{'.'.join(str(item) for item in error.get('loc', ()))}: {error.get('msg', 'invalid')}"
        for error in exc.errors(include_url=False)
    ]
