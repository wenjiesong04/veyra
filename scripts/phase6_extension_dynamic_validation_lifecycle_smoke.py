#!/usr/bin/env python3
from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime.extension_dynamic_validation_gate import (  # noqa: E402
    STATE_FILE,
    ExtensionDynamicValidationConflictError,
    ExtensionDynamicValidationGate,
    ExtensionDynamicValidationNotFoundError,
    ExtensionDynamicValidationStorageError,
    ExtensionDynamicValidationUnauthorizedError,
    ExtensionDynamicValidationUnavailableError,
)
from scripts.phase6_extension_dynamic_validation_test_support import (  # noqa: E402
    CONTROL_TOKEN,
    SOURCE,
    SESSION,
    REQUEST,
    USER,
    WORKSPACE,
    Clock,
    FakeIsolatedRunnerGate,
    FakeValidationBackend,
    expect,
    expect_raises,
    initialize_store,
    make_subject,
    valid_test_bundle,
)
from scripts import (  # noqa: E402
    phase6_extension_isolated_runner_lifecycle_smoke as runner_support,
)


def persisted_tree(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def build_gate(
    root: Path,
    *,
    subject_gate: FakeIsolatedRunnerGate | None = None,
    backend: FakeValidationBackend | None = None,
    enabled: bool = True,
    token: str = CONTROL_TOKEN,
    clock: Clock | None = None,
) -> tuple[
    ExtensionDynamicValidationGate,
    FakeIsolatedRunnerGate,
    FakeValidationBackend,
]:
    store = initialize_store(root)
    selected_subject = subject_gate or FakeIsolatedRunnerGate()
    selected_backend = backend or FakeValidationBackend()
    gate = ExtensionDynamicValidationGate(
        state_store=store,
        isolated_runner_gate=selected_subject,  # type: ignore[arg-type]
        backend=selected_backend,  # type: ignore[arg-type]
        enabled=enabled,
        control_token=token,
        now=clock or Clock(),
    )
    return gate, selected_subject, selected_backend


def start_payload(**overrides: Any) -> dict[str, Any]:
    subject = make_subject()
    value: dict[str, Any] = {
        "isolated_run_id": subject["run_id"],
        "user_id": USER,
        "workspace_id": WORKSPACE,
        "session_id": SESSION,
        "request_id": REQUEST,
        "operation_id": "dynamic-validation-start",
        "expected_artifact_revision": subject["artifact_revision"],
        "expected_artifact_sha256": subject["artifact_sha256"],
        "expected_source_check_report_digest": subject[
            "source_check_report_digest"
        ],
        "expected_isolated_runner_report_digest": subject[
            "isolated_runner_report_digest"
        ],
        "test_bundle": valid_test_bundle(),
        "control_token": CONTROL_TOKEN,
    }
    value.update(overrides)
    return value


def assert_public_redacted(value: dict[str, Any], label: str) -> None:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True)
    private_fields = {
        "owner_scope_digest",
        "binding",
        "report",
        "report_digest",
        "test_bundle",
        "test_contract",
        "vectors",
        "input_payload",
        "expected_output",
        "source",
        "source_bytes",
        "user_id",
        "workspace_id",
        "path",
        "command",
        "environment",
        "stdout",
        "stderr",
    }
    expect(
        not private_fields.intersection(value)
        and SOURCE.decode("utf-8") not in serialized
        and USER not in serialized
        and WORKSPACE not in serialized
        and "Veyra" not in serialized,
        label,
        value,
    )


