#!/usr/bin/env python3
from __future__ import annotations

from datetime import timedelta
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.world_state import WorldStateStore  # noqa: E402
from interface.extension_isolated_runner import (  # noqa: E402
    ISOLATED_RUNNER_BACKEND_KIND,
    ISOLATED_RUNNER_BACKEND_STATUS_SCHEMA_VERSION,
    ISOLATED_RUNNER_HARNESS_REVISION,
    ISOLATED_RUNNER_POLICY_DIGEST,
    ISOLATED_RUNNER_POLICY_REVISION,
    ISOLATED_RUNNER_REPORT_SCHEMA_VERSION,
    IsolatedRunnerAuthority,
    IsolatedRunnerBackendStatus,
    IsolatedRunnerBinding,
    IsolatedRunnerReport,
)
from runtime.extension_artifact_quarantine import (  # noqa: E402
    ExtensionArtifactQuarantine,
)
from runtime.extension_isolated_runner_gate import (  # noqa: E402
    STATE_FILE as RUNNER_STATE_FILE,
    ExtensionIsolatedRunnerConflictError,
    ExtensionIsolatedRunnerGate,
    ExtensionIsolatedRunnerNotFoundError,
    ExtensionIsolatedRunnerStorageError,
    ExtensionIsolatedRunnerUnauthorizedError,
    ExtensionIsolatedRunnerUnavailableError,
)
from runtime.extension_source_policy_gate import (  # noqa: E402
    STATE_FILE as SOURCE_CHECK_STATE_FILE,
    ExtensionSourceCheckConflictError,
    ExtensionSourceCheckNotFoundError,
    ExtensionSourceCheckStorageError,
    ExtensionSourcePolicyGate,
)
from runtime.extension_spec_quarantine import (  # noqa: E402
    ExtensionSpecQuarantine,
)
from scripts.phase6_extension_source_gate_lifecycle_smoke import (  # noqa: E402
    BASE_TIME,
    USER,
    WORKSPACE,
    SourceGateContext,
    prepare_context,
    start_check,
)


CONTROL_TOKEN = "phase6-isolated-runner-test-token"


def expect(condition: bool, label: str, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"PASS {label}")


def expect_raises(
    errors: tuple[type[BaseException], ...],
    call: Callable[[], Any],
    label: str,
) -> BaseException:
    try:
        call()
    except errors as exc:
        print(f"PASS {label}")
        return exc
    raise AssertionError(f"{label}: call did not fail closed")


def available_status() -> IsolatedRunnerBackendStatus:
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


def unavailable_status() -> IsolatedRunnerBackendStatus:
    return IsolatedRunnerBackendStatus(
        schema_version=ISOLATED_RUNNER_BACKEND_STATUS_SCHEMA_VERSION,
        backend_kind=ISOLATED_RUNNER_BACKEND_KIND,
        availability="unavailable",
        reason_code="engine_unavailable",
        runner_policy_revision=ISOLATED_RUNNER_POLICY_REVISION,
        runner_policy_digest=ISOLATED_RUNNER_POLICY_DIGEST,
        harness_revision=ISOLATED_RUNNER_HARNESS_REVISION,
        harness_digest="1" * 64,
        image_id=None,
        image_conformance_digest=None,
        engine_identity_digest=None,
        conformance_certified=False,
        authority=IsolatedRunnerAuthority(),
    )


def passing_report(binding: IsolatedRunnerBinding) -> IsolatedRunnerReport:
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
        completed_at="2026-08-02T08:00:00Z",
        authority=IsolatedRunnerAuthority(),
    )


