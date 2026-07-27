from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from starlette.concurrency import run_in_threadpool

from runtime.durable_case_store import (
    CaseNotFoundError,
    CaseOperationConflictError,
    CaseRevisionConflictError,
    CaseScopeError,
    CaseStorageError,
    CaseTraceBackpressureError,
    CaseTransitionError,
)
from runtime.bounded_agent_negotiation import (
    BoundedNegotiationError,
    BoundedNegotiationRuntime,
)
from core.durable_case import operation_id_digest


class CaseCommandRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    schema_version: Literal["veyra.durable_case_command.v1"] = (
        "veyra.durable_case_command.v1"
    )
    user_id: str = Field(min_length=1, max_length=240)
    workspace_id: str = Field(min_length=1, max_length=240)
    expected_revision: StrictInt = Field(ge=1)
    operation_id: str = Field(min_length=1, max_length=240)
    command: Literal["pause", "resume", "cancel", "close", "reconcile"]
    reason: str = Field(default="user_requested", min_length=1, max_length=600)


def _raise_case_error(exc: Exception) -> None:
    if isinstance(exc, (CaseNotFoundError, CaseScopeError)):
        raise HTTPException(status_code=404, detail="durable case not found")
    if isinstance(
        exc,
        (
            CaseOperationConflictError,
            CaseRevisionConflictError,
            CaseTraceBackpressureError,
            CaseTransitionError,
        ),
    ):
        raise HTTPException(status_code=409, detail=str(exc)[:600])
    if isinstance(exc, (TypeError, ValueError)):
        raise HTTPException(status_code=422, detail=str(exc)[:600])
    if isinstance(exc, CaseStorageError):
        raise HTTPException(
            status_code=503,
            detail="durable case state is temporarily unavailable",
        )
    if isinstance(exc, BoundedNegotiationError):
        raise HTTPException(
            status_code=409,
            detail=str(exc)[:600],
        )
    raise exc


def build_cases_router(deps: dict[str, Any]) -> APIRouter:
    router = APIRouter(prefix="/cases", tags=["durable-cases"])

    @router.get("")
    async def list_cases(
        user_id: str = Query(min_length=1, max_length=240),
        workspace_id: str = Query(min_length=1, max_length=240),
        status: list[str] = Query(default=[]),
        limit: int = Query(default=100, ge=1, le=100),
    ) -> dict[str, Any]:
        store = deps["durable_case_store"]
        try:
            cases = await run_in_threadpool(
                store.list_cases,
                user_id=user_id,
                workspace_id=workspace_id,
                statuses=set(status) if status else None,
                limit=limit,
            )
        except Exception as exc:
            _raise_case_error(exc)
            raise AssertionError("unreachable")
        return {
            "schema_version": "veyra.durable_case_list.v1",
            "count": len(cases),
            "cases": [
                BoundedNegotiationRuntime.public_case_summary(case)
                for case in cases
            ],
        }

    @router.get("/{case_id}")
    async def get_case(
        case_id: str,
        user_id: str = Query(min_length=1, max_length=240),
        workspace_id: str = Query(min_length=1, max_length=240),
    ) -> dict[str, Any]:
        store = deps["durable_case_store"]
        try:
            case = await run_in_threadpool(
                store.get_case,
                case_id=case_id,
                user_id=user_id,
                workspace_id=workspace_id,
            )
        except Exception as exc:
            _raise_case_error(exc)
            raise AssertionError("unreachable")
        return {
            "schema_version": "veyra.durable_case_view.v1",
            "case": BoundedNegotiationRuntime.public_case_detail(case),
        }

    @router.post("/{case_id}/commands")
    async def command_case(
        case_id: str,
        request: CaseCommandRequest,
    ) -> dict[str, Any]:
        store = deps["durable_case_store"]
        runtime = deps.get("bounded_negotiation")
        try:
            if request.command == "cancel":
                if runtime is None:
                    raise RuntimeError(
                        "bounded negotiation runtime is not configured"
                    )
                result = await run_in_threadpool(
                    runtime.cancel_case,
                    case_id=case_id,
                    user_id=request.user_id,
                    workspace_id=request.workspace_id,
                    expected_revision=request.expected_revision,
                    operation_id=request.operation_id,
                    reason=request.reason,
                )
            elif request.command == "reconcile":
                if runtime is None:
                    raise RuntimeError(
                        "bounded negotiation runtime is not configured"
                    )
                result = await run_in_threadpool(
                    runtime.recover_case,
                    case_id=case_id,
                    user_id=request.user_id,
                    workspace_id=request.workspace_id,
                    expected_revision=request.expected_revision,
                    operation_id=request.operation_id,
                    reason=request.reason,
                )
                result = (
                    BoundedNegotiationRuntime.public_recovery_summary(
                        result
                    )
                )
            else:
                current = await run_in_threadpool(
                    store.get_case,
                    case_id=case_id,
                    user_id=request.user_id,
                    workspace_id=request.workspace_id,
                )
                if (
                    request.command in {"pause", "close"}
                    and BoundedNegotiationRuntime.case_has_live_agent_authority(
                        current
                    )
                ):
                    raise CaseTransitionError(
                        "a live Agent run must be cancelled or reconciled "
                        f"before {request.command}"
                    )
                target = {
                    "pause": "PAUSED",
                    "resume": (
                        current.get("paused_from_status")
                        or "DELIBERATING"
                    ),
                    "close": "CLOSED",
                }[request.command]
                digest = operation_id_digest(
                    operation_id=request.operation_id,
                    case_id=case_id,
                    user_id=request.user_id,
                    workspace_id=request.workspace_id,
                )
                operations = (
                    current.get("operations")
                    if isinstance(current.get("operations"), dict)
                    else {}
                )
                prior = (
                    operations.get(digest)
                    if isinstance(operations.get(digest), dict)
                    else {}
                )
                if prior.get("kind") == "transition":
                    target = str(
                        prior.get("resulting_status") or target
                    )
                result = await run_in_threadpool(
                    store.transition,
                    case_id=case_id,
                    user_id=request.user_id,
                    workspace_id=request.workspace_id,
                    operation_id=request.operation_id,
                    expected_revision=request.expected_revision,
                    to_status=target,
                    reason=f"{request.command}: {request.reason}",
                )
                result = BoundedNegotiationRuntime.public_case_summary(
                    result
                )
        except Exception as exc:
            _raise_case_error(exc)
            raise AssertionError("unreachable")
        return {
            "schema_version": "veyra.durable_case_command_result.v1",
            "command": request.command,
            "result": result,
        }

    return router


__all__ = ["CaseCommandRequest", "build_cases_router"]
