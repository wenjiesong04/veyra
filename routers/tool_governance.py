from __future__ import annotations

import json
from typing import Any, Callable

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator
from starlette.concurrency import run_in_threadpool

from runtime.tool_governance_runtime import (
    ToolGovernanceConflict,
    ToolGovernanceRuntime,
    ToolGovernanceStorageError,
)
from runtime.openclaw_tool_broker import (
    OpenClawHookConflict,
    OpenClawHookDenied,
    OpenClawToolBroker,
    OpenClawToolBrokerError,
    project_current_hook_enforcement,
)
from tool_proxy.governance_contract import ToolInvocation, ToolObservation


class ToolPreflightRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    capability_token: str = Field(min_length=1, max_length=512)
    invocation: ToolInvocation

    @field_validator("invocation", mode="before")
    @classmethod
    def validate_json_contract(cls, value: Any) -> ToolInvocation:
        if isinstance(value, ToolInvocation):
            return value
        return ToolInvocation.model_validate_json(
            json.dumps(value, ensure_ascii=False, allow_nan=False),
            strict=True,
        )


class ToolPostflightRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    reservation_token: str = Field(min_length=1, max_length=512)
    observation: ToolObservation

    @field_validator("observation", mode="before")
    @classmethod
    def validate_json_contract(cls, value: Any) -> ToolObservation:
        if isinstance(value, ToolObservation):
            return value
        return ToolObservation.model_validate_json(
            json.dumps(value, ensure_ascii=False, allow_nan=False),
            strict=True,
        )


class _HookRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        populate_by_name=True,
    )

    run_id: str = Field(alias="runId", min_length=1, max_length=600)
    tool_call_id: str = Field(
        alias="toolCallId",
        min_length=1,
        max_length=600,
    )
    tool_name: str = Field(alias="toolName", min_length=1, max_length=240)


class HookPreflightRequest(_HookRequest):
    session_key: str = Field(
        alias="sessionKey",
        min_length=1,
        max_length=600,
    )
    params: dict[str, JsonValue]


class HookExecuteRequest(_HookRequest):
    params: dict[str, JsonValue]
    reservation_token: str = Field(
        alias="reservationToken",
        min_length=1,
        max_length=512,
    )
    execution_token: str = Field(
        alias="executionToken",
        min_length=1,
        max_length=512,
    )


class HookObservationRequest(_HookRequest):
    outcome: str = Field(min_length=1, max_length=40)
    params_digest: str | None = Field(
        default=None,
        alias="paramsDigest",
        min_length=64,
        max_length=64,
    )
    result_digest: str | None = Field(
        default=None,
        alias="resultDigest",
        min_length=64,
        max_length=64,
    )
    duration_ms: int | None = Field(
        default=None,
        alias="durationMs",
        ge=0,
        le=86_400_000,
    )
    reason: str = Field(default="", max_length=600)


class HookCanaryAttestationRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        populate_by_name=True,
    )

    run_id: str = Field(alias="runId", min_length=1, max_length=600)
    session_key: str = Field(alias="sessionKey", min_length=1, max_length=600)
    sentinel_relative_path: str = Field(
        alias="sentinelRelativePath",
        min_length=1,
        max_length=4096,
    )
    native_block_path: str = Field(
        alias="nativeBlockPath",
        min_length=1,
        max_length=4096,
    )
    native_block_content: str = Field(
        alias="nativeBlockContent",
        min_length=1,
        max_length=1024,
    )
    plugin_protocol: str = Field(
        alias="pluginProtocol",
        min_length=1,
        max_length=240,
    )
    plugin_implementation_revision: str = Field(
        alias="pluginImplementationRevision",
        min_length=1,
        max_length=240,
    )


