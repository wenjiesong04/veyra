"""Pure durable-state validation for the Living Source runtime.

The runtime owns the row rehydration methods and the WorldStateStore
transaction.  This helper owns the schema/index/integrity pass so the request
controller does not become a second state-management god module.
"""

from __future__ import annotations

import math
from datetime import timezone
from typing import Any, Mapping

from common.living_source_primitives import (
    MAX_BINDINGS,
    MAX_CONSENTS,
    MAX_RECEIPTS,
    RUNTIME_SCHEMA,
    SourceAdmissionError,
    SourceStateCorruptError,
    _aware_now,
)
from interface.living_source_contract import (
    SourceAuthority,
    SourceConsent,
    SourceNeedBinding,
    SourceReceipt,
    SourceRequest,
    consent_digest_from_record,
    stable_digest,
)


_STATE_KEYS = frozenset(
    {
        "schema_version",
        "bindings",
        "consents",
        "requests",
        "receipts",
        "request_keys",
        # WorldStateStore enriches every registered JSON state with its
        # public metadata contract.  These are part of the exact durable
        # schema, not arbitrary passthrough keys.
        "source",
        "confidence",
        "ttl_seconds",
        "status",
        "_state_revision",
        "updated_at",
    }
)

_STATE_SOURCE = "living_source_runtime"
_STATE_TTL_SECONDS = 0
_STATE_STATUS = "fresh"
_BINDING_KEYS = frozenset(
    {
        "schema_version",
        "binding_id",
        "need_id",
        "need_revision",
        "record_digest",
        "user_id",
        "workspace_id",
        "session_id",
        "situation_id",
        "source",
        "parameters",
        "issued_at",
        "expires_at",
    }
)
_LEGACY_BINDING_KEYS = frozenset(set(_BINDING_KEYS) - {"record_digest"} | {"need_digest"})
_CONSENT_KEYS = frozenset(
    {
        "schema_version",
        "consent_id",
        "user_id",
        "workspace_id",
        "session_id",
        "source",
        "purpose",
        "granted_at",
        "expires_at",
        "granted",
        "generation",
    }
)
_REQUEST_KEYS = frozenset(
    {
        "schema_version",
        "request_id",
        "need_id",
        "user_id",
        "workspace_id",
        "session_id",
        "source",
        "issued_at",
        "expires_at",
        "timeout_seconds",
        "parameters_digest",
        "binding_id",
        "attempt",
        "consent_id",
        "consent_generation",
        "consent_digest",
        "status",
        "receipt_id",
        "generation",
        "authority",
    }
)
_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "receipt_id",
        "request_id",
        "need_id",
        "user_id",
        "workspace_id",
        "session_id",
        "source",
        "status",
        "observed_at",
        "fresh_until",
        "ttl_seconds",
        "payload",
        "reason",
        "payload_digest",
        "authority",
        "replay_of",
        "generation",
        "binding_id",
        "consent_id",
        "consent_generation",
        "consent_digest",
    }
)

# Public aliases used by the runtime's row rehydrators.  The canonical schema
# sets remain owned by this module.
BINDING_KEYS = _BINDING_KEYS
LEGACY_BINDING_KEYS = _LEGACY_BINDING_KEYS
CONSENT_KEYS = _CONSENT_KEYS
REQUEST_KEYS = _REQUEST_KEYS
RECEIPT_KEYS = _RECEIPT_KEYS


def binding_from_dict(raw: Mapping[str, Any]) -> SourceNeedBinding:
    value = dict(raw)
    if set(value) not in {BINDING_KEYS, LEGACY_BINDING_KEYS} or value.get("schema_version") != "veyra.source_need_binding.v1":
        raise SourceStateCorruptError("source binding schema is unsupported")
    value.pop("schema_version")
    if "record_digest" in value:
        value["need_digest"] = value.pop("record_digest")
    return SourceNeedBinding(**value)


def consent_from_dict(raw: Mapping[str, Any]) -> SourceConsent:
    value = dict(raw)
    if set(value) != CONSENT_KEYS or value.get("schema_version") != "veyra.living_source.consent.v1":
        raise SourceStateCorruptError("source consent schema is unsupported")
    value.pop("schema_version")
    return SourceConsent(**value)


