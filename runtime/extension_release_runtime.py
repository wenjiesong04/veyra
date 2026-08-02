from __future__ import annotations

import base64
from datetime import datetime
import hashlib
import hmac
import os
from typing import Any, Callable, Mapping, TypeAlias

from core.world_state import WorldStateStore
from interface.extension_release import ExtensionReleaseAuthority
from runtime.extension_release_registry import (
    ExtensionReleaseRegistry,
    ExtensionReleaseUnauthorizedError,
    ExtensionReleaseUnavailableError,
)
from runtime.extension_release_signer import (
    Ed25519ReleaseSigner,
    Ed25519ReleaseTrustStore,
    ed25519_key_id,
)


PRIVATE_KEY_PATH_ENV = "VEYRA_PHASE6_RELEASE_PRIVATE_KEY_PATH"
PUBLIC_KEY_B64_ENV = "VEYRA_PHASE6_RELEASE_PUBLIC_KEY_B64"
SIGNING_SERVICE_ID_ENV = "VEYRA_PHASE6_RELEASE_SIGNING_SERVICE_ID"
RELEASE_ENABLED_ENV = "VEYRA_PHASE6_EXTENSION_RELEASE_ENABLED"
CONTROL_TOKEN_ENV = "VEYRA_LOCAL_API_TOKEN"
DEFAULT_SIGNING_SERVICE_ID = "veyra.release.signer.local.v1"
PUBLIC_STATUS_SCHEMA = "veyra.phase6.extension_release_status.v1"

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"", "0", "false", "no", "off"})
_SAFE_FAILURE_REASONS = frozenset(
    {
        "release_runtime_configuration_missing",
        "release_runtime_configuration_invalid",
        "release_runtime_construction_failed",
    }
)


class FailClosedExtensionReleaseRegistry:
    """Non-operational release surface used when startup admission fails.

    The object deliberately retains no configured path, public key, private key,
    exception text, or signer instance.  Its safe status remains observable so a
    bad optional Phase 6 configuration cannot prevent the Veyra API from
    starting, while every release and deployment operation remains unavailable.
    """

    def __init__(
        self,
        *,
        reason: str,
        requested_enabled: bool,
        control_token: str,
    ) -> None:
        selected_reason = str(reason or "")
        if selected_reason not in _SAFE_FAILURE_REASONS:
            selected_reason = "release_runtime_construction_failed"
        self._reason = selected_reason
        self.enabled = False
        self.control_token = ""
        self._requested_enabled = bool(requested_enabled)
        selected_token = str(control_token or "").strip()
        self._control_token_configured = bool(selected_token)
        self._control_token_digest = (
            hashlib.sha256(selected_token.encode("utf-8")).hexdigest()
            if selected_token
            else ""
        )

    def status(self, *, control_token: str = "") -> dict[str, Any]:
        self._authorize(control_token)
        return {
            "schema_version": PUBLIC_STATUS_SCHEMA,
            "phase": "6.2f",
            "status": "fail_closed",
            "operational_health": "unavailable",
            "completion_scope": (
                "ed25519 release attestation and immutable private registry only"
            ),
            "admission": {
                "enabled": False,
                "requested_enabled": self._requested_enabled,
                "control_token_configured": self._control_token_configured,
                "signer_configured": False,
                "trusted_signing_key": False,
            },
            "signer": None,
            "trust_roots": {
                "algorithm": "ed25519",
                "key_ids": [],
                "public_key_digests": {},
                "private_key_material_present": False,
            },
            "storage": {
                "status": "not_opened",
                "issue": self._reason,
                "release_count": 0,
                "operation_count": 0,
                "event_count": 0,
                "registry_revision": None,
                "counts": {},
            },
            "installation_status": "not_implemented",
            "activation_status": "not_installed",
            "capability_registry_visible": False,
            "canary_status": "not_started",
            "promotion_authorized": False,
            "read_side_effects": "none",
            "private_key_material_present_in_response": False,
            "authority": ExtensionReleaseAuthority().model_dump(mode="json"),
        }

    def _authorize(self, control_token: str) -> None:
        if not self._control_token_digest:
            return
        presented = hashlib.sha256(
            str(control_token or "").strip().encode("utf-8")
        ).hexdigest()
        if not hmac.compare_digest(presented, self._control_token_digest):
            raise ExtensionReleaseUnauthorizedError(
                "valid Veyra control token required for signed releases"
            )

    def _unavailable(self, *args: Any, **kwargs: Any) -> Any:
        del args
        self._authorize(str(kwargs.get("control_token") or ""))
        raise ExtensionReleaseUnavailableError(
            "signed release registry failed closed during startup"
        )

    create = _unavailable
    revoke = _unavailable
    list = _unavailable
    get = _unavailable
    integrity = _unavailable
    deployment_subject = _unavailable