def build_tool_governance_router(
    runtime: ToolGovernanceRuntime,
    broker: OpenClawToolBroker | None = None,
    plugin_status_resolver: Callable[[], dict[str, Any]] | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/tool-governance")

    async def current_hook_status() -> dict[str, Any]:
        if broker is None:
            return {
                "status": "not_configured",
                "tool_proxy_enforced": False,
            }
        snapshot = await run_in_threadpool(broker.status)
        agent_status: dict[str, Any] | None = None
        if plugin_status_resolver is not None:
            try:
                agent_status = await run_in_threadpool(
                    plugin_status_resolver
                )
            except Exception as exc:
                agent_status = {
                    "connected": False,
                    "status": "unavailable",
                    "reason": str(exc)[:600],
                }
        return project_current_hook_enforcement(
            snapshot,
            agent_status,
        )

    @router.get("/status")
    async def tool_governance_status() -> dict[str, Any]:
        contract_status = await run_in_threadpool(runtime.status)
        if broker is None:
            return contract_status
        hook_status = await current_hook_status()
        enforced = hook_status.get("tool_proxy_enforced") is True
        return {
            **contract_status,
            "status": (
                "validated"
                if enforced
                else hook_status.get("status", "validation_pending")
            ),
            "phase": (
                "scoped_enforcement"
                if enforced
                else "contract_only"
            ),
            "hook_enforcement": hook_status,
            "tool_proxy_enforced": enforced,
            "execution_authority_enabled": hook_status.get(
                "execution_authority_enabled"
            )
            is True,
            "scope": hook_status.get("scope"),
        }

    @router.get("/hook/status")
    async def hook_status() -> dict[str, Any]:
        if broker is None:
            raise HTTPException(
                status_code=503,
                detail="OpenClaw hook broker is not configured",
            )
        return await current_hook_status()

    @router.post("/preflight")
    async def tool_governance_preflight(
        request: ToolPreflightRequest,
    ) -> dict[str, Any]:
        try:
            result = await run_in_threadpool(
                runtime.preflight,
                request.invocation,
                capability_token=request.capability_token,
            )
            return result.public_dict()
        except ToolGovernanceStorageError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.post("/postflight")
    async def tool_governance_postflight(
        request: ToolPostflightRequest,
    ) -> dict[str, Any]:
        try:
            receipt = await run_in_threadpool(
                runtime.postflight,
                request.observation,
                reservation_token=request.reservation_token,
            )
            return {"receipt": receipt.model_dump(mode="json")}
        except ToolGovernanceConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ToolGovernanceStorageError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.post("/hook/preflight")
    async def hook_preflight(
        request: HookPreflightRequest,
        x_veyra_dispatch_token: str = Header(
            min_length=1,
            max_length=512,
        ),
    ) -> dict[str, Any]:
        if broker is None:
            raise HTTPException(
                status_code=503,
                detail="OpenClaw hook broker is not configured",
            )
        try:
            return await run_in_threadpool(
                broker.preflight,
                run_id=request.run_id,
                session_key=request.session_key,
                tool_call_id=request.tool_call_id,
                tool_name=request.tool_name,
                params=request.params,
                dispatch_token=x_veyra_dispatch_token,
            )
        except (OpenClawToolBrokerError, ToolGovernanceStorageError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.post("/hook/execute")
    async def hook_execute(
        request: HookExecuteRequest,
        x_veyra_dispatch_token: str = Header(
            min_length=1,
            max_length=512,
        ),
    ) -> dict[str, Any]:
        if broker is None:
            raise HTTPException(
                status_code=503,
                detail="OpenClaw hook broker is not configured",
            )
        try:
            return await run_in_threadpool(
                broker.execute,
                run_id=request.run_id,
                tool_call_id=request.tool_call_id,
                tool_name=request.tool_name,
                params=request.params,
                dispatch_token=x_veyra_dispatch_token,
                reservation_token=request.reservation_token,
                execution_token=request.execution_token,
            )
        except OpenClawHookDenied as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except (OpenClawHookConflict, ToolGovernanceConflict) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (
            OpenClawToolBrokerError,
            ToolGovernanceStorageError,
        ) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.post("/hook/observe")
    async def hook_observe(
        request: HookObservationRequest,
        x_veyra_dispatch_token: str = Header(
            min_length=1,
            max_length=512,
        ),
    ) -> dict[str, Any]:
        if broker is None:
            raise HTTPException(
                status_code=503,
                detail="OpenClaw hook broker is not configured",
            )
        try:
            return await run_in_threadpool(
                broker.observe,
                run_id=request.run_id,
                tool_call_id=request.tool_call_id,
                tool_name=request.tool_name,
                dispatch_token=x_veyra_dispatch_token,
                outcome=request.outcome,
                params_digest=request.params_digest,
                result_digest=request.result_digest,
                duration_ms=request.duration_ms,
                reason=request.reason,
            )
        except OpenClawHookDenied as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except OpenClawHookConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except OpenClawToolBrokerError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.post("/hook/canary/attest")
    async def hook_canary_attest(
        request: HookCanaryAttestationRequest,
        x_veyra_dispatch_token: str = Header(
            min_length=1,
            max_length=512,
        ),
    ) -> dict[str, Any]:
        if broker is None:
            raise HTTPException(
                status_code=503,
                detail="OpenClaw hook broker is not configured",
            )
        try:
            return await run_in_threadpool(
                broker.mark_canary_validated,
                run_id=request.run_id,
                session_key=request.session_key,
                sentinel_relative_path=request.sentinel_relative_path,
                native_block_path=request.native_block_path,
                native_block_content=request.native_block_content,
                plugin_protocol=request.plugin_protocol,
                plugin_implementation_revision=(
                    request.plugin_implementation_revision
                ),
                dispatch_token=x_veyra_dispatch_token,
            )
        except OpenClawHookDenied as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except OpenClawHookConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return router


__all__ = [
    "ToolPostflightRequest",
    "ToolPreflightRequest",
    "HookExecuteRequest",
    "HookObservationRequest",
    "HookPreflightRequest",
    "build_tool_governance_router",
]
