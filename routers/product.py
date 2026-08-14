"""Public, read-only local product projection routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from starlette.concurrency import run_in_threadpool

from runtime.product_experience import (
    PRODUCT_MATTERS_SCHEMA,
    PRODUCT_TODAY_SCHEMA,
    ProductExperienceService,
)


def _scope_from_context(context: dict[str, Any]) -> tuple[str, str] | None:
    value = context.get("internal_read_scope")
    if not isinstance(value, dict):
        return None
    user_id = str(value.get("user_id") or "").strip()
    session_id = str(value.get("session_id") or "").strip()
    return (user_id, session_id) if user_id and session_id else None


def _resolve_scope(
    service: ProductExperienceService,
    *,
    user_id: str | None,
    session_id: str | None,
    external_session_id: str | None,
) -> tuple[str, str] | dict[str, Any]:
    if (user_id is None) != (session_id is None):
        raise HTTPException(status_code=422, detail="user_id and session_id must be supplied together")
    if user_id is not None and session_id is not None:
        return user_id, session_id
    context = service.context(user_id=user_id, external_session_id=external_session_id)
    scope = _scope_from_context(context)
    if scope is None:
        # Ambiguous or mismatched context is an honest product state, not an
        # HTTP server failure.  The UI can explain setup/linking to the user.
        return context
    return scope


def _blocked_projection(context: dict[str, Any], *, matters: bool) -> dict[str, Any]:
    """Keep each product route on its own versioned response contract."""

    if matters:
        return {
            "schema_version": PRODUCT_MATTERS_SCHEMA,
            "status": context.get("status", "fail_closed"),
            "reason": context.get("reason"),
            "scope": None,
            "goal": None,
            "sections": {
                "situations": {"status": "empty", "count": 0, "items": []},
                "attention": {"status": "empty", "count": 0, "items": []},
                "suggestions": {"status": "empty", "count": 0, "items": []},
                "commitments": {"status": "empty", "count": 0, "items": []},
                "questions": {"status": "unsupported", "count": 0, "items": []},
                "waiting": {"status": "empty", "count": 0, "items": []},
            },
            "freshness": {},
            "authority": context.get("authority", {}),
        }
    return {
        "schema_version": PRODUCT_TODAY_SCHEMA,
        "status": context.get("status", "fail_closed"),
        "reason": context.get("reason"),
        "scope": None,
        "goal": None,
        "situations": [],
        "attention": [],
        "suggestions": [],
        "questions": {"status": "unsupported", "count": 0, "items": []},
        "waiting": [],
        "freshness": {},
        "authority": context.get("authority", {}),
    }


def build_product_router(*, service: ProductExperienceService) -> APIRouter:
    router = APIRouter(prefix="/product", tags=["product"])

    @router.get("/context")
    async def product_context(
        user_id: str | None = Query(default=None, min_length=1, max_length=240),
        session_id: str | None = Query(default=None, min_length=1, max_length=240),
        external_session_id: str | None = Query(default=None, min_length=1, max_length=240),
    ) -> dict[str, Any]:
        if session_id is not None and external_session_id is not None and session_id != external_session_id:
            raise HTTPException(status_code=422, detail="session_id and external_session_id must match when both are supplied")
        return await run_in_threadpool(
            service.context,
            user_id=user_id,
            external_session_id=external_session_id or session_id,
        )

    @router.get("/today")
    async def product_today(
        user_id: str | None = Query(default=None, min_length=1, max_length=240),
        session_id: str | None = Query(default=None, min_length=1, max_length=240),
        external_session_id: str | None = Query(default=None, min_length=1, max_length=240),
        limit: int = Query(default=20, ge=1, le=100),
    ) -> dict[str, Any]:
        selected = await run_in_threadpool(
            _resolve_scope,
            service,
            user_id=user_id,
            session_id=session_id,
            external_session_id=external_session_id,
        )
        if isinstance(selected, dict):
            return _blocked_projection(selected, matters=False)
        return await run_in_threadpool(
            service.today,
            user_id=selected[0],
            session_id=selected[1],
            limit=limit,
        )

    @router.get("/matters")
    async def product_matters(
        user_id: str | None = Query(default=None, min_length=1, max_length=240),
        session_id: str | None = Query(default=None, min_length=1, max_length=240),
        external_session_id: str | None = Query(default=None, min_length=1, max_length=240),
        limit: int = Query(default=20, ge=1, le=100),
    ) -> dict[str, Any]:
        selected = await run_in_threadpool(
            _resolve_scope,
            service,
            user_id=user_id,
            session_id=session_id,
            external_session_id=external_session_id,
        )
        if isinstance(selected, dict):
            return _blocked_projection(selected, matters=True)
        return await run_in_threadpool(
            service.matters,
            user_id=selected[0],
            session_id=selected[1],
            limit=limit,
        )

    @router.get("/status")
    async def product_status() -> dict[str, Any]:
        return await run_in_threadpool(service.status)

    return router


__all__ = ["build_product_router"]