ExtensionReleaseRuntimeRegistry: TypeAlias = (
    ExtensionReleaseRegistry | FailClosedExtensionReleaseRegistry
)


def build_extension_release_registry(
    *,
    state_store: WorldStateStore,
    generation_gate: Any,
    dynamic_validation_gate: Any,
    env: Mapping[str, str] | None = None,
    now: Callable[[], datetime] | None = None,
) -> ExtensionReleaseRuntimeRegistry:
    """Build a real release registry only from an exact trusted configuration.

    This function never generates, writes, repairs, or rotates signing keys.  It
    intentionally converts all configuration/construction failures into a
    source-free fail-closed registry so optional Phase 6 startup is non-fatal.
    """

    selected_env = os.environ if env is None else env
    raw_enabled = _env_text(selected_env, RELEASE_ENABLED_ENV).lower()
    try:
        requested_enabled = _parse_bool(raw_enabled)
    except ValueError:
        return _failed(
            reason="release_runtime_configuration_invalid",
            requested_enabled=False,
            control_token=_env_text(selected_env, CONTROL_TOKEN_ENV),
        )

    private_key_path = _env_text(selected_env, PRIVATE_KEY_PATH_ENV)
    public_key_b64 = _env_text(selected_env, PUBLIC_KEY_B64_ENV)
    control_token = _env_text(selected_env, CONTROL_TOKEN_ENV)
    signing_service_id = (
        _env_text(selected_env, SIGNING_SERVICE_ID_ENV)
        or DEFAULT_SIGNING_SERVICE_ID
    )
    control_token_configured = bool(control_token)

    if not private_key_path and not public_key_b64:
        return _failed(
            reason="release_runtime_configuration_missing",
            requested_enabled=requested_enabled,
            control_token=control_token,
        )
    if not private_key_path or not public_key_b64 or not control_token:
        return _failed(
            reason="release_runtime_configuration_invalid",
            requested_enabled=requested_enabled,
            control_token=control_token,
        )

    try:
        public_key_raw = base64.b64decode(
            public_key_b64.encode("ascii"),
            validate=True,
        )
        if len(public_key_raw) != 32:
            raise ValueError("invalid Ed25519 public key length")
        key_id = ed25519_key_id(public_key_raw)
        signer = Ed25519ReleaseSigner(
            private_key_path=private_key_path,
            signing_service_id=signing_service_id,
            forbidden_roots=[state_store.root],
        )
        trust_store = Ed25519ReleaseTrustStore({key_id: public_key_raw})
        return ExtensionReleaseRegistry(
            state_store=state_store,
            generation_gate=generation_gate,
            dynamic_validation_gate=dynamic_validation_gate,
            signer=signer,
            trust_store=trust_store,
            enabled=requested_enabled,
            control_token=control_token,
            now=now,
        )
    except Exception:
        return _failed(
            reason="release_runtime_construction_failed",
            requested_enabled=requested_enabled,
            control_token=control_token,
        )


def _failed(
    *,
    reason: str,
    requested_enabled: bool,
    control_token: str,
) -> FailClosedExtensionReleaseRegistry:
    return FailClosedExtensionReleaseRegistry(
        reason=reason,
        requested_enabled=requested_enabled,
        control_token=control_token,
    )


def _env_text(env: Mapping[str, str], key: str) -> str:
    value = env.get(key, "")
    return value.strip() if isinstance(value, str) else ""


def _parse_bool(value: str) -> bool:
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise ValueError("invalid boolean environment value")


__all__ = [
    "CONTROL_TOKEN_ENV",
    "DEFAULT_SIGNING_SERVICE_ID",
    "ExtensionReleaseRuntimeRegistry",
    "FailClosedExtensionReleaseRegistry",
    "PRIVATE_KEY_PATH_ENV",
    "PUBLIC_KEY_B64_ENV",
    "RELEASE_ENABLED_ENV",
    "SIGNING_SERVICE_ID_ENV",
    "build_extension_release_registry",
]
