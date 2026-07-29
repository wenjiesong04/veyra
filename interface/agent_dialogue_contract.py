from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import (
    AwareDatetime,
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
    CONTEXT_PATCH = "CONTEXT_PATCH"
    EVIDENCE_REQUEST = "EVIDENCE_REQUEST"
    CHALLENGE = "CHALLENGE"
    OPTION_SET = "OPTION_SET"
    PLAN_SELECTION = "PLAN_SELECTION"


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


class CollaborationPrivacyScope(_StrictModel):
    memory_read_allowed: Literal[False] = False
    memory_write_allowed: Literal[False] = False
    workspace_access_allowed: Literal[False] = False
    network_expansion_allowed: Literal[False] = False
    sensitive_data_allowed: Literal[False] = False


class CollaborationEffectScope(_StrictModel):
    mode: Literal["read_only"] = "read_only"
    tool_calls_allowed: Literal[False] = False
    side_effects_allowed: Literal[False] = False
    execution_authorized: Literal[False] = False
    verification_authority: Literal[False] = False


class CollaborationProviderBinding(_StrictModel):
    runtime: BoundedId
    provider: ShortText
    model: ShortText
    instance_id: BoundedId
    automatic_switch_allowed: Literal[False] = False

    @field_validator("provider", "model")
    @classmethod
    def _provider_text_is_normalized(
        cls,
        value: str,
        info: Any,
    ) -> str:
        if value != value.strip() or any(
            ord(character) < 32 for character in value
        ):
            raise ValueError(f"{info.field_name} must be normalized text")
        return value


class CollaborationBudget(_StrictModel):
    remaining_agent_calls: Annotated[int, Field(strict=True, ge=0, le=3)]
    remaining_handoffs: Annotated[int, Field(strict=True, ge=0, le=1)]
    remaining_evidence_patches: Annotated[
        int,
        Field(strict=True, ge=0, le=1),
    ]
    max_wall_time_seconds: Annotated[
        int,
        Field(strict=True, ge=1, le=600),
    ]
    max_context_bytes: Annotated[
        int,
        Field(strict=True, ge=256, le=MAX_CONTEXT_BYTES),
    ]
    max_output_bytes: Annotated[
        int,
        Field(strict=True, ge=256, le=MAX_DIALOGUE_BYTES),
    ]


class CollaborationBinding(_StrictModel):
    """Canonical, non-authorizing scope for one collaboration participant.

    The binding is optional on legacy Phase 4 messages. Once a Veyra outbound
    message carries it, the Agent reply must echo the entire binding exactly.
    Veyra-authored child turns may only narrow it through
    :func:`validate_collaboration_child`.
    """

    schema_version: Literal["veyra.agent_collaboration_binding.v1"] = (
        "veyra.agent_collaboration_binding.v1"
    )
    participant_id: BoundedId
    role: Literal["primary_analyst", "critic"]
    parent_participant_id: BoundedId | None = None
    handoff_index: Annotated[int, Field(strict=True, ge=0, le=1)]
    capability_scope: Annotated[
        list[CapabilityName],
        Field(default_factory=list, max_length=8),
    ]
    evidence_scope: Annotated[
        list[EvidenceRef],
        Field(default_factory=list, max_length=32),
    ]
    privacy: CollaborationPrivacyScope = Field(
        default_factory=CollaborationPrivacyScope
    )
    effect: CollaborationEffectScope = Field(
        default_factory=CollaborationEffectScope
    )
    provider: CollaborationProviderBinding
    budget: CollaborationBudget
    issued_at: AwareDatetime
    expires_at: AwareDatetime
    collaboration_scope_digest: ScopeDigest

    @field_validator("capability_scope", "evidence_scope")
    @classmethod
    def _scope_lists_are_canonical(
        cls,
        value: list[str],
        info: Any,
    ) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError(f"{info.field_name} cannot contain duplicates")
        if value != sorted(value):
            raise ValueError(f"{info.field_name} must use canonical sorted order")
        return value

    @field_validator("issued_at", "expires_at")
    @classmethod
    def _timestamps_use_utc(cls, value: datetime) -> datetime:
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _binding_is_canonical(self) -> "CollaborationBinding":
        if self.handoff_index == 0 and self.parent_participant_id is not None:
            raise ValueError(
                "initial participant cannot declare parent_participant_id"
            )
        if self.handoff_index == 0 and self.role != "primary_analyst":
            raise ValueError(
                "initial participant must use primary_analyst role"
            )
        if self.handoff_index > 0:
            if self.parent_participant_id is None:
                raise ValueError(
                    "handoff participant requires parent_participant_id"
                )
            if self.parent_participant_id == self.participant_id:
                raise ValueError(
                    "handoff participant must differ from its parent"
                )
            if self.role != "critic":
                raise ValueError("handoff participant must use critic role")
            if self.budget.remaining_handoffs != 0:
                raise ValueError(
                    "critic participant cannot retain handoff budget"
                )
        issued_at = self.issued_at.astimezone(timezone.utc)
        expires_at = self.expires_at.astimezone(timezone.utc)
        if expires_at <= issued_at:
            raise ValueError("collaboration expiry must follow issuance")
        if (
            expires_at - issued_at
        ).total_seconds() > self.budget.max_wall_time_seconds:
            raise ValueError(
                "collaboration expiry exceeds max_wall_time_seconds"
            )
        expected = collaboration_scope_digest(self)
        if self.collaboration_scope_digest != expected:
            raise ValueError(
                "collaboration_scope_digest does not match canonical scope"
            )
        return self


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


class ContextPatchPayload(_StrictModel):
    resolved_request_ids: Annotated[
        list[BoundedId],
        Field(default_factory=list, max_length=8),
    ]
    evidence_refs: Annotated[
        list[EvidenceRef],
        Field(default_factory=list, max_length=32),
    ]
    unresolved_request_ids: Annotated[
        list[BoundedId],
        Field(default_factory=list, max_length=8),
    ]
    context: dict[str, Any] = Field(default_factory=dict)
    context_digest: ScopeDigest
    authority: AuthorityBoundary = Field(
        default_factory=lambda: AuthorityBoundary(mode="read_only")
    )

    @field_validator(
        "resolved_request_ids",
        "evidence_refs",
        "unresolved_request_ids",
    )
    @classmethod
    def _lists_are_unique(cls, value: list[str], info: Any) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError(f"{info.field_name} cannot contain duplicates")
        return value

    @model_validator(mode="after")
    def _context_is_exact(self) -> "ContextPatchPayload":
        resolved = set(self.resolved_request_ids)
        unresolved = set(self.unresolved_request_ids)
        if not resolved and not unresolved:
            raise ValueError(
                "CONTEXT_PATCH must resolve or preserve at least one request"
            )
        if resolved.intersection(unresolved):
            raise ValueError(
                "resolved and unresolved request ids must be disjoint"
            )
        if resolved and not self.evidence_refs:
            raise ValueError(
                "resolved requests require bounded evidence_refs"
            )
        if self.evidence_refs and not resolved:
            raise ValueError(
                "evidence_refs cannot be added without a resolved request"
            )
        if self.authority.mode != "read_only":
            raise ValueError("CONTEXT_PATCH authority must remain read_only")
        try:
            encoded = _canonical_json(self.context)
        except (TypeError, ValueError) as exc:
            raise ValueError("context must contain JSON values") from exc
        if len(encoded) > MAX_CONTEXT_BYTES:
            raise ValueError(f"context exceeds {MAX_CONTEXT_BYTES} bytes")
        if self.context_digest != hashlib.sha256(encoded).hexdigest():
            raise ValueError("context_digest does not match canonical context")
        return self


class EvidenceNeed(_StrictModel):
    request_id: BoundedId | None = None
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

    @model_validator(mode="after")
    def _request_ids_are_unique(self) -> "EvidenceRequestPayload":
        request_ids = [
            item.request_id
            for item in self.requested_evidence
            if item.request_id is not None
        ]
        if len(request_ids) != len(set(request_ids)):
            raise ValueError("request_id values must be unique")
        return self


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


class PlanSelectionPayload(_StrictModel):
    option_set_message_id: BoundedId
    selected_option_id: BoundedId
    decision_reason: BoundedText
    decision_evidence_refs: Annotated[
        list[EvidenceRef],
        Field(default_factory=list, max_length=32),
    ]
    rejected_option_ids: Annotated[
        list[BoundedId],
        Field(default_factory=list, max_length=4),
    ]
    execution_authorized: Literal[False] = False
    next_action: Literal["await_human_or_close"] = "await_human_or_close"

    @field_validator("decision_evidence_refs", "rejected_option_ids")
    @classmethod
    def _selection_lists_are_unique(
        cls,
        value: list[str],
        info: Any,
    ) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError(f"{info.field_name} cannot contain duplicates")
        return value

    @model_validator(mode="after")
    def _selection_is_consistent(self) -> "PlanSelectionPayload":
        if self.selected_option_id in set(self.rejected_option_ids):
            raise ValueError("selected option cannot also be rejected")
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
    collaboration_binding: CollaborationBinding | None = None

    @model_validator(mode="after")
    def _message_is_bounded(self) -> _DialogueEnvelope:
        wire_payload = self.model_dump(mode="json")
        if wire_payload.get("collaboration_binding") is None:
            wire_payload.pop("collaboration_binding", None)
        if (
            wire_payload.get("message_type")
            == DialogueType.EVIDENCE_REQUEST.value
        ):
            for item in wire_payload.get("payload", {}).get(
                "requested_evidence",
                [],
            ):
                if isinstance(item, dict) and item.get("request_id") is None:
                    item.pop("request_id", None)
        if len(_canonical_json(wire_payload)) > MAX_DIALOGUE_BYTES:
            raise ValueError(f"dialogue message exceeds {MAX_DIALOGUE_BYTES} bytes")
        return self


class TaskRequestMessage(_DialogueEnvelope):
    message_type: Literal["TASK_REQUEST"]
    sender: Literal["veyra"]
    in_reply_to: BoundedId | None = None
    payload: TaskRequestPayload

    @model_validator(mode="after")
    def _legacy_or_handoff_request(self) -> "TaskRequestMessage":
        binding = self.collaboration_binding
        if binding is None:
            if self.in_reply_to is not None:
                raise ValueError(
                    "legacy TASK_REQUEST cannot identify a parent"
                )
            return self
        if self.payload.authority.mode != "read_only":
            raise ValueError(
                "collaboration TASK_REQUEST authority must remain read_only"
            )
        if binding.handoff_index == 0 and self.in_reply_to is not None:
            raise ValueError(
                "initial collaboration TASK_REQUEST cannot identify a parent"
            )
        if binding.handoff_index > 0 and self.in_reply_to is None:
            raise ValueError(
                "handoff TASK_REQUEST must identify its parent proposal"
            )
        return self


class ContextPatchMessage(_DialogueEnvelope):
    message_type: Literal["CONTEXT_PATCH"]
    sender: Literal["veyra"]
    in_reply_to: BoundedId
    payload: ContextPatchPayload

    @model_validator(mode="after")
    def _collaboration_is_required(self) -> "ContextPatchMessage":
        if self.collaboration_binding is None:
            raise ValueError(
                "CONTEXT_PATCH requires collaboration_binding"
            )
        return self


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


class PlanSelectionMessage(_DialogueEnvelope):
    message_type: Literal["PLAN_SELECTION"]
    sender: Literal["veyra"]
    in_reply_to: BoundedId
    payload: PlanSelectionPayload

    @model_validator(mode="after")
    def _collaboration_is_required(self) -> "PlanSelectionMessage":
        if self.collaboration_binding is None:
            raise ValueError(
                "PLAN_SELECTION requires collaboration_binding"
            )
        return self


DialogueMessage: TypeAlias = Annotated[
    TaskRequestMessage
    | ContextPatchMessage
    | EvidenceRequestMessage
    | ChallengeMessage
    | OptionSetMessage
    | PlanSelectionMessage,
    Field(discriminator="message_type"),
]
_DIALOGUE_ADAPTER = TypeAdapter(DialogueMessage)


def dump_dialogue_message(message: DialogueMessage) -> dict[str, Any]:
    payload = message.model_dump(mode="json")
    if payload.get("collaboration_binding") is None:
        payload.pop("collaboration_binding", None)
    if payload.get("message_type") == DialogueType.EVIDENCE_REQUEST.value:
        requested = payload.get("payload", {}).get(
            "requested_evidence",
            [],
        )
        for item in requested:
            if isinstance(item, dict) and item.get("request_id") is None:
                item.pop("request_id", None)
    return payload


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
    collaboration_binding: (
        dict[str, Any] | CollaborationBinding | None
    ) = None,
    in_reply_to: str | None = None,
) -> dict[str, Any]:
    """Build an initial or explicitly parent-bound Veyra task request.

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
            "collaboration_binding": (
                parse_collaboration_binding(collaboration_binding)
                if collaboration_binding is not None
                else None
            ),
            "sender": "veyra",
            "in_reply_to": in_reply_to,
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
    return dump_dialogue_message(message)


def build_context_patch(
    *,
    case_id: str,
    case_revision: int,
    turn_index: int,
    message_id: str,
    task_packet_id: str,
    operation_id: str,
    scope_digest: str,
    in_reply_to: str,
    collaboration_binding: dict[str, Any] | CollaborationBinding,
    resolved_request_ids: list[str] | None = None,
    evidence_refs: list[str] | None = None,
    unresolved_request_ids: list[str] | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    selected_context = dict(context or {})
    message = ContextPatchMessage.model_validate(
        {
            "contract_version": DIALOGUE_CONTRACT_VERSION,
            "message_id": message_id,
            "message_type": DialogueType.CONTEXT_PATCH.value,
            "case_id": case_id,
            "case_revision": case_revision,
            "turn_index": turn_index,
            "task_packet_id": task_packet_id,
            "operation_id": operation_id,
            "scope_digest": scope_digest,
            "collaboration_binding": parse_collaboration_binding(
                collaboration_binding
            ),
            "sender": "veyra",
            "in_reply_to": in_reply_to,
            "payload": {
                "resolved_request_ids": list(
                    resolved_request_ids or []
                ),
                "evidence_refs": list(evidence_refs or []),
                "unresolved_request_ids": list(
                    unresolved_request_ids or []
                ),
                "context": selected_context,
                "context_digest": hashlib.sha256(
                    _canonical_json(selected_context)
                ).hexdigest(),
                "authority": {
                    "mode": "read_only",
                    "side_effects_require_governance": True,
                    "capability_expansion_authorized": False,
                    "verification_authority": False,
                },
            },
        },
        strict=True,
    )
    return dump_dialogue_message(message)


def build_plan_selection(
    *,
    case_id: str,
    case_revision: int,
    turn_index: int,
    message_id: str,
    task_packet_id: str,
    operation_id: str,
    scope_digest: str,
    in_reply_to: str,
    collaboration_binding: dict[str, Any] | CollaborationBinding,
    option_set_message_id: str,
    selected_option_id: str,
    decision_reason: str,
    decision_evidence_refs: list[str] | None = None,
    rejected_option_ids: list[str] | None = None,
) -> dict[str, Any]:
    message = PlanSelectionMessage.model_validate(
        {
            "contract_version": DIALOGUE_CONTRACT_VERSION,
            "message_id": message_id,
            "message_type": DialogueType.PLAN_SELECTION.value,
            "case_id": case_id,
            "case_revision": case_revision,
            "turn_index": turn_index,
            "task_packet_id": task_packet_id,
            "operation_id": operation_id,
            "scope_digest": scope_digest,
            "collaboration_binding": parse_collaboration_binding(
                collaboration_binding
            ),
            "sender": "veyra",
            "in_reply_to": in_reply_to,
            "payload": {
                "option_set_message_id": option_set_message_id,
                "selected_option_id": selected_option_id,
                "decision_reason": decision_reason,
                "decision_evidence_refs": list(
                    decision_evidence_refs or []
                ),
                "rejected_option_ids": list(
                    rejected_option_ids or []
                ),
                "execution_authorized": False,
                "next_action": "await_human_or_close",
            },
        },
        strict=True,
    )
    return dump_dialogue_message(message)


def build_collaboration_binding(
    *,
    participant_id: str,
    role: Literal["primary_analyst", "critic"],
    parent_participant_id: str | None,
    handoff_index: int,
    capability_scope: list[str] | None,
    evidence_scope: list[str] | None,
    provider: dict[str, Any] | CollaborationProviderBinding,
    budget: dict[str, Any] | CollaborationBudget,
    issued_at: datetime,
    expires_at: datetime,
    privacy: (
        dict[str, Any] | CollaborationPrivacyScope | None
    ) = None,
    effect: (
        dict[str, Any] | CollaborationEffectScope | None
    ) = None,
) -> dict[str, Any]:
    payload = {
        "schema_version": "veyra.agent_collaboration_binding.v1",
        "participant_id": participant_id,
        "role": role,
        "parent_participant_id": parent_participant_id,
        "handoff_index": handoff_index,
        "capability_scope": sorted(set(capability_scope or [])),
        "evidence_scope": sorted(set(evidence_scope or [])),
        "privacy": (
            privacy.model_dump(mode="json")
            if isinstance(privacy, CollaborationPrivacyScope)
            else privacy
            or CollaborationPrivacyScope().model_dump(mode="json")
        ),
        "effect": (
            effect.model_dump(mode="json")
            if isinstance(effect, CollaborationEffectScope)
            else effect
            or CollaborationEffectScope().model_dump(mode="json")
        ),
        "provider": (
            provider.model_dump(mode="json")
            if isinstance(provider, CollaborationProviderBinding)
            else CollaborationProviderBinding.model_validate(
                provider,
                strict=True,
            ).model_dump(mode="json")
        ),
        "budget": (
            budget.model_dump(mode="json")
            if isinstance(budget, CollaborationBudget)
            else CollaborationBudget.model_validate(
                budget,
                strict=True,
            ).model_dump(mode="json")
        ),
        "issued_at": issued_at,
        "expires_at": expires_at,
    }
    json_payload = CollaborationBinding.model_validate(
        {
            **payload,
            "collaboration_scope_digest": collaboration_scope_digest(payload),
        },
        strict=True,
    )
    return json_payload.model_dump(mode="json")


def collaboration_turn_transport_identity(
    raw: Any,
) -> dict[str, str]:
    """Derive the only transport sessions allowed for a bound Veyra turn."""

    parsed = parse_dialogue_message(
        raw,
        expected_sender="veyra",
        allowed_types={
            DialogueType.TASK_REQUEST,
            DialogueType.CONTEXT_PATCH,
            DialogueType.PLAN_SELECTION,
        },
    )
    binding = parsed.collaboration_binding
    if binding is None:
        raise DialogueContractError(
            ["collaboration_binding is required for transport identity"]
        )
    canonical_dialogue = dump_dialogue_message(parsed)
    digest = hashlib.sha256(
        _canonical_json(
            {
                "namespace": "veyra.phase6.read_only_turn.v1",
                "dialogue": canonical_dialogue,
            }
        )
    ).hexdigest()
    return {
        "digest": digest,
        "packet_session_id": f"p6dialogue_{digest[:32]}",
        "agent_execution_session_id": f"p6session_{digest[:32]}",
    }


def expected_agent_reply_message_id(raw: Any) -> str:
    """Preallocate the only inbound message id for one Veyra turn."""

    parsed = parse_dialogue_message(
        raw,
        expected_sender="veyra",
        allowed_types={
            DialogueType.TASK_REQUEST,
            DialogueType.CONTEXT_PATCH,
        },
    )
    canonical_dialogue = dump_dialogue_message(parsed)
    digest = hashlib.sha256(
        _canonical_json(
            {
                "namespace": "veyra.agent_reply_message.v1",
                "dialogue": canonical_dialogue,
            }
        )
    ).hexdigest()
    return f"p6reply_{digest[:32]}"


def parse_dialogue_message(
    raw: Any,
    *,
    expected_message_id: str | None = None,
    expected_case_id: str | None = None,
    expected_case_revision: int | None = None,
    expected_turn_index: int | None = None,
    expected_in_reply_to: str | None = None,
    expected_sender: Literal["veyra", "agent"] | None = None,
    expected_task_packet_id: str | None = None,
    expected_operation_id: str | None = None,
    expected_scope_digest: str | None = None,
    expected_collaboration_binding: (
        dict[str, Any] | CollaborationBinding | None
    ) = None,
    require_collaboration_binding: bool = False,
    allowed_types: set[DialogueType | str] | None = None,
) -> DialogueMessage:
    """Parse one exact message and enforce its Case-turn binding.

    Validation never derives a message from prose and never treats Agent
    fields as authority, evidence, or a verified outcome.
    """

    try:
        if isinstance(raw, (dict, list)):
            message = _DIALOGUE_ADAPTER.validate_json(
                json.dumps(
                    _jsonable(raw),
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                ),
                strict=True,
            )
        else:
            message = _DIALOGUE_ADAPTER.validate_python(
                raw,
                strict=True,
            )
    except (ValidationError, TypeError, ValueError) as exc:
        if not isinstance(exc, ValidationError):
            raise DialogueContractError(
                [f"dialogue message is not strict JSON: {exc}"]
            ) from exc
        raise DialogueContractError(_validation_errors(exc)) from exc

    errors: list[str] = []
    _expect(errors, "message_id", message.message_id, expected_message_id)
    _expect(errors, "case_id", message.case_id, expected_case_id)
    _expect(errors, "case_revision", message.case_revision, expected_case_revision)
    _expect(errors, "turn_index", message.turn_index, expected_turn_index)
    _expect(errors, "in_reply_to", message.in_reply_to, expected_in_reply_to)
    _expect(errors, "sender", message.sender, expected_sender)
    _expect(errors, "task_packet_id", message.task_packet_id, expected_task_packet_id)
    _expect(errors, "operation_id", message.operation_id, expected_operation_id)
    _expect(errors, "scope_digest", message.scope_digest, expected_scope_digest)
    if require_collaboration_binding and message.collaboration_binding is None:
        errors.append("collaboration_binding missing")
    if expected_collaboration_binding is not None:
        try:
            expected_binding = parse_collaboration_binding(
                expected_collaboration_binding
            )
        except (ValidationError, ValueError) as exc:
            raise DialogueContractError(
                [f"expected collaboration binding invalid: {exc}"]
            ) from exc
        actual_binding = message.collaboration_binding
        if actual_binding is None:
            errors.append("collaboration_binding mismatch")
        elif actual_binding.model_dump(mode="json") != (
            expected_binding.model_dump(mode="json")
        ):
            errors.append("collaboration_binding mismatch")
        elif isinstance(raw, dict):
            expected_wire = (
                expected_collaboration_binding.model_dump(mode="json")
                if isinstance(
                    expected_collaboration_binding,
                    CollaborationBinding,
                )
                else _jsonable(expected_collaboration_binding)
            )
            if raw.get("collaboration_binding") != expected_wire:
                errors.append(
                    "collaboration_binding exact envelope mismatch"
                )

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
    expected_message_id: str | None = None,
    expected_case_id: str,
    expected_case_revision: int,
    expected_turn_index: int,
    expected_in_reply_to: str,
    expected_task_packet_id: str,
    expected_operation_id: str,
    expected_scope_digest: str,
    expected_collaboration_binding: (
        dict[str, Any] | CollaborationBinding | None
    ) = None,
) -> dict[str, Any] | None:
    """Read only the explicit Agent envelope; free text is never promoted."""

    if not isinstance(agent_response, dict) or "dialogue_message" not in agent_response:
        return None
    if (
        expected_collaboration_binding is not None
        and set(agent_response) != {"dialogue_message"}
    ):
        raise DialogueContractError(
            [
                "bound Agent response must contain only the "
                "dialogue_message top-level field"
            ]
        )
    message = parse_dialogue_message(
        agent_response["dialogue_message"],
        expected_message_id=expected_message_id,
        expected_sender="agent",
        expected_case_id=expected_case_id,
        expected_case_revision=expected_case_revision,
        expected_turn_index=expected_turn_index,
        expected_in_reply_to=expected_in_reply_to,
        expected_task_packet_id=expected_task_packet_id,
        expected_operation_id=expected_operation_id,
        expected_scope_digest=expected_scope_digest,
        expected_collaboration_binding=expected_collaboration_binding,
        allowed_types={
            DialogueType.EVIDENCE_REQUEST,
            DialogueType.CHALLENGE,
            DialogueType.OPTION_SET,
        },
    )
    if expected_collaboration_binding is not None:
        scope_errors = validate_agent_reply_scope(
            message,
            parent=expected_collaboration_binding,
        )
        if scope_errors:
            raise DialogueContractError(scope_errors)
    return dump_dialogue_message(message)


def parse_collaboration_binding(
    raw: Any,
) -> CollaborationBinding:
    if isinstance(raw, CollaborationBinding):
        return raw
    if isinstance(raw, dict):
        return CollaborationBinding.model_validate_json(
            json.dumps(
                _jsonable(raw),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ),
            strict=True,
        )
    return CollaborationBinding.model_validate(raw, strict=True)


def validate_collaboration_child(
    *,
    parent: dict[str, Any] | CollaborationBinding,
    child: dict[str, Any] | CollaborationBinding,
    outbound_type: DialogueType | str,
    provided_evidence_refs: list[str] | None = None,
) -> list[str]:
    """Return exact scope-narrowing errors for one Veyra-authored child turn."""

    try:
        parent_binding = parse_collaboration_binding(parent)
        child_binding = parse_collaboration_binding(child)
        message_type = (
            outbound_type
            if isinstance(outbound_type, DialogueType)
            else DialogueType(outbound_type)
        )
    except (ValidationError, ValueError) as exc:
        return [f"collaboration binding invalid: {exc}"]

    errors: list[str] = []
    same_participant = (
        child_binding.participant_id
        == parent_binding.participant_id
    )
    if same_participant:
        if message_type == DialogueType.TASK_REQUEST:
            errors.append(
                "parent-bound TASK_REQUEST must hand off to a distinct "
                "participant"
            )
        if child_binding.role != parent_binding.role:
            errors.append("participant role cannot change")
        if (
            child_binding.parent_participant_id
            != parent_binding.parent_participant_id
        ):
            errors.append("participant parent binding cannot change")
        if child_binding.handoff_index != parent_binding.handoff_index:
            errors.append("same participant cannot change handoff_index")
    else:
        if message_type != DialogueType.TASK_REQUEST:
            errors.append("only TASK_REQUEST may hand off to a child participant")
        if parent_binding.role != "primary_analyst":
            errors.append("only the primary analyst may hand off")
        if child_binding.role != "critic":
            errors.append("handoff participant must use critic role")
        if (
            child_binding.parent_participant_id
            != parent_binding.participant_id
        ):
            errors.append("child participant must bind the exact parent")
        if (
            child_binding.handoff_index
            != parent_binding.handoff_index + 1
        ):
            errors.append("handoff_index must advance exactly once")
        if parent_binding.budget.remaining_handoffs < 1:
            errors.append("parent handoff budget is exhausted")
        elif (
            child_binding.budget.remaining_handoffs
            > parent_binding.budget.remaining_handoffs - 1
        ):
            errors.append("child handoff budget did not narrow")

    if (
        child_binding.provider.model_dump(mode="json")
        != parent_binding.provider.model_dump(mode="json")
    ):
        errors.append("provider binding cannot change")
    if (
        child_binding.privacy.model_dump(mode="json")
        != parent_binding.privacy.model_dump(mode="json")
    ):
        errors.append("privacy scope cannot change")
    if (
        child_binding.effect.model_dump(mode="json")
        != parent_binding.effect.model_dump(mode="json")
    ):
        errors.append("effect scope cannot change")
    if not set(child_binding.capability_scope).issubset(
        set(parent_binding.capability_scope)
    ):
        errors.append("capability scope expanded")

    supplied_evidence = set(provided_evidence_refs or [])
    allowed_evidence = set(parent_binding.evidence_scope)
    if message_type == DialogueType.CONTEXT_PATCH:
        if not same_participant:
            errors.append("CONTEXT_PATCH cannot change participant")
        allowed_evidence.update(supplied_evidence)
        if not supplied_evidence.issubset(
            set(child_binding.evidence_scope)
        ):
            errors.append(
                "CONTEXT_PATCH omitted supplied evidence"
            )
        if parent_binding.budget.remaining_evidence_patches < 1:
            errors.append("parent evidence-patch budget is exhausted")
        elif (
            child_binding.budget.remaining_evidence_patches
            > parent_binding.budget.remaining_evidence_patches - 1
        ):
            errors.append("child evidence-patch budget did not narrow")
    elif (
        child_binding.budget.remaining_evidence_patches
        > parent_binding.budget.remaining_evidence_patches
    ):
        errors.append("child evidence-patch budget expanded")
    if not set(child_binding.evidence_scope).issubset(allowed_evidence):
        errors.append("evidence scope expanded")

    outbound_consumes_call = message_type in {
        DialogueType.TASK_REQUEST,
        DialogueType.CONTEXT_PATCH,
        DialogueType.PLAN_SELECTION,
    }
    if outbound_consumes_call:
        if parent_binding.budget.remaining_agent_calls < 1:
            errors.append("parent Agent-call budget is exhausted")
        elif (
            child_binding.budget.remaining_agent_calls
            > parent_binding.budget.remaining_agent_calls - 1
        ):
            errors.append("child Agent-call budget did not narrow")
    elif (
        child_binding.budget.remaining_agent_calls
        > parent_binding.budget.remaining_agent_calls
    ):
        errors.append("child Agent-call budget expanded")

    for field_name in (
        "max_wall_time_seconds",
        "max_context_bytes",
        "max_output_bytes",
    ):
        if getattr(child_binding.budget, field_name) > getattr(
            parent_binding.budget,
            field_name,
        ):
            errors.append(f"child {field_name} budget expanded")
    if child_binding.issued_at < parent_binding.issued_at:
        errors.append("child issued_at predates parent")
    if child_binding.expires_at > parent_binding.expires_at:
        errors.append("child expiry exceeds parent")
    return sorted(set(errors))


def validate_agent_reply_scope(
    raw: Any,
    *,
    parent: dict[str, Any] | CollaborationBinding,
) -> list[str]:
    """Validate that an Agent proposal references only its exact scope."""

    try:
        message = (
            raw
            if isinstance(
                raw,
                (
                    EvidenceRequestMessage,
                    ChallengeMessage,
                    OptionSetMessage,
                ),
            )
            else parse_dialogue_message(
                raw,
                expected_sender="agent",
                allowed_types={
                    DialogueType.EVIDENCE_REQUEST,
                    DialogueType.CHALLENGE,
                    DialogueType.OPTION_SET,
                },
            )
        )
        parent_binding = parse_collaboration_binding(parent)
    except (DialogueContractError, ValidationError, ValueError) as exc:
        return [f"Agent collaboration reply scope invalid: {exc}"]

    requested_capabilities: set[str] = set()
    referenced_evidence: set[str] = set()
    errors: list[str] = []
    if message.message_type == DialogueType.EVIDENCE_REQUEST.value:
        requested_capabilities.update(
            message.payload.requested_capabilities
        )
        if any(
            item.request_id is None
            for item in message.payload.requested_evidence
        ):
            errors.append(
                "bound EVIDENCE_REQUEST requires request_id for every "
                "evidence need"
            )
        referenced_evidence.update(
            item.claim_ref
            for item in message.payload.requested_evidence
            if item.claim_ref is not None
        )
    elif message.message_type == DialogueType.CHALLENGE.value:
        requested_capabilities.update(
            message.payload.requested_capabilities
        )
        referenced_evidence.update(
            message.payload.challenged_claim_refs
        )
        referenced_evidence.update(message.payload.evidence_refs)
    elif message.message_type == DialogueType.OPTION_SET.value:
        for option in message.payload.options:
            requested_capabilities.update(
                option.required_capabilities
            )
            referenced_evidence.update(option.evidence_refs)

    if not requested_capabilities.issubset(
        set(parent_binding.capability_scope)
    ):
        errors.append(
            "Agent reply capability request exceeds its exact parent scope"
        )
    if not referenced_evidence.issubset(
        set(parent_binding.evidence_scope)
    ):
        errors.append(
            "Agent reply evidence reference exceeds its exact parent scope"
        )
    return sorted(set(errors))


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
        "collaboration": {
            "binding_schema": "veyra.agent_collaboration_binding.v1",
            "legacy_binding_optional": True,
            "phase6_outbound_binding_required": [
                DialogueType.CONTEXT_PATCH.value,
                DialogueType.PLAN_SELECTION.value,
            ],
            "agent_reply_must_echo_parent_binding": True,
            "scope_fields": [
                "participant_id",
                "role",
                "parent_participant_id",
                "handoff_index",
                "capability_scope",
                "evidence_scope",
                "privacy",
                "effect",
                "provider",
                "budget",
                "issued_at",
                "expires_at",
            ],
            "execution_authorized": False,
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


def collaboration_scope_digest(
    value: dict[str, Any] | CollaborationBinding,
) -> str:
    """Return the canonical digest for one strict collaboration binding.

    The digest field itself is excluded, so callers may pass either an
    unsigned builder payload or a fully validated binding.
    """

    if isinstance(value, CollaborationBinding):
        payload = value.model_dump(
            mode="json",
            exclude={"collaboration_scope_digest"},
        )
    elif isinstance(value, dict):
        payload = dict(value)
        payload.pop("collaboration_scope_digest", None)
        payload.setdefault(
            "schema_version",
            "veyra.agent_collaboration_binding.v1",
        )
        payload.setdefault("parent_participant_id", None)
        payload.setdefault("capability_scope", [])
        payload.setdefault("evidence_scope", [])
        privacy = payload.get("privacy")
        if privacy is None:
            payload["privacy"] = CollaborationPrivacyScope().model_dump(
                mode="json"
            )
        else:
            payload["privacy"] = (
                privacy.model_dump(mode="json")
                if isinstance(privacy, CollaborationPrivacyScope)
                else CollaborationPrivacyScope.model_validate(
                    privacy,
                    strict=True,
                ).model_dump(mode="json")
            )
        effect = payload.get("effect")
        if effect is None:
            payload["effect"] = CollaborationEffectScope().model_dump(
                mode="json"
            )
        else:
            payload["effect"] = (
                effect.model_dump(mode="json")
                if isinstance(effect, CollaborationEffectScope)
                else CollaborationEffectScope.model_validate(
                    effect,
                    strict=True,
                ).model_dump(mode="json")
            )
        provider = payload.get("provider")
        if provider is not None:
            payload["provider"] = (
                provider.model_dump(mode="json")
                if isinstance(provider, CollaborationProviderBinding)
                else CollaborationProviderBinding.model_validate(
                    provider,
                    strict=True,
                ).model_dump(mode="json")
            )
        budget = payload.get("budget")
        if budget is not None:
            payload["budget"] = (
                budget.model_dump(mode="json")
                if isinstance(budget, CollaborationBudget)
                else CollaborationBudget.model_validate(
                    budget,
                    strict=True,
                ).model_dump(mode="json")
            )
        for field_name in ("issued_at", "expires_at"):
            timestamp = payload.get(field_name)
            if isinstance(timestamp, str):
                try:
                    parsed = datetime.fromisoformat(
                        timestamp.replace("Z", "+00:00")
                    )
                except ValueError as exc:
                    raise ValueError(
                        f"{field_name} must be an ISO-8601 timestamp"
                    ) from exc
                if parsed.tzinfo is None or parsed.utcoffset() is None:
                    raise ValueError(
                        f"{field_name} must be timezone-aware"
                    )
                payload[field_name] = parsed
    else:
        raise TypeError("collaboration binding must be an object")
    return _collaboration_digest(payload)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("collaboration timestamps must be timezone-aware")
        return (
            value.astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    if isinstance(value, dict):
        return {
            str(key): _jsonable(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _collaboration_digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(_jsonable(value))).hexdigest()


def _expect(errors: list[str], field: str, actual: Any, expected: Any) -> None:
    if expected is not None and actual != expected:
        errors.append(f"{field} mismatch")


def _validation_errors(exc: ValidationError) -> list[str]:
    return [
        f"{'.'.join(str(item) for item in error.get('loc', ()))}: {error.get('msg', 'invalid')}"
        for error in exc.errors(include_url=False)
    ]
