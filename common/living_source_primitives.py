"""Dependency-free primitives shared by the Living Source interface/runtime.

This module is deliberately below both the public interface contracts and the
runtime controller.  It may contain validation errors, constants, time and
digest helpers, and the small capability value objects needed by the registry,
but it must never import either the contract module or the runtime module.
Keeping these names here makes import order explicit and prevents helper
modules from reaching back into their controller to obtain constants/errors.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Callable, Literal, Mapping


# Interface contract constants.  These values are intentionally unchanged;
# durable rows and digest/ID derivation depend on them.
LIVING_SOURCE_SCHEMA = "veyra.living_source.contract.v1"
LIVING_SOURCE_REQUEST_SCHEMA = "veyra.living_source.request.v1"
LIVING_SOURCE_RECEIPT_SCHEMA = "veyra.living_source.receipt.v1"
LIVING_SOURCE_CONSENT_SCHEMA = "veyra.living_source.consent.v1"
SOURCE_NEED_BINDING_SCHEMA = "veyra.source_need_binding.v1"

SourceKind = Literal[
    "user_answer",
    "calendar",
    "weather",
    "public_web",
    "agent_research",
]

SOURCE_KINDS: frozenset[str] = frozenset(
    {"user_answer", "calendar", "weather", "public_web", "agent_research"}
)
READ_ONLY_SOURCE_KINDS: frozenset[str] = SOURCE_KINDS

# Parameters are bound to a server-issued source binding, never a request.
SOURCE_PARAMETER_KEYS: dict[str, frozenset[str]] = {
    "user_answer": frozenset(),
    # Calendar selection is server configuration, not a caller/model input.
    "calendar": frozenset({"window_start", "window_end"}),
    # ``target_date`` is optional: its absence means a current observation;
    # its presence selects one typed forecast day.  It is still server-bound
    # through SourceNeedBinding and never accepted from a provider/caller.
    "weather": frozenset({"location", "target_date"}),
    "public_web": frozenset({"query", "max_results"}),
    "agent_research": frozenset({"topic"}),
}
FORBIDDEN_PARAMETER_KEYS: frozenset[str] = frozenset(
    "url urls uri path paths file filename command cmd shell tool tools tool_args args arguments "
    "env headers credential credentials token recipient recipients external_target".split()
)

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@+%#=~-]{0,239}$")
_OWNER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@+%#=~-]{0,239}$")


class LivingSourceContractError(ValueError):
    """Raised when a source contract cannot be admitted safely."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise LivingSourceContractError("timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LivingSourceContractError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def canonical_utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise LivingSourceContractError("timestamp must include a timezone")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _canonical_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _canonical_value(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, datetime):
        return canonical_utc(value)
    return value


