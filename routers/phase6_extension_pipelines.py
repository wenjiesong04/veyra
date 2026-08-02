from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Path, Query, Request
from starlette.concurrency import run_in_threadpool

from interface.extension_pipeline import (
    ExtensionPipelineAdvanceCommand,
    ExtensionPipelineStartCommand,
)
from routers.private_control_plane import PrivateControlPlaneRoute
from runtime.extension_pipeline_coordinator import (
    ExtensionPipelineConflictError,
    ExtensionPipelineCoordinator,
    ExtensionPipelineError,
    ExtensionPipelineNotFoundError,
    ExtensionPipelineStorageError,
    ExtensionPipelineUnauthorizedError,
    ExtensionPipelineUnavailableError,
)


_PIPELINE_ID_PATTERN = r"^extpipe_[0-9a-f]{24}$"


def _control_token(request: Request) -> str:
    authorization = str(
        request.headers.get("authorization") or ""
    ).strip()
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return str(request.headers.get("x-veyra-token") or "").strip()


def _raise_pipeline_error(exc: Exception) -> None:
    if isinstance(exc, ExtensionPipelineUnauthorizedError):
        raise HTTPException(status_code=401, detail="unauthorized") from exc
    if isinstance(exc, ExtensionPipelineNotFoundError):
        raise HTTPException(status_code=404, detail="not_found") from exc
    if isinstance(exc, ExtensionPipelineConflictError):
        raise HTTPException(status_code=409, detail="conflict") from exc
    if isinstance(exc, ExtensionPipelineUnavailableError):
        raise HTTPException(status_code=503, detail="unavailable") from exc
    if isinstance(exc, ExtensionPipelineStorageError):
        raise HTTPException(status_code=503, detail="storage_unavailable") from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=422, detail="invalid_request") from exc
    if isinstance(exc, ExtensionPipelineError):
        raise HTTPException(status_code=409, detail="pipeline_rejected") from exc
    raise HTTPException(status_code=500, detail="pipeline_failure") from exc


def build_phase6_extension_pipelines_router(
    *, coordinator: ExtensionPipelineCoordinator
) -> APIRouter:
    router = APIRouter(
        prefix="/phase6/extensions/pipelines",
        tags=["phase6-extension-pipelines"],
        route_class=PrivateControlPlaneRoute,
    )

    @router.get("/status")
    async def status(request: Request) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                coordinator.status,
                control_token=_control_token(request),
            )
        except Exception as exc:
            _raise_pipeline_error(exc)
            raise AssertionError("unreachable")

    @router.get("")
    async def list_pipelines(
        request: Request,
        user_id: str = Query(min_length=1, max_length=240),
        workspace_id: str = Query(min_length=1, max_length=1024),
        session_id: str = Query(min_length=1, max_length=240),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                coordinator.list,
                user_id=user_id,
                workspace_id=workspace_id,
                session_id=session_id,
                limit=limit,
                control_token=_control_token(request),
            )
        except Exception as exc:
            _raise_pipeline_error(exc)
            raise AssertionError("unreachable")

    @router.get("/{pipeline_id}")
    async def get_pipeline(
        request: Request,
        pipeline_id: str = Path(pattern=_PIPELINE_ID_PATTERN),
        user_id: str = Query(min_length=1, max_length=240),
        workspace_id: str = Query(min_length=1, max_length=1024),
        session_id: str = Query(min_length=1, max_length=240),
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                coordinator.get,
                pipeline_id=pipeline_id,
                user_id=user_id,
                workspace_id=workspace_id,
                session_id=session_id,
                control_token=_control_token(request),
            )
        except Exception as exc:
            _raise_pipeline_error(exc)
            raise AssertionError("unreachable")

    @router.post("/start")
    async def start_pipeline(
        command: ExtensionPipelineStartCommand,
        request: Request,
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                coordinator.start,
                **command.model_dump(
                    mode="python", exclude={"schema_version"}
                ),
                control_token=_control_token(request),
            )
        except Exception as exc:
            _raise_pipeline_error(exc)
            raise AssertionError("unreachable")

    @router.post("/{pipeline_id}/advance")
    async def advance_pipeline(
        command: ExtensionPipelineAdvanceCommand,
        request: Request,
        pipeline_id: str = Path(pattern=_PIPELINE_ID_PATTERN),
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                coordinator.advance,
                pipeline_id=pipeline_id,
                **command.model_dump(
                    mode="python", exclude={"schema_version"}
                ),
                control_token=_control_token(request),
            )
        except Exception as exc:
            _raise_pipeline_error(exc)
            raise AssertionError("unreachable")

    return router


__all__ = ["build_phase6_extension_pipelines_router"]
