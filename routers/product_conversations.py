"""Exact-scope Product Conversation HTTP contract."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from runtime.product_conversation_runtime import (
    ProductConversationConflict,
    ProductConversationNotFound,
    ProductConversationRevisionConflict,
    ProductConversationRuntime,
    ProductConversationStorageError,
)


class ProductConversationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="", max_length=240)
    # The browser currently sends scope as query parameters, while direct API
    # clients often put it in the body.  Accept either and require an exact
    # pair before reaching the runtime.
    user_id: str | None = Field(default=None, min_length=1, max_length=240)
    session_id: str | None = Field(default=None, min_length=1, max_length=240)


def _scope(
    *,
    query_user_id: str | None,
    query_session_id: str | None,
    body_user_id: str | None = None,
    body_session_id: str | None = None,
) -> tuple[str, str]:
    selected_user = query_user_id if query_user_id is not None else body_user_id
    selected_session = query_session_id if query_session_id is not None else body_session_id
    if not selected_user or not selected_session:
        raise HTTPException(status_code=422, detail="user_id and session_id are required")
    if query_user_id is not None and body_user_id is not None and query_user_id != body_user_id:
        raise HTTPException(status_code=422, detail="user_id values must match")
    if query_session_id is not None and body_session_id is not None and query_session_id != body_session_id:
        raise HTTPException(status_code=422, detail="session_id values must match")
    return selected_user, selected_session


def _raise_conversation_error(exc: Exception) -> None:
    if isinstance(exc, ProductConversationNotFound):
        raise HTTPException(status_code=404, detail="conversation not found") from exc
    if isinstance(exc, ProductConversationRevisionConflict):
        raise HTTPException(status_code=409, detail="conversation revision conflict") from exc
    if isinstance(exc, ProductConversationConflict):
        raise HTTPException(status_code=409, detail="conversation identity conflict") from exc
    if isinstance(exc, (ValueError, TypeError)):
        raise HTTPException(status_code=422, detail="invalid conversation request") from exc
    if isinstance(exc, ProductConversationStorageError):
        raise HTTPException(status_code=503, detail="conversation state unavailable") from exc
    raise HTTPException(status_code=503, detail="conversation runtime unavailable") from exc


def build_product_conversations_router(
    runtime: ProductConversationRuntime,
) -> APIRouter:
    router = APIRouter(prefix="/product/conversations", tags=["product-conversations"])

    @router.get("")
    async def list_product_conversations(
        user_id: str = Query(..., min_length=1, max_length=240),
        session_id: str = Query(..., min_length=1, max_length=240),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        try:
            items = await run_in_threadpool(
                runtime.list_conversations,
                user_id=user_id,
                session_id=session_id,
                limit=limit,
            )
        except Exception as exc:
            _raise_conversation_error(exc)
            raise AssertionError("unreachable")
        scope = {"user_id": user_id, "session_id": session_id}
        return {
            "schema_version": "veyra.product_conversation_list.v1",
            "status": "success",
            "scope": scope,
            "count": len(items),
            "items": items,
            "conversations": items,
            "authority": runtime.status().get("authority", {}),
        }

    @router.post("")
    async def create_product_conversation(
        request: ProductConversationCreateRequest,
        user_id: str | None = Query(default=None, min_length=1, max_length=240),
        session_id: str | None = Query(default=None, min_length=1, max_length=240),
    ) -> dict[str, Any]:
        selected_user, selected_session = _scope(
            query_user_id=user_id,
            query_session_id=session_id,
            body_user_id=request.user_id,
            body_session_id=request.session_id,
        )
        try:
            conversation = await run_in_threadpool(
                runtime.create_conversation,
                user_id=selected_user,
                session_id=selected_session,
                title=request.title,
            )
        except Exception as exc:
            _raise_conversation_error(exc)
            raise AssertionError("unreachable")
        return {
            "schema_version": "veyra.product_conversation.v1",
            "status": "success",
            "conversation_id": conversation.get("conversation_id"),
            "conversation": conversation,
            "messages": conversation.get("messages", []),
            "authority": conversation.get("authority", {}),
        }

    @router.get("/{conversation_id}")
    async def get_product_conversation(
        conversation_id: str,
        user_id: str = Query(..., min_length=1, max_length=240),
        session_id: str = Query(..., min_length=1, max_length=240),
    ) -> dict[str, Any]:
        try:
            conversation = await run_in_threadpool(
                runtime.get_conversation,
                conversation_id,
                user_id=user_id,
                session_id=session_id,
            )
        except Exception as exc:
            _raise_conversation_error(exc)
            raise AssertionError("unreachable")
        if conversation is None:
            raise HTTPException(status_code=404, detail="conversation not found")
        return {
            "schema_version": "veyra.product_conversation.v1",
            "status": "success",
            "scope": {"user_id": user_id, "session_id": session_id},
            "conversation_id": conversation.get("conversation_id"),
            "conversation": conversation,
            "messages": conversation.get("messages", []),
            "authority": conversation.get("authority", {}),
        }

    return router


__all__ = [
    "ProductConversationCreateRequest",
    "build_product_conversations_router",
]
