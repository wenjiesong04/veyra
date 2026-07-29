from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from starlette.concurrency import run_in_threadpool

from interface.event_schema import (
    EventSource,
    EventType,
    VeyraEvent,
)
from runtime.agent_capability_directory import (
    AgentCapabilitySelectionError,
)
from runtime.bounded_agent_negotiation import BoundedNegotiationError
from runtime.durable_case_store import (
    CaseNotFoundError,
    CaseOperationConflictError,
    CaseRevisionConflictError,
    CaseStorageError,
    CaseTraceBackpressureError,
    CaseTransitionError,
)
from runtime.read_only_agent_collaboration import (
    CollaborationConflictError,
    CollaborationNotFoundError,
    CollaborationStorageError,
    ReadOnlyAgentCollaborationRuntime,
    ReadOnlyCollaborationError,
)


_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$"


class Phase6StartRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    schema_version: Literal[
        "veyra.phase6.collaboration_start.v1"
    ]
    event_id: str = Field(
        min_length=1, max_length=128, pattern=_ID_PATTERN
    )
    operation_id: str = Field(
        min_length=1, max_length=240, pattern=_ID_PATTERN
    )
    user_id: str = Field(min_length=1, max_length=240)
    workspace_id: str = Field(min_length=1, max_length=240)
    session_id: str = Field(
        min_length=1, max_length=240, pattern=_ID_PATTERN
    )
    channel: str = Field(
        default="api",
        min_length=1,
        max_length=120,
        pattern=_ID_PATTERN,
    )
    runtime: str = Field(
        default="openclaw",
        min_length=1,
        max_length=120,
        pattern=_ID_PATTERN,
    )
    user_goal: str = Field(min_length=1, max_length=4_000)
    context_summary: str = Field(default="", max_length=4_000)
    evidence_refs: list[str] = Field(
        default_factory=list, max_length=32
    )


class Phase6AdvanceRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    schema_version: Literal[
        "veyra.phase6.collaboration_advance.v1"
    ]
    user_id: str = Field(min_length=1, max_length=240)
    workspace_id: str = Field(min_length=1, max_length=240)
    operation_id: str = Field(
        min_length=1, max_length=240, pattern=_ID_PATTERN
    )


class Phase6SelectionRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    schema_version: Literal[
        "veyra.phase6.plan_selection_command.v1"
    ]
    user_id: str = Field(min_length=1, max_length=240)
    workspace_id: str = Field(min_length=1, max_length=240)
    expected_revision: StrictInt = Field(ge=1)
    operation_id: str = Field(
        min_length=1, max_length=240, pattern=_ID_PATTERN
    )
    selected_option_id: str = Field(
        min_length=1, max_length=128, pattern=_ID_PATTERN
    )
    decision_reason: str = Field(
        min_length=1, max_length=1_200
    )


class Phase6CancelRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    schema_version: Literal[
        "veyra.phase6.collaboration_cancel.v1"
    ]
    user_id: str = Field(min_length=1, max_length=240)
    workspace_id: str = Field(min_length=1, max_length=240)
    expected_revision: StrictInt = Field(ge=1)
    operation_id: str = Field(
        min_length=1, max_length=240, pattern=_ID_PATTERN
    )
    reason: str = Field(
        default="user_requested",
        min_length=1,
        max_length=600,
    )


def _raise_phase6_error(exc: Exception) -> None:
    if isinstance(
        exc,
        (
            CollaborationNotFoundError,
            CaseNotFoundError,
        ),
    ):
        raise HTTPException(
            status_code=404, detail="collaboration not found"
        )
    if isinstance(
        exc,
        (
            AgentCapabilitySelectionError,
            CollaborationConflictError,
            CaseOperationConflictError,
            CaseRevisionConflictError,
            CaseTraceBackpressureError,
            CaseTransitionError,
            BoundedNegotiationError,
            ReadOnlyCollaborationError,
        ),
    ):
        raise HTTPException(status_code=409, detail=str(exc)[:600])
    if isinstance(exc, (CollaborationStorageError, CaseStorageError)):
        raise HTTPException(
            status_code=503,
            detail="Phase 6 collaboration state is unavailable",
        )
    if isinstance(exc, (TypeError, ValueError)):
        raise HTTPException(status_code=422, detail=str(exc)[:600])
    raise exc


