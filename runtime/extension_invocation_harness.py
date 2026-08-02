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
from typing import Any


BINDING_SCHEMA = "veyra.phase6.extension_invocation_binding.v1"
RESULT_SCHEMA = "veyra.phase6.extension_invocation_harness_result.v1"
HARNESS_REVISION = "veyra.phase6.fixed_signed_extension_invocation_harness.v1"
POLICY_REVISION = "veyra.phase6.signed_extension_deployment_policy.v1"
ENTRYPOINT = "run_extension"
MAX_SOURCE_BYTES = 65_536
MAX_DOCUMENT_BYTES = 64 * 1024
MAX_AST_NODES = 128
MAX_AST_DEPTH = 16
MAX_LITERAL_BYTES = 16 * 1024

INPUT_ROOT = Path("/input")
ARTIFACT_PATH = INPUT_ROOT / "artifact.py"
SPEC_PATH = INPUT_ROOT / "spec.json"
BINDING_PATH = INPUT_ROOT / "binding.json"
PAYLOAD_PATH = INPUT_ROOT / "payload.json"


class HarnessFailure(RuntimeError):
    def __init__(self, issue_code: str) -> None:
        self.issue_code = issue_code
        super().__init__(issue_code)


def _pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


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


def _load_json(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    if not raw or len(raw) > MAX_DOCUMENT_BYTES:
        raise ValueError("JSON input exceeds budget")
    value = json.loads(
        raw.decode("utf-8", errors="strict"),
        object_pairs_hook=_pairs_no_duplicates,
        parse_constant=lambda _: (_ for _ in ()).throw(
            ValueError("non-finite JSON number")
        ),
    )
    if not isinstance(value, dict) or _canonical_bytes(value) != raw:
        raise ValueError("JSON input is not one canonical object")
    return value


def _validate_binding(binding: dict[str, Any]) -> None:
    required = {
        "schema_version",
        "invocation_id",
        "deployment_id",
        "deployment_revision",
        "deployment_binding_digest",
        "release_id",
        "release_revision",
        "attestation_digest",
        "owner_scope_digest",
        "authenticated_principal_digest",
        "initiating_session_digest",
        "request_provenance_digest",
        "extension_id",
        "extension_version",
        "artifact_sha256",
        "spec_digest",
        "input_digest",
        "mode",
        "mode_epoch",
        "scope_digest",
        "runner_engine_identity_digest",
        "runner_image_id",
        "isolation_conformance_digest",
        "invocation_conformance_digest",
        "invocation_harness_revision",
        "invocation_harness_digest",
        "deployment_policy_revision",
        "deployment_policy_digest",
    }
    digest_fields = {
        "deployment_binding_digest",
        "attestation_digest",
        "owner_scope_digest",
        "authenticated_principal_digest",
        "initiating_session_digest",
        "request_provenance_digest",
        "artifact_sha256",
        "spec_digest",
        "input_digest",
        "scope_digest",
        "runner_engine_identity_digest",
        "isolation_conformance_digest",
        "invocation_conformance_digest",
        "invocation_harness_digest",
        "deployment_policy_digest",
    }
    if (
        set(binding) != required
        or binding.get("schema_version") != BINDING_SCHEMA
        or binding.get("invocation_harness_revision") != HARNESS_REVISION
        or binding.get("deployment_policy_revision") != POLICY_REVISION
        or binding.get("mode")
        not in {"shadow", "read_only_canary", "scoped_canary", "promoted"}
        or any(
            not isinstance(binding.get(field), str)
            or len(binding[field]) != 64
            or any(ch not in "0123456789abcdef" for ch in binding[field])
            for field in digest_fields
        )
        or binding.get("invocation_harness_digest")
        != hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    ):
        raise HarnessFailure("binding_identity_failed")


def _validate_inputs(
    binding: dict[str, Any],
    source: bytes,
    spec: dict[str, Any],
    payload: dict[str, Any],
) -> None:
    if (
        not source
        or len(source) > MAX_SOURCE_BYTES
        or hashlib.sha256(source).hexdigest() != binding["artifact_sha256"]
    ):
        raise HarnessFailure("artifact_identity_failed")
    if _digest(spec) != binding["spec_digest"]:
        raise HarnessFailure("spec_identity_failed")
    if _digest(payload) != binding["input_digest"]:
        raise HarnessFailure("input_identity_failed")


def _validate_spec(spec: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
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
    return input_schema, output_schema


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
        any(type(key) is not str for key in payload)
        or not set(payload).issubset(properties)
        or not set(required).issubset(payload)
    ):
        return False
    return all(_primitive_accepts(properties[key], value) for key, value in payload.items())


def _primitive_accepts(schema: Any, value: Any) -> bool:
    if not isinstance(schema, dict):
        return False
    selected_type = schema.get("type")
    if selected_type == "string":
        if type(value) is not str:
            return False
        minimum = schema.get("minLength")
        maximum = schema.get("maxLength")
        return (
            type(minimum) is int
            and type(maximum) is int
            and minimum <= len(value) <= maximum
        )
    if selected_type == "integer":
        return type(value) is int and _numeric_bounds(schema, value)
    if selected_type == "number":
        return (
            type(value) in {int, float}
            and math.isfinite(float(value))
            and _numeric_bounds(schema, float(value))
        )
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
        text = source.decode("utf-8", errors="strict")
        tree = ast.parse(
            text,
            filename="<signed-extension-invocation>",
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
    output_properties = spec["output_schema"].get("properties")
    if not isinstance(output_properties, dict) or len(returned.keys) != len(output_properties):
        raise HarnessFailure("source_policy_failed")
    seen: set[str] = set()
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
            and value.slice.value in spec["input_schema"].get("required", [])
        ):
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
    if seen != set(output_properties) or any(isinstance(node, forbidden) for node in nodes):
        raise HarnessFailure("source_policy_failed")
    return tree


def _isolation_checks() -> bool:
    if os.getuid() == 0 or os.getgid() == 0:
        return False
    if any(Path(path).exists() for path in ("/Users", "/workspace", "/state", "/host", "/hostfs")):
        return False
    forbidden = (
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
    if any(any(fragment in key.upper() for fragment in forbidden) for key in os.environ):
        return False
    if _write_succeeds(Path("/signed-extension-rootfs-sentinel"), b"x"):
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
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.05)
        probe.connect(("192.0.2.1", 9))
    except OSError:
        return True
    finally:
        probe.close()
    return False


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
        code = compile(tree, "<signed-extension-invocation>", "exec")
        namespace: dict[str, Any] = {"__builtins__": {}}
        exec(code, namespace, namespace)
    except Exception as exc:
        raise HarnessFailure("candidate_load_failed") from exc
    candidate = namespace.get(ENTRYPOINT)
    if not callable(candidate) or candidate.__globals__.get("__builtins__") != {}:
        raise HarnessFailure("candidate_load_failed")
    return candidate


def _failed(
    *,
    binding: dict[str, Any] | None,
    source: bytes | None,
    spec: dict[str, Any] | None,
    payload: dict[str, Any] | None,
    issue_code: str,
) -> dict[str, Any]:
    binding_digest = _digest(binding) if isinstance(binding, dict) else "0" * 64
    artifact_sha256 = hashlib.sha256(source).hexdigest() if isinstance(source, bytes) else "0" * 64
    spec_digest = _digest(spec) if isinstance(spec, dict) else "0" * 64
    input_digest = _digest(payload) if isinstance(payload, dict) else "0" * 64
    return {
        "schema_version": RESULT_SCHEMA,
        "binding_digest": binding_digest,
        "artifact_sha256": artifact_sha256,
        "spec_digest": spec_digest,
        "input_digest": input_digest,
        "invocation_status": "failed",
        "source_policy_status": "failed" if issue_code == "source_policy_failed" else "passed",
        "contract_status": "failed" if issue_code in {"input_contract_failed", "output_contract_failed"} else "passed",
        "determinism_status": "failed" if issue_code == "determinism_failed" else "passed",
        "input_immutability_status": "failed" if issue_code == "input_mutation_failed" else "passed",
        "isolation_status": "failed" if issue_code == "isolation_failed" else "passed",
        "output_payload": None,
        "output_digest": None,
        "issue_code": issue_code,
    }


def run_harness() -> dict[str, Any]:
    binding: dict[str, Any] | None = None
    source: bytes | None = None
    spec: dict[str, Any] | None = None
    payload: dict[str, Any] | None = None
    try:
        binding = _load_json(BINDING_PATH)
        source = ARTIFACT_PATH.read_bytes()
        spec = _load_json(SPEC_PATH)
        payload = _load_json(PAYLOAD_PATH)
        _validate_binding(binding)
        _validate_inputs(binding, source, spec, payload)
        input_schema, output_schema = _validate_spec(spec)
        if not _schema_accepts(input_schema, payload):
            raise HarnessFailure("input_contract_failed")
        tree = _validate_source_shape(source, spec)
        if not _isolation_checks():
            raise HarnessFailure("isolation_failed")
        candidate = _load_candidate(tree)
        original = copy.deepcopy(payload)
        first_input = copy.deepcopy(payload)
        second_input = copy.deepcopy(payload)
        try:
            first = candidate(first_input)
            second = candidate(second_input)
        except BaseException as exc:
            raise HarnessFailure("candidate_execution_failed") from exc
        if first_input != original or second_input != original:
            raise HarnessFailure("input_mutation_failed")
        if first != second:
            raise HarnessFailure("determinism_failed")
        if not _schema_accepts(output_schema, first):
            raise HarnessFailure("output_contract_failed")
        return {
            "schema_version": RESULT_SCHEMA,
            "binding_digest": _digest(binding),
            "artifact_sha256": hashlib.sha256(source).hexdigest(),
            "spec_digest": _digest(spec),
            "input_digest": _digest(payload),
            "invocation_status": "passed",
            "source_policy_status": "passed",
            "contract_status": "passed",
            "determinism_status": "passed",
            "input_immutability_status": "passed",
            "isolation_status": "passed",
            "output_payload": first,
            "output_digest": _digest(first),
            "issue_code": None,
        }
    except HarnessFailure as exc:
        return _failed(
            binding=binding,
            source=source,
            spec=spec,
            payload=payload,
            issue_code=exc.issue_code,
        )
    except BaseException:
        return _failed(
            binding=binding,
            source=source,
            spec=spec,
            payload=payload,
            issue_code="harness_internal_error",
        )


def main() -> None:
    os.write(1, _canonical_bytes(run_harness()) + b"\n")


if __name__ == "__main__":
    main()
