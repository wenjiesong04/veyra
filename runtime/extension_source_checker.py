from __future__ import annotations

import ast
import hashlib
import math
import unicodedata
from typing import Any

from interface.extension_source_check import (
    EXTENSION_SOURCE_CHECK_SCHEMA_VERSION,
    EXTENSION_SOURCE_CHECKER_REVISION,
    EXTENSION_SOURCE_ENTRYPOINT,
    MAX_EXTENSION_SOURCE_AST_DEPTH,
    MAX_EXTENSION_SOURCE_AST_NODES,
    MAX_EXTENSION_SOURCE_LITERAL_BYTES,
    SOURCE_CHECK_ISSUE_CODES,
    ExtensionSourceCheckAuthority,
    ExtensionSourceCheckBinding,
    ExtensionSourceCheckReport,
    parse_extension_source_check_binding,
)
from interface.extension_spec import (
    ExtensionPrimitiveSchema,
    ExtensionSpec,
    parse_extension_spec,
)


MAX_SOURCE_AST_NODES = MAX_EXTENSION_SOURCE_AST_NODES
MAX_SOURCE_AST_DEPTH = MAX_EXTENSION_SOURCE_AST_DEPTH
MAX_SOURCE_LITERAL_BYTES = MAX_EXTENSION_SOURCE_LITERAL_BYTES
_FIXED_AST_FEATURE_VERSION = (3, 11)


def check_extension_source(
    *,
    source: bytes,
    spec: ExtensionSpec | dict[str, Any],
    binding: ExtensionSourceCheckBinding | dict[str, Any],
    checked_at: str,
) -> ExtensionSourceCheckReport:
    """Apply the fixed, non-executing Phase 6.2c source policy.

    This function only decodes UTF-8, builds a Python AST, and inspects model
    objects.  It never compiles, imports, evaluates, executes, or dispatches
    the candidate.
    """

    selected = parse_extension_source_check_binding(binding)
    issues: set[str] = set()
    parsed_spec: ExtensionSpec | None = None
    try:
        parsed_spec = parse_extension_spec(spec)
    except Exception:
        issues.add("spec_invalid")
    if parsed_spec is not None:
        if not _spec_matches_binding(parsed_spec, selected):
            issues.add("spec_binding_invalid")
        if parsed_spec.dependencies:
            issues.add("dependencies_not_empty")

    syntax_status = "not_checked"
    tree: ast.Module | None = None
    text: str | None = None
    if not isinstance(source, bytes) or not source:
        issues.add("source_size_invalid")
    elif len(source) != selected.source_size_bytes:
        issues.add("source_identity_mismatch")
    elif hashlib.sha256(source).hexdigest() != selected.artifact_sha256:
        issues.add("source_identity_mismatch")
    elif (
        source.startswith(b"\xef\xbb\xbf")
        or b"\x00" in source
        or b"\r" in source
    ):
        issues.add("source_canonicalization_invalid")
    else:
        try:
            text = source.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            issues.add("source_encoding_invalid")
        if (
            text is not None
            and unicodedata.normalize("NFC", text) != text
        ):
            issues.add("source_canonicalization_invalid")

    if text is not None and not {
        "source_identity_mismatch",
        "source_canonicalization_invalid",
    }.intersection(issues):
        try:
            tree = ast.parse(
                text,
                filename="<extension-source>",
                mode="exec",
                type_comments=True,
                feature_version=_FIXED_AST_FEATURE_VERSION,
            )
            syntax_status = "passed"
        except (
            SyntaxError,
            ValueError,
            OverflowError,
            MemoryError,
            RecursionError,
        ):
            syntax_status = "failed"
            issues.add("source_syntax_invalid")

    if tree is not None:
        try:
            within_budgets = _within_ast_budgets(tree)
        except (ValueError, OverflowError, MemoryError, RecursionError):
            within_budgets = False
        if not within_budgets:
            issues.add("source_ast_budget_exceeded")
        elif parsed_spec is not None:
            issues.update(_check_fixed_shape(tree, parsed_spec))

    ordered_issues = [
        code
        for code in SOURCE_CHECK_ISSUE_CODES
        if code in issues
    ]
    passed = not ordered_issues
    return ExtensionSourceCheckReport(
        schema_version=EXTENSION_SOURCE_CHECK_SCHEMA_VERSION,
        checker_revision=EXTENSION_SOURCE_CHECKER_REVISION,
        candidate_id=selected.candidate_id,
        candidate_revision=selected.candidate_revision,
        artifact_id=selected.artifact_id,
        artifact_revision=selected.artifact_revision,
        owner_scope_digest=selected.owner_scope_digest,
        extension_id=selected.extension_id,
        extension_version=selected.extension_version,
        spec_digest=selected.spec_digest,
        artifact_sha256=selected.artifact_sha256,
        source_size_bytes=selected.source_size_bytes,
        extension_policy_revision=(
            selected.extension_policy_revision
        ),
        artifact_policy_revision=selected.artifact_policy_revision,
        source_check_policy_revision=(
            selected.source_check_policy_revision
        ),
        parser_identity=selected.parser_identity,
        ruleset_digest=selected.ruleset_digest,
        check_status="passed" if passed else "failed",
        source_syntax_status=syntax_status,
        static_checks_status="passed" if passed else "failed",
        static_security_policy_status=(
            "passed" if passed else "failed"
        ),
        issue_codes=ordered_issues,
        checked_at=checked_at,
        authority=ExtensionSourceCheckAuthority(),
    )


