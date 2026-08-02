from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
import math
import os
import re
from typing import Any, Callable

from core.world_state import WorldStateStore
from interface.extension_artifact import artifact_owner_scope_digest
from interface.extension_dynamic_validation import (
    DYNAMIC_VALIDATION_BINDING_SCHEMA_VERSION,
    DYNAMIC_VALIDATION_HARNESS_REVISION,
    DYNAMIC_VALIDATION_POLICY_DIGEST,
    DYNAMIC_VALIDATION_POLICY_REVISION,
    DynamicValidationAuthority,
    DynamicValidationBackendStatus,
    DynamicValidationBinding,
    DynamicValidationReport,
    DynamicValidationTestBundle,
    dynamic_build_identity_digest,
    dynamic_request_provenance_digest,
    parse_dynamic_validation_backend_status,
    parse_dynamic_validation_binding,
    parse_dynamic_validation_report,
    parse_dynamic_validation_test_bundle,
)
from interface.extension_isolated_runner import (
    ISOLATED_RUNNER_HARNESS_REVISION,
    ISOLATED_RUNNER_POLICY_REVISION,
)
from interface.extension_generation import (
    authenticated_local_principal_digest,
    initiating_session_digest,
)
from interface.extension_spec import ExtensionObjectSchema, parse_extension_spec
from runtime.extension_isolated_runner_gate import (
    ExtensionIsolatedRunnerConflictError,
    ExtensionIsolatedRunnerError,
    ExtensionIsolatedRunnerGate,
    ExtensionIsolatedRunnerNotFoundError,
)
from runtime.trusted_extension_validation_runner import (
    TrustedExtensionValidationBindingError,
    TrustedExtensionValidationError,
    TrustedExtensionValidationRunner,
    TrustedExtensionValidationUnavailableError,
)


