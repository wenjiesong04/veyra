from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import unicodedata
from typing import Any, Callable, Literal

from core.world_state import WorldStateStore
from interface.extension_artifact import (
    EXTENSION_ARTIFACT_POLICY_REVISION,
    ExtensionArtifactEnvelope,
    artifact_owner_scope_digest,
    encode_artifact_content,
    parse_extension_artifact,
)
from runtime.extension_spec_quarantine import (
    ExtensionSpecQuarantineError,
    ExtensionSpecQuarantine,
)
from runtime.private_artifact_blob_store import (
    BlobMetadata,
    PrivateArtifactBlobStoreError,
    PrivateArtifactBlobStore,
)


STATE_FILE = "phase6_extension_artifact_state.json"
STATE_SCHEMA_VERSION = "veyra.phase6.extension_artifact_state.v1"
PUBLIC_STATUS_SCHEMA = "veyra.phase6.extension_artifact_status.v1"
PUBLIC_ARTIFACT_SCHEMA = "veyra.phase6.extension_artifact_record.v1"
PUBLIC_LIST_SCHEMA = "veyra.phase6.extension_artifact_list.v1"
PUBLIC_INTEGRITY_SCHEMA = "veyra.phase6.extension_artifact_integrity.v1"
RECORD_SCHEMA_VERSION = "veyra.phase6.extension_artifact_private_record.v1"
MAX_ARTIFACTS = 200
MAX_OPERATIONS = 2_000
MAX_NON_TERMINAL_OPERATIONS = MAX_OPERATIONS - MAX_ARTIFACTS
MAX_HISTORY = 8
MAX_EXPIRY_DAYS = 30

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,239}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_ARTIFACT_ID = re.compile(r"^extart_[0-9a-f]{24}$")
_STAGES = frozenset(
    {
        "ARTIFACT_QUARANTINED",
        "ARTIFACT_REJECTED",
        "ARTIFACT_REVOKED",
    }
)
_TERMINAL_STAGES = frozenset(
    {
        "ARTIFACT_REJECTED",
        "ARTIFACT_REVOKED",
    }
)
_CONTROL_SOURCE = "explicit_local_control_plane"
_RECORD_FIELDS = frozenset(
    {
        "schema_version",
        "artifact_id",
        "candidate_id",
        "candidate_revision",
        "user_id",
        "workspace_id",
        "owner_scope_digest",
        "extension_id",
        "extension_version",
        "spec_digest",
        "artifact_kind",
        "artifact_sha256",
        "size_bytes",
        "envelope_digest",
        "artifact_policy_revision",
        "extension_policy_revision",
        "blob",
        "stage",
        "revision",
        "expires_at",
        "rejection",
        "revocation",
        "history",
        "created_at",
        "updated_at",
    }
)
_TERMINAL_FIELDS = frozenset(
    {
        "reason_digest",
        "source",
        "recorded_at",
    }
)
_HISTORY_FIELDS = frozenset(
    {
        "revision",
        "transition",
        "reason_digest",
        "recorded_at",
    }
)
ArtifactTerminalKind = Literal["reject", "revoke"]


class ExtensionArtifactError(RuntimeError):
    """Base class for Phase 6.2b artifact quarantine failures."""


class ExtensionArtifactConflictError(ExtensionArtifactError):
    """An identity, CAS, replay, lifecycle, or capacity binding conflicted."""


class ExtensionArtifactNotFoundError(ExtensionArtifactError):
    """An artifact was absent or outside the caller's logical owner scope."""


class ExtensionArtifactStorageError(ExtensionArtifactError):
    """Private artifact state or bytes are invalid or unavailable."""