def request_from_dict(raw: Mapping[str, Any]) -> SourceRequest:
    if not raw:
        raise SourceAdmissionError("unknown source request")
    value = dict(raw)
    if set(value) != REQUEST_KEYS or value.get("schema_version") != "veyra.living_source.request.v1" or value.get("authority") != SourceAuthority().to_dict():
        raise SourceStateCorruptError("source request schema or authority is invalid")
    value.pop("schema_version")
    value.pop("authority")
    return SourceRequest(**value)


def receipt_from_dict(raw: Mapping[str, Any]) -> SourceReceipt:
    if not raw:
        raise SourceAdmissionError("unknown source receipt")
    value = dict(raw)
    if set(value) != RECEIPT_KEYS or value.get("schema_version") != "veyra.living_source.receipt.v1" or value.get("authority") != SourceAuthority().to_dict():
        raise SourceStateCorruptError("source receipt schema or authority is invalid")
    value.pop("schema_version")
    value.pop("authority")
    return SourceReceipt(**value)


def validate_state(runtime: Any, raw: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and rehydrate one state snapshot without mutating it."""

    if not isinstance(raw, Mapping):
        raise SourceStateCorruptError("living source state must be an object")
    if not raw:
        path = runtime.state_store.path_for(runtime.state_file)
        if path.exists():
            try:
                path.read_bytes()
            except OSError as exc:
                raise SourceStateCorruptError("living source state cannot be read") from exc
            raise SourceStateCorruptError("living source state is empty or unsupported")
        return runtime._empty_state()
    if raw.get("_state_corrupt") or raw.get("schema_version") != RUNTIME_SCHEMA:
        raise SourceStateCorruptError("living source state schema is unsupported or corrupt")
    selected = dict(raw)
    if set(selected) != _STATE_KEYS:
        raise SourceStateCorruptError("living source state keys are unsupported or incomplete")
    if selected.get("source") != _STATE_SOURCE:
        raise SourceStateCorruptError("living source state source metadata is invalid")
    confidence = selected.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(float(confidence)) or not 0.0 <= float(confidence) <= 1.0:
        raise SourceStateCorruptError("living source state confidence metadata is invalid")
    if isinstance(selected.get("ttl_seconds"), bool) or selected.get("ttl_seconds") != _STATE_TTL_SECONDS:
        raise SourceStateCorruptError("living source state ttl metadata is invalid")
    if selected.get("status") != _STATE_STATUS:
        raise SourceStateCorruptError("living source state status metadata is invalid")
    revision = selected.get("_state_revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise SourceStateCorruptError("living source state revision metadata is invalid")
    if not isinstance(selected.get("updated_at"), str) or not selected.get("updated_at"):
        raise SourceStateCorruptError("living source state updated_at metadata is invalid")
    for key in ("bindings", "consents", "requests", "receipts", "request_keys"):
        if not isinstance(selected.get(key), dict):
            raise SourceStateCorruptError(f"living source state {key} is invalid")
    if len(selected["bindings"]) > MAX_BINDINGS:
        raise SourceStateCorruptError("living source binding index exceeds its bounded cap")
    if len(selected["consents"]) > MAX_CONSENTS:
        raise SourceStateCorruptError("living source consent index exceeds its bounded cap")
    configured_receipt_cap = int(getattr(runtime, "_max_receipts", MAX_RECEIPTS) or MAX_RECEIPTS)
    if len(selected["requests"]) > configured_receipt_cap or len(selected["receipts"]) > configured_receipt_cap:
        raise SourceStateCorruptError("living source request/receipt index exceeds its bounded cap")
    if len(selected["request_keys"]) > configured_receipt_cap:
        raise SourceStateCorruptError("living source request index exceeds its bounded cap")

    bindings: dict[str, Any] = {}
    for key, raw_binding in selected["bindings"].items():
        try:
            binding = runtime._binding_from_dict(raw_binding)
            if key != binding.binding_id:
                raise ValueError("binding row/index identity mismatch")
        except Exception as exc:
            raise SourceStateCorruptError("stored source binding is invalid") from exc
        bindings[key] = binding

    consents: dict[str, Any] = {}
    for key, raw_consent in selected["consents"].items():
        try:
            consent = runtime._consent_from_dict(raw_consent)
            if key != consent.consent_id:
                raise ValueError("consent row/index identity mismatch")
        except Exception as exc:
            raise SourceStateCorruptError("stored source consent is invalid") from exc
        consents[key] = consent

    requests: dict[str, Any] = {}
    for key, raw_request in selected["requests"].items():
        try:
            request = runtime._request_from_dict(raw_request)
            if key != request.request_id:
                raise ValueError("request row/index identity mismatch")
            binding = bindings.get(request.binding_id or "")
            if (
                binding is None
                or request.need_id != binding.need_id
                or request.source != binding.source
                or request.scope != binding.scope
            ):
                raise ValueError("request/binding identity mismatch")
            if stable_digest(binding.parameters, namespace="source.parameters") != request.parameters_digest:
                raise ValueError("request parameter digest mismatch")
            if request.consent_id:
                consent = consents.get(request.consent_id)
                if consent is None or consent.scope != request.scope or consent.source != request.source:
                    raise ValueError("request/consent identity mismatch")
                if consent.granted and request.consent_generation == consent.generation and request.consent_digest != consent_digest_from_record(consent.to_dict()):
                    raise ValueError("request consent digest mismatch")
            elif request.consent_generation or request.consent_digest is not None:
                raise ValueError("request has orphaned consent metadata")
            if request.receipt_id and request.receipt_id not in selected["receipts"]:
                raise ValueError("request references a missing receipt")
            if request.status in {"running", "pending"} and not request.receipt_id:
                raise ValueError("inflight request has no receipt")
        except Exception as exc:
            raise SourceStateCorruptError("stored source request is invalid") from exc
        requests[key] = request

    for key, raw_receipt in selected["receipts"].items():
        try:
            receipt = runtime._receipt_from_dict(raw_receipt)
            if key != receipt.receipt_id:
                raise ValueError("receipt row/index identity mismatch")
            request = requests.get(receipt.request_id)
            if request is None or request.receipt_id != receipt.receipt_id:
                raise ValueError("receipt/request reverse reference mismatch")
            runtime._assert_receipt_matches_request(receipt, request)
        except Exception as exc:
            raise SourceStateCorruptError("stored source receipt is invalid") from exc

    for key, request_id in selected["request_keys"].items():
        request = requests.get(str(request_id))
        if request is None or key != f"{request.binding_id}:{request.source}":
            raise SourceStateCorruptError("source request index is inconsistent")
    indexed = set(selected["request_keys"].values())
    for request in requests.values():
        if request.status in {"admitted", "running", "pending"} and request.request_id not in indexed:
            raise SourceStateCorruptError("active request is missing from source request index")
    return selected


def scope_status(runtime: Any, *, user_id: str, session_id: str, now: Any = None) -> dict[str, Any]:
    """Build the product-safe, exact-scope read projection."""

    scope = (str(user_id or ""), str(session_id or ""))
    runtime._assert_scope(scope, user_id=user_id, session_id=session_id)
    selected_now = (now or _aware_now(runtime._clock)).astimezone(timezone.utc)
    capabilities = _status_capabilities(runtime)
    try:
        state = runtime._read_state()
    except SourceStateCorruptError as exc:
        return {
            "schema_version": RUNTIME_SCHEMA,
            "status": "degraded",
            "state_corrupt": True,
            "reason": str(exc),
            "scope": {"user_id": scope[0], "session_id": scope[1]},
            "capabilities": capabilities,
            "consent": {},
            "receipts": {},
        }

    consent_rows: dict[str, Any] = {}
    for raw in state["consents"].values():
        consent = runtime._consent_from_dict(raw)
        if consent.scope != scope:
            continue
        previous = consent_rows.get(consent.source)
        if previous is None or consent.generation > previous.generation:
            consent_rows[consent.source] = consent

    consent_status: dict[str, dict[str, Any]] = {}
    for source, capability in runtime._capabilities.items():
        consent = consent_rows.get(source)
        active = bool(consent and consent.active_at(selected_now))
        consent_status[source] = {
            "required": capability.consent_required,
            "granted": active if capability.consent_required else True,
            "consented": active if capability.consent_required else True,
            "generation": consent.generation if consent else 0,
            "expires_at": consent.expires_at if consent else None,
        }
        capability_projection = capabilities.get(source)
        if isinstance(capability_projection, dict):
            consented = bool(consent_status[source]["granted"])
            capability_projection["consented"] = consented
            # ``available`` means usable by a governed source read.  A
            # configured provider without user consent is intentionally not
            # available; Product still receives the separate configured and
            # consented fields to render the consent action.
            capability_projection["available"] = bool(
                capability_projection.get("enabled")
                and capability_projection.get("configured")
                and capability_projection.get("system_permission") in {"not_required", "ready"}
                and consented
            )
            # A configured, consented provider with an unconfirmed macOS TCC
            # state may perform one bounded read to establish that state.  It
            # is attemptable, but not yet available until the read succeeds.
            can_request = bool(
                capability_projection.get("enabled")
                and capability_projection.get("configured")
                and capability_projection.get("system_permission") in {"unknown", "not_required", "ready"}
                and consented
            )
            capability_projection["attemptable"] = can_request
            capability_projection["can_request"] = can_request

    receipt_status: dict[str, dict[str, Any]] = {
        source: {
            "count": 0,
            "fresh_count": 0,
            "stale_count": 0,
            "revoked_count": 0,
            "other_count": 0,
            "latest_observed_at": None,
        }
        for source in runtime._capabilities
    }
    for raw in state["receipts"].values():
        receipt = runtime._receipt_from_dict(raw)
        if receipt.scope != scope:
            continue
        summary = receipt_status[receipt.source]
        summary["count"] += 1
        latest = summary["latest_observed_at"]
        if latest is None or receipt.observed_at > latest:
            summary["latest_observed_at"] = receipt.observed_at
        consent = consent_rows.get(receipt.source)
        consent_invalid = runtime._capabilities[receipt.source].consent_required and (
            consent is None
            or not consent.active_at(selected_now)
            or receipt.consent_generation != consent.generation
            or receipt.consent_digest != consent_digest_from_record(consent.to_dict())
        )
        if receipt.status == "revoked" or consent_invalid:
            summary["revoked_count"] += 1
        elif receipt.status in {"ok", "empty"} and receipt.is_fresh(selected_now):
            summary["fresh_count"] += 1
        elif receipt.status in {"ok", "empty"}:
            summary["stale_count"] += 1
        else:
            summary["other_count"] += 1

    return {
        "schema_version": RUNTIME_SCHEMA,
        "status": "ok",
        "state_corrupt": False,
        "scope": {"user_id": scope[0], "session_id": scope[1]},
        "capabilities": capabilities,
        "consent": consent_status,
        "receipts": receipt_status,
    }


def _status_capabilities(runtime: Any) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for source, capability in runtime._capabilities.items():
        provider = runtime._providers.get(source)
        provider_id = str(getattr(provider, "provider_id", "") or "")
        configured_value = getattr(provider, "configured", None)
        configured = bool(configured_value) if isinstance(configured_value, bool) else (
            provider is not None
            and provider_id not in {"calendar.disabled.v1", "agent_research.unavailable.v1"}
        )
        permission = str(getattr(provider, "system_permission", "not_required") or "not_required").strip().lower()
        if permission not in {"not_configured", "not_required", "unknown", "ready", "denied"}:
            permission = "unknown"
        result[source] = {
            "enabled": capability.enabled,
            "configured": configured,
            "system_permission": permission,
            "consented": False,
            "available": False,
            "attemptable": False,
            "can_request": False,
            "consent_required": capability.consent_required,
            "provider_id": provider_id or capability.provider_id,
        }
    return result


__all__ = [
    "BINDING_KEYS",
    "binding_from_dict",
    "consent_from_dict",
    "LEGACY_BINDING_KEYS",
    "CONSENT_KEYS",
    "RECEIPT_KEYS",
    "REQUEST_KEYS",
    "receipt_from_dict",
    "request_from_dict",
    "scope_status",
    "validate_state",
]
