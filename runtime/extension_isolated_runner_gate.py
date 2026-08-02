from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
import re
import unicodedata
from typing import Any, Callable

from core.world_state import WorldStateStore
from interface.extension_artifact import artifact_owner_scope_digest
from interface.extension_isolated_runner import (
    ISOLATED_RUNNER_BACKEND_KIND,
    ISOLATED_RUNNER_BACKEND_SNAPSHOT_SCHEMA_VERSION,
    ISOLATED_RUNNER_BACKEND_SNAPSHOT_TTL_SECONDS,
    ISOLATED_RUNNER_BINDING_SCHEMA_VERSION,
    ISOLATED_RUNNER_CPU_MILLIS,
    ISOLATED_RUNNER_HARNESS_REVISION,
    ISOLATED_RUNNER_MEMORY_BYTES,
    ISOLATED_RUNNER_NOFILE_LIMIT,
    ISOLATED_RUNNER_PIDS_LIMIT,
    ISOLATED_RUNNER_POLICY_DIGEST,
    ISOLATED_RUNNER_POLICY_REVISION,
    ISOLATED_RUNNER_TMPFS_BYTES,
    ISOLATED_RUNNER_WALL_TIMEOUT_SECONDS,
    MAX_ISOLATED_RUNNER_STDOUT_BYTES,
    IsolatedRunnerAuthority,
    IsolatedRunnerBackendSnapshot,
    IsolatedRunnerBackendStatus,
    IsolatedRunnerBinding,
    IsolatedRunnerReport,
    parse_isolated_runner_backend_snapshot,
    parse_isolated_runner_backend_status,
    parse_isolated_runner_binding,
    parse_isolated_runner_report,
)
from runtime.extension_artifact_quarantine import (
    ExtensionArtifactConflictError,
    ExtensionArtifactNotFoundError,
    ExtensionArtifactStorageError,
)
from runtime.extension_source_policy_gate import (
    ExtensionSourceCheckConflictError,
    ExtensionSourceCheckError,
    ExtensionSourceCheckNotFoundError,
    ExtensionSourceCheckStorageError,
    ExtensionSourcePolicyGate,
)
from runtime.trusted_isolated_runner import (
    TrustedIsolatedRunnerBackend,
    TrustedIsolatedRunnerBindingError,
    TrustedIsolatedRunnerError,
    TrustedIsolatedRunnerUnavailableError,
)


STATE_FILE = "phase6_extension_isolated_runner_state.json"
STATE_SCHEMA_VERSION = (
    "veyra.phase6.extension_isolated_runner_state.v1"
)
RECORD_SCHEMA_VERSION = (
    "veyra.phase6.extension_isolated_runner_private_record.v1"
)
PUBLIC_STATUS_SCHEMA = (
    "veyra.phase6.extension_isolated_runner_status.v1"
)
PUBLIC_RUN_SCHEMA = (
    "veyra.phase6.extension_isolated_runner_record.v1"
)
PUBLIC_LIST_SCHEMA = (
    "veyra.phase6.extension_isolated_runner_list.v1"
)
PUBLIC_INTEGRITY_SCHEMA = (
    "veyra.phase6.extension_isolated_runner_integrity.v1"
)
RUNNER_ENABLE_ENV = "VEYRA_PHASE6_TRUSTED_RUNNER_ENABLED"
RUNNER_TOKEN_ENV = "VEYRA_LOCAL_API_TOKEN"
MAX_RUNS = 200
MAX_OPERATIONS = 2_000

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,239}$")
_RUN_ID = re.compile(r"^extrun_[0-9a-f]{24}$")
_CHECK_ID = re.compile(r"^extcheck_[0-9a-f]{24}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_STAGES = frozenset(
    {
        "RUNNER_JOB_CLAIMED",
        "RUNNER_JOB_STARTED",
        "RUNNER_JOB_PASSED",
        "RUNNER_JOB_FAILED",
        "RUNNER_JOB_INDETERMINATE",
    }
)
_TERMINAL_STAGES = frozenset(
    {
        "RUNNER_JOB_PASSED",
        "RUNNER_JOB_FAILED",
        "RUNNER_JOB_INDETERMINATE",
    }
)
_FAILURE_CODES = frozenset(
    {
        "backend_unavailable",
        "backend_report_invalid",
        "prerequisite_changed",
    }
)
_STATE_FIELDS = frozenset(
    {
        "schema_version",
        "runs",
        "binding_index",
        "operation_index",
        "run_count",
        "updated_at",
    }
)
_STATE_METADATA_FIELDS = frozenset(
    {
        "_state_revision",
        "source",
        "confidence",
        "ttl_seconds",
        "status",
    }
)
_STATE_OPTIONAL_FIELDS = frozenset({"backend_snapshot"})
_RECORD_FIELDS = frozenset(
    {
        "schema_version",
        "run_id",
        "owner_scope_digest",
        "binding",
        "binding_digest",
        "stage",
        "report",
        "report_digest",
        "failure_code",
        "created_at",
        "started_at",
        "updated_at",
    }
)
_OPERATION_FIELDS = frozenset(
    {
        "kind",
        "request_digest",
        "run_id",
        "recorded_at",
    }
)


class ExtensionIsolatedRunnerError(RuntimeError):
    """Base error for the private Phase 6.2d isolation gate."""


class ExtensionIsolatedRunnerConflictError(ExtensionIsolatedRunnerError):
    """A CAS, replay, lifecycle, or trust binding conflicted."""


class ExtensionIsolatedRunnerNotFoundError(ExtensionIsolatedRunnerError):
    """A run was absent or outside the logical owner scope."""


class ExtensionIsolatedRunnerStorageError(ExtensionIsolatedRunnerError):
    """Private runner state or prerequisite evidence is unavailable."""


class ExtensionIsolatedRunnerUnavailableError(ExtensionIsolatedRunnerError):
    """The explicitly enabled and certified runner is unavailable."""


class ExtensionIsolatedRunnerUnauthorizedError(ExtensionIsolatedRunnerError):
    """A runner start omitted or supplied an invalid control token."""


