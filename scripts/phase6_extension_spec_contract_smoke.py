#!/usr/bin/env python3
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from typing import Any, Callable

from pydantic import ValidationError


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from interface.extension_spec import (  # noqa: E402
    EXTENSION_POLICY_REVISION,
    JSON_SCHEMA_URI,
    REQUIRED_FUTURE_CHECKS,
    TCB_FORBIDDEN_PATHS,
    ExtensionSpec,
    parse_extension_spec,
)


def expect(
    condition: bool,
    label: str,
    detail: Any = None,
) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"PASS {label}")


def valid_spec(
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    return {
        "schema_version": "veyra.extension_spec.v1",
        "extension_kind": "pure_function",
        "extension_id": "example.bounded_projection",
        "version": 1,
        "purpose": (
            "Describe a bounded pure transformation for future isolated "
            "generation."
        ),
        "expires_at": (
            current + timedelta(days=7)
        ).isoformat(),
        "input_schema": {
            "$schema": JSON_SCHEMA_URI,
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 120,
                },
                "count": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 100,
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        "output_schema": {
            "$schema": JSON_SCHEMA_URI,
            "type": "object",
            "properties": {
                "label": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 160,
                }
            },
            "required": ["label"],
            "additionalProperties": False,
        },
        "permissions": {
            "files": [],
            "network_hosts": [],
            "secret_ids": [],
            "external_account_ids": [],
            "max_cost_usd_cents": 0,
        },
        "side_effects": [],
        "risk_floor": "R0",
        "budgets": {
            "timeout_ms": 100,
            "cpu_ms": 50,
            "memory_bytes": 1024 * 1024,
            "output_bytes": 4 * 1024,
            "max_retries": 0,
        },
        "idempotency": {
            "mode": "pure",
            "key": "canonical_input_sha256",
        },
        "verification": {
            "strategy": "future_isolated_contract_tests",
            "required_checks": list(REQUIRED_FUTURE_CHECKS),
            "independent_verifier_required": True,
        },
        "compensation": {
            "strategy": "not_applicable_no_side_effects"
        },
        "dependencies": [
            {
                "dependency_id": "example.fixed_schema",
                "version": "1.2.3",
                "sha256": "a" * 64,
            }
        ],
        "artifact": {
            "status": "not_generated",
            "artifact_kind": "none",
            "code_sha256": None,
        },
        "tcb_policy": {
            "policy_revision": EXTENSION_POLICY_REVISION,
            "forbidden_paths": list(TCB_FORBIDDEN_PATHS),
            "workspace_access_allowed": False,
            "state_access_allowed": False,
            "environment_access_allowed": False,
        },
    }


def rejected(
    mutate: Callable[[dict[str, Any]], None],
) -> bool:
    payload = valid_spec()
    mutate(payload)
    try:
        parse_extension_spec(payload)
    except (TypeError, ValueError, ValidationError):
        return True
    return False


