from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from starlette.concurrency import run_in_threadpool

from interface.extension_dynamic_validation import DynamicValidationTestBundle
from routers.private_control_plane import PrivateControlPlaneRoute
from runtime.extension_dynamic_validation_gate import (
    ExtensionDynamicValidationConflictError,
    ExtensionDynamicValidationError,
    ExtensionDynamicValidationGate,
    ExtensionDynamicValidationNotFoundError,
    ExtensionDynamicValidationStorageError,
    ExtensionDynamicValidationUnauthorizedError,
    ExtensionDynamicValidationUnavailableError,
)
from runtime.extension_isolated_runner_gate import (
    ExtensionIsolatedRunnerConflictError,
    ExtensionIsolatedRunnerError,
    ExtensionIsolatedRunnerNotFoundError,
    ExtensionIsolatedRunnerStorageError,
    ExtensionIsolatedRunnerUnavailableError,
)


_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$"
_RUN_ID_PATTERN = r"^extrun_[0-9a-f]{24}$"
_VALIDATION_ID_PATTERN = r"^extval_[0-9a-f]{24}$"
_DIGEST_PATTERN = r"^[0-9a-f]{64}$"


class ExtensionDynamicValidationCommand(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    schema_version: Literal[
        "veyra.phase6.extension_dynamic_validation_command.v1"
    ]
    operation_id: str = Field(
        min_length=1,
        max_length=240,
        pattern=_ID_PATTERN,
    )
    user_id: str = Field(min_length=1, max_length=240)
    workspace_id: str = Field(min_length=1, max_length=240)
    session_id: str = Field(min_length=1, max_length=240)
    request_id: str = Field(
        min_length=1,
        max_length=240,
        pattern=_ID_PATTERN,
    )
    expected_artifact_revision: StrictInt = Field(
        ge=1,
        le=2_147_483_647,
    )
    expected_artifact_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=_DIGEST_PATTERN,
    )
    expected_source_check_report_digest: str = Field(
        min_length=64,
        max_length=64,
        pattern=_DIGEST_PATTERN,
    )
    expected_isolated_runner_report_digest: str = Field(
        min_length=64,
        max_length=64,
        pattern=_DIGEST_PATTERN,
    )
    test_bundle: DynamicValidationTestBundle


def _control_token(request: Request) -> str:
    authorization = str(request.headers.get("authorization") or "").strip()
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return str(request.headers.get("x-veyra-token") or "").strip()


def _raise_dynamic_validation_error(exc: Exception) -> None:
    if isinstance(
        exc,
        (
            ExtensionDynamicValidationNotFoundError,
            ExtensionIsolatedRunnerNotFoundError,
        ),
    ):
        raise HTTPException(
            status_code=404,
            detail="dynamic validation or isolated-run prerequisite not found",
        )
    if isinstance(exc, ExtensionDynamicValidationUnauthorizedError):
        raise HTTPException(
            status_code=401,
            detail="valid Veyra control token required for dynamic validation",
        )
    if isinstance(
        exc,
        (
            ExtensionDynamicValidationStorageError,
            ExtensionDynamicValidationUnavailableError,
            ExtensionIsolatedRunnerStorageError,
            ExtensionIsolatedRunnerUnavailableError,
        ),
    ):
        raise HTTPException(
            status_code=503,
            detail="trusted dynamic validation service is unavailable",
        )
    if isinstance(
        exc,
        (
            ExtensionDynamicValidationConflictError,
            ExtensionIsolatedRunnerConflictError,
        ),
    ):
        raise HTTPException(
            status_code=409,
            detail="dynamic validation request conflicts with current state",
        )
    if isinstance(exc, (TypeError, ValueError)):
        raise HTTPException(
            status_code=422,
            detail="invalid dynamic validation request",
        )
    if isinstance(
        exc,
        (ExtensionDynamicValidationError, ExtensionIsolatedRunnerError),
    ):
        raise HTTPException(
            status_code=409,
            detail="dynamic validation request was rejected",
        )
    raise exc


def build_phase6_extension_dynamic_validations_router(
    *,
    gate: ExtensionDynamicValidationGate,
) -> APIRouter:
    router = APIRouter(
        prefix="/phase6/extensions",
        tags=["phase6-extension-dynamic-validations"],
        route_class=PrivateControlPlaneRoute,
    )

    @router.get("/dynamic-validations/status")
    async def dynamic_validation_status() -> dict[str, Any]:
        return await run_in_threadpool(gate.status)

    @router.post("/dynamic-validations/backend/refresh")
    async def refresh_dynamic_validation_backend(
        request: Request,
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                gate.refresh_backend,
                control_token=_control_token(request),
            )
        except Exception as exc:
            _raise_dynamic_validation_error(exc)
            raise AssertionError("unreachable")

    @router.get("/dynamic-validations")
    async def list_dynamic_validations(
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
            _raise_dynamic_validation_error(exc)
            raise AssertionError("unreachable")

    @router.get("/dynamic-validations/{validation_id}")
    async def get_dynamic_validation(
        request: Request,
        validation_id: str = Path(pattern=_VALIDATION_ID_PATTERN),
        user_id: str = Query(min_length=1, max_length=240),
        workspace_id: str = Query(min_length=1, max_length=240),
        session_id: str = Query(min_length=1, max_length=240),
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                gate.get,
                validation_id=validation_id,
                user_id=user_id,
                workspace_id=workspace_id,
                session_id=session_id,
                control_token=_control_token(request),
            )
        except Exception as exc:
            _raise_dynamic_validation_error(exc)
            raise AssertionError("unreachable")

    @router.get("/dynamic-validations/{validation_id}/integrity")
    async def dynamic_validation_integrity(
        request: Request,
        validation_id: str = Path(pattern=_VALIDATION_ID_PATTERN),
        user_id: str = Query(min_length=1, max_length=240),
        workspace_id: str = Query(min_length=1, max_length=240),
        session_id: str = Query(min_length=1, max_length=240),
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                gate.integrity,
                validation_id=validation_id,
                user_id=user_id,
                workspace_id=workspace_id,
                session_id=session_id,
                control_token=_control_token(request),
            )
        except Exception as exc:
            _raise_dynamic_validation_error(exc)
            raise AssertionError("unreachable")

    @router.post("/isolated-runs/{run_id}/dynamic-validation")
    async def start_dynamic_validation(
        request: Request,
        command: ExtensionDynamicValidationCommand,
        run_id: str = Path(pattern=_RUN_ID_PATTERN),
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                gate.start,
                isolated_run_id=run_id,
                user_id=command.user_id,
                workspace_id=command.workspace_id,
                session_id=command.session_id,
                request_id=command.request_id,
                operation_id=command.operation_id,
                expected_artifact_revision=(
                    command.expected_artifact_revision
                ),
                expected_artifact_sha256=(
                    command.expected_artifact_sha256
                ),
                expected_source_check_report_digest=(
                    command.expected_source_check_report_digest
                ),
                expected_isolated_runner_report_digest=(
                    command.expected_isolated_runner_report_digest
                ),
                test_bundle=command.test_bundle.canonical_dict(),
                control_token=_control_token(request),
            )
        except Exception as exc:
            _raise_dynamic_validation_error(exc)
            raise AssertionError("unreachable")

    return router


__all__ = [
    "ExtensionDynamicValidationCommand",
    "build_phase6_extension_dynamic_validations_router",
]