STATE_FILE = "phase6_extension_dynamic_validation_state.json"
STATE_SCHEMA_VERSION = "veyra.phase6.extension_dynamic_validation_state.v1"
RECORD_SCHEMA_VERSION = (
    "veyra.phase6.extension_dynamic_validation_private_record.v1"
)
TEST_CONTRACT_SCHEMA_VERSION = (
    "veyra.phase6.extension_dynamic_validation_test_contract.v1"
)
BACKEND_SNAPSHOT_SCHEMA_VERSION = (
    "veyra.phase6.extension_dynamic_validation_backend_snapshot.v1"
)
PUBLIC_STATUS_SCHEMA = (
    "veyra.phase6.extension_dynamic_validation_status.v1"
)
PUBLIC_RECORD_SCHEMA = (
    "veyra.phase6.extension_dynamic_validation_record.v1"
)
PUBLIC_LIST_SCHEMA = "veyra.phase6.extension_dynamic_validation_list.v1"
PUBLIC_INTEGRITY_SCHEMA = (
    "veyra.phase6.extension_dynamic_validation_integrity.v1"
)
VALIDATION_ENABLE_ENV = "VEYRA_PHASE6_DYNAMIC_VALIDATION_ENABLED"
VALIDATION_TOKEN_ENV = "VEYRA_LOCAL_API_TOKEN"
BACKEND_SNAPSHOT_TTL_SECONDS = 300
MAX_VALIDATIONS = 200
MAX_OPERATIONS = 2_000

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,239}$")
_VALIDATION_ID = re.compile(r"^extval_[0-9a-f]{24}$")
_RUN_ID = re.compile(r"^extrun_[0-9a-f]{24}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_STAGES = frozenset(
    {
        "DYNAMIC_VALIDATION_CLAIMED",
        "DYNAMIC_VALIDATION_STARTED",
        "DYNAMIC_VALIDATION_PASSED",
        "DYNAMIC_VALIDATION_FAILED",
        "DYNAMIC_VALIDATION_INDETERMINATE",
    }
)
_FAILURE_CODES = frozenset(
    {
        "backend_unavailable",
        "backend_report_invalid",
        "prerequisite_changed",
        "validation_internal_error",
    }
)
_STATE_FIELDS = frozenset(
    {
        "schema_version",
        "validations",
        "binding_index",
        "operation_index",
        "validation_count",
        "backend_snapshot",
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
_RECORD_FIELDS = frozenset(
    {
        "schema_version",
        "validation_id",
        "owner_scope_digest",
        "principal_digest",
        "session_digest",
        "request_id",
        "request_provenance_digest",
        "binding",
        "binding_digest",
        "test_bundle",
        "test_bundle_digest",
        "test_contract",
        "test_contract_digest",
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
    {"kind", "request_digest", "validation_id", "recorded_at"}
)
_SNAPSHOT_FIELDS = frozenset(
    {"schema_version", "observed_at", "backend_status", "status_digest"}
)


class ExtensionDynamicValidationError(RuntimeError):
    """Base error for the private Phase 6.2e dynamic validation gate."""


class ExtensionDynamicValidationConflictError(ExtensionDynamicValidationError):
    """A lifecycle, CAS, replay, test, or trust identity conflicted."""


class ExtensionDynamicValidationNotFoundError(ExtensionDynamicValidationError):
    """A validation record was absent or outside the owner scope."""


class ExtensionDynamicValidationStorageError(ExtensionDynamicValidationError):
    """Private validation state or prerequisite evidence is unavailable."""


class ExtensionDynamicValidationUnavailableError(ExtensionDynamicValidationError):
    """The flag, token configuration, or certified backend is unavailable."""


class ExtensionDynamicValidationUnauthorizedError(ExtensionDynamicValidationError):
    """An active validation request omitted the exact local control token."""


class ExtensionDynamicValidationGate:
    """Durable admission gate for the first candidate-executing Phase 6 slice.

    Only one exact ``SOURCE_CHECK_PASSED`` + ``RUNNER_JOB_PASSED`` subject is
    accepted.  Test vectors are strict caller data frozen outside generation,
    persisted privately and bound into the build identity.  The resulting
    report grants no signing, install, registry, canary or promotion authority.
    """

    def __init__(
        self,
        *,
        state_store: WorldStateStore,
        isolated_runner_gate: ExtensionIsolatedRunnerGate,
        backend: TrustedExtensionValidationRunner,
        enabled: bool | None = None,
        control_token: str | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.state_store = state_store
        self.isolated_runner_gate = isolated_runner_gate
        self.backend = backend
        self.enabled = (
            self._env_enabled(os.environ.get(VALIDATION_ENABLE_ENV, ""))
            if enabled is None
            else bool(enabled)
        )
        self.control_token = str(
            os.environ.get(VALIDATION_TOKEN_ENV, "")
            if control_token is None
            else control_token
        ).strip()
        self._now = now or (lambda: datetime.now(timezone.utc))

    def status(self) -> dict[str, Any]:
        """Return a pure persisted snapshot; never invoke Docker or a gate."""

        try:
            state = self._read_valid_state()
            counts = {stage: 0 for stage in sorted(_STAGES)}
            for record in state["validations"].values():
                counts[str(record["stage"])] += 1
            storage = {
                "status": "ready",
                "issue": None,
                "validation_count": len(state["validations"]),
                "operation_count": len(state["operation_index"]),
                "counts": counts,
            }
            storage_ready = True
        except Exception as exc:
            state = self._initial_state()
            storage = {
                "status": "fault",
                "issue": self._safe_issue(exc),
                "validation_count": 0,
                "operation_count": 0,
                "counts": {},
            }
            storage_ready = False
        backend, backend_snapshot, backend_ready = self._cached_backend(state)
        token_configured = bool(self.control_token)
        start_ready = bool(
            storage_ready
            and self.enabled
            and token_configured
            and backend_ready
        )
        return {
            "schema_version": PUBLIC_STATUS_SCHEMA,
            "phase": "6.2e",
            "status": (
                "technical_complete_dynamic_validation_only"
                if storage_ready
                else "fail_closed"
            ),
            "operational_health": "available" if storage_ready else "degraded",
            "completion_scope": (
                "fixed isolated unit contract security fuzz and behavior validation only"
            ),
            "candidate_execution_scope": (
                "single direct projection or JSON primitive constant function"
            ),
            "backend": backend.canonical_dict(),
            "backend_snapshot": backend_snapshot,
            "storage": storage,
            "admission": {
                "enabled": self.enabled,
                "control_token_configured": token_configured,
                "backend_ready": backend_ready,
                "start_ready": start_ready,
                "active_backend_refresh_required": not backend_ready,
            },
            "signature_status": "not_implemented",
            "activation_status": "not_installed",
            "capability_registry_visible": False,
            "canary_status": "not_started",
            "promotion_authorized": False,
            "policy_effect": "none",
            "read_side_effects": "none",
            "authority": self._authority(),
        }

    def refresh_backend(self, *, control_token: str) -> dict[str, Any]:
        self._authorize_active(control_token)
        self._observe_backend()
        return self.status()

    def start(
        self,
        *,
        isolated_run_id: str,
        user_id: str,
        workspace_id: str,
        session_id: str,
        request_id: str,
        operation_id: str,
        expected_artifact_revision: int,
        expected_artifact_sha256: str,
        expected_source_check_report_digest: str,
        expected_isolated_runner_report_digest: str,
        test_bundle: dict[str, Any],
        control_token: str,
    ) -> dict[str, Any]:
        # Authenticate before state reads, backend calls or prerequisite reads.
        principal = self._authorize_active(control_token)
        selected_run = self._required_run_id(isolated_run_id)
        selected_user = self._required_text(user_id, "user_id", 240)
        selected_workspace = self._required_text(
            workspace_id, "workspace_id", 240
        )
        selected_session = self._required_text(
            session_id, "session_id", 240
        )
        selected_request = self._required_text(
            request_id, "request_id", 240
        )
        selected_operation = self._required_text(
            operation_id, "operation_id", 240
        )
        selected_revision = self._required_revision(expected_artifact_revision)
        selected_artifact_digest = self._required_digest(
            expected_artifact_sha256, "expected_artifact_sha256"
        )
        selected_source_report = self._required_digest(
            expected_source_check_report_digest,
            "expected_source_check_report_digest",
        )
        selected_runner_report = self._required_digest(
            expected_isolated_runner_report_digest,
            "expected_isolated_runner_report_digest",
        )
        selected_tests = parse_dynamic_validation_test_bundle(test_bundle)
        owner_scope = artifact_owner_scope_digest(
            selected_user, selected_workspace
        )
        principal_digest = authenticated_local_principal_digest(principal)
        session_digest = initiating_session_digest(selected_session)
        request_provenance_digest = dynamic_request_provenance_digest(
            {
                "request_id": selected_request,
                "owner_scope_digest": owner_scope,
                "authenticated_principal_digest": principal_digest,
                "initiating_session_digest": session_digest,
                "isolated_run_id": selected_run,
                "test_bundle_digest": selected_tests.bundle_digest(),
            }
        )
        operation_key = self._operation_key(
            principal_digest,
            session_digest,
            selected_operation,
        )
        request_digest = self._request_digest(
            owner_scope=owner_scope,
            isolated_run_id=selected_run,
            artifact_revision=selected_revision,
            artifact_sha256=selected_artifact_digest,
            source_check_report_digest=selected_source_report,
            isolated_runner_report_digest=selected_runner_report,
            test_bundle_digest=selected_tests.bundle_digest(),
            authenticated_principal_digest=principal_digest,
            initiating_session_digest=session_digest,
            request_id=selected_request,
            request_provenance_digest=request_provenance_digest,
        )

        # Replay is a local durable lookup only.  It never executes again.
        _, _, _, state = self._read_owner_state(
            selected_user, selected_workspace
        )
        replay = self._existing_operation(
            state=state,
            operation_key=operation_key,
            request_digest=request_digest,
            owner_scope_digest=owner_scope,
            principal_digest=principal_digest,
            session_digest=session_digest,
        )
        if replay is not None:
            return self._public_record(
                replay,
                operation_replayed=True,
            )

        subject = self._validation_subject(
            run_id=selected_run,
            user_id=selected_user,
            workspace_id=selected_workspace,
            expected_artifact_revision=selected_revision,
            expected_artifact_sha256=selected_artifact_digest,
            expected_source_check_report_digest=selected_source_report,
            expected_isolated_runner_report_digest=selected_runner_report,
        )
        spec = parse_extension_spec(subject["source_check_spec"])
        self._validate_frozen_contract(spec.input_schema, spec.output_schema, selected_tests)
        test_contract = self._test_contract(spec, selected_tests)
        test_contract_digest = self._digest(test_contract)
        backend = self._required_backend_status()
        binding = self._binding_from_subject(
            subject,
            backend,
            selected_tests,
            authenticated_principal_digest=principal_digest,
            initiating_session_digest=session_digest,
            request_id=selected_request,
            request_provenance_digest=request_provenance_digest,
        )
        binding_digest = binding.binding_digest()

        claimed: dict[str, Any] | None = None
        operation_replayed = False
        validation_existing = False

        def claim(current: dict[str, Any]) -> None:
            nonlocal claimed, operation_replayed, validation_existing
            self._initialize_mutation_state(current)
            self._validate_state(current)
            existing_operation = self._existing_operation(
                state=current,
                operation_key=operation_key,
                request_digest=request_digest,
                owner_scope_digest=owner_scope,
                principal_digest=principal_digest,
                session_digest=session_digest,
            )
            if existing_operation is not None:
                claimed = existing_operation
                operation_replayed = True
                return
            existing_id = current["binding_index"].get(binding_digest)
            if existing_id is not None:
                existing = self._record_by_id(current, str(existing_id))
                self._require_owner(
                    existing,
                    owner_scope,
                    principal_digest,
                    session_digest,
                )
                self._record_operation(
                    current,
                    operation_key=operation_key,
                    request_digest=request_digest,
                    validation_id=str(existing["validation_id"]),
                )
                claimed = existing
                validation_existing = True
                return
            if len(current["validations"]) >= MAX_VALIDATIONS:
                raise ExtensionDynamicValidationConflictError(
                    "dynamic validation capacity is exhausted"
                )
            now = self._now_iso()
            record = {
                "schema_version": RECORD_SCHEMA_VERSION,
                "validation_id": binding.validation_id,
                "owner_scope_digest": owner_scope,
                "principal_digest": principal_digest,
                "session_digest": session_digest,
                "request_id": selected_request,
                "request_provenance_digest": request_provenance_digest,
                "binding": binding.canonical_dict(),
                "binding_digest": binding_digest,
                "test_bundle": selected_tests.canonical_dict(),
                "test_bundle_digest": selected_tests.bundle_digest(),
                "test_contract": test_contract,
                "test_contract_digest": test_contract_digest,
                "stage": "DYNAMIC_VALIDATION_CLAIMED",
                "report": None,
                "report_digest": None,
                "failure_code": None,
                "created_at": now,
                "started_at": None,
                "updated_at": now,
            }
            self._validate_record(record)
            current["validations"][binding.validation_id] = record
            current["binding_index"][binding_digest] = binding.validation_id
            current["validation_count"] = len(current["validations"])
            self._record_operation(
                current,
                operation_key=operation_key,
                request_digest=request_digest,
                validation_id=binding.validation_id,
            )
            current["updated_at"] = now
            claimed = record

        self._mutate(claim)
        if claimed is None:
            raise ExtensionDynamicValidationStorageError(
                "dynamic validation claim produced no record"
            )
        if operation_replayed or validation_existing:
            return self._public_record(
                claimed,
                operation_replayed=operation_replayed,
                validation_existing=validation_existing,
            )

        started: dict[str, Any] | None = None

        def mark_started(current: dict[str, Any]) -> None:
            nonlocal started
            self._validate_state(current)
            record = self._record_by_id(current, binding.validation_id)
            self._require_owner(
                record,
                owner_scope,
                principal_digest,
                session_digest,
            )
            if record["stage"] != "DYNAMIC_VALIDATION_CLAIMED":
                raise ExtensionDynamicValidationConflictError(
                    "dynamic validation cannot be replayed"
                )
            now = self._now_iso()
            record["stage"] = "DYNAMIC_VALIDATION_STARTED"
            record["started_at"] = now
            record["updated_at"] = now
            current["updated_at"] = now
            started = record

        self._mutate(mark_started)
        if started is None:
            raise ExtensionDynamicValidationStorageError(
                "dynamic validation start was not persisted"
            )

        try:
            self._revalidate_subject(binding, selected_user, selected_workspace)
            report = self.backend.run(
                artifact_bytes=subject["source_bytes"],
                spec=spec,
                test_bundle=selected_tests,
                binding=binding,
            )
            parsed_report = parse_dynamic_validation_report(report)
            if (
                parsed_report.binding.canonical_dict()
                != binding.canonical_dict()
                or parsed_report.binding_digest != binding_digest
                or any(parsed_report.authority.model_dump(mode="python").values())
            ):
                raise TrustedExtensionValidationBindingError(
                    "dynamic validation report binding changed"
                )
            self._revalidate_subject(binding, selected_user, selected_workspace)
        except (
            ExtensionDynamicValidationConflictError,
            ExtensionIsolatedRunnerError,
            TrustedExtensionValidationBindingError,
        ):
            return self._mark_indeterminate(
                binding.validation_id,
                owner_scope,
                principal_digest,
                session_digest,
                "prerequisite_changed",
            )
        except TrustedExtensionValidationUnavailableError:
            return self._mark_indeterminate(
                binding.validation_id,
                owner_scope,
                principal_digest,
                session_digest,
                "backend_unavailable",
            )
        except TrustedExtensionValidationError:
            return self._mark_indeterminate(
                binding.validation_id,
                owner_scope,
                principal_digest,
                session_digest,
                "backend_report_invalid",
            )
        except Exception:
            return self._mark_indeterminate(
                binding.validation_id,
                owner_scope,
                principal_digest,
                session_digest,
                "validation_internal_error",
            )
        # BaseException is deliberately not caught.  A crash leaves STARTED;
        # all reads expose it as INDETERMINATE and no replay executes it again.

        finalized: dict[str, Any] | None = None

        def finalize(current: dict[str, Any]) -> None:
            nonlocal finalized
            self._validate_state(current)
            record = self._record_by_id(current, binding.validation_id)
            self._require_owner(
                record,
                owner_scope,
                principal_digest,
                session_digest,
            )
            if record["stage"] != "DYNAMIC_VALIDATION_STARTED":
                raise ExtensionDynamicValidationConflictError(
                    "dynamic validation completion raced"
                )
            current_binding = parse_dynamic_validation_binding(record["binding"])
            if current_binding.canonical_dict() != binding.canonical_dict():
                raise ExtensionDynamicValidationConflictError(
                    "dynamic validation binding changed"
                )
            now = self._now_iso()
            record["stage"] = (
                "DYNAMIC_VALIDATION_PASSED"
                if parsed_report.validation_status == "passed"
                else "DYNAMIC_VALIDATION_FAILED"
            )
            record["report"] = parsed_report.canonical_dict()
            record["report_digest"] = parsed_report.report_digest()
            record["failure_code"] = None
            record["updated_at"] = now
            current["updated_at"] = now
            finalized = record

        self._mutate(finalize)
        if finalized is None:
            raise ExtensionDynamicValidationStorageError(
                "dynamic validation completion produced no record"
            )
        return self._public_record(finalized)

    def list(
        self,
        *,
        user_id: str,
        workspace_id: str,
        session_id: str,
        control_token: str,
        limit: int = 50,
    ) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        principal_digest = authenticated_local_principal_digest(
            self._authorize_active(control_token)
        )
        session_digest = initiating_session_digest(session_id)
        _, _, owner_scope, state = self._read_owner_state(user_id, workspace_id)
        records = [
            record
            for record in state["validations"].values()
            if record["owner_scope_digest"] == owner_scope
            and record["principal_digest"] == principal_digest
            and record["session_digest"] == session_digest
        ]
        records.sort(
            key=lambda item: (str(item["updated_at"]), str(item["validation_id"])),
            reverse=True,
        )
        selected = records[:limit]
        return {
            "schema_version": PUBLIC_LIST_SCHEMA,
            "count": len(selected),
            "validations": [self._public_record(item) for item in selected],
            "state_mutated": False,
            "authority": self._authority(),
        }

    def get(
        self,
        *,
        validation_id: str,
        user_id: str,
        workspace_id: str,
        session_id: str,
        control_token: str,
    ) -> dict[str, Any]:
        principal_digest = authenticated_local_principal_digest(
            self._authorize_active(control_token)
        )
        session_digest = initiating_session_digest(session_id)
        _, _, owner_scope, state = self._read_owner_state(user_id, workspace_id)
        record = self._record_by_id(
            state, self._required_validation_id(validation_id)
        )
        self._require_owner(
            record, owner_scope, principal_digest, session_digest
        )
        return self._public_record(record)

    def integrity(
        self,
        *,
        validation_id: str,
        user_id: str,
        workspace_id: str,
        session_id: str,
        control_token: str,
    ) -> dict[str, Any]:
        principal_digest = authenticated_local_principal_digest(
            self._authorize_active(control_token)
        )
        session_digest = initiating_session_digest(session_id)
        _, _, owner_scope, state = self._read_owner_state(user_id, workspace_id)
        record = self._record_by_id(
            state, self._required_validation_id(validation_id)
        )
        self._require_owner(
            record, owner_scope, principal_digest, session_digest
        )
        public = self._public_record(record)
        return {
            **public,
            "schema_version": PUBLIC_INTEGRITY_SCHEMA,
            "status": "dynamic_validation_integrity_passed",
            "private_test_bundle_integrity_status": "validated",
            "report_integrity_status": (
                "validated" if record["report"] is not None else "unavailable"
            ),
            "prerequisite_verification_status": "not_refreshed",
            "active_verification_required": True,
            "state_mutated": False,
        }

    def _validation_subject(self, **kwargs: Any) -> dict[str, Any]:
        resolver = getattr(
            self.isolated_runner_gate,
            "dynamic_validation_subject",
            None,
        )
        if not callable(resolver):
            raise ExtensionDynamicValidationUnavailableError(
                "isolated runner dynamic-validation subject is unavailable"
            )
        try:
            subject = resolver(**kwargs)
        except (ExtensionIsolatedRunnerError, ExtensionDynamicValidationError):
            raise
        except Exception as exc:
            raise ExtensionDynamicValidationStorageError(
                "dynamic validation prerequisite is unavailable"
            ) from exc
        required = {
            "schema_version",
            "run_id",
            "isolated_runner_binding_digest",
            "isolated_runner_report_digest",
            "isolated_runner_stage",
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
            "extension_id",
            "extension_version",
            "spec_digest",
            "parser_identity",
            "ruleset_digest",
            "source_check_spec",
            "source_bytes",
            "user_id",
            "workspace_id",
            "authority",
        }
        if (
            not isinstance(subject, dict)
            or set(subject) != required
            or subject["schema_version"]
            != "veyra.phase6.extension_dynamic_validation_subject.v1"
            or subject["isolated_runner_stage"] != "RUNNER_JOB_PASSED"
            or subject["source_check_stage"] != "SOURCE_CHECK_PASSED"
            or not isinstance(subject["source_check_spec"], dict)
            or not isinstance(subject["source_bytes"], bytes)
            or not isinstance(subject.get("authority"), dict)
            or any(subject.get("authority", {}).values())
        ):
            raise ExtensionDynamicValidationStorageError(
                "dynamic validation prerequisite subject is invalid"
            )
        expected_pairs = {
            "run_id": kwargs.get("run_id"),
            "user_id": kwargs.get("user_id"),
            "workspace_id": kwargs.get("workspace_id"),
            "artifact_revision": kwargs.get("expected_artifact_revision"),
            "artifact_sha256": kwargs.get("expected_artifact_sha256"),
            "source_check_report_digest": kwargs.get(
                "expected_source_check_report_digest"
            ),
            "isolated_runner_report_digest": kwargs.get(
                "expected_isolated_runner_report_digest"
            ),
        }
        if any(subject.get(key) != value for key, value in expected_pairs.items()):
            raise ExtensionDynamicValidationConflictError(
                "dynamic validation prerequisite CAS changed"
            )
        try:
            subject_spec_digest = parse_extension_spec(
                subject["source_check_spec"]
            ).digest()
        except Exception as exc:
            raise ExtensionDynamicValidationStorageError(
                "dynamic validation prerequisite Spec is invalid"
            ) from exc
        if (
            type(subject.get("artifact_size_bytes")) is not int
            or len(subject["source_bytes"]) != subject["artifact_size_bytes"]
            or hashlib.sha256(subject["source_bytes"]).hexdigest()
            != subject["artifact_sha256"]
            or subject["owner_scope_digest"]
            != artifact_owner_scope_digest(
                str(subject["user_id"]),
                str(subject["workspace_id"]),
            )
            or subject_spec_digest != subject["spec_digest"]
        ):
            raise ExtensionDynamicValidationConflictError(
                "dynamic validation prerequisite identity changed"
            )
        return subject

    def _revalidate_subject(
        self,
        binding: DynamicValidationBinding,
        user_id: str,
        workspace_id: str,
    ) -> None:
        subject = self._validation_subject(
            run_id=binding.isolated_run_id,
            user_id=user_id,
            workspace_id=workspace_id,
            expected_artifact_revision=binding.artifact_revision,
            expected_artifact_sha256=binding.artifact_sha256,
            expected_source_check_report_digest=(
                binding.source_check_report_digest
            ),
            expected_isolated_runner_report_digest=(
                binding.isolated_runner_report_digest
            ),
        )
        rebound = self._binding_from_subject(
            subject,
            self._backend_from_binding(binding),
            parse_dynamic_validation_test_bundle(
                self._record_test_bundle(
                    binding.validation_id,
                    user_id,
                    workspace_id,
                    binding.authenticated_principal_digest,
                    binding.initiating_session_digest,
                )
            ),
            validation_id=binding.validation_id,
            authenticated_principal_digest=(
                binding.authenticated_principal_digest
            ),
            initiating_session_digest=binding.initiating_session_digest,
            request_id=binding.request_id,
            request_provenance_digest=binding.request_provenance_digest,
        )
        if rebound.canonical_dict() != binding.canonical_dict():
            raise ExtensionDynamicValidationConflictError(
                "dynamic validation prerequisite changed"
            )

    def _record_test_bundle(
        self,
        validation_id: str,
        user_id: str,
        workspace_id: str,
        principal_digest: str,
        session_digest: str,
    ) -> dict[str, Any]:
        _, _, owner_scope, state = self._read_owner_state(user_id, workspace_id)
        record = self._record_by_id(state, validation_id)
        self._require_owner(
            record, owner_scope, principal_digest, session_digest
        )
        return dict(record["test_bundle"])

    @staticmethod
    def _backend_from_binding(
        binding: DynamicValidationBinding,
    ) -> DynamicValidationBackendStatus:
        return DynamicValidationBackendStatus(
            schema_version="veyra.phase6.dynamic_validation_backend_status.v1",
            backend_kind="docker_cli",
            availability="available",
            reason_code="ready",
            engine_identity_digest=binding.engine_identity_digest,
            image_id=binding.image_id,
            isolation_conformance_digest=binding.isolation_conformance_digest,
            validation_conformance_digest=binding.validation_conformance_digest,
            harness_revision=DYNAMIC_VALIDATION_HARNESS_REVISION,
            harness_digest=binding.validation_harness_digest,
            validation_policy_revision=DYNAMIC_VALIDATION_POLICY_REVISION,
            validation_policy_digest=DYNAMIC_VALIDATION_POLICY_DIGEST,
            conformance_certified=True,
            authority=DynamicValidationAuthority(),
        )

    def _required_backend_status(self) -> DynamicValidationBackendStatus:
        status = self._observe_backend()
        if (
            status.availability != "available"
            or not status.conformance_certified
            or status.image_id is None
            or status.engine_identity_digest is None
            or status.isolation_conformance_digest is None
            or status.validation_conformance_digest is None
            or any(status.authority.model_dump(mode="python").values())
        ):
            raise ExtensionDynamicValidationUnavailableError(
                "trusted dynamic validation backend is not certified"
            )
        return status

    def _observe_backend(self) -> DynamicValidationBackendStatus:
        try:
            status = parse_dynamic_validation_backend_status(self.backend.status())
        except Exception:
            status = self._unavailable_backend_status()
        observed_at = self._now_iso()
        snapshot = {
            "schema_version": BACKEND_SNAPSHOT_SCHEMA_VERSION,
            "observed_at": observed_at,
            "backend_status": status.canonical_dict(),
            "status_digest": hashlib.sha256(status.canonical_bytes()).hexdigest(),
        }

        def persist(current: dict[str, Any]) -> None:
            self._initialize_mutation_state(current)
            self._validate_state(current)
            current["backend_snapshot"] = snapshot
            current["updated_at"] = observed_at

        self._mutate(persist)
        return status

    def _cached_backend(
        self,
        state: dict[str, Any],
    ) -> tuple[DynamicValidationBackendStatus, dict[str, Any], bool]:
        missing = {
            "status": "missing",
            "reason_code": "not_observed",
            "observed_at": None,
            "age_seconds": None,
            "ttl_seconds": BACKEND_SNAPSHOT_TTL_SECONDS,
            "status_digest": None,
        }
        snapshot = state.get("backend_snapshot")
        if snapshot is None:
            return self._unavailable_backend_status(), missing, False
        try:
            status = self._validate_backend_snapshot(snapshot)
            age = (
                self._now().astimezone(timezone.utc)
                - self._parse_time(snapshot["observed_at"])
            ).total_seconds()
        except Exception:
            return self._unavailable_backend_status(), {
                **missing,
                "status": "stale",
                "reason_code": "snapshot_invalid",
            }, False
        fresh = 0 <= age <= BACKEND_SNAPSHOT_TTL_SECONDS
        ready = bool(
            fresh
            and status.availability == "available"
            and status.conformance_certified
            and not any(status.authority.model_dump(mode="python").values())
        )
        return status, {
            "status": "fresh" if fresh else "stale",
            "reason_code": (
                "observed" if fresh else "clock_skew" if age < 0 else "expired"
            ),
            "observed_at": snapshot["observed_at"],
            "age_seconds": max(0, int(age)),
            "ttl_seconds": BACKEND_SNAPSHOT_TTL_SECONDS,
            "status_digest": snapshot["status_digest"],
        }, ready

    @staticmethod
    def _binding_from_subject(
        subject: dict[str, Any],
        backend: DynamicValidationBackendStatus,
        test_bundle: DynamicValidationTestBundle,
        *,
        validation_id: str | None = None,
        authenticated_principal_digest: str,
        initiating_session_digest: str,
        request_id: str,
        request_provenance_digest: str,
    ) -> DynamicValidationBinding:
        if (
            backend.engine_identity_digest is None
            or backend.image_id is None
            or backend.isolation_conformance_digest is None
            or backend.validation_conformance_digest is None
        ):
            raise ExtensionDynamicValidationUnavailableError(
                "dynamic validation backend identity is unavailable"
            )
        values: dict[str, Any] = {
            "schema_version": DYNAMIC_VALIDATION_BINDING_SCHEMA_VERSION,
            "validation_id": validation_id or "extval_" + "0" * 24,
            "isolated_run_id": subject["run_id"],
            "isolated_runner_binding_digest": subject[
                "isolated_runner_binding_digest"
            ],
            "isolated_runner_report_digest": subject[
                "isolated_runner_report_digest"
            ],
            "isolated_runner_stage": "RUNNER_JOB_PASSED",
            "isolated_runner_policy_revision": ISOLATED_RUNNER_POLICY_REVISION,
            "isolated_runner_harness_revision": ISOLATED_RUNNER_HARNESS_REVISION,
            "source_check_id": subject["source_check_id"],
            "source_check_binding_digest": subject[
                "source_check_binding_digest"
            ],
            "source_check_report_digest": subject[
                "source_check_report_digest"
            ],
            "source_check_stage": "SOURCE_CHECK_PASSED",
            "candidate_id": subject["candidate_id"],
            "candidate_revision": subject["candidate_revision"],
            "artifact_id": subject["artifact_id"],
            "artifact_revision": subject["artifact_revision"],
            "artifact_envelope_digest": subject["artifact_envelope_digest"],
            "artifact_sha256": subject["artifact_sha256"],
            "artifact_size_bytes": subject["artifact_size_bytes"],
            "owner_scope_digest": subject["owner_scope_digest"],
            "authenticated_principal_digest": (
                authenticated_principal_digest
            ),
            "initiating_session_digest": initiating_session_digest,
            "request_id": request_id,
            "request_provenance_digest": request_provenance_digest,
            "extension_id": subject["extension_id"],
            "extension_version": subject["extension_version"],
            "spec_digest": subject["spec_digest"],
            "source_parser_identity": subject["parser_identity"],
            "source_ruleset_digest": subject["ruleset_digest"],
            "engine_identity_digest": backend.engine_identity_digest,
            "image_id": backend.image_id,
            "isolation_conformance_digest": (
                backend.isolation_conformance_digest
            ),
            "validation_conformance_digest": (
                backend.validation_conformance_digest
            ),
            "validation_policy_revision": DYNAMIC_VALIDATION_POLICY_REVISION,
            "validation_policy_digest": DYNAMIC_VALIDATION_POLICY_DIGEST,
            "validation_harness_revision": DYNAMIC_VALIDATION_HARNESS_REVISION,
            "validation_harness_digest": backend.harness_digest,
            "test_bundle_digest": test_bundle.bundle_digest(),
        }
        values["build_identity_digest"] = dynamic_build_identity_digest(values)
        if validation_id is None:
            values["validation_id"] = (
                f"extval_{values['build_identity_digest'][:24]}"
            )
        return DynamicValidationBinding.model_validate(values, strict=True)

    @staticmethod
    def _validate_frozen_contract(
        input_schema: ExtensionObjectSchema,
        output_schema: ExtensionObjectSchema,
        test_bundle: DynamicValidationTestBundle,
    ) -> None:
        for vector in test_bundle.vectors:
            if not ExtensionDynamicValidationGate._schema_accepts(
                input_schema, vector.input_payload
            ):
                raise ExtensionDynamicValidationConflictError(
                    "dynamic validation input vector violates the Spec"
                )
            if not ExtensionDynamicValidationGate._schema_accepts(
                output_schema, vector.expected_output
            ):
                raise ExtensionDynamicValidationConflictError(
                    "dynamic validation expected output violates the Spec"
                )

    @staticmethod
    def _schema_accepts(
        schema: ExtensionObjectSchema,
        payload: dict[str, Any],
    ) -> bool:
        properties = schema.properties
        if not set(payload).issubset(properties) or not set(schema.required).issubset(
            payload
        ):
            return False
        for key, value in payload.items():
            primitive = properties[key]
            if primitive.type == "string":
                if type(value) is not str:
                    return False
                minimum = primitive.minLength or 0
                if not minimum <= len(value) <= int(primitive.maxLength or -1):
                    return False
            elif primitive.type == "integer":
                if type(value) is not int:
                    return False
                if not int(primitive.minimum) <= value <= int(primitive.maximum):
                    return False
            elif primitive.type == "number":
                if type(value) not in {int, float} or not math.isfinite(float(value)):
                    return False
                if not float(primitive.minimum) <= float(value) <= float(primitive.maximum):
                    return False
            elif primitive.type == "boolean":
                if type(value) is not bool:
                    return False
            elif primitive.type == "null":
                if value is not None:
                    return False
        return True

    @staticmethod
    def _test_contract(
        spec: Any,
        test_bundle: DynamicValidationTestBundle,
    ) -> dict[str, Any]:
        input_schema = spec.input_schema.model_dump(mode="json", by_alias=True)
        output_schema = spec.output_schema.model_dump(mode="json", by_alias=True)
        return {
            "schema_version": TEST_CONTRACT_SCHEMA_VERSION,
            "bundle_id": test_bundle.bundle_id,
            "origin": test_bundle.origin,
            "vector_count": len(test_bundle.vectors),
            "test_bundle_digest": test_bundle.bundle_digest(),
            "spec_digest": spec.digest(),
            "input_schema_digest": ExtensionDynamicValidationGate._digest(
                input_schema
            ),
            "output_schema_digest": ExtensionDynamicValidationGate._digest(
                output_schema
            ),
        }

    def _mark_indeterminate(
        self,
        validation_id: str,
        owner_scope: str,
        principal_digest: str,
        session_digest: str,
        failure_code: str,
    ) -> dict[str, Any]:
        finalized: dict[str, Any] | None = None

        def mark(current: dict[str, Any]) -> None:
            nonlocal finalized
            self._validate_state(current)
            record = self._record_by_id(current, validation_id)
            self._require_owner(
                record, owner_scope, principal_digest, session_digest
            )
            if record["stage"] != "DYNAMIC_VALIDATION_STARTED":
                raise ExtensionDynamicValidationConflictError(
                    "dynamic validation indeterminate transition raced"
                )
            now = self._now_iso()
            record["stage"] = "DYNAMIC_VALIDATION_INDETERMINATE"
            record["failure_code"] = failure_code
            record["updated_at"] = now
            current["updated_at"] = now
            finalized = record

        self._mutate(mark)
        if finalized is None:
            raise ExtensionDynamicValidationStorageError(
                "dynamic validation indeterminate state was not persisted"
            )
        return self._public_record(finalized)

    def _public_record(
        self,
        record: dict[str, Any],
        *,
        operation_replayed: bool = False,
        validation_existing: bool = False,
    ) -> dict[str, Any]:
        self._validate_record(record)
        binding = parse_dynamic_validation_binding(record["binding"])
        report = (
            parse_dynamic_validation_report(record["report"])
            if isinstance(record["report"], dict)
            else None
        )
        effective, health = self._effective_status(record)
        if report is None:
            candidate = "indeterminate"
            statuses = {
                "unit_checks_status": "not_checked",
                "contract_checks_status": "not_checked",
                "security_runtime_checks_status": "not_checked",
                "fuzz_checks_status": "not_checked",
                "behavior_verification_status": "not_checked",
            }
            issues: list[str] = []
            vector_count = len(record["test_bundle"]["vectors"])
            fuzz_count = 0
            report_integrity = "unavailable"
        else:
            candidate = report.candidate_execution_status
            statuses = {
                key: getattr(report, key)
                for key in (
                    "unit_checks_status",
                    "contract_checks_status",
                    "security_runtime_checks_status",
                    "fuzz_checks_status",
                    "behavior_verification_status",
                )
            }
            issues = list(report.issue_codes)
            vector_count = report.vector_count
            fuzz_count = report.fuzz_case_count
            report_integrity = "validated"
        return {
            "schema_version": PUBLIC_RECORD_SCHEMA,
            "validation_id": record["validation_id"],
            "isolated_run_id": binding.isolated_run_id,
            "source_check_id": binding.source_check_id,
            "artifact_id": binding.artifact_id,
            "artifact_revision": binding.artifact_revision,
            "artifact_sha256": binding.artifact_sha256,
            "candidate_id": binding.candidate_id,
            "candidate_revision": binding.candidate_revision,
            "extension_id": binding.extension_id,
            "extension_version": binding.extension_version,
            "stored_stage": record["stage"],
            "effective_status": effective,
            "candidate_execution_status": candidate,
            **statuses,
            "vector_count": vector_count,
            "fuzz_case_count": fuzz_count,
            "issue_codes": issues,
            "failure_code": record["failure_code"],
            "test_bundle_digest": binding.test_bundle_digest,
            "test_contract_digest": record["test_contract_digest"],
            "build_identity_digest": binding.build_identity_digest,
            "isolated_runner_binding_digest": (
                binding.isolated_runner_binding_digest
            ),
            "isolated_runner_report_digest": (
                binding.isolated_runner_report_digest
            ),
            "source_check_binding_digest": binding.source_check_binding_digest,
            "source_check_report_digest": binding.source_check_report_digest,
            "engine_identity_digest": binding.engine_identity_digest,
            "image_id": binding.image_id,
            "isolation_conformance_digest": (
                binding.isolation_conformance_digest
            ),
            "validation_conformance_digest": (
                binding.validation_conformance_digest
            ),
            "validation_policy_revision": binding.validation_policy_revision,
            "validation_policy_digest": binding.validation_policy_digest,
            "validation_harness_revision": binding.validation_harness_revision,
            "validation_harness_digest": binding.validation_harness_digest,
            "report_integrity_status": report_integrity,
            "operational_health": health,
            "operation_replayed": operation_replayed,
            "validation_existing": validation_existing,
            "signature_status": "not_implemented",
            "activation_status": "not_installed",
            "capability_registry_visible": False,
            "canary_status": "not_started",
            "promotion_authorized": False,
            "policy_effect": "none",
            "authority": self._authority(),
        }

    @staticmethod
    def _effective_status(record: dict[str, Any]) -> tuple[str, str]:
        if record["stage"] in {
            "DYNAMIC_VALIDATION_CLAIMED",
            "DYNAMIC_VALIDATION_STARTED",
            "DYNAMIC_VALIDATION_INDETERMINATE",
        }:
            return "DYNAMIC_VALIDATION_INDETERMINATE", "degraded"
        return str(record["stage"]), "available"

    def _read_valid_state(self) -> dict[str, Any]:
        try:
            state = self.state_store.read_json(STATE_FILE)
            if not state:
                state = self._initial_state()
            self._validate_state(state)
            return state
        except ExtensionDynamicValidationError:
            raise
        except Exception as exc:
            raise ExtensionDynamicValidationStorageError(
                "dynamic validation state is unavailable"
            ) from exc

    def _read_owner_state(
        self,
        user_id: str,
        workspace_id: str,
    ) -> tuple[str, str, str, dict[str, Any]]:
        selected_user = self._required_text(user_id, "user_id", 240)
        selected_workspace = self._required_text(
            workspace_id, "workspace_id", 240
        )
        try:
            snapshot = self.state_store.read_snapshot(
                ["local_world.json", STATE_FILE]
            )
            self._assert_workspace_document(
                snapshot["local_world.json"], selected_workspace
            )
            state = snapshot[STATE_FILE] or self._initial_state()
            self._validate_state(state)
        except ExtensionDynamicValidationError:
            raise
        except Exception as exc:
            raise ExtensionDynamicValidationStorageError(
                "dynamic validation owner state is unavailable"
            ) from exc
        return (
            selected_user,
            selected_workspace,
            artifact_owner_scope_digest(selected_user, selected_workspace),
            state,
        )

    def _mutate(self, mutator: Callable[[dict[str, Any]], None]) -> None:
        try:
            self.state_store.mutate_json(STATE_FILE, mutator)
        except ExtensionDynamicValidationError:
            raise
        except Exception as exc:
            raise ExtensionDynamicValidationStorageError(
                "dynamic validation state is unavailable"
            ) from exc

    @staticmethod
    def _initial_state() -> dict[str, Any]:
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "validations": {},
            "binding_index": {},
            "operation_index": {},
            "validation_count": 0,
            "backend_snapshot": None,
            "updated_at": None,
        }

    def _initialize_mutation_state(self, state: dict[str, Any]) -> None:
        if not state:
            state.update(self._initial_state())

    def _validate_state(self, state: dict[str, Any]) -> None:
        if (
            not isinstance(state, dict)
            or state.get("_state_corrupt") is True
            or not _STATE_FIELDS.issubset(state)
            or bool(set(state) - _STATE_FIELDS - _STATE_METADATA_FIELDS)
            or state.get("schema_version") != STATE_SCHEMA_VERSION
            or not isinstance(state.get("validations"), dict)
            or not isinstance(state.get("binding_index"), dict)
            or not isinstance(state.get("operation_index"), dict)
            or type(state.get("validation_count")) is not int
            or state["validation_count"] != len(state["validations"])
            or len(state["validations"]) > MAX_VALIDATIONS
            or len(state["operation_index"]) > MAX_OPERATIONS
            or (
                state.get("updated_at") is not None
                and not isinstance(state.get("updated_at"), str)
            )
        ):
            raise ExtensionDynamicValidationStorageError(
                "dynamic validation private state is invalid"
            )
        if state["backend_snapshot"] is not None:
            self._validate_backend_snapshot(state["backend_snapshot"])
        expected_index: dict[str, str] = {}
        for validation_id, record in state["validations"].items():
            if validation_id != record.get("validation_id"):
                raise ExtensionDynamicValidationStorageError(
                    "dynamic validation map identity is invalid"
                )
            self._validate_record(record)
            digest = str(record["binding_digest"])
            if digest in expected_index:
                raise ExtensionDynamicValidationStorageError(
                    "dynamic validation binding is duplicated"
                )
            expected_index[digest] = validation_id
        if state["binding_index"] != expected_index:
            raise ExtensionDynamicValidationStorageError(
                "dynamic validation binding index is invalid"
            )
        for operation_key, operation in state["operation_index"].items():
            self._validate_operation(operation_key, operation, state)

    def _validate_record(self, record: dict[str, Any]) -> None:
        if (
            not isinstance(record, dict)
            or set(record) != _RECORD_FIELDS
            or record.get("schema_version") != RECORD_SCHEMA_VERSION
            or not _VALIDATION_ID.fullmatch(
                str(record.get("validation_id") or "")
            )
            or not _DIGEST.fullmatch(
                str(record.get("owner_scope_digest") or "")
            )
            or not _DIGEST.fullmatch(
                str(record.get("principal_digest") or "")
            )
            or not _DIGEST.fullmatch(
                str(record.get("session_digest") or "")
            )
            or not _ID.fullmatch(str(record.get("request_id") or ""))
            or not _DIGEST.fullmatch(
                str(record.get("request_provenance_digest") or "")
            )
            or not isinstance(record.get("binding"), dict)
            or not _DIGEST.fullmatch(str(record.get("binding_digest") or ""))
            or not isinstance(record.get("test_bundle"), dict)
            or not _DIGEST.fullmatch(
                str(record.get("test_bundle_digest") or "")
            )
            or not isinstance(record.get("test_contract"), dict)
            or not _DIGEST.fullmatch(
                str(record.get("test_contract_digest") or "")
            )
            or record.get("stage") not in _STAGES
            or not isinstance(record.get("created_at"), str)
            or not isinstance(record.get("updated_at"), str)
            or (
                record.get("started_at") is not None
                and not isinstance(record.get("started_at"), str)
            )
        ):
            raise ExtensionDynamicValidationStorageError(
                "dynamic validation private record is invalid"
            )
        binding = parse_dynamic_validation_binding(record["binding"])
        bundle = parse_dynamic_validation_test_bundle(record["test_bundle"])
        if (
            binding.validation_id != record["validation_id"]
            or binding.owner_scope_digest != record["owner_scope_digest"]
            or binding.authenticated_principal_digest
            != record["principal_digest"]
            or binding.initiating_session_digest != record["session_digest"]
            or binding.request_id != record["request_id"]
            or binding.request_provenance_digest
            != record["request_provenance_digest"]
            or binding.binding_digest() != record["binding_digest"]
            or bundle.bundle_digest() != record["test_bundle_digest"]
            or bundle.bundle_digest() != binding.test_bundle_digest
            or self._digest(record["test_contract"])
            != record["test_contract_digest"]
            or record["test_contract"].get("test_bundle_digest")
            != bundle.bundle_digest()
            or record["test_contract"].get("spec_digest") != binding.spec_digest
        ):
            raise ExtensionDynamicValidationStorageError(
                "dynamic validation private binding is invalid"
            )
        self._parse_time(record["created_at"])
        self._parse_time(record["updated_at"])
        if record["started_at"] is not None:
            self._parse_time(record["started_at"])
        report = record.get("report")
        if record["stage"] in {
            "DYNAMIC_VALIDATION_PASSED",
            "DYNAMIC_VALIDATION_FAILED",
        }:
            if (
                not isinstance(report, dict)
                or not _DIGEST.fullmatch(str(record.get("report_digest") or ""))
                or record.get("failure_code") is not None
                or record.get("started_at") is None
            ):
                raise ExtensionDynamicValidationStorageError(
                    "dynamic validation terminal report is invalid"
                )
            parsed = parse_dynamic_validation_report(report)
            if (
                parsed.binding.canonical_dict() != binding.canonical_dict()
                or parsed.binding_digest != record["binding_digest"]
                or parsed.report_digest() != record["report_digest"]
                or (
                    parsed.validation_status == "passed"
                    and record["stage"] != "DYNAMIC_VALIDATION_PASSED"
                )
                or (
                    parsed.validation_status == "failed"
                    and record["stage"] != "DYNAMIC_VALIDATION_FAILED"
                )
            ):
                raise ExtensionDynamicValidationStorageError(
                    "dynamic validation report identity is invalid"
                )
        elif (
            report is not None
            or record.get("report_digest") is not None
            or (
                record["stage"] == "DYNAMIC_VALIDATION_CLAIMED"
                and record.get("started_at") is not None
            )
            or (
                record["stage"] == "DYNAMIC_VALIDATION_STARTED"
                and record.get("started_at") is None
            )
            or (
                record["stage"] in {
                    "DYNAMIC_VALIDATION_CLAIMED",
                    "DYNAMIC_VALIDATION_STARTED",
                }
                and record.get("failure_code") is not None
            )
            or (
                record["stage"] == "DYNAMIC_VALIDATION_INDETERMINATE"
                and (
                    record.get("failure_code") not in _FAILURE_CODES
                    or record.get("started_at") is None
                )
            )
        ):
            raise ExtensionDynamicValidationStorageError(
                "dynamic validation non-report state is invalid"
            )

    def _validate_backend_snapshot(
        self, snapshot: Any
    ) -> DynamicValidationBackendStatus:
        if not isinstance(snapshot, dict) or set(snapshot) != _SNAPSHOT_FIELDS:
            raise ExtensionDynamicValidationStorageError(
                "dynamic validation backend snapshot is invalid"
            )
        if snapshot["schema_version"] != BACKEND_SNAPSHOT_SCHEMA_VERSION:
            raise ExtensionDynamicValidationStorageError(
                "dynamic validation backend snapshot schema is invalid"
            )
        self._parse_time(snapshot["observed_at"])
        status = parse_dynamic_validation_backend_status(
            snapshot["backend_status"]
        )
        expected = hashlib.sha256(status.canonical_bytes()).hexdigest()
        if snapshot["status_digest"] != expected:
            raise ExtensionDynamicValidationStorageError(
                "dynamic validation backend snapshot digest is invalid"
            )
        return status

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
            or operation.get("kind") != "dynamic_validation"
            or not _DIGEST.fullmatch(
                str(operation.get("request_digest") or "")
            )
            or not _VALIDATION_ID.fullmatch(
                str(operation.get("validation_id") or "")
            )
            or operation["validation_id"] not in state["validations"]
            or not isinstance(operation.get("recorded_at"), str)
        ):
            raise ExtensionDynamicValidationStorageError(
                "dynamic validation operation binding is invalid"
            )
        record = self._record_by_id(
            state,
            str(operation["validation_id"]),
        )
        binding = parse_dynamic_validation_binding(record["binding"])
        expected_request = self._request_digest(
            owner_scope=str(record["owner_scope_digest"]),
            isolated_run_id=binding.isolated_run_id,
            artifact_revision=binding.artifact_revision,
            artifact_sha256=binding.artifact_sha256,
            source_check_report_digest=binding.source_check_report_digest,
            isolated_runner_report_digest=(
                binding.isolated_runner_report_digest
            ),
            test_bundle_digest=binding.test_bundle_digest,
            authenticated_principal_digest=(
                binding.authenticated_principal_digest
            ),
            initiating_session_digest=binding.initiating_session_digest,
            request_id=binding.request_id,
            request_provenance_digest=binding.request_provenance_digest,
        )
        if operation["request_digest"] != expected_request:
            raise ExtensionDynamicValidationStorageError(
                "dynamic validation operation semantic binding is invalid"
            )
        self._parse_time(operation["recorded_at"])

    def _record_operation(
        self,
        state: dict[str, Any],
        *,
        operation_key: str,
        request_digest: str,
        validation_id: str,
    ) -> None:
        if len(state["operation_index"]) >= MAX_OPERATIONS:
            raise ExtensionDynamicValidationConflictError(
                "dynamic validation operation capacity is exhausted"
            )
        now = self._now_iso()
        state["operation_index"][operation_key] = {
            "kind": "dynamic_validation",
            "request_digest": request_digest,
            "validation_id": validation_id,
            "recorded_at": now,
        }
        state["updated_at"] = now

    def _existing_operation(
        self,
        *,
        state: dict[str, Any],
        operation_key: str,
        request_digest: str,
        owner_scope_digest: str,
        principal_digest: str,
        session_digest: str,
    ) -> dict[str, Any] | None:
        operation = state["operation_index"].get(operation_key)
        if operation is None:
            return None
        self._validate_operation(operation_key, operation, state)
        if operation["request_digest"] != request_digest:
            raise ExtensionDynamicValidationConflictError(
                "dynamic validation operation identity was rebound"
            )
        record = self._record_by_id(state, str(operation["validation_id"]))
        self._require_owner(
            record,
            owner_scope_digest,
            principal_digest,
            session_digest,
        )
        return record

    @staticmethod
    def _record_by_id(
        state: dict[str, Any], validation_id: str
    ) -> dict[str, Any]:
        record = state["validations"].get(validation_id)
        if not isinstance(record, dict):
            raise ExtensionDynamicValidationNotFoundError(
                "dynamic validation was not found"
            )
        return record

    @staticmethod
    def _require_owner(
        record: dict[str, Any],
        owner_scope: str,
        principal_digest: str,
        session_digest: str,
    ) -> None:
        if (
            record.get("owner_scope_digest") != owner_scope
            or record.get("principal_digest") != principal_digest
            or record.get("session_digest") != session_digest
        ):
            raise ExtensionDynamicValidationNotFoundError(
                "dynamic validation was not found"
            )

    def _authorize_active(self, supplied_token: str) -> str:
        if not self.enabled:
            raise ExtensionDynamicValidationUnavailableError(
                "dynamic validation is disabled by policy"
            )
        if not self.control_token:
            raise ExtensionDynamicValidationUnavailableError(
                "dynamic validation control token is not configured"
            )
        selected = str(supplied_token or "").strip()
        if not selected or not hmac.compare_digest(selected, self.control_token):
            raise ExtensionDynamicValidationUnauthorizedError(
                "dynamic validation control token is invalid"
            )
        return selected

    @staticmethod
    def _assert_workspace_document(
        local_world: dict[str, Any], workspace_id: str
    ) -> None:
        current = str(local_world.get("current_project") or "").strip()
        if not current or current != workspace_id:
            raise ExtensionDynamicValidationConflictError(
                "workspace must match the current local Veyra scope"
            )

    @staticmethod
    def _request_digest(**values: Any) -> str:
        return ExtensionDynamicValidationGate._digest(
            {
                "schema_version": (
                    "veyra.phase6.dynamic_validation_request_identity.v1"
                ),
                **values,
            }
        )

    @staticmethod
    def _operation_key(
        principal_digest: str,
        session_digest: str,
        operation_id: str,
    ) -> str:
        return hashlib.sha256(
            (
                "veyra.phase6.dynamic_validation_operation.v1\x00"
                + principal_digest
                + "\x00"
                + session_digest
                + "\x00"
                + operation_id
            ).encode("utf-8")
        ).hexdigest()

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
    def _required_text(value: Any, field: str, maximum: int) -> str:
        if not isinstance(value, str):
            raise TypeError(f"{field} must be a string")
        selected = value.strip()
        if (
            not selected
            or len(selected.encode("utf-8")) > maximum
            or "\x00" in selected
        ):
            raise ValueError(f"{field} is invalid")
        if field in {"operation_id", "request_id"} and not _ID.fullmatch(
            selected
        ):
            raise ValueError(f"{field} is invalid")
        return selected

    @staticmethod
    def _required_revision(value: Any) -> int:
        if type(value) is not int or not 1 <= value <= 2_147_483_647:
            raise ValueError("expected_artifact_revision is invalid")
        return value

    @staticmethod
    def _required_digest(value: Any, field: str) -> str:
        if not isinstance(value, str) or not _DIGEST.fullmatch(value):
            raise ValueError(f"{field} must be a SHA-256 digest")
        return value

    @staticmethod
    def _required_run_id(value: Any) -> str:
        if not isinstance(value, str) or not _RUN_ID.fullmatch(value):
            raise ValueError("isolated_run_id is invalid")
        return value

    @staticmethod
    def _required_validation_id(value: Any) -> str:
        if not isinstance(value, str) or not _VALIDATION_ID.fullmatch(value):
            raise ValueError("validation_id is invalid")
        return value

    def _now_iso(self) -> str:
        selected = self._now()
        if not isinstance(selected, datetime) or selected.tzinfo is None:
            raise ExtensionDynamicValidationStorageError(
                "dynamic validation clock is invalid"
            )
        value = selected.astimezone(timezone.utc)
        if value.microsecond:
            return value.isoformat(timespec="microseconds").replace(
                "+00:00", "Z"
            )
        return value.isoformat(timespec="seconds").replace("+00:00", "Z")

    @staticmethod
    def _parse_time(value: Any) -> datetime:
        if not isinstance(value, str) or not value.endswith("Z"):
            raise ValueError("timestamp must be canonical UTC")
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
        if parsed.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        selected = parsed.astimezone(timezone.utc)
        canonical = (
            selected.isoformat(timespec="microseconds").replace("+00:00", "Z")
            if selected.microsecond
            else selected.isoformat(timespec="seconds").replace("+00:00", "Z")
        )
        if canonical != value:
            raise ValueError("timestamp must be canonical UTC")
        return selected

    @staticmethod
    def _env_enabled(value: str) -> bool:
        return str(value or "").strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _authority() -> dict[str, bool]:
        return DynamicValidationAuthority().model_dump(mode="json")

    @staticmethod
    def _safe_issue(exc: Exception) -> str:
        if isinstance(exc, ExtensionDynamicValidationStorageError):
            return "extension_dynamic_validation_state_invalid"
        return f"extension_dynamic_validation_unavailable:{type(exc).__name__}"

    @staticmethod
    def _unavailable_backend_status() -> DynamicValidationBackendStatus:
        return DynamicValidationBackendStatus(
            schema_version="veyra.phase6.dynamic_validation_backend_status.v1",
            backend_kind="docker_cli",
            availability="unavailable",
            reason_code="configuration_missing",
            engine_identity_digest=None,
            image_id=None,
            isolation_conformance_digest=None,
            validation_conformance_digest=None,
            harness_revision=DYNAMIC_VALIDATION_HARNESS_REVISION,
            harness_digest="0" * 64,
            validation_policy_revision=DYNAMIC_VALIDATION_POLICY_REVISION,
            validation_policy_digest=DYNAMIC_VALIDATION_POLICY_DIGEST,
            conformance_certified=False,
            authority=DynamicValidationAuthority(),
        )


__all__ = [
    "BACKEND_SNAPSHOT_TTL_SECONDS",
    "ExtensionDynamicValidationConflictError",
    "ExtensionDynamicValidationError",
    "ExtensionDynamicValidationGate",
    "ExtensionDynamicValidationNotFoundError",
    "ExtensionDynamicValidationStorageError",
    "ExtensionDynamicValidationUnauthorizedError",
    "ExtensionDynamicValidationUnavailableError",
    "STATE_FILE",
    "VALIDATION_ENABLE_ENV",
    "VALIDATION_TOKEN_ENV",
]