def stable_digest(value: Any, *, namespace: str = LIVING_SOURCE_SCHEMA) -> str:
    encoded = json.dumps(
        {"namespace": namespace, "value": _canonical_value(value)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def need_digest_from_record(record: Mapping[str, Any]) -> str:
    """Return the authoritative server ``record_digest``.

    ``need_digest`` remains accepted only as a migration alias for older
    resolver payloads.  Source code must not derive a second digest from a
    partial record.
    """

    digest = record.get("record_digest") or record.get("need_digest")
    if not re.fullmatch(r"[0-9a-f]{64}", str(digest or "")):
        raise LivingSourceContractError("authoritative InformationNeed record_digest is required")
    return str(digest)


def consent_digest_from_record(consent: Mapping[str, Any]) -> str:
    return stable_digest(dict(consent), namespace="veyra.source_consent.digest.v1")


def _require_identifier(value: Any, label: str, *, owner: bool = False) -> str:
    selected = str(value or "").strip()
    pattern = _OWNER if owner else _IDENTIFIER
    if not pattern.fullmatch(selected):
        raise LivingSourceContractError(f"{label} is invalid")
    return selected


def _require_time(value: Any, label: str) -> str:
    selected = str(value or "").strip()
    parsed = parse_utc(selected)
    canonical = canonical_utc(parsed)
    if selected != canonical:
        raise LivingSourceContractError(f"{label} must be canonical UTC")
    return canonical


def _scope_tuple(user_id: str, session_id: str) -> tuple[str, str]:
    """Return the semantic Living Context scope.

    ``workspace_id`` is compatibility/server-installation metadata and is
    deliberately not part of the person/session identity.
    """

    return (str(user_id), str(session_id))


@dataclass(frozen=True, slots=True)
class SourceAuthority:
    """Informational sources never inherit route or execution authority."""

    route_change: bool = False
    agent_dispatch: bool = False
    tool_call: bool = False
    capability_grant: bool = False
    external_delivery: bool = False
    execution: bool = False
    fact_certification: bool = False

    def __post_init__(self) -> None:
        values = (
            self.route_change,
            self.agent_dispatch,
            self.tool_call,
            self.capability_grant,
            self.external_delivery,
            self.execution,
            self.fact_certification,
        )
        if any(value is not False for value in values):
            raise LivingSourceContractError("living source authority must remain false")

    def to_dict(self) -> dict[str, bool]:
        return {
            "route_change": False,
            "agent_dispatch": False,
            "tool_call": False,
            "capability_grant": False,
            "external_delivery": False,
            "execution": False,
            "fact_certification": False,
        }


@dataclass(frozen=True, slots=True)
class SourceCapability:
    source: SourceKind
    provider_id: str
    description: str
    consent_required: bool = True
    read_only: bool = True
    enabled: bool = True
    default_ttl_seconds: int = 900
    max_timeout_seconds: float = 5.0
    # Provider-owned cadence for an explicitly watch-mode Need. ``None``
    # means the source has no automatic refresh policy.
    watch_cadence_seconds: int | None = None
    allowed_parameter_keys: tuple[str, ...] = ()
    authority: SourceAuthority = field(default_factory=SourceAuthority)

    def __post_init__(self) -> None:
        if self.source not in SOURCE_KINDS:
            raise LivingSourceContractError("source kind is not registered")
        _require_identifier(self.provider_id, "provider_id")
        if not str(self.description).strip() or len(self.description) > 600:
            raise LivingSourceContractError("source description is invalid")
        if not self.read_only:
            raise LivingSourceContractError("living sources must be read-only")
        if isinstance(self.default_ttl_seconds, bool) or not 1 <= self.default_ttl_seconds <= 604800:
            raise LivingSourceContractError("source TTL is out of range")
        if isinstance(self.max_timeout_seconds, bool) or not 0.05 <= float(self.max_timeout_seconds) <= 60.0:
            raise LivingSourceContractError("source timeout is out of range")
        if self.watch_cadence_seconds is not None and (
            isinstance(self.watch_cadence_seconds, bool)
            or not isinstance(self.watch_cadence_seconds, int)
            or not 60 <= self.watch_cadence_seconds <= 604800
        ):
            raise LivingSourceContractError("source watch cadence is out of range")
        allowed = set(self.allowed_parameter_keys)
        if not allowed.issubset(SOURCE_PARAMETER_KEYS[self.source]):
            raise LivingSourceContractError("source capability parameter mapping is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "provider_id": self.provider_id,
            "description": self.description,
            "consent_required": self.consent_required,
            "read_only": True,
            "enabled": self.enabled,
            "default_ttl_seconds": self.default_ttl_seconds,
            "max_timeout_seconds": self.max_timeout_seconds,
            "watch_cadence_seconds": self.watch_cadence_seconds,
            "allowed_parameter_keys": list(self.allowed_parameter_keys),
            "authority": self.authority.to_dict(),
        }


# Runtime constants/errors also live here so state/execution helpers do not
# import their controller to reach them.  The controller re-exports these
# names for backwards compatibility with existing callers.
RUNTIME_SCHEMA = "veyra.living_source.runtime.v1"
MAX_RECEIPTS = 500
MAX_BINDINGS = 200
MAX_CONSENTS = 100
MAX_ATTEMPTS = 4
MAX_FLIGHT_LEASES = 128
RETRY_BASE_SECONDS = 0.5
MAX_RETRY_DELAY_SECONDS = 60.0

_ACTIVE_NEED_STATUSES = frozenset({"open", "asked", "observing", "waiting"})
_SOURCE_POLICY_CLASS = {
    "user_answer": "user",
    "calendar": "calendar",
    "weather": "weather",
    "public_web": "public_web",
    "agent_research": "agent",
}
_RETRYABLE_RECEIPTS = frozenset({"unknown", "timeout", "unavailable"})
_TERMINAL_REQUESTS = frozenset(
    {"completed", "denied", "expired", "unknown", "timeout", "unavailable", "stale", "revoked"}
)


class LivingSourceRuntimeError(RuntimeError):
    """Base error for source admission and durable state failures."""


class SourceAdmissionError(LivingSourceRuntimeError):
    """Raised when a server-issued need cannot be resolved safely."""


class SourceStateCorruptError(LivingSourceRuntimeError):
    """Raised for corrupt or unsupported source state; no repair is implicit."""


class SourceCapacityError(LivingSourceRuntimeError):
    """Raised when bounded retention cannot remove a terminal record safely."""


def _aware_now(clock: Callable[[], datetime]) -> datetime:
    selected = clock()
    if selected.tzinfo is None or selected.utcoffset() is None:
        raise LivingSourceRuntimeError("runtime clock must return an aware datetime")
    return selected.astimezone(timezone.utc)


def parse_time(value: str) -> datetime:
    try:
        selected = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise LivingSourceRuntimeError("source timestamp is invalid") from exc
    if selected.tzinfo is None or selected.utcoffset() is None:
        raise LivingSourceRuntimeError("source timestamp must be timezone-aware")
    return selected.astimezone(timezone.utc)


__all__ = [
    "FORBIDDEN_PARAMETER_KEYS",
    "LIVING_SOURCE_CONSENT_SCHEMA",
    "LIVING_SOURCE_RECEIPT_SCHEMA",
    "LIVING_SOURCE_REQUEST_SCHEMA",
    "LIVING_SOURCE_SCHEMA",
    "MAX_ATTEMPTS",
    "MAX_BINDINGS",
    "MAX_CONSENTS",
    "MAX_FLIGHT_LEASES",
    "MAX_RECEIPTS",
    "MAX_RETRY_DELAY_SECONDS",
    "READ_ONLY_SOURCE_KINDS",
    "RETRY_BASE_SECONDS",
    "RUNTIME_SCHEMA",
    "SOURCE_KINDS",
    "SOURCE_NEED_BINDING_SCHEMA",
    "SOURCE_PARAMETER_KEYS",
    "SourceAdmissionError",
    "SourceAuthority",
    "SourceCapacityError",
    "SourceCapability",
    "SourceKind",
    "SourceStateCorruptError",
    "LivingSourceContractError",
    "LivingSourceRuntimeError",
    "_ACTIVE_NEED_STATUSES",
    "_RETRYABLE_RECEIPTS",
    "_SOURCE_POLICY_CLASS",
    "_TERMINAL_REQUESTS",
    "_aware_now",
    "_canonical_value",
    "_require_identifier",
    "_require_time",
    "_scope_tuple",
    "canonical_utc",
    "consent_digest_from_record",
    "need_digest_from_record",
    "parse_time",
    "parse_utc",
    "stable_digest",
    "utc_now",
]
