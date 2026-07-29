from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute


class PrivateControlPlaneRoute(APIRoute):
    """Prevent FastAPI validation errors from echoing private request input."""

    def get_route_handler(
        self,
    ) -> Callable[[Request], Awaitable[Response]]:
        original = super().get_route_handler()

        async def sanitized(request: Request) -> Response:
            try:
                return await original(request)
            except RequestValidationError:
                return JSONResponse(
                    status_code=422,
                    content={"detail": "invalid private control request"},
                )

        return sanitized


__all__ = ["PrivateControlPlaneRoute"]