class CertifiedFakeBackend:
    def __init__(
        self,
        *,
        status_value: IsolatedRunnerBackendStatus | None = None,
        before_return: Callable[[bytes, IsolatedRunnerBinding], None]
        | None = None,
        crash: BaseException | None = None,
    ) -> None:
        self.status_value = status_value or available_status()
        self.before_return = before_return
        self.crash = crash
        self.calls = 0
        self.status_calls = 0
        self.artifact_digests: list[str] = []
        self.bindings: list[IsolatedRunnerBinding] = []

    def status(self) -> IsolatedRunnerBackendStatus:
        self.status_calls += 1
        return self.status_value

    def run(
        self,
        *,
        artifact_bytes: bytes,
        binding: IsolatedRunnerBinding,
    ) -> IsolatedRunnerReport:
        import hashlib

        self.calls += 1
        self.artifact_digests.append(hashlib.sha256(artifact_bytes).hexdigest())
        self.bindings.append(binding)
        if self.before_return is not None:
            self.before_return(artifact_bytes, binding)
        if self.crash is not None:
            raise self.crash
        return passing_report(binding)


class CountingSourceGate:
    def __init__(self, delegate: ExtensionSourcePolicyGate) -> None:
        self.delegate = delegate
        self.calls = 0

    def isolated_runner_subject(self, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        return self.delegate.isolated_runner_subject(**kwargs)


def passed_context(
    root: Path,
    *,
    extension_id: str,
    operation_prefix: str,
    expires_in_seconds: int | None = None,
) -> tuple[SourceGateContext, dict[str, Any], str]:
    context = prepare_context(
        root,
        extension_id=extension_id,
        operation_prefix=operation_prefix,
        artifact_expires_at=(
            BASE_TIME + timedelta(seconds=expires_in_seconds)
            if expires_in_seconds is not None
            else None
        ),
    )
    checked = start_check(context, f"{operation_prefix}-source-check")
    expect(
        checked["stored_stage"] == "SOURCE_CHECK_PASSED"
        and checked["source_check_status"] == "passed",
        f"{operation_prefix} establishes a real passed source check",
        checked,
    )
    private = context.store.read_json(SOURCE_CHECK_STATE_FILE)
    report_digest = private["checks"][checked["check_id"]]["report_digest"]
    return context, checked, report_digest


def build_gate(
    context: SourceGateContext,
    backend: CertifiedFakeBackend,
    *,
    enabled: bool = True,
    token: str = CONTROL_TOKEN,
) -> ExtensionIsolatedRunnerGate:
    counting_source = CountingSourceGate(context.source_gate)
    gate = ExtensionIsolatedRunnerGate(
        state_store=context.store,
        source_check_gate=counting_source,  # type: ignore[arg-type]
        backend=backend,  # type: ignore[arg-type]
        enabled=enabled,
        control_token=token,
        now=context.clock,
    )
    context.source_gate.bind_isolated_runner_projection(
        gate.projection_for_source_check
    )
    return gate


def start_run(
    gate: ExtensionIsolatedRunnerGate,
    context: SourceGateContext,
    checked: dict[str, Any],
    report_digest: str,
    operation_id: str,
    **overrides: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "check_id": checked["check_id"],
        "user_id": USER,
        "workspace_id": WORKSPACE,
        "expected_artifact_revision": context.artifact[
            "artifact_revision"
        ],
        "expected_artifact_sha256": context.artifact["artifact_sha256"],
        "expected_source_check_report_digest": report_digest,
        "operation_id": operation_id,
        "control_token": CONTROL_TOKEN,
    }
    payload.update(overrides)
    return gate.start(**payload)


def runner_state_bytes(context: SourceGateContext) -> bytes:
    return context.store.path_for(RUNNER_STATE_FILE).read_bytes()


def persisted_tree_bytes(context: SourceGateContext) -> dict[str, bytes]:
    return {
        str(path.relative_to(context.root)): path.read_bytes()
        for path in sorted(context.root.rglob("*"))
        if path.is_file()
    }


def source_calls(gate: ExtensionIsolatedRunnerGate) -> int:
    return int(getattr(gate.source_check_gate, "calls"))


def assert_zero_future_authority(value: dict[str, Any], label: str) -> None:
    expect(
        value["isolated_test_execution_status"] == "not_started"
        and value["candidate_execution_status"] == "not_started"
        and value["unit_checks_status"] == "not_started"
        and value["contract_checks_status"] == "not_started"
        and value["security_runtime_checks_status"] == "not_started"
        and value["fuzz_checks_status"] == "not_started"
        and value["behavior_verification_status"] == "not_started"
        and value["signature_status"] == "not_implemented"
        and value["activation_status"] == "not_installed"
        and value["capability_registry_visible"] is False
        and value["promotion_authorized"] is False
        and value["policy_effect"] == "none"
        and isinstance(value["authority"], dict)
        and not any(value["authority"].values()),
        label,
        value,
    )


def assert_public_redacted(
    value: dict[str, Any],
    *,
    source: bytes,
    label: str,
) -> None:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True)
    private_fields = {
        "owner_scope_digest",
        "binding",
        "report",
        "report_digest",
        "source",
        "source_bytes",
        "user_id",
        "workspace_id",
        "path",
        "host_path",
        "command",
        "argv",
        "environment",
        "env",
        "stdout",
        "stderr",
        "logs",
    }
    expect(
        not private_fields.intersection(value)
        and source.decode("utf-8") not in serialized
        and USER not in serialized
        and WORKSPACE not in serialized,
        label,
        value,
    )