def authorization_and_pure_get() -> None:
    with TemporaryDirectory(prefix="veyra-phase6-dynamic-auth-") as raw:
        root = Path(raw)
        gate, subject, backend = build_gate(root, enabled=False)
        before = persisted_tree(root)
        expect_raises(
            (ExtensionDynamicValidationUnavailableError,),
            lambda: gate.start(**start_payload()),
            "disabled dynamic validation rejects before reads or dispatch",
        )
        expect(
            persisted_tree(root) == before
            and subject.calls == 0
            and backend.status_calls == 0
            and backend.run_calls == 0,
            "disabled admission has zero state or backend effects",
        )

        gate.enabled = True
        expect_raises(
            (ExtensionDynamicValidationUnauthorizedError,),
            lambda: gate.start(
                **start_payload(control_token="wrong-control-token")
            ),
            "invalid token rejects before state or prerequisite access",
        )
        expect(
            persisted_tree(root) == before
            and subject.calls == 0
            and backend.status_calls == 0,
            "unauthorized admission has zero observable effects",
        )

        status = gate.status()
        listed = gate.list(
            user_id=USER,
            workspace_id=WORKSPACE,
            session_id=SESSION,
            control_token=CONTROL_TOKEN,
        )
        expect(
            status["backend_snapshot"]["status"] == "missing"
            and status["admission"]["start_ready"] is False
            and listed["count"] == 0
            and persisted_tree(root) == before
            and subject.calls == 0
            and backend.status_calls == 0,
            "status and empty list are pure and never observe Docker",
            status,
        )