class ExtensionIsolatedRunnerGate:
    """Durable admission and evidence gate for one fixed isolation probe.

    The gate accepts only an exact, current ``SOURCE_CHECK_PASSED`` record.
    HTTP never supplies source, paths, commands, environment, mounts, image,
    harness, tests, or policy.  A passed run proves only the fixed isolation
    boundary; candidate execution, behavior verification, signing,
    installation, activation, canary and promotion remain unavailable.
    """

    def __init__(
        self,
        *,
        state_store: WorldStateStore,
        source_check_gate: ExtensionSourcePolicyGate,
        backend: TrustedIsolatedRunnerBackend,
        enabled: bool | None = None,
        control_token: str | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.state_store = state_store
        self.source_check_gate = source_check_gate
        self.backend = backend
        self.enabled = (
            self._env_enabled(os.environ.get(RUNNER_ENABLE_ENV, ""))
            if enabled is None
            else bool(enabled)
        )
        self.control_token = str(
            os.environ.get(RUNNER_TOKEN_ENV, "")
            if control_token is None
            else control_token
        ).strip()
        self._now = now or (lambda: datetime.now(timezone.utc))

    def status(self) -> dict[str, Any]:
        state: dict[str, Any] | None = None
        try:
            state = self._read_valid_state()
            counts = {stage: 0 for stage in sorted(_STAGES)}
            for record in state["runs"].values():
                counts[str(record["stage"])] += 1
            storage = {
                "status": "ready",
                "issue": None,
                "run_count": len(state["runs"]),
                "operation_count": len(state["operation_index"]),
                "counts": counts,
            }
            storage_ready = True
        except Exception as exc:
            storage = {
                "status": "fault",
                "issue": self._safe_issue(exc),
                "run_count": 0,
                "operation_count": 0,
                "counts": {},
            }
            storage_ready = False
        backend, backend_snapshot, backend_ready = (
            self._cached_backend_snapshot(state)
        )
        token_configured = bool(self.control_token)
        start_ready = bool(
            storage_ready
            and backend_ready
            and self.enabled
            and token_configured
        )
        if start_ready:
            health = "available"
        elif not self.enabled:
            health = "disabled_by_policy"
        else:
            health = "degraded"
        return {
            "schema_version": PUBLIC_STATUS_SCHEMA,
            "phase": "6.2d",
            "status": (
                "technical_complete_isolated_runner_only"
                if storage_ready and backend_ready
                else "fail_closed"
            ),
            "operational_health": health,
            "completion_scope": (
                "fixed trusted isolation probe over one exact passed artifact only"
            ),
            "backend": backend.canonical_dict(),
            "backend_snapshot": backend_snapshot,
            "storage": storage,
            "admission": {
                "enabled": self.enabled,
                "token_configured": token_configured,
                "start_ready": start_ready,
                "loopback_bypass_allowed": False,
            },
            "isolation_contract": {
                "network": "none",
                "rootfs": "read_only",
                "candidate_mount": "private_vm_volume_read_only",
                "harness_mount": "private_vm_volume_read_only",
                "host_workspace": False,
                "host_state": False,
                "host_secrets": False,
                "host_bind_mounts": False,
                "fixed_harness": True,
                "candidate_executed": False,
                "limits": {
                    "pids": ISOLATED_RUNNER_PIDS_LIMIT,
                    "memory_bytes": ISOLATED_RUNNER_MEMORY_BYTES,
                    "cpu_millis": ISOLATED_RUNNER_CPU_MILLIS,
                    "nofile": ISOLATED_RUNNER_NOFILE_LIMIT,
                    "tmpfs_bytes": ISOLATED_RUNNER_TMPFS_BYTES,
                    "wall_timeout_seconds": (
                        ISOLATED_RUNNER_WALL_TIMEOUT_SECONDS
                    ),
                    "stdout_bytes": MAX_ISOLATED_RUNNER_STDOUT_BYTES,
                },
            },
            "authority": self._authority(),
            "next_stage": {
                "isolated_generation": "not_started",
                "candidate_unit_checks": "not_started",
                "candidate_contract_checks": "not_started",
                "candidate_security_runtime_checks": "not_started",
                "candidate_fuzz_checks": "not_started",
                "behavior_verification": "not_started",
                "signature_verification": "not_started",
                "installation": "not_started",
                "activation": "not_started",
                "read_only_execution_canary": "not_started",
                "scoped_execution_canary": "not_started",
                "promotion": "not_started",
            },
        }

    def refresh_backend(self, *, control_token: str) -> dict[str, Any]:
        """Explicitly observe and cache sanitized backend readiness."""

        self._authorize_control(control_token)
        self._observe_backend_status()
        return self.status()

    def start(
        self,
        *,
        check_id: str,
        user_id: str,
        workspace_id: str,
        expected_artifact_revision: int,
        expected_artifact_sha256: str,
        expected_source_check_report_digest: str,
        operation_id: str,
        control_token: str,
    ) -> dict[str, Any]:
        self._authorize_start(control_token)
        selected_check = self._required_check_id(check_id)
        selected_revision = self._required_revision(
            expected_artifact_revision
        )
        selected_artifact_digest = self._required_digest(
            expected_artifact_sha256,
            "expected_artifact_sha256",
        )
        selected_report_digest = self._required_digest(
            expected_source_check_report_digest,
            "expected_source_check_report_digest",
        )
        selected_operation = self._required_id(operation_id, "operation_id")
        selected_user, selected_workspace, owner_scope, initial_state = (
            self._read_owner_state(user_id, workspace_id)
        )
        operation_key = self._operation_key(
            owner_scope,
            selected_operation,
        )
        request_digest = self._request_digest(
            owner_scope,
            selected_check,
            selected_revision,
            selected_artifact_digest,
            selected_report_digest,
        )
        replay = self._existing_operation(
            state=initial_state,
            operation_key=operation_key,
            request_digest=request_digest,
            owner_scope_digest=owner_scope,
        )
        if replay is not None:
            # A start command is an active control-plane operation even when
            # idempotently replayed: refresh backend readiness and re-open the
            # exact prerequisite before returning the durable record.
            self._required_backend_status()
            return self._public_run_with_prerequisite(
                replay,
                user_id=selected_user,
                workspace_id=selected_workspace,
                operation_replayed=True,
            )

        subject = self._runner_subject(
            check_id=selected_check,
            user_id=selected_user,
            workspace_id=selected_workspace,
            expected_artifact_revision=selected_revision,
            expected_artifact_sha256=selected_artifact_digest,
            expected_source_check_report_digest=selected_report_digest,
        )
        backend_status = self._required_backend_status()
        run_id = self._run_id(subject, backend_status)
        binding = self._binding_from_subject(
            subject,
            backend_status,
            run_id=run_id,
        )
        binding_digest = binding.binding_digest()
        claimed: dict[str, Any] | None = None
        operation_replayed = False
        run_existing = False

        def claim(state: dict[str, Any]) -> None:
            nonlocal claimed
            nonlocal operation_replayed
            nonlocal run_existing
            self._assert_current_workspace(selected_workspace)
            self._validate_state(state)
            existing_operation = state["operation_index"].get(operation_key)
            if existing_operation is not None:
                self._validate_operation(
                    operation_key,
                    existing_operation,
                    state,
                )
                if existing_operation["request_digest"] != request_digest:
                    raise ExtensionIsolatedRunnerConflictError(
                        "isolated-run operation identity was rebound"
                    )
                record = self._record_by_id(
                    state,
                    str(existing_operation["run_id"]),
                )
                self._require_owner(record, owner_scope)
                claimed = record
                operation_replayed = True
                return
            indexed = state["binding_index"].get(binding_digest)
            if indexed is not None:
                record = self._record_by_id(state, str(indexed))
                self._require_owner(record, owner_scope)
                if record["binding_digest"] != binding_digest:
                    raise ExtensionIsolatedRunnerStorageError(
                        "isolated-run binding index changed"
                    )
                self._record_operation(
                    state,
                    operation_key=operation_key,
                    request_digest=request_digest,
                    run_id=record["run_id"],
                )
                claimed = record
                run_existing = True
                return
            if (
                len(state["runs"]) >= MAX_RUNS
                or len(state["operation_index"]) >= MAX_OPERATIONS
            ):
                raise ExtensionIsolatedRunnerConflictError(
                    "isolated-run capacity is exhausted"
                )
            created_at = self._now_iso()
            record = {
                "schema_version": RECORD_SCHEMA_VERSION,
                "run_id": run_id,
                "owner_scope_digest": owner_scope,
                "binding": binding.canonical_dict(),
                "binding_digest": binding_digest,
                "stage": "RUNNER_JOB_CLAIMED",
                "report": None,
                "report_digest": None,
                "failure_code": None,
                "created_at": created_at,
                "started_at": None,
                "updated_at": created_at,
            }
            state["runs"][run_id] = record
            state["binding_index"][binding_digest] = run_id
            state["run_count"] = len(state["runs"])
            self._record_operation(
                state,
                operation_key=operation_key,
                request_digest=request_digest,
                run_id=run_id,
            )
            claimed = record

        self._mutate(claim)
        if claimed is None:
            raise ExtensionIsolatedRunnerStorageError(
                "isolated-run claim produced no record"
            )
        if operation_replayed or run_existing:
            return self._public_run_with_prerequisite(
                claimed,
                user_id=selected_user,
                workspace_id=selected_workspace,
                operation_replayed=operation_replayed,
                run_existing=run_existing,
            )

        def mark_started(state: dict[str, Any]) -> None:
            self._validate_state(state)
            self._assert_current_workspace(selected_workspace)
            record = self._record_by_id(state, run_id)
            self._require_owner(record, owner_scope)
            if (
                record["stage"] != "RUNNER_JOB_CLAIMED"
                or record["binding_digest"] != binding_digest
            ):
                raise ExtensionIsolatedRunnerConflictError(
                    "isolated-run claim changed before dispatch"
                )
            started_at = self._now_iso()
            record["stage"] = "RUNNER_JOB_STARTED"
            record["started_at"] = started_at
            record["updated_at"] = started_at

        self._mutate(mark_started)
        report: IsolatedRunnerReport | None = None
        failure_code: str | None = None
        try:
            report = parse_isolated_runner_report(
                self.backend.run(
                    artifact_bytes=subject["source_bytes"],
                    binding=binding,
                )
            )
            if (
                report.binding_digest != binding_digest
                or report.binding.canonical_dict()
                != binding.canonical_dict()
                or any(report.authority.model_dump().values())
            ):
                raise ExtensionIsolatedRunnerStorageError(
                    "isolated-run backend report binding is invalid"
                )
        except (
            TrustedIsolatedRunnerUnavailableError,
            TrustedIsolatedRunnerBindingError,
        ):
            failure_code = "backend_unavailable"
            report = None
        except Exception:
            failure_code = "backend_report_invalid"
            report = None

        try:
            final_subject = self._runner_subject(
                check_id=selected_check,
                user_id=selected_user,
                workspace_id=selected_workspace,
                expected_artifact_revision=selected_revision,
                expected_artifact_sha256=selected_artifact_digest,
                expected_source_check_report_digest=selected_report_digest,
            )
            final_binding = self._binding_from_subject(
                final_subject,
                backend_status,
                run_id=run_id,
            )
            if final_binding.binding_digest() != binding_digest:
                failure_code = "prerequisite_changed"
                report = None
        except Exception:
            failure_code = "prerequisite_changed"
            report = None

        finalized: dict[str, Any] | None = None

        def finish(state: dict[str, Any]) -> None:
            nonlocal finalized
            nonlocal failure_code
            nonlocal report
            self._validate_state(state)
            try:
                self._assert_current_workspace(selected_workspace)
            except ExtensionIsolatedRunnerConflictError:
                failure_code = "prerequisite_changed"
                report = None
            record = self._record_by_id(state, run_id)
            self._require_owner(record, owner_scope)
            if record["binding_digest"] != binding_digest:
                raise ExtensionIsolatedRunnerStorageError(
                    "isolated-run binding changed before completion"
                )
            if record["stage"] != "RUNNER_JOB_STARTED":
                finalized = record
                return
            if report is None:
                record["stage"] = "RUNNER_JOB_INDETERMINATE"
                record["report"] = None
                record["report_digest"] = None
                record["failure_code"] = (
                    failure_code or "backend_report_invalid"
                )
            else:
                record["stage"] = (
                    "RUNNER_JOB_PASSED"
                    if report.probe_status == "passed"
                    else "RUNNER_JOB_FAILED"
                )
                record["report"] = report.canonical_dict()
                record["report_digest"] = report.report_digest()
                record["failure_code"] = None
            record["updated_at"] = self._now_iso()
            finalized = record

        self._mutate(finish)
        if finalized is None:
            raise ExtensionIsolatedRunnerStorageError(
                "isolated-run completion produced no record"
            )
        self._audit(finalized)
        return self._public_run_with_prerequisite(
            finalized,
            user_id=selected_user,
            workspace_id=selected_workspace,
        )

    def get(
        self,
        *,
        run_id: str,
        user_id: str,
        workspace_id: str,
    ) -> dict[str, Any]:
        selected_user, selected_workspace, owner_scope, state = (
            self._read_owner_state(user_id, workspace_id)
        )
        record = self._record_by_id(
            state,
            self._required_run_id(run_id),
        )
        self._require_owner(record, owner_scope)
        return self._public_run_for_owner(
            record,
            user_id=selected_user,
            workspace_id=selected_workspace,
        )

    def list(
        self,
        *,
        user_id: str,
        workspace_id: str,
        limit: int = 50,
    ) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        selected_user, selected_workspace, owner_scope, state = (
            self._read_owner_state(user_id, workspace_id)
        )
        records = [
            record
            for record in state["runs"].values()
            if record["owner_scope_digest"] == owner_scope
        ]
        records.sort(
            key=lambda item: (
                str(item["updated_at"]),
                str(item["run_id"]),
            ),
            reverse=True,
        )
        selected = records[:limit]
        return {
            "schema_version": PUBLIC_LIST_SCHEMA,
            "count": len(selected),
            "runs": [
                self._public_run_for_owner(
                    record,
                    user_id=selected_user,
                    workspace_id=selected_workspace,
                )
                for record in selected
            ],
            "authority": self._authority(),
        }

    def integrity(
        self,
        *,
        run_id: str,
        user_id: str,
        workspace_id: str,
    ) -> dict[str, Any]:
        _selected_user, _selected_workspace, owner_scope, state = (
            self._read_owner_state(user_id, workspace_id)
        )
        record = self._record_by_id(
            state,
            self._required_run_id(run_id),
        )
        self._require_owner(record, owner_scope)
        public = self._public_run(record)
        return {
            **public,
            "schema_version": PUBLIC_INTEGRITY_SCHEMA,
            "status": "isolated_runner_integrity_passed",
            "report_integrity_status": (
                "validated" if record["report"] is not None else "unavailable"
            ),
            "prerequisite_verification_status": "not_refreshed",
            "active_verification_required": True,
            "state_mutated": False,
        }

    def dynamic_validation_subject(
        self,
        *,
        run_id: str,
        user_id: str,
        workspace_id: str,
        expected_artifact_revision: int,
        expected_artifact_sha256: str,
        expected_source_check_report_digest: str,
        expected_isolated_runner_report_digest: str,
    ) -> dict[str, Any]:
        """Re-open one exact passed run for the 6.2e in-process gate.

        This active prerequisite boundary is never called by a GET route.  It
        returns private source and Spec data only in process after revalidating
        owner scope, both terminal reports, artifact CAS and source-check
        binding.  It grants no HTTP, signing, install, registry, canary or
        promotion authority and does not alter 6.2d's non-executing semantics.
        """

        selected_run = self._required_run_id(run_id)
        selected_revision = self._required_revision(
            expected_artifact_revision
        )
        selected_artifact_digest = self._required_digest(
            expected_artifact_sha256,
            "expected_artifact_sha256",
        )
        selected_source_report = self._required_digest(
            expected_source_check_report_digest,
            "expected_source_check_report_digest",
        )
        selected_runner_report = self._required_digest(
            expected_isolated_runner_report_digest,
            "expected_isolated_runner_report_digest",
        )
        selected_user, selected_workspace, owner_scope, state = (
            self._read_owner_state(user_id, workspace_id)
        )
        record = self._record_by_id(state, selected_run)
        self._require_owner(record, owner_scope)
        if (
            record["stage"] != "RUNNER_JOB_PASSED"
            or record.get("report_digest") != selected_runner_report
            or not isinstance(record.get("report"), dict)
        ):
            raise ExtensionIsolatedRunnerConflictError(
                "dynamic validation requires the exact passed isolated run"
            )
        binding = parse_isolated_runner_binding(record["binding"])
        report = parse_isolated_runner_report(record["report"])
        if (
            binding.run_id != selected_run
            or binding.owner_scope_digest != owner_scope
            or binding.artifact_revision != selected_revision
            or binding.artifact_sha256 != selected_artifact_digest
            or binding.source_check_report_digest != selected_source_report
            or report.binding.canonical_dict() != binding.canonical_dict()
            or report.binding_digest != record["binding_digest"]
            or report.report_digest() != selected_runner_report
            or report.probe_status != "passed"
            or report.isolated_runner_status != "passed"
            or any(
                getattr(report, field) != "passed"
                for field in (
                    "artifact_identity_status",
                    "harness_identity_status",
                    "non_root_status",
                    "rootfs_read_only_status",
                    "input_read_only_status",
                    "network_isolation_status",
                    "secret_isolation_status",
                    "host_surface_isolation_status",
                    "resource_limits_status",
                )
            )
            or any(report.authority.model_dump(mode="python").values())
        ):
            raise ExtensionIsolatedRunnerConflictError(
                "dynamic validation isolated-run evidence changed"
            )
        source_subject = self._runner_subject(
            check_id=binding.check_id,
            user_id=selected_user,
            workspace_id=selected_workspace,
            expected_artifact_revision=selected_revision,
            expected_artifact_sha256=selected_artifact_digest,
            expected_source_check_report_digest=selected_source_report,
        )
        try:
            artifact_subject = (
                self.source_check_gate.artifact_quarantine.source_check_subject(
                    artifact_id=binding.artifact_id,
                    user_id=selected_user,
                    workspace_id=selected_workspace,
                    expected_artifact_revision=selected_revision,
                    expected_artifact_sha256=selected_artifact_digest,
                )
            )
        except Exception as exc:
            raise ExtensionIsolatedRunnerStorageError(
                "dynamic validation artifact contract is unavailable"
            ) from exc
        rebound = self._runner_subject(
            check_id=binding.check_id,
            user_id=selected_user,
            workspace_id=selected_workspace,
            expected_artifact_revision=selected_revision,
            expected_artifact_sha256=selected_artifact_digest,
            expected_source_check_report_digest=selected_source_report,
        )
        source_bytes = source_subject.get("source_bytes")
        spec = artifact_subject.get("source_check_spec")
        if (
            source_subject != rebound
            or not isinstance(source_bytes, bytes)
            or not isinstance(spec, dict)
            or artifact_subject.get("source_bytes") != source_bytes
            or artifact_subject.get("artifact_envelope_digest")
            != binding.artifact_envelope_digest
            or artifact_subject.get("owner_scope_digest") != owner_scope
            or artifact_subject.get("candidate_id") != binding.candidate_id
            or artifact_subject.get("candidate_revision")
            != binding.candidate_revision
            or artifact_subject.get("spec_digest") != binding.spec_digest
            or artifact_subject.get("extension_id") != binding.extension_id
            or artifact_subject.get("extension_version")
            != binding.extension_version
            or any(artifact_subject.get("authority", {}).values())
        ):
            raise ExtensionIsolatedRunnerConflictError(
                "dynamic validation source or Spec binding changed"
            )
        return {
            "schema_version": (
                "veyra.phase6.extension_dynamic_validation_subject.v1"
            ),
            "run_id": binding.run_id,
            "isolated_runner_binding_digest": record["binding_digest"],
            "isolated_runner_report_digest": selected_runner_report,
            "isolated_runner_stage": "RUNNER_JOB_PASSED",
            "source_check_id": binding.check_id,
            "source_check_binding_digest": (
                binding.source_check_binding_digest
            ),
            "source_check_report_digest": selected_source_report,
            "source_check_stage": "SOURCE_CHECK_PASSED",
            "candidate_id": binding.candidate_id,
            "candidate_revision": binding.candidate_revision,
            "artifact_id": binding.artifact_id,
            "artifact_revision": binding.artifact_revision,
            "artifact_envelope_digest": binding.artifact_envelope_digest,
            "artifact_sha256": binding.artifact_sha256,
            "artifact_size_bytes": binding.artifact_size_bytes,
            "owner_scope_digest": binding.owner_scope_digest,
            "extension_id": binding.extension_id,
            "extension_version": binding.extension_version,
            "spec_digest": binding.spec_digest,
            "parser_identity": binding.source_parser_identity,
            "ruleset_digest": binding.source_ruleset_digest,
            "source_check_spec": dict(spec),
            "source_bytes": source_bytes,
            "user_id": selected_user,
            "workspace_id": selected_workspace,
            "authority": self._authority(),
        }

    def verify_prerequisite(
        self,
        *,
        run_id: str,
        user_id: str,
        workspace_id: str,
        control_token: str,
    ) -> dict[str, Any]:
        """Actively re-open and hash one run's exact prerequisite."""

        self._authorize_control(control_token)
        selected_user, selected_workspace, owner_scope, state = (
            self._read_owner_state(user_id, workspace_id)
        )
        record = self._record_by_id(
            state,
            self._required_run_id(run_id),
        )
        self._require_owner(record, owner_scope)
        public = self._public_run(record)
        try:
            self._validate_prerequisite(
                record,
                user_id=selected_user,
                workspace_id=selected_workspace,
            )
        except (
            ExtensionSourceCheckConflictError,
            ExtensionSourceCheckNotFoundError,
            ExtensionIsolatedRunnerConflictError,
        ):
            return {
                **public,
                "schema_version": PUBLIC_INTEGRITY_SCHEMA,
                "effective_status": "BLOCKED_PREREQUISITE",
                "operational_health": "degraded",
                "status": "prerequisite_verification_blocked",
                "prerequisite_verification_status": "blocked",
                "active_verification_required": False,
                "state_mutated": False,
            }
        except Exception as exc:
            raise ExtensionIsolatedRunnerStorageError(
                "isolated-run prerequisite integrity is unavailable"
            ) from exc
        return {
            **public,
            "schema_version": PUBLIC_INTEGRITY_SCHEMA,
            "status": "isolated_runner_prerequisite_verified",
            "prerequisite_verification_status": "verified",
            "active_verification_required": False,
            "state_mutated": False,
        }

    def projection_for_source_check(
        self,
        *,
        check_id: str,
        user_id: str,
        workspace_id: str,
    ) -> dict[str, Any]:
        selected_user, selected_workspace, owner_scope, state = (
            self._read_owner_state(user_id, workspace_id)
        )
        selected_check = self._required_check_id(check_id)
        records = []
        for record in state["runs"].values():
            binding = parse_isolated_runner_binding(record["binding"])
            if (
                record["owner_scope_digest"] == owner_scope
                and binding.check_id == selected_check
            ):
                records.append(record)
        if not records:
            return self.empty_projection()
        records.sort(
            key=lambda item: (
                str(item["updated_at"]),
                str(item["run_id"]),
            ),
            reverse=True,
        )
        public = self._public_run_for_owner(
            records[0],
            user_id=selected_user,
            workspace_id=selected_workspace,
        )
        return {
            "trusted_isolated_runner_status": public[
                "trusted_isolated_runner_status"
            ],
            "isolated_test_execution_status": public[
                "isolated_test_execution_status"
            ],
        }

    @staticmethod
    def empty_projection() -> dict[str, str]:
        return {
            "trusted_isolated_runner_status": "not_started",
            "isolated_test_execution_status": "not_started",
        }

    def _runner_subject(self, **kwargs: Any) -> dict[str, Any]:
        try:
            subject = self.source_check_gate.isolated_runner_subject(**kwargs)
        except (
            ExtensionArtifactConflictError,
            ExtensionArtifactNotFoundError,
        ) as exc:
            raise ExtensionIsolatedRunnerConflictError(
                "isolated-run prerequisite changed"
            ) from exc
        except ExtensionArtifactStorageError as exc:
            raise ExtensionIsolatedRunnerStorageError(
                "isolated-run prerequisite is unavailable"
            ) from exc
        except ExtensionSourceCheckError:
            raise
        except Exception as exc:
            raise ExtensionIsolatedRunnerStorageError(
                "isolated-run prerequisite is unavailable"
            ) from exc
        if (
            not isinstance(subject, dict)
            or not isinstance(subject.get("source_bytes"), bytes)
            or not _DIGEST.fullmatch(
                str(subject.get("source_check_binding_digest") or "")
            )
            or not _DIGEST.fullmatch(
                str(subject.get("source_check_report_digest") or "")
            )
            or any(subject.get("authority", {}).values())
        ):
            raise ExtensionIsolatedRunnerStorageError(
                "isolated-run prerequisite subject is invalid"
            )
        return subject

    def _required_backend_status(self) -> IsolatedRunnerBackendStatus:
        status = self._observe_backend_status()
        if (
            status.availability != "available"
            or not status.conformance_certified
            or status.image_id is None
            or status.image_conformance_digest is None
            or status.engine_identity_digest is None
            or any(status.authority.model_dump().values())
        ):
            raise ExtensionIsolatedRunnerUnavailableError(
                "trusted isolated runner is not certified"
            )
        return status

    def _observe_backend_status(self) -> IsolatedRunnerBackendStatus:
        """Perform one explicit backend observation and persist its snapshot."""

        try:
            status = parse_isolated_runner_backend_status(
                self.backend.status()
            )
        except Exception:
            status = self._unavailable_backend_status()
        self._persist_backend_snapshot(status)
        return status

    def _persist_backend_snapshot(
        self,
        status: IsolatedRunnerBackendStatus,
    ) -> None:
        observed_at = self._now_iso()
        snapshot = IsolatedRunnerBackendSnapshot(
            schema_version=(
                ISOLATED_RUNNER_BACKEND_SNAPSHOT_SCHEMA_VERSION
            ),
            observed_at=observed_at,
            backend_status=status,
            backend_status_digest=hashlib.sha256(
                status.canonical_bytes()
            ).hexdigest(),
        )

        def persist(state: dict[str, Any]) -> None:
            self._validate_state(state)
            state["backend_snapshot"] = snapshot.canonical_dict()
            state["updated_at"] = observed_at

        self._mutate(persist)

    def _cached_backend_snapshot(
        self,
        state: dict[str, Any] | None,
    ) -> tuple[IsolatedRunnerBackendStatus, dict[str, Any], bool]:
        missing = {
            "status": "missing",
            "reason_code": "not_observed",
            "observed_at": None,
            "age_seconds": None,
            "ttl_seconds": ISOLATED_RUNNER_BACKEND_SNAPSHOT_TTL_SECONDS,
            "backend_status_digest": None,
        }
        if not isinstance(state, dict) or "backend_snapshot" not in state:
            return self._unavailable_backend_status(), missing, False
        try:
            snapshot = parse_isolated_runner_backend_snapshot(
                state["backend_snapshot"]
            )
            now = self._now()
            if now.tzinfo is None:
                raise ValueError("now must be timezone-aware")
            age = (
                now.astimezone(timezone.utc)
                - self._parse_time(snapshot.observed_at)
            ).total_seconds()
        except Exception:
            return self._unavailable_backend_status(), {
                **missing,
                "status": "stale",
                "reason_code": "snapshot_invalid",
            }, False
        fresh = bool(
            0 <= age <= ISOLATED_RUNNER_BACKEND_SNAPSHOT_TTL_SECONDS
        )
        backend = snapshot.backend_status
        backend_ready = bool(
            fresh
            and backend.availability == "available"
            and backend.conformance_certified
            and not any(backend.authority.model_dump().values())
        )
        return backend, {
            "status": "fresh" if fresh else "stale",
            "reason_code": (
                "observed"
                if fresh
                else "clock_skew"
                if age < 0
                else "expired"
            ),
            "observed_at": snapshot.observed_at,
            "age_seconds": max(0, int(age)),
            "ttl_seconds": ISOLATED_RUNNER_BACKEND_SNAPSHOT_TTL_SECONDS,
            "backend_status_digest": snapshot.backend_status_digest,
        }, backend_ready

    @staticmethod
    def _binding_from_subject(
        subject: dict[str, Any],
        backend: IsolatedRunnerBackendStatus,
        *,
        run_id: str,
    ) -> IsolatedRunnerBinding:
        if (
            backend.image_id is None
            or backend.image_conformance_digest is None
            or backend.engine_identity_digest is None
        ):
            raise ExtensionIsolatedRunnerUnavailableError(
                "trusted isolated runner identity is unavailable"
            )
        return IsolatedRunnerBinding(
            schema_version=ISOLATED_RUNNER_BINDING_SCHEMA_VERSION,
            run_id=run_id,
            check_id=subject["check_id"],
            source_check_binding_digest=subject[
                "source_check_binding_digest"
            ],
            source_check_report_digest=subject[
                "source_check_report_digest"
            ],
            source_check_stage="SOURCE_CHECK_PASSED",
            candidate_id=subject["candidate_id"],
            candidate_revision=subject["candidate_revision"],
            artifact_id=subject["artifact_id"],
            artifact_revision=subject["artifact_revision"],
            artifact_envelope_digest=subject["artifact_envelope_digest"],
            owner_scope_digest=subject["owner_scope_digest"],
            extension_id=subject["extension_id"],
            extension_version=subject["extension_version"],
            spec_digest=subject["spec_digest"],
            artifact_sha256=subject["artifact_sha256"],
            artifact_size_bytes=subject["artifact_size_bytes"],
            extension_policy_revision=subject[
                "extension_policy_revision"
            ],
            artifact_policy_revision=subject["artifact_policy_revision"],
            source_check_policy_revision=subject[
                "source_check_policy_revision"
            ],
            source_checker_revision=subject["checker_revision"],
            source_parser_identity=subject["parser_identity"],
            source_ruleset_digest=subject["ruleset_digest"],
            runner_policy_revision=backend.runner_policy_revision,
            runner_policy_digest=backend.runner_policy_digest,
            backend_kind=backend.backend_kind,
            engine_identity_digest=backend.engine_identity_digest,
            image_id=backend.image_id,
            image_conformance_digest=backend.image_conformance_digest,
            harness_revision=backend.harness_revision,
            harness_digest=backend.harness_digest,
        )

    def _validate_prerequisite(
        self,
        record: dict[str, Any],
        *,
        user_id: str,
        workspace_id: str,
    ) -> None:
        binding = parse_isolated_runner_binding(record["binding"])
        subject = self._runner_subject(
            check_id=binding.check_id,
            user_id=user_id,
            workspace_id=workspace_id,
            expected_artifact_revision=binding.artifact_revision,
            expected_artifact_sha256=binding.artifact_sha256,
            expected_source_check_report_digest=(
                binding.source_check_report_digest
            ),
        )
        if (
            subject["source_check_binding_digest"]
            != binding.source_check_binding_digest
            or subject["artifact_envelope_digest"]
            != binding.artifact_envelope_digest
        ):
            raise ExtensionIsolatedRunnerConflictError(
                "isolated-run prerequisite changed"
            )

    def _public_run_for_owner(
        self,
        record: dict[str, Any],
        *,
        user_id: str,
        workspace_id: str,
        operation_replayed: bool = False,
        run_existing: bool = False,
    ) -> dict[str, Any]:
        # Owner scope is checked by the caller from one atomic local snapshot.
        # Public read paths must never re-open artifacts or invoke integrations.
        del user_id, workspace_id
        return self._public_run(
            record,
            operation_replayed=operation_replayed,
            run_existing=run_existing,
        )

    def _public_run_with_prerequisite(
        self,
        record: dict[str, Any],
        *,
        user_id: str,
        workspace_id: str,
        operation_replayed: bool = False,
        run_existing: bool = False,
    ) -> dict[str, Any]:
        public = self._public_run(
            record,
            operation_replayed=operation_replayed,
            run_existing=run_existing,
        )
        try:
            self._validate_prerequisite(
                record,
                user_id=user_id,
                workspace_id=workspace_id,
            )
        except (
            ExtensionSourceCheckConflictError,
            ExtensionSourceCheckNotFoundError,
            ExtensionIsolatedRunnerConflictError,
        ):
            public["effective_status"] = "BLOCKED_PREREQUISITE"
            public["operational_health"] = "degraded"
        except Exception:
            public["effective_status"] = "PREREQUISITE_UNAVAILABLE"
            public["operational_health"] = "degraded"
        return public

    def _public_run(
        self,
        record: dict[str, Any],
        *,
        operation_replayed: bool = False,
        run_existing: bool = False,
    ) -> dict[str, Any]:
        self._validate_record(record)
        binding = parse_isolated_runner_binding(record["binding"])
        report = (
            parse_isolated_runner_report(record["report"])
            if isinstance(record["report"], dict)
            else None
        )
        effective, health = self._stored_effective_status(record)
        if report is None:
            probe_status = "indeterminate"
            runner_status = "indeterminate"
            issue_codes: list[str] = []
            report_integrity = "unavailable"
            probe_checks = {
                "artifact_identity_status": "not_checked",
                "harness_identity_status": "not_checked",
                "non_root_status": "not_checked",
                "rootfs_read_only_status": "not_checked",
                "input_read_only_status": "not_checked",
                "network_isolation_status": "not_checked",
                "secret_isolation_status": "not_checked",
                "host_surface_isolation_status": "not_checked",
                "resource_limits_status": "not_checked",
            }
        else:
            probe_status = report.probe_status
            runner_status = report.isolated_runner_status
            issue_codes = list(report.issue_codes)
            report_integrity = "validated"
            probe_checks = {
                key: getattr(report, key)
                for key in (
                    "artifact_identity_status",
                    "harness_identity_status",
                    "non_root_status",
                    "rootfs_read_only_status",
                    "input_read_only_status",
                    "network_isolation_status",
                    "secret_isolation_status",
                    "host_surface_isolation_status",
                    "resource_limits_status",
                )
            }
        return {
            "schema_version": PUBLIC_RUN_SCHEMA,
            "run_id": record["run_id"],
            "check_id": binding.check_id,
            "artifact_id": binding.artifact_id,
            "artifact_revision": binding.artifact_revision,
            "candidate_id": binding.candidate_id,
            "candidate_revision": binding.candidate_revision,
            "extension_id": binding.extension_id,
            "extension_version": binding.extension_version,
            "artifact_sha256": binding.artifact_sha256,
            "stored_stage": record["stage"],
            "effective_status": effective,
            "probe_status": probe_status,
            **probe_checks,
            "trusted_isolated_runner_status": runner_status,
            # Phase 6.2d proves the fixed runner boundary only.  No candidate
            # test or candidate instruction is executed by this stage.
            "isolated_test_execution_status": "not_started",
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
            "issue_codes": issue_codes,
            "backend_kind": binding.backend_kind,
            "engine_identity_digest": binding.engine_identity_digest,
            "image_id": binding.image_id,
            "image_conformance_digest": binding.image_conformance_digest,
            "harness_revision": binding.harness_revision,
            "harness_digest": binding.harness_digest,
            "runner_policy_revision": binding.runner_policy_revision,
            "runner_policy_digest": binding.runner_policy_digest,
            "report_integrity_status": report_integrity,
            "operational_health": health,
            "operation_replayed": operation_replayed,
            "run_existing": run_existing,
            "policy_effect": "none",
            "authority": self._authority(),
        }

    @staticmethod
    def _stored_effective_status(
        record: dict[str, Any],
    ) -> tuple[str, str]:
        if record["stage"] in {
            "RUNNER_JOB_CLAIMED",
            "RUNNER_JOB_STARTED",
            "RUNNER_JOB_INDETERMINATE",
        }:
            return "RUNNER_JOB_INDETERMINATE", "degraded"
        return str(record["stage"]), "available"

    def _read_valid_state(self) -> dict[str, Any]:
        try:
            state = self.state_store.read_json(STATE_FILE)
            self._validate_state(state)
            return state
        except ExtensionIsolatedRunnerError:
            raise
        except Exception as exc:
            raise ExtensionIsolatedRunnerStorageError(
                "isolated-run state is unavailable"
            ) from exc

    def _read_owner_state(
        self,
        user_id: str,
        workspace_id: str,
    ) -> tuple[str, str, str, dict[str, Any]]:
        selected_user = self._required_text(user_id, "user_id", 240)
        selected_workspace = self._required_text(
            workspace_id,
            "workspace_id",
            240,
        )
        try:
            snapshot = self.state_store.read_snapshot(
                ["local_world.json", STATE_FILE]
            )
            self._assert_workspace_document(
                snapshot["local_world.json"],
                selected_workspace,
            )
            state = snapshot[STATE_FILE]
            self._validate_state(state)
        except ExtensionIsolatedRunnerError:
            raise
        except Exception as exc:
            raise ExtensionIsolatedRunnerStorageError(
                "isolated-run owner-scoped state is unavailable"
            ) from exc
        return (
            selected_user,
            selected_workspace,
            artifact_owner_scope_digest(selected_user, selected_workspace),
            state,
        )

    def _mutate(
        self,
        mutator: Callable[[dict[str, Any]], None],
    ) -> None:
        try:
            self.state_store.mutate_json(STATE_FILE, mutator)
        except ExtensionIsolatedRunnerError:
            raise
        except Exception as exc:
            raise ExtensionIsolatedRunnerStorageError(
                "isolated-run state is unavailable"
            ) from exc

    def _validate_state(self, state: dict[str, Any]) -> None:
        if (
            not isinstance(state, dict)
            or state.get("_state_corrupt") is True
            or not _STATE_FIELDS.issubset(state)
            or bool(
                set(state)
                - _STATE_FIELDS
                - _STATE_OPTIONAL_FIELDS
                - _STATE_METADATA_FIELDS
            )
            or state.get("schema_version") != STATE_SCHEMA_VERSION
            or not isinstance(state.get("runs"), dict)
            or not isinstance(state.get("binding_index"), dict)
            or not isinstance(state.get("operation_index"), dict)
            or type(state.get("run_count")) is not int
            or state["run_count"] != len(state["runs"])
            or len(state["runs"]) > MAX_RUNS
            or len(state["operation_index"]) > MAX_OPERATIONS
            or (
                state.get("updated_at") is not None
                and not isinstance(state.get("updated_at"), str)
            )
        ):
            raise ExtensionIsolatedRunnerStorageError(
                "isolated-run private state is invalid"
            )
        if "backend_snapshot" in state:
            try:
                parse_isolated_runner_backend_snapshot(
                    state["backend_snapshot"]
                )
            except Exception as exc:
                raise ExtensionIsolatedRunnerStorageError(
                    "isolated-run backend snapshot is invalid"
                ) from exc
        expected_index: dict[str, str] = {}
        for run_id, record in state["runs"].items():
            if run_id != record.get("run_id"):
                raise ExtensionIsolatedRunnerStorageError(
                    "isolated-run map identity is invalid"
                )
            self._validate_record(record)
            digest = str(record["binding_digest"])
            if digest in expected_index:
                raise ExtensionIsolatedRunnerStorageError(
                    "isolated-run binding identity is duplicated"
                )
            expected_index[digest] = run_id
        if state["binding_index"] != expected_index:
            raise ExtensionIsolatedRunnerStorageError(
                "isolated-run binding index is invalid"
            )
        for operation_key, operation in state["operation_index"].items():
            self._validate_operation(operation_key, operation, state)

    def _validate_record(self, record: dict[str, Any]) -> None:
        if (
            not isinstance(record, dict)
            or set(record) != _RECORD_FIELDS
            or record.get("schema_version") != RECORD_SCHEMA_VERSION
            or not _RUN_ID.fullmatch(str(record.get("run_id") or ""))
            or not _DIGEST.fullmatch(
                str(record.get("owner_scope_digest") or "")
            )
            or not isinstance(record.get("binding"), dict)
            or not _DIGEST.fullmatch(
                str(record.get("binding_digest") or "")
            )
            or record.get("stage") not in _STAGES
            or not isinstance(record.get("created_at"), str)
            or not isinstance(record.get("updated_at"), str)
            or (
                record.get("started_at") is not None
                and not isinstance(record.get("started_at"), str)
            )
        ):
            raise ExtensionIsolatedRunnerStorageError(
                "isolated-run private record is invalid"
            )
        binding = parse_isolated_runner_binding(record["binding"])
        if (
            binding.run_id != record["run_id"]
            or binding.owner_scope_digest != record["owner_scope_digest"]
            or binding.binding_digest() != record["binding_digest"]
        ):
            raise ExtensionIsolatedRunnerStorageError(
                "isolated-run record binding is invalid"
            )
        self._parse_time(record["created_at"])
        self._parse_time(record["updated_at"])
        if record["started_at"] is not None:
            self._parse_time(record["started_at"])
        report = record.get("report")
        if record["stage"] in {"RUNNER_JOB_PASSED", "RUNNER_JOB_FAILED"}:
            if (
                not isinstance(report, dict)
                or not _DIGEST.fullmatch(
                    str(record.get("report_digest") or "")
                )
                or record.get("failure_code") is not None
                or record.get("started_at") is None
            ):
                raise ExtensionIsolatedRunnerStorageError(
                    "isolated-run terminal report is invalid"
                )
            parsed = parse_isolated_runner_report(report)
            if (
                parsed.binding.canonical_dict() != binding.canonical_dict()
                or parsed.binding_digest != record["binding_digest"]
                or parsed.report_digest() != record["report_digest"]
                or (
                    parsed.probe_status == "passed"
                    and record["stage"] != "RUNNER_JOB_PASSED"
                )
                or (
                    parsed.probe_status == "failed"
                    and record["stage"] != "RUNNER_JOB_FAILED"
                )
            ):
                raise ExtensionIsolatedRunnerStorageError(
                    "isolated-run report identity is invalid"
                )
        elif (
            report is not None
            or record.get("report_digest") is not None
            or (
                record["stage"] in {
                    "RUNNER_JOB_CLAIMED",
                    "RUNNER_JOB_STARTED",
                }
                and record.get("failure_code") is not None
            )
            or (
                record["stage"] == "RUNNER_JOB_CLAIMED"
                and record.get("started_at") is not None
            )
            or (
                record["stage"] == "RUNNER_JOB_STARTED"
                and record.get("started_at") is None
            )
            or (
                record["stage"] == "RUNNER_JOB_INDETERMINATE"
                and record.get("failure_code") not in _FAILURE_CODES
            )
        ):
            raise ExtensionIsolatedRunnerStorageError(
                "isolated-run non-report state is invalid"
            )

    def _validate_operation(
        self,
        operation_key: Any,
        operation: Any,
        state: dict[str, Any],
    ) -> None:
        if (
            not isinstance(operation_key, str)
            or not _DIGEST.fullmatch(operation_key)
            or not isinstance(operation, dict)
            or set(operation) != _OPERATION_FIELDS
            or operation.get("kind") != "isolated_run"
            or not _DIGEST.fullmatch(
                str(operation.get("request_digest") or "")
            )
            or not _RUN_ID.fullmatch(str(operation.get("run_id") or ""))
            or operation["run_id"] not in state["runs"]
            or not isinstance(operation.get("recorded_at"), str)
        ):
            raise ExtensionIsolatedRunnerStorageError(
                "isolated-run operation binding is invalid"
            )
        record = self._record_by_id(state, str(operation["run_id"]))
        binding = parse_isolated_runner_binding(record["binding"])
        if operation["request_digest"] != self._request_digest(
            str(record["owner_scope_digest"]),
            binding.check_id,
            binding.artifact_revision,
            binding.artifact_sha256,
            binding.source_check_report_digest,
        ):
            raise ExtensionIsolatedRunnerStorageError(
                "isolated-run operation semantic binding is invalid"
            )
        self._parse_time(operation["recorded_at"])

    def _record_operation(
        self,
        state: dict[str, Any],
        *,
        operation_key: str,
        request_digest: str,
        run_id: str,
    ) -> None:
        if len(state["operation_index"]) >= MAX_OPERATIONS:
            raise ExtensionIsolatedRunnerConflictError(
                "isolated-run operation capacity is exhausted"
            )
        record = self._record_by_id(state, run_id)
        binding = parse_isolated_runner_binding(record["binding"])
        if request_digest != self._request_digest(
            str(record["owner_scope_digest"]),
            binding.check_id,
            binding.artifact_revision,
            binding.artifact_sha256,
            binding.source_check_report_digest,
        ):
            raise ExtensionIsolatedRunnerStorageError(
                "isolated-run operation request is invalid"
            )
        recorded_at = self._now_iso()
        state["operation_index"][operation_key] = {
            "kind": "isolated_run",
            "request_digest": request_digest,
            "run_id": run_id,
            "recorded_at": recorded_at,
        }
        state["updated_at"] = recorded_at

    def _existing_operation(
        self,
        *,
        state: dict[str, Any],
        operation_key: str,
        request_digest: str,
        owner_scope_digest: str,
    ) -> dict[str, Any] | None:
        operation = state["operation_index"].get(operation_key)
        if operation is None:
            return None
        self._validate_operation(operation_key, operation, state)
        if operation["request_digest"] != request_digest:
            raise ExtensionIsolatedRunnerConflictError(
                "isolated-run operation identity was rebound"
            )
        record = self._record_by_id(state, str(operation["run_id"]))
        self._require_owner(record, owner_scope_digest)
        return record

    @staticmethod
    def _record_by_id(
        state: dict[str, Any],
        run_id: str,
    ) -> dict[str, Any]:
        record = state["runs"].get(run_id)
        if not isinstance(record, dict):
            raise ExtensionIsolatedRunnerNotFoundError(
                "isolated run was not found"
            )
        return record

    @staticmethod
    def _require_owner(
        record: dict[str, Any],
        owner_scope_digest: str,
    ) -> None:
        if record.get("owner_scope_digest") != owner_scope_digest:
            raise ExtensionIsolatedRunnerNotFoundError(
                "isolated run was not found"
            )

    def _authorize_start(self, supplied_token: str) -> None:
        if not self.enabled:
            raise ExtensionIsolatedRunnerUnavailableError(
                "trusted isolated runner is disabled by policy"
            )
        self._authorize_control(supplied_token)

    def _authorize_control(self, supplied_token: str) -> None:
        if not self.control_token:
            raise ExtensionIsolatedRunnerUnavailableError(
                "trusted isolated runner control token is not configured"
            )
        selected = str(supplied_token or "").strip()
        if not selected or not hmac.compare_digest(
            selected,
            self.control_token,
        ):
            raise ExtensionIsolatedRunnerUnauthorizedError(
                "trusted isolated runner control token is invalid"
            )

    def _assert_current_workspace(self, workspace_id: str) -> None:
        self._assert_workspace_document(
            self.state_store.read_json("local_world.json"),
            workspace_id,
        )

    @staticmethod
    def _assert_workspace_document(
        local_world: dict[str, Any],
        workspace_id: str,
    ) -> None:
        current_workspace = str(
            local_world.get("current_project") or ""
        ).strip()
        if not current_workspace or current_workspace != workspace_id:
            raise ExtensionIsolatedRunnerConflictError(
                "workspace must match the current local Veyra scope"
            )

    @staticmethod
    def _run_id(
        subject: dict[str, Any],
        backend: IsolatedRunnerBackendStatus,
    ) -> str:
        seed = ExtensionIsolatedRunnerGate._digest(
            {
                "owner_scope_digest": subject["owner_scope_digest"],
                "check_id": subject["check_id"],
                "source_check_report_digest": subject[
                    "source_check_report_digest"
                ],
                "artifact_sha256": subject["artifact_sha256"],
                "runner_policy_digest": backend.runner_policy_digest,
                "harness_digest": backend.harness_digest,
                "image_id": backend.image_id,
                "image_conformance_digest": (
                    backend.image_conformance_digest
                ),
            }
        )
        return f"extrun_{seed[:24]}"

    @staticmethod
    def _operation_key(owner_scope_digest: str, operation_id: str) -> str:
        return ExtensionIsolatedRunnerGate._digest(
            {
                "owner_scope_digest": owner_scope_digest,
                "operation_id": operation_id,
            }
        )

    @staticmethod
    def _request_digest(
        owner_scope_digest: str,
        check_id: str,
        artifact_revision: int,
        artifact_sha256: str,
        source_check_report_digest: str,
    ) -> str:
        return ExtensionIsolatedRunnerGate._digest(
            {
                "kind": "isolated_run",
                "owner_scope_digest": owner_scope_digest,
                "check_id": check_id,
                "artifact_revision": artifact_revision,
                "artifact_sha256": artifact_sha256,
                "source_check_report_digest": source_check_report_digest,
            }
        )

    @staticmethod
    def _digest(value: Any) -> str:
        return hashlib.sha256(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _required_id(value: Any, field: str) -> str:
        if not isinstance(value, str) or not _ID.fullmatch(value):
            raise ValueError(
                f"{field} must be a bounded opaque identifier"
            )
        return value

    @staticmethod
    def _required_run_id(value: Any) -> str:
        if not isinstance(value, str) or not _RUN_ID.fullmatch(value):
            raise ValueError("run_id is invalid")
        return value

    @staticmethod
    def _required_check_id(value: Any) -> str:
        if not isinstance(value, str) or not _CHECK_ID.fullmatch(value):
            raise ValueError("check_id is invalid")
        return value

    @staticmethod
    def _required_revision(value: Any) -> int:
        if type(value) is not int or value < 1:
            raise ValueError(
                "expected_artifact_revision must be positive"
            )
        return value

    @staticmethod
    def _required_digest(value: Any, field: str) -> str:
        if not isinstance(value, str) or not _DIGEST.fullmatch(value):
            raise ValueError(f"{field} must be a SHA-256 digest")
        return value

    @staticmethod
    def _required_text(value: Any, field: str, limit: int) -> str:
        if not isinstance(value, str):
            raise TypeError(f"{field} must be a string")
        selected = value.strip()
        if (
            not selected
            or len(selected.encode("utf-8")) > limit
            or "\x00" in selected
            or unicodedata.normalize("NFC", selected) != selected
        ):
            raise ValueError(f"{field} is invalid")
        return selected

    @staticmethod
    def _parse_time(value: str) -> datetime:
        if not isinstance(value, str):
            raise ValueError("isolated-run timestamp is invalid")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("isolated-run timestamp is invalid") from exc
        if parsed.tzinfo is None:
            raise ValueError("isolated-run timestamp must be timezone-aware")
        return parsed.astimezone(timezone.utc)

    def _now_iso(self) -> str:
        selected = self._now()
        if selected.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        selected = selected.astimezone(timezone.utc)
        if selected.microsecond:
            return selected.isoformat(
                timespec="microseconds"
            ).replace("+00:00", "Z")
        return selected.isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        )

    @staticmethod
    def _env_enabled(value: str) -> bool:
        return str(value or "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    @staticmethod
    def _authority() -> dict[str, bool]:
        return IsolatedRunnerAuthority().model_dump(mode="python")

    @staticmethod
    def _safe_issue(exc: Exception) -> str:
        if isinstance(exc, ExtensionIsolatedRunnerStorageError):
            return "extension_isolated_runner_state_invalid"
        return f"extension_isolated_runner_unavailable:{type(exc).__name__}"

    @staticmethod
    def _unavailable_backend_status() -> IsolatedRunnerBackendStatus:
        return IsolatedRunnerBackendStatus(
            schema_version=(
                "veyra.phase6.isolated_runner_backend_status.v1"
            ),
            backend_kind=ISOLATED_RUNNER_BACKEND_KIND,
            availability="unavailable",
            reason_code="engine_unavailable",
            runner_policy_revision=ISOLATED_RUNNER_POLICY_REVISION,
            runner_policy_digest=ISOLATED_RUNNER_POLICY_DIGEST,
            harness_revision=ISOLATED_RUNNER_HARNESS_REVISION,
            harness_digest="0" * 64,
            image_id=None,
            image_conformance_digest=None,
            engine_identity_digest=None,
            conformance_certified=False,
            authority=IsolatedRunnerAuthority(),
        )

    def _audit(self, record: dict[str, Any]) -> None:
        try:
            self.state_store.append_jsonl(
                "action_record.jsonl",
                {
                    "route": "phase6_extension_isolated_runner",
                    "status": "recorded",
                    "artifacts": {
                        "private_run_scope_digest": self._digest(
                            {
                                "run_id": record.get("run_id"),
                                "owner_scope_digest": record.get(
                                    "owner_scope_digest"
                                ),
                            }
                        ),
                        "runner_probe_status": (
                            "passed"
                            if record.get("stage") == "RUNNER_JOB_PASSED"
                            else "failed"
                            if record.get("stage") == "RUNNER_JOB_FAILED"
                            else "indeterminate"
                        ),
                        "candidate_executed": False,
                        "behavior_verified": False,
                        "signing_authorized": False,
                        "promotion_authorized": False,
                    },
                },
            )
        except Exception:
            return


__all__ = [
    "MAX_OPERATIONS",
    "MAX_RUNS",
    "RUNNER_ENABLE_ENV",
    "RUNNER_TOKEN_ENV",
    "STATE_FILE",
    "STATE_SCHEMA_VERSION",
    "ExtensionIsolatedRunnerConflictError",
    "ExtensionIsolatedRunnerError",
    "ExtensionIsolatedRunnerGate",
    "ExtensionIsolatedRunnerNotFoundError",
    "ExtensionIsolatedRunnerStorageError",
    "ExtensionIsolatedRunnerUnauthorizedError",
    "ExtensionIsolatedRunnerUnavailableError",
]
