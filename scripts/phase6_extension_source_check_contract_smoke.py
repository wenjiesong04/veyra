#!/usr/bin/env python3
from __future__ import annotations

import ast
import copy
import hashlib
from pathlib import Path
import sys
from typing import Any

from pydantic import ValidationError


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from interface.extension_artifact import (  # noqa: E402
    EXTENSION_ARTIFACT_POLICY_REVISION,
)
from interface.extension_source_check import (  # noqa: E402
    EXTENSION_SOURCE_CHECK_BINDING_SCHEMA_VERSION,
    EXTENSION_SOURCE_CHECK_POLICY_REVISION,
    EXTENSION_SOURCE_CHECK_SCHEMA_VERSION,
    EXTENSION_SOURCE_CHECKER_REVISION,
    EXTENSION_SOURCE_PARSER_IDENTITY,
    EXTENSION_SOURCE_RULESET_DIGEST,
    ExtensionSourceCheckBinding,
    parse_extension_source_check_report,
)
from interface.extension_spec import EXTENSION_POLICY_REVISION  # noqa: E402
from runtime.extension_source_checker import (  # noqa: E402
    MAX_SOURCE_AST_NODES,
    check_extension_source,
)
from scripts.phase6_extension_spec_contract_smoke import (  # noqa: E402
    valid_spec,
)


FIXED_TIME = "2026-07-30T08:00:00Z"
VALID_SOURCE = (
    b"def run_extension(payload):\n"
    b"    return {\"label\": payload[\"name\"]}\n"
)


def expect(
    condition: bool,
    label: str,
    detail: Any = None,
) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"PASS {label}")


def source_spec() -> dict[str, Any]:
    payload = valid_spec()
    payload["dependencies"] = []
    return payload


def binding_for(
    source: bytes,
    spec_payload: dict[str, Any] | None = None,
) -> ExtensionSourceCheckBinding:
    from interface.extension_spec import parse_extension_spec

    spec = parse_extension_spec(spec_payload or source_spec())
    return ExtensionSourceCheckBinding(
        schema_version=EXTENSION_SOURCE_CHECK_BINDING_SCHEMA_VERSION,
        candidate_id="extspec_0123456789abcdef01234567",
        candidate_revision=2,
        artifact_id="extart_0123456789abcdef01234567",
        artifact_revision=1,
        owner_scope_digest="b" * 64,
        extension_id=spec.extension_id,
        extension_version=spec.version,
        spec_digest=spec.digest(),
        artifact_sha256=hashlib.sha256(source).hexdigest(),
        source_size_bytes=len(source),
        extension_policy_revision=EXTENSION_POLICY_REVISION,
        artifact_policy_revision=EXTENSION_ARTIFACT_POLICY_REVISION,
        source_check_policy_revision=(
            EXTENSION_SOURCE_CHECK_POLICY_REVISION
        ),
        parser_identity=EXTENSION_SOURCE_PARSER_IDENTITY,
        ruleset_digest=EXTENSION_SOURCE_RULESET_DIGEST,
    )


def check(
    source: bytes,
    *,
    spec_payload: dict[str, Any] | None = None,
):
    selected_spec = spec_payload or source_spec()
    return check_extension_source(
        source=source,
        spec=selected_spec,
        binding=binding_for(source, selected_spec),
        checked_at=FIXED_TIME,
    )


def issue(
    source: bytes,
    expected: str,
    *,
    spec_payload: dict[str, Any] | None = None,
) -> bool:
    report = check(source, spec_payload=spec_payload)
    return (
        report.check_status == "failed"
        and expected in report.issue_codes
        and report.execution_status == "not_started"
    )


