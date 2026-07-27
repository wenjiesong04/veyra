from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.concurrency import run_in_threadpool

from runtime.tool_governance_runtime import (
    ToolGovernanceConflict,
    ToolGovernanceRuntime,
    ToolGovernanceStorageError,
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


def build_tool_governance_router(
    runtime: ToolGovernanceRuntime,
) -> APIRouter:
    router = APIRouter(prefix="/tool-governance")

    @router.get("/status")
    async def tool_governance_status() -> dict[str, Any]:
        return await run_in_threadpool(runtime.status)

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

    return router


__all__ = [
    "ToolPostflightRequest",
    "ToolPreflightRequest",
    "build_tool_governance_router",
]
