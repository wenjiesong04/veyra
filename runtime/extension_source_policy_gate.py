from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
import unicodedata
from typing import Any, Callable

from core.world_state import WorldStateStore
from interface.extension_artifact import artifact_owner_scope_digest
from interface.extension_source_check import (
    EXTENSION_SOURCE_CHECK_BINDING_SCHEMA_VERSION,
    EXTENSION_SOURCE_CHECK_POLICY_REVISION,
    EXTENSION_SOURCE_CHECKER_REVISION,
    EXTENSION_SOURCE_PARSER_IDENTITY,
    EXTENSION_SOURCE_RULESET_DIGEST,
    ExtensionSourceCheckBinding,
    ExtensionSourceCheckReport,
    parse_extension_source_check_binding,
    parse_extension_source_check_report,
)
from runtime.extension_artifact_quarantine import (
    ExtensionArtifactConflictError,
    ExtensionArtifactError,
    ExtensionArtifactQuarantine,
    ExtensionArtifactStorageError,
)
from runtime.extension_source_checker import check_extension_source
from runtime.extension_spec_quarantine import (
    ExtensionSpecConflictError,
    ExtensionSpecNotFoundError,
    ExtensionSpecStorageError,
)


STATE_FILE = "phase6_extension_source_check_state.json"
STATE_SCHEMA_VERSION = "veyra.phase6.extension_source_check_state.v1"
RECORD_SCHEMA_VERSION = (
    "veyra.phase6.extension_source_check_private_record.v1"
)
PUBLIC_STATUS_SCHEMA = (
    "veyra.phase6.extension_source_check_status.v1"
)
PUBLIC_CHECK_SCHEMA = (
    "veyra.phase6.extension_source_check_record.v1"
)
PUBLIC_LIST_SCHEMA = "veyra.phase6.extension_source_check_list.v1"
PUBLIC_INTEGRITY_SCHEMA = (
    "veyra.phase6.extension_source_check_integrity.v1"
)
MAX_CHECKS = 200
MAX_OPERATIONS = 2_000

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,239}$")
_CHECK_ID = re.compile(r"^extcheck_[0-9a-f]{24}$")
_ARTIFACT_ID = re.compile(r"^extart_[0-9a-f]{24}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_STAGES = frozenset(
    {
        "SOURCE_CHECK_CLAIMED",
        "SOURCE_CHECK_PASSED",
        "SOURCE_CHECK_FAILED",
        "SOURCE_CHECK_INDETERMINATE",
    }
)
_TERMINAL_STAGES = frozenset(
    {
        "SOURCE_CHECK_PASSED",
        "SOURCE_CHECK_FAILED",
        "SOURCE_CHECK_INDETERMINATE",
    }
)
_FAILURE_CODES = frozenset(
    {
        "checker_unavailable",
        "prerequisite_changed",
        "checker_report_invalid",
    }
)
_STATE_FIELDS = frozenset(
    {
        "schema_version",
        "checks",
        "artifact_index",
        "operation_index",
        "check_count",
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
        "check_id",
        "owner_scope_digest",
        "binding",
        "binding_digest",
        "stage",
        "report",
        "report_digest",
        "failure_code",
        "created_at",
        "updated_at",
    }
)
_OPERATION_FIELDS = frozenset(
    {
        "kind",
        "request_digest",
        "check_id",
        "recorded_at",
    }
)


class ExtensionSourceCheckError(RuntimeError):
    """Base error for the private non-executing source policy gate."""


class ExtensionSourceCheckConflictError(ExtensionSourceCheckError):
    """A CAS, replay, lifecycle, or capacity binding conflicted."""


class ExtensionSourceCheckNotFoundError(ExtensionSourceCheckError):
    """A source check was absent or outside the logical owner scope."""


class ExtensionSourceCheckStorageError(ExtensionSourceCheckError):
    """Private source-check state or prerequisite evidence is unavailable."""


