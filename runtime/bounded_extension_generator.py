from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import unicodedata
from typing import Any
from urllib.parse import urlparse

from core.model_client import CoreModelClient
from interface.extension_artifact import MAX_EXTENSION_ARTIFACT_BYTES
from interface.extension_generation import (
    EXTENSION_GENERATION_PROMPT,
    ExtensionGenerationBinding,
    parse_extension_generation_binding,
)
from interface.extension_spec import ExtensionSpec, parse_extension_spec


class BoundedExtensionGeneratorError(RuntimeError):
    """Base failure for bounded model-to-source generation."""


class BoundedExtensionGeneratorUnavailable(BoundedExtensionGeneratorError):
    """The exact configured generator transport is unavailable."""


class BoundedExtensionGeneratorOutputError(BoundedExtensionGeneratorError):
    """The model response is not canonical candidate source data."""


@dataclass(frozen=True, slots=True)
class GeneratedExtensionSource:
    source_bytes: bytes
    provider_id: str
    model_id: str
    model_config_digest: str
    duration_ms: int


class BoundedExtensionGenerator:
    """Ask the configured core model for source data, never a filesystem edit.

    The model receives only the immutable pure-function specification. It has
    no Agent tools, workspace context, state snapshot, signing key, test oracle,
    canary input, or promotion authority. Its output is untrusted bytes that
    must still pass artifact quarantine, static policy, isolated validation,
    signature verification, and governed promotion.
    """

    def __init__(self, client: CoreModelClient) -> None:
        self.client = client

    def identity(self) -> dict[str, Any]:
        config = self.client.config()
        endpoint = urlparse(config.base_url)
        endpoint_origin = (
            f"{endpoint.scheme.lower()}://{endpoint.hostname.lower()}"
            if endpoint.scheme and endpoint.hostname
            else ""
        )
        payload = {
            "provider_id": str(config.provider or ""),
            "model_id": str(config.model or ""),
            "endpoint_origin": endpoint_origin,
            "timeout_millis": int(config.timeout * 1000),
            "max_tokens": int(config.max_tokens),
            "response_format": "json_object",
            "temperature": 0,
            "api_key_configured": bool(config.api_key),
        }
        return {
            "configured": bool(
                config.configured()
                and config.provider == "openai_compatible"
                and (not self.client._api_key_required(config) or config.api_key)
            ),
            "provider_id": payload["provider_id"],
            "model_id": payload["model_id"],
            "model_config_digest": hashlib.sha256(
                _canonical_bytes(payload)
            ).hexdigest(),
        }

    def generate(
        self,
        *,
        binding: ExtensionGenerationBinding | dict[str, Any],
        spec: ExtensionSpec | dict[str, Any],
    ) -> GeneratedExtensionSource:
        selected_binding = parse_extension_generation_binding(binding)
        selected_spec = parse_extension_spec(spec)
        if (
            selected_spec.digest() != selected_binding.spec_digest
            or selected_spec.extension_id != selected_binding.extension_id
            or selected_spec.version != selected_binding.extension_version
            or selected_spec.extension_kind != "pure_function"
            or selected_spec.risk_floor != "R0"
            or selected_spec.dependencies
            or selected_spec.permissions.files
            or selected_spec.permissions.network_hosts
            or selected_spec.permissions.secret_ids
            or selected_spec.permissions.external_account_ids
            or selected_spec.permissions.max_cost_usd_cents != 0
            or selected_spec.side_effects
        ):
            raise BoundedExtensionGeneratorOutputError(
                "generation specification does not match the binding"
            )

        before = self.identity()
        if (
            not before["configured"]
            or before["provider_id"] != selected_binding.provider_id
            or before["model_id"] != selected_binding.model_id
            or before["model_config_digest"]
            != selected_binding.model_config_digest
        ):
            raise BoundedExtensionGeneratorUnavailable(
                "generation model identity is unavailable or changed"
            )

        request_payload = {
            "schema_version": "veyra.phase6.extension_generation_request.v1",
            "purpose": selected_spec.purpose,
            "entrypoint": "run_extension",
            "input_schema": selected_spec.input_schema.model_dump(
                mode="json",
                by_alias=True,
            ),
            "output_schema": selected_spec.output_schema.model_dump(
                mode="json",
                by_alias=True,
            ),
            "constraints": {
                "dependencies": [],
                "permissions": "none",
                "side_effects": "none",
                "single_return_dict": True,
                "values": "primitive_constant_or_direct_required_input_projection",
            },
        }
        result = self.client.complete_json(
            purpose="phase6_extension_generation",
            system=EXTENSION_GENERATION_PROMPT,
            user=json.dumps(
                request_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ),
        )
        after = self.identity()
        if after != before:
            raise BoundedExtensionGeneratorUnavailable(
                "generation model identity changed during the request"
            )
        if result.get("status") != "model_assisted":
            raise BoundedExtensionGeneratorUnavailable(
                "generation model did not return a successful response"
            )
        model = result.get("_model")
        if (
            not isinstance(model, dict)
            or str(model.get("provider") or "") != before["provider_id"]
            or str(model.get("model") or "") != before["model_id"]
            or str(model.get("purpose") or "")
            != "phase6_extension_generation"
        ):
            raise BoundedExtensionGeneratorOutputError(
                "generation response model identity is invalid"
            )
        allowed_keys = {
            "_model",
            "duration_ms",
            "model_status",
            "source",
            "status",
        }
        if any(key not in allowed_keys for key in result):
            raise BoundedExtensionGeneratorOutputError(
                "generation response contains unsupported fields"
            )
        source = result.get("source")
        if not isinstance(source, str):
            raise BoundedExtensionGeneratorOutputError(
                "generation response does not contain source text"
            )
        source_bytes = _canonical_source(source)
        return GeneratedExtensionSource(
            source_bytes=source_bytes,
            provider_id=before["provider_id"],
            model_id=before["model_id"],
            model_config_digest=before["model_config_digest"],
            duration_ms=_nonnegative_int(result.get("duration_ms")),
        )


def _canonical_source(value: str) -> bytes:
    if value.startswith("\ufeff") or "\x00" in value or "\r" in value:
        raise BoundedExtensionGeneratorOutputError(
            "generated source is not canonical UTF-8 text"
        )
    if unicodedata.normalize("NFC", value) != value:
        raise BoundedExtensionGeneratorOutputError(
            "generated source is not NFC-normalized"
        )
    # OpenAI-compatible JSON transports agree on the decoded string value but
    # do not all preserve a final source newline.  A missing terminal LF has no
    # semantic meaning and is safe to normalize before hashing/quarantine.  We
    # deliberately normalize only this one transport-level difference; CR,
    # BOM, NUL, Unicode drift, Markdown, and invalid Python remain rejected by
    # this boundary or the mandatory source-policy gate.
    if not value.endswith("\n"):
        value += "\n"
    payload = value.encode("utf-8", errors="strict")
    if not payload or len(payload) > MAX_EXTENSION_ARTIFACT_BYTES:
        raise BoundedExtensionGeneratorOutputError(
            "generated source exceeds the artifact byte budget"
        )
    return payload


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _nonnegative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


__all__ = [
    "BoundedExtensionGenerator",
    "BoundedExtensionGeneratorError",
    "BoundedExtensionGeneratorOutputError",
    "BoundedExtensionGeneratorUnavailable",
    "GeneratedExtensionSource",
]
