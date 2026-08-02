#!/usr/bin/env python3
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from typing import Any, Callable

from pydantic import ValidationError


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from interface.extension_dynamic_validation import (  # noqa: E402
    DYNAMIC_VALIDATION_HARNESS_RESULT_SCHEMA_VERSION,
    DynamicValidationAuthority,
    DynamicValidationBinding,
    DynamicValidationHarnessResult,
    DynamicValidationTestBundle,
    parse_dynamic_validation_test_bundle,
)
from interface.extension_spec import parse_extension_spec  # noqa: E402
from runtime import extension_validation_harness as harness  # noqa: E402
from scripts.phase6_extension_dynamic_validation_test_support import (  # noqa: E402
    SOURCE,
    expect,
    make_binding,
    valid_spec,
    valid_test_bundle,
)


def rejected(
    mutate: Callable[[dict[str, Any]], None],
) -> bool:
    value = copy.deepcopy(valid_test_bundle())
    mutate(value)
    try:
        parse_dynamic_validation_test_bundle(value)
    except (TypeError, ValueError, ValidationError):
        return True
    return False


def write_inputs(
    root: Path,
    *,
    source: bytes,
    tests: dict[str, Any],
) -> tuple[DynamicValidationBinding, dict[str, Path]]:
    spec = parse_extension_spec(valid_spec())
    bundle = parse_dynamic_validation_test_bundle(tests)
    harness_digest = hashlib.sha256(
        Path(harness.__file__).read_bytes()
    ).hexdigest()
    binding = make_binding(
        harness_digest=harness_digest,
        test_bundle=bundle,
    )
    # Rebind the helper's fixed source identity for negative source cases.
    if source != SOURCE:
        values = binding.canonical_dict()
        values["artifact_sha256"] = hashlib.sha256(source).hexdigest()
        values["artifact_size_bytes"] = len(source)
        from interface.extension_dynamic_validation import (
            dynamic_build_identity_digest,
        )

        values["build_identity_digest"] = dynamic_build_identity_digest(values)
        values["validation_id"] = "extval_" + values[
            "build_identity_digest"
        ][:24]
        binding = DynamicValidationBinding.model_validate(values, strict=True)
    paths = {
        "artifact": root / "artifact.py",
        "spec": root / "spec.json",
        "tests": root / "tests.json",
        "binding": root / "binding.json",
    }
    paths["artifact"].write_bytes(source)
    paths["spec"].write_bytes(spec.canonical_bytes())
    paths["tests"].write_bytes(bundle.canonical_bytes())
    paths["binding"].write_bytes(binding.canonical_bytes())
    return binding, paths


