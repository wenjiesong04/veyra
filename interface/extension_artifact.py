from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
import re
import unicodedata
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)

from interface.extension_spec import EXTENSION_POLICY_REVISION


EXTENSION_ARTIFACT_SCHEMA_VERSION = (
    "veyra.phase6.extension_artifact_envelope.v1"
)
EXTENSION_ARTIFACT_POLICY_REVISION = (
    "veyra.phase6.extension_artifact_policy.v1"
)
MAX_EXTENSION_ARTIFACT_BYTES = 65_536
MAX_EXTENSION_ARTIFACT_CANONICAL_BYTES = 100_000

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,239}$")
_EXTENSION_ID = re.compile(r"^[a-z][a-z0-9_.-]{0,119}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_BASE64URL = re.compile(r"^[A-Za-z0-9_-]+$")


class ExtensionArtifactEnvelope(BaseModel):
    """Strict, non-executable source artifact admitted by Phase 6.2b.

    The envelope has no path, filename, command, environment, entrypoint,
    dependency, permission, signature, installation, or activation field.
    Decoding and integrity validation never parse or execute the source.
    """

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal[EXTENSION_ARTIFACT_SCHEMA_VERSION]
    artifact_kind: Literal["python_source_utf8"]
    candidate_id: str = Field(min_length=1, max_length=240)
    candidate_revision: StrictInt = Field(ge=1, le=2_147_483_647)
    owner_scope_digest: str = Field(min_length=64, max_length=64)
    extension_id: str = Field(min_length=1, max_length=120)
    extension_version: StrictInt = Field(ge=1, le=2_147_483_647)
    spec_digest: str = Field(min_length=64, max_length=64)
    extension_policy_revision: Literal[EXTENSION_POLICY_REVISION]
    artifact_policy_revision: Literal[
        EXTENSION_ARTIFACT_POLICY_REVISION
    ]
    artifact_sha256: str = Field(min_length=64, max_length=64)
    size_bytes: StrictInt = Field(
        ge=1,
        le=MAX_EXTENSION_ARTIFACT_BYTES,
    )
    content_b64url: str = Field(
        min_length=2,
        max_length=90_000,
    )
    expires_at: str = Field(min_length=20, max_length=32)

    @field_validator(
        "candidate_id",
    )
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError("candidate_id must be a bounded identifier")
        return value

    @field_validator("extension_id")
    @classmethod
    def validate_extension_id(cls, value: str) -> str:
        if not _EXTENSION_ID.fullmatch(value):
            raise ValueError("extension_id must be canonical")
        return value

    @field_validator(
        "owner_scope_digest",
        "spec_digest",
        "artifact_sha256",
    )
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if not _DIGEST.fullmatch(value):
            raise ValueError("artifact binding digests must be SHA-256")
        return value

    @field_validator("expires_at")
    @classmethod
    def validate_expires_at(cls, value: str) -> str:
        parsed = _parse_canonical_utc(value)
        if _canonical_utc(parsed) != value:
            raise ValueError("expires_at must be canonical UTC")
        return value

    @model_validator(mode="after")
    def validate_content(self) -> "ExtensionArtifactEnvelope":
        content = _decode_canonical_base64url(self.content_b64url)
        if len(content) != self.size_bytes:
            raise ValueError("artifact size does not match size_bytes")
        if len(content) > MAX_EXTENSION_ARTIFACT_BYTES:
            raise ValueError("artifact exceeds the byte budget")
        if hashlib.sha256(content).hexdigest() != self.artifact_sha256:
            raise ValueError("artifact digest mismatch")
        if content.startswith(b"\xef\xbb\xbf"):
            raise ValueError("UTF-8 BOM is not allowed")
        if b"\x00" in content:
            raise ValueError("NUL bytes are not allowed")
        if b"\r" in content:
            raise ValueError("artifact source must use LF newlines")
        try:
            text = content.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ValueError("artifact must be strict UTF-8") from exc
        if unicodedata.normalize("NFC", text) != text:
            raise ValueError("artifact source must use NFC Unicode")
        self.canonical_bytes()
        return self

    def decoded_bytes(self) -> bytes:
        return _decode_canonical_base64url(self.content_b64url)

    def canonical_dict(
        self,
        *,
        include_content: bool = True,
    ) -> dict[str, Any]:
        payload = self.model_dump(mode="json", by_alias=True)
        if not include_content:
            payload.pop("content_b64url", None)
        return payload

    def canonical_bytes(
        self,
        *,
        include_content: bool = True,
    ) -> bytes:
        payload = json.dumps(
            self.canonical_dict(include_content=include_content),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(payload) > MAX_EXTENSION_ARTIFACT_CANONICAL_BYTES:
            raise ValueError("canonical artifact envelope exceeds budget")
        return payload

    def envelope_digest(self) -> str:
        return hashlib.sha256(
            self.canonical_bytes(include_content=False)
        ).hexdigest()


def encode_artifact_content(content: bytes) -> str:
    if not isinstance(content, bytes):
        raise TypeError("artifact content must be bytes")
    return base64.urlsafe_b64encode(content).rstrip(b"=").decode("ascii")


def artifact_owner_scope_digest(
    user_id: str,
    workspace_id: str,
) -> str:
    selected_user = _bounded_text(user_id, "user_id", 240)
    selected_workspace = _bounded_text(
        workspace_id,
        "workspace_id",
        240,
    )
    payload = json.dumps(
        {
            "user_id": selected_user,
            "workspace_id": selected_workspace,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def parse_extension_artifact(
    value: Any,
) -> ExtensionArtifactEnvelope:
    if isinstance(value, ExtensionArtifactEnvelope):
        value = value.model_dump(mode="python", by_alias=True)
    artifact = ExtensionArtifactEnvelope.model_validate(value, strict=True)
    artifact.canonical_bytes()
    return artifact


def _decode_canonical_base64url(value: str) -> bytes:
    if not isinstance(value, str) or not _BASE64URL.fullmatch(value):
        raise ValueError("artifact content must be canonical base64url")
    padding = "=" * ((4 - len(value) % 4) % 4)
    try:
        decoded = base64.b64decode(
            value + padding,
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, base64.binascii.Error) as exc:
        raise ValueError("artifact content must be canonical base64url") from exc
    if encode_artifact_content(decoded) != value:
        raise ValueError("artifact content must be canonical base64url")
    return decoded


def _parse_canonical_utc(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("timestamp must be canonical UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("timestamp must be canonical UTC") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(
        parsed
    ):
        raise ValueError("timestamp must be canonical UTC")
    return parsed.astimezone(timezone.utc)


def _canonical_utc(value: datetime) -> str:
    selected = value.astimezone(timezone.utc)
    if selected.microsecond:
        return selected.isoformat(
            timespec="microseconds",
        ).replace("+00:00", "Z")
    return selected.isoformat(timespec="seconds").replace("+00:00", "Z")


def _bounded_text(
    value: Any,
    field: str,
    limit: int,
) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    selected = value.strip()
    if (
        not selected
        or len(selected.encode("utf-8")) > limit
        or "\x00" in selected
    ):
        raise ValueError(f"{field} is invalid")
    return selected


__all__ = [
    "EXTENSION_ARTIFACT_POLICY_REVISION",
    "EXTENSION_ARTIFACT_SCHEMA_VERSION",
    "MAX_EXTENSION_ARTIFACT_BYTES",
    "ExtensionArtifactEnvelope",
    "artifact_owner_scope_digest",
    "encode_artifact_content",
    "parse_extension_artifact",
]
