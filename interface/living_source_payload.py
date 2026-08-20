"""Strict JSON and receipt-payload helpers for Living Source contracts."""

from __future__ import annotations

from datetime import datetime
import json
from typing import Any, Mapping

from common.living_source_primitives import LivingSourceContractError, canonical_utc


_RECEIPT_TOP_LEVEL_KEYS = frozenset({"facts", "provider", "source", "summary"})
_FORBIDDEN_PAYLOAD_KEYS = frozenset(
    "path paths file filename command cmd shell tool tools tool_args args arguments env headers "
    "credential credentials token recipient recipients external_target raw debug traceback".split()
)


def validate_receipt_payload(source: str, status: str, payload: Mapping[str, Any]) -> None:
    unknown = set(str(key) for key in payload) - _RECEIPT_TOP_LEVEL_KEYS
    if unknown:
        raise LivingSourceContractError(f"receipt payload contains unsupported keys: {sorted(unknown)!r}")
    if status == "ok":
        facts = payload.get("facts")
        if not isinstance(facts, Mapping) or not facts:
            raise LivingSourceContractError("successful receipt must contain typed facts")
    if status == "revoked" and payload:
        raise LivingSourceContractError("revoked receipt must not retain provider facts")

    def visit(value: Any, path: tuple[str, ...] = ()) -> None:
        if isinstance(value, Mapping):
            for raw_key, item in value.items():
                key = str(raw_key)
                if key.lower() in _FORBIDDEN_PAYLOAD_KEYS:
                    raise LivingSourceContractError(f"receipt payload key {key!r} is forbidden")
                if key == "url" and (source != "public_web" or len(path) < 2 or path[-2] != "results"):
                    raise LivingSourceContractError("receipt URL is only allowed in public_web results")
                visit(item, path + (key,))
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                visit(item, path + (str(index),))
        elif isinstance(value, (str, bool, int)) or value is None:
            return
        elif isinstance(value, float):
            if value != value or value in (float("inf"), float("-inf")):
                raise LivingSourceContractError("receipt payload must contain finite JSON values")
        else:
            raise LivingSourceContractError("receipt payload contains a non-JSON value")

    visit(payload)


def _strict_json_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 8:
        raise LivingSourceContractError("provider payload nesting is too deep")
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            if not key or len(key) > 120:
                raise LivingSourceContractError("provider payload key is invalid")
            result[key] = _strict_json_value(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        if len(value) > 500:
            raise LivingSourceContractError("provider payload list is too large")
        return [_strict_json_value(item, depth=depth + 1) for item in value]
    if isinstance(value, bool) or value is None or isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise LivingSourceContractError("provider payload must contain finite JSON values")
        return value
    if isinstance(value, datetime):
        return canonical_utc(value)
    if isinstance(value, str):
        if len(value) > 4000:
            raise LivingSourceContractError("provider payload string is too long")
        return value
    raise LivingSourceContractError("provider payload contains a non-JSON value")


def normalize_provider_payload(value: Any, *, max_bytes: int = 32768) -> dict[str, Any]:
    """Keep provider output JSON-shaped and bounded before projection."""

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise LivingSourceContractError("provider payload must be an object")
    selected = _strict_json_value(dict(value))
    encoded = json.dumps(selected, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > max_bytes:
        raise LivingSourceContractError("provider payload exceeds receipt budget")
    return dict(selected)


__all__ = ["normalize_provider_payload", "validate_receipt_payload"]
