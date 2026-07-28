from __future__ import annotations

import hmac
import ipaddress
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
        return self._host_is_network_exposed(self.bind_host)

    def status(
        self,
        *,
        actual_server_host: str | None = None,
    ) -> dict[str, object]:
        actual_host = self._normalized_server_host(actual_server_host)
        actual_network_exposed = (
            self._host_is_network_exposed(actual_host)
            if actual_host is not None
            else self.network_exposed
        )
        binding_mismatch = bool(
            not self.network_exposed and actual_network_exposed
        )
        if binding_mismatch:
            status = "blocked_binding_mismatch"
        elif not actual_network_exposed:
            status = "local_only"
        elif self.token:
            status = "token_required"
        else:
            status = "blocked_misconfigured"
        return {
            "status": status,
            "bind_host": self.bind_host,
            "network_exposed": self.network_exposed,
            "actual_server_host": actual_host,
            "actual_network_exposed": actual_network_exposed,
            "binding_mismatch": binding_mismatch,
            "token_configured": bool(self.token),
            "origin_check": "enabled",
            "same_origin_allowed": True,
            "allowed_origins": sorted(self.allowed_origins),
            "public_read_paths": sorted(self.public_read_paths),
            "public_callback_paths": sorted(self.public_callback_paths),
        }

    def authorize(
        self,
        *,
        method: str,
        path: str,
        headers: Mapping[str, str],
        actual_server_host: str | None = None,
    ) -> ControlDecision:
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

        actual_host = self._normalized_server_host(actual_server_host)
        actual_network_exposed = (
            self._host_is_network_exposed(actual_host)
            if actual_host is not None
            else self.network_exposed
        )
        if not actual_network_exposed:
            return ControlDecision(True, code="loopback_control_plane")
        if normalized_method in SAFE_METHODS and path in self.public_read_paths:
            return ControlDecision(True, code="public_health_read")
        if normalized_method not in SAFE_METHODS and path in self.public_callback_paths:
            return ControlDecision(True, code="signed_provider_callback")
        if not self.network_exposed:
            return ControlDecision(
                False,
                status_code=503,
                code="listener_binding_mismatch",
                reason=(
                    "The actual server listener is network exposed while "
                    "VEYRA_HOST declares a loopback-only control plane."
                ),
            )
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
    def _normalized_server_host(value: str | None) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip().lower()
        if not normalized:
            return None
        if normalized.startswith("[") and normalized.endswith("]"):
            normalized = normalized[1:-1]
        try:
            address = ipaddress.ip_address(normalized)
        except ValueError:
            # ASGI servers normally publish a numeric socket address. An
            # unresolved Starlette ``testserver`` address is synthetic and
            # not evidence of a listener. Other hostnames remain exposed.
            return None if normalized == "testserver" else normalized
        return str(address)

    @staticmethod
    def _host_is_network_exposed(host: str) -> bool:
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
        if host in LOOPBACK_HOSTS:
            return False
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return True
        if address.is_loopback:
            return False
        mapped = getattr(address, "ipv4_mapped", None)
        return not bool(mapped and mapped.is_loopback)

    @staticmethod
    def _is_same_origin(origin: str, headers: Mapping[str, str]) -> bool:
        """Allow only an actual loopback console served by Veyra itself.

        Treating any ``Origin == Host`` pair as local permits DNS rebinding:
        an attacker-controlled hostname can resolve to 127.0.0.1 while the
        browser supplies that same external hostname in both headers. Public
        deployments must therefore use the explicit origin allowlist.
        """
        host = str(headers.get("host") or headers.get("Host") or "").strip().lower()
        if not host:
            return False
        try:
            parsed = urlsplit(origin)
        except ValueError:
            return False
        origin_host = str(parsed.hostname or "").strip().lower()
        return bool(
            parsed.scheme in {"http", "https"}
            and parsed.netloc.lower() == host
            and origin_host
            and not LocalControlPolicy._host_is_network_exposed(
                origin_host
            )
        )

    def _supplied_token(self, headers: Mapping[str, str]) -> str:
        authorization = str(headers.get("authorization") or headers.get("Authorization") or "").strip()
        if authorization.lower().startswith("bearer "):
            return authorization[7:].strip()
        return str(headers.get("x-veyra-token") or headers.get("X-Veyra-Token") or "").strip()
