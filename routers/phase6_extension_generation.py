from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from starlette.concurrency import run_in_threadpool

from routers.private_control_plane import PrivateControlPlaneRoute
from runtime.extension_artifact_quarantine import (
    ExtensionArtifactError,
    ExtensionArtifactNotFoundError,
    ExtensionArtifactStorageError,
)
from runtime.extension_generation_gate import (
    ExtensionGenerationConflictError,
    ExtensionGenerationError,
    ExtensionGenerationGate,
    ExtensionGenerationNotFoundError,
    ExtensionGenerationStorageError,
    ExtensionGenerationUnauthorizedError,
    ExtensionGenerationUnavailableError,
)
from runtime.extension_spec_quarantine import (
    ExtensionSpecConflictError,
    ExtensionSpecNotFoundError,
    ExtensionSpecQuarantineError,
    ExtensionSpecStorageError,
)


_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$"
_CANDIDATE_ID_PATTERN = r"^extspec_[0-9a-f]{24}$"
_GENERATION_ID_PATTERN = r"^extgen_[0-9a-f]{24}$"
_DIGEST_PATTERN = r"^[0-9a-f]{64}$"


class ExtensionGenerationCommand(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    schema_version: Literal[
        "veyra.phase6.extension_generation_command.v1"
    ]
    operation_id: str = Field(
        min_length=1,
        max_length=240,
        pattern=_ID_PATTERN,
    )
    request_id: str = Field(
        min_length=1,
        max_length=240,
        pattern=_ID_PATTERN,
    )
    user_id: str = Field(min_length=1, max_length=240)
    workspace_id: str = Field(min_length=1, max_length=240)
    session_id: str = Field(min_length=1, max_length=240)
    expected_candidate_revision: StrictInt = Field(
        ge=1,
        le=2_147_483_647,
    )
    expected_spec_digest: str = Field(
        min_length=64,
        max_length=64,
        pattern=_DIGEST_PATTERN,
    )


def _control_token(request: Request) -> str:
    authorization = str(request.headers.get("authorization") or "").strip()
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return str(request.headers.get("x-veyra-token") or "").strip()


def _raise_generation_error(exc: Exception) -> None:
    if isinstance(
        exc,
        (
            ExtensionGenerationNotFoundError,
            ExtensionSpecNotFoundError,
            ExtensionArtifactNotFoundError,
        ),
    ):
        raise HTTPException(
            status_code=404,
            detail="generation or prerequisite not found",
        )
    if isinstance(exc, ExtensionGenerationUnauthorizedError):
        raise HTTPException(
            status_code=401,
            detail="valid Veyra control token required for generation",
        )
    if isinstance(
        exc,
        (
            ExtensionGenerationStorageError,
            ExtensionGenerationUnavailableError,
            ExtensionSpecStorageError,
            ExtensionArtifactStorageError,
        ),
    ):
        raise HTTPException(
            status_code=503,
            detail="bounded extension generation is unavailable",
        )
    if isinstance(
        exc,
        (
            ExtensionGenerationConflictError,
            ExtensionSpecConflictError,
        ),
    ):
        raise HTTPException(
            status_code=409,
            detail="generation request conflicts with current state",
        )
    if isinstance(exc, (TypeError, ValueError)):
        raise HTTPException(
            status_code=422,
            detail="invalid extension-generation request",
        )
    if isinstance(
        exc,
        (
            ExtensionGenerationError,
            ExtensionSpecQuarantineError,
            ExtensionArtifactError,
        ),
    ):
        raise HTTPException(
            status_code=409,
            detail="extension-generation request was rejected",
        )
    raise exc


def build_phase6_extension_generation_router(
    *,
    gate: ExtensionGenerationGate,
) -> APIRouter:
    router = APIRouter(
        prefix="/phase6/extensions",
        tags=["phase6-extension-generation"],
        route_class=PrivateControlPlaneRoute,
    )

    @router.get("/generations/status")
    async def generation_status() -> dict[str, Any]:
        return await run_in_threadpool(gate.status)

    @router.get("/generations")
    async def list_generations(
        request: Request,
        user_id: str = Query(min_length=1, max_length=240),
        workspace_id: str = Query(min_length=1, max_length=240),
        session_id: str = Query(min_length=1, max_length=240),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                gate.list,
                user_id=user_id,
                workspace_id=workspace_id,
                session_id=session_id,
                control_token=_control_token(request),
                limit=limit,
            )
        except Exception as exc:
            _raise_generation_error(exc)
            raise AssertionError("unreachable")

    @router.get("/generations/{generation_id}")
    async def get_generation(
        request: Request,
        generation_id: str = Path(pattern=_GENERATION_ID_PATTERN),
        user_id: str = Query(min_length=1, max_length=240),
        workspace_id: str = Query(min_length=1, max_length=240),
        session_id: str = Query(min_length=1, max_length=240),
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                gate.get,
                generation_id=generation_id,
                user_id=user_id,
                workspace_id=workspace_id,
                session_id=session_id,
                control_token=_control_token(request),
            )
        except Exception as exc:
            _raise_generation_error(exc)
            raise AssertionError("unreachable")

    @router.get("/generations/{generation_id}/integrity")
    async def generation_integrity(
        request: Request,
        generation_id: str = Path(pattern=_GENERATION_ID_PATTERN),
        user_id: str = Query(min_length=1, max_length=240),
        workspace_id: str = Query(min_length=1, max_length=240),
        session_id: str = Query(min_length=1, max_length=240),
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                gate.integrity,
                generation_id=generation_id,
                user_id=user_id,
                workspace_id=workspace_id,
                session_id=session_id,
                control_token=_control_token(request),
            )
        except Exception as exc:
            _raise_generation_error(exc)
            raise AssertionError("unreachable")

    @router.post("/candidates/{candidate_id}/generate")
    async def generate_candidate(
        request: Request,
        command: ExtensionGenerationCommand,
        candidate_id: str = Path(pattern=_CANDIDATE_ID_PATTERN),
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                gate.generate,
                candidate_id=candidate_id,
                user_id=command.user_id,
                workspace_id=command.workspace_id,
                session_id=command.session_id,
                request_id=command.request_id,
                operation_id=command.operation_id,
                expected_candidate_revision=(
                    command.expected_candidate_revision
                ),
                expected_spec_digest=command.expected_spec_digest,
                control_token=_control_token(request),
            )
        except Exception as exc:
            _raise_generation_error(exc)
            raise AssertionError("unreachable")

    return router


__all__ = [
    "ExtensionGenerationCommand",
    "build_phase6_extension_generation_router",
]
