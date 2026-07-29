from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from starlette.concurrency import run_in_threadpool

from interface.extension_spec import ExtensionSpec
from runtime.extension_spec_quarantine import (
    ExtensionSpecConflictError,
    ExtensionSpecNotFoundError,
    ExtensionSpecQuarantine,
    ExtensionSpecQuarantineError,
    ExtensionSpecStorageError,
)


_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$"
_DIGEST_PATTERN = r"^[0-9a-f]{64}$"


class ExtensionSpecQuarantineRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    schema_version: Literal[
        "veyra.phase6.extension_spec_quarantine_command.v1"
    ]
    operation_id: str = Field(
        min_length=1,
        max_length=240,
        pattern=_ID_PATTERN,
    )
    user_id: str = Field(min_length=1, max_length=240)
    workspace_id: str = Field(min_length=1, max_length=240)
    expected_spec_digest: str = Field(pattern=_DIGEST_PATTERN)
    spec: ExtensionSpec


class ExtensionSpecReviewRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    schema_version: Literal[
        "veyra.phase6.extension_spec_review_command.v1"
    ]
    operation_id: str = Field(
        min_length=1,
        max_length=240,
        pattern=_ID_PATTERN,
    )
    user_id: str = Field(min_length=1, max_length=240)
    workspace_id: str = Field(min_length=1, max_length=240)
    expected_revision: StrictInt = Field(ge=1)
    decision: Literal[
        "accept_for_future_isolated_generation",
        "reject",
    ]
    reason: str = Field(min_length=1, max_length=1_200)


class ExtensionSpecRevokeRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    schema_version: Literal[
        "veyra.phase6.extension_spec_revoke_command.v1"
    ]
    operation_id: str = Field(
        min_length=1,
        max_length=240,
        pattern=_ID_PATTERN,
    )
    user_id: str = Field(min_length=1, max_length=240)
    workspace_id: str = Field(min_length=1, max_length=240)
    expected_revision: StrictInt = Field(ge=1)
    reason: str = Field(min_length=1, max_length=1_200)


def _raise_extension_error(exc: Exception) -> None:
    if isinstance(exc, ExtensionSpecNotFoundError):
        raise HTTPException(
            status_code=404,
            detail="ExtensionSpec candidate not found",
        )
    if isinstance(exc, ExtensionSpecStorageError):
        raise HTTPException(
            status_code=503,
            detail="ExtensionSpec quarantine is unavailable",
        )
    if isinstance(exc, ExtensionSpecConflictError):
        raise HTTPException(
            status_code=409,
            detail=str(exc)[:600],
        )
    if isinstance(exc, (TypeError, ValueError)):
        raise HTTPException(
            status_code=422,
            detail=str(exc)[:600],
        )
    if isinstance(exc, ExtensionSpecQuarantineError):
        raise HTTPException(
            status_code=409,
            detail=str(exc)[:600],
        )
    raise exc


def build_phase6_extensions_router(
    *,
    quarantine: ExtensionSpecQuarantine,
) -> APIRouter:
    router = APIRouter(
        prefix="/phase6/extensions",
        tags=["phase6-extensions"],
    )

    @router.get("/status")
    async def extension_status() -> dict[str, Any]:
        return await run_in_threadpool(quarantine.status)

    @router.get("/specs")
    async def list_extension_specs(
        user_id: str = Query(min_length=1, max_length=240),
        workspace_id: str = Query(min_length=1, max_length=240),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                quarantine.list,
                user_id=user_id,
                workspace_id=workspace_id,
                limit=limit,
            )
        except Exception as exc:
            _raise_extension_error(exc)
            raise AssertionError("unreachable")

    @router.get("/specs/{candidate_id}")
    async def get_extension_spec(
        candidate_id: str,
        user_id: str = Query(min_length=1, max_length=240),
        workspace_id: str = Query(min_length=1, max_length=240),
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                quarantine.get,
                candidate_id=candidate_id,
                user_id=user_id,
                workspace_id=workspace_id,
            )
        except Exception as exc:
            _raise_extension_error(exc)
            raise AssertionError("unreachable")

    @router.get("/specs/{candidate_id}/integrity")
    async def extension_spec_integrity(
        candidate_id: str,
        user_id: str = Query(min_length=1, max_length=240),
        workspace_id: str = Query(min_length=1, max_length=240),
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                quarantine.integrity,
                candidate_id=candidate_id,
                user_id=user_id,
                workspace_id=workspace_id,
            )
        except Exception as exc:
            _raise_extension_error(exc)
            raise AssertionError("unreachable")

    @router.post("/specs")
    async def quarantine_extension_spec(
        request: ExtensionSpecQuarantineRequest,
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                quarantine.quarantine,
                spec=request.spec,
                expected_spec_digest=request.expected_spec_digest,
                user_id=request.user_id,
                workspace_id=request.workspace_id,
                operation_id=request.operation_id,
            )
        except Exception as exc:
            _raise_extension_error(exc)
            raise AssertionError("unreachable")

    @router.post("/specs/{candidate_id}/review")
    async def review_extension_spec(
        candidate_id: str,
        request: ExtensionSpecReviewRequest,
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                quarantine.review,
                candidate_id=candidate_id,
                user_id=request.user_id,
                workspace_id=request.workspace_id,
                expected_revision=request.expected_revision,
                operation_id=request.operation_id,
                decision=request.decision,
                reason=request.reason,
            )
        except Exception as exc:
            _raise_extension_error(exc)
            raise AssertionError("unreachable")

    @router.post("/specs/{candidate_id}/revoke")
    async def revoke_extension_spec(
        candidate_id: str,
        request: ExtensionSpecRevokeRequest,
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                quarantine.revoke,
                candidate_id=candidate_id,
                user_id=request.user_id,
                workspace_id=request.workspace_id,
                expected_revision=request.expected_revision,
                operation_id=request.operation_id,
                reason=request.reason,
            )
        except Exception as exc:
            _raise_extension_error(exc)
            raise AssertionError("unreachable")

    return router


__all__ = [
    "ExtensionSpecQuarantineRequest",
    "ExtensionSpecReviewRequest",
    "ExtensionSpecRevokeRequest",
    "build_phase6_extensions_router",
]
