from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from pydantic import ValidationError

from core.durable_case import (
    ALLOWED_CASE_TRANSITIONS,
    CASE_TRACE_SCHEMA_VERSION,
    MAX_REASON_LENGTH,
    CaseCheckpoint,
    CaseOperation,
    CasePriority,
    CaseStatus,
    DialogueMessageType,
    DialogueRecord,
    DurableCase,
    DurableCaseDocument,
    CheckpointEffectState,
    TERMINAL_CASE_STATUSES,
    canonical_json,
    deterministic_case_id,
    operation_id_digest,
    sha256_digest,
)
from core.world_state import WorldStateStore


STATE_FILE = "durable_case_state.json"
TRACE_FILE = "durable_case_trace.jsonl"


class DurableCaseError(RuntimeError):
    """Base error for the Phase 4 Durable Case authority."""


class CaseNotFoundError(DurableCaseError):
    """A case is absent or outside the caller's exact scope."""


class CaseRevisionConflictError(DurableCaseError):
    """The caller's per-case revision is stale."""


class CaseOperationConflictError(DurableCaseError):
    """An operation identity was reused with different semantics."""


class CaseTransitionError(DurableCaseError):
    """A requested case lifecycle transition is not in the frozen table."""


class CaseScopeError(DurableCaseError):
    """Internal scope-integrity error; public reads collapse this to not-found."""


class CaseStorageError(DurableCaseError):
    """The durable case registry failed strict validation."""


class CaseTraceBackpressureError(DurableCaseError):
    """A transition was rejected before its trace outbox could overflow."""


