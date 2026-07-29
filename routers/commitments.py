from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core.context_scope import owner_scope
from memory_bridge.scope import normalize_scope_component


class CommitmentCreateRequest(BaseModel):
    kind: str = "generic_reminder"
    title: str = ""
    user_id: str = Field(min_length=1, max_length=240)
    channel: str = "api"
    session_id: str = Field(min_length=1, max_length=240)
    status: str = "active"
    schedule: dict[str, Any] = Field(default_factory=dict)
    payload: dict[str, Any] = Field(default_factory=dict)
    risk_level: str = "R0"
    next_run_at: str | None = None


class CommitmentPushRequest(BaseModel):
    limit: int = 10
    reason: str = "manual"


def _scope_component(value: Any, field: str) -> str:
    try:
        return normalize_scope_component(value, field)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _owned_commitments(
    items: list[dict[str, Any]],
    *,
    user_id: str,
    session_id: str | None,
) -> list[dict[str, Any]]:
    visible: list[dict[str, Any]] = []
    for item in items:
        owner_state, item_user, item_session = owner_scope(item)
        if owner_state != "exact" or item_user != user_id:
            continue
        if session_id is not None and item_session != session_id:
            continue
        visible.append(item)
    return visible


def build_commitments_router(deps: dict[str, Any]) -> APIRouter:
    router = APIRouter()

    @router.get("/commitments")
    async def list_commitments(
        user_id: str,
        session_id: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        user_scope = _scope_component(user_id, "user_id")
        session_scope = (
            _scope_component(session_id, "session_id")
            if session_id is not None
            else None
        )
        items = _owned_commitments(
            deps["commitment_core"].list_commitments(status=status),
            user_id=user_scope,
            session_id=session_scope,
        )
        return {"status": "success", "count": len(items), "commitments": items}

    @router.get("/commitments/{commitment_id}")
    async def get_commitment(
        commitment_id: str,
        user_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        user_scope = _scope_component(user_id, "user_id")
        session_scope = _scope_component(session_id, "session_id")
        item = deps["commitment_core"].get_commitment(
            commitment_id,
            user_id=user_scope,
            session_id=session_scope,
        )
        if not item:
            raise HTTPException(status_code=404, detail="commitment not found")
        return {"status": "success", "commitment": item}

    @router.post("/commitments")
    async def create_commitment(request: CommitmentCreateRequest) -> dict[str, Any]:
        payload = request.model_dump(exclude_none=True)
        payload["user_id"] = _scope_component(request.user_id, "user_id")
        payload["session_id"] = _scope_component(
            request.session_id,
            "session_id",
        )
        item = deps["commitment_core"].create_commitment(payload)
        return {"status": "success", "commitment": item}

    @router.post("/commitments/{commitment_id}/confirm")
    async def confirm_commitment(
        commitment_id: str,
        user_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        user_scope = _scope_component(user_id, "user_id")
        session_scope = _scope_component(session_id, "session_id")
        item = deps["commitment_core"].confirm_commitment(
            commitment_id,
            user_id=user_scope,
            session_id=session_scope,
        )
        if not item:
            raise HTTPException(status_code=404, detail="commitment not found")
        return {"status": "success", "commitment": item}

    @router.post("/commitments/{commitment_id}/pause")
    async def pause_commitment(
        commitment_id: str,
        user_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        user_scope = _scope_component(user_id, "user_id")
        session_scope = _scope_component(session_id, "session_id")
        item = deps["commitment_core"].pause_commitment(
            commitment_id,
            user_id=user_scope,
            session_id=session_scope,
        )
        if not item:
            raise HTTPException(status_code=404, detail="commitment not found")
        return {"status": "success", "commitment": item}

    @router.post("/commitments/{commitment_id}/cancel")
    async def cancel_commitment(
        commitment_id: str,
        user_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        user_scope = _scope_component(user_id, "user_id")
        session_scope = _scope_component(session_id, "session_id")
        item = deps["commitment_core"].cancel_commitment(
            commitment_id,
            user_id=user_scope,
            session_id=session_scope,
        )
        if not item:
            raise HTTPException(status_code=404, detail="commitment not found")
        return {"status": "success", "commitment": item}

    @router.post("/commitments/run-due")
    async def run_due_commitments(request: CommitmentPushRequest | None = None) -> dict[str, Any]:
        """Operator-wide scheduler control; no caller-owned filtering applies."""

        payload = request or CommitmentPushRequest()
        result = deps["commitment_push"].run_due(limit=payload.limit, reason=payload.reason)
        return {**result, "operation_scope": "operator_wide"}

    return router
