from __future__ import annotations

import hmac
import os
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlsplit


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
DEFAULT_ALLOWED_ORIGINS = {
    "http://127.0.0.1:5173",
    "http://127.0.0.1:5174",
    "http://localhost:5173",
    "http://localhost:5174",
    "http://tauri.localhost",
    "https://tauri.localhost",
    "tauri://localhost",
}
SAFE_METHODS = {"GET", "HEAD"}
PUBLIC_READ_PATHS = {"/", "/health"}
PUBLIC_CALLBACK_PATHS = {"/integrations/feishu/events"}


@dataclass(frozen=True, slots=True)
class ControlDecision:
    allowed: bool
    status_code: int = 200
    code: str = "allowed"
    reason: str = ""


class LocalControlPolicy:
    """Protect local control-plane writes when Veyra is network exposed."""

    def __init__(
        self,
        *,
        bind_host: str | None = None,
        token: str | None = None,
        allowed_origins: set[str] | None = None,
        public_callback_paths: set[str] | None = None,
        public_read_paths: set[str] | None = None,
    ) -> None:
        self.bind_host = str(bind_host if bind_host is not None else os.getenv("VEYRA_HOST", "127.0.0.1")).strip().lower()
        self.token = str(token if token is not None else os.getenv("VEYRA_LOCAL_API_TOKEN", "")).strip()
        configured_origins = {
            item.strip()
            for item in str(os.getenv("VEYRA_ALLOWED_ORIGINS", "")).split(",")
            if item.strip()
        }
        self.allowed_origins = set(allowed_origins or DEFAULT_ALLOWED_ORIGINS) | configured_origins
        if public_callback_paths is None:
            allow_callbacks = str(os.getenv("VEYRA_ALLOW_PUBLIC_PROVIDER_CALLBACKS", "")).strip().lower() in {"1", "true", "yes", "on"}
            callback_verification_ready = bool(str(os.getenv("FEISHU_VERIFICATION_TOKEN", "")).strip())
            self.public_callback_paths = set(PUBLIC_CALLBACK_PATHS if allow_callbacks and callback_verification_ready else set())
        else:
            self.public_callback_paths = set(public_callback_paths)
        self.public_read_paths = set(PUBLIC_READ_PATHS if public_read_paths is None else public_read_paths)

    @property
    def network_exposed(self) -> bool:
        return self.bind_host not in LOOPBACK_HOSTS

    def status(self) -> dict[str, object]:
        if not self.network_exposed:
            status = "local_only"
        elif self.token:
            status = "token_required"
        else:
            status = "blocked_misconfigured"
        return {
            "status": status,
            "bind_host": self.bind_host,
            "network_exposed": self.network_exposed,
            "token_configured": bool(self.token),
            "origin_check": "enabled",
            "same_origin_allowed": True,
            "allowed_origins": sorted(self.allowed_origins),
            "public_read_paths": sorted(self.public_read_paths),
            "public_callback_paths": sorted(self.public_callback_paths),
        }

    def authorize(self, *, method: str, path: str, headers: Mapping[str, str]) -> ControlDecision:
        normalized_method = str(method or "GET").upper()
        if normalized_method == "OPTIONS":
            return ControlDecision(True)

        origin = str(headers.get("origin") or headers.get("Origin") or "").strip()
        if origin and origin not in self.allowed_origins and not self._is_same_origin(origin, headers):
            return ControlDecision(
                False,
                status_code=403,
                code="origin_not_allowed",
                reason="This browser origin is not allowed to mutate the Veyra control plane.",
            )

        if not self.network_exposed:
            return ControlDecision(True, code="loopback_control_plane")
        if normalized_method in SAFE_METHODS and path in self.public_read_paths:
            return ControlDecision(True, code="public_health_read")
        if normalized_method not in SAFE_METHODS and path in self.public_callback_paths:
            return ControlDecision(True, code="signed_provider_callback")
        if not self.token:
            return ControlDecision(
                False,
                status_code=503,
                code="control_token_not_configured",
                reason="VEYRA_LOCAL_API_TOKEN is required when VEYRA_HOST is not loopback.",
            )
        supplied = self._supplied_token(headers)
        if not supplied or not hmac.compare_digest(supplied, self.token):
            return ControlDecision(
                False,
                status_code=401,
                code="control_token_invalid",
                reason="A valid local control token is required for this operation.",
            )
        return ControlDecision(True, code="control_token_validated")

    @staticmethod
    def _is_same_origin(origin: str, headers: Mapping[str, str]) -> bool:
        """Allow the console served by Veyra itself, regardless of API port."""
        host = str(headers.get("host") or headers.get("Host") or "").strip().lower()
        if not host:
            return False
        try:
            parsed = urlsplit(origin)
        except ValueError:
            return False
        return parsed.scheme in {"http", "https"} and parsed.netloc.lower() == host

    def _supplied_token(self, headers: Mapping[str, str]) -> str:
        authorization = str(headers.get("authorization") or headers.get("Authorization") or "").strip()
        if authorization.lower().startswith("bearer "):
            return authorization[7:].strip()
        return str(headers.get("x-veyra-token") or headers.get("X-Veyra-Token") or "").strip()