def success_replay_owner_cas_and_tamper() -> None:
    with TemporaryDirectory(prefix="veyra-phase6-dynamic-success-") as raw:
        root = Path(raw)
        observed: list[str] = []
        gate_ref: list[ExtensionDynamicValidationGate] = []

        def observe_started(binding: Any) -> None:
            state = gate_ref[0].state_store.read_json(STATE_FILE)
            observed.append(state["validations"][binding.validation_id]["stage"])

        backend = FakeValidationBackend(before_return=observe_started)
        gate, subject, _ = build_gate(root, backend=backend)
        gate_ref.append(gate)
        result = gate.start(**start_payload())
        expect(
            result["stored_stage"] == "DYNAMIC_VALIDATION_PASSED"
            and result["effective_status"] == "DYNAMIC_VALIDATION_PASSED"
            and result["candidate_execution_status"] == "passed"
            and result["unit_checks_status"] == "passed"
            and result["contract_checks_status"] == "passed"
            and result["security_runtime_checks_status"] == "passed"
            and result["fuzz_checks_status"] == "passed"
            and result["behavior_verification_status"] == "passed"
            and observed == ["DYNAMIC_VALIDATION_STARTED"]
            and backend.status_calls == 1
            and backend.run_calls == 1
            and subject.calls == 3,
            "claim and STARTED persist before one exact validation dispatch",
            {"result": result, "observed": observed},
        )
        expect(
            result["signature_status"] == "not_implemented"
            and result["activation_status"] == "not_installed"
            and result["capability_registry_visible"] is False
            and result["canary_status"] == "not_started"
            and result["promotion_authorized"] is False
            and result["policy_effect"] == "none"
            and not any(result["authority"].values()),
            "passed validation grants no signing registry canary or promotion authority",
            result,
        )
        assert_public_redacted(
            result,
            "public passed record omits source owner and frozen behavior vectors",
        )
        private = gate.state_store.read_json(STATE_FILE)
        record = private["validations"][result["validation_id"]]
        expect(
            record["test_bundle"]["origin"]
            == "caller_frozen_outside_generator"
            and len(record["test_bundle"]["vectors"]) == 2
            and record["test_bundle_digest"]
            == result["test_bundle_digest"]
            and record["test_contract"]["vector_count"] == 2
            and record["test_contract_digest"]
            == result["test_contract_digest"],
            "private state durably binds the exact caller test bundle and schema contract",
        )

        before_replay = persisted_tree(root)
        replay = gate.start(**start_payload())
        expect(
            replay["validation_id"] == result["validation_id"]
            and replay["operation_replayed"] is True
            and backend.status_calls == 1
            and backend.run_calls == 1
            and subject.calls == 3
            and persisted_tree(root) == before_replay,
            "exact operation replay performs no prerequisite or candidate execution",
            replay,
        )

        rebound = valid_test_bundle()
        rebound["vectors"][1]["expected_output"] = {"label": "Different"}
        expect_raises(
            (ExtensionDynamicValidationConflictError,),
            lambda: gate.start(**start_payload(test_bundle=rebound)),
            "operation id cannot be rebound to different expected behavior",
        )
        expect(
            backend.run_calls == 1 and subject.calls == 3,
            "operation rebinding fails before any prerequisite or backend call",
        )

        schema_invalid = valid_test_bundle()
        schema_invalid["vectors"][0]["expected_output"] = {"label": "x" * 161}
        expect_raises(
            (ExtensionDynamicValidationConflictError,),
            lambda: gate.start(
                **start_payload(
                    operation_id="schema-invalid-start",
                    test_bundle=schema_invalid,
                )
            ),
            "expected behavior outside output schema fails before backend observation",
        )
        expect(
            backend.status_calls == 1 and backend.run_calls == 1,
            "schema-invalid vectors never reach executable backend",
        )

        before_reads = persisted_tree(root)
        calls_before = (subject.calls, backend.status_calls, backend.run_calls)
        status = gate.status()
        detail = gate.get(
            validation_id=result["validation_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
            session_id=SESSION,
            control_token=CONTROL_TOKEN,
        )
        listed = gate.list(
            user_id=USER,
            workspace_id=WORKSPACE,
            session_id=SESSION,
            control_token=CONTROL_TOKEN,
        )
        integrity = gate.integrity(
            validation_id=result["validation_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
            session_id=SESSION,
            control_token=CONTROL_TOKEN,
        )
        expect(
            status["backend_snapshot"]["status"] == "fresh"
            and detail["validation_id"] == result["validation_id"]
            and listed["count"] == 1
            and integrity["status"] == "dynamic_validation_integrity_passed"
            and integrity["prerequisite_verification_status"]
            == "not_refreshed"
            and (subject.calls, backend.status_calls, backend.run_calls)
            == calls_before
            and persisted_tree(root) == before_reads,
            "all dynamic validation GET paths are byte-pure and call nothing external",
        )
        for label, value in {
            "detail": detail,
            "list record": listed["validations"][0],
            "integrity": integrity,
        }.items():
            assert_public_redacted(value, f"{label} remains privately redacted")

        expect_raises(
            (ExtensionDynamicValidationConflictError,),
            lambda: gate.get(
                validation_id=result["validation_id"],
                user_id=USER,
                workspace_id="/another/workspace",
                session_id=SESSION,
                control_token=CONTROL_TOKEN,
            ),
            "workspace isolation fails closed",
        )
        expect_raises(
            (ExtensionDynamicValidationNotFoundError,),
            lambda: gate.get(
                validation_id=result["validation_id"],
                user_id=USER,
                workspace_id=WORKSPACE,
                session_id="another-session",
                control_token=CONTROL_TOKEN,
            ),
            "same user and workspace cannot read another session record",
        )

        def tamper(state: dict[str, Any]) -> None:
            state["validations"][result["validation_id"]]["test_bundle"][
                "vectors"
            ][0]["expected_output"]["label"] = "Tampered"

        gate.state_store.mutate_json(STATE_FILE, tamper)
        expect_raises(
            (ExtensionDynamicValidationStorageError,),
            lambda: gate.get(
                validation_id=result["validation_id"],
                user_id=USER,
                workspace_id=WORKSPACE,
                session_id=SESSION,
                control_token=CONTROL_TOKEN,
            ),
            "private behavior vector tamper invalidates the entire state",
        )


def crash_and_prerequisite_drift_do_not_replay() -> None:
    with TemporaryDirectory(prefix="veyra-phase6-dynamic-crash-") as raw:
        root = Path(raw)
        backend = FakeValidationBackend(crash=KeyboardInterrupt())
        gate, subject, _ = build_gate(root, backend=backend)
        expect_raises(
            (KeyboardInterrupt,),
            lambda: gate.start(**start_payload(operation_id="crash-start")),
            "BaseException escapes after durable STARTED",
        )
        private = gate.state_store.read_json(STATE_FILE)
        record = next(iter(private["validations"].values()))
        detail = gate.get(
            validation_id=record["validation_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
            session_id=SESSION,
            control_token=CONTROL_TOKEN,
        )
        expect(
            record["stage"] == "DYNAMIC_VALIDATION_STARTED"
            and detail["effective_status"] == "DYNAMIC_VALIDATION_INDETERMINATE"
            and backend.run_calls == 1,
            "crashed STARTED record is exposed as indeterminate",
            detail,
        )
        replay = gate.start(
            **start_payload(operation_id="crash-start")
        )
        expect(
            replay["operation_replayed"] is True
            and replay["effective_status"] == "DYNAMIC_VALIDATION_INDETERMINATE"
            and backend.run_calls == 1,
            "crashed validation never automatically replays candidate execution",
            replay,
        )

    with TemporaryDirectory(prefix="veyra-phase6-dynamic-drift-") as raw:
        root = Path(raw)
        subject_gate = FakeIsolatedRunnerGate()

        def drift_after_execution(_binding: Any) -> None:
            subject_gate.subject["artifact_envelope_digest"] = "9" * 64

        backend = FakeValidationBackend(before_return=drift_after_execution)
        gate, _, _ = build_gate(
            root,
            subject_gate=subject_gate,
            backend=backend,
        )
        result = gate.start(
            **start_payload(operation_id="drift-after-execution")
        )
        expect(
            result["stored_stage"] == "DYNAMIC_VALIDATION_INDETERMINATE"
            and result["effective_status"] == "DYNAMIC_VALIDATION_INDETERMINATE"
            and result["failure_code"] == "prerequisite_changed"
            and backend.run_calls == 1,
            "post-run prerequisite CAS drift cannot produce passed evidence",
            result,
        )


def exact_real_passed_subject_boundary() -> None:
    with TemporaryDirectory(prefix="veyra-phase6-dynamic-subject-") as raw:
        context, checked, source_report = runner_support.passed_context(
            Path(raw),
            extension_id="example.dynamic_subject",
            operation_prefix="dynamic-subject",
        )
        backend = runner_support.CertifiedFakeBackend()
        runner_gate = runner_support.build_gate(context, backend)
        run = runner_support.start_run(
            runner_gate,
            context,
            checked,
            source_report,
            "dynamic-subject-run",
        )
        private = context.store.read_json(runner_support.RUNNER_STATE_FILE)
        runner_report = private["runs"][run["run_id"]]["report_digest"]
        # The 6.2d lifecycle helper wraps the source gate only to count calls;
        # the production assembly passes the concrete gate directly.
        runner_gate.source_check_gate = context.source_gate
        before = runner_support.persisted_tree_bytes(context)
        subject = runner_gate.dynamic_validation_subject(
            run_id=run["run_id"],
            user_id=runner_support.USER,
            workspace_id=runner_support.WORKSPACE,
            expected_artifact_revision=context.artifact["artifact_revision"],
            expected_artifact_sha256=context.artifact["artifact_sha256"],
            expected_source_check_report_digest=source_report,
            expected_isolated_runner_report_digest=runner_report,
        )
        expect(
            subject["isolated_runner_stage"] == "RUNNER_JOB_PASSED"
            and subject["source_check_stage"] == "SOURCE_CHECK_PASSED"
            and subject["artifact_sha256"]
            == context.artifact["artifact_sha256"]
            and subject["source_bytes"] == context.source
            and isinstance(subject["source_check_spec"], dict)
            and not any(subject["authority"].values())
            and backend.calls == 1
            and runner_support.persisted_tree_bytes(context) == before,
            "real 6.2d gate exposes only exact dual-passed source/Spec evidence in process",
        )
        expect_raises(
            (runner_support.ExtensionIsolatedRunnerConflictError,),
            lambda: runner_gate.dynamic_validation_subject(
                run_id=run["run_id"],
                user_id=runner_support.USER,
                workspace_id=runner_support.WORKSPACE,
                expected_artifact_revision=context.artifact[
                    "artifact_revision"
                ],
                expected_artifact_sha256=context.artifact["artifact_sha256"],
                expected_source_check_report_digest=source_report,
                expected_isolated_runner_report_digest="9" * 64,
            ),
            "runner report CAS mismatch blocks dynamic validation subject",
        )


def main() -> int:
    authorization_and_pure_get()
    success_replay_owner_cas_and_tamper()
    crash_and_prerequisite_drift_do_not_replay()
    exact_real_passed_subject_boundary()
    print("phase6 extension dynamic validation lifecycle smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
