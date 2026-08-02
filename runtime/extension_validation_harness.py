#!/usr/bin/env python3
from __future__ import annotations

import ast
import copy
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import resource
import socket
import sys
from typing import Any


RESULT_SCHEMA = "veyra.phase6.dynamic_validation_harness_result.v1"
BINDING_SCHEMA = "veyra.phase6.dynamic_validation_binding.v1"
TEST_BUNDLE_SCHEMA = "veyra.phase6.dynamic_validation_test_bundle.v1"
HARNESS_REVISION = "veyra.phase6.fixed_dynamic_validation_harness.v1"
POLICY_REVISION = "veyra.phase6.dynamic_validation_policy.v1"
POLICY_DIGEST = (
    "8bb0371686c69e0741a17b73174ead73552ea17125cfb7ec8f09bd0ae3ca723e"
)
ISOLATED_RUNNER_POLICY_REVISION = (
    "veyra.phase6.trusted_isolated_runner_policy.v1"
)
ISOLATED_RUNNER_HARNESS_REVISION = (
    "veyra.phase6.fixed_isolation_probe_harness.v1"
)
ENTRYPOINT = "run_extension"
FUZZ_CASES = 32
MAX_AST_NODES = 128
MAX_AST_DEPTH = 16
MAX_LITERAL_BYTES = 16 * 1024
MAX_SOURCE_BYTES = 65_536
MAX_DOCUMENT_BYTES = 96 * 1024

ISSUE_ORDER = (
    "artifact_identity_failed",
    "binding_identity_failed",
    "spec_identity_failed",
    "test_bundle_identity_failed",
    "source_policy_failed",
    "candidate_compile_failed",
    "candidate_load_failed",
    "unit_checks_failed",
    "contract_checks_failed",
    "security_runtime_checks_failed",
    "fuzz_checks_failed",
    "behavior_verification_failed",
    "validation_timeout",
    "validation_output_budget_exceeded",
    "validation_exit_nonzero",
    "validation_output_invalid",
    "validation_binding_mismatch",
    "validation_internal_error",
)

INPUT_ROOT = Path("/input")
ARTIFACT_PATH = INPUT_ROOT / "artifact.py"
SPEC_PATH = INPUT_ROOT / "spec.json"
TESTS_PATH = INPUT_ROOT / "tests.json"
BINDING_PATH = INPUT_ROOT / "binding.json"


class HarnessFailure(RuntimeError):
    def __init__(self, issue: str) -> None:
        self.issue = issue
        super().__init__(issue)