def main() -> int:
    fixed_now = datetime(2026, 7, 30, 8, 0, tzinfo=timezone.utc)
    payload = valid_spec(now=fixed_now)
    first = parse_extension_spec(payload)
    reordered = {
        key: value
        for key, value in reversed(
            list(payload.items())
        )
    }
    second = ExtensionSpec.model_validate(
        reordered,
        strict=True,
    )
    expect(
        first.digest() == second.digest()
        and first.canonical_dict() == second.canonical_dict(),
        "valid ExtensionSpec has stable canonical identity",
    )
    expect(
        first.artifact.status == "not_generated"
        and first.permissions.files == []
        and first.side_effects == []
        and first.risk_floor == "R0",
        "contract carries explicit zero-authority declarations",
    )

    checks: list[tuple[str, Callable[[dict[str, Any]], None]]] = [
        (
            "unknown top-level fields are rejected",
            lambda value: value.__setitem__("entrypoint", "evil.py"),
        ),
        (
            "bool cannot coerce into an extension version",
            lambda value: value.__setitem__("version", True),
        ),
        (
            "unsupported extension kinds are rejected",
            lambda value: value.__setitem__(
                "extension_kind",
                "arbitrary_code",
            ),
        ),
        (
            "file permission requests are rejected",
            lambda value: value["permissions"]["files"].append(
                "workspace/**"
            ),
        ),
        (
            "network permission requests are rejected",
            lambda value: value["permissions"][
                "network_hosts"
            ].append("example.com"),
        ),
        (
            "secret requests are rejected",
            lambda value: value["permissions"]["secret_ids"].append(
                "API_KEY"
            ),
        ),
        (
            "external account requests are rejected",
            lambda value: value["permissions"][
                "external_account_ids"
            ].append("github"),
        ),
        (
            "cost requests are rejected",
            lambda value: value["permissions"].__setitem__(
                "max_cost_usd_cents",
                1,
            ),
        ),
        (
            "side effects are rejected",
            lambda value: value["side_effects"].append(
                {
                    "effect_type": "file_write",
                    "target": "workspace",
                }
            ),
        ),
        (
            "retry authority is rejected",
            lambda value: value["budgets"].__setitem__(
                "max_retries",
                1,
            ),
        ),
        (
            "risk-floor changes are rejected",
            lambda value: value.__setitem__("risk_floor", "R1"),
        ),
        (
            "remote JSON Schema references are rejected",
            lambda value: value["input_schema"].__setitem__(
                "$ref",
                "https://example.com/schema.json",
            ),
        ),
        (
            "recursive JSON Schema shapes are rejected",
            lambda value: value["input_schema"]["properties"].__setitem__(
                "nested",
                {"type": "object"},
            ),
        ),
        (
            "case-colliding fields are rejected",
            lambda value: value["input_schema"]["properties"].update(
                {
                    "Name": {
                        "type": "string",
                        "maxLength": 120,
                    },
                }
            ),
        ),
        (
            "required fields must exist in properties",
            lambda value: value["input_schema"]["required"].append(
                "missing"
            ),
        ),
        (
            "duplicate dependency identities are rejected",
            lambda value: value["dependencies"].append(
                {
                    "dependency_id": "EXAMPLE.FIXED_SCHEMA",
                    "version": "1.2.3",
                    "sha256": "b" * 64,
                }
            ),
        ),
        (
            "mutable dependency versions are rejected",
            lambda value: value["dependencies"][0].__setitem__(
                "version",
                "latest",
            ),
        ),
        (
            "unbounded string schemas are rejected",
            lambda value: value["input_schema"]["properties"].__setitem__(
                "unbounded",
                {"type": "string"},
            ),
        ),
        (
            "unbounded numeric schemas are rejected",
            lambda value: value["input_schema"]["properties"].__setitem__(
                "unbounded_count",
                {"type": "integer"},
            ),
        ),
        (
            "the fixed TCB deny policy cannot be weakened",
            lambda value: value["tcb_policy"][
                "forbidden_paths"
            ].pop(),
        ),
        (
            "workspace access cannot be enabled",
            lambda value: value["tcb_policy"].__setitem__(
                "workspace_access_allowed",
                True,
            ),
        ),
        (
            "generated artifact claims are rejected",
            lambda value: value["artifact"].update(
                {
                    "status": "generated",
                    "artifact_kind": "python",
                    "code_sha256": "c" * 64,
                }
            ),
        ),
        (
            "non-NFC purpose text is rejected",
            lambda value: value.__setitem__(
                "purpose",
                "Cafe\u0301 future transform",
            ),
        ),
    ]
    for label, mutate in checks:
        expect(rejected(mutate), label)

    oversized = valid_spec()
    oversized["purpose"] = "x" * 513
    expect(
        rejected(lambda value: value.update(oversized)),
        "bounded text prevents oversized manifests",
    )

    mutated_instance = parse_extension_spec(
        valid_spec(now=fixed_now)
    )
    mutated_instance.permissions.files.append("/tmp/forbidden")
    try:
        parse_extension_spec(mutated_instance)
    except (TypeError, ValueError, ValidationError):
        pass
    else:
        raise AssertionError(
            "mutated nested data in a frozen model was trusted"
        )
    print("PASS shallow-frozen model instances are revalidated")

    print("phase6 extension spec contract smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
