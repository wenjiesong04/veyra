#!/usr/bin/env python3
from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
from typing import Any, Callable

from pydantic import ValidationError


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from interface.extension_artifact import (  # noqa: E402
    EXTENSION_ARTIFACT_POLICY_REVISION,
)
from interface.extension_isolated_runner import (  # noqa: E402
    ISOLATED_RUNNER_BACKEND_KIND,
    ISOLATED_RUNNER_BACKEND_STATUS_SCHEMA_VERSION,
    ISOLATED_RUNNER_BINDING_SCHEMA_VERSION,
    ISOLATED_RUNNER_HARNESS_REVISION,
    ISOLATED_RUNNER_POLICY_DIGEST,
    ISOLATED_RUNNER_POLICY_REVISION,
    ISOLATED_RUNNER_REPORT_SCHEMA_VERSION,
    IsolatedRunnerAuthority,
    IsolatedRunnerBackendStatus,
    IsolatedRunnerBinding,
    IsolatedRunnerReport,
    parse_isolated_runner_backend_status,
    parse_isolated_runner_binding,
    parse_isolated_runner_report,
)
from interface.extension_source_check import (  # noqa: E402
    EXTENSION_SOURCE_CHECK_POLICY_REVISION,
    EXTENSION_SOURCE_CHECKER_REVISION,
    EXTENSION_SOURCE_PARSER_IDENTITY,
    EXTENSION_SOURCE_RULESET_DIGEST,
)
from interface.extension_spec import EXTENSION_POLICY_REVISION  # noqa: E402


FIXED_TIME = "2026-08-02T08:00:00Z"


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"PASS {label}")


def expect_rejected(call: Callable[[], Any], label: str) -> None:
    try:
        call()
    except (TypeError, ValueError, ValidationError):
        print(f"PASS {label}")
        return
    raise AssertionError(f"{label}: invalid contract was accepted")


def valid_backend_status() -> IsolatedRunnerBackendStatus:
    return IsolatedRunnerBackendStatus(
        schema_version=ISOLATED_RUNNER_BACKEND_STATUS_SCHEMA_VERSION,
        backend_kind=ISOLATED_RUNNER_BACKEND_KIND,
        availability="available",
        reason_code="ready",
        runner_policy_revision=ISOLATED_RUNNER_POLICY_REVISION,
        runner_policy_digest=ISOLATED_RUNNER_POLICY_DIGEST,
        harness_revision=ISOLATED_RUNNER_HARNESS_REVISION,
        harness_digest="1" * 64,
        image_id="sha256:" + "2" * 64,
        image_conformance_digest="3" * 64,
        engine_identity_digest="4" * 64,
        conformance_certified=True,
        authority=IsolatedRunnerAuthority(),
    )


def valid_binding() -> IsolatedRunnerBinding:
    backend = valid_backend_status()
    assert backend.image_id is not None
    assert backend.image_conformance_digest is not None
    assert backend.engine_identity_digest is not None
    return IsolatedRunnerBinding(
        schema_version=ISOLATED_RUNNER_BINDING_SCHEMA_VERSION,
        run_id="extrun_0123456789abcdef01234567",
        check_id="extcheck_0123456789abcdef01234567",
        source_check_binding_digest="5" * 64,
        source_check_report_digest="6" * 64,
        source_check_stage="SOURCE_CHECK_PASSED",
        candidate_id="extspec_0123456789abcdef01234567",
        candidate_revision=2,
        artifact_id="extart_0123456789abcdef01234567",
        artifact_revision=1,
        artifact_envelope_digest="7" * 64,
        owner_scope_digest="8" * 64,
        extension_id="example.isolated_contract",
        extension_version=1,
        spec_digest="9" * 64,
        artifact_sha256="a" * 64,
        artifact_size_bytes=83,
        extension_policy_revision=EXTENSION_POLICY_REVISION,
        artifact_policy_revision=EXTENSION_ARTIFACT_POLICY_REVISION,
        source_check_policy_revision=EXTENSION_SOURCE_CHECK_POLICY_REVISION,
        source_checker_revision=EXTENSION_SOURCE_CHECKER_REVISION,
        source_parser_identity=EXTENSION_SOURCE_PARSER_IDENTITY,
        source_ruleset_digest=EXTENSION_SOURCE_RULESET_DIGEST,
        runner_policy_revision=backend.runner_policy_revision,
        runner_policy_digest=backend.runner_policy_digest,
        backend_kind=backend.backend_kind,
        engine_identity_digest=backend.engine_identity_digest,
        image_id=backend.image_id,
        image_conformance_digest=backend.image_conformance_digest,
        harness_revision=backend.harness_revision,
        harness_digest=backend.harness_digest,
    )


def valid_report() -> IsolatedRunnerReport:
    binding = valid_binding()
    return IsolatedRunnerReport(
        schema_version=ISOLATED_RUNNER_REPORT_SCHEMA_VERSION,
        binding=binding,
        binding_digest=binding.binding_digest(),
        probe_status="passed",
        artifact_identity_status="passed",
        harness_identity_status="passed",
        non_root_status="passed",
        rootfs_read_only_status="passed",
        input_read_only_status="passed",
        network_isolation_status="passed",
        secret_isolation_status="passed",
        host_surface_isolation_status="passed",
        resource_limits_status="passed",
        isolated_runner_status="passed",
        completed_at=FIXED_TIME,
        authority=IsolatedRunnerAuthority(),
    )


