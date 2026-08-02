from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from starlette.concurrency import run_in_threadpool

from interface.capability_gap import CapabilityGapLifecycleReceipt
from routers.private_control_plane import PrivateControlPlaneRoute
from runtime.capability_gap_registry import (
    CapabilityGapConflictError,
    CapabilityGapError,
    CapabilityGapNotFoundError,
    CapabilityGapRegistry,
    CapabilityGapStorageError,
    CapabilityGapUnauthorizedError,
    CapabilityGapUnavailableError,
)
from runtime.extension_spec_quarantine import (
    ExtensionSpecConflictError,
    ExtensionSpecNotFoundError,
    ExtensionSpecStorageError,
)


_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$"
_GAP_ID_PATTERN = r"^capgap_[0-9a-f]{24}$"
_CANDIDATE_ID_PATTERN = r"^extspec_[0-9a-f]{24}$"
_DIGEST_PATTERN = r"^[0-9a-f]{64}$"


class CapabilityGapSpecLinkCommand(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    schema_version: Literal[
        "veyra.phase6.capability_gap_spec_link_command.v1"
    ]
    operation_id: str = Field(
        min_length=1, max_length=240, pattern=_ID_PATTERN
    )
    user_id: str = Field(min_length=1, max_length=240)
    workspace_id: str = Field(min_length=1, max_length=240)
    session_id: str = Field(min_length=1, max_length=240)
    candidate_id: str = Field(
        min_length=32, max_length=32, pattern=_CANDIDATE_ID_PATTERN
    )
    expected_gap_revision: StrictInt = Field(ge=1, le=2_147_483_647)
    expected_candidate_revision: StrictInt = Field(
        ge=1, le=2_147_483_647
    )
    expected_spec_digest: str = Field(
        min_length=64, max_length=64, pattern=_DIGEST_PATTERN
    )


class CapabilityGapObservationCommand(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    schema_version: Literal[
        "veyra.phase6.capability_gap_observation_command.v1"
    ]
    operation_id: str = Field(
        min_length=1, max_length=240, pattern=_ID_PATTERN
    )
    user_id: str = Field(min_length=1, max_length=240)
    workspace_id: str = Field(min_length=1, max_length=240)
    session_id: str = Field(min_length=1, max_length=240)
    expected_gap_revision: StrictInt = Field(ge=1, le=2_147_483_647)
    receipt: CapabilityGapLifecycleReceipt


def _control_token(request: Request) -> str:
    authorization = str(request.headers.get("authorization") or "").strip()
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return str(request.headers.get("x-veyra-token") or "").strip()


def _raise_gap_error(exc: Exception) -> None:
    if isinstance(
        exc, (CapabilityGapNotFoundError, ExtensionSpecNotFoundError)
    ):
        raise HTTPException(status_code=404, detail="capability gap not found")
    if isinstance(exc, CapabilityGapUnauthorizedError):
        raise HTTPException(
            status_code=401,
            detail="valid Veyra control token required",
        )
    if isinstance(
        exc,
        (
            CapabilityGapStorageError,
            CapabilityGapUnavailableError,
            ExtensionSpecStorageError,
        ),
    ):
        raise HTTPException(
            status_code=503,
            detail="capability-gap registry is unavailable",
        )
    if isinstance(
        exc,
        (
            CapabilityGapConflictError,
            ExtensionSpecConflictError,
        ),
    ):
        raise HTTPException(
            status_code=409,
            detail="capability-gap request conflicts with current state",
        )
    if isinstance(exc, (TypeError, ValueError)):
        raise HTTPException(status_code=422, detail="invalid capability-gap request")
    if isinstance(exc, CapabilityGapError):
        raise HTTPException(status_code=409, detail="capability-gap request failed")
    raise exc


def build_phase6_capability_gaps_router(
    *, registry: CapabilityGapRegistry
) -> APIRouter:
    router = APIRouter(
        prefix="/phase6/capability-gaps",
        tags=["phase6-capability-gaps"],
        route_class=PrivateControlPlaneRoute,
    )

    @router.get("/status")
    async def capability_gap_status(
        request: Request,
        user_id: str = Query(min_length=1, max_length=240),
        workspace_id: str = Query(min_length=1, max_length=240),
        session_id: str = Query(min_length=1, max_length=240),
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                registry.status,
                user_id=user_id,
                workspace_id=workspace_id,
                session_id=session_id,
                control_token=_control_token(request),
            )
        except Exception as exc:
            _raise_gap_error(exc)
            raise AssertionError("unreachable")

    @router.get("")
    async def list_capability_gaps(
        request: Request,
        user_id: str = Query(min_length=1, max_length=240),
        workspace_id: str = Query(min_length=1, max_length=240),
        session_id: str = Query(min_length=1, max_length=240),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                registry.list,
                user_id=user_id,
                workspace_id=workspace_id,
                session_id=session_id,
                control_token=_control_token(request),
                limit=limit,
            )
        except Exception as exc:
            _raise_gap_error(exc)
            raise AssertionError("unreachable")

    @router.get("/{gap_id}")
    async def get_capability_gap(
        request: Request,
        gap_id: str = Path(pattern=_GAP_ID_PATTERN),
        user_id: str = Query(min_length=1, max_length=240),
        workspace_id: str = Query(min_length=1, max_length=240),
        session_id: str = Query(min_length=1, max_length=240),
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                registry.get,
                gap_id=gap_id,
                user_id=user_id,
                workspace_id=workspace_id,
                session_id=session_id,
                control_token=_control_token(request),
            )
        except Exception as exc:
            _raise_gap_error(exc)
            raise AssertionError("unreachable")

    @router.get("/{gap_id}/timeline")
    async def capability_gap_timeline(
        request: Request,
        gap_id: str = Path(pattern=_GAP_ID_PATTERN),
        user_id: str = Query(min_length=1, max_length=240),
        workspace_id: str = Query(min_length=1, max_length=240),
        session_id: str = Query(min_length=1, max_length=240),
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                registry.timeline,
                gap_id=gap_id,
                user_id=user_id,
                workspace_id=workspace_id,
                session_id=session_id,
                control_token=_control_token(request),
            )
        except Exception as exc:
            _raise_gap_error(exc)
            raise AssertionError("unreachable")

    @router.post("/{gap_id}/spec-link")
    async def link_capability_gap_spec(
        request: Request,
        command: CapabilityGapSpecLinkCommand,
        gap_id: str = Path(pattern=_GAP_ID_PATTERN),
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                registry.link_spec_candidate,
                gap_id=gap_id,
                candidate_id=command.candidate_id,
                user_id=command.user_id,
                workspace_id=command.workspace_id,
                session_id=command.session_id,
                expected_gap_revision=command.expected_gap_revision,
                expected_candidate_revision=command.expected_candidate_revision,
                expected_spec_digest=command.expected_spec_digest,
                operation_id=command.operation_id,
                control_token=_control_token(request),
            )
        except Exception as exc:
            _raise_gap_error(exc)
            raise AssertionError("unreachable")

    @router.post("/{gap_id}/observations")
    async def observe_capability_gap_lifecycle(
        request: Request,
        command: CapabilityGapObservationCommand,
        gap_id: str = Path(pattern=_GAP_ID_PATTERN),
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                registry.observe_lifecycle,
                gap_id=gap_id,
                receipt=command.receipt,
                user_id=command.user_id,
                workspace_id=command.workspace_id,
                session_id=command.session_id,
                expected_gap_revision=command.expected_gap_revision,
                operation_id=command.operation_id,
                control_token=_control_token(request),
            )
        except Exception as exc:
            _raise_gap_error(exc)
            raise AssertionError("unreachable")

    return router


__all__ = [
    "CapabilityGapObservationCommand",
    "CapabilityGapSpecLinkCommand",
    "build_phase6_capability_gaps_router",
]
