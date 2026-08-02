from __future__ import annotations

from datetime import timedelta
from pathlib import Path
import sys
import tempfile
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pydantic import ValidationError

from interface.extension_deployment import (
    EXTENSION_INVOCATION_HARNESS_REVISION,
    EXTENSION_REVIEW_APPROVAL_SCHEMA_VERSION,
    ExtensionDeploymentAuthority,
    ExtensionDeploymentBinding,
    ExtensionInvocationResult,
    ExtensionReviewApproval,
    canonical_utc,
)
from runtime.extension_invocation_harness import (
    HarnessFailure,
    _load_candidate,
    _schema_accepts,
    _validate_source_shape,
)
from runtime.trusted_extension_invocation_runner import (
    TrustedExtensionInvocationRunner,
)
from runtime.trusted_isolated_runner import TrustedIsolatedRunnerBackend
from scripts.phase6_extension_deployment_test_support import (
    BASE_TIME,
    TOKEN,
    build_gate,
    expect,
    expect_raises,
    propose,
)
from scripts.phase6_extension_dynamic_validation_test_support import (
    SOURCE,
    valid_spec,
)


def main() -> None:
    expect(
        not any(ExtensionDeploymentAuthority().model_dump(mode="python").values()),
        "deployment authority is entirely false",
    )

    with tempfile.TemporaryDirectory(prefix="veyra-phase6-contract-") as tmp:
        gate, _, _, _ = build_gate(Path(tmp))
        deployment = propose(gate)
        record = gate.state_store.read_json(
            "phase6_extension_deployment_state.json"
        )["deployments"][deployment["deployment_id"]]
        binding = ExtensionDeploymentBinding.model_validate(
            record["binding"], strict=True
        )
        expect(binding.mode == "record_only", "deployment binding parses strictly")
        invalid_binding = binding.model_dump(mode="python")
        invalid_binding["max_invocations"] = 1
        expect_raises(
            (ValidationError,),
            lambda: ExtensionDeploymentBinding.model_validate(
                invalid_binding, strict=True
            ),
            "record_only binding rejects execution budget",
        )
        invalid_binding = binding.model_dump(mode="python")
        invalid_binding["mode"] = "shadow"
        invalid_binding["mode_epoch"] = 1
        expect_raises(
            (ValidationError,),
            lambda: ExtensionDeploymentBinding.model_validate(
                invalid_binding, strict=True
            ),
            "executing binding requires a positive budget",
        )
        state = gate.state_store.read_json(
            "phase6_extension_deployment_state.json"
        )
        expect(
            "source_bytes" not in str(state)
            and "source_check_spec" not in str(state),
            "deployment state is source-free",
        )

    spec = valid_spec()
    input_schema = spec["input_schema"]
    output_schema = spec["output_schema"]
    expect(_schema_accepts(input_schema, {"name": "Veyra"}), "input schema accepts valid payload")
    expect(
        not _schema_accepts(input_schema, {"name": "Veyra", "unknown": 1}),
        "input schema rejects additional fields",
    )
    tree = _validate_source_shape(SOURCE, spec)
    candidate = _load_candidate(tree)
    expect(
        candidate.__globals__["__builtins__"] == {}
        and candidate({"name": "Veyra"}) == {"label": "Veyra"},
        "fixed harness loads candidate with empty builtins",
    )
    expect(_schema_accepts(output_schema, {"label": "Veyra"}), "output schema accepts exact pure output")
    expect_raises(
        (HarnessFailure,),
        lambda: _validate_source_shape(
            b"def run_extension(payload):\n    return {'label': __import__('os')}\n",
            spec,
        ),
        "fixed harness rejects call and import surfaces",
    )
    expect_raises(
        (HarnessFailure,),
        lambda: _validate_source_shape(
            b"def run_extension(payload):\n    payload['name'] = 'x'\n    return {'label': 'x'}\n",
            spec,
        ),
        "fixed harness rejects mutation statements",
    )

    backend = TrustedIsolatedRunnerBackend(
        docker_binary="/bin/false",
        docker_context="colima-veyra-runner",
    )
    runner = TrustedExtensionInvocationRunner(isolation_backend=backend)
    command = runner._invocation_create_command(
        docker_binary="/bin/false",
        image_id="sha256:" + "a" * 64,
        volume_name="veyra-phase6-invoke-" + "b" * 32,
        cidfile=Path("/tmp/veyra-phase6-invocation.cid"),
    )
    joined = " ".join(command)
    for required in (
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges=true",
        "--security-opt=seccomp=builtin",
        "--pids-limit=16",
        "--memory=32m",
        "--memory-swap=32m",
        "--cpus=0.25",
        "--log-driver=none",
        "volume-subpath=input,readonly",
        "volume-subpath=harness,readonly",
        "/runner/invocation_harness.py",
    ):
        expect(required in joined, f"fixed runner includes {required}")
    for forbidden in ("--env", "--privileged", "type=bind", "--network=host"):
        expect(forbidden not in joined, f"fixed runner excludes {forbidden}")
    expect(
        runner.harness_digest()
        and EXTENSION_INVOCATION_HARNESS_REVISION
        == "veyra.phase6.fixed_signed_extension_invocation_harness.v1",
        "invocation harness has a fixed identity",
    )

    review = ExtensionReviewApproval(
        schema_version=EXTENSION_REVIEW_APPROVAL_SCHEMA_VERSION,
        review_id="review.contract",
        status="approved",
        receipt_origin="veyra_review_queue_approved_identity",
        receipt_digest="e" * 64,
        deployment_id="extdep_" + "a" * 24,
        deployment_revision=4,
        release_id="extrel_" + "b" * 24,
        attestation_digest="c" * 64,
        current_mode="scoped_canary",
        target_mode="promoted",
        mode_epoch=3,
        approver_identity_digest="d" * 64,
        approved_at=canonical_utc(BASE_TIME),
        expires_at=canonical_utc(BASE_TIME + timedelta(minutes=10)),
    )
    expect(review.status == "approved", "promotion review contract parses")
    invalid_review = review.model_dump(mode="python")
    invalid_review["expires_at"] = invalid_review["approved_at"]
    expect_raises(
        (ValidationError,),
        lambda: ExtensionReviewApproval.model_validate(invalid_review, strict=True),
        "promotion review rejects empty approval window",
    )

    expect(
        "output_payload" in ExtensionInvocationResult.model_fields,
        "invocation result makes returned data explicit",
    )
    print("Phase 6 signed extension deployment contract smoke passed")


if __name__ == "__main__":
    main()
