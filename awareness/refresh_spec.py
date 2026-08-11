from __future__ import annotations

import re
from typing import Any


REFRESH_SPEC_SCHEMA_VERSION = "veyra.belief.refresh_spec.v1"
REFRESH_RESOLVER_LITERAL = "literal_target.v1"
REFRESH_RESOLVER_DEFAULT = "probe_default.v1"
_PROBE_KIND = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


def make_refresh_spec(
    *,
    probe_kind: str,
    target_ref: str = "",
    resolver_id: str,
) -> dict[str, str]:
    """Build the only refresh target contract accepted by Belief claims."""

    return validate_refresh_spec(
        {
            "schema_version": REFRESH_SPEC_SCHEMA_VERSION,
            "probe_kind": probe_kind,
            "target_ref": target_ref,
            "resolver_id": resolver_id,
        }
    )


def validate_refresh_spec(
    value: Any,
    *,
    source: str | None = None,
) -> dict[str, str]:
    """Validate a durable, non-prose refresh binding.

    ``target_ref`` is intentionally opaque to this contract. The selected
    read-only probe owns its interpretation; this layer only guarantees that
    a stale claim cannot silently switch probe kind or resolver identity.
    """

    if not isinstance(value, dict):
        raise ValueError("refresh_spec must be an object")
    expected_keys = {
        "schema_version",
        "probe_kind",
        "target_ref",
        "resolver_id",
    }
    if set(value) != expected_keys:
        raise ValueError("refresh_spec fields are not canonical")
    schema_version = value.get("schema_version")
    probe_kind = value.get("probe_kind")
    target_ref = value.get("target_ref")
    resolver_id = value.get("resolver_id")
    if schema_version != REFRESH_SPEC_SCHEMA_VERSION:
        raise ValueError("refresh_spec schema is unsupported")
    if not isinstance(probe_kind, str) or not _PROBE_KIND.fullmatch(probe_kind):
        raise ValueError("refresh_spec probe kind is invalid")
    if source is not None and probe_kind != source:
        raise ValueError("refresh_spec probe kind does not match claim source")
    if not isinstance(target_ref, str) or len(target_ref) > 2048:
        raise ValueError("refresh_spec target ref is invalid")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in target_ref):
        raise ValueError("refresh_spec target ref contains control characters")
    if resolver_id not in {REFRESH_RESOLVER_LITERAL, REFRESH_RESOLVER_DEFAULT}:
        raise ValueError("refresh_spec resolver is unsupported")
    if resolver_id == REFRESH_RESOLVER_LITERAL and not target_ref.strip():
        raise ValueError("literal refresh target is empty")
    if resolver_id == REFRESH_RESOLVER_DEFAULT and target_ref:
        raise ValueError("default refresh resolver cannot carry a target")
    return {
        "schema_version": schema_version,
        "probe_kind": probe_kind,
        "target_ref": target_ref,
        "resolver_id": resolver_id,
    }


__all__ = [
    "REFRESH_RESOLVER_DEFAULT",
    "REFRESH_RESOLVER_LITERAL",
    "REFRESH_SPEC_SCHEMA_VERSION",
    "make_refresh_spec",
    "validate_refresh_spec",
]
