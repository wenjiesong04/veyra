from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from starlette.concurrency import run_in_threadpool

from routers.private_control_plane import PrivateControlPlaneRoute
from runtime.extension_isolated_runner_gate import (
    ExtensionIsolatedRunnerConflictError,
    ExtensionIsolatedRunnerError,
    ExtensionIsolatedRunnerGate,
    ExtensionIsolatedRunnerNotFoundError,
    ExtensionIsolatedRunnerStorageError,
    ExtensionIsolatedRunnerUnauthorizedError,
    ExtensionIsolatedRunnerUnavailableError,
)
from runtime.extension_source_policy_gate import (
    ExtensionSourceCheckConflictError,
    ExtensionSourceCheckError,
    ExtensionSourceCheckNotFoundError,
    ExtensionSourceCheckStorageError,
)


_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$"
_RUN_ID_PATTERN = r"^extrun_[0-9a-f]{24}$"
_CHECK_ID_PATTERN = r"^extcheck_[0-9a-f]{24}$"
_DIGEST_PATTERN = r"^[0-9a-f]{64}$"


class ExtensionIsolatedRunCommand(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    schema_version: Literal[
        "veyra.phase6.extension_isolated_run_command.v1"
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
    expected_source_check_report_digest: str = Field(
        min_length=64,
        max_length=64,
        pattern=_DIGEST_PATTERN,
    )


def _control_token(request: Request) -> str:
    authorization = str(request.headers.get("authorization") or "").strip()
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return str(request.headers.get("x-veyra-token") or "").strip()


def _raise_isolated_runner_error(exc: Exception) -> None:
    if isinstance(
        exc,
        (
            ExtensionIsolatedRunnerNotFoundError,
            ExtensionSourceCheckNotFoundError,
        ),
    ):
        raise HTTPException(
            status_code=404,
            detail="isolated run or source-check prerequisite not found",
        )
    if isinstance(exc, ExtensionIsolatedRunnerUnauthorizedError):
        raise HTTPException(
            status_code=401,
            detail="valid Veyra control token required for isolated run",
        )
    if isinstance(
        exc,
        (
            ExtensionIsolatedRunnerStorageError,
            ExtensionIsolatedRunnerUnavailableError,
            ExtensionSourceCheckStorageError,
        ),
    ):
        raise HTTPException(
            status_code=503,
            detail="trusted isolated runner is unavailable",
        )
    if isinstance(
        exc,
        (
            ExtensionIsolatedRunnerConflictError,
            ExtensionSourceCheckConflictError,
        ),
    ):
        raise HTTPException(
            status_code=409,
            detail="isolated-run request conflicts with current state",
        )
    if isinstance(exc, (TypeError, ValueError)):
        raise HTTPException(
            status_code=422,
            detail="invalid isolated-run request",
        )
    if isinstance(
        exc,
        (ExtensionIsolatedRunnerError, ExtensionSourceCheckError),
    ):
        raise HTTPException(
            status_code=409,
            detail="isolated-run request was rejected",
        )
    raise exc


def build_phase6_extension_isolated_runner_router(
    *,
    gate: ExtensionIsolatedRunnerGate,
) -> APIRouter:
    router = APIRouter(
        prefix="/phase6/extensions",
        tags=["phase6-extension-isolated-runner"],
        route_class=PrivateControlPlaneRoute,
    )

    @router.get("/isolated-runs/status")
    async def isolated_runner_status() -> dict[str, Any]:
        return await run_in_threadpool(gate.status)

    @router.get("/isolated-runs")
    async def list_isolated_runs(
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
            _raise_isolated_runner_error(exc)
            raise AssertionError("unreachable")

    @router.get("/isolated-runs/{run_id}")
    async def get_isolated_run(
        run_id: str = Path(pattern=_RUN_ID_PATTERN),
        user_id: str = Query(min_length=1, max_length=240),
        workspace_id: str = Query(min_length=1, max_length=240),
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                gate.get,
                run_id=run_id,
                user_id=user_id,
                workspace_id=workspace_id,
            )
        except Exception as exc:
            _raise_isolated_runner_error(exc)
            raise AssertionError("unreachable")

    @router.get("/isolated-runs/{run_id}/integrity")
    async def isolated_run_integrity(
        run_id: str = Path(pattern=_RUN_ID_PATTERN),
        user_id: str = Query(min_length=1, max_length=240),
        workspace_id: str = Query(min_length=1, max_length=240),
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                gate.integrity,
                run_id=run_id,
                user_id=user_id,
                workspace_id=workspace_id,
            )
        except Exception as exc:
            _raise_isolated_runner_error(exc)
            raise AssertionError("unreachable")

    @router.post("/source-checks/{check_id}/isolated-run")
    async def start_isolated_run(
        request: Request,
        command: ExtensionIsolatedRunCommand,
        check_id: str = Path(pattern=_CHECK_ID_PATTERN),
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                gate.start,
                check_id=check_id,
                user_id=command.user_id,
                workspace_id=command.workspace_id,
                expected_artifact_revision=(
                    command.expected_artifact_revision
                ),
                expected_artifact_sha256=(
                    command.expected_artifact_sha256
                ),
                expected_source_check_report_digest=(
                    command.expected_source_check_report_digest
                ),
                operation_id=command.operation_id,
                control_token=_control_token(request),
            )
        except Exception as exc:
            _raise_isolated_runner_error(exc)
            raise AssertionError("unreachable")

    return router


__all__ = [
    "ExtensionIsolatedRunCommand",
    "build_phase6_extension_isolated_runner_router",
]
