from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Iterable, Mapping

try:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )
except ImportError as exc:  # pragma: no cover - fail closed at import time.
    raise RuntimeError("Ed25519 release signing requires cryptography") from exc

from interface.extension_release import ExtensionReleaseAttestation


_SERVICE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,239}$")
_KEY_ID = re.compile(r"^ed25519_[0-9a-f]{64}$")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


class ExtensionReleaseSigningError(RuntimeError):
    """Release signing or trust verification failed closed."""


class ExtensionReleaseKeyConfigurationError(ExtensionReleaseSigningError):
    """The external private key or public trust-root configuration is invalid."""


class ExtensionReleaseSignatureError(ExtensionReleaseSigningError):
    """The Ed25519 signature is absent, invalid, or bound to another key."""


def ed25519_key_id(public_key_raw: bytes) -> str:
    selected = bytes(public_key_raw)
    if len(selected) != 32:
        raise ExtensionReleaseKeyConfigurationError(
            "Ed25519 public keys must contain exactly 32 raw bytes"
        )
    return "ed25519_" + hashlib.sha256(selected).hexdigest()


class Ed25519ReleaseSigner:
    """One fixed signing identity backed by an external mode-0600 key file.

    The class never creates keys, rotates keys, writes key material, or exposes
    the configured private path in status/report data. Tests may create their
    own temporary external key files before constructing this class.
    """

    def __init__(
        self,
        *,
        private_key_path: str | Path,
        signing_service_id: str,
        forbidden_roots: Iterable[str | Path],
    ) -> None:
        selected_service = str(signing_service_id or "").strip()
        if not _SERVICE_ID.fullmatch(selected_service):
            raise ExtensionReleaseKeyConfigurationError(
                "signing service identity is invalid"
            )
        selected_path = Path(private_key_path)
        if not selected_path.is_absolute():
            raise ExtensionReleaseKeyConfigurationError(
                "release private key path must be explicit and absolute"
            )
        self._private_key_path = selected_path
        repository_root = Path(__file__).resolve().parents[1]
        self._assert_external([repository_root, *forbidden_roots])
        self._private_key = self._load_private_key()
        self.signing_service_id = selected_service
        self.public_key_raw = self._private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        self.key_id = ed25519_key_id(self.public_key_raw)
        self.identity_digest = hashlib.sha256(
            json.dumps(
                {
                    "schema_version": (
                        "veyra.phase6.extension_release_signer_identity.v1"
                    ),
                    "algorithm": "ed25519",
                    "signing_service_id": self.signing_service_id,
                    "key_id": self.key_id,
                    "public_key_sha256": hashlib.sha256(
                        self.public_key_raw
                    ).hexdigest(),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    def _assert_external(self, forbidden_roots: Iterable[str | Path]) -> None:
        candidate = self._private_key_path.resolve(strict=False)
        roots = [Path(item).resolve(strict=False) for item in forbidden_roots]
        if not roots:
            raise ExtensionReleaseKeyConfigurationError(
                "at least one repository or state root must be forbidden"
            )
        for root in roots:
            try:
                candidate.relative_to(root)
            except ValueError:
                continue
            raise ExtensionReleaseKeyConfigurationError(
                "release private key must be outside repository and state roots"
            )

    def assert_external_to(self, forbidden_roots: Iterable[str | Path]) -> None:
        """Fail closed when a registry binds this signer to new private roots."""

        self._assert_external(forbidden_roots)

    def _load_private_key(self) -> Ed25519PrivateKey:
        try:
            linked = os.lstat(self._private_key_path)
        except OSError as exc:
            raise ExtensionReleaseKeyConfigurationError(
                "release private key is unavailable"
            ) from exc
        if (
            not stat.S_ISREG(linked.st_mode)
            or stat.S_IMODE(linked.st_mode) != 0o600
            or int(linked.st_nlink) != 1
            or int(linked.st_uid) != os.getuid()
            or not 1 <= int(linked.st_size) <= 4096
        ):
            raise ExtensionReleaseKeyConfigurationError(
                "release private key must be an owned regular 0600 single-link file"
            )
        flags = os.O_RDONLY | _NOFOLLOW | _CLOEXEC
        try:
            fd = os.open(self._private_key_path, flags)
        except OSError as exc:
            raise ExtensionReleaseKeyConfigurationError(
                "release private key cannot be opened safely"
            ) from exc
        try:
            held = os.fstat(fd)
            if (
                int(held.st_dev) != int(linked.st_dev)
                or int(held.st_ino) != int(linked.st_ino)
                or int(held.st_size) != int(linked.st_size)
                or stat.S_IMODE(held.st_mode) != 0o600
                or int(held.st_nlink) != 1
            ):
                raise ExtensionReleaseKeyConfigurationError(
                    "release private key filesystem binding changed"
                )
            material = bytearray()
            while len(material) <= 4096:
                chunk = os.read(fd, 4097 - len(material))
                if not chunk:
                    break
                material.extend(chunk)
            if len(material) != int(held.st_size) or len(material) > 4096:
                raise ExtensionReleaseKeyConfigurationError(
                    "release private key size changed while reading"
                )
        finally:
            os.close(fd)
        try:
            if len(material) == 32:
                return Ed25519PrivateKey.from_private_bytes(bytes(material))
            loaded = serialization.load_pem_private_key(
                bytes(material),
                password=None,
            )
            if not isinstance(loaded, Ed25519PrivateKey):
                raise ExtensionReleaseKeyConfigurationError(
                    "release private key is not Ed25519"
                )
            return loaded
        except ExtensionReleaseKeyConfigurationError:
            raise
        except Exception as exc:
            raise ExtensionReleaseKeyConfigurationError(
                "release private key encoding is invalid"
            ) from exc
        finally:
            for index in range(len(material)):
                material[index] = 0

    def sign(self, payload: bytes) -> str:
        if not isinstance(payload, bytes) or not payload:
            raise ExtensionReleaseSigningError(
                "release signing payload must be non-empty bytes"
            )
        signature = self._private_key.sign(payload)
        return base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")

    def public_identity(self) -> dict[str, str]:
        return {
            "algorithm": "ed25519",
            "signing_service_id": self.signing_service_id,
            "signing_service_identity_digest": self.identity_digest,
            "key_id": self.key_id,
            "public_key_sha256": hashlib.sha256(
                self.public_key_raw
            ).hexdigest(),
        }


class Ed25519ReleaseTrustStore:
    """Immutable in-memory trust roots containing only raw public keys."""

    def __init__(self, roots: Mapping[str, bytes]) -> None:
        parsed: dict[str, Ed25519PublicKey] = {}
        raw: dict[str, bytes] = {}
        if not isinstance(roots, Mapping) or not roots:
            raise ExtensionReleaseKeyConfigurationError(
                "at least one Ed25519 public trust root is required"
            )
        for key_id, public_bytes in roots.items():
            selected_id = str(key_id or "")
            selected_raw = bytes(public_bytes)
            if (
                not _KEY_ID.fullmatch(selected_id)
                or selected_id != ed25519_key_id(selected_raw)
                or selected_id in parsed
            ):
                raise ExtensionReleaseKeyConfigurationError(
                    "release public trust root identity is invalid"
                )
            try:
                parsed[selected_id] = Ed25519PublicKey.from_public_bytes(
                    selected_raw
                )
            except Exception as exc:
                raise ExtensionReleaseKeyConfigurationError(
                    "release public trust root encoding is invalid"
                ) from exc
            raw[selected_id] = selected_raw
        self._roots = parsed
        self._raw_roots = raw

    @property
    def key_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._roots))

    def raw_public_key(self, key_id: str) -> bytes:
        try:
            return bytes(self._raw_roots[key_id])
        except KeyError as exc:
            raise ExtensionReleaseSignatureError(
                "release signing key is not trusted"
            ) from exc

    def verify(self, *, key_id: str, payload: bytes, signature_b64url: str) -> None:
        public = self._roots.get(str(key_id or ""))
        if public is None:
            raise ExtensionReleaseSignatureError(
                "release signing key is not trusted"
            )
        try:
            signature = base64.urlsafe_b64decode(signature_b64url + "==")
        except Exception as exc:
            raise ExtensionReleaseSignatureError(
                "release signature encoding is invalid"
            ) from exc
        if len(signature) != 64:
            raise ExtensionReleaseSignatureError(
                "release signature length is invalid"
            )
        try:
            public.verify(signature, payload)
        except InvalidSignature as exc:
            raise ExtensionReleaseSignatureError(
                "release signature verification failed"
            ) from exc

    def verify_attestation(self, attestation: ExtensionReleaseAttestation) -> None:
        self.verify(
            key_id=attestation.signing_key_id,
            payload=attestation.signing_bytes(),
            signature_b64url=attestation.signature_b64url,
        )

    def public_status(self) -> dict[str, object]:
        return {
            "algorithm": "ed25519",
            "key_ids": list(self.key_ids),
            "public_key_digests": {
                key_id: hashlib.sha256(value).hexdigest()
                for key_id, value in sorted(self._raw_roots.items())
            },
            "private_key_material_present": False,
        }


__all__ = [
    "Ed25519ReleaseSigner",
    "Ed25519ReleaseTrustStore",
    "ExtensionReleaseKeyConfigurationError",
    "ExtensionReleaseSignatureError",
    "ExtensionReleaseSigningError",
    "ed25519_key_id",
]