class ExtensionSourcePolicyGate:
    """Durable Phase 6.2c AST policy gate with zero candidate execution.

    The checker only receives bytes reopened from the private Artifact
    quarantine.  This runtime never accepts source, paths, commands, rulesets,
    or environments from HTTP and never imports or invokes candidate code.
    """

    def __init__(
        self,
        *,
        state_store: WorldStateStore,
        artifact_quarantine: ExtensionArtifactQuarantine,
        checker: Callable[..., ExtensionSourceCheckReport] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.state_store = state_store
        self.artifact_quarantine = artifact_quarantine
        self.checker = checker or check_extension_source
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._isolated_runner_projection: Callable[..., dict[str, Any]] | None = (
            None
        )

    def bind_isolated_runner_projection(
        self,
        resolver: Callable[..., dict[str, Any]],
    ) -> None:
        """Attach a fault-contained read projection after runtime assembly."""

        if not callable(resolver):
            raise TypeError("isolated runner projection must be callable")
        self._isolated_runner_projection = resolver

    def status(self) -> dict[str, Any]:
        try:
            state = self._read_valid_state()
            counts = {stage: 0 for stage in sorted(_STAGES)}
            for record in state["checks"].values():
                counts[str(record["stage"])] += 1
            storage = {
                "status": "ready",
                "issue": None,
                "check_count": len(state["checks"]),
                "operation_count": len(state["operation_index"]),
                "counts": counts,
            }
            health = "available"
        except Exception as exc:
            storage = {
                "status": "fault",
                "issue": self._safe_issue(exc),
                "check_count": 0,
                "operation_count": 0,
                "counts": {},
            }
            health = "degraded"
        return {
            "schema_version": PUBLIC_STATUS_SCHEMA,
            "phase": "6.2c",
            "status": (
                "technical_complete_non_executing_source_gate_only"
                if health == "available"
                else "fail_closed"
            ),
            "operational_health": health,
            "completion_scope": (
                "exact artifact syntax and fixed AST policy only"
            ),
            "checker_kind": "in_process_non_executing_ast",
            "checker_revision": EXTENSION_SOURCE_CHECKER_REVISION,
            "source_check_policy_revision": (
                EXTENSION_SOURCE_CHECK_POLICY_REVISION
            ),
            "parser_identity": EXTENSION_SOURCE_PARSER_IDENTITY,
            "ruleset_digest": EXTENSION_SOURCE_RULESET_DIGEST,
            "storage": storage,
            "authority": self._authority(),
            "next_stage": {
                "isolated_generation": "not_started",
                "trusted_isolated_runner": "not_started",
                "unit_checks": "not_started",
                "contract_runtime_checks": "not_started",
                "security_runtime_checks": "not_started",
                "fuzz_checks": "not_started",
                "behavior_verification": "not_started",
                "signature_verification": "not_started",
                "read_only_execution_canary": "not_started",
                "scoped_execution_canary": "not_started",
                "promotion": "not_started",
            },
        }

    def start(
        self,
        *,
        artifact_id: str,
        user_id: str,
        workspace_id: str,
        expected_artifact_revision: int,
        expected_artifact_sha256: str,
        operation_id: str,
    ) -> dict[str, Any]:
        selected_artifact = self._required_artifact_id(artifact_id)
        selected_revision = self._required_revision(
            expected_artifact_revision
        )
        selected_digest = self._required_digest(
            expected_artifact_sha256,
            "expected_artifact_sha256",
        )
        selected_operation = self._required_id(
            operation_id,
            "operation_id",
        )
        (
            selected_user,
            selected_workspace,
            owner_scope,
            initial_state,
        ) = self._read_owner_state(user_id, workspace_id)
        operation_key = self._operation_key(
            owner_scope,
            selected_operation,
        )
        request_digest = self._request_digest(
            owner_scope,
            selected_artifact,
            selected_revision,
            selected_digest,
        )

        replay = self._existing_operation(
            state=initial_state,
            operation_key=operation_key,
            request_digest=request_digest,
            owner_scope_digest=owner_scope,
        )
        if replay is not None:
            return self._public_check_for_owner(
                replay,
                user_id=selected_user,
                workspace_id=selected_workspace,
                operation_replayed=True,
            )

        subject = self._source_subject(
            artifact_id=selected_artifact,
            user_id=selected_user,
            workspace_id=selected_workspace,
            expected_artifact_revision=selected_revision,
            expected_artifact_sha256=selected_digest,
        )
        binding = self._binding_from_subject(subject)
        binding_digest = binding.binding_digest()
        artifact_key = self._artifact_key(
            owner_scope,
            selected_artifact,
        )
        check_id = self._check_id(artifact_key)
        claimed: dict[str, Any] | None = None
        operation_replayed = False
        check_existing = False

        def claim(state: dict[str, Any]) -> None:
            nonlocal claimed
            nonlocal operation_replayed
            nonlocal check_existing
            self._assert_current_workspace(selected_workspace)
            self._validate_state(state)
            existing_operation = state["operation_index"].get(
                operation_key
            )
            if existing_operation is not None:
                self._validate_operation(
                    operation_key,
                    existing_operation,
                    state,
                )
                if (
                    existing_operation["request_digest"]
                    != request_digest
                ):
                    raise ExtensionSourceCheckConflictError(
                        "source-check operation identity was rebound"
                    )
                record = self._record_by_id(
                    state,
                    str(existing_operation["check_id"]),
                )
                self._require_owner(record, owner_scope)
                claimed = record
                operation_replayed = True
                return

            indexed = state["artifact_index"].get(artifact_key)
            if indexed is not None:
                record = self._record_by_id(state, str(indexed))
                self._require_owner(record, owner_scope)
                if (
                    record["check_id"] != check_id
                    or record["binding_digest"] != binding_digest
                ):
                    raise ExtensionSourceCheckConflictError(
                        "artifact already has a different current source check"
                    )
                self._record_operation(
                    state,
                    operation_key=operation_key,
                    request_digest=request_digest,
                    check_id=record["check_id"],
                )
                claimed = record
                check_existing = True
                return

            if (
                len(state["checks"]) >= MAX_CHECKS
                or len(state["operation_index"]) >= MAX_OPERATIONS
            ):
                raise ExtensionSourceCheckConflictError(
                    "source-check capacity is exhausted"
                )
            created_at = self._now_iso()
            record = {
                "schema_version": RECORD_SCHEMA_VERSION,
                "check_id": check_id,
                "owner_scope_digest": owner_scope,
                "binding": binding.canonical_dict(),
                "binding_digest": binding_digest,
                "stage": "SOURCE_CHECK_CLAIMED",
                "report": None,
                "report_digest": None,
                "failure_code": None,
                "created_at": created_at,
                "updated_at": created_at,
            }
            state["checks"][check_id] = record
            state["artifact_index"][artifact_key] = check_id
            state["check_count"] = len(state["checks"])
            self._record_operation(
                state,
                operation_key=operation_key,
                request_digest=request_digest,
                check_id=check_id,
            )
            claimed = record

        self._mutate(claim)
        if claimed is None:
            raise ExtensionSourceCheckStorageError(
                "source-check claim produced no record"
            )
        if operation_replayed or check_existing:
            return self._public_check_for_owner(
                claimed,
                user_id=user_id,
                workspace_id=workspace_id,
                operation_replayed=operation_replayed,
                check_existing=check_existing,
            )

        report: ExtensionSourceCheckReport | None = None
        failure_code: str | None = None
        try:
            candidate_report = self.checker(
                source=subject["source_bytes"],
                spec=subject["source_check_spec"],
                binding=binding,
                checked_at=self._now_iso(),
            )
        except Exception:
            failure_code = "checker_unavailable"
            report = None
        else:
            try:
                report = parse_extension_source_check_report(
                    candidate_report
                )
                self._validate_report_binding(report, binding)
            except Exception:
                failure_code = "checker_report_invalid"
                report = None

        try:
            final_subject = self._source_subject(
                artifact_id=selected_artifact,
                user_id=selected_user,
                workspace_id=selected_workspace,
                expected_artifact_revision=selected_revision,
                expected_artifact_sha256=selected_digest,
            )
            final_binding = self._binding_from_subject(final_subject)
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
            except ExtensionSourceCheckConflictError:
                failure_code = "prerequisite_changed"
                report = None
            record = self._record_by_id(state, check_id)
            self._require_owner(record, owner_scope)
            if record["binding_digest"] != binding_digest:
                raise ExtensionSourceCheckStorageError(
                    "source-check binding changed before completion"
                )
            if record["stage"] != "SOURCE_CHECK_CLAIMED":
                finalized = record
                return
            if report is None:
                record["stage"] = "SOURCE_CHECK_INDETERMINATE"
                record["report"] = None
                record["report_digest"] = None
                record["failure_code"] = (
                    failure_code or "checker_report_invalid"
                )
            else:
                record["stage"] = (
                    "SOURCE_CHECK_PASSED"
                    if report.check_status == "passed"
                    else "SOURCE_CHECK_FAILED"
                )
                record["report"] = report.canonical_dict()
                record["report_digest"] = report.report_digest()
                record["failure_code"] = None
            record["updated_at"] = self._now_iso()
            finalized = record

        self._mutate(finish)
        if finalized is None:
            raise ExtensionSourceCheckStorageError(
                "source-check completion produced no record"
            )
        self._audit(finalized)
        return self._public_check_for_owner(
            finalized,
            user_id=selected_user,
            workspace_id=selected_workspace,
        )

    def get(
        self,
        *,
        check_id: str,
        user_id: str,
        workspace_id: str,
    ) -> dict[str, Any]:
        selected_user, selected_workspace, owner_scope, state = (
            self._read_owner_state(user_id, workspace_id)
        )
        record = self._record_by_id(
            state,
            self._required_check_id(check_id),
        )
        self._require_owner(record, owner_scope)
        return self._public_check_for_owner(
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
            for record in state["checks"].values()
            if record["owner_scope_digest"] == owner_scope
        ]
        records.sort(
            key=lambda item: (
                str(item["updated_at"]),
                str(item["check_id"]),
            ),
            reverse=True,
        )
        selected = records[:limit]
        return {
            "schema_version": PUBLIC_LIST_SCHEMA,
            "count": len(selected),
            "checks": [
                self._public_check_for_owner(
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
        check_id: str,
        user_id: str,
        workspace_id: str,
    ) -> dict[str, Any]:
        selected_user, selected_workspace, owner_scope, state = (
            self._read_owner_state(user_id, workspace_id)
        )
        record = self._record_by_id(
            state,
            self._required_check_id(check_id),
        )
        self._require_owner(record, owner_scope)
        binding = parse_extension_source_check_binding(
            record["binding"]
        )
        try:
            self._source_subject(
                artifact_id=binding.artifact_id,
                user_id=selected_user,
                workspace_id=selected_workspace,
                expected_artifact_revision=binding.artifact_revision,
                expected_artifact_sha256=binding.artifact_sha256,
            )
        except (
            ExtensionArtifactConflictError,
            ExtensionSpecConflictError,
            ExtensionSpecNotFoundError,
        ):
            public = self._public_check(record)
            return {
                **public,
                "schema_version": PUBLIC_INTEGRITY_SCHEMA,
                "effective_status": "BLOCKED_PREREQUISITE",
                "operational_health": "degraded",
                "status": "blocked",
                "state_mutated": False,
            }
        except Exception as exc:
            raise ExtensionSourceCheckStorageError(
                "source-check prerequisite integrity is unavailable"
            ) from exc
        public = self._public_check(record)
        return {
            **public,
            "schema_version": PUBLIC_INTEGRITY_SCHEMA,
            "status": "source_check_integrity_passed",
            "report_integrity_status": "validated",
            "state_mutated": False,
        }

    def isolated_runner_subject(
        self,
        *,
        check_id: str,
        user_id: str,
        workspace_id: str,
        expected_artifact_revision: int,
        expected_artifact_sha256: str,
        expected_source_check_report_digest: str,
    ) -> dict[str, Any]:
        """Return one exact passed-check subject to the isolated runner gate.

        This is an in-process-only boundary.  It revalidates the logical
        owner, current workspace, terminal source-check report, immutable
        report digest, artifact CAS, artifact bytes, Spec gate, lifecycle and
        expiry before returning the private bytes.  It grants no HTTP access
        to source, paths, commands, environments, images, tests, signing or
        production execution.
        """

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
        selected_user, selected_workspace, owner_scope, state = (
            self._read_owner_state(user_id, workspace_id)
        )
        record = self._record_by_id(
            state,
            self._required_check_id(check_id),
        )
        self._require_owner(record, owner_scope)
        if (
            record["stage"] != "SOURCE_CHECK_PASSED"
            or record.get("report_digest") != selected_report_digest
            or not isinstance(record.get("report"), dict)
        ):
            raise ExtensionSourceCheckConflictError(
                "isolated runner requires the exact passed source check"
            )
        binding = parse_extension_source_check_binding(record["binding"])
        report = parse_extension_source_check_report(record["report"])
        self._validate_report_binding(report, binding)
        if (
            binding.artifact_revision != selected_revision
            or binding.artifact_sha256 != selected_artifact_digest
            or report.report_digest() != selected_report_digest
        ):
            raise ExtensionSourceCheckConflictError(
                "isolated runner prerequisite CAS changed"
            )
        subject = self._source_subject(
            artifact_id=binding.artifact_id,
            user_id=selected_user,
            workspace_id=selected_workspace,
            expected_artifact_revision=selected_revision,
            expected_artifact_sha256=selected_artifact_digest,
        )
        rebound = self._binding_from_subject(subject)
        if rebound.binding_digest() != record["binding_digest"]:
            raise ExtensionSourceCheckConflictError(
                "isolated runner source-check binding changed"
            )
        return {
            "schema_version": (
                "veyra.phase6.extension_isolated_runner_subject.v1"
            ),
            "check_id": record["check_id"],
            "source_check_binding": binding.canonical_dict(),
            "source_check_binding_digest": record["binding_digest"],
            "source_check_report": report.canonical_dict(),
            "source_check_report_digest": selected_report_digest,
            "candidate_id": binding.candidate_id,
            "candidate_revision": binding.candidate_revision,
            "artifact_id": binding.artifact_id,
            "artifact_revision": binding.artifact_revision,
            "artifact_sha256": binding.artifact_sha256,
            "artifact_size_bytes": binding.source_size_bytes,
            "artifact_envelope_digest": subject[
                "artifact_envelope_digest"
            ],
            "owner_scope_digest": binding.owner_scope_digest,
            "extension_id": binding.extension_id,
            "extension_version": binding.extension_version,
            "spec_digest": binding.spec_digest,
            "extension_policy_revision": (
                binding.extension_policy_revision
            ),
            "artifact_policy_revision": binding.artifact_policy_revision,
            "source_check_policy_revision": (
                binding.source_check_policy_revision
            ),
            "checker_revision": report.checker_revision,
            "parser_identity": binding.parser_identity,
            "ruleset_digest": binding.ruleset_digest,
            "source_bytes": subject["source_bytes"],
            "user_id": selected_user,
            "workspace_id": selected_workspace,
            "authority": self._authority(),
        }

    def projection_for_artifact(
        self,
        *,
        artifact_id: str,
        user_id: str,
        workspace_id: str,
    ) -> dict[str, Any]:
        return self.projections_for_artifacts(
            artifact_ids=[artifact_id],
            user_id=user_id,
            workspace_id=workspace_id,
        )[artifact_id]

    def projections_for_artifacts(
        self,
        *,
        artifact_ids: list[str],
        user_id: str,
        workspace_id: str,
    ) -> dict[str, dict[str, Any]]:
        if (
            not isinstance(artifact_ids, list)
            or len(artifact_ids) > 100
            or any(not isinstance(item, str) for item in artifact_ids)
        ):
            raise ValueError("artifact_ids must be a bounded string list")
        selected_ids = [
            self._required_artifact_id(item) for item in artifact_ids
        ]
        if len(set(selected_ids)) != len(selected_ids):
            raise ValueError("artifact_ids must be unique")
        selected_user, selected_workspace, owner_scope, state = (
            self._read_owner_state(user_id, workspace_id)
        )
        projections: dict[str, dict[str, Any]] = {}
        for artifact_id in selected_ids:
            artifact_key = self._artifact_key(owner_scope, artifact_id)
            check_id = state["artifact_index"].get(artifact_key)
            if check_id is None:
                projections[artifact_id] = self.empty_projection()
                continue
            record = self._record_by_id(state, str(check_id))
            self._require_owner(record, owner_scope)
            projections[artifact_id] = self._projection(
                self._public_check_for_owner(
                    record,
                    user_id=selected_user,
                    workspace_id=selected_workspace,
                )
            )
        return projections

    @staticmethod
    def empty_projection() -> dict[str, Any]:
        return {
            "source_check_id": None,
            "source_check_status": "not_started",
            "source_check_effective_status": "NOT_STARTED",
            "source_syntax_status": "not_checked",
            "static_checks_status": "not_started",
            "static_security_policy_status": "not_started",
            **ExtensionSourcePolicyGate._dynamic_statuses(),
            "operational_health": "available",
        }

    @staticmethod
    def unavailable_projection() -> dict[str, Any]:
        return {
            "source_check_id": None,
            "source_check_status": "indeterminate",
            "source_check_effective_status": "SOURCE_CHECK_UNAVAILABLE",
            "source_syntax_status": "not_checked",
            "static_checks_status": "indeterminate",
            "static_security_policy_status": "indeterminate",
            **ExtensionSourcePolicyGate._dynamic_statuses(),
            "operational_health": "fail_closed",
        }

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
            raise ExtensionSourceCheckConflictError(
                "source-check operation identity was rebound"
            )
        record = self._record_by_id(
            state,
            str(operation["check_id"]),
        )
        self._require_owner(record, owner_scope_digest)
        return record

    def _source_subject(self, **kwargs: Any) -> dict[str, Any]:
        try:
            subject = self.artifact_quarantine.source_check_subject(
                **kwargs
            )
        except ExtensionArtifactError:
            raise
        except Exception as exc:
            raise ExtensionSourceCheckStorageError(
                "source-check prerequisite is unavailable"
            ) from exc
        if (
            not isinstance(subject, dict)
            or not isinstance(subject.get("source_bytes"), bytes)
            or not isinstance(subject.get("source_check_spec"), dict)
            or any(subject.get("authority", {}).values())
        ):
            raise ExtensionSourceCheckStorageError(
                "source-check prerequisite subject is invalid"
            )
        return subject

    @staticmethod
    def _binding_from_subject(
        subject: dict[str, Any],
    ) -> ExtensionSourceCheckBinding:
        return parse_extension_source_check_binding(
            {
                "schema_version": (
                    EXTENSION_SOURCE_CHECK_BINDING_SCHEMA_VERSION
                ),
                "candidate_id": subject["candidate_id"],
                "candidate_revision": subject["candidate_revision"],
                "artifact_id": subject["artifact_id"],
                "artifact_revision": subject["artifact_revision"],
                "owner_scope_digest": subject["owner_scope_digest"],
                "extension_id": subject["extension_id"],
                "extension_version": subject["extension_version"],
                "spec_digest": subject["spec_digest"],
                "artifact_sha256": subject["artifact_sha256"],
                "source_size_bytes": subject["artifact_size_bytes"],
                "extension_policy_revision": subject[
                    "extension_policy_revision"
                ],
                "artifact_policy_revision": subject[
                    "artifact_policy_revision"
                ],
                "source_check_policy_revision": (
                    EXTENSION_SOURCE_CHECK_POLICY_REVISION
                ),
                "parser_identity": EXTENSION_SOURCE_PARSER_IDENTITY,
                "ruleset_digest": EXTENSION_SOURCE_RULESET_DIGEST,
            }
        )

    @staticmethod
    def _validate_report_binding(
        report: ExtensionSourceCheckReport,
        binding: ExtensionSourceCheckBinding,
    ) -> None:
        report_binding = {
            key: report.model_dump(mode="json")[key]
            for key in binding.canonical_dict()
            if key != "schema_version"
        }
        expected = {
            key: value
            for key, value in binding.canonical_dict().items()
            if key != "schema_version"
        }
        if report_binding != expected:
            raise ExtensionSourceCheckStorageError(
                "source-check report binding is invalid"
            )

    def _public_check(
        self,
        record: dict[str, Any],
        *,
        operation_replayed: bool = False,
        check_existing: bool = False,
    ) -> dict[str, Any]:
        self._validate_record(record)
        binding = parse_extension_source_check_binding(
            record["binding"]
        )
        report = (
            parse_extension_source_check_report(record["report"])
            if isinstance(record["report"], dict)
            else None
        )
        effective, health = self._stored_effective_status(record)
        if report is None:
            source_check_status = "indeterminate"
            syntax_status = "not_checked"
            static_status = "indeterminate"
            static_security = "indeterminate"
            issue_codes: list[str] = []
        else:
            source_check_status = report.check_status
            syntax_status = report.source_syntax_status
            static_status = report.static_checks_status
            static_security = report.static_security_policy_status
            issue_codes = list(report.issue_codes)
        return {
            "schema_version": PUBLIC_CHECK_SCHEMA,
            "check_id": record["check_id"],
            "artifact_id": binding.artifact_id,
            "artifact_revision": binding.artifact_revision,
            "candidate_id": binding.candidate_id,
            "candidate_revision": binding.candidate_revision,
            "extension_id": binding.extension_id,
            "extension_version": binding.extension_version,
            "artifact_sha256": binding.artifact_sha256,
            "stored_stage": record["stage"],
            "effective_status": effective,
            "source_check_status": source_check_status,
            "source_syntax_status": syntax_status,
            "static_checks_status": static_status,
            "static_security_policy_status": static_security,
            "issue_codes": issue_codes,
            "checker_revision": EXTENSION_SOURCE_CHECKER_REVISION,
            "source_check_policy_revision": (
                EXTENSION_SOURCE_CHECK_POLICY_REVISION
            ),
            "parser_identity": EXTENSION_SOURCE_PARSER_IDENTITY,
            "ruleset_digest": EXTENSION_SOURCE_RULESET_DIGEST,
            "source_check_report_digest": record["report_digest"],
            "report_integrity_status": (
                "validated" if report is not None else "unavailable"
            ),
            **self._dynamic_statuses(),
            "operational_health": health,
            "operation_replayed": operation_replayed,
            "check_existing": check_existing,
            "policy_effect": "none",
            "authority": self._authority(),
        }

    @staticmethod
    def _stored_effective_status(
        record: dict[str, Any],
    ) -> tuple[str, str]:
        if record["stage"] == "SOURCE_CHECK_CLAIMED":
            return "SOURCE_CHECK_INDETERMINATE", "degraded"
        if record["stage"] == "SOURCE_CHECK_INDETERMINATE":
            return "SOURCE_CHECK_INDETERMINATE", "degraded"
        return record["stage"], "available"

    def _public_check_for_owner(
        self,
        record: dict[str, Any],
        *,
        user_id: str,
        workspace_id: str,
        operation_replayed: bool = False,
        check_existing: bool = False,
    ) -> dict[str, Any]:
        public = self._public_check(
            record,
            operation_replayed=operation_replayed,
            check_existing=check_existing,
        )
        binding = parse_extension_source_check_binding(record["binding"])
        try:
            self.artifact_quarantine.source_check_subject(
                artifact_id=binding.artifact_id,
                user_id=user_id,
                workspace_id=workspace_id,
                expected_artifact_revision=binding.artifact_revision,
                expected_artifact_sha256=binding.artifact_sha256,
            )
        except (
            ExtensionArtifactConflictError,
            ExtensionSpecConflictError,
            ExtensionSpecNotFoundError,
        ):
            public["effective_status"] = "BLOCKED_PREREQUISITE"
            public["operational_health"] = "degraded"
        except (
            ExtensionArtifactStorageError,
            ExtensionSpecStorageError,
        ):
            public["effective_status"] = "PREREQUISITE_UNAVAILABLE"
            public["operational_health"] = "degraded"
        except Exception:
            public["effective_status"] = "PREREQUISITE_UNAVAILABLE"
            public["operational_health"] = "degraded"
        if self._isolated_runner_projection is not None:
            try:
                projection = self._isolated_runner_projection(
                    check_id=record["check_id"],
                    user_id=user_id,
                    workspace_id=workspace_id,
                )
                if not isinstance(projection, dict):
                    raise TypeError("runner projection is invalid")
                public["trusted_isolated_runner_status"] = str(
                    projection["trusted_isolated_runner_status"]
                )
                public["isolated_test_execution_status"] = str(
                    projection["isolated_test_execution_status"]
                )
            except Exception:
                # A corrupt or unavailable later-stage store must never make
                # the already-established source-check result less truthful.
                public["trusted_isolated_runner_status"] = "indeterminate"
                public["isolated_test_execution_status"] = "not_started"
        return public

    @staticmethod
    def _projection(public: dict[str, Any]) -> dict[str, Any]:
        return {
            "source_check_id": public["check_id"],
            "source_check_status": public["source_check_status"],
            "source_check_effective_status": public["effective_status"],
            "source_syntax_status": public["source_syntax_status"],
            "static_checks_status": public["static_checks_status"],
            "static_security_policy_status": public[
                "static_security_policy_status"
            ],
            **{
                key: public[key]
                for key in ExtensionSourcePolicyGate._dynamic_statuses()
            },
            "operational_health": public["operational_health"],
        }

    @staticmethod
    def _dynamic_statuses() -> dict[str, Any]:
        return {
            "isolated_generation_status": "not_started",
            "trusted_isolated_runner_status": "not_started",
            "isolated_test_execution_status": "not_started",
            "unit_checks_status": "not_started",
            "contract_checks_status": "not_started",
            "security_runtime_checks_status": "not_started",
            "fuzz_checks_status": "not_started",
            "test_execution_status": "not_started",
            "behavior_verification_status": "not_started",
            "execution_status": "not_started",
            "signature_status": "not_implemented",
            "activation_status": "not_installed",
            "capability_registry_visible": False,
            "promotion_authorized": False,
        }

    def _read_valid_state(self) -> dict[str, Any]:
        try:
            state = self.state_store.read_json(STATE_FILE)
            self._validate_state(state)
            return state
        except ExtensionSourceCheckError:
            raise
        except Exception as exc:
            raise ExtensionSourceCheckStorageError(
                "source-check state is unavailable"
            ) from exc

    def _read_owner_state(
        self,
        user_id: str,
        workspace_id: str,
    ) -> tuple[str, str, str, dict[str, Any]]:
        selected_user = self._required_text(
            user_id,
            "user_id",
            240,
        )
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
        except ExtensionSourceCheckError:
            raise
        except Exception as exc:
            raise ExtensionSourceCheckStorageError(
                "source-check owner-scoped state is unavailable"
            ) from exc
        return (
            selected_user,
            selected_workspace,
            artifact_owner_scope_digest(
                selected_user,
                selected_workspace,
            ),
            state,
        )

    def _mutate(
        self,
        mutator: Callable[[dict[str, Any]], None],
    ) -> None:
        try:
            self.state_store.mutate_json(STATE_FILE, mutator)
        except ExtensionSourceCheckError:
            raise
        except Exception as exc:
            raise ExtensionSourceCheckStorageError(
                "source-check state is unavailable"
            ) from exc

    def _validate_state(self, state: dict[str, Any]) -> None:
        if (
            not isinstance(state, dict)
            or state.get("_state_corrupt") is True
            or not _STATE_FIELDS.issubset(state)
            or bool(
                set(state)
                - _STATE_FIELDS
                - _STATE_METADATA_FIELDS
            )
            or state.get("schema_version") != STATE_SCHEMA_VERSION
            or not isinstance(state.get("checks"), dict)
            or not isinstance(state.get("artifact_index"), dict)
            or not isinstance(state.get("operation_index"), dict)
            or type(state.get("check_count")) is not int
            or state["check_count"] != len(state["checks"])
            or len(state["checks"]) > MAX_CHECKS
            or len(state["operation_index"]) > MAX_OPERATIONS
            or (
                state.get("updated_at") is not None
                and not isinstance(state.get("updated_at"), str)
            )
        ):
            raise ExtensionSourceCheckStorageError(
                "source-check private state is invalid"
            )
        expected_index: dict[str, str] = {}
        for check_id, record in state["checks"].items():
            if check_id != record.get("check_id"):
                raise ExtensionSourceCheckStorageError(
                    "source-check map identity is invalid"
                )
            self._validate_record(record)
            binding = parse_extension_source_check_binding(
                record["binding"]
            )
            artifact_key = self._artifact_key(
                record["owner_scope_digest"],
                binding.artifact_id,
            )
            if artifact_key in expected_index:
                raise ExtensionSourceCheckStorageError(
                    "source-check artifact identity is duplicated"
                )
            expected_index[artifact_key] = check_id
        if state["artifact_index"] != expected_index:
            raise ExtensionSourceCheckStorageError(
                "source-check artifact index is invalid"
            )
        for operation_key, operation in state[
            "operation_index"
        ].items():
            self._validate_operation(
                operation_key,
                operation,
                state,
            )

    def _validate_record(self, record: dict[str, Any]) -> None:
        if (
            not isinstance(record, dict)
            or set(record) != _RECORD_FIELDS
            or record.get("schema_version") != RECORD_SCHEMA_VERSION
            or not _CHECK_ID.fullmatch(
                str(record.get("check_id") or "")
            )
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
        ):
            raise ExtensionSourceCheckStorageError(
                "source-check private record is invalid"
            )
        binding = parse_extension_source_check_binding(record["binding"])
        if (
            binding.owner_scope_digest != record["owner_scope_digest"]
            or binding.binding_digest() != record["binding_digest"]
            or record["check_id"]
            != self._check_id(
                self._artifact_key(
                    record["owner_scope_digest"],
                    binding.artifact_id,
                )
            )
        ):
            raise ExtensionSourceCheckStorageError(
                "source-check record binding is invalid"
            )
        self._parse_time(record["created_at"])
        self._parse_time(record["updated_at"])
        report = record.get("report")
        if record["stage"] in {"SOURCE_CHECK_PASSED", "SOURCE_CHECK_FAILED"}:
            if (
                not isinstance(report, dict)
                or not _DIGEST.fullmatch(
                    str(record.get("report_digest") or "")
                )
                or record.get("failure_code") is not None
            ):
                raise ExtensionSourceCheckStorageError(
                    "source-check terminal report is invalid"
                )
            parsed = parse_extension_source_check_report(report)
            self._validate_report_binding(parsed, binding)
            if (
                parsed.report_digest() != record["report_digest"]
                or (
                    parsed.check_status == "passed"
                    and record["stage"] != "SOURCE_CHECK_PASSED"
                )
                or (
                    parsed.check_status == "failed"
                    and record["stage"] != "SOURCE_CHECK_FAILED"
                )
            ):
                raise ExtensionSourceCheckStorageError(
                    "source-check report identity is invalid"
                )
        elif (
            report is not None
            or record.get("report_digest") is not None
            or (
                record["stage"] == "SOURCE_CHECK_CLAIMED"
                and record.get("failure_code") is not None
            )
            or (
                record["stage"] == "SOURCE_CHECK_INDETERMINATE"
                and record.get("failure_code") not in _FAILURE_CODES
            )
        ):
            raise ExtensionSourceCheckStorageError(
                "source-check non-report state is invalid"
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
            or operation.get("kind") != "source_check"
            or not _DIGEST.fullmatch(
                str(operation.get("request_digest") or "")
            )
            or not _CHECK_ID.fullmatch(
                str(operation.get("check_id") or "")
            )
            or operation["check_id"] not in state["checks"]
            or not isinstance(operation.get("recorded_at"), str)
        ):
            raise ExtensionSourceCheckStorageError(
                "source-check operation binding is invalid"
            )
        record = self._record_by_id(
            state,
            str(operation["check_id"]),
        )
        binding = parse_extension_source_check_binding(
            record["binding"]
        )
        if operation["request_digest"] != self._request_digest(
            str(record["owner_scope_digest"]),
            binding.artifact_id,
            binding.artifact_revision,
            binding.artifact_sha256,
        ):
            raise ExtensionSourceCheckStorageError(
                "source-check operation semantic binding is invalid"
            )
        ExtensionSourcePolicyGate._parse_time(
            operation["recorded_at"]
        )

    def _record_operation(
        self,
        state: dict[str, Any],
        *,
        operation_key: str,
        request_digest: str,
        check_id: str,
    ) -> None:
        if len(state["operation_index"]) >= MAX_OPERATIONS:
            raise ExtensionSourceCheckConflictError(
                "source-check operation capacity is exhausted"
            )
        if not _DIGEST.fullmatch(operation_key):
            raise ExtensionSourceCheckStorageError(
                "source-check operation key is invalid"
            )
        record = self._record_by_id(state, check_id)
        binding = parse_extension_source_check_binding(
            record["binding"]
        )
        if request_digest != self._request_digest(
            str(record["owner_scope_digest"]),
            binding.artifact_id,
            binding.artifact_revision,
            binding.artifact_sha256,
        ):
            raise ExtensionSourceCheckStorageError(
                "source-check operation request is invalid"
            )
        recorded_at = self._now_iso()
        state["operation_index"][operation_key] = {
            "kind": "source_check",
            "request_digest": request_digest,
            "check_id": check_id,
            "recorded_at": recorded_at,
        }
        state["updated_at"] = recorded_at

    @staticmethod
    def _record_by_id(
        state: dict[str, Any],
        check_id: str,
    ) -> dict[str, Any]:
        record = state["checks"].get(check_id)
        if not isinstance(record, dict):
            raise ExtensionSourceCheckNotFoundError(
                "source check was not found"
            )
        return record

    @staticmethod
    def _require_owner(
        record: dict[str, Any],
        owner_scope_digest: str,
    ) -> None:
        if record.get("owner_scope_digest") != owner_scope_digest:
            raise ExtensionSourceCheckNotFoundError(
                "source check was not found"
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
            raise ExtensionSourceCheckConflictError(
                "workspace must match the current local Veyra scope"
            )

    @staticmethod
    def _artifact_key(
        owner_scope_digest: str,
        artifact_id: str,
    ) -> str:
        return ExtensionSourcePolicyGate._digest(
            {
                "owner_scope_digest": owner_scope_digest,
                "artifact_id": artifact_id,
                "policy_revision": (
                    EXTENSION_SOURCE_CHECK_POLICY_REVISION
                ),
                "parser_identity": EXTENSION_SOURCE_PARSER_IDENTITY,
                "ruleset_digest": EXTENSION_SOURCE_RULESET_DIGEST,
            }
        )

    @staticmethod
    def _check_id(artifact_key: str) -> str:
        return f"extcheck_{artifact_key[:24]}"

    @staticmethod
    def _operation_key(
        owner_scope_digest: str,
        operation_id: str,
    ) -> str:
        return ExtensionSourcePolicyGate._digest(
            {
                "owner_scope_digest": owner_scope_digest,
                "operation_id": operation_id,
            }
        )

    @staticmethod
    def _request_digest(
        owner_scope_digest: str,
        artifact_id: str,
        artifact_revision: int,
        artifact_sha256: str,
    ) -> str:
        return ExtensionSourcePolicyGate._digest(
            {
                "kind": "source_check",
                "owner_scope_digest": owner_scope_digest,
                "artifact_id": artifact_id,
                "artifact_revision": artifact_revision,
                "artifact_sha256": artifact_sha256,
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
    def _required_check_id(value: Any) -> str:
        if not isinstance(value, str) or not _CHECK_ID.fullmatch(value):
            raise ValueError("check_id is invalid")
        return value

    @staticmethod
    def _required_artifact_id(value: Any) -> str:
        if not isinstance(value, str) or not _ARTIFACT_ID.fullmatch(value):
            raise ValueError("artifact_id is invalid")
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
    def _required_text(
        value: Any,
        field: str,
        limit: int,
    ) -> str:
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
            raise ValueError("source-check timestamp is invalid")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(
                "source-check timestamp is invalid"
            ) from exc
        if parsed.tzinfo is None:
            raise ValueError("source-check timestamp must be timezone-aware")
        return parsed.astimezone(timezone.utc)

    def _now_iso(self) -> str:
        selected = self._now()
        if selected.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        selected = selected.astimezone(timezone.utc)
        if selected.microsecond:
            return selected.isoformat(
                timespec="microseconds",
            ).replace("+00:00", "Z")
        return selected.isoformat(timespec="seconds").replace(
            "+00:00",
            "Z",
        )

    @staticmethod
    def _authority() -> dict[str, bool]:
        return {
            "code_generation": False,
            "artifact_write": False,
            "artifact_write_outside_quarantine": False,
            "workspace_access": False,
            "state_access": False,
            "environment_access": False,
            "network_access": False,
            "secret_access": False,
            "tool_access": False,
            "agent_dispatch": False,
            "signing": False,
            "installation": False,
            "activation": False,
            "capability_registration": False,
            "execution": False,
            "behavior_verification": False,
            "promotion": False,
            "provider_switch": False,
        }

    @staticmethod
    def _safe_issue(exc: Exception) -> str:
        if isinstance(exc, ExtensionSourceCheckStorageError):
            return "extension_source_check_state_invalid"
        return f"extension_source_check_unavailable:{type(exc).__name__}"

    def _audit(self, record: dict[str, Any]) -> None:
        try:
            self.state_store.append_jsonl(
                "action_record.jsonl",
                {
                    "route": "phase6_extension_source_check",
                    "status": "recorded",
                    "artifacts": {
                        "private_source_check_scope_digest": self._digest(
                            {
                                "check_id": record.get("check_id"),
                                "owner_scope_digest": record.get(
                                    "owner_scope_digest"
                                ),
                            }
                        ),
                        "source_check_status": (
                            "passed"
                            if record.get("stage")
                            == "SOURCE_CHECK_PASSED"
                            else "failed"
                            if record.get("stage")
                            == "SOURCE_CHECK_FAILED"
                            else "indeterminate"
                        ),
                        "candidate_executed": False,
                        "execution_authorized": False,
                        "promotion_authorized": False,
                    },
                },
            )
        except Exception:
            return

__all__ = [
    "MAX_CHECKS",
    "MAX_OPERATIONS",
    "STATE_FILE",
    "STATE_SCHEMA_VERSION",
    "ExtensionSourceCheckConflictError",
    "ExtensionSourceCheckError",
    "ExtensionSourceCheckNotFoundError",
    "ExtensionSourceCheckStorageError",
    "ExtensionSourcePolicyGate",
]
