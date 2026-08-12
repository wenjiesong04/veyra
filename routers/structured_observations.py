from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from interface.structured_observation import (
    ComponentHealthObservationRequest,
    StructuredObservationCommand,
    TrustedWorkspaceObserverConfigRequest,
    TrustedWorkspaceObserverRunRequest,
)
from routers.private_control_plane import PrivateControlPlaneRoute
from runtime.structured_observation_ingress import (
    StructuredObservationConflictError,
    StructuredObservationIngress,
    StructuredObservationIngressError,
    StructuredObservationUnauthorizedError,
    StructuredObservationUnavailableError,
)
from runtime.trusted_workspace_observer import (
    TrustedWorkspaceObserver,
    TrustedWorkspaceObserverError,
    TrustedWorkspaceObserverUnauthorized,
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
    workspace_observer: TrustedWorkspaceObserver | None = None,
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

    @router.get("/workspace/status")
    async def workspace_observer_status() -> dict[str, Any]:
        if workspace_observer is None:
            return {"status": "not_configured", "mode": "disabled", "authority": TrustedWorkspaceObserver.authority()}
        return await run_in_threadpool(workspace_observer.status)

    @router.post("/workspace/configure")
    async def configure_workspace_observer(
        request: Request,
        command: TrustedWorkspaceObserverConfigRequest,
    ) -> dict[str, Any]:
        if workspace_observer is None:
            raise HTTPException(status_code=503, detail="workspace observer is unavailable")
        try:
            token = _control_token(request)

            def configure_bound_observer() -> dict[str, Any]:
                # GitHub identity binding performs bounded network reads. Keep
                # the complete bind + final CAS off the event loop so another
                # private status request is not stalled by provider latency.
                workspace_observer.authorize_control_token(token)
                ci_binding = None
                ci_fields = (
                    command.github_repo_id,
                    command.github_workflow_path,
                    command.github_required_jobs,
                    command.github_expected_app_id,
                )
                selected_mode = str(command.mode or "").strip().lower()
                if selected_mode == "disabled" and any(
                    value is not None for value in ci_fields
                ):
                    # A disabled observer is deliberately a no-probe path.
                    # Reject provider fields before bind_ci_policy can perform
                    # any network identity reads (and before configure can
                    # resolve a workspace, Git snapshot, or Goal).
                    raise ValueError(
                        "GitHub CI fields are not allowed when observer is disabled"
                    )
                if selected_mode != "disabled" and any(
                    value is not None for value in ci_fields
                ):
                    if not all(value is not None for value in ci_fields):
                        raise ValueError("GitHub CI fields must be configured together")
                    ci_binding = workspace_observer.bind_ci_policy(
                        repo_id=str(command.github_repo_id),
                        workflow_path=str(command.github_workflow_path),
                        required_jobs=list(command.github_required_jobs or []),
                        expected_app_id=int(command.github_expected_app_id),
                    )
                return workspace_observer.configure(
                    control_token=token,
                    expected_state_revision=command.expected_state_revision,
                    mode=command.mode,
                    user_id=command.user_id,
                    session_id=command.session_id,
                    workspace_id=command.workspace_id,
                    goal_id=command.goal_id,
                    ci_binding=ci_binding,
                )

            return await run_in_threadpool(configure_bound_observer)
        except Exception as exc:
            if isinstance(exc, TrustedWorkspaceObserverUnauthorized):
                raise HTTPException(status_code=401, detail="valid Veyra control token required") from exc
            if isinstance(exc, TrustedWorkspaceObserverError):
                raise HTTPException(status_code=409, detail="workspace observer configuration rejected") from exc
            if isinstance(exc, (TypeError, ValueError)):
                raise HTTPException(status_code=422, detail="invalid workspace observer configuration") from exc
            raise

    @router.post("/workspace/run-once")
    async def run_workspace_observer(
        request: Request,
        command: TrustedWorkspaceObserverRunRequest,
    ) -> dict[str, Any]:
        if workspace_observer is None:
            raise HTTPException(status_code=503, detail="workspace observer is unavailable")
        try:
            workspace_observer.authorize_control_token(_control_token(request))
            return await run_in_threadpool(workspace_observer.run_once, reason=command.reason)
        except Exception as exc:
            if isinstance(exc, TrustedWorkspaceObserverUnauthorized):
                raise HTTPException(status_code=401, detail="valid Veyra control token required") from exc
            if isinstance(exc, TrustedWorkspaceObserverError):
                raise HTTPException(status_code=409, detail="workspace observer run rejected") from exc
            raise

    return router


__all__ = ["build_structured_observations_router"]
