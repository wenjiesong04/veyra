from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from starlette.concurrency import run_in_threadpool

from routers.private_control_plane import PrivateControlPlaneRoute
from runtime.extension_artifact_quarantine import (
    ExtensionArtifactConflictError,
    ExtensionArtifactError,
    ExtensionArtifactNotFoundError,
    ExtensionArtifactStorageError,
)
from runtime.extension_source_policy_gate import (
    ExtensionSourceCheckConflictError,
    ExtensionSourceCheckError,
    ExtensionSourceCheckNotFoundError,
    ExtensionSourceCheckStorageError,
    ExtensionSourcePolicyGate,
)
from runtime.extension_spec_quarantine import (
    ExtensionSpecConflictError,
    ExtensionSpecNotFoundError,
    ExtensionSpecQuarantineError,
    ExtensionSpecStorageError,
)


_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$"
_ARTIFACT_ID_PATTERN = r"^extart_[0-9a-f]{24}$"
_CHECK_ID_PATTERN = r"^extcheck_[0-9a-f]{24}$"
_DIGEST_PATTERN = r"^[0-9a-f]{64}$"


class ExtensionSourceCheckCommand(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    schema_version: Literal[
        "veyra.phase6.extension_source_check_command.v1"
    ]
    operation_id: str = Field(
        min_length=1,
        max_length=240,
        pattern=_ID_PATTERN,
    )
    user_id: str = Field(min_length=1, max_length=240)
    workspace_id: str = Field(min_length=1, max_length=240)
    expected_artifact_revision: StrictInt = Field(
        ge=1,
        le=2_147_483_647,
    )
    expected_artifact_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=_DIGEST_PATTERN,
    )


def _raise_source_check_error(exc: Exception) -> None:
    if isinstance(
        exc,
        (
            ExtensionSourceCheckNotFoundError,
            ExtensionArtifactNotFoundError,
            ExtensionSpecNotFoundError,
        ),
    ):
        raise HTTPException(
            status_code=404,
            detail="extension source check or prerequisite not found",
        )
    if isinstance(
        exc,
        (
            ExtensionSourceCheckStorageError,
            ExtensionArtifactStorageError,
            ExtensionSpecStorageError,
        ),
    ):
        raise HTTPException(
            status_code=503,
            detail="extension source-check service is unavailable",
        )
    if isinstance(
        exc,
        (
            ExtensionSourceCheckConflictError,
            ExtensionArtifactConflictError,
            ExtensionSpecConflictError,
        ),
    ):
        raise HTTPException(
            status_code=409,
            detail="extension source-check request conflicts with current state",
        )
    if isinstance(exc, (TypeError, ValueError)):
        raise HTTPException(
            status_code=422,
            detail="invalid extension source-check request",
        )
    if isinstance(
        exc,
        (
            ExtensionSourceCheckError,
            ExtensionArtifactError,
            ExtensionSpecQuarantineError,
        ),
    ):
        raise HTTPException(
            status_code=409,
            detail="extension source-check request was rejected",
        )
    raise exc


def build_phase6_extension_source_checks_router(
    *,
    gate: ExtensionSourcePolicyGate,
) -> APIRouter:
    router = APIRouter(
        prefix="/phase6/extensions",
        tags=["phase6-extension-source-checks"],
        route_class=PrivateControlPlaneRoute,
    )

    @router.get("/source-checks/status")
    async def source_check_status() -> dict[str, Any]:
        return await run_in_threadpool(gate.status)

    @router.get("/source-checks")
    async def list_source_checks(
        user_id: str = Query(min_length=1, max_length=240),
        workspace_id: str = Query(min_length=1, max_length=240),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                gate.list,
                user_id=user_id,
                workspace_id=workspace_id,
                limit=limit,
            )
        except Exception as exc:
            _raise_source_check_error(exc)
            raise AssertionError("unreachable")

    @router.get("/source-checks/{check_id}")
    async def get_source_check(
        check_id: str = Path(pattern=_CHECK_ID_PATTERN),
        user_id: str = Query(min_length=1, max_length=240),
        workspace_id: str = Query(min_length=1, max_length=240),
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                gate.get,
                check_id=check_id,
                user_id=user_id,
                workspace_id=workspace_id,
            )
        except Exception as exc:
            _raise_source_check_error(exc)
            raise AssertionError("unreachable")

    @router.get("/source-checks/{check_id}/integrity")
    async def source_check_integrity(
        check_id: str = Path(pattern=_CHECK_ID_PATTERN),
        user_id: str = Query(min_length=1, max_length=240),
        workspace_id: str = Query(min_length=1, max_length=240),
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                gate.integrity,
                check_id=check_id,
                user_id=user_id,
                workspace_id=workspace_id,
            )
        except Exception as exc:
            _raise_source_check_error(exc)
            raise AssertionError("unreachable")

    @router.post("/artifacts/{artifact_id}/source-check")
    async def start_source_check(
        request: ExtensionSourceCheckCommand,
        artifact_id: str = Path(pattern=_ARTIFACT_ID_PATTERN),
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                gate.start,
                artifact_id=artifact_id,
                user_id=request.user_id,
                workspace_id=request.workspace_id,
                expected_artifact_revision=(
                    request.expected_artifact_revision
                ),
                expected_artifact_sha256=(
                    request.expected_artifact_sha256
                ),
                operation_id=request.operation_id,
            )
        except Exception as exc:
            _raise_source_check_error(exc)
            raise AssertionError("unreachable")

    return router


__all__ = [
    "ExtensionSourceCheckCommand",
    "build_phase6_extension_source_checks_router",
]