def authorization_and_unavailable() -> None:
    with TemporaryDirectory(prefix="veyra-phase6-runner-auth-") as raw:
        context, checked, digest = passed_context(
            Path(raw),
            extension_id="example.runner_auth",
            operation_prefix="runner-auth",
        )
        before = runner_state_bytes(context)
        disabled_backend = CertifiedFakeBackend()
        disabled = build_gate(context, disabled_backend, enabled=False)
        expect_raises(
            (ExtensionIsolatedRunnerUnavailableError,),
            lambda: start_run(
                disabled,
                context,
                checked,
                digest,
                "runner-disabled-start",
            ),
            "disabled policy rejects before runner state access",
        )
        bad_token_backend = CertifiedFakeBackend()
        bad_token = build_gate(context, bad_token_backend)
        expect_raises(
            (ExtensionIsolatedRunnerUnauthorizedError,),
            lambda: start_run(
                bad_token,
                context,
                checked,
                digest,
                "runner-bad-token-start",
                control_token="wrong-token",
            ),
            "invalid token rejects before runner state access",
        )
        expect(
            runner_state_bytes(context) == before
            and disabled_backend.calls == 0
            and disabled_backend.status_calls == 0
            and bad_token_backend.calls == 0
            and bad_token_backend.status_calls == 0,
            "authorization failures neither mutate state nor dispatch backend",
        )

        unavailable_backend = CertifiedFakeBackend(
            status_value=unavailable_status()
        )
        unavailable = build_gate(context, unavailable_backend)
        before_unavailable = runner_state_bytes(context)
        expect_raises(
            (ExtensionIsolatedRunnerUnavailableError,),
            lambda: start_run(
                unavailable,
                context,
                checked,
                digest,
                "runner-unavailable-start",
            ),
            "uncertified backend fails closed",
        )
        expect(
            runner_state_bytes(context) != before_unavailable
            and unavailable_backend.calls == 0
            and unavailable_backend.status_calls == 1,
            "unavailable backend caches one sanitized observation without dispatch",
        )
        unavailable_state = context.store.read_json(RUNNER_STATE_FILE)
        expect(
            unavailable_state["runs"] == {}
            and unavailable_state["operation_index"] == {}
            and unavailable_state["backend_snapshot"]["backend_status"][
                "availability"
            ]
            == "unavailable",
            "unavailable backend snapshot grants no run admission",
            unavailable_state,
        )


