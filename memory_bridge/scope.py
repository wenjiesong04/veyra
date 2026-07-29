from __future__ import annotations

import hashlib
import unicodedata
from typing import Any


def normalize_scope_component(
    value: Any,
    field: str,
    *,
    max_chars: int = 240,
) -> str:
    """Return one canonical scope component or fail closed.

    Scope values are identifiers, not free text. Unicode control/surrogate
    code points are rejected so they cannot be hidden in logs, paths, or
    transport fields. Leading/trailing whitespace keeps the existing
    canonicalization behavior.
    """

    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field} must contain a non-whitespace value")
    if len(normalized) > max_chars:
        raise ValueError(f"{field} exceeds {max_chars} characters")
    if any(
        unicodedata.category(character) in {"Cc", "Cs"}
        for character in normalized
    ):
        raise ValueError(f"{field} contains a prohibited control character")
    try:
        normalized.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field} is not valid UTF-8 text") from exc
    return normalized


def framed_sha256(namespace: str, *parts: Any) -> str:
    """Hash length-prefixed UTF-8 fields without delimiter ambiguity."""

    digest = hashlib.sha256()
    for value in (namespace, *parts):
        encoded = str(value).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()
