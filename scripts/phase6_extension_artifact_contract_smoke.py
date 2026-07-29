#!/usr/bin/env python3
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import sys
import unicodedata
from typing import Any, Callable

from pydantic import ValidationError


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from interface.extension_artifact import (  # noqa: E402
    EXTENSION_ARTIFACT_POLICY_REVISION,
    EXTENSION_ARTIFACT_SCHEMA_VERSION,
    MAX_EXTENSION_ARTIFACT_BYTES,
    ExtensionArtifactEnvelope,
    artifact_owner_scope_digest,
    encode_artifact_content,
    parse_extension_artifact,
)
from interface.extension_spec import EXTENSION_POLICY_REVISION  # noqa: E402


FIXED_NOW = datetime(2026, 7, 30, 8, 0, tzinfo=timezone.utc)
DEFAULT_SOURCE = (
    b"def bounded_identity(value: str) -> str:\n"
    b"    return value\n"
)


def expect(
    condition: bool,
    label: str,
    detail: Any = None,
) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"PASS {label}")


def _canonical_utc(value: datetime) -> str:
    selected = value.astimezone(timezone.utc)
    if selected.microsecond:
        return selected.isoformat(
            timespec="microseconds",
        ).replace("+00:00", "Z")
    return selected.isoformat(timespec="seconds").replace("+00:00", "Z")


