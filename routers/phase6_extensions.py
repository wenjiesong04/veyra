from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from starlette.concurrency import run_in_threadpool

from interface.extension_spec import ExtensionSpec
from routers.private_control_plane import PrivateControlPlaneRoute
from runtime.extension_spec_quarantine import (
    ExtensionSpecConflictError,
    ExtensionSpecNotFoundError,
    ExtensionSpecQuarantine,
    ExtensionSpecQuarantineError,
    ExtensionSpecStorageError,
)
from runtime.extension_artifact_quarantine import (
    ExtensionArtifactError,
    ExtensionArtifactQuarantine,
)
from runtime.extension_source_policy_gate import (
    ExtensionSourceCheckError,
    ExtensionSourcePolicyGate,
)


_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$"
_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_ARTIFACT_PROJECTION_FIELDS = (
    "artifact_id",
    "artifact_status",
    "artifact_integrity_status",
    "source_syntax_status",
    "static_checks_status",
    "behavior_verification_status",
    "signature_status",
    "execution_status",
    "activation_status",
    "capability_registry_visible",
    "promotion_authorized",
)
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


def _unavailable_artifact_projection() -> dict[str, Any]:
    return {
        "artifact_status": "unavailable",
        "artifact_integrity_status": "unavailable",
        "source_syntax_status": "not_checked",
        "static_checks_status": "not_started",
        "behavior_verification_status": "not_started",
        "signature_status": "not_implemented",
        "execution_status": "not_started",
        "activation_status": "not_installed",
        "capability_registry_visible": False,
        "promotion_authorized": False,
        "candidate_binding_status": "unavailable",
        "operational_health": "fail_closed",
    }


