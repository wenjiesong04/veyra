from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime, timezone
from typing import Any, Callable, Literal, TypeAlias

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    field_validator,
    model_validator,
)

from core.world_state import STATE_FILE_LAYOUT, WorldStateStore


STATE_FILE = "state_change_proposals.json"
DOCUMENT_SCHEMA_VERSION = "veyra.state_change_proposals.v1"
PROPOSAL_SCHEMA_VERSION = "veyra.state_change_proposal.v1"
RESULT_SCHEMA_VERSION = "veyra.state_change_result.v1"
APPROVAL_SCHEMA_VERSION = "veyra.state_change_approval.v1"

StateEffect: TypeAlias = Literal[
    "memory.write",
    "profile.write",
    "commitment.mutate",
    "proactive.create",
]
ProposalExplicitness: TypeAlias = Literal[
    "explicit",
    "strong_implied",
    "weak_implied",
    "inferred",
    "unknown",
]
ApprovalStatus: TypeAlias = Literal["not_required", "pending", "approved"]
ProposalStatus: TypeAlias = Literal[
    "pending",
    "committing",
    "committed",
    "failed",
    "expired",
    "stale_revision",
    "indeterminate",
]
CommitStatus: TypeAlias = Literal[
    "committed",
    "failed",
    "approval_required",
    "expired",
    "stale_revision",
    "in_progress",
    "indeterminate",
]
StateChangeHandler: TypeAlias = Callable[["StateChangeProposal"], JsonValue]

_JSON_VALUE_ADAPTER = TypeAdapter(JsonValue)
_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_STATE_FILE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*\.json$")
_MAX_PAYLOAD_BYTES = 64 * 1024


class StateProposalError(RuntimeError):
    """Base error for state-change proposal coordination."""


class ProposalNotFoundError(StateProposalError):
    """Raised when a requested proposal does not exist."""


class ProposalConflictError(StateProposalError):
    """Raised when an idempotency key is reused for a different request."""