def valid_artifact_envelope(
    *,
    content: bytes = DEFAULT_SOURCE,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return a reusable, internally consistent Phase 6.2b envelope."""

    current = now or FIXED_NOW
    return {
        "schema_version": EXTENSION_ARTIFACT_SCHEMA_VERSION,
        "artifact_kind": "python_source_utf8",
        "candidate_id": "extspec_0123456789abcdef01234567",
        "candidate_revision": 2,
        "owner_scope_digest": artifact_owner_scope_digest(
            "phase6-user",
            "phase6-workspace",
        ),
        "extension_id": "example.bounded_identity",
        "extension_version": 1,
        "spec_digest": "a" * 64,
        "extension_policy_revision": EXTENSION_POLICY_REVISION,
        "artifact_policy_revision": EXTENSION_ARTIFACT_POLICY_REVISION,
        "artifact_sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
        "content_b64url": encode_artifact_content(content),
        "expires_at": _canonical_utc(current + timedelta(days=7)),
    }


def _replace_content(
    payload: dict[str, Any],
    content: bytes,
) -> None:
    payload["artifact_sha256"] = hashlib.sha256(content).hexdigest()
    payload["size_bytes"] = len(content)
    payload["content_b64url"] = encode_artifact_content(content)


def rejected(
    mutate: Callable[[dict[str, Any]], None],
) -> bool:
    payload = valid_artifact_envelope()
    mutate(payload)
    try:
        parse_extension_artifact(payload)
    except (TypeError, ValueError, ValidationError):
        return True
    return False


def main() -> int:
    payload = valid_artifact_envelope()
    first = parse_extension_artifact(payload)
    reordered = {
        key: value
        for key, value in reversed(list(payload.items()))
    }
    second = ExtensionArtifactEnvelope.model_validate(
        reordered,
        strict=True,
    )
    expect(
        first.canonical_dict() == second.canonical_dict()
        and first.canonical_bytes() == second.canonical_bytes()
        and first.envelope_digest() == second.envelope_digest(),
        "valid artifact envelope has stable canonical identity",
    )
    expect(
        first.decoded_bytes() == DEFAULT_SOURCE
        and first.size_bytes == len(DEFAULT_SOURCE)
        and first.artifact_sha256
        == hashlib.sha256(DEFAULT_SOURCE).hexdigest(),
        "valid artifact envelope preserves exact source bytes",
    )
    expect(
        "content_b64url"
        not in first.canonical_dict(include_content=False),
        "content-free canonical projection omits source bytes",
    )

    for forbidden_field in (
        "path",
        "filename",
        "entrypoint",
        "command",
        "permissions",
        "signature",
        "activate",
    ):
        expect(
            rejected(
                lambda value, field=forbidden_field: value.__setitem__(
                    field,
                    "forbidden",
                )
            ),
            f"forbidden {forbidden_field} field is rejected",
        )

    coercion_checks: list[
        tuple[str, Callable[[dict[str, Any]], None]]
    ] = [
        (
            "string candidate revisions are not coerced",
            lambda value: value.__setitem__(
                "candidate_revision",
                "2",
            ),
        ),
        (
            "bool candidate revisions are not coerced",
            lambda value: value.__setitem__(
                "candidate_revision",
                True,
            ),
        ),
        (
            "float extension versions are not coerced",
            lambda value: value.__setitem__(
                "extension_version",
                1.0,
            ),
        ),
        (
            "string artifact sizes are not coerced",
            lambda value: value.__setitem__(
                "size_bytes",
                str(value["size_bytes"]),
            ),
        ),
        (
            "numeric identifiers are not coerced",
            lambda value: value.__setitem__(
                "candidate_id",
                123,
            ),
        ),
    ]
    for label, mutate in coercion_checks:
        expect(rejected(mutate), label)

    identity_and_policy_checks: list[
        tuple[str, Callable[[dict[str, Any]], None]]
    ] = [
        (
            "candidate identifiers cannot contain paths",
            lambda value: value.__setitem__(
                "candidate_id",
                "../candidate",
            ),
        ),
        (
            "candidate identifiers cannot contain whitespace",
            lambda value: value.__setitem__(
                "candidate_id",
                "candidate id",
            ),
        ),
        (
            "candidate identifiers are bounded",
            lambda value: value.__setitem__(
                "candidate_id",
                "a" * 241,
            ),
        ),
        (
            "candidate revisions must be positive",
            lambda value: value.__setitem__(
                "candidate_revision",
                0,
            ),
        ),
        (
            "extension identifiers must start with lowercase text",
            lambda value: value.__setitem__(
                "extension_id",
                "Example.invalid",
            ),
        ),
        (
            "extension identifiers cannot contain paths",
            lambda value: value.__setitem__(
                "extension_id",
                "example/invalid",
            ),
        ),
        (
            "extension identifiers are bounded",
            lambda value: value.__setitem__(
                "extension_id",
                "e" + ("x" * 120),
            ),
        ),
        (
            "extension versions must be positive",
            lambda value: value.__setitem__(
                "extension_version",
                0,
            ),
        ),
        (
            "owner scope digests must be lowercase SHA-256",
            lambda value: value.__setitem__(
                "owner_scope_digest",
                "A" * 64,
            ),
        ),
        (
            "spec digests must be lowercase SHA-256",
            lambda value: value.__setitem__(
                "spec_digest",
                "g" * 64,
            ),
        ),
        (
            "artifact digests must be exactly 64 characters",
            lambda value: value.__setitem__(
                "artifact_sha256",
                "a" * 63,
            ),
        ),
        (
            "schema versions must match exactly",
            lambda value: value.__setitem__(
                "schema_version",
                "veyra.phase6.extension_artifact_envelope.v2",
            ),
        ),
        (
            "artifact kinds must match exactly",
            lambda value: value.__setitem__(
                "artifact_kind",
                "python_bytecode",
            ),
        ),
        (
            "extension policy revisions must match exactly",
            lambda value: value.__setitem__(
                "extension_policy_revision",
                "veyra.phase6.extension_policy.v999",
            ),
        ),
        (
            "artifact policy revisions must match exactly",
            lambda value: value.__setitem__(
                "artifact_policy_revision",
                "veyra.phase6.extension_artifact_policy.v999",
            ),
        ),
    ]
    for label, mutate in identity_and_policy_checks:
        expect(rejected(mutate), label)

    base64_checks: list[
        tuple[str, Callable[[dict[str, Any]], None]]
    ] = [
        (
            "padded base64url content is rejected",
            lambda value: value.__setitem__(
                "content_b64url",
                value["content_b64url"] + "=",
            ),
        ),
        (
            "standard base64 alphabet content is rejected",
            lambda value: value.__setitem__(
                "content_b64url",
                "++//",
            ),
        ),
        (
            "base64url content with whitespace is rejected",
            lambda value: value.__setitem__(
                "content_b64url",
                value["content_b64url"] + "\n",
            ),
        ),
        (
            "non-canonical base64url trailing bits are rejected",
            lambda value: (
                value.update(
                    {
                        "artifact_sha256": hashlib.sha256(b"a").hexdigest(),
                        "size_bytes": 1,
                        "content_b64url": "YR",
                    }
                )
            ),
        ),
    ]
    for label, mutate in base64_checks:
        expect(rejected(mutate), label)

    expect(
        rejected(lambda value: _replace_content(value, b"")),
        "empty artifact content is rejected",
    )
    expect(
        rejected(
            lambda value: _replace_content(
                value,
                b"x" * (MAX_EXTENSION_ARTIFACT_BYTES + 1),
            )
        ),
        "oversized artifact content is rejected",
    )
    expect(
        rejected(
            lambda value: value.__setitem__(
                "size_bytes",
                value["size_bytes"] + 1,
            )
        ),
        "artifact size mismatches are rejected",
    )
    expect(
        rejected(
            lambda value: value.__setitem__(
                "artifact_sha256",
                "f" * 64,
            )
        ),
        "artifact hash mismatches are rejected",
    )

    content_policy_checks: list[
        tuple[str, Callable[[dict[str, Any]], None]]
    ] = [
        (
            "UTF-8 BOM content is rejected",
            lambda value: _replace_content(
                value,
                b"\xef\xbb\xbfprint('no')\n",
            ),
        ),
        (
            "NUL bytes are rejected",
            lambda value: _replace_content(
                value,
                b"before\x00after\n",
            ),
        ),
        (
            "CRLF source is rejected",
            lambda value: _replace_content(
                value,
                b"line_one\r\nline_two\r\n",
            ),
        ),
        (
            "bare carriage returns are rejected",
            lambda value: _replace_content(
                value,
                b"line_one\rline_two\n",
            ),
        ),
        (
            "invalid UTF-8 source is rejected",
            lambda value: _replace_content(
                value,
                b"invalid:\xff\n",
            ),
        ),
        (
            "non-NFC source is rejected",
            lambda value: _replace_content(
                value,
                "Cafe\u0301\n".encode("utf-8"),
            ),
        ),
    ]
    for label, mutate in content_policy_checks:
        expect(rejected(mutate), label)
    expect(
        unicodedata.normalize("NFC", "Cafe\u0301") == "Café",
        "non-NFC fixture exercises canonical Unicode normalization",
    )

    timestamp_checks: list[
        tuple[str, Callable[[dict[str, Any]], None]]
    ] = [
        (
            "UTC offsets are not canonical timestamps",
            lambda value: value.__setitem__(
                "expires_at",
                "2026-08-06T08:00:00+00:00",
            ),
        ),
        (
            "space-separated UTC timestamps are not canonical",
            lambda value: value.__setitem__(
                "expires_at",
                "2026-08-06 08:00:00Z",
            ),
        ),
        (
            "short fractional UTC timestamps are not canonical",
            lambda value: value.__setitem__(
                "expires_at",
                "2026-08-06T08:00:00.1Z",
            ),
        ),
        (
            "redundant zero fractions are not canonical",
            lambda value: value.__setitem__(
                "expires_at",
                "2026-08-06T08:00:00.000000Z",
            ),
        ),
        (
            "lowercase UTC suffixes are not canonical",
            lambda value: value.__setitem__(
                "expires_at",
                "2026-08-06T08:00:00z",
            ),
        ),
    ]
    for label, mutate in timestamp_checks:
        expect(rejected(mutate), label)

    mutated_instance = parse_extension_artifact(
        valid_artifact_envelope()
    )
    object.__setattr__(
        mutated_instance,
        "artifact_sha256",
        "f" * 64,
    )
    try:
        parse_extension_artifact(mutated_instance)
    except (TypeError, ValueError, ValidationError):
        pass
    else:
        raise AssertionError(
            "mutated data in a frozen artifact model was trusted"
        )
    print("PASS existing artifact model instances are revalidated")

    print("phase6 extension artifact contract smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
