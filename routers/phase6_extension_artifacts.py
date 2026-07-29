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
    ExtensionArtifactQuarantine,
    ExtensionArtifactStorageError,
)
from runtime.extension_spec_quarantine import (
    ExtensionSpecConflictError,
    ExtensionSpecNotFoundError,
    ExtensionSpecStorageError,
)
from runtime.extension_source_policy_gate import (
    ExtensionSourceCheckError,
    ExtensionSourcePolicyGate,
)


_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$"
_ARTIFACT_ID_PATTERN = r"^extart_[0-9a-f]{24}$"
_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_SOURCE_CHECK_PROJECTION_FIELDS = (
    "source_check_id",
    "source_check_status",
    "source_check_effective_status",
    "source_syntax_status",
    "static_checks_status",
    "static_security_policy_status",
    "isolated_generation_status",
    "unit_checks_status",
    "contract_checks_status",
    "security_runtime_checks_status",
    "fuzz_checks_status",
    "test_execution_status",
    "behavior_verification_status",
    "execution_status",
    "signature_status",
    "activation_status",
    "capability_registry_visible",
    "promotion_authorized",
)


def _merge_source_check_projection(
    artifact: dict[str, Any],
    projection: dict[str, Any],
) -> dict[str, Any]:
    public = dict(artifact)
    for field in _SOURCE_CHECK_PROJECTION_FIELDS:
        if field in projection:
            public[field] = projection[field]
    public["source_check_operational_health"] = projection.get(
        "operational_health",
        "fail_closed",
    )
    public["source_check_projection"] = dict(projection)
    return public


def _with_source_check_projection(
    artifact: dict[str, Any],
    *,
    user_id: str,
    workspace_id: str,
    source_check_gate: ExtensionSourcePolicyGate | None,
) -> dict[str, Any]:
    if source_check_gate is None:
        return artifact
    try:
        projection = source_check_gate.projection_for_artifact(
            artifact_id=str(artifact["artifact_id"]),
            user_id=user_id,
            workspace_id=workspace_id,
        )
    except ExtensionSourceCheckError:
        projection = source_check_gate.unavailable_projection()
    return _merge_source_check_projection(artifact, projection)


def _with_source_check_projections(
    artifacts: list[dict[str, Any]],
    *,
    user_id: str,
    workspace_id: str,
    source_check_gate: ExtensionSourcePolicyGate,
) -> list[dict[str, Any]]:
    artifact_ids = [
        str(artifact["artifact_id"]) for artifact in artifacts
    ]
    try:
        projections = source_check_gate.projections_for_artifacts(
            artifact_ids=artifact_ids,
            user_id=user_id,
            workspace_id=workspace_id,
        )
    except ExtensionSourceCheckError:
        projections = {
            artifact_id: source_check_gate.unavailable_projection()
            for artifact_id in artifact_ids
        }
    return [
        _merge_source_check_projection(
            artifact,
            projections[str(artifact["artifact_id"])],
        )
        for artifact in artifacts
    ]


class ExtensionArtifactQuarantineRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    schema_version: Literal[
        "veyra.phase6.extension_artifact_quarantine_command.v1"
    ]
    operation_id: str = Field(
        min_length=1,
        max_length=240,
        pattern=_ID_PATTERN,
    )
    user_id: str = Field(min_length=1, max_length=240)
    workspace_id: str = Field(min_length=1, max_length=240)
    expected_artifact_sha256: str = Field(pattern=_DIGEST_PATTERN)
    artifact: dict[str, Any]


class ExtensionArtifactTerminalRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    schema_version: Literal[
        "veyra.phase6.extension_artifact_terminal_command.v1"
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


def _raise_artifact_error(exc: Exception) -> None:
    if isinstance(
        exc,
        (ExtensionArtifactNotFoundError, ExtensionSpecNotFoundError),
    ):
        raise HTTPException(
            status_code=404,
            detail="extension artifact or prerequisite not found",
        )
    if isinstance(
        exc,
        (ExtensionArtifactStorageError, ExtensionSpecStorageError),
    ):
        raise HTTPException(
            status_code=503,
            detail="extension artifact quarantine is unavailable",
        )
    if isinstance(
        exc,
        (ExtensionArtifactConflictError, ExtensionSpecConflictError),
    ):
        raise HTTPException(
            status_code=409,
            detail=str(exc)[:600],
        )
    if isinstance(exc, (TypeError, ValueError)):
        raise HTTPException(
            status_code=422,
            detail="invalid extension artifact request",
        )
    if isinstance(exc, ExtensionArtifactError):
        raise HTTPException(
            status_code=409,
            detail=str(exc)[:600],
        )
    raise exc


def build_phase6_extension_artifacts_router(
    *,
    quarantine: ExtensionArtifactQuarantine,
    source_check_gate: ExtensionSourcePolicyGate | None = None,
) -> APIRouter:
    router = APIRouter(
        prefix="/phase6/extensions/artifacts",
        tags=["phase6-extension-artifacts"],
        route_class=PrivateControlPlaneRoute,
    )

    @router.get("/status")
    async def artifact_status() -> dict[str, Any]:
        status = await run_in_threadpool(quarantine.status)
        if source_check_gate is not None:
            status = {
                **status,
                "source_check_gate": await run_in_threadpool(
                    source_check_gate.status
                ),
            }
        return status

    @router.get("")
    async def list_artifacts(
        user_id: str = Query(min_length=1, max_length=240),
        workspace_id: str = Query(min_length=1, max_length=240),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        try:
            result = await run_in_threadpool(
                quarantine.list,
                user_id=user_id,
                workspace_id=workspace_id,
                limit=limit,
            )
            if source_check_gate is None:
                return result
            artifacts = await run_in_threadpool(
                _with_source_check_projections,
                result["artifacts"],
                user_id=user_id,
                workspace_id=workspace_id,
                source_check_gate=source_check_gate,
            )
            return {**result, "artifacts": artifacts}
        except Exception as exc:
            _raise_artifact_error(exc)
            raise AssertionError("unreachable")

    @router.get("/{artifact_id}")
    async def get_artifact(
        artifact_id: str = Path(pattern=_ARTIFACT_ID_PATTERN),
        user_id: str = Query(min_length=1, max_length=240),
        workspace_id: str = Query(min_length=1, max_length=240),
    ) -> dict[str, Any]:
        try:
            result = await run_in_threadpool(
                quarantine.get,
                artifact_id=artifact_id,
                user_id=user_id,
                workspace_id=workspace_id,
            )
            return await run_in_threadpool(
                _with_source_check_projection,
                result,
                user_id=user_id,
                workspace_id=workspace_id,
                source_check_gate=source_check_gate,
            )
        except Exception as exc:
            _raise_artifact_error(exc)
            raise AssertionError("unreachable")

    @router.get("/{artifact_id}/integrity")
    async def artifact_integrity(
        artifact_id: str = Path(pattern=_ARTIFACT_ID_PATTERN),
        user_id: str = Query(min_length=1, max_length=240),
        workspace_id: str = Query(min_length=1, max_length=240),
    ) -> dict[str, Any]:
        try:
            result = await run_in_threadpool(
                quarantine.integrity,
                artifact_id=artifact_id,
                user_id=user_id,
                workspace_id=workspace_id,
            )
            return await run_in_threadpool(
                _with_source_check_projection,
                result,
                user_id=user_id,
                workspace_id=workspace_id,
                source_check_gate=source_check_gate,
            )
        except Exception as exc:
            _raise_artifact_error(exc)
            raise AssertionError("unreachable")

    @router.post("")
    async def quarantine_artifact(
        request: ExtensionArtifactQuarantineRequest,
    ) -> dict[str, Any]:
        try:
            result = await run_in_threadpool(
                quarantine.submit,
                envelope=request.artifact,
                expected_artifact_sha256=(
                    request.expected_artifact_sha256
                ),
                user_id=request.user_id,
                workspace_id=request.workspace_id,
                operation_id=request.operation_id,
            )
            return await run_in_threadpool(
                _with_source_check_projection,
                result,
                user_id=request.user_id,
                workspace_id=request.workspace_id,
                source_check_gate=source_check_gate,
            )
        except Exception as exc:
            _raise_artifact_error(exc)
            raise AssertionError("unreachable")

    @router.post("/{artifact_id}/reject")
    async def reject_artifact(
        request: ExtensionArtifactTerminalRequest,
        artifact_id: str = Path(pattern=_ARTIFACT_ID_PATTERN),
    ) -> dict[str, Any]:
        try:
            result = await run_in_threadpool(
                quarantine.reject,
                artifact_id=artifact_id,
                user_id=request.user_id,
                workspace_id=request.workspace_id,
                expected_revision=request.expected_revision,
                operation_id=request.operation_id,
                reason=request.reason,
            )
            return await run_in_threadpool(
                _with_source_check_projection,
                result,
                user_id=request.user_id,
                workspace_id=request.workspace_id,
                source_check_gate=source_check_gate,
            )
        except Exception as exc:
            _raise_artifact_error(exc)
            raise AssertionError("unreachable")

    @router.post("/{artifact_id}/revoke")
    async def revoke_artifact(
        request: ExtensionArtifactTerminalRequest,
        artifact_id: str = Path(pattern=_ARTIFACT_ID_PATTERN),
    ) -> dict[str, Any]:
        try:
            result = await run_in_threadpool(
                quarantine.revoke,
                artifact_id=artifact_id,
                user_id=request.user_id,
                workspace_id=request.workspace_id,
                expected_revision=request.expected_revision,
                operation_id=request.operation_id,
                reason=request.reason,
            )
            return await run_in_threadpool(
                _with_source_check_projection,
                result,
                user_id=request.user_id,
                workspace_id=request.workspace_id,
                source_check_gate=source_check_gate,
            )
        except Exception as exc:
            _raise_artifact_error(exc)
            raise AssertionError("unreachable")

    return router


__all__ = [
    "ExtensionArtifactQuarantineRequest",
    "ExtensionArtifactTerminalRequest",
    "build_phase6_extension_artifacts_router",
]