class ProposalStorageError(StateProposalError):
    """Raised when the durable proposal registry cannot be validated."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        strict=True,
        extra="forbid",
        frozen=True,
        validate_assignment=True,
        populate_by_name=True,
    )


class StateChangeApproval(_StrictModel):
    schema_version: Literal["veyra.state_change_approval.v1"] = APPROVAL_SCHEMA_VERSION
    proposal_id: str = Field(min_length=8, max_length=80)
    payload_digest: str
    approved_by: str = Field(min_length=1, max_length=240)
    approved_at: AwareDatetime

    @field_validator("payload_digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if not _DIGEST_PATTERN.fullmatch(value):
            raise ValueError("payload_digest must be a lowercase SHA-256 digest")
        return value


class StateChangeCommitResult(_StrictModel):
    schema_version: Literal["veyra.state_change_result.v1"] = RESULT_SCHEMA_VERSION
    proposal_id: str = Field(min_length=8, max_length=80)
    effect: StateEffect
    status: CommitStatus
    applied: bool | None
    replayed: bool = False
    output: JsonValue | None = None
    error: str | None = Field(default=None, max_length=1200)
    expected_state_revisions: dict[str, int] = Field(default_factory=dict, max_length=16)
    observed_state_revisions: dict[str, int] = Field(default_factory=dict, max_length=16)
    final_state_revisions: dict[str, int] = Field(default_factory=dict, max_length=16)
    started_at: AwareDatetime | None = None
    completed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> "StateChangeCommitResult":
        if self.status == "committed" and self.applied is not True:
            raise ValueError("committed results must report applied=true")
        if self.status == "indeterminate" and self.applied is not None:
            raise ValueError("indeterminate results must report applied=null")
        if self.status not in {"committed", "indeterminate"} and self.applied is not False:
            raise ValueError("non-committed precondition results must report applied=false")
        if self.status == "in_progress":
            if self.started_at is None or self.completed_at is not None:
                raise ValueError("in-progress results need started_at and no completed_at")
        elif self.completed_at is None:
            raise ValueError("terminal and precondition results need completed_at")
        if self.status != "committed" and not self.error:
            raise ValueError("non-committed results must explain why no change was applied")
        return self


class StateChangeTarget(_StrictModel):
    type: str = Field(default="unknown", min_length=1, max_length=120)
    value: str = Field(default="", max_length=1000)
    scope: str = Field(default="current_user", min_length=1, max_length=160)
    attributes: dict[str, JsonValue] = Field(default_factory=dict)


class StateChangeSourceSpan(_StrictModel):
    text: str = Field(min_length=1, max_length=800)
    start: int = Field(ge=0)
    end: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_range(self) -> "StateChangeSourceSpan":
        if self.end <= self.start:
            raise ValueError("source span end must be greater than start")
        return self


class StateChangeProposal(_StrictModel):
    schema_version: Literal["veyra.state_change_proposal.v1"] = PROPOSAL_SCHEMA_VERSION
    proposal_id: str = Field(min_length=8, max_length=80)
    idempotency_key: str = Field(min_length=1, max_length=320)
    payload_digest: str
    effect: StateEffect
    operation: str = Field(default="unknown", min_length=1, max_length=200)
    target: StateChangeTarget = Field(default_factory=StateChangeTarget)
    source_span: StateChangeSourceSpan | None = None
    explicitness: ProposalExplicitness = "unknown"
    preconditions: list[str] = Field(default_factory=list, max_length=24)
    payload: dict[str, JsonValue]
    expected_state_revisions: dict[str, int] = Field(default_factory=dict, max_length=16)
    requires_approval: bool = True
    approval_status: ApprovalStatus = "pending"
    status: ProposalStatus = "pending"
    created_at: AwareDatetime
    expires_at: AwareDatetime | None = None
    approval: StateChangeApproval | None = None
    claim_token: str | None = Field(default=None, min_length=32, max_length=64)
    commit_started_at: AwareDatetime | None = None
    commit_result: StateChangeCommitResult | None = None

    @field_validator("idempotency_key")
    @classmethod
    def validate_idempotency_key(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("idempotency_key cannot have leading or trailing whitespace")
        return value

    @field_validator("payload_digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if not _DIGEST_PATTERN.fullmatch(value):
            raise ValueError("payload_digest must be a lowercase SHA-256 digest")
        return value

    @field_validator("payload")
    @classmethod
    def validate_payload_size(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        if len(_canonical_json(value).encode("utf-8")) > _MAX_PAYLOAD_BYTES:
            raise ValueError(f"proposal payload exceeds {_MAX_PAYLOAD_BYTES} bytes")
        return value

    @model_validator(mode="after")
    def validate_effect_payload(self) -> "StateChangeProposal":
        required_any = {
            "memory.write": {"patch"},
            "profile.write": {"signals", "profile"},
            "commitment.mutate": {"semantic_context", "event", "operation"},
            "proactive.create": {"semantic_context", "event", "intent", "kind"},
        }[self.effect]
        if not required_any.intersection(self.payload):
            raise ValueError(
                f"{self.effect} payload must contain one of {sorted(required_any)}"
            )
        return self

    @field_validator("expected_state_revisions")
    @classmethod
    def validate_expected_revisions(cls, value: dict[str, int]) -> dict[str, int]:
        for name, revision in value.items():
            if not _STATE_FILE_PATTERN.fullmatch(name) or "/" in name or "\\" in name or ".." in name:
                raise ValueError(f"invalid state filename: {name!r}")
            if name not in STATE_FILE_LAYOUT or not name.endswith(".json"):
                raise ValueError(f"expected revision target is not a registered JSON state: {name!r}")
            if name == STATE_FILE:
                raise ValueError("a proposal cannot precondition its own registry revision")
            if revision < 0:
                raise ValueError("expected state revisions cannot be negative")
        return value

    @model_validator(mode="after")
    def validate_integrity(self) -> "StateChangeProposal":
        if self.proposal_id != deterministic_proposal_id(self.idempotency_key):
            raise ValueError("proposal_id does not match idempotency_key")
        if self.payload_digest != state_change_payload_digest(self.effect, self.payload):
            raise ValueError("payload_digest does not match effect and payload")
        if self.expires_at is not None and self.expires_at <= self.created_at:
            raise ValueError("expires_at must be later than created_at")
        if self.approval is not None:
            if self.approval.proposal_id != self.proposal_id:
                raise ValueError("approval is bound to a different proposal")
            if self.approval.payload_digest != self.payload_digest:
                raise ValueError("approval is bound to a different payload")
        if self.requires_approval:
            expected_approval_status = "approved" if self.approval is not None else "pending"
            if self.approval_status != expected_approval_status:
                raise ValueError("approval_status does not match the bound approval")
        elif self.approval_status != "not_required":
            raise ValueError("approval_status must be not_required when approval is not required")
        if self.status == "pending":
            if self.commit_started_at is not None:
                raise ValueError("pending proposal cannot have commit_started_at")
            if self.commit_result is not None and self.commit_result.status != "approval_required":
                raise ValueError("pending proposal may only retain an approval_required result")
        if self.status == "committing":
            if self.commit_started_at is None:
                raise ValueError("committing proposal requires commit_started_at")
            if self.commit_result is None or self.commit_result.status != "in_progress":
                raise ValueError("committing proposal requires an in-progress result")
        if self.status in {"committed", "failed", "expired", "stale_revision", "indeterminate"}:
            if self.commit_result is None or self.commit_result.status != self.status:
                raise ValueError("terminal proposal status must match its commit result")
        if self.commit_result is not None:
            if self.commit_result.proposal_id != self.proposal_id:
                raise ValueError("commit result is bound to a different proposal")
            if self.commit_result.effect != self.effect:
                raise ValueError("commit result effect does not match proposal")
        return self


class _ProposalDocument(_StrictModel):
    schema_version: Literal["veyra.state_change_proposals.v1"] = DOCUMENT_SCHEMA_VERSION
    proposals: dict[str, StateChangeProposal] = Field(default_factory=dict)
    state_revision: int = Field(default=0, ge=0, alias="_state_revision")
    source: Literal["state_proposal_coordinator"] = "state_proposal_coordinator"
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    ttl_seconds: Literal[0] = 0
    status: Literal["fresh"] = "fresh"
    updated_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_index(self) -> "_ProposalDocument":
        for proposal_id, proposal in self.proposals.items():
            if proposal_id != proposal.proposal_id:
                raise ValueError("proposal registry key does not match proposal_id")
        return self


def deterministic_idempotency_key(*parts: str, namespace: str = "veyra.state-change.v1") -> str:
    """Derive a stable key from caller-owned request identity parts.

    Callers should include a durable event/message id and semantic act id.  The
    payload is intentionally not part of this key so reuse with changed payload
    is detected as a conflict rather than silently creating a new proposal.
    """

    normalized = [namespace.strip(), *(part.strip() for part in parts)]
    if not normalized[0] or len(normalized) == 1 or any(not part for part in normalized[1:]):
        raise ValueError("namespace and all idempotency identity parts must be non-empty")
    digest = hashlib.sha256(_canonical_json(normalized).encode("utf-8")).hexdigest()
    return f"scik_{digest}"


def deterministic_proposal_id(idempotency_key: str) -> str:
    key = idempotency_key.strip()
    if not key or key != idempotency_key:
        raise ValueError("idempotency_key must be non-empty and normalized")
    digest = hashlib.sha256(f"veyra.state-change-proposal.v1:{key}".encode("utf-8")).hexdigest()
    return f"scp_{digest[:32]}"


def state_change_payload_digest(effect: StateEffect, payload: dict[str, JsonValue]) -> str:
    encoded = _canonical_json({"effect": effect, "payload": payload}).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class StateChangeProposalStore:
    """Durable, idempotent coordinator for Veyra-owned state effects.

    A proposal is claimed before the handler runs.  Once claimed, later callers
    observe ``in_progress`` or the recorded result and never invoke the handler
    again.  A process crash after the claim intentionally leaves the proposal in
    ``committing`` for explicit reconciliation; automatically retrying would
    violate at-most-once handling.
    """

    def __init__(
        self,
        store: WorldStateStore,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def create_or_get(
        self,
        *,
        effect: StateEffect,
        payload: dict[str, JsonValue],
        idempotency_key: str,
        operation: str = "unknown",
        target: StateChangeTarget | dict[str, JsonValue] | None = None,
        source_span: StateChangeSourceSpan | dict[str, JsonValue] | None = None,
        explicitness: ProposalExplicitness = "unknown",
        preconditions: list[str] | None = None,
        expected_state_revisions: dict[str, int] | None = None,
        requires_approval: bool = True,
        expires_at: datetime | None = None,
    ) -> StateChangeProposal:
        now = self._now()
        digest = state_change_payload_digest(effect, payload)
        candidate = StateChangeProposal.model_validate(
            {
                "proposal_id": deterministic_proposal_id(idempotency_key),
                "idempotency_key": idempotency_key,
                "payload_digest": digest,
                "effect": effect,
                "operation": operation,
                "target": target or {},
                "source_span": source_span,
                "explicitness": explicitness,
                "preconditions": preconditions or [],
                "payload": payload,
                "expected_state_revisions": expected_state_revisions or {},
                "requires_approval": requires_approval,
                "approval_status": "pending" if requires_approval else "not_required",
                "created_at": now,
                "expires_at": expires_at,
            },
            strict=True,
        )
        selected: dict[str, StateChangeProposal] = {}

        def create(document: _ProposalDocument) -> _ProposalDocument:
            existing = document.proposals.get(candidate.proposal_id)
            if existing is not None:
                if existing.idempotency_key != candidate.idempotency_key:
                    raise ProposalConflictError("deterministic proposal id collision")
                self._assert_same_request(existing, candidate)
                selected["proposal"] = existing
                return document
            proposals = dict(document.proposals)
            proposals[candidate.proposal_id] = candidate
            selected["proposal"] = candidate
            return document.model_copy(update={"proposals": proposals})

        self._mutate_document(create)
        return selected["proposal"]

    def get(self, proposal_id: str) -> StateChangeProposal:
        proposal = self._read_document().proposals.get(proposal_id)
        if proposal is None:
            raise ProposalNotFoundError(f"unknown state-change proposal: {proposal_id}")
        return proposal

    def approval_for(
        self,
        proposal: StateChangeProposal,
        *,
        approved_by: str,
        approved_at: datetime | None = None,
    ) -> StateChangeApproval:
        return StateChangeApproval.model_validate(
            {
                "proposal_id": proposal.proposal_id,
                "payload_digest": proposal.payload_digest,
                "approved_by": approved_by,
                "approved_at": approved_at or self._now(),
            },
            strict=True,
        )

    def commit(
        self,
        proposal_id: str,
        handler: StateChangeHandler,
        *,
        approval: StateChangeApproval | None = None,
    ) -> StateChangeCommitResult:
        claim: dict[str, Any] = {}
        claim_token = secrets.token_hex(16)

        def claim_pending(document: _ProposalDocument) -> _ProposalDocument:
            proposal = document.proposals.get(proposal_id)
            if proposal is None:
                raise ProposalNotFoundError(f"unknown state-change proposal: {proposal_id}")
            if proposal.status == "committing":
                claim["result"] = self._replayed(proposal.commit_result)
                return document
            if proposal.status in {"committed", "failed", "expired", "stale_revision", "indeterminate"}:
                claim["result"] = self._replayed(proposal.commit_result)
                return document

            now = self._now()
            if proposal.expires_at is not None and now >= proposal.expires_at:
                result = self._precondition_result(
                    proposal,
                    status="expired",
                    error="proposal expired before it could be committed",
                    now=now,
                )
                updated = self._updated_proposal(proposal, status="expired", commit_result=result)
                claim["result"] = result
                return self._replace(document, updated)

            approval_error = self._approval_error(proposal, approval, now)
            if approval_error:
                result = self._precondition_result(
                    proposal,
                    status="approval_required",
                    error=approval_error,
                    now=now,
                )
                updated = self._updated_proposal(proposal, commit_result=result)
                claim["result"] = result
                return self._replace(document, updated)

            observed = self._observed_revisions(proposal.expected_state_revisions)
            stale = {
                name: {"expected": expected, "observed": observed.get(name, 0)}
                for name, expected in proposal.expected_state_revisions.items()
                if observed.get(name, 0) != expected
            }
            if stale:
                result = self._precondition_result(
                    proposal,
                    status="stale_revision",
                    error=f"state revision precondition failed: {_canonical_json(stale)}",
                    now=now,
                    observed=observed,
                )
                updated = self._updated_proposal(proposal, status="stale_revision", commit_result=result)
                claim["result"] = result
                return self._replace(document, updated)

            in_progress = StateChangeCommitResult(
                proposal_id=proposal.proposal_id,
                effect=proposal.effect,
                status="in_progress",
                applied=False,
                error="proposal has been claimed and its handler is running",
                expected_state_revisions=proposal.expected_state_revisions,
                observed_state_revisions=observed,
                started_at=now,
            )
            updated = self._updated_proposal(
                proposal,
                status="committing",
                approval=approval or proposal.approval,
                approval_status="approved" if proposal.requires_approval else "not_required",
                claim_token=claim_token,
                commit_started_at=now,
                commit_result=in_progress,
            )
            claim["proposal"] = updated
            return self._replace(document, updated)

        with self.store.writer_transaction():
            # Keep the claim, handler, and final record under one process-wide
            # writer transaction.  The claim is still durably written before the
            # handler runs, so a process crash remains explicitly reconcilable,
            # but another thread cannot reconcile a live claim in the hand-off
            # window between claiming and executing it.
            self._mutate_document(claim_pending)
            if "proposal" not in claim:
                return claim["result"]

            claimed: StateChangeProposal = claim["proposal"]
            current_before = self._read_document().proposals.get(proposal_id)
            if current_before is None:
                raise ProposalStorageError(f"claimed proposal disappeared before handler: {proposal_id}")

            final_result: StateChangeCommitResult
            final_status: ProposalStatus
            if not self._claim_matches(current_before, claimed):
                final_result = self._claim_lost_result(
                    claimed,
                    current=current_before,
                    phase="before handler execution",
                )
                final_status = "indeterminate"
            else:
                observed_again = self._observed_revisions(claimed.expected_state_revisions)
                stale = {
                    name: {"expected": expected, "observed": observed_again.get(name, 0)}
                    for name, expected in claimed.expected_state_revisions.items()
                    if observed_again.get(name, 0) != expected
                }
                if stale:
                    final_result = StateChangeCommitResult(
                        proposal_id=claimed.proposal_id,
                        effect=claimed.effect,
                        status="stale_revision",
                        applied=False,
                        error=f"state revision changed before handler execution: {_canonical_json(stale)}",
                        expected_state_revisions=claimed.expected_state_revisions,
                        observed_state_revisions=observed_again,
                        final_state_revisions=observed_again,
                        started_at=claimed.commit_started_at,
                        completed_at=self._now(),
                    )
                    final_status = "stale_revision"
                else:
                    raw_output: Any = None
                    output: JsonValue | None = None
                    handler_error: Exception | None = None
                    try:
                        raw_output = handler(claimed)
                        output = _validate_json_value(raw_output)
                    except Exception as exc:
                        handler_error = exc

                    current_after = self._read_document().proposals.get(proposal_id)
                    final_revisions = self._observed_revisions(claimed.expected_state_revisions)
                    if current_after is None:
                        raise ProposalStorageError(f"claimed proposal disappeared after handler: {proposal_id}")
                    if not self._claim_matches(current_after, claimed):
                        final_result = self._claim_lost_result(
                            claimed,
                            current=current_after,
                            phase="after handler execution",
                            final_revisions=final_revisions,
                        )
                        final_status = "indeterminate"
                    elif handler_error is not None:
                        final_result = StateChangeCommitResult(
                            proposal_id=claimed.proposal_id,
                            effect=claimed.effect,
                            status="indeterminate",
                            applied=None,
                            error=(
                                "handler outcome requires reconciliation; it may have applied before failing: "
                                f"{_handler_error(handler_error)}"
                            ),
                            expected_state_revisions=claimed.expected_state_revisions,
                            observed_state_revisions=observed_again,
                            final_state_revisions=final_revisions,
                            started_at=claimed.commit_started_at,
                            completed_at=self._now(),
                        )
                        final_status = "indeterminate"
                    else:
                        business_status, business_error = _handler_business_failure(output)
                        if business_status == "indeterminate":
                            final_result = StateChangeCommitResult(
                                proposal_id=claimed.proposal_id,
                                effect=claimed.effect,
                                status="indeterminate",
                                applied=None,
                                output=output,
                                error=business_error,
                                expected_state_revisions=claimed.expected_state_revisions,
                                observed_state_revisions=observed_again,
                                final_state_revisions=final_revisions,
                                started_at=claimed.commit_started_at,
                                completed_at=self._now(),
                            )
                            final_status = "indeterminate"
                        elif business_status == "failed":
                            final_result = StateChangeCommitResult(
                                proposal_id=claimed.proposal_id,
                                effect=claimed.effect,
                                status="failed",
                                applied=False,
                                output=output,
                                error=business_error,
                                expected_state_revisions=claimed.expected_state_revisions,
                                observed_state_revisions=observed_again,
                                final_state_revisions=final_revisions,
                                started_at=claimed.commit_started_at,
                                completed_at=self._now(),
                            )
                            final_status = "failed"
                        else:
                            final_result = StateChangeCommitResult(
                                proposal_id=claimed.proposal_id,
                                effect=claimed.effect,
                                status="committed",
                                applied=True,
                                output=output,
                                expected_state_revisions=claimed.expected_state_revisions,
                                observed_state_revisions=observed_again,
                                final_state_revisions=final_revisions,
                                started_at=claimed.commit_started_at,
                                completed_at=self._now(),
                            )
                            final_status = "committed"

            recorded: dict[str, StateChangeCommitResult] = {}

            def finish(document: _ProposalDocument) -> _ProposalDocument:
                current = document.proposals.get(proposal_id)
                if current is None:
                    raise ProposalStorageError(f"claimed proposal disappeared: {proposal_id}")
                if current.status != "committing":
                    if current.commit_result is None:
                        raise ProposalStorageError("proposal left committing without a result")
                    recorded["result"] = self._replayed(current.commit_result)
                    return document
                if not self._claim_matches(current, claimed):
                    lost_result = self._claim_lost_result(
                        claimed,
                        current=current,
                        phase="while recording handler outcome",
                    )
                    updated = self._updated_proposal(
                        current,
                        status="indeterminate",
                        commit_result=lost_result,
                    )
                    recorded["result"] = lost_result
                    return self._replace(document, updated)
                updated = self._updated_proposal(current, status=final_status, commit_result=final_result)
                recorded["result"] = final_result
                return self._replace(document, updated)

            self._mutate_document(finish)
            return recorded["result"]

    def list_committing(self) -> list[StateChangeProposal]:
        return [
            proposal
            for proposal in self._read_document().proposals.values()
            if proposal.status == "committing"
        ]

    def reconcile_indeterminate(
        self,
        proposal_id: str,
        *,
        reason: str,
        observed_output: JsonValue | None = None,
    ) -> StateChangeCommitResult:
        """Close a stranded claim without rerunning a possibly non-idempotent handler."""

        recorded: dict[str, StateChangeCommitResult] = {}

        def reconcile(document: _ProposalDocument) -> _ProposalDocument:
            proposal = document.proposals.get(proposal_id)
            if proposal is None:
                raise ProposalNotFoundError(f"unknown state-change proposal: {proposal_id}")
            if proposal.status != "committing":
                if proposal.commit_result is None:
                    raise ProposalStorageError("proposal has no result to reconcile")
                recorded["result"] = self._replayed(proposal.commit_result)
                return document
            final_revisions = self._observed_revisions(proposal.expected_state_revisions)
            result = StateChangeCommitResult(
                proposal_id=proposal.proposal_id,
                effect=proposal.effect,
                status="indeterminate",
                applied=None,
                output=observed_output,
                error=f"manual reconciliation required: {reason}"[:1200],
                expected_state_revisions=proposal.expected_state_revisions,
                observed_state_revisions=proposal.commit_result.observed_state_revisions,
                final_state_revisions=final_revisions,
                started_at=proposal.commit_started_at,
                completed_at=self._now(),
            )
            updated = self._updated_proposal(proposal, status="indeterminate", commit_result=result)
            recorded["result"] = result
            return self._replace(document, updated)

        self._mutate_document(reconcile)
        return recorded["result"]

    def _read_document(self) -> _ProposalDocument:
        return _document_from_store(self.store.read_json(STATE_FILE))

    def _mutate_document(
        self,
        mutator: Callable[[_ProposalDocument], _ProposalDocument],
    ) -> _ProposalDocument:
        def mutate(raw: dict[str, Any]) -> dict[str, Any]:
            document = _document_from_store(raw)
            updated = mutator(document)
            if not isinstance(updated, _ProposalDocument):
                raise TypeError("proposal document mutator must return _ProposalDocument")
            return updated.model_dump(mode="json", by_alias=True)

        try:
            stored = self.store.mutate_json(STATE_FILE, mutate)
            return _document_from_store(stored)
        except StateProposalError:
            raise
        except Exception as exc:
            raise ProposalStorageError(f"unable to mutate {STATE_FILE}: {exc}") from exc

    @staticmethod
    def _replace(document: _ProposalDocument, proposal: StateChangeProposal) -> _ProposalDocument:
        proposals = dict(document.proposals)
        proposals[proposal.proposal_id] = proposal
        return document.model_copy(update={"proposals": proposals})

    @staticmethod
    def _updated_proposal(proposal: StateChangeProposal, **updates: Any) -> StateChangeProposal:
        payload = proposal.model_dump(mode="python")
        payload.update(updates)
        return StateChangeProposal.model_validate(payload, strict=True)

    @staticmethod
    def _assert_same_request(existing: StateChangeProposal, candidate: StateChangeProposal) -> None:
        if existing.payload_digest != candidate.payload_digest:
            raise ProposalConflictError(
                "idempotency key already belongs to a different effect or payload "
                f"(existing={existing.payload_digest}, requested={candidate.payload_digest})"
            )
        comparable = (
            "effect",
            "operation",
            "target",
            "source_span",
            "explicitness",
            "preconditions",
            "payload",
            "expected_state_revisions",
            "requires_approval",
            "expires_at",
        )
        differences = [name for name in comparable if getattr(existing, name) != getattr(candidate, name)]
        if differences:
            raise ProposalConflictError(
                f"idempotency key already belongs to a proposal with different {', '.join(differences)}"
            )

    @staticmethod
    def _approval_error(
        proposal: StateChangeProposal,
        approval: StateChangeApproval | None,
        now: datetime,
    ) -> str:
        if proposal.requires_approval and approval is None:
            return "explicit approval is required before this state change"
        if approval is None:
            return ""
        if approval.proposal_id != proposal.proposal_id:
            return "approval is bound to a different proposal"
        if approval.payload_digest != proposal.payload_digest:
            return "approval is bound to a different payload"
        if approval.approved_at > now:
            return "approval timestamp is in the future"
        if proposal.expires_at is not None and approval.approved_at >= proposal.expires_at:
            return "approval was issued after the proposal expired"
        return ""

    @staticmethod
    def _precondition_result(
        proposal: StateChangeProposal,
        *,
        status: Literal["approval_required", "expired", "stale_revision"],
        error: str,
        now: datetime,
        observed: dict[str, int] | None = None,
    ) -> StateChangeCommitResult:
        return StateChangeCommitResult(
            proposal_id=proposal.proposal_id,
            effect=proposal.effect,
            status=status,
            applied=False,
            error=error,
            expected_state_revisions=proposal.expected_state_revisions,
            observed_state_revisions=observed or {},
            completed_at=now,
        )

    @staticmethod
    def _replayed(result: StateChangeCommitResult | None) -> StateChangeCommitResult:
        if result is None:
            raise ProposalStorageError("proposal status has no recorded commit result")
        return result.model_copy(update={"replayed": True})

    @staticmethod
    def _claim_matches(current: StateChangeProposal, claimed: StateChangeProposal) -> bool:
        if (
            current.proposal_id != claimed.proposal_id
            or current.payload_digest != claimed.payload_digest
            or current.status != "committing"
            or current.commit_started_at != claimed.commit_started_at
        ):
            return False
        if claimed.claim_token is not None:
            return current.claim_token == claimed.claim_token
        # Backward compatibility for a v1 registry that was stranded in
        # ``committing`` before claim tokens were introduced.
        return (
            current.claim_token is None
            and current.commit_result is not None
            and claimed.commit_result is not None
            and current.commit_result.started_at == claimed.commit_result.started_at
        )

    def _claim_lost_result(
        self,
        claimed: StateChangeProposal,
        *,
        current: StateChangeProposal,
        phase: str,
        final_revisions: dict[str, int] | None = None,
    ) -> StateChangeCommitResult:
        observed = (
            dict(claimed.commit_result.observed_state_revisions)
            if claimed.commit_result is not None
            else {}
        )
        return StateChangeCommitResult(
            proposal_id=claimed.proposal_id,
            effect=claimed.effect,
            status="indeterminate",
            applied=None,
            error=(
                f"proposal claim ownership changed {phase}; "
                "the handler outcome cannot be attributed to the original claim"
            ),
            expected_state_revisions=claimed.expected_state_revisions,
            observed_state_revisions=observed,
            final_state_revisions=(
                final_revisions
                if final_revisions is not None
                else self._observed_revisions(claimed.expected_state_revisions)
            ),
            started_at=current.commit_started_at or claimed.commit_started_at,
            completed_at=self._now(),
        )

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("state proposal clock must return a timezone-aware datetime")
        return now.astimezone(timezone.utc)

    def _observed_revisions(self, expected: dict[str, int]) -> dict[str, int]:
        observed: dict[str, int] = {}
        for name in expected:
            payload = self.store.read_json(name)
            if not payload:
                raise ProposalStorageError(f"revision target is missing: {name}")
            if payload.get("_state_corrupt"):
                raise ProposalStorageError(f"revision target is corrupt: {name}")
            observed[name] = int(payload.get("_state_revision") or 0)
        return observed


def _document_from_store(raw: dict[str, Any]) -> _ProposalDocument:
    if not raw:
        return _ProposalDocument()
    try:
        encoded = json.dumps(raw, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        return _ProposalDocument.model_validate_json(encoded, strict=True)
    except Exception as exc:
        raise ProposalStorageError(f"invalid {STATE_FILE}: {exc}") from exc


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _validate_json_value(value: Any) -> JsonValue:
    validated = _JSON_VALUE_ADAPTER.validate_python(value, strict=True)
    encoded = _canonical_json(validated)
    return json.loads(encoded)


def _handler_business_failure(
    output: JsonValue | None,
) -> tuple[Literal["failed", "indeterminate"] | None, str | None]:
    """Interpret an explicit handler outcome without changing legacy successes.

    Existing handlers that return arbitrary JSON, a success-shaped mapping, or
    no ``status`` field remain successful.  Only explicit negative terminal
    signals are converted into a non-applied proposal result.
    """

    if not isinstance(output, dict):
        return None, None
    raw_status = str(output.get("status") or "").strip().lower()
    status = re.sub(r"[\s-]+", "_", raw_status)
    detail = str(
        output.get("error")
        or output.get("reason")
        or output.get("message")
        or ""
    ).strip()

    indeterminate_statuses = {
        "indeterminate",
        "uncertain",
        "unknown_outcome",
        "partial",
        "partially_applied",
    }
    failed_statuses = {
        "approval_required",
        "blocked",
        "denied",
        "duplicate",
        "error",
        "expired",
        "failed",
        "failure",
        "in_progress",
        "invalid",
        "missing",
        "needs_clarification",
        "needs_disambiguation",
        "needs_parameters",
        "needs_user_input",
        "not_configured",
        "not_found",
        "rejected",
        "skipped",
        "stale_revision",
        "unsupported",
    }
    explicit_false = (
        output.get("applied") is False
        or output.get("success") is False
        or output.get("ok") is False
    )
    if status in indeterminate_statuses:
        message = f"handler reported an indeterminate business outcome: {status}"
        if detail:
            message = f"{message}: {detail}"
        return "indeterminate", message[:1200]
    if status in failed_statuses or status.startswith("state_change_failed") or explicit_false:
        label = status or "explicit_false_outcome"
        message = f"handler reported a non-applied business outcome: {label}"
        if detail:
            message = f"{message}: {detail}"
        return "failed", message[:1200]
    return None, None


def _handler_error(exc: Exception) -> str:
    detail = str(exc).strip()
    message = f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__
    return message[:1200]