class DurableCaseStore:
    """Revisioned, replay-safe ownership for analysis-only Agent cases.

    The store owns lifecycle and recovery identity; it grants no capability and
    exposes no authorization/execution state. All writes are scoped, use
    per-case revision CAS, and retain only a digest of caller operation IDs.
    """

    def __init__(
        self,
        state_store: WorldStateStore,
        *,
        max_cases: int = 500,
        max_operations_per_case: int = 64,
        max_checkpoints_per_case: int = 32,
        max_dialogue_messages_per_case: int = 32,
        max_trace_outbox: int = 512,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        for name, value in {
            "max_cases": max_cases,
            "max_operations_per_case": max_operations_per_case,
            "max_checkpoints_per_case": max_checkpoints_per_case,
            "max_dialogue_messages_per_case": max_dialogue_messages_per_case,
            "max_trace_outbox": max_trace_outbox,
        }.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if max_operations_per_case < 3:
            raise ValueError(
                "max_operations_per_case must reserve admission and two "
                "cancellation operations"
            )
        if max_checkpoints_per_case < 2:
            raise ValueError(
                "max_checkpoints_per_case must reserve two cancellation "
                "checkpoints"
            )
        self.state_store = state_store
        self.max_cases = min(max_cases, 500)
        self.max_operations_per_case = min(max_operations_per_case, 64)
        self.max_checkpoints_per_case = min(max_checkpoints_per_case, 32)
        self.max_dialogue_messages_per_case = min(
            max_dialogue_messages_per_case, 32
        )
        self.max_trace_outbox = min(max_trace_outbox, 512)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def admit_event(
        self,
        *,
        event_id: str,
        user_id: str,
        workspace_id: str,
        user_goal: str,
        situation_id: str | None = None,
        goal_ids: list[str] | None = None,
        commitment_ids: list[str] | None = None,
        case_type: str = "agent_task",
        priority: CasePriority | str = CasePriority.NORMAL,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        """Admit one source event as exactly one scoped QUALIFIED Case."""

        self._try_flush_trace_outbox()
        now = self._now()
        selected_priority = CasePriority(priority)
        case_id = deterministic_case_id(
            user_id=user_id, workspace_id=workspace_id, event_id=event_id
        )
        op_id = operation_id or f"admit:{event_id}"
        op_digest = operation_id_digest(
            operation_id=op_id,
            case_id=case_id,
            user_id=user_id,
            workspace_id=workspace_id,
        )
        immutable_admission = {
            "event_id": event_id,
            "user_id": user_id,
            "workspace_id": workspace_id,
            "user_goal": user_goal,
            "situation_id": situation_id,
            "goal_ids": goal_ids or [],
            "commitment_ids": commitment_ids or [],
            "case_type": case_type,
            "priority": selected_priority.value,
        }
        semantic_digest = sha256_digest(
            {"kind": "admit_event", "admission": immutable_admission}
        )
        candidate = DurableCase.model_validate(
            {
                "case_id": case_id,
                "case_type": case_type,
                "scope": {
                    "user_id": user_id,
                    "workspace_id": workspace_id,
                },
                "source_event_id": event_id,
                "user_goal": user_goal,
                "situation_id": situation_id,
                "goal_ids": goal_ids or [],
                "commitment_ids": commitment_ids or [],
                "status": CaseStatus.QUALIFIED,
                "priority": selected_priority,
                "revision": 1,
                "operations": {
                    op_digest: {
                        "operation_digest": op_digest,
                        "semantic_digest": semantic_digest,
                        "kind": "admit_event",
                        "resulting_revision": 1,
                        "resulting_status": CaseStatus.QUALIFIED,
                        "applied_at": now,
                    }
                },
                "created_at": now,
                "updated_at": now,
            },
            strict=True,
        )
        event_key = self._event_index_key(
            event_id=event_id, user_id=user_id, workspace_id=workspace_id
        )
        selected: dict[str, Any] = {}

        def admit(document: DurableCaseDocument) -> DurableCaseDocument:
            existing_id = document.event_index.get(event_key)
            existing = document.cases.get(existing_id or case_id)
            if existing is not None:
                self._assert_scope(
                    existing, user_id=user_id, workspace_id=workspace_id
                )
                operation = existing.operations.get(op_digest)
                if (
                    operation is not None
                    and operation.semantic_digest != semantic_digest
                ):
                    raise CaseOperationConflictError(
                        "admission operation is already bound to different "
                        "Case semantics"
                    )
                if self._admission_projection(existing) != immutable_admission:
                    raise CaseOperationConflictError(
                        "source event is already bound to different Case "
                        "admission semantics"
                    )
                if operation is None:
                    operation = next(
                        (
                            item
                            for item in existing.operations.values()
                            if item.kind == "admit_event"
                            and item.semantic_digest == semantic_digest
                        ),
                        None,
                    )
                if operation is None:
                    raise CaseStorageError(
                        "existing Case is missing its admission operation"
                    )
                selected["case"] = existing
                selected["operation"] = operation
                selected["replayed"] = True
                return document

            cases = dict(document.cases)
            cases[candidate.case_id] = candidate
            cases = self._bound_cases(cases)
            event_index = {
                key: value
                for key, value in document.event_index.items()
                if value in cases
            }
            event_index[event_key] = candidate.case_id
            updated = document.model_copy(
                update={"cases": cases, "event_index": event_index}
            )
            updated = self._enqueue_trace(
                updated,
                trace_type="case_admitted",
                case=candidate,
                operation=candidate.operations[op_digest],
                detail={
                    "source_event_id": event_id,
                    "case_type": case_type,
                    "priority": selected_priority.value,
                },
                timestamp=now,
            )
            selected["case"] = candidate
            selected["operation"] = candidate.operations[op_digest]
            selected["replayed"] = False
            return updated

        self._mutate_document(admit)
        self._try_flush_trace_outbox()
        return self._public_result(
            selected["case"],
            operation=selected["operation"],
            replayed=bool(selected["replayed"]),
        )

    def get_case(
        self, *, case_id: str, user_id: str, workspace_id: str
    ) -> dict[str, Any]:
        document = self._read_document()
        case = document.cases.get(case_id)
        if case is None:
            raise CaseNotFoundError("case not found")
        try:
            self._assert_scope(
                case, user_id=user_id, workspace_id=workspace_id
            )
        except CaseScopeError as exc:
            raise CaseNotFoundError("case not found") from exc
        return case.model_dump(mode="json")

    def list_cases(
        self,
        *,
        user_id: str,
        workspace_id: str,
        statuses: set[CaseStatus | str] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise TypeError("limit must be an integer")
        selected_statuses = (
            {CaseStatus(status) for status in statuses}
            if statuses is not None
            else None
        )
        cases = [
            case
            for case in self._read_document().cases.values()
            if case.scope.user_id == user_id
            and case.scope.workspace_id == workspace_id
            and (
                selected_statuses is None or case.status in selected_statuses
            )
        ]
        cases.sort(key=lambda case: case.updated_at, reverse=True)
        return [
            case.model_dump(mode="json")
            for case in cases[: max(0, min(limit, 500))]
        ]

    def rotate_recovery_candidates(
        self,
        candidates: list[dict[str, Any]],
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Select a durable round-robin recovery batch.

        Recovery attempts that cannot advance a Case must not permanently
        monopolize the oldest-work window. The cursor is scheduling metadata,
        not a Case lifecycle transition, so it consumes no per-Case operation
        or checkpoint capacity.
        """

        if isinstance(limit, bool) or not isinstance(limit, int):
            raise TypeError("limit must be an integer")
        bounded_limit = max(0, min(limit, 100))
        if bounded_limit == 0 or not candidates:
            return []
        candidate_ids = [
            str(candidate.get("case_id") or "") for candidate in candidates
        ]
        if (
            any(not case_id for case_id in candidate_ids)
            or len(candidate_ids) != len(set(candidate_ids))
        ):
            raise ValueError(
                "recovery candidates must have unique non-empty case ids"
            )

        selected: list[dict[str, Any]] = []

        def rotate(document: DurableCaseDocument) -> DurableCaseDocument:
            start = document.recovery_cursor % len(candidates)
            ordered = candidates[start:] + candidates[:start]
            selected.extend(ordered[:bounded_limit])
            next_cursor = (start + len(selected)) % len(candidates)
            return document.model_copy(
                update={"recovery_cursor": next_cursor}
            )

        self._mutate_document(rotate)
        return selected

    def transition(
        self,
        *,
        case_id: str,
        user_id: str,
        workspace_id: str,
        operation_id: str,
        expected_revision: int,
        to_status: CaseStatus | str,
        reason: str,
        checkpoint: CaseCheckpoint | dict[str, Any] | None = None,
        dialogue_message: DialogueRecord | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        selected_status = CaseStatus(to_status)
        normalized_checkpoint = self._checkpoint(checkpoint)
        normalized_dialogue = self._dialogue(dialogue_message)
        return self._mutate_case(
            case_id=case_id,
            user_id=user_id,
            workspace_id=workspace_id,
            operation_id=operation_id,
            expected_revision=expected_revision,
            kind="transition",
            reason=reason,
            to_status=selected_status,
            checkpoint=normalized_checkpoint,
            dialogue=normalized_dialogue,
            allow_same_status=False,
        )

    def append_checkpoint(
        self,
        *,
        case_id: str,
        user_id: str,
        workspace_id: str,
        operation_id: str,
        expected_revision: int,
        checkpoint: CaseCheckpoint | dict[str, Any],
        reason: str = "checkpoint recorded",
    ) -> dict[str, Any]:
        return self._mutate_case(
            case_id=case_id,
            user_id=user_id,
            workspace_id=workspace_id,
            operation_id=operation_id,
            expected_revision=expected_revision,
            kind="append_checkpoint",
            reason=reason,
            checkpoint=self._checkpoint(checkpoint),
            allow_same_status=True,
        )

    def append_dialogue(
        self,
        *,
        case_id: str,
        user_id: str,
        workspace_id: str,
        operation_id: str,
        expected_revision: int,
        dialogue_message: DialogueRecord | dict[str, Any],
        reason: str = "dialogue recorded",
    ) -> dict[str, Any]:
        return self._mutate_case(
            case_id=case_id,
            user_id=user_id,
            workspace_id=workspace_id,
            operation_id=operation_id,
            expected_revision=expected_revision,
            kind="append_dialogue",
            reason=reason,
            dialogue=self._dialogue(dialogue_message),
            allow_same_status=True,
        )

    def request_cancel(
        self,
        *,
        case_id: str,
        user_id: str,
        workspace_id: str,
        operation_id: str,
        expected_revision: int,
        reason: str,
        checkpoint: CaseCheckpoint | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._mutate_case(
            case_id=case_id,
            user_id=user_id,
            workspace_id=workspace_id,
            operation_id=operation_id,
            expected_revision=expected_revision,
            kind="request_cancel",
            reason=reason,
            to_status=CaseStatus.CANCELLING,
            checkpoint=self._checkpoint(checkpoint),
            allow_same_status=False,
        )

    def complete_cancel(
        self,
        *,
        case_id: str,
        user_id: str,
        workspace_id: str,
        operation_id: str,
        expected_revision: int,
        reason: str,
        checkpoint: CaseCheckpoint | dict[str, Any],
    ) -> dict[str, Any]:
        normalized = self._checkpoint(checkpoint)
        if normalized is None:
            raise ValueError("cancel completion requires a revocation checkpoint")
        if not normalized.evidence_refs:
            raise ValueError(
                "cancel completion requires durable revocation evidence refs"
            )
        if normalized.effect_state != CheckpointEffectState.OBSERVED:
            raise ValueError(
                "cancel completion requires an observed revocation checkpoint"
            )
        return self._mutate_case(
            case_id=case_id,
            user_id=user_id,
            workspace_id=workspace_id,
            operation_id=operation_id,
            expected_revision=expected_revision,
            kind="complete_cancel",
            reason=reason,
            to_status=CaseStatus.CANCELLED,
            checkpoint=normalized,
            allow_same_status=False,
        )

    def flush_trace_outbox(self) -> dict[str, int]:
        appended = 0
        deduplicated = 0
        acknowledged = 0
        remaining_after_ack = 0
        with self.state_store.writer_transaction():
            document = self._read_document()
            pending = list(document.trace_outbox)
            if not pending:
                return {
                    "pending": 0,
                    "appended": 0,
                    "deduplicated": 0,
                    "acknowledged": 0,
                }
            persisted_ids = {
                str(row.get("transition_id") or "")
                for row in self.state_store.read_jsonl(
                    TRACE_FILE, limit=(1 << 63) - 1
                )
                if isinstance(row, dict) and row.get("transition_id")
            }
            acknowledged_ids: set[str] = set()
            for entry in pending:
                transition_id = str(entry.get("transition_id") or "")
                if not transition_id:
                    raise CaseStorageError(
                        "case trace outbox entry is missing transition_id"
                    )
                if transition_id in persisted_ids:
                    deduplicated += 1
                else:
                    self.state_store.append_jsonl(
                        TRACE_FILE, copy.deepcopy(entry)
                    )
                    persisted_ids.add(transition_id)
                    appended += 1
                acknowledged_ids.add(transition_id)

            def acknowledge(
                current: DurableCaseDocument,
            ) -> DurableCaseDocument:
                nonlocal acknowledged, remaining_after_ack
                remaining = [
                    entry
                    for entry in current.trace_outbox
                    if str(entry.get("transition_id") or "")
                    not in acknowledged_ids
                ]
                acknowledged = len(current.trace_outbox) - len(remaining)
                remaining_after_ack = len(remaining)
                return current.model_copy(
                    update={
                        "trace_outbox": remaining,
                        "trace_outbox_count": len(remaining),
                    }
                )

            self._mutate_document(acknowledge)
        return {
            "pending": remaining_after_ack,
            "appended": appended,
            "deduplicated": deduplicated,
            "acknowledged": acknowledged,
        }

    def _mutate_case(
        self,
        *,
        case_id: str,
        user_id: str,
        workspace_id: str,
        operation_id: str,
        expected_revision: int,
        kind: str,
        reason: str,
        to_status: CaseStatus | None = None,
        checkpoint: CaseCheckpoint | None = None,
        dialogue: DialogueRecord | None = None,
        allow_same_status: bool,
    ) -> dict[str, Any]:
        self._try_flush_trace_outbox()
        self._validate_expected_revision(expected_revision)
        normalized_reason = self._reason(reason)
        op_digest = operation_id_digest(
            operation_id=operation_id,
            case_id=case_id,
            user_id=user_id,
            workspace_id=workspace_id,
        )
        semantic_payload = {
            "kind": kind,
            "to_status": to_status.value if to_status is not None else None,
            "reason": normalized_reason,
            # recorded_at is storage metadata, not caller operation semantics.
            # Excluding it keeps an otherwise identical at-least-once retry
            # idempotent while the first persisted timestamp remains intact.
            "checkpoint": self._operation_checkpoint(checkpoint),
            "dialogue": self._operation_dialogue(dialogue),
        }
        semantic_digest = sha256_digest(semantic_payload)
        now = self._now()
        selected: dict[str, Any] = {}

        def mutate(document: DurableCaseDocument) -> DurableCaseDocument:
            existing = document.cases.get(case_id)
            if existing is None:
                raise CaseNotFoundError("case not found")
            try:
                self._assert_scope(
                    existing, user_id=user_id, workspace_id=workspace_id
                )
            except CaseScopeError as exc:
                raise CaseNotFoundError("case not found") from exc
            prior_operation = existing.operations.get(op_digest)
            if prior_operation is not None:
                if prior_operation.semantic_digest != semantic_digest:
                    raise CaseOperationConflictError(
                        "operation ID is already bound to different semantics"
                    )
                selected["case"] = existing
                selected["operation"] = prior_operation
                selected["replayed"] = True
                return document
            if existing.revision != expected_revision:
                raise CaseRevisionConflictError(
                    f"case revision conflict: expected {expected_revision}, "
                    f"observed {existing.revision}"
                )
            next_status = to_status or existing.status
            if not allow_same_status:
                allowed = ALLOWED_CASE_TRANSITIONS[existing.status]
                if next_status not in allowed:
                    raise CaseTransitionError(
                        f"transition {existing.status.value} -> "
                        f"{next_status.value} is not allowed"
                    )
            elif next_status != existing.status:
                raise CaseTransitionError(
                    "metadata append cannot change case status"
                )
            if dialogue is not None:
                if dialogue.sender == "veyra":
                    if dialogue.case_revision != existing.revision:
                        raise CaseRevisionConflictError(
                            "outbound dialogue case_revision must match the "
                            "current case"
                        )
                else:
                    parent = next(
                        (
                            item
                            for item in existing.dialogue
                            if item.message_id == dialogue.in_reply_to
                            and item.sender == "veyra"
                            and item.message_type
                            == DialogueMessageType.TASK_REQUEST
                        ),
                        None,
                    )
                    if parent is None:
                        raise CaseOperationConflictError(
                            "Agent dialogue in_reply_to does not identify a "
                            "recorded Veyra TASK_REQUEST"
                        )
                    if dialogue.case_revision != parent.case_revision:
                        raise CaseRevisionConflictError(
                            "Agent dialogue case_revision must echo the parent "
                            "TASK_REQUEST revision"
                        )
                    if dialogue.turn_index != parent.turn_index:
                        raise CaseOperationConflictError(
                            "Agent dialogue turn_index must echo the parent "
                            "TASK_REQUEST turn"
                        )
                if any(
                    item.message_id == dialogue.message_id
                    for item in existing.dialogue
                ):
                    raise CaseOperationConflictError(
                        "dialogue message_id is already bound to another operation"
                    )
            if checkpoint is not None and any(
                item.checkpoint_id == checkpoint.checkpoint_id
                for item in existing.checkpoints
            ):
                raise CaseOperationConflictError(
                    "checkpoint_id is already bound to another operation"
                )

            next_revision = existing.revision + 1
            operation = CaseOperation.model_validate(
                {
                    "operation_digest": op_digest,
                    "semantic_digest": semantic_digest,
                    "kind": kind,
                    "resulting_revision": next_revision,
                    "resulting_status": next_status,
                    "applied_at": now,
                },
                strict=True,
            )
            operations = dict(existing.operations)
            operations[op_digest] = operation
            cancellation_finalization = bool(
                kind == "complete_cancel"
                or (
                    existing.status == CaseStatus.CANCELLING
                    and next_status
                    in {
                        CaseStatus.CANCELLED,
                        CaseStatus.FAILED,
                        CaseStatus.INDETERMINATE,
                    }
                )
            )
            operation_limit = (
                self.max_operations_per_case
                if cancellation_finalization
                else self.max_operations_per_case - 1
                if kind == "request_cancel"
                else self.max_operations_per_case - 2
            )
            if len(operations) > operation_limit:
                raise CaseTraceBackpressureError(
                    "case operation history reached its safety reserve"
                )
            checkpoints = list(existing.checkpoints)
            if checkpoint is not None:
                checkpoints.append(checkpoint)
            checkpoint_limit = (
                self.max_checkpoints_per_case
                if cancellation_finalization
                else self.max_checkpoints_per_case - 1
                if kind == "request_cancel"
                else self.max_checkpoints_per_case - 2
            )
            if len(checkpoints) > checkpoint_limit:
                raise CaseTraceBackpressureError(
                    "case checkpoint history reached its safety reserve"
                )
            messages = list(existing.dialogue)
            if dialogue is not None:
                messages.append(dialogue)
            if len(messages) > self.max_dialogue_messages_per_case:
                raise CaseTraceBackpressureError(
                    "case dialogue history is full"
                )
            paused_from_status = existing.paused_from_status
            if (
                next_status == CaseStatus.PAUSED
                and existing.status != CaseStatus.PAUSED
            ):
                paused_from_status = existing.status
            elif (
                existing.status == CaseStatus.PAUSED
                and next_status != CaseStatus.PAUSED
            ):
                paused_from_status = None
            updated = existing.model_copy(
                update={
                    "status": next_status,
                    "paused_from_status": paused_from_status,
                    "revision": next_revision,
                    "checkpoints": checkpoints,
                    "dialogue": messages,
                    "operations": operations,
                    "next_wakeup_at": (
                        None
                        if next_status
                        in {
                            CaseStatus.CANCELLING,
                            CaseStatus.CANCELLED,
                            CaseStatus.FAILED,
                            CaseStatus.INDETERMINATE,
                            CaseStatus.CLOSED,
                        }
                        else existing.next_wakeup_at
                    ),
                    "updated_at": now,
                }
            )
            # model_copy does not validate updates; revalidate the entire record.
            updated = DurableCase.model_validate(
                updated.model_dump(mode="python"), strict=True
            )
            cases = dict(document.cases)
            cases[case_id] = updated
            next_document = document.model_copy(update={"cases": cases})
            next_document = self._enqueue_trace(
                next_document,
                trace_type=f"case_{kind}",
                case=updated,
                operation=operation,
                detail={
                    "from_status": existing.status.value,
                    "to_status": next_status.value,
                    "reason": normalized_reason,
                    "checkpoint_id": (
                        checkpoint.checkpoint_id if checkpoint else None
                    ),
                    "dialogue_message_id": (
                        dialogue.message_id if dialogue else None
                    ),
                },
                timestamp=now,
            )
            selected["case"] = updated
            selected["operation"] = operation
            selected["replayed"] = False
            return next_document

        self._mutate_document(mutate)
        self._try_flush_trace_outbox()
        return self._public_result(
            selected["case"],
            operation=selected["operation"],
            replayed=bool(selected["replayed"]),
        )

    @staticmethod
    def _operation_checkpoint(
        checkpoint: CaseCheckpoint | None,
    ) -> dict[str, Any] | None:
        if checkpoint is None:
            return None
        value = checkpoint.model_dump(mode="json")
        value.pop("recorded_at", None)
        return value

    @staticmethod
    def _operation_dialogue(
        dialogue: DialogueRecord | None,
    ) -> dict[str, Any] | None:
        if dialogue is None:
            return None
        value = dialogue.model_dump(mode="json")
        value.pop("recorded_at", None)
        return value

    def _enqueue_trace(
        self,
        document: DurableCaseDocument,
        *,
        trace_type: str,
        case: DurableCase,
        operation: CaseOperation,
        detail: dict[str, Any],
        timestamp: datetime,
    ) -> DurableCaseDocument:
        if len(document.trace_outbox) >= self.max_trace_outbox:
            raise CaseTraceBackpressureError(
                "case trace outbox capacity is exhausted"
            )
        sequence = document.trace_sequence + 1
        identity = {
            "state_file": STATE_FILE,
            "sequence": sequence,
            "trace_type": trace_type,
            "case_id": case.case_id,
            "operation_digest": operation.operation_digest,
        }
        digest = sha256_digest(identity)
        entry: dict[str, Any] = {
            "schema_version": CASE_TRACE_SCHEMA_VERSION,
            "trace_id": f"ctrace_{digest[:16]}",
            "transition_id": f"casetxn_{digest[:32]}",
            "transition_sequence": sequence,
            "trace_type": trace_type,
            "case_id": case.case_id,
            "user_id": case.scope.user_id,
            "workspace_id": case.scope.workspace_id,
            "source_event_id": case.source_event_id,
            "case_revision": case.revision,
            "case_status": case.status.value,
            "operation_digest": operation.operation_digest,
            "semantic_digest": operation.semantic_digest,
            "timestamp": timestamp.isoformat(),
            # Deliberately excludes user_goal and dialogue content.
            "detail": detail,
        }
        outbox = [*document.trace_outbox, entry]
        return document.model_copy(
            update={
                "trace_sequence": sequence,
                "trace_outbox": outbox,
                "trace_outbox_count": len(outbox),
            }
        )

    def _mutate_document(
        self,
        mutator: Callable[[DurableCaseDocument], DurableCaseDocument],
    ) -> DurableCaseDocument:
        def mutate(raw: dict[str, Any]) -> dict[str, Any]:
            document = self._document_from_store(raw)
            updated = mutator(document)
            if not isinstance(updated, DurableCaseDocument):
                raise TypeError(
                    "durable case mutator must return DurableCaseDocument"
                )
            # Revalidate model_copy updates before they cross the state boundary.
            validated = DurableCaseDocument.model_validate(
                updated.model_dump(mode="python", by_alias=True), strict=True
            )
            return validated.model_dump(mode="json", by_alias=True)

        try:
            stored = self.state_store.mutate_json(STATE_FILE, mutate)
            return self._document_from_store(stored)
        except DurableCaseError:
            raise
        except (ValidationError, ValueError, TypeError) as exc:
            raise CaseStorageError(
                f"unable to mutate {STATE_FILE}: {exc}"
            ) from exc

    def _read_document(self) -> DurableCaseDocument:
        return self._document_from_store(
            self.state_store.read_json(STATE_FILE)
        )

    @staticmethod
    def _document_from_store(raw: dict[str, Any]) -> DurableCaseDocument:
        if not raw:
            return DurableCaseDocument()
        if raw.get("_state_corrupt"):
            raise CaseStorageError(f"{STATE_FILE} is corrupt")
        try:
            encoded = json.dumps(
                raw,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
            return DurableCaseDocument.model_validate_json(
                encoded, strict=True
            )
        except Exception as exc:
            raise CaseStorageError(
                f"invalid {STATE_FILE}: {exc}"
            ) from exc

    def _try_flush_trace_outbox(self) -> dict[str, Any]:
        try:
            return self.flush_trace_outbox()
        except Exception as exc:
            try:
                pending = len(self._read_document().trace_outbox)
            except Exception:
                pending = -1
            return {
                "pending": pending,
                "appended": 0,
                "deduplicated": 0,
                "acknowledged": 0,
                "error_type": type(exc).__name__,
            }

    @staticmethod
    def _assert_scope(
        case: DurableCase, *, user_id: str, workspace_id: str
    ) -> None:
        if (
            case.scope.user_id != user_id
            or case.scope.workspace_id != workspace_id
        ):
            raise CaseScopeError("case belongs to another scope")

    @staticmethod
    def _event_index_key(
        *, event_id: str, user_id: str, workspace_id: str
    ) -> str:
        return sha256_digest(
            {
                "namespace": "veyra.durable-case-event-index.v1",
                "event_id": event_id,
                "user_id": user_id,
                "workspace_id": workspace_id,
            }
        )

    @staticmethod
    def _admission_projection(case: DurableCase) -> dict[str, Any]:
        return {
            "event_id": case.source_event_id,
            "user_id": case.scope.user_id,
            "workspace_id": case.scope.workspace_id,
            "user_goal": case.user_goal,
            "situation_id": case.situation_id,
            "goal_ids": case.goal_ids,
            "commitment_ids": case.commitment_ids,
            "case_type": case.case_type,
            "priority": case.priority.value,
        }

    @staticmethod
    def _checkpoint(
        value: CaseCheckpoint | dict[str, Any] | None,
    ) -> CaseCheckpoint | None:
        if value is None:
            return None
        if isinstance(value, CaseCheckpoint):
            return value
        return CaseCheckpoint.model_validate_json(
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                default=str,
            ),
            strict=True,
        )

    @staticmethod
    def _dialogue(
        value: DialogueRecord | dict[str, Any] | None,
    ) -> DialogueRecord | None:
        if value is None:
            return None
        if isinstance(value, DialogueRecord):
            return value
        return DialogueRecord.model_validate_json(
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                default=str,
            ),
            strict=True,
        )

    def _bound_cases(
        self, cases: dict[str, DurableCase]
    ) -> dict[str, DurableCase]:
        if len(cases) > self.max_cases:
            raise CaseTraceBackpressureError(
                "durable case capacity is exhausted; automatic eviction is "
                "disabled so source-event dedupe cannot be weakened"
            )
        return cases

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError(
                "durable case clock must return a timezone-aware datetime"
            )
        return now.astimezone(timezone.utc)

    @staticmethod
    def _reason(value: str) -> str:
        if not isinstance(value, str):
            raise TypeError("reason must be a string")
        normalized = value.strip()
        if not normalized:
            raise ValueError("reason cannot be empty")
        if normalized != value:
            raise ValueError(
                "reason cannot have leading or trailing whitespace"
            )
        if len(normalized) > MAX_REASON_LENGTH:
            raise ValueError(
                f"reason exceeds {MAX_REASON_LENGTH} characters"
            )
        return normalized

    @staticmethod
    def _validate_expected_revision(value: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("expected_revision must be a positive integer")

    @staticmethod
    def _public_result(
        case: DurableCase,
        *,
        operation: CaseOperation,
        replayed: bool,
    ) -> dict[str, Any]:
        result = case.model_dump(mode="json")
        result["operation_replayed"] = replayed
        result["operation_result"] = operation.model_dump(mode="json")
        return result
