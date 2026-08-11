from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from interface.structured_observation import (
    ComponentHealthObservationRequest,
    StructuredObservationCommand,
)
from routers.private_control_plane import PrivateControlPlaneRoute
from runtime.structured_observation_ingress import (
    StructuredObservationConflictError,
    StructuredObservationIngress,
    StructuredObservationIngressError,
    StructuredObservationUnauthorizedError,
    StructuredObservationUnavailableError,
)


def _control_token(request: Request) -> str:
    authorization = str(request.headers.get("authorization") or "").strip()
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return str(request.headers.get("x-veyra-token") or "").strip()


def _raise_ingress_error(exc: Exception) -> None:
    if isinstance(exc, StructuredObservationUnauthorizedError):
        raise HTTPException(
            status_code=401,
            detail="valid Veyra control token required",
        )
    if isinstance(exc, StructuredObservationUnavailableError):
        raise HTTPException(
            status_code=503,
            detail="structured observation ingress is unavailable",
        )
    if isinstance(exc, StructuredObservationConflictError):
        raise HTTPException(
            status_code=409,
            detail="structured observation conflicts with current state",
        )
    if isinstance(exc, (TypeError, ValueError)):
        raise HTTPException(
            status_code=422,
            detail="invalid structured observation command",
        )
    if isinstance(exc, StructuredObservationIngressError):
        raise HTTPException(
            status_code=409,
            detail="structured observation was rejected",
        )
    raise exc


def build_structured_observations_router(
    *,
    ingress: StructuredObservationIngress,
) -> APIRouter:
    router = APIRouter(
        prefix="/awareness/structured-observations",
        tags=["structured-observations"],
        route_class=PrivateControlPlaneRoute,
    )

    @router.get("/status")
    async def structured_observation_status() -> dict[str, Any]:
        return await run_in_threadpool(ingress.status)

    @router.post("")
    async def submit_structured_observation(
        request: Request,
        command: StructuredObservationCommand,
    ) -> dict[str, Any]:
        if command.producer_id != "local_operator":
            raise HTTPException(
                status_code=409,
                detail="structured observation producer is not available over HTTP",
            )
        try:
            return await run_in_threadpool(
                ingress.submit,
                command,
                control_token=_control_token(request),
            )
        except Exception as exc:
            _raise_ingress_error(exc)
            raise AssertionError("unreachable")

    @router.post("/component-health")
    async def submit_component_health_observation(
        request: Request,
        command: ComponentHealthObservationRequest,
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                ingress.submit_component_health,
                command,
                control_token=_control_token(request),
            )
        except Exception as exc:
            _raise_ingress_error(exc)
            raise AssertionError("unreachable")

    return router


__all__ = ["build_structured_observations_router"]