def build_phase6_router(
    *,
    collaboration: ReadOnlyAgentCollaborationRuntime,
) -> APIRouter:
    router = APIRouter(prefix="/phase6", tags=["phase6"])

    @router.get("/status")
    async def status() -> dict[str, Any]:
        return await run_in_threadpool(collaboration.status)

    @router.get("/collaborations")
    async def list_collaborations(
        user_id: str = Query(min_length=1, max_length=240),
        workspace_id: str = Query(min_length=1, max_length=240),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                collaboration.list,
                user_id=user_id,
                workspace_id=workspace_id,
                limit=limit,
            )
        except Exception as exc:
            _raise_phase6_error(exc)
            raise AssertionError("unreachable")

    @router.get("/collaborations/{case_id}")
    async def get_collaboration(
        case_id: str,
        user_id: str = Query(min_length=1, max_length=240),
        workspace_id: str = Query(min_length=1, max_length=240),
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                collaboration.get,
                case_id=case_id,
                user_id=user_id,
                workspace_id=workspace_id,
            )
        except Exception as exc:
            _raise_phase6_error(exc)
            raise AssertionError("unreachable")

    @router.post("/collaborations")
    async def start_collaboration(
        request: Phase6StartRequest,
    ) -> dict[str, Any]:
        event = VeyraEvent(
            type=EventType.USER_MESSAGE,
            source=EventSource(
                channel=request.channel,
                user_id=request.user_id,
                session_id=request.session_id,
            ),
            payload={"text": request.user_goal},
            event_id=request.event_id,
            privacy_scope="user",
        )
        try:
            return await run_in_threadpool(
                collaboration.start,
                event=event,
                workspace_id=request.workspace_id,
                runtime=request.runtime,
                operation_id=request.operation_id,
                context_summary=request.context_summary,
                evidence_refs=request.evidence_refs,
            )
        except Exception as exc:
            _raise_phase6_error(exc)
            raise AssertionError("unreachable")

    @router.post("/collaborations/{case_id}/advance")
    async def advance_collaboration(
        case_id: str,
        request: Phase6AdvanceRequest,
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                collaboration.advance,
                case_id=case_id,
                user_id=request.user_id,
                workspace_id=request.workspace_id,
                operation_id=request.operation_id,
            )
        except Exception as exc:
            _raise_phase6_error(exc)
            raise AssertionError("unreachable")

    @router.post("/collaborations/{case_id}/select")
    async def select_plan(
        case_id: str,
        request: Phase6SelectionRequest,
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                collaboration.select_plan,
                case_id=case_id,
                user_id=request.user_id,
                workspace_id=request.workspace_id,
                expected_revision=request.expected_revision,
                operation_id=request.operation_id,
                selected_option_id=request.selected_option_id,
                decision_reason=request.decision_reason,
            )
        except Exception as exc:
            _raise_phase6_error(exc)
            raise AssertionError("unreachable")

    @router.post("/collaborations/{case_id}/cancel")
    async def cancel_collaboration(
        case_id: str,
        request: Phase6CancelRequest,
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                collaboration.cancel,
                case_id=case_id,
                user_id=request.user_id,
                workspace_id=request.workspace_id,
                expected_revision=request.expected_revision,
                operation_id=request.operation_id,
                reason=request.reason,
            )
        except Exception as exc:
            _raise_phase6_error(exc)
            raise AssertionError("unreachable")

    return router


__all__ = [
    "Phase6AdvanceRequest",
    "Phase6CancelRequest",
    "Phase6SelectionRequest",
    "Phase6StartRequest",
    "build_phase6_router",
]
