from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field


class CommitmentCreateRequest(BaseModel):
    kind: str = "generic_reminder"
    title: str = ""
    user_id: str = "local-user"
    channel: str = "api"
    session_id: str = "local-session"
    status: str = "active"
    schedule: dict[str, Any] = Field(default_factory=dict)
    payload: dict[str, Any] = Field(default_factory=dict)
    risk_level: str = "R0"
    next_run_at: str | None = None


class CommitmentPushRequest(BaseModel):
    limit: int = 10
    reason: str = "manual"


def build_commitments_router(deps: dict[str, Any]) -> APIRouter:
    router = APIRouter()

    @router.get("/commitments")
    async def list_commitments(
        user_id: str | None = None,
        session_id: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        items = deps["commitment_core"].list_commitments(user_id=user_id, session_id=session_id, status=status)
        return {"status": "success", "count": len(items), "commitments": items}

    @router.get("/commitments/{commitment_id}")
    async def get_commitment(commitment_id: str) -> dict[str, Any]:
        item = deps["commitment_core"].get_commitment(commitment_id)
        if not item:
            raise HTTPException(status_code=404, detail="commitment not found")
        return {"status": "success", "commitment": item}

    @router.post("/commitments")
    async def create_commitment(request: CommitmentCreateRequest) -> dict[str, Any]:
        item = deps["commitment_core"].create_commitment(request.model_dump(exclude_none=True))
        return {"status": "success", "commitment": item}

    @router.post("/commitments/{commitment_id}/confirm")
    async def confirm_commitment(commitment_id: str) -> dict[str, Any]:
        item = deps["commitment_core"].confirm_commitment(commitment_id)
        if not item:
            raise HTTPException(status_code=404, detail="commitment not found")
        return {"status": "success", "commitment": item}

    @router.post("/commitments/{commitment_id}/pause")
    async def pause_commitment(commitment_id: str) -> dict[str, Any]:
        item = deps["commitment_core"].pause_commitment(commitment_id)
        if not item:
            raise HTTPException(status_code=404, detail="commitment not found")
        return {"status": "success", "commitment": item}

    @router.post("/commitments/{commitment_id}/cancel")
    async def cancel_commitment(commitment_id: str) -> dict[str, Any]:
        item = deps["commitment_core"].cancel_commitment(commitment_id)
        if not item:
            raise HTTPException(status_code=404, detail="commitment not found")
        return {"status": "success", "commitment": item}

    @router.post("/commitments/run-due")
    async def run_due_commitments(request: CommitmentPushRequest | None = None) -> dict[str, Any]:
        payload = request or CommitmentPushRequest()
        return deps["commitment_push"].run_due(limit=payload.limit, reason=payload.reason)

    return router