def _spec_matches_binding(
    spec: ExtensionSpec,
    binding: ExtensionSourceCheckBinding,
) -> bool:
    return (
        spec.extension_id == binding.extension_id
        and spec.version == binding.extension_version
        and spec.digest() == binding.spec_digest
        and spec.tcb_policy.policy_revision
        == binding.extension_policy_revision
        and spec.extension_kind == "pure_function"
        and spec.risk_floor == "R0"
        and not spec.permissions.files
        and not spec.permissions.network_hosts
        and not spec.permissions.secret_ids
        and not spec.permissions.external_account_ids
        and spec.permissions.max_cost_usd_cents == 0
        and not spec.side_effects
    )


def _within_ast_budgets(tree: ast.AST) -> bool:
    nodes = list(ast.walk(tree))
    if len(nodes) > MAX_SOURCE_AST_NODES:
        return False
    literal_bytes = 0
    for node in nodes:
        if isinstance(node, ast.Constant):
            value = node.value
            if isinstance(value, str):
                literal_bytes += len(value.encode("utf-8"))
            elif isinstance(value, (int, float, bool)) or value is None:
                literal_bytes += len(repr(value).encode("ascii"))
            else:
                return False
    if literal_bytes > MAX_SOURCE_LITERAL_BYTES:
        return False
    stack: list[tuple[ast.AST, int]] = [(tree, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > MAX_SOURCE_AST_DEPTH:
            return False
        stack.extend(
            (child, depth + 1) for child in ast.iter_child_nodes(node)
        )
    return True


def _check_fixed_shape(
    tree: ast.Module,
    spec: ExtensionSpec,
) -> set[str]:
    issues: set[str] = set()
    if (
        len(tree.body) != 1
        or not isinstance(tree.body[0], ast.FunctionDef)
    ):
        return {"top_level_shape_invalid"}
    function = tree.body[0]
    if not _valid_signature(function):
        issues.add("entrypoint_signature_invalid")
    if len(function.body) != 1 or not isinstance(
        function.body[0],
        ast.Return,
    ):
        issues.add("function_body_invalid")
        return issues
    returned = function.body[0].value
    if not isinstance(returned, ast.Dict):
        issues.add("return_mapping_invalid")
        return issues
    issues.update(_check_return_mapping(returned, spec))
    return issues


def _valid_signature(function: ast.FunctionDef) -> bool:
    arguments = function.args
    return (
        function.name == EXTENSION_SOURCE_ENTRYPOINT
        and not function.decorator_list
        and function.returns is None
        and function.type_comment is None
        and not arguments.posonlyargs
        and len(arguments.args) == 1
        and arguments.args[0].arg == "payload"
        and arguments.args[0].annotation is None
        and arguments.vararg is None
        and arguments.kwarg is None
        and not arguments.kwonlyargs
        and not arguments.kw_defaults
        and not arguments.defaults
    )


def _check_return_mapping(
    returned: ast.Dict,
    spec: ExtensionSpec,
) -> set[str]:
    issues: set[str] = set()
    output_properties = spec.output_schema.properties
    input_properties = spec.input_schema.properties
    required_inputs = set(spec.input_schema.required)
    required_outputs = set(spec.output_schema.required)
    seen: set[str] = set()

    for key_node, value_node in zip(
        returned.keys,
        returned.values,
        strict=True,
    ):
        if (
            not isinstance(key_node, ast.Constant)
            or not isinstance(key_node.value, str)
            or key_node.value in seen
        ):
            issues.add("return_mapping_invalid")
            continue
        output_key = key_node.value
        seen.add(output_key)
        output_schema = output_properties.get(output_key)
        if output_schema is None:
            issues.add("output_field_invalid")
            continue

        if isinstance(value_node, ast.Constant):
            value = value_node.value
            if not _constant_type_compatible(value, output_schema):
                issues.add("schema_type_incompatible")
            elif not _constant_bounds_compatible(value, output_schema):
                issues.add("schema_bounds_incompatible")
            continue

        input_key = _direct_payload_projection(value_node)
        if (
            input_key is None
            or input_key not in required_inputs
            or input_key not in input_properties
        ):
            issues.add("input_projection_invalid")
            continue
        input_schema = input_properties[input_key]
        if not _schema_type_compatible(input_schema, output_schema):
            issues.add("schema_type_incompatible")
        elif not _schema_bounds_compatible(input_schema, output_schema):
            issues.add("schema_bounds_incompatible")

    if not required_outputs.issubset(seen):
        issues.add("output_field_invalid")
    return issues


def _direct_payload_projection(node: ast.AST) -> str | None:
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.ctx, ast.Load)
        and isinstance(node.value, ast.Name)
        and node.value.id == "payload"
        and isinstance(node.value.ctx, ast.Load)
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, str)
    ):
        return node.slice.value
    return None