def success_replay_owner_and_cas() -> None:
    with TemporaryDirectory(prefix="veyra-phase6-runner-success-") as raw:
        context, checked, digest = passed_context(
            Path(raw),
            extension_id="example.runner_success",
            operation_prefix="runner-success",
        )
        observed_stages: list[str] = []

        def observe_claim(
            _artifact: bytes,
            binding: IsolatedRunnerBinding,
        ) -> None:
            private = context.store.read_json(RUNNER_STATE_FILE)
            observed_stages.append(private["runs"][binding.run_id]["stage"])

        backend = CertifiedFakeBackend(before_return=observe_claim)
        gate = build_gate(context, backend)
        legacy_state = context.store.read_json(RUNNER_STATE_FILE)
        pure_status_before = persisted_tree_bytes(context)
        missing_status = gate.status()
        expect(
            "backend_snapshot" not in legacy_state
            and missing_status["status"] == "fail_closed"
            and missing_status["backend"]["availability"] == "unavailable"
            and missing_status["backend_snapshot"]["status"] == "missing"
            and backend.status_calls == 0
            and source_calls(gate) == 0
            and persisted_tree_bytes(context) == pure_status_before,
            "legacy v1 state remains readable and status is a pure missing snapshot",
            missing_status,
        )
        refreshed = gate.refresh_backend(control_token=CONTROL_TOKEN)
        expect(
            refreshed["status"]
            == "technical_complete_isolated_runner_only"
            and refreshed["backend_snapshot"]["status"] == "fresh"
            and refreshed["admission"]["start_ready"] is True
            and backend.status_calls == 1
            and source_calls(gate) == 0,
            "token-auth backend refresh is the only readiness observation path",
            refreshed,
        )
        refreshed_tree = persisted_tree_bytes(context)
        context.clock.current = BASE_TIME + timedelta(seconds=301)
        stale_status = gate.status()
        expect(
            stale_status["status"] == "fail_closed"
            and stale_status["backend_snapshot"]["status"] == "stale"
            and stale_status["backend_snapshot"]["reason_code"] == "expired"
            and stale_status["admission"]["start_ready"] is False
            and backend.status_calls == 1
            and source_calls(gate) == 0
            and persisted_tree_bytes(context) == refreshed_tree,
            "expired cached readiness is reported stale without active refresh",
            stale_status,
        )
        context.clock.current = BASE_TIME
        result = start_run(
            gate,
            context,
            checked,
            digest,
            "runner-success-start",
        )
        expect(
            result["stored_stage"] == "RUNNER_JOB_PASSED"
            and result["effective_status"] == "RUNNER_JOB_PASSED"
            and result["probe_status"] == "passed"
            and result["trusted_isolated_runner_status"] == "passed"
            and observed_stages == ["RUNNER_JOB_STARTED"]
            and backend.calls == 1
            and backend.status_calls == 2
            and backend.artifact_digests == [context.artifact["artifact_sha256"]],
            "durable claim and STARTED state precede one backend dispatch",
            {"result": result, "observed_stages": observed_stages},
        )
        assert_zero_future_authority(
            result,
            "passed isolation probe grants no candidate or production authority",
        )
        source_projection = context.source_gate.get(
            check_id=checked["check_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
        )
        expect(
            source_projection["trusted_isolated_runner_status"] == "passed"
            and source_projection["isolated_test_execution_status"]
            == "not_started",
            "source projection separates runner proof from unstarted tests",
            source_projection,
        )
        assert_public_redacted(
            result,
            source=context.source,
            label="start result omits source and private owner/runtime data",
        )

        state_before_replay = context.store.read_json(RUNNER_STATE_FILE)
        replay = start_run(
            gate,
            context,
            checked,
            digest,
            "runner-success-start",
        )
        expect(
            replay["run_id"] == result["run_id"]
            and replay["operation_replayed"] is True
            and backend.calls == 1
            and backend.status_calls == 3,
            "exact operation replay refreshes admission but never reruns",
            replay,
        )
        state_after_replay = context.store.read_json(RUNNER_STATE_FILE)
        expect(
            state_after_replay["runs"] == state_before_replay["runs"]
            and state_after_replay["binding_index"]
            == state_before_replay["binding_index"]
            and state_after_replay["operation_index"]
            == state_before_replay["operation_index"],
            "operation replay changes no durable run or idempotency evidence",
        )
        expect_raises(
            (ExtensionIsolatedRunnerConflictError,),
            lambda: start_run(
                gate,
                context,
                checked,
                digest,
                "runner-success-start",
                expected_artifact_sha256="f" * 64,
            ),
            "operation identity cannot be rebound",
        )

        state_before_failures = runner_state_bytes(context)
        conflict_errors = (
            ExtensionIsolatedRunnerConflictError,
            ExtensionIsolatedRunnerNotFoundError,
            ExtensionIsolatedRunnerStorageError,
            ExtensionSourceCheckConflictError,
            ExtensionSourceCheckNotFoundError,
            ExtensionSourceCheckStorageError,
        )
        failure_calls = (
            lambda: start_run(
                gate,
                context,
                checked,
                digest,
                "runner-wrong-owner",
                user_id="another-user",
            ),
            lambda: start_run(
                gate,
                context,
                checked,
                digest,
                "runner-wrong-workspace",
                workspace_id="/private/veyra/another-workspace",
            ),
            lambda: start_run(
                gate,
                context,
                checked,
                digest,
                "runner-stale-revision",
                expected_artifact_revision=context.artifact[
                    "artifact_revision"
                ]
                + 1,
            ),
            lambda: start_run(
                gate,
                context,
                checked,
                digest,
                "runner-wrong-artifact-digest",
                expected_artifact_sha256="e" * 64,
            ),
            lambda: start_run(
                gate,
                context,
                checked,
                digest,
                "runner-wrong-report-digest",
                expected_source_check_report_digest="d" * 64,
            ),
        )
        for index, call in enumerate(failure_calls):
            expect_raises(
                conflict_errors,
                call,
                f"owner/workspace/CAS/report mismatch {index + 1} fails closed",
            )
        expect(
            runner_state_bytes(context) == state_before_failures
            and backend.calls == 1,
            "owner and CAS failures cannot mutate or dispatch",
        )

        get_tree_before = persisted_tree_bytes(context)
        backend_status_before_get = backend.status_calls
        backend_runs_before_get = backend.calls
        source_before_get = source_calls(gate)
        cached_status = gate.status()
        detail = gate.get(
            run_id=result["run_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
        )
        listed = gate.list(user_id=USER, workspace_id=WORKSPACE)
        integrity = gate.integrity(
            run_id=result["run_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
        )
        projection = gate.projection_for_source_check(
            check_id=checked["check_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
        )
        expect(
            cached_status["backend_snapshot"]["status"] == "fresh"
            and listed["count"] == 1
            and listed["runs"][0]["run_id"] == result["run_id"]
            and detail["run_id"] == result["run_id"]
            and integrity["status"] == "isolated_runner_integrity_passed"
            and integrity["prerequisite_verification_status"]
            == "not_refreshed"
            and integrity["state_mutated"] is False
            and projection["trusted_isolated_runner_status"] == "passed"
            and backend.status_calls == backend_status_before_get
            and backend.calls == backend_runs_before_get
            and source_calls(gate) == source_before_get
            and persisted_tree_bytes(context) == get_tree_before,
            "all status/get/list/integrity/projection reads preserve bytes and call nothing external",
        )
        for label, value in {
            "detail": detail,
            "list record": listed["runs"][0],
            "integrity": integrity,
        }.items():
            assert_public_redacted(
                value,
                source=context.source,
                label=f"{label} is source- and owner-free",
            )

        verify_tree_before = persisted_tree_bytes(context)
        verified = gate.verify_prerequisite(
            run_id=result["run_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
            control_token=CONTROL_TOKEN,
        )
        expect(
            verified["status"] == "isolated_runner_prerequisite_verified"
            and verified["prerequisite_verification_status"] == "verified"
            and source_calls(gate) == source_before_get + 1
            and backend.status_calls == backend_status_before_get
            and backend.calls == backend_runs_before_get
            and persisted_tree_bytes(context) == verify_tree_before,
            "token-auth prerequisite verify alone re-opens the artifact without mutation",
            verified,
        )

        context.artifact_runtime.revoke(
            artifact_id=context.artifact["artifact_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
            expected_revision=context.artifact["artifact_revision"],
            operation_id="runner-success-post-read-revoke",
            reason="Prove cached GET and active prerequisite verification differ.",
        )
        revoked_tree = persisted_tree_bytes(context)
        revoked_source_before = source_calls(gate)
        cached_after_revoke = gate.get(
            run_id=result["run_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
        )
        blocked = gate.verify_prerequisite(
            run_id=result["run_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
            control_token=CONTROL_TOKEN,
        )
        expect(
            cached_after_revoke["effective_status"] == "RUNNER_JOB_PASSED"
            and blocked["effective_status"] == "BLOCKED_PREREQUISITE"
            and blocked["prerequisite_verification_status"] == "blocked"
            and source_calls(gate) == revoked_source_before + 1
            and persisted_tree_bytes(context) == revoked_tree,
            "cached GET stays historical while explicit verify detects revocation",
            {"cached": cached_after_revoke, "verified": blocked},
        )


def prerequisite_revoke_expiry_and_tamper() -> None:
    with TemporaryDirectory(prefix="veyra-phase6-runner-prereq-") as raw:
        root = Path(raw)
        cases: list[
            tuple[str, SourceGateContext, dict[str, Any], str, Callable[[], None]]
        ] = []

        revoked, revoked_check, revoked_digest = passed_context(
            root / "revoked",
            extension_id="example.runner_revoked",
            operation_prefix="runner-revoked",
        )
        cases.append(
            (
                "revoked artifact",
                revoked,
                revoked_check,
                revoked_digest,
                lambda: revoked.artifact_runtime.revoke(
                    artifact_id=revoked.artifact["artifact_id"],
                    user_id=USER,
                    workspace_id=WORKSPACE,
                    expected_revision=revoked.artifact["artifact_revision"],
                    operation_id="runner-revoked-artifact-control",
                    reason="Invalidate the isolated-run prerequisite.",
                ),
            )
        )

        expired, expired_check, expired_digest = passed_context(
            root / "expired",
            extension_id="example.runner_expired",
            operation_prefix="runner-expired",
            expires_in_seconds=1,
        )
        cases.append(
            (
                "expired artifact",
                expired,
                expired_check,
                expired_digest,
                lambda: setattr(
                    expired.clock,
                    "current",
                    BASE_TIME + timedelta(seconds=1),
                ),
            )
        )

        tampered, tampered_check, tampered_digest = passed_context(
            root / "tampered",
            extension_id="example.runner_tampered",
            operation_prefix="runner-tampered",
        )

        def tamper_report() -> None:
            def mutate(state: dict[str, Any]) -> None:
                state["checks"][tampered_check["check_id"]][
                    "report_digest"
                ] = "f" * 64

            tampered.store.mutate_json(SOURCE_CHECK_STATE_FILE, mutate)

        cases.append(
            (
                "tampered source-check report",
                tampered,
                tampered_check,
                tampered_digest,
                tamper_report,
            )
        )

        errors = (
            ExtensionIsolatedRunnerConflictError,
            ExtensionIsolatedRunnerStorageError,
            ExtensionSourceCheckConflictError,
            ExtensionSourceCheckStorageError,
        )
        for label, context, checked, digest, invalidate in cases:
            backend = CertifiedFakeBackend()
            gate = build_gate(context, backend)
            invalidate()
            before = runner_state_bytes(context)
            expect_raises(
                errors,
                lambda gate=gate,
                context=context,
                checked=checked,
                digest=digest,
                label=label: start_run(
                    gate,
                    context,
                    checked,
                    digest,
                    "runner-prerequisite-" + label.replace(" ", "-"),
                ),
                f"{label} blocks isolated-run admission",
            )
            expect(
                runner_state_bytes(context) == before and backend.calls == 0,
                f"{label} causes zero runner mutation and dispatch",
            )


def crash_restart_and_final_drift() -> None:
    with TemporaryDirectory(prefix="veyra-phase6-runner-crash-") as raw:
        root = Path(raw)
        context, checked, digest = passed_context(
            root / "crash",
            extension_id="example.runner_crash",
            operation_prefix="runner-crash",
        )
        crashing = CertifiedFakeBackend(
            crash=KeyboardInterrupt("simulated runner process interruption")
        )
        gate = build_gate(context, crashing)
        expect_raises(
            (KeyboardInterrupt,),
            lambda: start_run(
                gate,
                context,
                checked,
                digest,
                "runner-crash-start",
            ),
            "BaseException interruption escapes without fabricated result",
        )
        private = context.store.read_json(RUNNER_STATE_FILE)
        run_id = next(iter(private["runs"]))
        expect(
            private["runs"][run_id]["stage"] == "RUNNER_JOB_STARTED"
            and private["runs"][run_id]["report"] is None
            and crashing.calls == 1,
            "interruption leaves durable STARTED evidence only",
            private["runs"][run_id],
        )

        restarted_store = WorldStateStore(context.root)
        restarted_spec = ExtensionSpecQuarantine(
            state_store=restarted_store,
            now=context.clock,
        )
        restarted_artifact = ExtensionArtifactQuarantine(
            state_store=restarted_store,
            spec_quarantine=restarted_spec,
            now=context.clock,
        )
        restarted_source = ExtensionSourcePolicyGate(
            state_store=restarted_store,
            artifact_quarantine=restarted_artifact,
            now=context.clock,
        )
        restarted_backend = CertifiedFakeBackend()
        restarted = ExtensionIsolatedRunnerGate(
            state_store=restarted_store,
            source_check_gate=restarted_source,
            backend=restarted_backend,  # type: ignore[arg-type]
            enabled=True,
            control_token=CONTROL_TOKEN,
            now=context.clock,
        )
        replay = restarted.start(
            check_id=checked["check_id"],
            user_id=USER,
            workspace_id=WORKSPACE,
            expected_artifact_revision=context.artifact[
                "artifact_revision"
            ],
            expected_artifact_sha256=context.artifact["artifact_sha256"],
            expected_source_check_report_digest=digest,
            operation_id="runner-crash-start",
            control_token=CONTROL_TOKEN,
        )
        expect(
            replay["stored_stage"] == "RUNNER_JOB_STARTED"
            and replay["effective_status"] == "RUNNER_JOB_INDETERMINATE"
            and replay["probe_status"] == "indeterminate"
            and replay["operation_replayed"] is True
            and restarted_backend.calls == 0
            and restarted_backend.status_calls == 1,
            "restart refreshes admission but preserves STARTED without auto-rerun",
            replay,
        )
        assert_zero_future_authority(
            replay,
            "indeterminate restart grants no future-stage authority",
        )

        drift, drift_check, drift_digest = passed_context(
            root / "final-drift",
            extension_id="example.runner_final_drift",
            operation_prefix="runner-final-drift",
        )

        def revoke_during_run(
            _artifact: bytes,
            _binding: IsolatedRunnerBinding,
        ) -> None:
            drift.artifact_runtime.revoke(
                artifact_id=drift.artifact["artifact_id"],
                user_id=USER,
                workspace_id=WORKSPACE,
                expected_revision=drift.artifact["artifact_revision"],
                operation_id="runner-final-drift-revoke",
                reason="Change prerequisite before result commit.",
            )

        drift_backend = CertifiedFakeBackend(before_return=revoke_during_run)
        drift_gate = build_gate(drift, drift_backend)
        drift_result = start_run(
            drift_gate,
            drift,
            drift_check,
            drift_digest,
            "runner-final-drift-start",
        )
        expect(
            drift_backend.calls == 1
            and drift_result["stored_stage"] == "RUNNER_JOB_INDETERMINATE"
            and drift_result["effective_status"]
            in {
                "RUNNER_JOB_INDETERMINATE",
                "BLOCKED_PREREQUISITE",
                "PREREQUISITE_UNAVAILABLE",
            }
            and drift_result["probe_status"] == "indeterminate"
            and drift_result["trusted_isolated_runner_status"]
            == "indeterminate",
            "final prerequisite drift can never persist PASSED evidence",
            drift_result,
        )
        assert_zero_future_authority(
            drift_result,
            "prerequisite drift grants no future-stage authority",
        )


def main() -> int:
    authorization_and_unavailable()
    success_replay_owner_and_cas()
    prerequisite_revoke_expiry_and_tamper()
    crash_restart_and_final_drift()
    print("phase6 extension isolated-runner lifecycle smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
