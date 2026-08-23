"""V1 product routes over exact owner/session Living Context projections."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from starlette.concurrency import run_in_threadpool

from core.world_state import StateRevisionConflictError
from interface.product_living_context import (
    QuestionAnswerRequest,
    QuestionTransitionRequest,
    ReactionFeedbackRequest,
    SourceConsentRequest,
    SourceRevokeRequest,
    SituationCommandRequest,
)
from runtime.product_experience import (
    PRODUCT_MATTERS_SCHEMA,
    PRODUCT_QUESTIONS_SCHEMA,
    PRODUCT_REACTIONS_SCHEMA,
    PRODUCT_SITUATIONS_SCHEMA,
    PRODUCT_SOURCES_SCHEMA,
    PRODUCT_SITUATION_SCHEMA,
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


def _resolve_scope(service: ProductExperienceService, *, user_id: str | None, session_id: str | None, external_session_id: str | None) -> tuple[str, str] | dict[str, Any]:
    if (user_id is None) != (session_id is None):
        raise HTTPException(status_code=422, detail="user_id and session_id must be supplied together")
    if user_id is not None and session_id is not None:
        return user_id, session_id
    context = service.context(user_id=user_id, external_session_id=external_session_id)
    scope = _scope_from_context(context)
    return context if scope is None else scope


def _blocked_projection(context: dict[str, Any], *, matters: bool) -> dict[str, Any]:
    if matters:
        return {
            "schema_version": PRODUCT_MATTERS_SCHEMA,
            "status": context.get("status", "fail_closed"),
            "reason": context.get("reason"),
            "scope": None,
            "focus": None,
            "goal": None,
            "sections": {name: {"status": "empty", "count": 0, "items": []} for name in ("situations", "recent_changes", "deadlines", "unknowns", "questions", "suggestions", "commitments", "waiting")},
            "freshness": {},
            "authority": context.get("authority", {}),
        }
    return {
        "schema_version": PRODUCT_TODAY_SCHEMA,
        "status": context.get("status", "fail_closed"),
        "reason": context.get("reason"),
        "scope": None,
        "first_meeting": False,
        "focus": None,
        "goal": None,
        "situations": [],
        "recent_terminal": [],
        "recent_changes": [],
        "deadlines": [],
        "unknowns": [],
        "suggestions": [],
        "questions": {"status": "empty", "items": []},
        "reactions": {"status": "empty", "items": [], "silent_count": 0},
        "attention": [],
        "waiting": [],
        "legacy": {"status": "empty", "items": []},
        "freshness": {},
        "authority": context.get("authority", {}),
    }


def _typed_degraded(result: dict[str, Any]) -> dict[str, Any]:
    """Keep every read-model degradation response structurally explicit."""

    if str(result.get("status") or "").lower() != "degraded":
        return result
    projected = dict(result)
    marker = projected.get("degraded")
    if not isinstance(marker, dict) or str(marker.get("status") or "") != "degraded":
        projected["degraded"] = {
            "status": "degraded",
            "code": "product_projection_degraded",
            "read_only": True,
        }
    return projected


def _raise_runtime_error(exc: Exception) -> None:
    if isinstance(exc, StateRevisionConflictError):
        raise HTTPException(status_code=409, detail="state revision conflict") from exc
    if isinstance(exc, KeyError):
        raise HTTPException(status_code=404, detail="product resource not found") from exc
    if isinstance(exc, (ValueError, TypeError)):
        raise HTTPException(status_code=422, detail="invalid product command") from exc
    raise HTTPException(status_code=503, detail="product runtime unavailable") from exc


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
        return await run_in_threadpool(service.context, user_id=user_id, external_session_id=external_session_id or session_id)

    @router.get("/today")
    async def product_today(
        user_id: str | None = Query(default=None, min_length=1, max_length=240),
        session_id: str | None = Query(default=None, min_length=1, max_length=240),
        external_session_id: str | None = Query(default=None, min_length=1, max_length=240),
        first_meeting: bool = Query(default=False),
        limit: int = Query(default=20, ge=1, le=100),
    ) -> dict[str, Any]:
        selected = await run_in_threadpool(_resolve_scope, service, user_id=user_id, session_id=session_id, external_session_id=external_session_id)
        if isinstance(selected, dict):
            return _blocked_projection(selected, matters=False)
        return _typed_degraded(await run_in_threadpool(service.today, user_id=selected[0], session_id=selected[1], limit=limit, first_meeting=first_meeting))

    @router.get("/matters")
    async def product_matters(
        user_id: str | None = Query(default=None, min_length=1, max_length=240),
        session_id: str | None = Query(default=None, min_length=1, max_length=240),
        external_session_id: str | None = Query(default=None, min_length=1, max_length=240),
        limit: int = Query(default=20, ge=1, le=100),
    ) -> dict[str, Any]:
        selected = await run_in_threadpool(_resolve_scope, service, user_id=user_id, session_id=session_id, external_session_id=external_session_id)
        if isinstance(selected, dict):
            return _blocked_projection(selected, matters=True)
        return _typed_degraded(await run_in_threadpool(service.matters, user_id=selected[0], session_id=selected[1], limit=limit))

    @router.get("/status")
    async def product_status() -> dict[str, Any]:
        return _typed_degraded(await run_in_threadpool(service.status))

    @router.get("/situations")
    async def product_situations(
        user_id: str = Query(..., min_length=1, max_length=240),
        session_id: str = Query(..., min_length=1, max_length=240),
        limit: int = Query(default=20, ge=1, le=100),
    ) -> dict[str, Any]:
        return _typed_degraded(await run_in_threadpool(service.list_situations, user_id=user_id, session_id=session_id, limit=limit))

    @router.get("/situations/{situation_id}")
    async def product_situation_detail(
        situation_id: str,
        user_id: str = Query(..., min_length=1, max_length=240),
        session_id: str = Query(..., min_length=1, max_length=240),
    ) -> dict[str, Any]:
        result = await run_in_threadpool(service.situation_detail, situation_id, user_id=user_id, session_id=session_id)
        if result.get("status") == "not_found":
            raise HTTPException(status_code=404, detail="Situation not found")
        return _typed_degraded(result)

    @router.post("/situations/{situation_id}/command")
    async def product_situation_command(
        situation_id: str,
        request: SituationCommandRequest,
        user_id: str = Query(..., min_length=1, max_length=240),
        session_id: str = Query(..., min_length=1, max_length=240),
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(service.command_situation, situation_id, user_id=user_id, session_id=session_id, request=request)
        except Exception as exc:
            _raise_runtime_error(exc)
        raise AssertionError("unreachable")

    @router.get("/questions")
    async def product_questions(
        user_id: str = Query(..., min_length=1, max_length=240),
        session_id: str = Query(..., min_length=1, max_length=240),
        limit: int = Query(default=20, ge=1, le=100),
    ) -> dict[str, Any]:
        return _typed_degraded(await run_in_threadpool(service.questions, user_id=user_id, session_id=session_id, limit=limit))

    @router.post("/questions/{need_id}/answer")
    async def product_question_answer(
        need_id: str,
        request: QuestionAnswerRequest,
        user_id: str = Query(..., min_length=1, max_length=240),
        session_id: str = Query(..., min_length=1, max_length=240),
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(service.answer_question, need_id, user_id=user_id, session_id=session_id, request=request)
        except Exception as exc:
            _raise_runtime_error(exc)
        raise AssertionError("unreachable")

    @router.post("/questions/{need_id}/defer")
    async def product_question_defer(
        need_id: str,
        request: QuestionTransitionRequest,
        user_id: str = Query(..., min_length=1, max_length=240),
        session_id: str = Query(..., min_length=1, max_length=240),
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(service.defer_question, need_id, user_id=user_id, session_id=session_id, request=request)
        except Exception as exc:
            _raise_runtime_error(exc)
        raise AssertionError("unreachable")

    @router.post("/questions/{need_id}/dismiss")
    async def product_question_dismiss(
        need_id: str,
        request: QuestionTransitionRequest,
        user_id: str = Query(..., min_length=1, max_length=240),
        session_id: str = Query(..., min_length=1, max_length=240),
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(service.dismiss_question, need_id, user_id=user_id, session_id=session_id, request=request)
        except Exception as exc:
            _raise_runtime_error(exc)
        raise AssertionError("unreachable")

    async def _product_reactions(
        user_id: str,
        session_id: str,
        situation_id: str | None,
        situation_revision: int | None,
        limit: int,
        visible_dispositions: set[str] | None = None,
    ) -> dict[str, Any]:
        result = await run_in_threadpool(service.reactions, user_id=user_id, session_id=session_id, situation_id=situation_id, situation_revision=situation_revision, limit=limit, visible_dispositions=visible_dispositions)
        return _typed_degraded(result)

    @router.get("/reactions")
    async def product_reactions(
        user_id: str = Query(..., min_length=1, max_length=240),
        session_id: str = Query(..., min_length=1, max_length=240),
        situation_id: str | None = Query(default=None, min_length=1, max_length=240),
        situation_revision: int | None = Query(default=None, ge=1),
        limit: int = Query(default=20, ge=1, le=200),
    ) -> dict[str, Any]:
        try:
            return await _product_reactions(user_id, session_id, situation_id, situation_revision, limit)
        except Exception as exc:
            _raise_runtime_error(exc)
        raise AssertionError("unreachable")

    @router.get("/suggestions")
    async def product_suggestions(
        user_id: str = Query(..., min_length=1, max_length=240),
        session_id: str = Query(..., min_length=1, max_length=240),
        situation_id: str | None = Query(default=None, min_length=1, max_length=240),
        situation_revision: int | None = Query(default=None, ge=1),
        limit: int = Query(default=20, ge=1, le=200),
    ) -> dict[str, Any]:
        try:
            result = await run_in_threadpool(
                service.suggestions,
                user_id=user_id,
                session_id=session_id,
                situation_id=situation_id,
                situation_revision=situation_revision,
                limit=limit,
            )
            return _typed_degraded(result)
        except Exception as exc:
            _raise_runtime_error(exc)
        raise AssertionError("unreachable")

    @router.post("/reactions/{reaction_id}/feedback")
    async def product_reaction_feedback(
        reaction_id: str,
        request: ReactionFeedbackRequest,
        user_id: str = Query(..., min_length=1, max_length=240),
        session_id: str = Query(..., min_length=1, max_length=240),
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(service.feedback_reaction, reaction_id, user_id=user_id, session_id=session_id, request=request)
        except Exception as exc:
            _raise_runtime_error(exc)
        raise AssertionError("unreachable")

    @router.get("/sources")
    async def product_sources(
        user_id: str = Query(..., min_length=1, max_length=240),
        session_id: str = Query(..., min_length=1, max_length=240),
    ) -> dict[str, Any]:
        return _typed_degraded(await run_in_threadpool(service.sources, user_id=user_id, session_id=session_id))

    @router.post("/sources/{source}/consent")
    async def product_source_consent(
        source: str,
        request: SourceConsentRequest,
        user_id: str = Query(..., min_length=1, max_length=240),
        session_id: str = Query(..., min_length=1, max_length=240),
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(service.consent_source, source, user_id=user_id, session_id=session_id, request=request)
        except Exception as exc:
            _raise_runtime_error(exc)
        raise AssertionError("unreachable")

    @router.delete("/sources/{source}/consent")
    async def product_source_revoke(
        source: str,
        request: SourceRevokeRequest,
        user_id: str = Query(..., min_length=1, max_length=240),
        session_id: str = Query(..., min_length=1, max_length=240),
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(service.revoke_source, source, user_id=user_id, session_id=session_id, request=request)
        except Exception as exc:
            _raise_runtime_error(exc)
        raise AssertionError("unreachable")

    return router


__all__ = ["build_product_router"]