def _constant_type_compatible(
    value: Any,
    schema: ExtensionPrimitiveSchema,
) -> bool:
    if value is None:
        return schema.type == "null"
    if type(value) is bool:
        return schema.type == "boolean"
    if type(value) is int:
        return schema.type in {"integer", "number"}
    if type(value) is float:
        return schema.type == "number" and math.isfinite(value)
    if type(value) is str:
        return schema.type == "string"
    return False


def _constant_bounds_compatible(
    value: Any,
    schema: ExtensionPrimitiveSchema,
) -> bool:
    if type(value) is str:
        minimum = schema.minLength or 0
        return minimum <= len(value) <= int(schema.maxLength or 0)
    if type(value) in {int, float}:
        return (
            schema.minimum is not None
            and schema.maximum is not None
            and schema.minimum <= value <= schema.maximum
        )
    return True


def _schema_type_compatible(
    source: ExtensionPrimitiveSchema,
    target: ExtensionPrimitiveSchema,
) -> bool:
    return source.type == target.type or (
        source.type == "integer" and target.type == "number"
    )


def _schema_bounds_compatible(
    source: ExtensionPrimitiveSchema,
    target: ExtensionPrimitiveSchema,
) -> bool:
    if source.type == "string":
        return (
            (source.minLength or 0) >= (target.minLength or 0)
            and int(source.maxLength or 0)
            <= int(target.maxLength or 0)
        )
    if source.type in {"integer", "number"}:
        return (
            source.minimum is not None
            and source.maximum is not None
            and target.minimum is not None
            and target.maximum is not None
            and source.minimum >= target.minimum
            and source.maximum <= target.maximum
        )
    return True


__all__ = [
    "MAX_SOURCE_AST_DEPTH",
    "MAX_SOURCE_AST_NODES",
    "MAX_SOURCE_LITERAL_BYTES",
    "check_extension_source",
]