def _merge_artifact_projection(
    candidate: dict[str, Any],
    projection: dict[str, Any],
    source_check_projection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    public = dict(candidate)
    for field in _ARTIFACT_PROJECTION_FIELDS:
        if field in projection:
            public[field] = projection[field]
    public["artifact_projection"] = dict(projection)
    if source_check_projection is not None:
        for field in _SOURCE_CHECK_PROJECTION_FIELDS:
            if field in source_check_projection:
                public[field] = source_check_projection[field]
        public["source_check_operational_health"] = (
            source_check_projection.get(
                "operational_health",
                "fail_closed",
            )
        )
        public["source_check_projection"] = dict(
            source_check_projection
        )
    return public


def _source_check_projection(
    artifact_projection: dict[str, Any],
    *,
    user_id: str,
    workspace_id: str,
    source_check_gate: ExtensionSourcePolicyGate,
) -> dict[str, Any]:
    artifact_id = artifact_projection.get("artifact_id")
    if not isinstance(artifact_id, str):
        if artifact_projection.get("artifact_status") == "not_submitted":
            return source_check_gate.empty_projection()
        return source_check_gate.unavailable_projection()
    try:
        return source_check_gate.projection_for_artifact(
            artifact_id=artifact_id,
            user_id=user_id,
            workspace_id=workspace_id,
        )
    except ExtensionSourceCheckError:
        return source_check_gate.unavailable_projection()


def _with_artifact_projection(
    candidate: dict[str, Any],
    *,
    user_id: str,
    workspace_id: str,
    artifact_quarantine: ExtensionArtifactQuarantine | None,
    source_check_gate: ExtensionSourcePolicyGate | None,
) -> dict[str, Any]:
    if artifact_quarantine is None:
        return candidate
    try:
        projection = artifact_quarantine.projection_for_candidate(
            candidate_id=str(candidate["candidate_id"]),
            user_id=user_id,
            workspace_id=workspace_id,
        )
    except ExtensionArtifactError:
        projection = _unavailable_artifact_projection()
    source_projection = (
        _source_check_projection(
            projection,
            user_id=user_id,
            workspace_id=workspace_id,
            source_check_gate=source_check_gate,
        )
        if source_check_gate is not None
        else None
    )
    return _merge_artifact_projection(
        candidate,
        projection,
        source_projection,
    )


def _with_artifact_projections(
    candidates: list[dict[str, Any]],
    *,
    user_id: str,
    workspace_id: str,
    artifact_quarantine: ExtensionArtifactQuarantine,
    source_check_gate: ExtensionSourcePolicyGate | None,
) -> list[dict[str, Any]]:
    candidate_ids = [
        str(candidate["candidate_id"]) for candidate in candidates
    ]
    try:
        projections = artifact_quarantine.projections_for_candidates(
            candidate_ids=candidate_ids,
            user_id=user_id,
            workspace_id=workspace_id,
        )
    except ExtensionArtifactError:
        projections = {
            candidate_id: _unavailable_artifact_projection()
            for candidate_id in candidate_ids
        }
    source_projections: dict[str, dict[str, Any]] = {}
    if source_check_gate is not None:
        artifact_ids = sorted(
            {
                str(projection["artifact_id"])
                for projection in projections.values()
                if isinstance(projection.get("artifact_id"), str)
            }
        )
        try:
            by_artifact = source_check_gate.projections_for_artifacts(
                artifact_ids=artifact_ids,
                user_id=user_id,
                workspace_id=workspace_id,
            )
        except ExtensionSourceCheckError:
            by_artifact = {
                artifact_id: source_check_gate.unavailable_projection()
                for artifact_id in artifact_ids
            }
        for candidate_id, projection in projections.items():
            artifact_id = projection.get("artifact_id")
            if isinstance(artifact_id, str):
                source_projections[candidate_id] = by_artifact[
                    artifact_id
                ]
            elif projection.get("artifact_status") == "not_submitted":
                source_projections[candidate_id] = (
                    source_check_gate.empty_projection()
                )
            else:
                source_projections[candidate_id] = (
                    source_check_gate.unavailable_projection()
                )
    return [
        _merge_artifact_projection(
            candidate,
            projections[str(candidate["candidate_id"])],
            source_projections.get(str(candidate["candidate_id"])),
        )
        for candidate in candidates
    ]


class ExtensionSpecQuarantineRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    schema_version: Literal[
        "veyra.phase6.extension_spec_quarantine_command.v1"
    ]
    operation_id: str = Field(
        min_length=1,
        max_length=240,
        pattern=_ID_PATTERN,
    )
    user_id: str = Field(min_length=1, max_length=240)
    workspace_id: str = Field(min_length=1, max_length=240)
    expected_spec_digest: str = Field(pattern=_DIGEST_PATTERN)
    spec: ExtensionSpec


class ExtensionSpecReviewRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    schema_version: Literal[
        "veyra.phase6.extension_spec_review_command.v1"
    ]
    operation_id: str = Field(
        min_length=1,
        max_length=240,
        pattern=_ID_PATTERN,
    )
    user_id: str = Field(min_length=1, max_length=240)
    workspace_id: str = Field(min_length=1, max_length=240)
    expected_revision: StrictInt = Field(ge=1)
    decision: Literal[
        "accept_for_future_isolated_generation",
        "reject",
    ]
    reason: str = Field(min_length=1, max_length=1_200)


class ExtensionSpecRevokeRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    schema_version: Literal[
        "veyra.phase6.extension_spec_revoke_command.v1"
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


def _raise_extension_error(exc: Exception) -> None:
    if isinstance(exc, ExtensionSpecNotFoundError):
        raise HTTPException(
            status_code=404,
            detail="ExtensionSpec candidate not found",
        )
    if isinstance(exc, ExtensionSpecStorageError):
        raise HTTPException(
            status_code=503,
            detail="ExtensionSpec quarantine is unavailable",
        )
    if isinstance(exc, ExtensionSpecConflictError):
        raise HTTPException(
            status_code=409,
            detail=str(exc)[:600],
        )
    if isinstance(exc, (TypeError, ValueError)):
        raise HTTPException(
            status_code=422,
            detail="invalid ExtensionSpec request",
        )
    if isinstance(exc, ExtensionSpecQuarantineError):
        raise HTTPException(
            status_code=409,
            detail=str(exc)[:600],
        )
    raise exc


def build_phase6_extensions_router(
    *,
    quarantine: ExtensionSpecQuarantine,
    artifact_quarantine: ExtensionArtifactQuarantine | None = None,
    source_check_gate: ExtensionSourcePolicyGate | None = None,
) -> APIRouter:
    router = APIRouter(
        prefix="/phase6/extensions",
        tags=["phase6-extensions"],
        route_class=PrivateControlPlaneRoute,
    )

    @router.get("/status")
    async def extension_status() -> dict[str, Any]:
        status = await run_in_threadpool(quarantine.status)
        if artifact_quarantine is not None:
            status = {
                **status,
                "artifact_quarantine": await run_in_threadpool(
                    artifact_quarantine.status
                ),
            }
        if source_check_gate is not None:
            status = {
                **status,
                "source_check_gate": await run_in_threadpool(
                    source_check_gate.status
                ),
            }
        return status

    @router.get("/specs")
    async def list_extension_specs(
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
            if artifact_quarantine is None:
                return result
            candidates = await run_in_threadpool(
                _with_artifact_projections,
                result["candidates"],
                user_id=user_id,
                workspace_id=workspace_id,
                artifact_quarantine=artifact_quarantine,
                source_check_gate=source_check_gate,
            )
            return {
                **result,
                "candidates": candidates,
            }
        except Exception as exc:
            _raise_extension_error(exc)
            raise AssertionError("unreachable")

    @router.get("/specs/{candidate_id}")
    async def get_extension_spec(
        candidate_id: str,
        user_id: str = Query(min_length=1, max_length=240),
        workspace_id: str = Query(min_length=1, max_length=240),
    ) -> dict[str, Any]:
        try:
            result = await run_in_threadpool(
                quarantine.get,
                candidate_id=candidate_id,
                user_id=user_id,
                workspace_id=workspace_id,
            )
            return await run_in_threadpool(
                _with_artifact_projection,
                result,
                user_id=user_id,
                workspace_id=workspace_id,
                artifact_quarantine=artifact_quarantine,
                source_check_gate=source_check_gate,
            )
        except Exception as exc:
            _raise_extension_error(exc)
            raise AssertionError("unreachable")

    @router.get("/specs/{candidate_id}/integrity")
    async def extension_spec_integrity(
        candidate_id: str,
        user_id: str = Query(min_length=1, max_length=240),
        workspace_id: str = Query(min_length=1, max_length=240),
    ) -> dict[str, Any]:
        try:
            result = await run_in_threadpool(
                quarantine.integrity,
                candidate_id=candidate_id,
                user_id=user_id,
                workspace_id=workspace_id,
            )
            return await run_in_threadpool(
                _with_artifact_projection,
                result,
                user_id=user_id,
                workspace_id=workspace_id,
                artifact_quarantine=artifact_quarantine,
                source_check_gate=source_check_gate,
            )
        except Exception as exc:
            _raise_extension_error(exc)
            raise AssertionError("unreachable")

    @router.post("/specs")
    async def quarantine_extension_spec(
        request: ExtensionSpecQuarantineRequest,
    ) -> dict[str, Any]:
        try:
            result = await run_in_threadpool(
                quarantine.quarantine,
                spec=request.spec,
                expected_spec_digest=request.expected_spec_digest,
                user_id=request.user_id,
                workspace_id=request.workspace_id,
                operation_id=request.operation_id,
            )
            return await run_in_threadpool(
                _with_artifact_projection,
                result,
                user_id=request.user_id,
                workspace_id=request.workspace_id,
                artifact_quarantine=artifact_quarantine,
                source_check_gate=source_check_gate,
            )
        except Exception as exc:
            _raise_extension_error(exc)
            raise AssertionError("unreachable")

    @router.post("/specs/{candidate_id}/review")
    async def review_extension_spec(
        candidate_id: str,
        request: ExtensionSpecReviewRequest,
    ) -> dict[str, Any]:
        try:
            result = await run_in_threadpool(
                quarantine.review,
                candidate_id=candidate_id,
                user_id=request.user_id,
                workspace_id=request.workspace_id,
                expected_revision=request.expected_revision,
                operation_id=request.operation_id,
                decision=request.decision,
                reason=request.reason,
            )
            return await run_in_threadpool(
                _with_artifact_projection,
                result,
                user_id=request.user_id,
                workspace_id=request.workspace_id,
                artifact_quarantine=artifact_quarantine,
                source_check_gate=source_check_gate,
            )
        except Exception as exc:
            _raise_extension_error(exc)
            raise AssertionError("unreachable")

    @router.post("/specs/{candidate_id}/revoke")
    async def revoke_extension_spec(
        candidate_id: str,
        request: ExtensionSpecRevokeRequest,
    ) -> dict[str, Any]:
        try:
            result = await run_in_threadpool(
                quarantine.revoke,
                candidate_id=candidate_id,
                user_id=request.user_id,
                workspace_id=request.workspace_id,
                expected_revision=request.expected_revision,
                operation_id=request.operation_id,
                reason=request.reason,
            )
            return await run_in_threadpool(
                _with_artifact_projection,
                result,
                user_id=request.user_id,
                workspace_id=request.workspace_id,
                artifact_quarantine=artifact_quarantine,
                source_check_gate=source_check_gate,
            )
        except Exception as exc:
            _raise_extension_error(exc)
            raise AssertionError("unreachable")

    return router


__all__ = [
    "ExtensionSpecQuarantineRequest",
    "ExtensionSpecReviewRequest",
    "ExtensionSpecRevokeRequest",
    "build_phase6_extensions_router",
]
