from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from starlette.concurrency import run_in_threadpool

from routers.private_control_plane import PrivateControlPlaneRoute
from runtime.extension_release_registry import (
    ExtensionReleaseConflictError,
    ExtensionReleaseError,
    ExtensionReleaseNotFoundError,
    ExtensionReleaseRegistry,
    ExtensionReleaseStorageError,
    ExtensionReleaseUnauthorizedError,
    ExtensionReleaseUnavailableError,
)
from runtime.extension_release_signer import ExtensionReleaseSigningError


_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$"
_RELEASE_ID_PATTERN = r"^extrel_[0-9a-f]{24}$"
_GENERATION_ID_PATTERN = r"^extgen_[0-9a-f]{24}$"
_VALIDATION_ID_PATTERN = r"^extval_[0-9a-f]{24}$"
_DIGEST_PATTERN = r"^[0-9a-f]{64}$"


class ExtensionReleaseCreateCommand(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    schema_version: Literal[
        "veyra.phase6.extension_release_create_command.v1"
    ]
    operation_id: str = Field(
        min_length=1,
        max_length=240,
        pattern=_ID_PATTERN,
    )
    generation_id: str = Field(
        min_length=31,
        max_length=31,
        pattern=_GENERATION_ID_PATTERN,
    )
    user_id: str = Field(min_length=1, max_length=240)
    workspace_id: str = Field(min_length=1, max_length=240)
    session_id: str = Field(min_length=1, max_length=240)
    expected_registry_revision: StrictInt = Field(
        ge=0,
        le=2_147_483_647,
    )
    expected_generation_report_digest: str = Field(
        min_length=64,
        max_length=64,
        pattern=_DIGEST_PATTERN,
    )
    expected_validation_report_digest: str = Field(
        min_length=64,
        max_length=64,
        pattern=_DIGEST_PATTERN,
    )
    expected_generator_identity_digest: str = Field(
        min_length=64,
        max_length=64,
        pattern=_DIGEST_PATTERN,
    )
    expected_verifier_identity_digest: str = Field(
        min_length=64,
        max_length=64,
        pattern=_DIGEST_PATTERN,
    )
    expected_signing_identity_digest: str = Field(
        min_length=64,
        max_length=64,
        pattern=_DIGEST_PATTERN,
    )
    expires_at: str = Field(min_length=20, max_length=32)


class ExtensionReleaseRevokeCommand(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    schema_version: Literal[
        "veyra.phase6.extension_release_revoke_command.v1"
    ]
    operation_id: str = Field(
        min_length=1,
        max_length=240,
        pattern=_ID_PATTERN,
    )
    user_id: str = Field(min_length=1, max_length=240)
    workspace_id: str = Field(min_length=1, max_length=240)
    session_id: str = Field(min_length=1, max_length=240)
    expected_release_revision: StrictInt = Field(
        ge=1,
        le=2_147_483_647,
    )
    expected_registry_revision: StrictInt = Field(
        ge=0,
        le=2_147_483_647,
    )
    reason_digest: str = Field(
        min_length=64,
        max_length=64,
        pattern=_DIGEST_PATTERN,
    )


def _control_token(request: Request) -> str:
    authorization = str(request.headers.get("authorization") or "").strip()
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return str(request.headers.get("x-veyra-token") or "").strip()


def _raise_release_error(exc: Exception) -> None:
    if isinstance(exc, ExtensionReleaseNotFoundError):
        raise HTTPException(status_code=404, detail="signed release not found")
    if isinstance(exc, ExtensionReleaseUnauthorizedError):
        raise HTTPException(
            status_code=401,
            detail="valid Veyra control token required for signed releases",
        )
    if isinstance(
        exc,
        (
            ExtensionReleaseStorageError,
            ExtensionReleaseUnavailableError,
            ExtensionReleaseSigningError,
        ),
    ):
        raise HTTPException(
            status_code=503,
            detail="signed release registry is unavailable",
        )
    if isinstance(exc, ExtensionReleaseConflictError):
        raise HTTPException(
            status_code=409,
            detail="signed release request conflicts with current state",
        )
    if isinstance(exc, (TypeError, ValueError)):
        raise HTTPException(status_code=422, detail="invalid signed release request")
    if isinstance(exc, ExtensionReleaseError):
        raise HTTPException(status_code=409, detail="signed release request rejected")
    raise exc


def build_phase6_extension_releases_router(
    *,
    registry: ExtensionReleaseRegistry,
) -> APIRouter:
    router = APIRouter(
        prefix="/phase6/extensions",
        tags=["phase6-extension-signed-releases"],
        route_class=PrivateControlPlaneRoute,
    )

    @router.get("/signed-releases/status")
    async def signed_release_status(request: Request) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                registry.status,
                control_token=_control_token(request),
            )
        except Exception as exc:
            _raise_release_error(exc)
            raise AssertionError("unreachable")

    @router.get("/signed-releases")
    async def list_signed_releases(
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
            _raise_release_error(exc)
            raise AssertionError("unreachable")

    @router.get("/signed-releases/{release_id}")
    async def get_signed_release(
        request: Request,
        release_id: str = Path(pattern=_RELEASE_ID_PATTERN),
        user_id: str = Query(min_length=1, max_length=240),
        workspace_id: str = Query(min_length=1, max_length=240),
        session_id: str = Query(min_length=1, max_length=240),
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                registry.get,
                release_id=release_id,
                user_id=user_id,
                workspace_id=workspace_id,
                session_id=session_id,
                control_token=_control_token(request),
            )
        except Exception as exc:
            _raise_release_error(exc)
            raise AssertionError("unreachable")

    @router.get("/signed-releases/{release_id}/integrity")
    async def signed_release_integrity(
        request: Request,
        release_id: str = Path(pattern=_RELEASE_ID_PATTERN),
        user_id: str = Query(min_length=1, max_length=240),
        workspace_id: str = Query(min_length=1, max_length=240),
        session_id: str = Query(min_length=1, max_length=240),
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                registry.integrity,
                release_id=release_id,
                user_id=user_id,
                workspace_id=workspace_id,
                session_id=session_id,
                control_token=_control_token(request),
            )
        except Exception as exc:
            _raise_release_error(exc)
            raise AssertionError("unreachable")

    @router.post("/dynamic-validations/{validation_id}/signed-release")
    async def create_signed_release(
        request: Request,
        command: ExtensionReleaseCreateCommand,
        validation_id: str = Path(pattern=_VALIDATION_ID_PATTERN),
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                registry.create,
                generation_id=command.generation_id,
                validation_id=validation_id,
                user_id=command.user_id,
                workspace_id=command.workspace_id,
                session_id=command.session_id,
                operation_id=command.operation_id,
                expected_registry_revision=command.expected_registry_revision,
                expected_generation_report_digest=(
                    command.expected_generation_report_digest
                ),
                expected_validation_report_digest=(
                    command.expected_validation_report_digest
                ),
                expected_generator_identity_digest=(
                    command.expected_generator_identity_digest
                ),
                expected_verifier_identity_digest=(
                    command.expected_verifier_identity_digest
                ),
                expected_signing_identity_digest=(
                    command.expected_signing_identity_digest
                ),
                expires_at=command.expires_at,
                control_token=_control_token(request),
            )
        except Exception as exc:
            _raise_release_error(exc)
            raise AssertionError("unreachable")

    @router.post("/signed-releases/{release_id}/revoke")
    async def revoke_signed_release(
        request: Request,
        command: ExtensionReleaseRevokeCommand,
        release_id: str = Path(pattern=_RELEASE_ID_PATTERN),
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                registry.revoke,
                release_id=release_id,
                user_id=command.user_id,
                workspace_id=command.workspace_id,
                session_id=command.session_id,
                operation_id=command.operation_id,
                expected_release_revision=command.expected_release_revision,
                expected_registry_revision=command.expected_registry_revision,
                reason_digest=command.reason_digest,
                control_token=_control_token(request),
            )
        except Exception as exc:
            _raise_release_error(exc)
            raise AssertionError("unreachable")

    return router


__all__ = [
    "ExtensionReleaseCreateCommand",
    "ExtensionReleaseRevokeCommand",
    "build_phase6_extension_releases_router",
]