def main() -> int:
    report = check(VALID_SOURCE)
    expect(
        report.check_status == "passed"
        and report.source_syntax_status == "passed"
        and report.static_checks_status == "passed"
        and not report.issue_codes,
        "fixed pure projection passes the non-executing AST gate",
    )
    expect(
        report.isolated_generation_status == "not_started"
        and report.unit_checks_status == "not_started"
        and report.contract_checks_status == "not_started"
        and report.security_runtime_checks_status == "not_started"
        and report.fuzz_checks_status == "not_started"
        and report.test_execution_status == "not_started"
        and report.behavior_verification_status == "not_started",
        "dynamic generation and test statuses remain not_started",
    )
    expect(
        not any(report.authority.model_dump().values())
        and report.capability_registry_visible is False
        and report.promotion_authorized is False,
        "source check grants no authority, registry visibility, or promotion",
    )
    expect(
        report.parser_identity == EXTENSION_SOURCE_PARSER_IDENTITY
        and report.ruleset_digest == EXTENSION_SOURCE_RULESET_DIGEST,
        "report binds the fixed parser and ruleset identities",
    )

    canonical = report.canonical_dict()
    reparsed = parse_extension_source_check_report(
        dict(reversed(list(canonical.items())))
    )
    expect(
        report.canonical_bytes() == reparsed.canonical_bytes()
        and report.report_digest() == reparsed.report_digest(),
        "report canonical identity is stable across key order",
    )
    mutated = copy.deepcopy(report)
    mutated.issue_codes.append("source_syntax_invalid")
    try:
        parse_extension_source_check_report(mutated)
    except (TypeError, ValueError, ValidationError):
        pass
    else:
        raise AssertionError("mutated frozen report was accepted")
    expect(True, "reparse rejects shallow mutation of a frozen report")

    dependencies = source_spec()
    dependencies["dependencies"] = [
        {
            "dependency_id": "example.fixed",
            "version": "1.2.3",
            "sha256": "a" * 64,
        }
    ]
    expect(
        issue(
            VALID_SOURCE,
            "dependencies_not_empty",
            spec_payload=dependencies,
        ),
        "source checking requires an empty dependency set",
    )

    shape_cases: list[tuple[str, bytes, str]] = [
        (
            "syntax errors are source-free structured failures",
            b"def run_extension(:\n",
            "source_syntax_invalid",
        ),
        (
            "top-level executable statements are rejected",
            b"sentinel = open('/tmp/forbidden', 'w')\n" + VALID_SOURCE,
            "top_level_shape_invalid",
        ),
        (
            "imports are rejected",
            b"import os\n" + VALID_SOURCE,
            "top_level_shape_invalid",
        ),
        (
            "async entrypoints are rejected",
            b"async def run_extension(payload):\n"
            b"    return {\"label\": payload[\"name\"]}\n",
            "top_level_shape_invalid",
        ),
        (
            "decorators are rejected",
            b"@decorator\n" + VALID_SOURCE,
            "entrypoint_signature_invalid",
        ),
        (
            "defaults are rejected",
            b"def run_extension(payload={}):\n"
            b"    return {\"label\": payload[\"name\"]}\n",
            "entrypoint_signature_invalid",
        ),
        (
            "annotations are rejected",
            b"def run_extension(payload: dict) -> dict:\n"
            b"    return {\"label\": payload[\"name\"]}\n",
            "entrypoint_signature_invalid",
        ),
        (
            "extra function statements are rejected",
            b"def run_extension(payload):\n"
            b"    value = payload[\"name\"]\n"
            b"    return {\"label\": value}\n",
            "function_body_invalid",
        ),
        (
            "calls are rejected",
            b"def run_extension(payload):\n"
            b"    return {\"label\": str(payload[\"name\"])}\n",
            "input_projection_invalid",
        ),
        (
            "attributes are rejected",
            b"def run_extension(payload):\n"
            b"    return {\"label\": payload.get(\"name\")}\n",
            "input_projection_invalid",
        ),
        (
            "dunder access is rejected",
            b"def run_extension(payload):\n"
            b"    return {\"label\": payload.__class__}\n",
            "input_projection_invalid",
        ),
        (
            "non-dict returns are rejected",
            b"def run_extension(payload):\n"
            b"    return payload[\"name\"]\n",
            "return_mapping_invalid",
        ),
        (
            "unknown output fields are rejected",
            b"def run_extension(payload):\n"
            b"    return {\"unknown\": payload[\"name\"]}\n",
            "output_field_invalid",
        ),
        (
            "missing required output fields are rejected",
            b"def run_extension(payload):\n    return {}\n",
            "output_field_invalid",
        ),
    ]
    for label, source, expected in shape_cases:
        expect(issue(source, expected), label)

    optional_projection = source_spec()
    optional_projection["output_schema"]["properties"]["label"] = {
        "type": "integer",
        "minimum": 0,
        "maximum": 100,
    }
    expect(
        issue(
            b"def run_extension(payload):\n"
            b"    return {\"label\": payload[\"count\"]}\n",
            "input_projection_invalid",
            spec_payload=optional_projection,
        ),
        "only required input fields may be projected",
    )

    narrowed_output = source_spec()
    narrowed_output["output_schema"]["properties"]["label"][
        "maxLength"
    ] = 10
    expect(
        issue(
            VALID_SOURCE,
            "schema_bounds_incompatible",
            spec_payload=narrowed_output,
        ),
        "input projections must fit output schema bounds",
    )
    expect(
        issue(
            b"def run_extension(payload):\n"
            b"    return {\"label\": 7}\n",
            "schema_type_incompatible",
        ),
        "constant values must match output schema types",
    )
    expect(
        issue(
            b"def run_extension(payload):\n"
            b"    return {\"label\": \"\"}\n",
            "schema_bounds_incompatible",
        ),
        "constant values must fit output schema bounds",
    )

    many_items = ", ".join(
        f"\"k{index}\": \"v\"" for index in range(MAX_SOURCE_AST_NODES)
    )
    budget_source = (
        "def run_extension(payload):\n"
        f"    return {{{many_items}}}\n"
    ).encode("utf-8")
    expect(
        issue(budget_source, "source_ast_budget_exceeded"),
        "AST node budget fails closed before structural interpretation",
    )
    deep_source = (
        "def run_extension(payload):\n"
        "    return {\"label\": "
        + ("+" * 20)
        + "1}\n"
    ).encode("utf-8")
    expect(
        issue(deep_source, "source_ast_budget_exceeded"),
        "AST depth budget fails closed",
    )
    literal_source = (
        "def run_extension(payload):\n"
        "    return {\"label\": \""
        + ("a" * (16 * 1024 + 1))
        + "\"}\n"
    ).encode("utf-8")
    expect(
        issue(literal_source, "source_ast_budget_exceeded"),
        "literal byte budget fails closed",
    )

    invalid_syntax = check(b"def run_extension(:\n")
    serialized = str(invalid_syntax.canonical_dict())
    expect(
        "def run_extension" not in serialized
        and "SyntaxError" not in serialized
        and "<extension-source>" not in serialized,
        "reports never expose source text or parser exception detail",
    )

    mismatch_spec = source_spec()
    mismatched_binding = binding_for(VALID_SOURCE, mismatch_spec)
    mismatch = check_extension_source(
        source=VALID_SOURCE + b"\n",
        spec=mismatch_spec,
        binding=mismatched_binding,
        checked_at=FIXED_TIME,
    )
    expect(
        mismatch.issue_codes == ["source_identity_mismatch"]
        and mismatch.source_syntax_status == "not_checked",
        "source identity drift prevents AST interpretation",
    )

    checker_tree = ast.parse(
        (ROOT / "runtime/extension_source_checker.py").read_text(
            encoding="utf-8"
        )
    )
    forbidden_calls = {
        node.func.id
        for node in ast.walk(checker_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"compile", "eval", "exec", "__import__"}
    }
    imported_modules = {
        alias.name
        for node in checker_tree.body
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    expect(
        not forbidden_calls and "subprocess" not in imported_modules,
        "checker contains no compile/eval/exec/import hook or subprocess",
    )

    expect(
        report.schema_version == EXTENSION_SOURCE_CHECK_SCHEMA_VERSION
        and report.checker_revision
        == EXTENSION_SOURCE_CHECKER_REVISION,
        "report exposes exact schema and checker revisions",
    )
    print("phase6 extension source check contract smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