class ExtensionArtifactQuarantine:
    """Private, non-executing artifact quarantine for a gated ExtensionSpec.

    The runtime accepts one bounded source blob for one exact
    ``SPEC_GATE_PASSED`` candidate.  It never parses Python syntax, imports,
    compiles, evaluates, executes, signs, installs, registers, activates, or
    promotes the blob.
    """

    def __init__(
        self,
        *,
        state_store: WorldStateStore,
        spec_quarantine: ExtensionSpecQuarantine,
        blob_store: PrivateArtifactBlobStore | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.state_store = state_store
        self.spec_quarantine = spec_quarantine
        self.blob_store = blob_store or PrivateArtifactBlobStore(
            state_store.path_for(STATE_FILE).parent.resolve(strict=True)
            / "phase6_extension_artifacts"
        )
        self._now = now or (lambda: datetime.now(timezone.utc))

    def status(self) -> dict[str, Any]:
        try:
            state = self._read_valid_state()
            stored_counts = {
                "ARTIFACT_QUARANTINED": 0,
                "ARTIFACT_REJECTED": 0,
                "ARTIFACT_REVOKED": 0,
            }
            effective_counts = {
                "ARTIFACT_QUARANTINED": 0,
                "ARTIFACT_REJECTED": 0,
                "ARTIFACT_REVOKED": 0,
                "EXPIRED": 0,
                "BLOCKED_CANDIDATE": 0,
                "CANDIDATE_UNAVAILABLE": 0,
            }
            candidate_bindings = {
                "validated": 0,
                "blocked": 0,
                "unavailable": 0,
            }
            records = list(state["artifacts"].values())
            binding_statuses = self._candidate_bindings_for_records(
                records
            )
            for record in records:
                stage = str(record["stage"])
                stored_counts[stage] += 1
                binding = binding_statuses[str(record["artifact_id"])]
                candidate_bindings[binding] += 1
                effective = stage
                if stage == "ARTIFACT_QUARANTINED":
                    if self._is_expired(record):
                        effective = "EXPIRED"
                    elif binding == "unavailable":
                        effective = "CANDIDATE_UNAVAILABLE"
                    elif binding != "validated":
                        effective = "BLOCKED_CANDIDATE"
                effective_counts[effective] += 1
            storage = {
                "status": "ready",
                "issue": None,
                "artifact_count": len(state["artifacts"]),
                "operation_count": len(state["operation_index"]),
                "counts": effective_counts,
                "stored_counts": stored_counts,
                "candidate_binding_counts": candidate_bindings,
            }
            health = (
                "degraded"
                if candidate_bindings["unavailable"]
                else "available"
            )
        except Exception as exc:
            storage = {
                "status": "fault",
                "issue": self._safe_issue(exc),
                "artifact_count": 0,
                "operation_count": 0,
                "counts": {},
            }
            health = "degraded"
        return {
            "schema_version": PUBLIC_STATUS_SCHEMA,
            "phase": "6.2b",
            "status": (
                "technical_complete_artifact_quarantine_only"
                if health == "available"
                else "fail_closed"
            ),
            "operational_health": health,
            "completion_scope": (
                "bounded private source artifact quarantine only"
            ),
            "artifact_kind": "python_source_utf8",
            "private_quarantine_persistence": (
                "immutable_blob_bytes_and_bound_metadata"
            ),
            "lifecycle": [
                "ARTIFACT_QUARANTINED",
                "ARTIFACT_REJECTED",
                "ARTIFACT_REVOKED",
            ],
            "storage": storage,
            "policy_revision": EXTENSION_ARTIFACT_POLICY_REVISION,
            "authority": self._authority(),
            "next_stage": {
                "isolated_generation": "not_implemented",
                "static_checks": "not_implemented",
                "unit_contract_security_fuzz_tests": "not_implemented",
                "signature_verification": "not_implemented",
                "read_only_execution_canary": "not_implemented",
                "scoped_execution_canary": "not_implemented",
                "promotion": "not_implemented",
            },
        }

    def submit(
        self,
        *,
        envelope: ExtensionArtifactEnvelope | dict[str, Any],
        expected_artifact_sha256: str,
        user_id: str,
        workspace_id: str,
        operation_id: str,
    ) -> dict[str, Any]:
        selected_user, selected_workspace = self._owner_scope(
            user_id,
            workspace_id,
        )
        selected_operation = self._required_id(
            operation_id,
            "operation_id",
        )
        selected_digest = str(expected_artifact_sha256 or "")
        if not _DIGEST.fullmatch(selected_digest):
            raise ValueError(
                "expected_artifact_sha256 must be a SHA-256 digest"
            )
        parsed = parse_extension_artifact(envelope)
        if parsed.artifact_sha256 != selected_digest:
            raise ValueError("artifact digest mismatch")
        if parsed.owner_scope_digest != artifact_owner_scope_digest(
            selected_user,
            selected_workspace,
        ):
            raise ValueError("artifact owner scope digest mismatch")
        self._validate_new_expiry(parsed.expires_at)
        content = parsed.decoded_bytes()
        envelope_digest = parsed.envelope_digest()
        candidate_key = self._candidate_key(
            selected_user,
            selected_workspace,
            parsed.candidate_id,
        )
        artifact_id = self._artifact_id(
            candidate_key,
            parsed.artifact_sha256,
            parsed.artifact_policy_revision,
        )
        request_digest = self._digest(
            {
                "kind": "submit",
                "operation_id": selected_operation,
                "user_id": selected_user,
                "workspace_id": selected_workspace,
                "artifact_id": artifact_id,
                "candidate_revision": parsed.candidate_revision,
                "artifact_sha256": parsed.artifact_sha256,
                "size_bytes": parsed.size_bytes,
                "envelope_digest": envelope_digest,
            }
        )
        operation_key = self._operation_key(
            selected_user,
            selected_workspace,
            selected_operation,
        )
        selected: dict[str, Any] | None = None
        operation_replayed = False
        replayed_operation: dict[str, Any] | None = None
        artifact_existing = False

        def admit(state: dict[str, Any]) -> None:
            nonlocal selected
            nonlocal operation_replayed
            nonlocal replayed_operation
            nonlocal artifact_existing
            self._validate_state(state)
            replay = self._operation_replay(
                state,
                operation_key=operation_key,
                request_digest=request_digest,
                kind="submit",
                artifact_id=artifact_id,
            )
            if replay is not None:
                selected = self._record_by_id(
                    state,
                    replay["artifact_id"],
                )
                self._require_owner(
                    selected,
                    selected_user,
                    selected_workspace,
                )
                operation_replayed = True
                replayed_operation = dict(replay)
                return

            writer_subject = self.spec_quarantine.artifact_subject(
                candidate_id=parsed.candidate_id,
                user_id=selected_user,
                workspace_id=selected_workspace,
                require_gate_passed=True,
            )
            self._validate_subject(
                parsed,
                writer_subject,
                expected_user_id=selected_user,
                expected_workspace_id=selected_workspace,
            )
            writer_snapshot = parse_extension_artifact(
                {
                    **parsed.canonical_dict(include_content=False),
                    "content_b64url": encode_artifact_content(content),
                }
            )
            if (
                writer_snapshot.artifact_sha256
                != parsed.artifact_sha256
                or writer_snapshot.envelope_digest()
                != envelope_digest
            ):
                raise ExtensionArtifactStorageError(
                    "artifact changed before durable admission"
                )

            indexed = state["candidate_index"].get(candidate_key)
            if indexed is not None:
                current = self._record_by_id(state, str(indexed))
                self._require_owner(
                    current,
                    selected_user,
                    selected_workspace,
                )
                if (
                    current["artifact_id"] != artifact_id
                    or current["artifact_sha256"]
                    != parsed.artifact_sha256
                    or current["envelope_digest"] != envelope_digest
                ):
                    raise ExtensionArtifactConflictError(
                        "candidate already has another artifact; "
                        "increment the ExtensionSpec version"
                    )
                artifact_existing = True
                selected = current
                self._record_operation(
                    state,
                    operation_key=operation_key,
                    request_digest=request_digest,
                    kind="submit",
                    artifact_id=artifact_id,
                    result_revision=current["revision"],
                    result_stage=current["stage"],
                    terminal_control=False,
                )
                return

            if len(state["artifacts"]) >= MAX_ARTIFACTS:
                raise ExtensionArtifactConflictError(
                    "artifact quarantine capacity is exhausted"
                )
            if self._non_terminal_operation_count(state) >= (
                MAX_NON_TERMINAL_OPERATIONS
            ):
                raise ExtensionArtifactConflictError(
                    "artifact operation capacity is reserved for "
                    "terminal controls"
                )
            try:
                blob_metadata = self.blob_store.store(
                    artifact_id,
                    content,
                    parsed.artifact_sha256,
                )
            except PrivateArtifactBlobStoreError as exc:
                raise ExtensionArtifactStorageError(
                    "private artifact blob admission failed"
                ) from exc
            created_at = self._now_iso()
            record = {
                "schema_version": RECORD_SCHEMA_VERSION,
                "artifact_id": artifact_id,
                "candidate_id": parsed.candidate_id,
                "candidate_revision": parsed.candidate_revision,
                "user_id": selected_user,
                "workspace_id": selected_workspace,
                "owner_scope_digest": parsed.owner_scope_digest,
                "extension_id": parsed.extension_id,
                "extension_version": parsed.extension_version,
                "spec_digest": parsed.spec_digest,
                "artifact_kind": parsed.artifact_kind,
                "artifact_sha256": parsed.artifact_sha256,
                "size_bytes": parsed.size_bytes,
                "envelope_digest": envelope_digest,
                "artifact_policy_revision": (
                    parsed.artifact_policy_revision
                ),
                "extension_policy_revision": (
                    parsed.extension_policy_revision
                ),
                "blob": blob_metadata.to_dict(),
                "stage": "ARTIFACT_QUARANTINED",
                "revision": 1,
                "expires_at": parsed.expires_at,
                "rejection": None,
                "revocation": None,
                "history": [
                    {
                        "revision": 1,
                        "transition": "artifact_quarantined",
                        "reason_digest": None,
                        "recorded_at": created_at,
                    }
                ],
                "created_at": created_at,
                "updated_at": created_at,
            }
            state["artifacts"][artifact_id] = record
            state["candidate_index"][candidate_key] = artifact_id
            state["artifact_count"] = len(state["artifacts"])
            self._record_operation(
                state,
                operation_key=operation_key,
                request_digest=request_digest,
                kind="submit",
                artifact_id=artifact_id,
                result_revision=1,
                result_stage="ARTIFACT_QUARANTINED",
                terminal_control=False,
            )
            selected = record

        self._mutate(admit)
        if selected is None:
            raise ExtensionArtifactStorageError(
                "artifact admission produced no record"
            )
        if not operation_replayed:
            self._audit(
                route="phase6_extension_artifact_submit",
                status="existing" if artifact_existing else "success",
                record=selected,
            )
        return self._public_artifact(
            selected,
            operation_replayed=operation_replayed,
            replayed_operation=replayed_operation,
            artifact_existing=artifact_existing,
        )

    def reject(
        self,
        *,
        artifact_id: str,
        user_id: str,
        workspace_id: str,
        expected_revision: int,
        operation_id: str,
        reason: str,
    ) -> dict[str, Any]:
        return self._terminal_transition(
            kind="reject",
            artifact_id=artifact_id,
            user_id=user_id,
            workspace_id=workspace_id,
            expected_revision=expected_revision,
            operation_id=operation_id,
            reason=reason,
        )

    def revoke(
        self,
        *,
        artifact_id: str,
        user_id: str,
        workspace_id: str,
        expected_revision: int,
        operation_id: str,
        reason: str,
    ) -> dict[str, Any]:
        return self._terminal_transition(
            kind="revoke",
            artifact_id=artifact_id,
            user_id=user_id,
            workspace_id=workspace_id,
            expected_revision=expected_revision,
            operation_id=operation_id,
            reason=reason,
        )

    def _terminal_transition(
        self,
        *,
        kind: ArtifactTerminalKind,
        artifact_id: str,
        user_id: str,
        workspace_id: str,
        expected_revision: int,
        operation_id: str,
        reason: str,
    ) -> dict[str, Any]:
        selected_user, selected_workspace = self._owner_scope(
            user_id,
            workspace_id,
        )
        selected_artifact = self._required_artifact_id(artifact_id)
        selected_revision = self._required_revision(expected_revision)
        selected_operation = self._required_id(
            operation_id,
            "operation_id",
        )
        selected_reason = self._required_text(reason, "reason", 1_200)
        reason_digest = self._digest({"reason": selected_reason})
        target_stage = (
            "ARTIFACT_REJECTED"
            if kind == "reject"
            else "ARTIFACT_REVOKED"
        )
        request_digest = self._digest(
            {
                "kind": kind,
                "operation_id": selected_operation,
                "user_id": selected_user,
                "workspace_id": selected_workspace,
                "artifact_id": selected_artifact,
                "expected_revision": selected_revision,
                "reason_digest": reason_digest,
            }
        )
        operation_key = self._operation_key(
            selected_user,
            selected_workspace,
            selected_operation,
        )
        selected: dict[str, Any] | None = None
        operation_replayed = False
        replayed_operation: dict[str, Any] | None = None

        def transition(state: dict[str, Any]) -> None:
            nonlocal selected
            nonlocal operation_replayed
            nonlocal replayed_operation
            self._assert_current_workspace(selected_workspace)
            self._validate_state(state)
            replay = self._operation_replay(
                state,
                operation_key=operation_key,
                request_digest=request_digest,
                kind=kind,
                artifact_id=selected_artifact,
            )
            if replay is not None:
                selected = self._record_by_id(
                    state,
                    replay["artifact_id"],
                )
                self._require_owner(
                    selected,
                    selected_user,
                    selected_workspace,
                )
                operation_replayed = True
                replayed_operation = dict(replay)
                return
            record = self._record_by_id(state, selected_artifact)
            self._require_owner(
                record,
                selected_user,
                selected_workspace,
            )
            if record["revision"] != selected_revision:
                raise ExtensionArtifactConflictError(
                    "artifact revision conflict"
                )
            if record["stage"] != "ARTIFACT_QUARANTINED":
                raise ExtensionArtifactConflictError(
                    "artifact terminal state cannot transition"
                )
            if len(record["history"]) >= MAX_HISTORY:
                raise ExtensionArtifactConflictError(
                    "artifact history capacity is exhausted"
                )
            recorded_at = self._now_iso()
            record["stage"] = target_stage
            record["revision"] += 1
            record["updated_at"] = recorded_at
            terminal = {
                "reason_digest": reason_digest,
                "source": _CONTROL_SOURCE,
                "recorded_at": recorded_at,
            }
            if kind == "reject":
                record["rejection"] = terminal
            else:
                record["revocation"] = terminal
            record["history"].append(
                {
                    "revision": record["revision"],
                    "transition": (
                        "artifact_rejected"
                        if kind == "reject"
                        else "artifact_revoked"
                    ),
                    "reason_digest": reason_digest,
                    "recorded_at": recorded_at,
                }
            )
            self._record_operation(
                state,
                operation_key=operation_key,
                request_digest=request_digest,
                kind=kind,
                artifact_id=selected_artifact,
                result_revision=record["revision"],
                result_stage=target_stage,
                terminal_control=True,
            )
            selected = record

        self._mutate(transition)
        if selected is None:
            raise ExtensionArtifactStorageError(
                "artifact transition produced no record"
            )
        if not operation_replayed:
            self._audit(
                route=f"phase6_extension_artifact_{kind}",
                status="success",
                record=selected,
            )
        return self._public_artifact(
            selected,
            operation_replayed=operation_replayed,
            replayed_operation=replayed_operation,
        )

    def get(
        self,
        *,
        artifact_id: str,
        user_id: str,
        workspace_id: str,
    ) -> dict[str, Any]:
        selected_user, selected_workspace, state = (
            self._read_owner_state(user_id, workspace_id)
        )
        record = self._record_by_id(
            state,
            self._required_artifact_id(artifact_id),
        )
        self._require_owner(record, selected_user, selected_workspace)
        return self._public_artifact(record)

    def list(
        self,
        *,
        user_id: str,
        workspace_id: str,
        limit: int = 50,
    ) -> dict[str, Any]:
        if type(limit) is not int or limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        selected_user, selected_workspace, state = (
            self._read_owner_state(user_id, workspace_id)
        )
        records = [
            record
            for record in state["artifacts"].values()
            if record["user_id"] == selected_user
            and record["workspace_id"] == selected_workspace
        ]
        records.sort(
            key=lambda item: (
                str(item["updated_at"]),
                str(item["artifact_id"]),
            ),
            reverse=True,
        )
        selected = records[:limit]
        binding_statuses = self._candidate_bindings_for_records(
            selected
        )
        return {
            "schema_version": PUBLIC_LIST_SCHEMA,
            "count": len(selected),
            "artifacts": [
                self._public_artifact(
                    record,
                    record_validated=True,
                    candidate_binding_override=(
                        binding_statuses[str(record["artifact_id"])]
                    ),
                )
                for record in selected
            ],
            "authority": self._authority(),
        }

    def projection_for_candidate(
        self,
        *,
        candidate_id: str,
        user_id: str,
        workspace_id: str,
    ) -> dict[str, Any]:
        selected_candidate = self._required_id(
            candidate_id,
            "candidate_id",
        )
        return self.projections_for_candidates(
            candidate_ids=[selected_candidate],
            user_id=user_id,
            workspace_id=workspace_id,
        )[selected_candidate]

    def projections_for_candidates(
        self,
        *,
        candidate_ids: list[str],
        user_id: str,
        workspace_id: str,
    ) -> dict[str, dict[str, Any]]:
        if (
            not isinstance(candidate_ids, list)
            or len(candidate_ids) > 100
            or any(not isinstance(item, str) for item in candidate_ids)
        ):
            raise ValueError("candidate_ids must be a bounded string list")
        selected_candidates = [
            self._required_id(item, "candidate_id")
            for item in candidate_ids
        ]
        if len(set(selected_candidates)) != len(selected_candidates):
            raise ValueError("candidate_ids must be unique")
        selected_user, selected_workspace, state = (
            self._read_owner_state(user_id, workspace_id)
        )
        records: dict[str, dict[str, Any] | None] = {}
        bound_candidates: list[str] = []
        for selected_candidate in selected_candidates:
            candidate_key = self._candidate_key(
                selected_user,
                selected_workspace,
                selected_candidate,
            )
            artifact_id = state["candidate_index"].get(candidate_key)
            if artifact_id is None:
                records[selected_candidate] = None
                continue
            record = self._record_by_id(state, str(artifact_id))
            self._require_owner(
                record,
                selected_user,
                selected_workspace,
            )
            records[selected_candidate] = record
            bound_candidates.append(selected_candidate)
        subjects: dict[str, dict[str, Any]] = {}
        subject_unavailable = False
        if bound_candidates:
            try:
                subjects = self.spec_quarantine.artifact_subjects(
                    candidate_ids=bound_candidates,
                    user_id=selected_user,
                    workspace_id=selected_workspace,
                    require_gate_passed=False,
                )
            except Exception:
                subject_unavailable = True
        projections: dict[str, dict[str, Any]] = {}
        for selected_candidate in selected_candidates:
            record = records[selected_candidate]
            if record is None:
                projections[selected_candidate] = (
                    self._empty_candidate_projection()
                )
                continue
            binding = (
                "unavailable"
                if subject_unavailable
                else self._binding_status_from_subject(
                    record,
                    subjects.get(selected_candidate),
                )
            )
            public = self._public_artifact(
                record,
                record_validated=True,
                candidate_binding_override=binding,
            )
            projections[selected_candidate] = {
                "artifact_id": public["artifact_id"],
                "artifact_status": public["artifact_status"],
                "artifact_integrity_status": public[
                    "artifact_integrity_status"
                ],
                "source_syntax_status": public[
                    "source_syntax_status"
                ],
                "static_checks_status": public[
                    "static_checks_status"
                ],
                "behavior_verification_status": public[
                    "behavior_verification_status"
                ],
                "signature_status": public["signature_status"],
                "execution_status": public["execution_status"],
                "activation_status": public["activation_status"],
                "capability_registry_visible": False,
                "promotion_authorized": False,
                "candidate_binding_status": binding,
                "operational_health": (
                    "fail_closed"
                    if binding == "unavailable"
                    else "available"
                ),
            }
        return projections

    @staticmethod
    def _empty_candidate_projection() -> dict[str, Any]:
        return {
            "artifact_status": "not_submitted",
            "artifact_integrity_status": "unverified",
            "source_syntax_status": "not_checked",
            "static_checks_status": "not_started",
            "behavior_verification_status": "not_started",
            "signature_status": "not_implemented",
            "execution_status": "not_started",
            "activation_status": "not_installed",
            "capability_registry_visible": False,
            "promotion_authorized": False,
            "candidate_binding_status": "not_applicable",
            "operational_health": "available",
        }

    def integrity(
        self,
        *,
        artifact_id: str,
        user_id: str,
        workspace_id: str,
    ) -> dict[str, Any]:
        selected_user, selected_workspace, state = (
            self._read_owner_state(user_id, workspace_id)
        )
        record = self._record_by_id(
            state,
            self._required_artifact_id(artifact_id),
        )
        self._require_owner(record, selected_user, selected_workspace)
        public = self._public_artifact(record)
        return {
            "schema_version": PUBLIC_INTEGRITY_SCHEMA,
            "artifact_id": record["artifact_id"],
            "artifact_revision": record["revision"],
            "candidate_id": record["candidate_id"],
            "candidate_revision": record["candidate_revision"],
            "stage": record["stage"],
            "effective_status": public["effective_status"],
            "status": (
                "artifact_integrity_passed"
                if public["effective_status"]
                == "ARTIFACT_QUARANTINED"
                else "blocked"
            ),
            "artifact_sha256": record["artifact_sha256"],
            "size_bytes": record["size_bytes"],
            "artifact_integrity_status": "validated",
            "source_syntax_status": "not_checked",
            "static_checks_status": "not_started",
            "behavior_verification_status": "not_started",
            "signature_status": "not_implemented",
            "execution_status": "not_started",
            "capability_registry_visible": False,
            "promotion_authorized": False,
            "state_mutated": False,
            "policy_effect": "none",
            "authority": self._authority(),
        }

    def _read_valid_state(self) -> dict[str, Any]:
        try:
            state = self.state_store.read_json(STATE_FILE)
            self._validate_state(state)
            return state
        except (ExtensionArtifactError, ExtensionSpecQuarantineError):
            raise
        except Exception as exc:
            raise ExtensionArtifactStorageError(
                "artifact quarantine state is unavailable"
            ) from exc

    def _read_owner_state(
        self,
        user_id: str,
        workspace_id: str,
    ) -> tuple[str, str, dict[str, Any]]:
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
        except ExtensionArtifactError:
            raise
        except Exception as exc:
            raise ExtensionArtifactStorageError(
                "artifact owner-scoped state is unavailable"
            ) from exc
        return selected_user, selected_workspace, state

    def _mutate(
        self,
        mutator: Callable[[dict[str, Any]], None],
    ) -> None:
        try:
            self.state_store.mutate_json(STATE_FILE, mutator)
        except (ExtensionArtifactError, ExtensionSpecQuarantineError):
            raise
        except Exception as exc:
            raise ExtensionArtifactStorageError(
                "artifact quarantine state is unavailable"
            ) from exc

    def _validate_state(self, state: dict[str, Any]) -> None:
        if (
            not isinstance(state, dict)
            or state.get("_state_corrupt") is True
            or state.get("schema_version") != STATE_SCHEMA_VERSION
            or not isinstance(state.get("artifacts"), dict)
            or not isinstance(state.get("candidate_index"), dict)
            or not isinstance(state.get("operation_index"), dict)
            or type(state.get("artifact_count")) is not int
            or state["artifact_count"] != len(state["artifacts"])
            or len(state["artifacts"]) > MAX_ARTIFACTS
            or len(state["operation_index"]) > MAX_OPERATIONS
        ):
            raise ExtensionArtifactStorageError(
                "artifact quarantine state is invalid"
            )
        expected_candidate_index: dict[str, str] = {}
        for artifact_id, record in state["artifacts"].items():
            if (
                not isinstance(artifact_id, str)
                or not isinstance(record, dict)
                or record.get("artifact_id") != artifact_id
            ):
                raise ExtensionArtifactStorageError(
                    "artifact record index is invalid"
                )
            self._validate_record(record)
            candidate_key = self._candidate_key(
                str(record["user_id"]),
                str(record["workspace_id"]),
                str(record["candidate_id"]),
            )
            if candidate_key in expected_candidate_index:
                raise ExtensionArtifactStorageError(
                    "candidate artifact identity is duplicated"
                )
            expected_candidate_index[candidate_key] = artifact_id
        if state["candidate_index"] != expected_candidate_index:
            raise ExtensionArtifactStorageError(
                "candidate artifact index is invalid"
            )
        non_terminal = 0
        terminal_artifacts: set[str] = set()
        for operation_key, operation in state["operation_index"].items():
            artifact = (
                state["artifacts"].get(operation.get("artifact_id"))
                if isinstance(operation, dict)
                else None
            )
            if (
                not isinstance(operation_key, str)
                or not _DIGEST.fullmatch(operation_key)
                or not isinstance(operation, dict)
                or set(operation)
                != {
                    "request_digest",
                    "kind",
                    "artifact_id",
                    "result_revision",
                    "result_stage",
                    "owner_scope_digest",
                    "terminal_control",
                    "recorded_at",
                }
                or not isinstance(artifact, dict)
                or operation.get("kind")
                not in {"submit", "reject", "revoke"}
                or not _DIGEST.fullmatch(
                    str(operation.get("request_digest") or "")
                )
                or type(operation.get("result_revision")) is not int
                or operation["result_revision"] < 1
                or operation["result_revision"] > artifact["revision"]
                or operation.get("result_stage") not in _STAGES
                or type(operation.get("terminal_control")) is not bool
                or operation.get("owner_scope_digest")
                != artifact["owner_scope_digest"]
                or not isinstance(operation.get("recorded_at"), str)
            ):
                raise ExtensionArtifactStorageError(
                    "artifact operation index is invalid"
                )
            self._parse_time(operation["recorded_at"])
            if operation["result_stage"] != self._stage_at_revision(
                artifact,
                operation["result_revision"],
            ):
                raise ExtensionArtifactStorageError(
                    "artifact operation result binding is invalid"
                )
            expected_terminal = operation["kind"] in {
                "reject",
                "revoke",
            }
            if operation["terminal_control"] is not expected_terminal:
                raise ExtensionArtifactStorageError(
                    "artifact operation control binding is invalid"
                )
            if expected_terminal:
                artifact_id = str(operation["artifact_id"])
                if artifact_id in terminal_artifacts:
                    raise ExtensionArtifactStorageError(
                        "artifact terminal operation is duplicated"
                    )
                terminal_artifacts.add(artifact_id)
            else:
                non_terminal += 1
        if (
            non_terminal > MAX_NON_TERMINAL_OPERATIONS
            or len(terminal_artifacts) > len(state["artifacts"])
        ):
            raise ExtensionArtifactStorageError(
                "artifact operation capacity binding is invalid"
            )

    def _validate_record(self, record: dict[str, Any]) -> None:
        if (
            not isinstance(record, dict)
            or set(record) != _RECORD_FIELDS
            or record.get("schema_version") != RECORD_SCHEMA_VERSION
            or not _ARTIFACT_ID.fullmatch(
                str(record.get("artifact_id") or "")
            )
            or not _ID.fullmatch(str(record.get("candidate_id") or ""))
            or type(record.get("candidate_revision")) is not int
            or record["candidate_revision"] < 1
            or not self._stored_owner_text_is_valid(
                record.get("user_id")
            )
            or not self._stored_owner_text_is_valid(
                record.get("workspace_id")
            )
            or record.get("owner_scope_digest")
            != artifact_owner_scope_digest(
                str(record.get("user_id") or ""),
                str(record.get("workspace_id") or ""),
            )
            or not _ID.fullmatch(str(record.get("extension_id") or ""))
            or type(record.get("extension_version")) is not int
            or record["extension_version"] < 1
            or not _DIGEST.fullmatch(
                str(record.get("spec_digest") or "")
            )
            or record.get("artifact_kind") != "python_source_utf8"
            or not _DIGEST.fullmatch(
                str(record.get("artifact_sha256") or "")
            )
            or type(record.get("size_bytes")) is not int
            or record["size_bytes"] < 1
            or not _DIGEST.fullmatch(
                str(record.get("envelope_digest") or "")
            )
            or record.get("artifact_policy_revision")
            != EXTENSION_ARTIFACT_POLICY_REVISION
            or not isinstance(
                record.get("extension_policy_revision"),
                str,
            )
            or record.get("stage") not in _STAGES
            or type(record.get("revision")) is not int
            or record["revision"] < 1
            or not isinstance(record.get("history"), list)
            or not record["history"]
            or len(record["history"]) > MAX_HISTORY
            or not isinstance(record.get("created_at"), str)
            or not isinstance(record.get("updated_at"), str)
            or not isinstance(record.get("expires_at"), str)
        ):
            raise ExtensionArtifactStorageError(
                "artifact private record is invalid"
            )
        expected_artifact_id = self._artifact_id(
            self._candidate_key(
                str(record["user_id"]),
                str(record["workspace_id"]),
                str(record["candidate_id"]),
            ),
            str(record["artifact_sha256"]),
            str(record["artifact_policy_revision"]),
        )
        if record["artifact_id"] != expected_artifact_id:
            raise ExtensionArtifactStorageError(
                "artifact identity binding is invalid"
            )
        try:
            blob_metadata = BlobMetadata.from_dict(record["blob"])
            content = self.blob_store.read(
                str(record["artifact_id"]),
                expected_metadata=blob_metadata,
            )
            parsed = parse_extension_artifact(
                {
                    "schema_version": (
                        "veyra.phase6.extension_artifact_envelope.v1"
                    ),
                    "artifact_kind": record["artifact_kind"],
                    "candidate_id": record["candidate_id"],
                    "candidate_revision": record["candidate_revision"],
                    "owner_scope_digest": record["owner_scope_digest"],
                    "extension_id": record["extension_id"],
                    "extension_version": record["extension_version"],
                    "spec_digest": record["spec_digest"],
                    "extension_policy_revision": record[
                        "extension_policy_revision"
                    ],
                    "artifact_policy_revision": record[
                        "artifact_policy_revision"
                    ],
                    "artifact_sha256": record["artifact_sha256"],
                    "size_bytes": record["size_bytes"],
                    "content_b64url": encode_artifact_content(content),
                    "expires_at": record["expires_at"],
                }
            )
        except Exception as exc:
            raise ExtensionArtifactStorageError(
                "artifact bytes or envelope binding is invalid"
            ) from exc
        if parsed.envelope_digest() != record["envelope_digest"]:
            raise ExtensionArtifactStorageError(
                "artifact envelope digest binding is invalid"
            )
        created_at = self._parse_time(record["created_at"])
        updated_at = self._parse_time(record["updated_at"])
        expires_at = self._parse_time(record["expires_at"])
        if updated_at < created_at or expires_at <= created_at:
            raise ExtensionArtifactStorageError(
                "artifact record timeline is invalid"
            )
        history = record["history"]
        previous_time: datetime | None = None
        for index, item in enumerate(history, start=1):
            if (
                not isinstance(item, dict)
                or set(item) != _HISTORY_FIELDS
                or item.get("revision") != index
                or not isinstance(item.get("recorded_at"), str)
                or (
                    item.get("reason_digest") is not None
                    and not _DIGEST.fullmatch(
                        str(item.get("reason_digest") or "")
                    )
                )
            ):
                raise ExtensionArtifactStorageError(
                    "artifact history is invalid"
                )
            recorded_at = self._parse_time(item["recorded_at"])
            if previous_time is not None and recorded_at < previous_time:
                raise ExtensionArtifactStorageError(
                    "artifact history ordering is invalid"
                )
            previous_time = recorded_at
        if (
            history[0]["transition"] != "artifact_quarantined"
            or history[0]["reason_digest"] is not None
            or history[0]["recorded_at"] != record["created_at"]
            or len(history) != record["revision"]
            or history[-1]["recorded_at"] != record["updated_at"]
        ):
            raise ExtensionArtifactStorageError(
                "artifact lifecycle history binding is invalid"
            )
        expected_stage = self._stage_at_revision(
            record,
            record["revision"],
        )
        if expected_stage != record["stage"]:
            raise ExtensionArtifactStorageError(
                "artifact lifecycle stage binding is invalid"
            )
        rejection = record.get("rejection")
        revocation = record.get("revocation")
        self._validate_terminal_record(rejection)
        self._validate_terminal_record(revocation)
        if record["stage"] == "ARTIFACT_QUARANTINED":
            terminal_valid = rejection is None and revocation is None
        elif record["stage"] == "ARTIFACT_REJECTED":
            terminal_valid = rejection is not None and revocation is None
        else:
            terminal_valid = rejection is None and revocation is not None
        if not terminal_valid:
            raise ExtensionArtifactStorageError(
                "artifact terminal state binding is invalid"
            )
        terminal = (
            rejection
            if record["stage"] == "ARTIFACT_REJECTED"
            else revocation
            if record["stage"] == "ARTIFACT_REVOKED"
            else None
        )
        if terminal is not None and (
            terminal["recorded_at"] != history[-1]["recorded_at"]
            or terminal["reason_digest"]
            != history[-1]["reason_digest"]
        ):
            raise ExtensionArtifactStorageError(
                "artifact terminal history binding is invalid"
            )

    def _validate_terminal_record(self, value: Any) -> None:
        if value is None:
            return
        if (
            not isinstance(value, dict)
            or set(value) != _TERMINAL_FIELDS
            or not _DIGEST.fullmatch(
                str(value.get("reason_digest") or "")
            )
            or value.get("source") != _CONTROL_SOURCE
            or not isinstance(value.get("recorded_at"), str)
        ):
            raise ExtensionArtifactStorageError(
                "artifact terminal record is invalid"
            )
        self._parse_time(value["recorded_at"])

    def _validate_subject(
        self,
        artifact: ExtensionArtifactEnvelope,
        subject: dict[str, Any],
        *,
        expected_user_id: str,
        expected_workspace_id: str,
    ) -> None:
        candidate_expires = self._parse_time(
            str(subject.get("candidate_expires_at") or "")
        )
        artifact_expires = self._parse_time(artifact.expires_at)
        declaration = (
            subject.get("artifact_declaration")
            if isinstance(subject.get("artifact_declaration"), dict)
            else {}
        )
        if (
            subject.get("candidate_id") != artifact.candidate_id
            or subject.get("candidate_revision")
            != artifact.candidate_revision
            or subject.get("user_id") != expected_user_id
            or subject.get("workspace_id") != expected_workspace_id
            or artifact.owner_scope_digest
            != subject.get("owner_scope_digest")
            or subject.get("extension_id") != artifact.extension_id
            or subject.get("extension_version")
            != artifact.extension_version
            or subject.get("extension_kind") != "pure_function"
            or subject.get("risk_floor") != "R0"
            or subject.get("spec_digest") != artifact.spec_digest
            or subject.get("extension_policy_revision")
            != artifact.extension_policy_revision
            or subject.get("candidate_stage") != "SPEC_GATE_PASSED"
            or declaration.get("status") != "not_generated"
            or declaration.get("artifact_kind") != "none"
            or declaration.get("code_sha256") is not None
            or artifact_expires > candidate_expires
            or any(subject.get("authority", {}).values())
        ):
            raise ExtensionArtifactConflictError(
                "artifact envelope does not match the gated ExtensionSpec"
            )

    def _public_artifact(
        self,
        record: dict[str, Any],
        *,
        operation_replayed: bool = False,
        replayed_operation: dict[str, Any] | None = None,
        artifact_existing: bool = False,
        record_validated: bool = False,
        candidate_binding_override: str | None = None,
    ) -> dict[str, Any]:
        if not record_validated:
            self._validate_record(record)
        candidate_binding = (
            candidate_binding_override
            if candidate_binding_override is not None
            else self._candidate_binding_status(record)
        )
        if candidate_binding not in {
            "validated",
            "blocked",
            "unavailable",
        }:
            raise ExtensionArtifactStorageError(
                "artifact candidate binding status is invalid"
            )
        effective = str(record["stage"])
        if effective == "ARTIFACT_QUARANTINED":
            if self._is_expired(record):
                effective = "EXPIRED"
            elif candidate_binding == "unavailable":
                effective = "CANDIDATE_UNAVAILABLE"
            elif candidate_binding != "validated":
                effective = "BLOCKED_CANDIDATE"
        artifact_status = {
            "ARTIFACT_QUARANTINED": "quarantined",
            "ARTIFACT_REJECTED": "rejected",
            "ARTIFACT_REVOKED": "revoked",
            "EXPIRED": "expired",
            "BLOCKED_CANDIDATE": "blocked_candidate",
            "CANDIDATE_UNAVAILABLE": "unavailable",
        }[effective]
        return {
            "schema_version": PUBLIC_ARTIFACT_SCHEMA,
            "artifact_id": record["artifact_id"],
            "candidate_id": record["candidate_id"],
            "candidate_revision": record["candidate_revision"],
            "artifact_revision": record["revision"],
            "extension_id": record["extension_id"],
            "extension_version": record["extension_version"],
            "spec_digest": record["spec_digest"],
            "artifact_kind": record["artifact_kind"],
            "private_quarantine_persisted": True,
            "artifact_sha256": record["artifact_sha256"],
            "size_bytes": record["size_bytes"],
            "stage": record["stage"],
            "effective_status": effective,
            "candidate_binding_status": candidate_binding,
            "artifact_status": artifact_status,
            "artifact_integrity_status": "validated",
            "source_syntax_status": "not_checked",
            "static_checks_status": "not_started",
            "behavior_verification_status": "not_started",
            "signature_status": "not_implemented",
            "execution_status": "not_started",
            "activation_status": "not_installed",
            "capability_registry_visible": False,
            "promotion_authorized": False,
            "policy_effect": "none",
            "history_count": len(record["history"]),
            "created_at": record["created_at"],
            "updated_at": record["updated_at"],
            "expires_at": record["expires_at"],
            "operation_replayed": operation_replayed,
            "replayed_operation_result": (
                {
                    "artifact_revision": replayed_operation.get(
                        "result_revision"
                    ),
                    "stored_stage": replayed_operation.get(
                        "result_stage"
                    ),
                }
                if operation_replayed
                and isinstance(replayed_operation, dict)
                else None
            ),
            "artifact_existing": artifact_existing,
            "authority": self._authority(),
        }

    def _candidate_binding_status(
        self,
        record: dict[str, Any],
    ) -> str:
        try:
            subject = self.spec_quarantine.artifact_subject(
                candidate_id=str(record["candidate_id"]),
                user_id=str(record["user_id"]),
                workspace_id=str(record["workspace_id"]),
                require_gate_passed=False,
            )
        except Exception:
            return "unavailable"
        return self._binding_status_from_subject(record, subject)

    def _candidate_bindings_for_records(
        self,
        records: list[dict[str, Any]],
    ) -> dict[str, str]:
        if not records:
            return {}
        try:
            subjects = self.spec_quarantine.artifact_subjects_for_bindings(
                bindings=[
                    (
                        str(record["user_id"]),
                        str(record["workspace_id"]),
                        str(record["candidate_id"]),
                    )
                    for record in records
                ]
            )
        except Exception:
            subjects = [None] * len(records)
        return {
            str(record["artifact_id"]): self._binding_status_from_subject(
                record,
                subject,
            )
            for record, subject in zip(records, subjects, strict=True)
        }

    @staticmethod
    def _binding_status_from_subject(
        record: dict[str, Any],
        subject: dict[str, Any] | None,
    ) -> str:
        if not isinstance(subject, dict):
            return "unavailable"
        if (
            subject.get("candidate_id") == record["candidate_id"]
            and subject.get("candidate_revision")
            == record["candidate_revision"]
            and subject.get("extension_id") == record["extension_id"]
            and subject.get("extension_version")
            == record["extension_version"]
            and subject.get("spec_digest") == record["spec_digest"]
            and subject.get("candidate_stage") == "SPEC_GATE_PASSED"
            and subject.get("extension_policy_revision")
            == record["extension_policy_revision"]
        ):
            return "validated"
        return "blocked"

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

    def _record_by_id(
        self,
        state: dict[str, Any],
        artifact_id: str,
    ) -> dict[str, Any]:
        record = state["artifacts"].get(artifact_id)
        if not isinstance(record, dict):
            raise ExtensionArtifactNotFoundError("artifact not found")
        return record

    @staticmethod
    def _require_owner(
        record: dict[str, Any],
        user_id: str,
        workspace_id: str,
    ) -> None:
        if (
            record.get("user_id") != user_id
            or record.get("workspace_id") != workspace_id
        ):
            raise ExtensionArtifactNotFoundError("artifact not found")

    def _operation_replay(
        self,
        state: dict[str, Any],
        *,
        operation_key: str,
        request_digest: str,
        kind: str,
        artifact_id: str,
    ) -> dict[str, Any] | None:
        existing = state["operation_index"].get(operation_key)
        if existing is None:
            return None
        if (
            not isinstance(existing, dict)
            or existing.get("request_digest") != request_digest
            or existing.get("kind") != kind
            or existing.get("artifact_id") != artifact_id
        ):
            raise ExtensionArtifactConflictError(
                "operation_id semantic conflict"
            )
        return existing

    def _record_operation(
        self,
        state: dict[str, Any],
        *,
        operation_key: str,
        request_digest: str,
        kind: str,
        artifact_id: str,
        result_revision: int,
        result_stage: str,
        terminal_control: bool,
    ) -> None:
        if operation_key in state["operation_index"]:
            raise ExtensionArtifactConflictError(
                "artifact operation already exists"
            )
        if len(state["operation_index"]) >= MAX_OPERATIONS:
            raise ExtensionArtifactConflictError(
                "artifact operation capacity is exhausted"
            )
        artifact = state["artifacts"].get(artifact_id)
        if not isinstance(artifact, dict):
            raise ExtensionArtifactStorageError(
                "artifact operation target is unavailable"
            )
        state["operation_index"][operation_key] = {
            "request_digest": request_digest,
            "kind": kind,
            "artifact_id": artifact_id,
            "result_revision": result_revision,
            "result_stage": result_stage,
            "owner_scope_digest": artifact["owner_scope_digest"],
            "terminal_control": terminal_control,
            "recorded_at": self._now_iso(),
        }

    @staticmethod
    def _stage_at_revision(
        record: dict[str, Any],
        revision: int,
    ) -> str:
        history = record.get("history")
        if (
            not isinstance(history, list)
            or revision < 1
            or revision > len(history)
        ):
            raise ExtensionArtifactStorageError(
                "artifact operation revision is invalid"
            )
        transition = history[revision - 1].get("transition")
        mapping = {
            "artifact_quarantined": "ARTIFACT_QUARANTINED",
            "artifact_rejected": "ARTIFACT_REJECTED",
            "artifact_revoked": "ARTIFACT_REVOKED",
        }
        stage = mapping.get(str(transition or ""))
        if stage is None:
            raise ExtensionArtifactStorageError(
                "artifact operation transition is invalid"
            )
        return stage

    @staticmethod
    def _non_terminal_operation_count(state: dict[str, Any]) -> int:
        return sum(
            1
            for operation in state["operation_index"].values()
            if isinstance(operation, dict)
            and operation.get("terminal_control") is False
        )

    def _owner_scope(
        self,
        user_id: str,
        workspace_id: str,
    ) -> tuple[str, str]:
        selected_user = self._required_text(user_id, "user_id", 240)
        selected_workspace = self._required_text(
            workspace_id,
            "workspace_id",
            240,
        )
        self._assert_current_workspace(selected_workspace)
        return selected_user, selected_workspace

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
            raise ExtensionArtifactConflictError(
                "workspace must match the current local Veyra scope"
            )

    def _is_expired(self, record: dict[str, Any]) -> bool:
        return self._now_utc() >= self._parse_time(
            str(record.get("expires_at") or "")
        )

    def _validate_new_expiry(self, value: str) -> None:
        expires = self._parse_time(value)
        now = self._now_utc()
        if expires <= now:
            raise ValueError("artifact expires_at must be in the future")
        if expires > now + timedelta(days=MAX_EXPIRY_DAYS):
            raise ValueError("artifact expiry exceeds the bounded lifetime")

    @staticmethod
    def _parse_time(value: str) -> datetime:
        selected = str(value or "")
        if selected.endswith("Z"):
            selected = selected[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(selected)
        except ValueError as exc:
            raise ExtensionArtifactStorageError(
                "artifact timestamp is invalid"
            ) from exc
        if parsed.tzinfo is None:
            raise ExtensionArtifactStorageError(
                "artifact timestamp must be timezone-aware"
            )
        return parsed.astimezone(timezone.utc)

    def _now_utc(self) -> datetime:
        selected = self._now()
        if selected.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        return selected.astimezone(timezone.utc)

    def _now_iso(self) -> str:
        selected = self._now_utc()
        if selected.microsecond:
            return selected.isoformat(
                timespec="microseconds",
            ).replace("+00:00", "Z")
        return selected.isoformat(timespec="seconds").replace(
            "+00:00",
            "Z",
        )

    @staticmethod
    def _candidate_key(
        user_id: str,
        workspace_id: str,
        candidate_id: str,
    ) -> str:
        return ExtensionArtifactQuarantine._digest(
            {
                "user_id": user_id,
                "workspace_id": workspace_id,
                "candidate_id": candidate_id,
            }
        )

    @staticmethod
    def _artifact_id(
        candidate_key: str,
        artifact_sha256: str,
        policy_revision: str,
    ) -> str:
        digest = ExtensionArtifactQuarantine._digest(
            {
                "candidate_key": candidate_key,
                "artifact_sha256": artifact_sha256,
                "policy_revision": policy_revision,
            }
        )
        return f"extart_{digest[:24]}"

    @staticmethod
    def _operation_key(
        user_id: str,
        workspace_id: str,
        operation_id: str,
    ) -> str:
        return ExtensionArtifactQuarantine._digest(
            {
                "user_id": user_id,
                "workspace_id": workspace_id,
                "operation_id": operation_id,
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
    def _required_artifact_id(value: Any) -> str:
        if not isinstance(value, str) or not _ARTIFACT_ID.fullmatch(value):
            raise ValueError("artifact_id is invalid")
        return value

    @staticmethod
    def _required_revision(value: Any) -> int:
        if type(value) is not int or value < 1:
            raise ValueError(
                "expected_revision must be a positive integer"
            )
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
    def _stored_owner_text_is_valid(value: Any) -> bool:
        return (
            isinstance(value, str)
            and value == value.strip()
            and bool(value)
            and "\x00" not in value
            and len(value.encode("utf-8")) <= 240
            and unicodedata.normalize("NFC", value) == value
        )

    @staticmethod
    def _safe_issue(exc: Exception) -> str:
        if isinstance(exc, ExtensionArtifactStorageError):
            return "extension_artifact_state_invalid"
        return f"extension_artifact_unavailable:{type(exc).__name__}"

    def _audit(
        self,
        *,
        route: str,
        status: str,
        record: dict[str, Any],
    ) -> None:
        try:
            private_scope_digest = self._digest(
                {
                    "artifact_id": record.get("artifact_id"),
                    "candidate_id": record.get("candidate_id"),
                    "user_id": record.get("user_id"),
                    "workspace_id": record.get("workspace_id"),
                }
            )
            self.state_store.append_jsonl(
                "action_record.jsonl",
                {
                    "route": route,
                    "status": status,
                    "artifacts": {
                        "private_artifact_scope_digest": (
                            private_scope_digest
                        ),
                        "artifact_revision": record.get("revision"),
                        "execution_authorized": False,
                        "promotion_authorized": False,
                    },
                },
            )
        except Exception:
            return


__all__ = [
    "MAX_ARTIFACTS",
    "MAX_EXPIRY_DAYS",
    "MAX_HISTORY",
    "MAX_NON_TERMINAL_OPERATIONS",
    "MAX_OPERATIONS",
    "STATE_FILE",
    "STATE_SCHEMA_VERSION",
    "ExtensionArtifactConflictError",
    "ExtensionArtifactError",
    "ExtensionArtifactNotFoundError",
    "ExtensionArtifactQuarantine",
    "ExtensionArtifactStorageError",
]