def main() -> int:
    backend = valid_backend_status()
    binding = valid_binding()
    report = valid_report()

    rebound = parse_isolated_runner_binding(
        dict(reversed(list(binding.canonical_dict().items())))
    )
    rereport = parse_isolated_runner_report(
        dict(reversed(list(report.canonical_dict().items())))
    )
    restatus = parse_isolated_runner_backend_status(
        dict(reversed(list(backend.canonical_dict().items())))
    )
    expect(
        rebound.canonical_bytes() == binding.canonical_bytes()
        and rebound.binding_digest() == binding.binding_digest()
        and rereport.canonical_bytes() == report.canonical_bytes()
        and rereport.report_digest() == report.report_digest()
        and restatus.canonical_bytes() == backend.canonical_bytes(),
        "binding, report, and backend have stable canonical identities",
    )
    expect(
        binding.source_parser_identity == EXTENSION_SOURCE_PARSER_IDENTITY
        and binding.source_ruleset_digest == EXTENSION_SOURCE_RULESET_DIGEST
        and binding.source_checker_revision == EXTENSION_SOURCE_CHECKER_REVISION
        and binding.engine_identity_digest == backend.engine_identity_digest
        and binding.image_id == backend.image_id
        and binding.image_conformance_digest
        == backend.image_conformance_digest
        and binding.harness_revision == ISOLATED_RUNNER_HARNESS_REVISION
        and binding.harness_digest == backend.harness_digest
        and binding.runner_policy_digest == ISOLATED_RUNNER_POLICY_DIGEST,
        "exact parser, ruleset, engine, image, harness, and policy are bound",
    )

    authority_documents = (
        backend.authority.model_dump(mode="python"),
        report.authority.model_dump(mode="python"),
    )
    expect(
        all(
            document and not any(document.values())
            for document in authority_documents
        ),
        "backend and report grant zero authority",
    )
    unavailable_statuses = {
        "candidate_execution_status": "not_started",
        "unit_checks_status": "not_started",
        "contract_checks_status": "not_started",
        "security_runtime_checks_status": "not_started",
        "fuzz_checks_status": "not_started",
        "behavior_verification_status": "not_started",
        "signature_status": "not_implemented",
        "activation_status": "not_installed",
        "capability_registry_visible": False,
        "promotion_authorized": False,
        "policy_effect": "none",
    }
    expect(
        all(
            getattr(report, key) == value
            for key, value in unavailable_statuses.items()
        ),
        (
            "candidate tests, execution, signing, install, activation, "
            "and promotion remain unavailable"
        ),
        report.canonical_dict(),
    )

    private_value_sentinels = (
        "PRIVATE_SOURCE_SENTINEL",
        "PRIVATE_PATH_SENTINEL",
        "PRIVATE_LOG_SENTINEL",
        "PRIVATE_ENV_SENTINEL",
    )
    payload = json.dumps(
        {
            "backend": backend.canonical_dict(),
            "binding": binding.canonical_dict(),
            "report": report.canonical_dict(),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    forbidden_raw_fields = {
        "source",
        "source_bytes",
        "path",
        "host_path",
        "stdout",
        "stderr",
        "logs",
        "environment",
        "env",
        "argv",
        "command",
    }
    public_keys = set(backend.canonical_dict()) | set(report.canonical_dict())
    expect(
        not forbidden_raw_fields.intersection(public_keys)
        and not any(sentinel in payload for sentinel in private_value_sentinels),
        (
            "canonical contracts contain no source, log, path, argv, or "
            "environment payload"
        ),
    )

    extra_binding = binding.canonical_dict()
    extra_binding["source"] = "PRIVATE_SOURCE_SENTINEL"
    expect_rejected(
        lambda: parse_isolated_runner_binding(extra_binding),
        "binding rejects caller source and unknown fields",
    )
    mutable_identity = binding.canonical_dict()
    mutable_identity["image_id"] = "veyra/runner:latest"
    expect_rejected(
        lambda: parse_isolated_runner_binding(mutable_identity),
        "binding rejects mutable image tags",
    )
    coerced_revision = binding.canonical_dict()
    coerced_revision["artifact_revision"] = "1"
    expect_rejected(
        lambda: parse_isolated_runner_binding(coerced_revision),
        "binding rejects coerced revision identity",
    )
    inconsistent_backend = backend.canonical_dict()
    inconsistent_backend["conformance_certified"] = False
    expect_rejected(
        lambda: parse_isolated_runner_backend_status(inconsistent_backend),
        "available backend requires certified exact identities",
    )
    tampered_report = copy.deepcopy(report.canonical_dict())
    tampered_report["binding_digest"] = "f" * 64
    expect_rejected(
        lambda: parse_isolated_runner_report(tampered_report),
        "report rejects binding digest tamper",
    )
    elevated_report = copy.deepcopy(report.canonical_dict())
    elevated_report["authority"]["candidate_execution"] = True
    expect_rejected(
        lambda: parse_isolated_runner_report(elevated_report),
        "report cannot elevate candidate execution authority",
    )
    premature_test = copy.deepcopy(report.canonical_dict())
    premature_test["unit_checks_status"] = "passed"
    expect_rejected(
        lambda: parse_isolated_runner_report(premature_test),
        "report cannot claim candidate unit checks",
    )
    inconsistent_failure = copy.deepcopy(report.canonical_dict())
    inconsistent_failure["probe_status"] = "failed"
    expect_rejected(
        lambda: parse_isolated_runner_report(inconsistent_failure),
        "report status and probe evidence must be internally consistent",
    )

    print("phase6 extension isolated-runner contract smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
