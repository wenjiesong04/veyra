from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from starlette.concurrency import run_in_threadpool

from routers.private_control_plane import PrivateControlPlaneRoute
from runtime.extension_deployment_gate import (
    ExtensionDeploymentConflictError,
    ExtensionDeploymentError,
    ExtensionDeploymentGate,
    ExtensionDeploymentNotFoundError,
    ExtensionDeploymentStorageError,
    ExtensionDeploymentUnauthorizedError,
    ExtensionDeploymentUnavailableError,
)


_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$"
_DEPLOYMENT_ID_PATTERN = r"^extdep_[0-9a-f]{24}$"
_RELEASE_ID_PATTERN = r"^extrel_[0-9a-f]{24}$"
_DIGEST_PATTERN = r"^[0-9a-f]{64}$"


class _Command(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    operation_id: str = Field(min_length=1, max_length=240, pattern=_ID_PATTERN)


class ExtensionBackendRefreshCommand(_Command):
    schema_version: Literal[
        "veyra.phase6.extension_backend_refresh_command.v1"
    ]
    expected_state_revision: StrictInt = Field(ge=0, le=2_147_483_647)


class ExtensionDeploymentProposalCommand(_Command):
    schema_version: Literal[
        "veyra.phase6.extension_deployment_proposal_command.v1"
    ]
    request_id: str = Field(min_length=1, max_length=240)
    release_id: str = Field(
        min_length=31, max_length=31, pattern=_RELEASE_ID_PATTERN
    )
    user_id: str = Field(min_length=1, max_length=240)
    workspace_id: str = Field(min_length=1, max_length=240)
    session_id: str = Field(min_length=1, max_length=240)
    expected_state_revision: StrictInt = Field(ge=0, le=2_147_483_647)
    expected_release_revision: StrictInt = Field(ge=1, le=2_147_483_647)
    expected_attestation_digest: str = Field(
        min_length=64, max_length=64, pattern=_DIGEST_PATTERN
    )
    expires_at: str = Field(min_length=20, max_length=32)


class ExtensionDeploymentTransitionCommand(_Command):
    schema_version: Literal[
        "veyra.phase6.extension_deployment_transition_command.v1"
    ]
    request_id: str = Field(min_length=1, max_length=240)
    user_id: str = Field(min_length=1, max_length=240)
    workspace_id: str = Field(min_length=1, max_length=240)
    session_id: str = Field(min_length=1, max_length=240)
    target_mode: Literal[
        "shadow", "read_only_canary", "scoped_canary", "promoted"
    ]
    expected_state_revision: StrictInt = Field(ge=0, le=2_147_483_647)
    expected_deployment_revision: StrictInt = Field(ge=1, le=2_147_483_647)
    expected_mode_epoch: StrictInt = Field(ge=0, le=2_147_483_647)
    max_invocations: StrictInt = Field(ge=1, le=100)
    expires_at: str = Field(min_length=20, max_length=32)
    review_id: str | None = Field(default=None, min_length=1, max_length=240)


class ExtensionInvocationCommand(_Command):
    schema_version: Literal[
        "veyra.phase6.extension_invocation_command.v1"
    ]
    request_id: str = Field(min_length=1, max_length=240)
    user_id: str = Field(min_length=1, max_length=240)
    workspace_id: str = Field(min_length=1, max_length=240)
    session_id: str = Field(min_length=1, max_length=240)
    expected_state_revision: StrictInt = Field(ge=0, le=2_147_483_647)
    expected_deployment_revision: StrictInt = Field(ge=1, le=2_147_483_647)
    expected_mode_epoch: StrictInt = Field(ge=1, le=2_147_483_647)
    input_payload: dict[str, Any] = Field(max_length=64)


class ExtensionDeploymentReviewRequestCommand(_Command):
    schema_version: Literal[
        "veyra.phase6.extension_deployment_review_request_command.v1"
    ]
    request_id: str = Field(min_length=1, max_length=240)
    user_id: str = Field(min_length=1, max_length=240)
    workspace_id: str = Field(min_length=1, max_length=240)
    session_id: str = Field(min_length=1, max_length=240)
    target_mode: Literal["scoped_canary", "promoted"]
    expected_state_revision: StrictInt = Field(ge=0, le=2_147_483_647)
    expected_deployment_revision: StrictInt = Field(ge=1, le=2_147_483_647)
    expected_mode_epoch: StrictInt = Field(ge=0, le=2_147_483_647)


class ExtensionDeploymentReviewApprovalCommand(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    schema_version: Literal[
        "veyra.phase6.extension_deployment_review_approval_command.v1"
    ]
    expected_review_revision: StrictInt = Field(ge=1, le=2_147_483_647)
    reason_digest: str = Field(
        min_length=64, max_length=64, pattern=_DIGEST_PATTERN
    )


class ExtensionDeploymentStopCommand(_Command):
    schema_version: Literal[
        "veyra.phase6.extension_deployment_stop_command.v1"
    ]
    user_id: str = Field(min_length=1, max_length=240)
    workspace_id: str = Field(min_length=1, max_length=240)
    session_id: str = Field(min_length=1, max_length=240)
    expected_state_revision: StrictInt = Field(ge=0, le=2_147_483_647)
    expected_deployment_revision: StrictInt = Field(ge=1, le=2_147_483_647)
    expected_mode_epoch: StrictInt = Field(ge=0, le=2_147_483_647)
    reason_digest: str = Field(
        min_length=64, max_length=64, pattern=_DIGEST_PATTERN
    )


def _control_token(request: Request) -> str:
    authorization = str(request.headers.get("authorization") or "").strip()
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return str(request.headers.get("x-veyra-token") or "").strip()


def _approver_token(request: Request) -> str:
    # Deliberately never falls back to Authorization or x-veyra-token. The
    # approval principal must be separate from the control-plane principal.
    return str(
        request.headers.get("x-veyra-extension-approver-token") or ""
    ).strip()


def _raise_deployment_error(exc: Exception) -> None:
    if isinstance(exc, ExtensionDeploymentNotFoundError):
        raise HTTPException(status_code=404, detail="extension deployment not found")
    if isinstance(exc, ExtensionDeploymentUnauthorizedError):
        raise HTTPException(
            status_code=401,
            detail="valid Veyra control token required for extension deployment",
        )
    if isinstance(
        exc,
        (ExtensionDeploymentStorageError, ExtensionDeploymentUnavailableError),
    ):
        raise HTTPException(
            status_code=503, detail="extension deployment lifecycle is unavailable"
        )
    if isinstance(exc, ExtensionDeploymentConflictError):
        raise HTTPException(
            status_code=409, detail="extension deployment request conflicts with state"
        )
    if isinstance(exc, (TypeError, ValueError)):
        raise HTTPException(status_code=422, detail="invalid extension deployment request")
    if isinstance(exc, ExtensionDeploymentError):
        raise HTTPException(status_code=409, detail="extension deployment request rejected")
    raise exc


def build_phase6_extension_deployments_router(
    *, gate: ExtensionDeploymentGate
) -> APIRouter:
    router = APIRouter(
        prefix="/phase6/extensions",
        tags=["phase6-extension-deployments"],
        route_class=PrivateControlPlaneRoute,
    )

    @router.get("/deployments/status")
    async def status(request: Request) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                gate.status, control_token=_control_token(request)
            )
        except Exception as exc:
            _raise_deployment_error(exc)
            raise AssertionError("unreachable")

    @router.get("/deployments")
    async def list_deployments(
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
            _raise_deployment_error(exc)
            raise AssertionError("unreachable")

    @router.get("/deployments/{deployment_id}")
    async def get_deployment(
        request: Request,
        deployment_id: str = Path(pattern=_DEPLOYMENT_ID_PATTERN),
        user_id: str = Query(min_length=1, max_length=240),
        workspace_id: str = Query(min_length=1, max_length=240),
        session_id: str = Query(min_length=1, max_length=240),
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                gate.get,
                deployment_id=deployment_id,
                user_id=user_id,
                workspace_id=workspace_id,
                session_id=session_id,
                control_token=_control_token(request),
            )
        except Exception as exc:
            _raise_deployment_error(exc)
            raise AssertionError("unreachable")

    @router.get("/deployments/{deployment_id}/integrity")
    async def deployment_integrity(
        request: Request,
        deployment_id: str = Path(pattern=_DEPLOYMENT_ID_PATTERN),
        user_id: str = Query(min_length=1, max_length=240),
        workspace_id: str = Query(min_length=1, max_length=240),
        session_id: str = Query(min_length=1, max_length=240),
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                gate.integrity,
                deployment_id=deployment_id,
                user_id=user_id,
                workspace_id=workspace_id,
                session_id=session_id,
                control_token=_control_token(request),
            )
        except Exception as exc:
            _raise_deployment_error(exc)
            raise AssertionError("unreachable")

    @router.get("/public-extension-registry")
    async def public_registry(
        request: Request,
        user_id: str = Query(min_length=1, max_length=240),
        workspace_id: str = Query(min_length=1, max_length=240),
        session_id: str = Query(min_length=1, max_length=240),
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                gate.public_registry,
                user_id=user_id,
                workspace_id=workspace_id,
                session_id=session_id,
                control_token=_control_token(request),
            )
        except Exception as exc:
            _raise_deployment_error(exc)
            raise AssertionError("unreachable")

    @router.post("/deployments/backend/refresh")
    async def refresh_backend(
        command: ExtensionBackendRefreshCommand, request: Request
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                gate.refresh_backend,
                **command.model_dump(mode="python", exclude={"schema_version"}),
                control_token=_control_token(request),
            )
        except Exception as exc:
            _raise_deployment_error(exc)
            raise AssertionError("unreachable")

    @router.post("/deployments/proposals")
    async def propose(
        command: ExtensionDeploymentProposalCommand, request: Request
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                gate.propose,
                **command.model_dump(mode="python", exclude={"schema_version"}),
                control_token=_control_token(request),
            )
        except Exception as exc:
            _raise_deployment_error(exc)
            raise AssertionError("unreachable")

    @router.post("/deployments/{deployment_id}/transitions")
    async def transition(
        command: ExtensionDeploymentTransitionCommand,
        request: Request,
        deployment_id: str = Path(pattern=_DEPLOYMENT_ID_PATTERN),
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                gate.transition,
                deployment_id=deployment_id,
                **command.model_dump(mode="python", exclude={"schema_version"}),
                control_token=_control_token(request),
            )
        except Exception as exc:
            _raise_deployment_error(exc)
            raise AssertionError("unreachable")

    @router.post("/deployments/{deployment_id}/reviews")
    async def request_transition_review(
        command: ExtensionDeploymentReviewRequestCommand,
        request: Request,
        deployment_id: str = Path(pattern=_DEPLOYMENT_ID_PATTERN),
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                gate.request_transition_review,
                deployment_id=deployment_id,
                **command.model_dump(mode="python", exclude={"schema_version"}),
                control_token=_control_token(request),
            )
        except Exception as exc:
            _raise_deployment_error(exc)
            raise AssertionError("unreachable")

    @router.post("/deployment-reviews/{review_id}/approve")
    async def approve_transition_review(
        command: ExtensionDeploymentReviewApprovalCommand,
        request: Request,
        review_id: str = Path(min_length=1, max_length=240),
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                gate.approve_transition_review,
                review_id=review_id,
                **command.model_dump(mode="python", exclude={"schema_version"}),
                approver_token=_approver_token(request),
            )
        except Exception as exc:
            _raise_deployment_error(exc)
            raise AssertionError("unreachable")

    @router.post("/deployments/{deployment_id}/invocations")
    async def invoke(
        command: ExtensionInvocationCommand,
        request: Request,
        deployment_id: str = Path(pattern=_DEPLOYMENT_ID_PATTERN),
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                gate.invoke,
                deployment_id=deployment_id,
                **command.model_dump(mode="python", exclude={"schema_version"}),
                control_token=_control_token(request),
            )
        except Exception as exc:
            _raise_deployment_error(exc)
            raise AssertionError("unreachable")

    @router.post("/deployments/{deployment_id}/disable")
    async def disable(
        command: ExtensionDeploymentStopCommand,
        request: Request,
        deployment_id: str = Path(pattern=_DEPLOYMENT_ID_PATTERN),
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                gate.disable,
                deployment_id=deployment_id,
                **command.model_dump(mode="python", exclude={"schema_version"}),
                control_token=_control_token(request),
            )
        except Exception as exc:
            _raise_deployment_error(exc)
            raise AssertionError("unreachable")

    @router.post("/deployments/{deployment_id}/rollback")
    async def rollback(
        command: ExtensionDeploymentStopCommand,
        request: Request,
        deployment_id: str = Path(pattern=_DEPLOYMENT_ID_PATTERN),
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                gate.rollback,
                deployment_id=deployment_id,
                **command.model_dump(mode="python", exclude={"schema_version"}),
                control_token=_control_token(request),
            )
        except Exception as exc:
            _raise_deployment_error(exc)
            raise AssertionError("unreachable")

    @router.post("/deployments/{deployment_id}/reconcile-release")
    async def reconcile(
        command: ExtensionDeploymentStopCommand,
        request: Request,
        deployment_id: str = Path(pattern=_DEPLOYMENT_ID_PATTERN),
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                gate.reconcile_release,
                deployment_id=deployment_id,
                **command.model_dump(mode="python", exclude={"schema_version"}),
                control_token=_control_token(request),
            )
        except Exception as exc:
            _raise_deployment_error(exc)
            raise AssertionError("unreachable")

    return router


__all__ = [
    "ExtensionBackendRefreshCommand",
    "ExtensionDeploymentProposalCommand",
    "ExtensionDeploymentReviewApprovalCommand",
    "ExtensionDeploymentReviewRequestCommand",
    "ExtensionDeploymentStopCommand",
    "ExtensionDeploymentTransitionCommand",
    "ExtensionInvocationCommand",
    "build_phase6_extension_deployments_router",
]