def _pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _load_json(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    if not data or len(data) > MAX_DOCUMENT_BYTES:
        raise ValueError("JSON input exceeds budget")
    value = json.loads(
        data.decode("utf-8"),
        object_pairs_hook=_pairs_no_duplicates,
        parse_constant=lambda _: (_ for _ in ()).throw(
            ValueError("non-finite JSON number")
        ),
    )
    if not isinstance(value, dict):
        raise ValueError("JSON input must be an object")
    if _canonical_bytes(value) != data:
        raise ValueError("JSON input is not canonical")
    return value


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _binding_digest(binding: dict[str, Any]) -> str:
    return _digest(binding)


def _validate_binding(binding: dict[str, Any]) -> None:
    required = {
        "schema_version",
        "validation_id",
        "isolated_run_id",
        "isolated_runner_binding_digest",
        "isolated_runner_report_digest",
        "isolated_runner_stage",
        "isolated_runner_policy_revision",
        "isolated_runner_harness_revision",
        "source_check_id",
        "source_check_binding_digest",
        "source_check_report_digest",
        "source_check_stage",
        "candidate_id",
        "candidate_revision",
        "artifact_id",
        "artifact_revision",
        "artifact_envelope_digest",
        "artifact_sha256",
        "artifact_size_bytes",
        "owner_scope_digest",
        "authenticated_principal_digest",
        "initiating_session_digest",
        "request_id",
        "request_provenance_digest",
        "extension_id",
        "extension_version",
        "spec_digest",
        "source_parser_identity",
        "source_ruleset_digest",
        "engine_identity_digest",
        "image_id",
        "isolation_conformance_digest",
        "validation_conformance_digest",
        "validation_policy_revision",
        "validation_policy_digest",
        "validation_harness_revision",
        "validation_harness_digest",
        "test_bundle_digest",
        "build_identity_digest",
    }
    if set(binding) != required:
        raise HarnessFailure("binding_identity_failed")
    if (
        binding.get("schema_version") != BINDING_SCHEMA
        or binding.get("isolated_runner_stage") != "RUNNER_JOB_PASSED"
        or binding.get("source_check_stage") != "SOURCE_CHECK_PASSED"
        or binding.get("isolated_runner_policy_revision")
        != ISOLATED_RUNNER_POLICY_REVISION
        or binding.get("isolated_runner_harness_revision")
        != ISOLATED_RUNNER_HARNESS_REVISION
        or binding.get("validation_policy_revision") != POLICY_REVISION
        or binding.get("validation_policy_digest") != POLICY_DIGEST
        or binding.get("validation_harness_revision") != HARNESS_REVISION
        or type(binding.get("artifact_size_bytes")) is not int
        or not 1 <= binding["artifact_size_bytes"] <= MAX_SOURCE_BYTES
        or binding.get("build_identity_digest")
        != _build_identity_digest(binding)
        or binding.get("request_provenance_digest")
        != _request_provenance_digest(binding)
        or binding.get("validation_harness_digest")
        != hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    ):
        raise HarnessFailure("binding_identity_failed")


def _build_identity_digest(binding: dict[str, Any]) -> str:
    fields = (
        "isolated_run_id",
        "isolated_runner_binding_digest",
        "isolated_runner_report_digest",
        "source_check_id",
        "source_check_binding_digest",
        "source_check_report_digest",
        "candidate_id",
        "candidate_revision",
        "artifact_id",
        "artifact_revision",
        "artifact_envelope_digest",
        "artifact_sha256",
        "artifact_size_bytes",
        "owner_scope_digest",
        "authenticated_principal_digest",
        "initiating_session_digest",
        "request_id",
        "request_provenance_digest",
        "extension_id",
        "extension_version",
        "spec_digest",
        "source_parser_identity",
        "source_ruleset_digest",
        "engine_identity_digest",
        "image_id",
        "isolation_conformance_digest",
        "validation_conformance_digest",
        "validation_policy_revision",
        "validation_policy_digest",
        "validation_harness_revision",
        "validation_harness_digest",
        "test_bundle_digest",
    )
    try:
        payload = {field: binding[field] for field in fields}
    except KeyError as exc:
        raise HarnessFailure("binding_identity_failed") from exc
    return _digest(
        {
            "schema_version": "veyra.phase6.extension_build_identity.v1",
            **payload,
        }
    )


def _request_provenance_digest(binding: dict[str, Any]) -> str:
    fields = (
        "request_id",
        "owner_scope_digest",
        "authenticated_principal_digest",
        "initiating_session_digest",
        "isolated_run_id",
        "test_bundle_digest",
    )
    try:
        payload = {field: binding[field] for field in fields}
    except KeyError as exc:
        raise HarnessFailure("binding_identity_failed") from exc
    return _digest(
        {
            "schema_version": (
                "veyra.phase6.dynamic_validation_request_provenance.v1"
            ),
            **payload,
        }
    )


def _validate_inputs(
    binding: dict[str, Any],
    source: bytes,
    spec: dict[str, Any],
    tests: dict[str, Any],
) -> None:
    if (
        len(source) != binding["artifact_size_bytes"]
        or hashlib.sha256(source).hexdigest() != binding["artifact_sha256"]
    ):
        raise HarnessFailure("artifact_identity_failed")
    if _digest(spec) != binding["spec_digest"]:
        raise HarnessFailure("spec_identity_failed")
    if (
        tests.get("schema_version") != TEST_BUNDLE_SCHEMA
        or tests.get("origin") != "caller_frozen_outside_generator"
        or _digest(tests) != binding["test_bundle_digest"]
    ):
        raise HarnessFailure("test_bundle_identity_failed")
    vectors = tests.get("vectors")
    if not isinstance(vectors, list) or not 1 <= len(vectors) <= 32:
        raise HarnessFailure("test_bundle_identity_failed")


def _validate_spec_and_vectors(
    spec: dict[str, Any],
    tests: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    if (
        spec.get("schema_version") != "veyra.extension_spec.v1"
        or spec.get("extension_kind") != "pure_function"
        or spec.get("risk_floor") != "R0"
        or spec.get("side_effects") != []
        or spec.get("dependencies") != []
    ):
        raise HarnessFailure("spec_identity_failed")
    permissions = spec.get("permissions")
    if not isinstance(permissions, dict) or any(
        permissions.get(key) not in ([], 0)
        for key in (
            "files",
            "network_hosts",
            "secret_ids",
            "external_account_ids",
            "max_cost_usd_cents",
        )
    ):
        raise HarnessFailure("spec_identity_failed")
    input_schema = spec.get("input_schema")
    output_schema = spec.get("output_schema")
    if not isinstance(input_schema, dict) or not isinstance(output_schema, dict):
        raise HarnessFailure("spec_identity_failed")
    vectors = tests["vectors"]
    case_ids: set[str] = set()
    for vector in vectors:
        if not isinstance(vector, dict) or set(vector) != {
            "case_id",
            "input_payload",
            "expected_output",
        }:
            raise HarnessFailure("test_bundle_identity_failed")
        case_id = vector.get("case_id")
        if not isinstance(case_id, str) or not case_id or case_id in case_ids:
            raise HarnessFailure("test_bundle_identity_failed")
        case_ids.add(case_id)
        if not _schema_accepts(input_schema, vector.get("input_payload")):
            raise HarnessFailure("contract_checks_failed")
        if not _schema_accepts(output_schema, vector.get("expected_output")):
            raise HarnessFailure("contract_checks_failed")
    return input_schema, output_schema, vectors


def _schema_accepts(schema: dict[str, Any], payload: Any) -> bool:
    if (
        not isinstance(payload, dict)
        or schema.get("type") != "object"
        or schema.get("additionalProperties") is not False
        or not isinstance(schema.get("properties"), dict)
        or not isinstance(schema.get("required"), list)
    ):
        return False
    properties = schema["properties"]
    required = schema["required"]
    if (
        any(not isinstance(key, str) for key in payload)
        or not set(payload).issubset(properties)
        or not set(required).issubset(payload)
    ):
        return False
    return all(
        _primitive_accepts(properties[key], value)
        for key, value in payload.items()
    )


def _primitive_accepts(schema: Any, value: Any) -> bool:
    if not isinstance(schema, dict):
        return False
    selected_type = schema.get("type")
    if selected_type == "string":
        if type(value) is not str:
            return False
        length = len(value)
        minimum = schema.get("minLength")
        maximum = schema.get("maxLength")
        return (
            (minimum is None or type(minimum) is int and length >= minimum)
            and type(maximum) is int
            and length <= maximum
        )
    if selected_type == "integer":
        if type(value) is not int:
            return False
        return _numeric_bounds(schema, value)
    if selected_type == "number":
        if type(value) not in {int, float} or not math.isfinite(float(value)):
            return False
        return _numeric_bounds(schema, float(value))
    if selected_type == "boolean":
        return type(value) is bool
    if selected_type == "null":
        return value is None
    return False


def _numeric_bounds(schema: dict[str, Any], value: int | float) -> bool:
    minimum = schema.get("minimum")
    maximum = schema.get("maximum")
    return (
        type(minimum) is int
        and type(maximum) is int
        and minimum <= value <= maximum
    )


def _validate_source_shape(source: bytes, spec: dict[str, Any]) -> ast.Module:
    try:
        text = source.decode("utf-8")
        tree = ast.parse(
            text,
            filename="<extension-dynamic-validation>",
            mode="exec",
            type_comments=True,
            feature_version=(3, 11),
        )
    except Exception as exc:
        raise HarnessFailure("source_policy_failed") from exc
    nodes = list(ast.walk(tree))
    if len(nodes) > MAX_AST_NODES:
        raise HarnessFailure("source_policy_failed")
    literal_bytes = 0
    stack: list[tuple[ast.AST, int]] = [(tree, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > MAX_AST_DEPTH:
            raise HarnessFailure("source_policy_failed")
        stack.extend((child, depth + 1) for child in ast.iter_child_nodes(node))
        if isinstance(node, ast.Constant):
            if type(node.value) is str:
                literal_bytes += len(node.value.encode("utf-8"))
            elif type(node.value) in {int, float, bool} or node.value is None:
                literal_bytes += len(repr(node.value).encode("ascii"))
            else:
                raise HarnessFailure("source_policy_failed")
    if literal_bytes > MAX_LITERAL_BYTES:
        raise HarnessFailure("source_policy_failed")
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
        raise HarnessFailure("source_policy_failed")
    function = tree.body[0]
    arguments = function.args
    if not (
        function.name == ENTRYPOINT
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
        and len(function.body) == 1
        and isinstance(function.body[0], ast.Return)
        and isinstance(function.body[0].value, ast.Dict)
    ):
        raise HarnessFailure("source_policy_failed")
    returned = function.body[0].value
    output_properties = spec["output_schema"]["properties"]
    seen: set[str] = set()
    if len(returned.keys) != len(output_properties):
        raise HarnessFailure("source_policy_failed")
    for key, value in zip(returned.keys, returned.values, strict=True):
        if (
            not isinstance(key, ast.Constant)
            or type(key.value) is not str
            or key.value not in output_properties
            or key.value in seen
        ):
            raise HarnessFailure("source_policy_failed")
        seen.add(key.value)
        if isinstance(value, ast.Constant):
            continue
        if not (
            isinstance(value, ast.Subscript)
            and isinstance(value.value, ast.Name)
            and value.value.id == "payload"
            and isinstance(value.slice, ast.Constant)
            and type(value.slice.value) is str
            and value.slice.value in spec["input_schema"]["required"]
        ):
            raise HarnessFailure("source_policy_failed")
    if seen != set(output_properties):
        raise HarnessFailure("source_policy_failed")
    forbidden = (
        ast.Import,
        ast.ImportFrom,
        ast.Call,
        ast.Attribute,
        ast.Global,
        ast.Nonlocal,
        ast.Await,
        ast.Yield,
        ast.YieldFrom,
        ast.Lambda,
        ast.ClassDef,
    )
    if any(isinstance(node, forbidden) for node in nodes):
        raise HarnessFailure("source_policy_failed")
    return tree


def _isolation_checks() -> bool:
    if os.getuid() == 0 or os.getgid() == 0:
        return False
    if any(Path(path).exists() for path in ("/Users", "/workspace", "/state", "/host", "/hostfs")):
        return False
    forbidden_env = (
        "VEYRA",
        "OPENAI",
        "MOONSHOT",
        "ANTHROPIC",
        "AWS_",
        "AZURE_",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "TOKEN",
        "SECRET",
        "PASSWORD",
    )
    if any(any(fragment in key.upper() for fragment in forbidden_env) for key in os.environ):
        return False
    if _write_succeeds(Path("/dynamic-validation-rootfs-sentinel"), b"x"):
        return False
    if _write_succeeds(ARTIFACT_PATH, b"\n"):
        return False
    try:
        nofile = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
        nproc = resource.getrlimit(resource.RLIMIT_NPROC)[0]
        core = resource.getrlimit(resource.RLIMIT_CORE)[0]
    except Exception:
        return False
    if nofile > 64 or nproc > 16 or core != 0:
        return False
    network_blocked = False
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.05)
        probe.connect(("192.0.2.1", 9))
    except OSError:
        network_blocked = True
    finally:
        probe.close()
    return network_blocked


def _write_succeeds(path: Path, content: bytes) -> bool:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    except OSError as exc:
        return exc.errno not in {errno.EROFS, errno.EACCES, errno.EPERM}
    try:
        os.write(descriptor, content)
    finally:
        os.close(descriptor)
    return True


def _load_candidate(tree: ast.Module) -> Any:
    try:
        code = compile(tree, "<extension-dynamic-validation>", "exec")
    except Exception as exc:
        raise HarnessFailure("candidate_compile_failed") from exc
    namespace: dict[str, Any] = {"__builtins__": {}}
    try:
        exec(code, namespace, namespace)
    except Exception as exc:
        raise HarnessFailure("candidate_load_failed") from exc
    candidate = namespace.get(ENTRYPOINT)
    if not callable(candidate) or candidate.__globals__.get("__builtins__") != {}:
        raise HarnessFailure("candidate_load_failed")
    return candidate


def _execute_case(
    candidate: Any,
    payload: dict[str, Any],
    output_schema: dict[str, Any],
) -> tuple[bool, bool, Any]:
    original = copy.deepcopy(payload)
    first_input = copy.deepcopy(payload)
    second_input = copy.deepcopy(payload)
    try:
        first = candidate(first_input)
        second = candidate(second_input)
    except BaseException:
        return False, False, None
    contract_ok = (
        first_input == original
        and second_input == original
        and first == second
        and _schema_accepts(output_schema, first)
    )
    return True, contract_ok, first


def _fuzz_payload(
    schema: dict[str, Any],
    *,
    case_index: int,
    seed_digest: str,
) -> dict[str, Any]:
    properties = schema["properties"]
    required = set(schema["required"])
    payload: dict[str, Any] = {}
    for position, key in enumerate(sorted(properties)):
        selector = hashlib.sha256(
            f"{seed_digest}:{case_index}:{position}:{key}".encode("utf-8")
        ).digest()
        if key not in required and selector[0] % 3 == 0:
            continue
        primitive = properties[key]
        selected_type = primitive["type"]
        if selected_type == "string":
            minimum = int(primitive.get("minLength") or 0)
            maximum = int(primitive["maxLength"])
            choices = (minimum, maximum, minimum + (maximum - minimum) // 2)
            length = choices[selector[1] % len(choices)]
            payload[key] = chr(97 + selector[2] % 26) * length
        elif selected_type in {"integer", "number"}:
            minimum = int(primitive["minimum"])
            maximum = int(primitive["maximum"])
            choices = (minimum, maximum, minimum + (maximum - minimum) // 2)
            selected = choices[selector[1] % len(choices)]
            payload[key] = selected if selected_type == "integer" else float(selected)
        elif selected_type == "boolean":
            payload[key] = bool(selector[1] & 1)
        else:
            payload[key] = None
    return payload


def _result(
    *,
    binding: dict[str, Any],
    status: str,
    candidate_status: str,
    unit: str,
    contract: str,
    security: str,
    fuzz: str,
    behavior: str,
    vector_count: int,
    fuzz_count: int,
    issues: set[str],
) -> dict[str, Any]:
    return {
        "artifact_sha256": str(binding.get("artifact_sha256") or "0" * 64),
        "behavior_verification_status": behavior,
        "binding_digest": _binding_digest(binding) if binding else "0" * 64,
        "build_identity_digest": str(binding.get("build_identity_digest") or "0" * 64),
        "candidate_execution_status": candidate_status,
        "contract_checks_status": contract,
        "fuzz_case_count": fuzz_count,
        "fuzz_checks_status": fuzz,
        "issue_codes": [item for item in ISSUE_ORDER if item in issues],
        "schema_version": RESULT_SCHEMA,
        "security_runtime_checks_status": security,
        "test_bundle_digest": str(binding.get("test_bundle_digest") or "0" * 64),
        "unit_checks_status": unit,
        "validation_status": status,
        "vector_count": vector_count,
    }


def validate() -> dict[str, Any]:
    binding: dict[str, Any] = {}
    try:
        binding = _load_json(BINDING_PATH)
        _validate_binding(binding)
        source = ARTIFACT_PATH.read_bytes()
        spec = _load_json(SPEC_PATH)
        tests = _load_json(TESTS_PATH)
        _validate_inputs(binding, source, spec, tests)
        input_schema, output_schema, vectors = _validate_spec_and_vectors(spec, tests)
        tree = _validate_source_shape(source, spec)
        if not _isolation_checks():
            return _result(
                binding=binding,
                status="failed",
                candidate_status="failed",
                unit="not_checked",
                contract="not_checked",
                security="failed",
                fuzz="not_checked",
                behavior="not_checked",
                vector_count=len(vectors),
                fuzz_count=0,
                issues={"security_runtime_checks_failed"},
            )
        candidate = _load_candidate(tree)
        unit_ok = True
        contract_ok = True
        behavior_ok = True
        candidate_ok = True
        for vector in vectors:
            executed, current_contract, output = _execute_case(
                candidate,
                vector["input_payload"],
                output_schema,
            )
            candidate_ok = candidate_ok and executed
            contract_ok = contract_ok and current_contract
            expected = output == vector["expected_output"]
            unit_ok = unit_ok and executed and current_contract and expected
            behavior_ok = behavior_ok and executed and current_contract and expected

        fuzz_ok = True
        fuzz_count = 0
        seed_digest = hashlib.sha256(
            (binding["build_identity_digest"] + binding["test_bundle_digest"]).encode("ascii")
        ).hexdigest()
        for case_index in range(FUZZ_CASES):
            payload = _fuzz_payload(
                input_schema,
                case_index=case_index,
                seed_digest=seed_digest,
            )
            if not _schema_accepts(input_schema, payload):
                fuzz_ok = False
                break
            executed, current_contract, _ = _execute_case(
                candidate,
                payload,
                output_schema,
            )
            fuzz_count += 1
            candidate_ok = candidate_ok and executed
            contract_ok = contract_ok and current_contract
            fuzz_ok = fuzz_ok and executed and current_contract

        issues: set[str] = set()
        if not unit_ok:
            issues.add("unit_checks_failed")
        if not contract_ok:
            issues.add("contract_checks_failed")
        if not fuzz_ok or fuzz_count != FUZZ_CASES:
            issues.add("fuzz_checks_failed")
        if not behavior_ok:
            issues.add("behavior_verification_failed")
        passed = candidate_ok and not issues
        return _result(
            binding=binding,
            status="passed" if passed else "failed",
            candidate_status="passed" if candidate_ok else "failed",
            unit="passed" if unit_ok else "failed",
            contract="passed" if contract_ok else "failed",
            security="passed",
            fuzz="passed" if fuzz_ok and fuzz_count == FUZZ_CASES else "failed",
            behavior="passed" if behavior_ok else "failed",
            vector_count=len(vectors),
            fuzz_count=fuzz_count,
            issues=issues,
        )
    except HarnessFailure as exc:
        issue = exc.issue if exc.issue in ISSUE_ORDER else "validation_internal_error"
        status = "failed"
        return _result(
            binding=binding,
            status=status,
            candidate_status="failed",
            unit="not_checked",
            contract="failed" if issue == "contract_checks_failed" else "not_checked",
            security="failed" if issue == "source_policy_failed" else "not_checked",
            fuzz="not_checked",
            behavior="not_checked",
            vector_count=0,
            fuzz_count=0,
            issues={issue},
        )
    except BaseException:
        return _result(
            binding=binding,
            status="failed",
            candidate_status="failed",
            unit="not_checked",
            contract="not_checked",
            security="not_checked",
            fuzz="not_checked",
            behavior="not_checked",
            vector_count=0,
            fuzz_count=0,
            issues={"validation_internal_error"},
        )


def main() -> int:
    result = validate()
    sys.stdout.buffer.write(_canonical_bytes(result) + b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