def run_harness(
    root: Path,
    *,
    source: bytes = SOURCE,
    tests: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _binding, paths = write_inputs(
        root,
        source=source,
        tests=tests or valid_test_bundle(),
    )
    previous = (
        harness.ARTIFACT_PATH,
        harness.SPEC_PATH,
        harness.TESTS_PATH,
        harness.BINDING_PATH,
        harness._isolation_checks,
    )
    harness.ARTIFACT_PATH = paths["artifact"]
    harness.SPEC_PATH = paths["spec"]
    harness.TESTS_PATH = paths["tests"]
    harness.BINDING_PATH = paths["binding"]
    harness._isolation_checks = lambda: True
    try:
        return harness.validate()
    finally:
        (
            harness.ARTIFACT_PATH,
            harness.SPEC_PATH,
            harness.TESTS_PATH,
            harness.BINDING_PATH,
            harness._isolation_checks,
        ) = previous


def main() -> int:
    first = parse_dynamic_validation_test_bundle(valid_test_bundle())
    second = parse_dynamic_validation_test_bundle(first.canonical_dict())
    expect(
        first.canonical_bytes() == second.canonical_bytes()
        and first.bundle_digest() == second.bundle_digest(),
        "caller-frozen test bundle has deterministic canonical identity",
    )
    rejection_cases: list[tuple[str, Callable[[dict[str, Any]], None]]] = [
        (
            "generator-origin tests",
            lambda value: value.__setitem__("origin", "generated_with_candidate"),
        ),
        (
            "extra executable oracle",
            lambda value: value.__setitem__("oracle", "lambda output: True"),
        ),
        (
            "duplicate case ids",
            lambda value: value["vectors"].append(
                copy.deepcopy(value["vectors"][0])
            ),
        ),
        (
            "nested input payload",
            lambda value: value["vectors"][0]["input_payload"].__setitem__(
                "nested", {"secret": "x"}
            ),
        ),
        (
            "non-finite output",
            lambda value: value["vectors"][0]["expected_output"].__setitem__(
                "score", float("nan")
            ),
        ),
    ]
    for label, mutate in rejection_cases:
        expect(rejected(mutate), f"strict bundle rejects {label}")

    binding = make_binding()
    drifted = binding.canonical_dict()
    drifted["artifact_sha256"] = "9" * 64
    try:
        DynamicValidationBinding.model_validate(drifted, strict=True)
    except ValidationError:
        build_drift_rejected = True
    else:
        build_drift_rejected = False
    expect(
        build_drift_rejected,
        "build identity rejects artifact CAS drift",
    )

    elevated = {
        "schema_version": DYNAMIC_VALIDATION_HARNESS_RESULT_SCHEMA_VERSION,
        "binding_digest": binding.binding_digest(),
        "build_identity_digest": binding.build_identity_digest,
        "artifact_sha256": binding.artifact_sha256,
        "test_bundle_digest": binding.test_bundle_digest,
        "validation_status": "passed",
        "candidate_execution_status": "passed",
        "unit_checks_status": "passed",
        "contract_checks_status": "passed",
        "security_runtime_checks_status": "passed",
        "fuzz_checks_status": "passed",
        "behavior_verification_status": "passed",
        "vector_count": 2,
        "fuzz_case_count": 32,
        "issue_codes": [],
        "promotion_authorized": True,
    }
    try:
        DynamicValidationHarnessResult.model_validate(elevated, strict=True)
    except ValidationError:
        elevation_rejected = True
    else:
        elevation_rejected = False
    expect(
        elevation_rejected
        and not any(DynamicValidationAuthority().model_dump().values()),
        "validation contract rejects authority elevation",
    )

    with TemporaryDirectory(prefix="veyra-phase6-dynamic-contract-") as raw:
        result = run_harness(Path(raw))
        parsed = DynamicValidationHarnessResult.model_validate(
            result, strict=True
        )
        expect(
            parsed.validation_status == "passed"
            and parsed.candidate_execution_status == "passed"
            and parsed.unit_checks_status == "passed"
            and parsed.contract_checks_status == "passed"
            and parsed.security_runtime_checks_status == "passed"
            and parsed.fuzz_checks_status == "passed"
            and parsed.behavior_verification_status == "passed"
            and parsed.vector_count == 2
            and parsed.fuzz_case_count == 32,
            "fixed harness runs unit contract security fuzz and behavior checks",
            parsed.canonical_dict(),
        )

    wrong = valid_test_bundle()
    wrong["vectors"][1]["expected_output"] = {"label": "Wrong"}
    with TemporaryDirectory(prefix="veyra-phase6-dynamic-behavior-") as raw:
        result = run_harness(Path(raw), tests=wrong)
        expect(
            result["validation_status"] == "failed"
            and result["unit_checks_status"] == "failed"
            and result["behavior_verification_status"] == "failed"
            and "behavior_verification_failed" in result["issue_codes"],
            "frozen behavior mismatch fails closed",
            result,
        )

    malicious = (
        b'def run_extension(payload):\n'
        b'    return {"label": __import__("os").getenv("HOME")}\n'
    )
    with TemporaryDirectory(prefix="veyra-phase6-dynamic-source-") as raw:
        result = run_harness(Path(raw), source=malicious)
        expect(
            result["validation_status"] == "failed"
            and result["candidate_execution_status"] == "failed"
            and "source_policy_failed" in result["issue_codes"],
            "harness re-parses AST and rejects call/attribute source before execution",
            result,
        )

    serialized = json.dumps(result, sort_keys=True)
    expect(
        "HOME" not in serialized and "__import__" not in serialized,
        "harness report contains no candidate source or secret surface",
    )
    print("phase6 extension dynamic validation contract smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
