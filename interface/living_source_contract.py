"""Server-owned contracts for bounded, read-only Living Context sources."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import re
from typing import Any, Literal, Mapping

from common.living_source_primitives import (
    FORBIDDEN_PARAMETER_KEYS,
    LIVING_SOURCE_CONSENT_SCHEMA,
    LIVING_SOURCE_RECEIPT_SCHEMA,
    LIVING_SOURCE_REQUEST_SCHEMA,
    LIVING_SOURCE_SCHEMA,
    READ_ONLY_SOURCE_KINDS,
    SOURCE_KINDS,
    SOURCE_NEED_BINDING_SCHEMA,
    SOURCE_PARAMETER_KEYS,
    SourceAuthority,
    SourceCapability,
    SourceKind,
    LivingSourceContractError,
    _canonical_value,
    _require_identifier,
    _require_time,
    _scope_tuple,
    canonical_utc,
    consent_digest_from_record,
    need_digest_from_record,
    parse_utc,
    stable_digest,
    utc_now,
)
from interface.living_source_parameters import validate_parameters as _validate_parameters
from interface.living_source_payload import (
    normalize_provider_payload,
    validate_receipt_payload as _validate_receipt_payload,
)
from interface.living_source_registry import capability_registry


@dataclass(frozen=True, slots=True)
class SourceNeedBinding:
    """Non-authoritative adapter binding to the core InformationNeed record.

    ``need_digest`` is the constructor-level compatibility alias; durable rows
    serialize the authoritative value under ``record_digest``.
    """

    binding_id: str
    need_id: str
    need_revision: int
    need_digest: str
    user_id: str
    workspace_id: str
    session_id: str
    situation_id: str
    source: SourceKind
    parameters: Mapping[str, Any] = field(default_factory=dict)
    issued_at: str = field(default_factory=lambda: canonical_utc(utc_now()))
    expires_at: str = field(default_factory=lambda: canonical_utc(utc_now() + timedelta(hours=24)))

    @property
    def owner_id(self) -> str:
        return self.user_id

    @property
    def record_digest(self) -> str:
        return self.need_digest

    @property
    def scope(self) -> tuple[str, str]:
        return _scope_tuple(self.user_id, self.session_id)

    def __post_init__(self) -> None:
        _require_identifier(self.binding_id, "binding_id")
        _require_identifier(self.need_id, "need_id")
        if isinstance(self.need_revision, bool) or not isinstance(self.need_revision, int) or self.need_revision < 1:
            raise LivingSourceContractError("core InformationNeed revision is invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", str(self.need_digest or "")):
            raise LivingSourceContractError("core InformationNeed digest is invalid")
        _require_identifier(self.user_id, "user_id", owner=True)
        _require_identifier(self.workspace_id, "workspace_id")
        _require_identifier(self.session_id, "session_id")
        _require_identifier(self.situation_id, "situation_id")
        if self.source not in SOURCE_KINDS:
            raise LivingSourceContractError("binding source is not registered")
        object.__setattr__(self, "parameters", _validate_parameters(self.source, self.parameters))
        issued = _require_time(self.issued_at, "binding.issued_at")
        expires = _require_time(self.expires_at, "binding.expires_at")
        if parse_utc(expires) <= parse_utc(issued):
            raise LivingSourceContractError("binding expiry must be after issue")
        object.__setattr__(self, "issued_at", issued)
        object.__setattr__(self, "expires_at", expires)

    def parameters_for(self, source: str) -> dict[str, Any]:
        if source != self.source:
            raise LivingSourceContractError("source is not allowed by this binding")
        return dict(self.parameters)

    def active_at(self, now: datetime | None = None) -> bool:
        selected = now or utc_now()
        return parse_utc(self.issued_at) <= selected < parse_utc(self.expires_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SOURCE_NEED_BINDING_SCHEMA,
            "binding_id": self.binding_id,
            "need_id": self.need_id,
            "need_revision": self.need_revision,
            "record_digest": self.record_digest,
            "user_id": self.user_id,
            "workspace_id": self.workspace_id,
            "session_id": self.session_id,
            "situation_id": self.situation_id,
            "source": self.source,
            "parameters": _canonical_value(self.parameters),
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True, slots=True)
class SourceConsent:
    consent_id: str
    user_id: str
    workspace_id: str
    session_id: str
    source: SourceKind
    purpose: str
    granted_at: str
    expires_at: str
    granted: bool = True
    generation: int = 1

    @property
    def owner_id(self) -> str:
        return self.user_id

    @property
    def scope(self) -> tuple[str, str]:
        return _scope_tuple(self.user_id, self.session_id)

    def __post_init__(self) -> None:
        _require_identifier(self.consent_id, "consent_id")
        _require_identifier(self.user_id, "user_id", owner=True)
        _require_identifier(self.workspace_id, "workspace_id")
        _require_identifier(self.session_id, "session_id")
        if self.source not in SOURCE_KINDS:
            raise LivingSourceContractError("consent source is not registered")
        if not isinstance(self.granted, bool):
            raise LivingSourceContractError("consent granted flag is invalid")
        if isinstance(self.generation, bool) or not isinstance(self.generation, int) or self.generation < 1:
            raise LivingSourceContractError("consent generation is invalid")
        if not str(self.purpose or "").strip() or len(self.purpose) > 600:
            raise LivingSourceContractError("consent purpose is invalid")
        granted_at = _require_time(self.granted_at, "consent.granted_at")
        expires_at = _require_time(self.expires_at, "consent.expires_at")
        if parse_utc(expires_at) <= parse_utc(granted_at):
            raise LivingSourceContractError("consent expiry must be after grant")
        object.__setattr__(self, "granted_at", granted_at)
        object.__setattr__(self, "expires_at", expires_at)

    def active_at(self, now: datetime | None = None) -> bool:
        selected = now or utc_now()
        return self.granted and parse_utc(self.granted_at) <= selected < parse_utc(self.expires_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": LIVING_SOURCE_CONSENT_SCHEMA,
            "consent_id": self.consent_id,
            "user_id": self.user_id,
            "workspace_id": self.workspace_id,
            "session_id": self.session_id,
            "source": self.source,
            "purpose": self.purpose,
            "granted_at": self.granted_at,
            "expires_at": self.expires_at,
            "granted": self.granted,
            "generation": self.generation,
        }


RequestStatus = Literal[
    "admitted",
    "running",
    "pending",
    "completed",
    "denied",
    "expired",
    "unknown",
    "timeout",
    "unavailable",
    "stale",
    "revoked",
]
ReceiptStatus = Literal[
    "ok",
    "empty",
    "pending",
    "unknown",
    "timeout",
    "unavailable",
    "denied",
    "expired",
    "stale",
    "revoked",
]


@dataclass(frozen=True, slots=True)
class SourceRequest:
    request_id: str
    need_id: str
    user_id: str
    workspace_id: str
    session_id: str
    source: SourceKind
    issued_at: str
    expires_at: str
    timeout_seconds: float
    parameters_digest: str
    binding_id: str | None = None
    attempt: int = 1
    consent_id: str | None = None
    consent_generation: int = 0
    consent_digest: str | None = None
    status: RequestStatus = "admitted"
    receipt_id: str | None = None
    generation: int = 0
    authority: SourceAuthority = field(default_factory=SourceAuthority)

    @property
    def owner_id(self) -> str:
        return self.user_id

    @property
    def scope(self) -> tuple[str, str]:
        return _scope_tuple(self.user_id, self.session_id)

    def __post_init__(self) -> None:
        _require_identifier(self.request_id, "request_id")
        _require_identifier(self.need_id, "need_id")
        _require_identifier(self.user_id, "user_id", owner=True)
        _require_identifier(self.workspace_id, "workspace_id")
        _require_identifier(self.session_id, "session_id")
        if self.source not in SOURCE_KINDS:
            raise LivingSourceContractError("request source is not registered")
        if self.status not in {"admitted", "running", "pending", "completed", "denied", "expired", "unknown", "timeout", "unavailable", "stale", "revoked"}:
            raise LivingSourceContractError("request status is invalid")
        if isinstance(self.attempt, bool) or not isinstance(self.attempt, int) or not 1 <= self.attempt <= 4:
            raise LivingSourceContractError("request attempt is invalid")
        if isinstance(self.generation, bool) or not isinstance(self.generation, int) or self.generation < 0:
            raise LivingSourceContractError("request generation is invalid")
        _require_time(self.issued_at, "request.issued_at")
        _require_time(self.expires_at, "request.expires_at")
        if parse_utc(self.expires_at) < parse_utc(self.issued_at):
            raise LivingSourceContractError("request expiry is invalid")
        if isinstance(self.timeout_seconds, bool) or not 0.05 <= float(self.timeout_seconds) <= 60.0:
            raise LivingSourceContractError("request timeout is out of range")
        if not re.fullmatch(r"[0-9a-f]{64}", str(self.parameters_digest or "")):
            raise LivingSourceContractError("request parameter digest is invalid")
        if self.binding_id is not None:
            _require_identifier(self.binding_id, "binding_id")
        if self.receipt_id is not None:
            _require_identifier(self.receipt_id, "receipt_id")
        if self.consent_id is not None:
            _require_identifier(self.consent_id, "consent_id")
        if isinstance(self.consent_generation, bool) or not isinstance(self.consent_generation, int) or self.consent_generation < 0:
            raise LivingSourceContractError("request consent generation is invalid")
        if self.consent_digest is not None and not re.fullmatch(r"[0-9a-f]{64}", str(self.consent_digest)):
            raise LivingSourceContractError("request consent digest is invalid")
        if self.consent_generation == 0 and self.consent_digest is not None:
            raise LivingSourceContractError("request consent digest requires a generation")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": LIVING_SOURCE_REQUEST_SCHEMA,
            "request_id": self.request_id,
            "need_id": self.need_id,
            "user_id": self.user_id,
            "workspace_id": self.workspace_id,
            "session_id": self.session_id,
            "source": self.source,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "timeout_seconds": self.timeout_seconds,
            "parameters_digest": self.parameters_digest,
            "binding_id": self.binding_id,
            "attempt": self.attempt,
            "consent_id": self.consent_id,
            "consent_generation": self.consent_generation,
            "consent_digest": self.consent_digest,
            "status": self.status,
            "receipt_id": self.receipt_id,
            "generation": self.generation,
            "authority": self.authority.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class SourceReceipt:
    receipt_id: str
    request_id: str
    need_id: str
    user_id: str
    workspace_id: str
    session_id: str
    source: SourceKind
    status: ReceiptStatus
    observed_at: str
    fresh_until: str | None
    ttl_seconds: int
    payload: Mapping[str, Any] = field(default_factory=dict)
    reason: str = ""
    payload_digest: str = ""
    authority: SourceAuthority = field(default_factory=SourceAuthority)
    replay_of: str | None = None
    generation: int = 0
    binding_id: str | None = None
    consent_id: str | None = None
    consent_generation: int = 0
    consent_digest: str | None = None

    @property
    def owner_id(self) -> str:
        return self.user_id

    @property
    def scope(self) -> tuple[str, str]:
        return _scope_tuple(self.user_id, self.session_id)

    def __post_init__(self) -> None:
        _require_identifier(self.receipt_id, "receipt_id")
        _require_identifier(self.request_id, "request_id")
        _require_identifier(self.need_id, "need_id")
        _require_identifier(self.user_id, "user_id", owner=True)
        _require_identifier(self.workspace_id, "workspace_id")
        _require_identifier(self.session_id, "session_id")
        if self.source not in SOURCE_KINDS:
            raise LivingSourceContractError("receipt source is not registered")
        if self.status not in {"ok", "empty", "pending", "unknown", "timeout", "unavailable", "denied", "expired", "stale", "revoked"}:
            raise LivingSourceContractError("receipt status is invalid")
        if isinstance(self.generation, bool) or not isinstance(self.generation, int) or self.generation < 0:
            raise LivingSourceContractError("receipt generation is invalid")
        _require_time(self.observed_at, "receipt.observed_at")
        if self.fresh_until is not None:
            _require_time(self.fresh_until, "receipt.fresh_until")
        if isinstance(self.ttl_seconds, bool) or not 0 <= self.ttl_seconds <= 604800:
            raise LivingSourceContractError("receipt TTL is out of range")
        if not isinstance(self.payload, Mapping):
            raise LivingSourceContractError("receipt payload must be an object")
        _validate_receipt_payload(self.source, self.status, self.payload)
        if self.status in {"ok", "empty"}:
            if not isinstance(self.payload.get("facts"), Mapping):
                raise LivingSourceContractError("successful receipt must carry typed facts")
            if self.ttl_seconds <= 0 or self.fresh_until is None:
                raise LivingSourceContractError("successful receipt must carry a positive TTL")
        elif self.payload or self.ttl_seconds != 0 or self.fresh_until is not None:
            raise LivingSourceContractError("non-success receipt must not carry facts or TTL")
        if len(str(self.reason or "")) > 600:
            raise LivingSourceContractError("receipt reason is too long")
        expected_digest = stable_digest(self.payload, namespace="receipt.payload")
        if self.payload_digest and self.payload_digest != expected_digest:
            raise LivingSourceContractError("receipt payload digest does not match payload")
        digest = self.payload_digest or expected_digest
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise LivingSourceContractError("receipt payload digest is invalid")
        object.__setattr__(self, "payload_digest", digest)
        if self.replay_of is not None:
            _require_identifier(self.replay_of, "replay_of")
        if self.binding_id is not None:
            _require_identifier(self.binding_id, "binding_id")
        if self.consent_id is not None:
            _require_identifier(self.consent_id, "consent_id")
        if isinstance(self.consent_generation, bool) or not isinstance(self.consent_generation, int) or self.consent_generation < 0:
            raise LivingSourceContractError("receipt consent generation is invalid")
        if self.consent_digest is not None and not re.fullmatch(r"[0-9a-f]{64}", str(self.consent_digest)):
            raise LivingSourceContractError("receipt consent digest is invalid")
        if self.consent_generation == 0 and self.consent_digest is not None:
            raise LivingSourceContractError("receipt consent digest requires a generation")

    def is_fresh(self, now: datetime | None = None) -> bool:
        if self.status not in {"ok", "empty"} or self.fresh_until is None:
            return False
        return parse_utc(self.fresh_until) > (now or utc_now())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": LIVING_SOURCE_RECEIPT_SCHEMA,
            "receipt_id": self.receipt_id,
            "request_id": self.request_id,
            "need_id": self.need_id,
            "user_id": self.user_id,
            "workspace_id": self.workspace_id,
            "session_id": self.session_id,
            "source": self.source,
            "status": self.status,
            "observed_at": self.observed_at,
            "fresh_until": self.fresh_until,
            "ttl_seconds": self.ttl_seconds,
            "payload": _canonical_value(self.payload),
            "reason": self.reason,
            "payload_digest": self.payload_digest,
            "authority": self.authority.to_dict(),
            "replay_of": self.replay_of,
            "generation": self.generation,
            "binding_id": self.binding_id,
            "consent_id": self.consent_id,
            "consent_generation": self.consent_generation,
            "consent_digest": self.consent_digest,
        }


@dataclass(frozen=True, slots=True)
class SourceContext:
    """Server-bound provider input; no caller-facing URL/path/command fields."""

    binding: SourceNeedBinding
    request: SourceRequest
    parameters: Mapping[str, Any]
    now: datetime

    def __post_init__(self) -> None:
        if self.request.need_id != self.binding.need_id or self.request.binding_id != self.binding.binding_id:
            raise LivingSourceContractError("request and binding identity mismatch")
        if self.request.scope != self.binding.scope:
            raise LivingSourceContractError("request and binding scope mismatch")
        selected = _validate_parameters(self.request.source, self.parameters)
        object.__setattr__(self, "parameters", selected)

    @property
    def owner_id(self) -> str:
        return self.binding.user_id

    @property
    def scope(self) -> tuple[str, str]:
        return self.binding.scope

    @property
    def need(self) -> SourceNeedBinding:
        """Compatibility alias; this object is only a non-authoritative binding."""

        return self.binding


def make_request_id(need_id: str, source: str, issued_at: str) -> str:
    """Derive a stable request identity for idempotent retries."""

    return "srcreq-" + stable_digest(
        {"need_id": need_id, "source": source, "issued_at": issued_at},
        namespace=LIVING_SOURCE_REQUEST_SCHEMA,
    )[:48]


def make_binding_id(need_id: str, source: str, revision: int, digest: str) -> str:
    return "srcbind-" + stable_digest(
        {"need_id": need_id, "source": source, "revision": revision, "digest": digest},
        namespace=SOURCE_NEED_BINDING_SCHEMA,
    )[:48]


def make_receipt_id(request_id: str, observed_at: str) -> str:
    return "srcrec-" + stable_digest(
        {"request_id": request_id, "observed_at": observed_at},
        namespace=LIVING_SOURCE_RECEIPT_SCHEMA,
    )[:48]
