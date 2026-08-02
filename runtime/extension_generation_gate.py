from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
import re
from typing import Any, Callable

from core.world_state import WorldStateStore
from interface.extension_artifact import (
    EXTENSION_ARTIFACT_POLICY_REVISION,
    EXTENSION_ARTIFACT_SCHEMA_VERSION,
    ExtensionArtifactEnvelope,
    encode_artifact_content,
)
from interface.extension_generation import (
    EXTENSION_GENERATION_BINDING_SCHEMA_VERSION,
    EXTENSION_GENERATION_POLICY_DIGEST,
    EXTENSION_GENERATION_POLICY_REVISION,
    EXTENSION_GENERATION_PROMPT_DIGEST,
    EXTENSION_GENERATION_REPORT_SCHEMA_VERSION,
    EXTENSION_GENERATOR_REVISION,
    ExtensionGenerationAuthority,
    ExtensionGenerationBinding,
    ExtensionGenerationReport,
    authenticated_local_principal_digest,
    initiating_session_digest,
    parse_extension_generation_binding,
    parse_extension_generation_report,
)
from interface.extension_spec import (
    EXTENSION_POLICY_REVISION,
    parse_extension_spec,
)
from runtime.bounded_extension_generator import (
    BoundedExtensionGenerator,
    BoundedExtensionGeneratorOutputError,
    BoundedExtensionGeneratorUnavailable,
)
from runtime.extension_artifact_quarantine import (
    ExtensionArtifactError,
    ExtensionArtifactQuarantine,
)
from runtime.extension_spec_quarantine import (
    ExtensionSpecConflictError,
    ExtensionSpecQuarantine,
    ExtensionSpecQuarantineError,
)


STATE_FILE = "phase6_extension_generation_state.json"
STATE_SCHEMA_VERSION = "veyra.phase6.extension_generation_state.v1"
RECORD_SCHEMA_VERSION = "veyra.phase6.extension_generation_record.v1"
PUBLIC_STATUS_SCHEMA = "veyra.phase6.extension_generation_status.v1"
PUBLIC_RECORD_SCHEMA = "veyra.phase6.extension_generation_public_record.v1"
PUBLIC_LIST_SCHEMA = "veyra.phase6.extension_generation_list.v1"
PUBLIC_INTEGRITY_SCHEMA = "veyra.phase6.extension_generation_integrity.v1"

MAX_GENERATIONS = 500
MAX_OPERATIONS = 1_000
STARTED_INDETERMINATE_AFTER_SECONDS = 180
_STAGES = {
    "GENERATION_CLAIMED",
    "GENERATION_STARTED",
    "GENERATION_QUARANTINED",
    "GENERATION_REJECTED",
}
_TERMINAL = {"GENERATION_QUARANTINED", "GENERATION_REJECTED"}
_GENERATION_ID = re.compile(r"^extgen_[0-9a-f]{24}$")
_CANDIDATE_ID = re.compile(r"^extspec_[0-9a-f]{24}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,239}$")


class ExtensionGenerationError(RuntimeError):
    """Base generation lifecycle error."""


class ExtensionGenerationConflictError(ExtensionGenerationError):
    """The request conflicts with an exact durable generation identity."""


class ExtensionGenerationNotFoundError(ExtensionGenerationError):
    """The generation is absent or outside the authenticated scope."""


class ExtensionGenerationStorageError(ExtensionGenerationError):
    """Private generation state or prerequisite state is unavailable."""


class ExtensionGenerationUnauthorizedError(ExtensionGenerationError):
    """The generation command lacks the configured local credential."""


class ExtensionGenerationUnavailableError(ExtensionGenerationError):
    """The generation feature or exact model identity is unavailable."""


class ExtensionGenerationGate:
    """Durable, authenticated model-source generation into quarantine only."""

    def __init__(
        self,
        *,
        state_store: WorldStateStore,
        spec_quarantine: ExtensionSpecQuarantine,
        artifact_quarantine: ExtensionArtifactQuarantine,
        generator: BoundedExtensionGenerator,
        enabled: bool | None = None,
        control_token: str | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.state_store = state_store
        self.spec_quarantine = spec_quarantine
        self.artifact_quarantine = artifact_quarantine
        self.generator = generator
        self.enabled = (
            bool(enabled)
            if enabled is not None
            else _env_bool("VEYRA_EXTENSION_GENERATION_ENABLED")
        )
        self.control_token = (
            control_token
            if control_token is not None
            else os.getenv("VEYRA_LOCAL_API_TOKEN", "")
        )
        self._clock = now or (lambda: datetime.now(timezone.utc))

    def status(self) -> dict[str, Any]:
        try:
            state = self._read_valid_state()
            counts = {stage: 0 for stage in sorted(_STAGES)}
            for record in state["generations"].values():
                counts[str(record["stage"])] += 1
            storage = {
                "status": "ready",
                "generation_count": len(state["generations"]),
                "counts": counts,
                "issue": None,
            }
        except ExtensionGenerationError as exc:
            storage = {
                "status": "fault",
                "generation_count": 0,
                "counts": {stage: 0 for stage in sorted(_STAGES)},
                "issue": type(exc).__name__,
            }
        try:
            identity = self._generator_identity()
        except ExtensionGenerationError:
            identity = {
                "configured": False,
                "provider_id": "unavailable",
                "model_id": "unavailable",
                "model_config_digest": "0" * 64,
            }
        start_ready = bool(
            self.enabled
            and self.control_token
            and identity["configured"]
            and storage["status"] == "ready"
        )
        return {
            "schema_version": PUBLIC_STATUS_SCHEMA,
            "phase": "6.2e-generation",
            "status": "technical_complete_generation_to_quarantine_only",
            "operational_health": (
                "ready"
                if start_ready
                else "disabled_by_policy"
                if not self.enabled
                else "not_configured"
            ),
            "completion_scope": (
                "authenticated bounded model source data to private "
                "artifact quarantine only"
            ),
            "admission": {
                "enabled": self.enabled,
                "token_configured": bool(self.control_token),
                "start_ready": start_ready,
                "auth_derived_principal": True,
                "initiating_session_required": True,
                "loopback_bypass_allowed": False,
            },
            "generator": {
                "configured": bool(identity["configured"]),
                "provider_id": identity["provider_id"],
                "model_id": identity["model_id"],
                "model_config_digest": identity["model_config_digest"],
                "generator_revision": EXTENSION_GENERATOR_REVISION,
                "prompt_digest": EXTENSION_GENERATION_PROMPT_DIGEST,
                "policy_revision": EXTENSION_GENERATION_POLICY_REVISION,
                "policy_digest": EXTENSION_GENERATION_POLICY_DIGEST,
            },
            "storage": storage,
            "authority": self._authority(),
            "next_stage": {
                "static_source_check": "required",
                "trusted_isolated_runner_probe": "required",
                "dynamic_validation": "not_started",
                "signature": "not_started",
                "canary": "not_started",
                "promotion": "not_started",
            },
        }

    def generate(
        self,
        *,
        candidate_id: str,
        user_id: str,
        workspace_id: str,
        session_id: str,
        request_id: str,
        operation_id: str,
        expected_candidate_revision: int,
        expected_spec_digest: str,
        control_token: str,
    ) -> dict[str, Any]:
        principal = self._authorize(control_token)
        selected_candidate = self._required_candidate_id(candidate_id)
        selected_user = self._required_text(user_id, "user_id")
        selected_workspace = self._required_text(
            workspace_id,
            "workspace_id",
        )
        selected_session = self._required_text(session_id, "session_id")
        selected_request = self._required_identifier(request_id, "request_id")
        selected_operation = self._required_identifier(
            operation_id,
            "operation_id",
        )
        selected_revision = self._required_revision(
            expected_candidate_revision
        )
        selected_spec_digest = self._required_digest(
            expected_spec_digest,
            "expected_spec_digest",
        )
        subject = self._spec_subject(
            candidate_id=selected_candidate,
            user_id=selected_user,
            workspace_id=selected_workspace,
        )
        self._validate_subject(
            subject,
            candidate_id=selected_candidate,
            candidate_revision=selected_revision,
            spec_digest=selected_spec_digest,
        )
        generator_identity = self._generator_identity(required=True)
        principal_digest = authenticated_local_principal_digest(principal)
        session_digest = initiating_session_digest(selected_session)
        request_provenance_digest = self._digest(
            {
                "request_id": selected_request,
                "user_id": selected_user,
                "workspace_id": selected_workspace,
                "session_digest": session_digest,
                "principal_digest": principal_digest,
            }
        )
        generation_id = self._generation_id(
            candidate_id=selected_candidate,
            candidate_revision=selected_revision,
            spec_digest=selected_spec_digest,
            principal_digest=principal_digest,
            session_digest=session_digest,
            generator_identity=generator_identity,
        )
        binding = parse_extension_generation_binding(
            {
                "schema_version": (
                    EXTENSION_GENERATION_BINDING_SCHEMA_VERSION
                ),
                "generation_id": generation_id,
                "candidate_id": selected_candidate,
                "candidate_revision": selected_revision,
                "owner_scope_digest": subject["owner_scope_digest"],
                "authenticated_principal_digest": principal_digest,
                "initiating_session_digest": session_digest,
                "request_provenance_digest": request_provenance_digest,
                "extension_id": subject["extension_id"],
                "extension_version": subject["extension_version"],
                "spec_digest": selected_spec_digest,
                "candidate_expires_at": self._canonical_timestamp(
                    subject["candidate_expires_at"]
                ),
                "extension_policy_revision": EXTENSION_POLICY_REVISION,
                "artifact_policy_revision": (
                    EXTENSION_ARTIFACT_POLICY_REVISION
                ),
                "generation_policy_revision": (
                    EXTENSION_GENERATION_POLICY_REVISION
                ),
                "generation_policy_digest": (
                    EXTENSION_GENERATION_POLICY_DIGEST
                ),
                "generator_revision": EXTENSION_GENERATOR_REVISION,
                "prompt_digest": EXTENSION_GENERATION_PROMPT_DIGEST,
                "provider_id": generator_identity["provider_id"],
                "model_id": generator_identity["model_id"],
                "model_config_digest": generator_identity[
                    "model_config_digest"
                ],
            }
        )
        binding_digest = binding.binding_digest()
        request_digest = self._request_digest(
            binding=binding,
            operation_id=selected_operation,
        )
        operation_key = self._operation_key(
            principal_digest,
            session_digest,
            selected_operation,
        )
        selected: dict[str, Any] | None = None
        replayed = False
        existing = False

        def claim(state: dict[str, Any]) -> None:
            nonlocal selected, replayed, existing
            self._validate_state(state)
            operation = state["operation_index"].get(operation_key)
            if operation is not None:
                self._validate_operation(operation_key, operation, state)
                if operation["request_digest"] != request_digest:
                    raise ExtensionGenerationConflictError(
                        "generation operation identity was rebound"
                    )
                selected = self._record_by_id(
                    state,
                    str(operation["generation_id"]),
                )
                self._require_owner(
                    selected,
                    selected_user,
                    selected_workspace,
                    principal_digest,
                    session_digest,
                )
                replayed = True
                return
            indexed = state["candidate_index"].get(selected_candidate)
            if indexed is not None:
                selected = self._record_by_id(state, str(indexed))
                self._require_owner(
                    selected,
                    selected_user,
                    selected_workspace,
                    principal_digest,
                    session_digest,
                )
                if selected["binding_digest"] != binding_digest:
                    raise ExtensionGenerationConflictError(
                        "candidate already has another generation identity"
                    )
                existing = True
                self._record_operation(
                    state,
                    operation_key=operation_key,
                    request_digest=request_digest,
                    generation_id=generation_id,
                )
                return
            if len(state["generations"]) >= MAX_GENERATIONS:
                raise ExtensionGenerationConflictError(
                    "generation capacity is exhausted"
                )
            created_at = self._now_iso()
            record = {
                "schema_version": RECORD_SCHEMA_VERSION,
                "generation_id": generation_id,
                "candidate_id": selected_candidate,
                "user_id": selected_user,
                "workspace_id": selected_workspace,
                "principal_digest": principal_digest,
                "session_digest": session_digest,
                "binding": binding.canonical_dict(),
                "binding_digest": binding_digest,
                "stage": "GENERATION_CLAIMED",
                "report": None,
                "report_digest": None,
                "created_at": created_at,
                "started_at": None,
                "updated_at": created_at,
            }
            state["generations"][generation_id] = record
            state["candidate_index"][selected_candidate] = generation_id
            state["generation_count"] = len(state["generations"])
            self._record_operation(
                state,
                operation_key=operation_key,
                request_digest=request_digest,
                generation_id=generation_id,
            )
            selected = record

        self._mutate(claim)
        if selected is None:
            raise ExtensionGenerationStorageError(
                "generation claim produced no record"
            )
        if replayed or existing:
            return self._public_record(
                selected,
                operation_replayed=replayed,
                generation_existing=existing,
            )

        def mark_started(state: dict[str, Any]) -> None:
            self._validate_state(state)
            record = self._record_by_id(state, generation_id)
            self._require_owner(
                record,
                selected_user,
                selected_workspace,
                principal_digest,
                session_digest,
            )
            if (
                record["stage"] != "GENERATION_CLAIMED"
                or record["binding_digest"] != binding_digest
            ):
                raise ExtensionGenerationConflictError(
                    "generation claim changed before dispatch"
                )
            started_at = self._now_iso()
            record["stage"] = "GENERATION_STARTED"
            record["started_at"] = started_at
            record["updated_at"] = started_at

        self._mutate(mark_started)
        report: ExtensionGenerationReport | None = None
        try:
            generated = self.generator.generate(
                binding=binding,
                spec=subject["source_check_spec"],
            )
            if (
                generated.provider_id != binding.provider_id
                or generated.model_id != binding.model_id
                or generated.model_config_digest
                != binding.model_config_digest
            ):
                raise BoundedExtensionGeneratorOutputError(
                    "generator result identity drifted"
                )
            final_subject = self._spec_subject(
                candidate_id=selected_candidate,
                user_id=selected_user,
                workspace_id=selected_workspace,
            )
            self._validate_subject(
                final_subject,
                candidate_id=selected_candidate,
                candidate_revision=selected_revision,
                spec_digest=selected_spec_digest,
            )
            if self._generator_identity(required=True) != generator_identity:
                raise BoundedExtensionGeneratorUnavailable(
                    "generator identity changed before artifact admission"
                )
            source_digest = hashlib.sha256(
                generated.source_bytes
            ).hexdigest()
            envelope = ExtensionArtifactEnvelope(
                schema_version=EXTENSION_ARTIFACT_SCHEMA_VERSION,
                artifact_kind="python_source_utf8",
                candidate_id=binding.candidate_id,
                candidate_revision=binding.candidate_revision,
                owner_scope_digest=binding.owner_scope_digest,
                extension_id=binding.extension_id,
                extension_version=binding.extension_version,
                spec_digest=binding.spec_digest,
                extension_policy_revision=binding.extension_policy_revision,
                artifact_policy_revision=binding.artifact_policy_revision,
                artifact_sha256=source_digest,
                size_bytes=len(generated.source_bytes),
                content_b64url=encode_artifact_content(
                    generated.source_bytes
                ),
                expires_at=binding.candidate_expires_at,
            )
            artifact = self.artifact_quarantine.submit(
                envelope=envelope,
                expected_artifact_sha256=source_digest,
                user_id=selected_user,
                workspace_id=selected_workspace,
                operation_id=(
                    "generation-artifact-" + generation_id.removeprefix(
                        "extgen_"
                    )
                ),
            )
            if (
                artifact.get("artifact_sha256") != source_digest
                or artifact.get("candidate_id") != binding.candidate_id
                or artifact.get("artifact_status") != "quarantined"
            ):
                raise ExtensionGenerationStorageError(
                    "generated artifact admission result is invalid"
                )
            report = ExtensionGenerationReport(
                schema_version=EXTENSION_GENERATION_REPORT_SCHEMA_VERSION,
                binding=binding,
                binding_digest=binding_digest,
                generation_status="quarantined",
                artifact_id=str(artifact["artifact_id"]),
                artifact_sha256=source_digest,
                artifact_size_bytes=len(generated.source_bytes),
                artifact_envelope_digest=envelope.envelope_digest(),
                failure_code=None,
                generated_at=self._now_iso(),
                authority=ExtensionGenerationAuthority(),
            )
        except BoundedExtensionGeneratorUnavailable:
            report = self._rejected_report(
                binding,
                "model_unavailable",
            )
        except BoundedExtensionGeneratorOutputError:
            report = self._rejected_report(
                binding,
                "model_output_invalid",
            )
        except (ExtensionSpecQuarantineError, ExtensionSpecConflictError):
            report = self._rejected_report(
                binding,
                "prerequisite_changed",
            )
        except ExtensionArtifactError as exc:
            # Artifact admission owns a separate durable store/blob boundary.
            # An exception cannot prove that no artifact was persisted, so a
            # rejection would be a false terminal claim.  Preserve STARTED and
            # require operator reconciliation; replay must never call the model
            # or artifact store again.
            raise ExtensionGenerationStorageError(
                "artifact admission outcome is indeterminate"
            ) from exc

        if report is None:
            # Unknown exceptions deliberately leave durable STARTED evidence.
            # A retry cannot know whether the external model call or private
            # artifact admission completed, so it must not call either again.
            raise ExtensionGenerationStorageError(
                "generation ended without a canonical report"
            )
        finalized: dict[str, Any] | None = None

        def finish(state: dict[str, Any]) -> None:
            nonlocal finalized
            self._validate_state(state)
            record = self._record_by_id(state, generation_id)
            self._require_owner(
                record,
                selected_user,
                selected_workspace,
                principal_digest,
                session_digest,
            )
            if (
                record["stage"] != "GENERATION_STARTED"
                or record["binding_digest"] != binding_digest
            ):
                raise ExtensionGenerationStorageError(
                    "generation state changed before completion"
                )
            record["stage"] = (
                "GENERATION_QUARANTINED"
                if report.generation_status == "quarantined"
                else "GENERATION_REJECTED"
            )
            record["report"] = report.canonical_dict()
            record["report_digest"] = report.report_digest()
            record["updated_at"] = report.generated_at
            finalized = copy.deepcopy(record)

        self._mutate(finish)
        if finalized is None:
            raise ExtensionGenerationStorageError(
                "generation completion was not persisted"
            )
        return self._public_record(finalized)

    def get(
        self,
        *,
        generation_id: str,
        user_id: str,
        workspace_id: str,
        session_id: str,
        control_token: str,
    ) -> dict[str, Any]:
        principal = authenticated_local_principal_digest(
            self._authorize(control_token)
        )
        session = initiating_session_digest(session_id)
        state = self._read_valid_state()
        record = self._record_by_id(
            state,
            self._required_generation_id(generation_id),
        )
        self._require_owner(
            record,
            self._required_text(user_id, "user_id"),
            self._required_text(workspace_id, "workspace_id"),
            principal,
            session,
        )
        return self._public_record(record)

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
        principal = authenticated_local_principal_digest(
            self._authorize(control_token)
        )
        session = initiating_session_digest(session_id)
        selected_user = self._required_text(user_id, "user_id")
        selected_workspace = self._required_text(
            workspace_id,
            "workspace_id",
        )
        state = self._read_valid_state()
        records = [
            record
            for record in state["generations"].values()
            if record["user_id"] == selected_user
            and record["workspace_id"] == selected_workspace
            and record["principal_digest"] == principal
            and record["session_digest"] == session
        ]
        records.sort(
            key=lambda item: (
                str(item["updated_at"]),
                str(item["generation_id"]),
            ),
            reverse=True,
        )
        return {
            "schema_version": PUBLIC_LIST_SCHEMA,
            "count": len(records[:limit]),
            "generations": [
                self._public_record(record)
                for record in records[:limit]
            ],
            "authority": self._authority(),
        }

    def integrity(
        self,
        *,
        generation_id: str,
        user_id: str,
        workspace_id: str,
        session_id: str,
        control_token: str,
    ) -> dict[str, Any]:
        public = self.get(
            generation_id=generation_id,
            user_id=user_id,
            workspace_id=workspace_id,
            session_id=session_id,
            control_token=control_token,
        )
        return {
            "schema_version": PUBLIC_INTEGRITY_SCHEMA,
            "generation_id": public["generation_id"],
            "stored_stage": public["stored_stage"],
            "effective_status": public["effective_status"],
            "binding_digest": public["binding_digest"],
            "report_digest": public["report_digest"],
            "binding_integrity_status": "validated",
            "report_integrity_status": public["report_integrity_status"],
            "state_mutated": False,
            "authority": self._authority(),
        }

    def _public_record(
        self,
        record: dict[str, Any],
        *,
        operation_replayed: bool = False,
        generation_existing: bool = False,
    ) -> dict[str, Any]:
        self._validate_record(record)
        binding = parse_extension_generation_binding(record["binding"])
        report = (
            parse_extension_generation_report(record["report"])
            if isinstance(record.get("report"), dict)
            else None
        )
        effective = str(record["stage"])
        if effective == "GENERATION_STARTED" and self._started_stale(record):
            effective = "GENERATION_INDETERMINATE"
        return {
            "schema_version": PUBLIC_RECORD_SCHEMA,
            "generation_id": record["generation_id"],
            "candidate_id": binding.candidate_id,
            "candidate_revision": binding.candidate_revision,
            "extension_id": binding.extension_id,
            "extension_version": binding.extension_version,
            "spec_digest": binding.spec_digest,
            "stored_stage": record["stage"],
            "effective_status": effective,
            "generation_status": (
                report.generation_status
                if report is not None
                else "indeterminate"
                if effective == "GENERATION_INDETERMINATE"
                else "pending"
            ),
            "artifact_id": report.artifact_id if report else None,
            "artifact_sha256": report.artifact_sha256 if report else None,
            "artifact_size_bytes": (
                report.artifact_size_bytes if report else None
            ),
            "failure_code": report.failure_code if report else None,
            "provider_id": binding.provider_id,
            "model_id": binding.model_id,
            "model_config_digest": binding.model_config_digest,
            "generator_revision": binding.generator_revision,
            "prompt_digest": binding.prompt_digest,
            "generation_policy_digest": (
                binding.generation_policy_digest
            ),
            "binding_digest": record["binding_digest"],
            "report_digest": record["report_digest"],
            "report_integrity_status": (
                "validated" if report is not None else "not_available"
            ),
            "static_source_check_status": "not_started",
            "dynamic_validation_status": "not_started",
            "signature_status": "not_started",
            "activation_status": "not_installed",
            "capability_registry_visible": False,
            "promotion_authorized": False,
            "created_at": record["created_at"],
            "started_at": record["started_at"],
            "updated_at": record["updated_at"],
            "operation_replayed": operation_replayed,
            "generation_existing": generation_existing,
            "authority": self._authority(),
        }

    def _rejected_report(
        self,
        binding: ExtensionGenerationBinding,
        failure_code: str,
    ) -> ExtensionGenerationReport:
        return ExtensionGenerationReport(
            schema_version=EXTENSION_GENERATION_REPORT_SCHEMA_VERSION,
            binding=binding,
            binding_digest=binding.binding_digest(),
            generation_status="rejected",
            failure_code=failure_code,  # type: ignore[arg-type]
            generated_at=self._now_iso(),
            authority=ExtensionGenerationAuthority(),
        )

    def _generator_identity(
        self,
        *,
        required: bool = False,
    ) -> dict[str, Any]:
        try:
            identity = self.generator.identity()
        except Exception as exc:
            raise ExtensionGenerationUnavailableError(
                "generation model identity is unavailable"
            ) from exc
        if (
            not isinstance(identity, dict)
            or type(identity.get("configured")) is not bool
            or not isinstance(identity.get("provider_id"), str)
            or not isinstance(identity.get("model_id"), str)
            or not _DIGEST.fullmatch(
                str(identity.get("model_config_digest") or "")
            )
        ):
            raise ExtensionGenerationUnavailableError(
                "generation model identity is invalid"
            )
        if required and not identity["configured"]:
            raise ExtensionGenerationUnavailableError(
                "generation model is not configured"
            )
        return {
            "configured": identity["configured"],
            "provider_id": identity["provider_id"] or "unavailable",
            "model_id": identity["model_id"] or "unavailable",
            "model_config_digest": identity["model_config_digest"],
        }

    def _spec_subject(self, **kwargs: Any) -> dict[str, Any]:
        try:
            subject = self.spec_quarantine.artifact_subject(
                require_gate_passed=True,
                **kwargs,
            )
        except ExtensionSpecQuarantineError:
            raise
        except Exception as exc:
            raise ExtensionGenerationStorageError(
                "generation prerequisite is unavailable"
            ) from exc
        if (
            not isinstance(subject, dict)
            or not isinstance(subject.get("source_check_spec"), dict)
            or any(subject.get("authority", {}).values())
        ):
            raise ExtensionGenerationStorageError(
                "generation prerequisite subject is invalid"
            )
        return subject

    @staticmethod
    def _validate_subject(
        subject: dict[str, Any],
        *,
        candidate_id: str,
        candidate_revision: int,
        spec_digest: str,
    ) -> None:
        spec = parse_extension_spec(subject["source_check_spec"])
        if (
            subject.get("candidate_id") != candidate_id
            or subject.get("candidate_revision") != candidate_revision
            or subject.get("spec_digest") != spec_digest
            or spec.digest() != spec_digest
            or subject.get("candidate_stage") != "SPEC_GATE_PASSED"
            or spec.extension_kind != "pure_function"
            or spec.risk_floor != "R0"
            or spec.dependencies
            or spec.permissions.files
            or spec.permissions.network_hosts
            or spec.permissions.secret_ids
            or spec.permissions.external_account_ids
            or spec.permissions.max_cost_usd_cents != 0
            or spec.side_effects
        ):
            raise ExtensionGenerationConflictError(
                "generation requires the exact passed zero-authority spec"
            )

    def _authorize(self, supplied_token: str) -> str:
        if not self.enabled:
            raise ExtensionGenerationUnavailableError(
                "extension generation is disabled by policy"
            )
        if not self.control_token:
            raise ExtensionGenerationUnavailableError(
                "extension generation control token is not configured"
            )
        if (
            not isinstance(supplied_token, str)
            or not hmac.compare_digest(supplied_token, self.control_token)
        ):
            raise ExtensionGenerationUnauthorizedError(
                "extension generation control token is invalid"
            )
        return self.control_token

    def _read_valid_state(self) -> dict[str, Any]:
        try:
            state = self.state_store.read_json(STATE_FILE)
            self._validate_state(state)
            return state
        except ExtensionGenerationError:
            raise
        except Exception as exc:
            raise ExtensionGenerationStorageError(
                "generation state is unavailable"
            ) from exc

    def _mutate(
        self,
        mutator: Callable[[dict[str, Any]], None],
    ) -> None:
        try:
            self.state_store.mutate_json(STATE_FILE, mutator)
        except ExtensionGenerationError:
            raise
        except Exception as exc:
            raise ExtensionGenerationStorageError(
                "generation state is unavailable"
            ) from exc

    def _validate_state(self, state: dict[str, Any]) -> None:
        if (
            not isinstance(state, dict)
            or state.get("_state_corrupt") is True
            or state.get("schema_version") != STATE_SCHEMA_VERSION
            or not isinstance(state.get("generations"), dict)
            or not isinstance(state.get("candidate_index"), dict)
            or not isinstance(state.get("operation_index"), dict)
            or type(state.get("generation_count")) is not int
            or state["generation_count"] != len(state["generations"])
            or len(state["generations"]) > MAX_GENERATIONS
            or len(state["operation_index"]) > MAX_OPERATIONS
        ):
            raise ExtensionGenerationStorageError(
                "generation state is invalid"
            )
        expected_candidates: dict[str, str] = {}
        for generation_id, record in state["generations"].items():
            if (
                not isinstance(record, dict)
                or generation_id != record.get("generation_id")
            ):
                raise ExtensionGenerationStorageError(
                    "generation record index is invalid"
                )
            self._validate_record(record)
            candidate_id = str(record["candidate_id"])
            if candidate_id in expected_candidates:
                raise ExtensionGenerationStorageError(
                    "candidate generation identity is duplicated"
                )
            expected_candidates[candidate_id] = generation_id
        if state["candidate_index"] != expected_candidates:
            raise ExtensionGenerationStorageError(
                "candidate generation index is invalid"
            )
        for operation_key, operation in state["operation_index"].items():
            self._validate_operation(operation_key, operation, state)

    def _validate_record(self, record: dict[str, Any]) -> None:
        required_keys = {
            "schema_version",
            "generation_id",
            "candidate_id",
            "user_id",
            "workspace_id",
            "principal_digest",
            "session_digest",
            "binding",
            "binding_digest",
            "stage",
            "report",
            "report_digest",
            "created_at",
            "started_at",
            "updated_at",
        }
        if (
            not isinstance(record, dict)
            or set(record) != required_keys
            or record.get("schema_version") != RECORD_SCHEMA_VERSION
            or not _GENERATION_ID.fullmatch(
                str(record.get("generation_id") or "")
            )
            or not _CANDIDATE_ID.fullmatch(
                str(record.get("candidate_id") or "")
            )
            or record.get("stage") not in _STAGES
            or not _DIGEST.fullmatch(
                str(record.get("principal_digest") or "")
            )
            or not _DIGEST.fullmatch(
                str(record.get("session_digest") or "")
            )
        ):
            raise ExtensionGenerationStorageError(
                "generation record is invalid"
            )
        binding = parse_extension_generation_binding(record["binding"])
        if (
            binding.generation_id != record["generation_id"]
            or binding.candidate_id != record["candidate_id"]
            or binding.authenticated_principal_digest
            != record["principal_digest"]
            or binding.initiating_session_digest != record["session_digest"]
            or binding.binding_digest() != record["binding_digest"]
        ):
            raise ExtensionGenerationStorageError(
                "generation binding is invalid"
            )
        self._parse_time(record["created_at"])
        self._parse_time(record["updated_at"])
        if record["stage"] == "GENERATION_CLAIMED":
            if (
                record["started_at"] is not None
                or record["report"] is not None
                or record["report_digest"] is not None
            ):
                raise ExtensionGenerationStorageError(
                    "claimed generation has invalid terminal data"
                )
        elif record["stage"] == "GENERATION_STARTED":
            if (
                not isinstance(record["started_at"], str)
                or record["report"] is not None
                or record["report_digest"] is not None
            ):
                raise ExtensionGenerationStorageError(
                    "started generation has invalid terminal data"
                )
            self._parse_time(record["started_at"])
        else:
            if (
                not isinstance(record["started_at"], str)
                or not isinstance(record["report"], dict)
                or not _DIGEST.fullmatch(
                    str(record.get("report_digest") or "")
                )
            ):
                raise ExtensionGenerationStorageError(
                    "terminal generation report is unavailable"
                )
            report = parse_extension_generation_report(record["report"])
            if (
                report.binding_digest != record["binding_digest"]
                or report.report_digest() != record["report_digest"]
                or (
                    report.generation_status == "quarantined"
                    and record["stage"] != "GENERATION_QUARANTINED"
                )
                or (
                    report.generation_status == "rejected"
                    and record["stage"] != "GENERATION_REJECTED"
                )
            ):
                raise ExtensionGenerationStorageError(
                    "terminal generation report identity is invalid"
                )

    def _validate_operation(
        self,
        operation_key: str,
        operation: Any,
        state: dict[str, Any],
    ) -> None:
        if (
            not _DIGEST.fullmatch(str(operation_key or ""))
            or not isinstance(operation, dict)
            or set(operation)
            != {
                "kind",
                "request_digest",
                "generation_id",
                "recorded_at",
            }
            or operation.get("kind") != "generate"
            or not _DIGEST.fullmatch(
                str(operation.get("request_digest") or "")
            )
            or operation.get("generation_id")
            not in state["generations"]
        ):
            raise ExtensionGenerationStorageError(
                "generation operation index is invalid"
            )
        self._parse_time(operation["recorded_at"])

    def _record_operation(
        self,
        state: dict[str, Any],
        *,
        operation_key: str,
        request_digest: str,
        generation_id: str,
    ) -> None:
        if operation_key in state["operation_index"]:
            return
        if len(state["operation_index"]) >= MAX_OPERATIONS:
            raise ExtensionGenerationConflictError(
                "generation operation capacity is exhausted"
            )
        state["operation_index"][operation_key] = {
            "kind": "generate",
            "request_digest": request_digest,
            "generation_id": generation_id,
            "recorded_at": self._now_iso(),
        }

    @staticmethod
    def _record_by_id(
        state: dict[str, Any],
        generation_id: str,
    ) -> dict[str, Any]:
        record = state["generations"].get(generation_id)
        if not isinstance(record, dict):
            raise ExtensionGenerationNotFoundError(
                "generation was not found"
            )
        return record

    @staticmethod
    def _require_owner(
        record: dict[str, Any],
        user_id: str,
        workspace_id: str,
        principal_digest: str,
        session_digest: str,
    ) -> None:
        if (
            record.get("user_id") != user_id
            or record.get("workspace_id") != workspace_id
            or record.get("principal_digest") != principal_digest
            or record.get("session_digest") != session_digest
        ):
            raise ExtensionGenerationNotFoundError(
                "generation was not found"
            )

    def _started_stale(self, record: dict[str, Any]) -> bool:
        if record.get("stage") != "GENERATION_STARTED":
            return False
        started = self._parse_time(record["started_at"])
        return self._now() - started > timedelta(
            seconds=STARTED_INDETERMINATE_AFTER_SECONDS
        )

    def _generation_id(
        self,
        **subject: Any,
    ) -> str:
        return "extgen_" + self._digest(subject)[:24]

    @staticmethod
    def _operation_key(
        principal_digest: str,
        session_digest: str,
        operation_id: str,
    ) -> str:
        return ExtensionGenerationGate._digest(
            {
                "principal_digest": principal_digest,
                "session_digest": session_digest,
                "operation_id": operation_id,
            }
        )

    @staticmethod
    def _request_digest(
        *,
        binding: ExtensionGenerationBinding,
        operation_id: str,
    ) -> str:
        return ExtensionGenerationGate._digest(
            {
                "kind": "generate",
                "operation_id": operation_id,
                "binding_digest": binding.binding_digest(),
            }
        )

    @staticmethod
    def _authority() -> dict[str, bool]:
        return ExtensionGenerationAuthority().model_dump(mode="json")

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
    def _required_candidate_id(value: Any) -> str:
        if not isinstance(value, str) or not _CANDIDATE_ID.fullmatch(value):
            raise ValueError("candidate_id is invalid")
        return value

    @staticmethod
    def _required_generation_id(value: Any) -> str:
        if not isinstance(value, str) or not _GENERATION_ID.fullmatch(value):
            raise ValueError("generation_id is invalid")
        return value

    @staticmethod
    def _required_identifier(value: Any, field: str) -> str:
        if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
            raise ValueError(f"{field} is invalid")
        return value

    @staticmethod
    def _required_text(value: Any, field: str) -> str:
        if not isinstance(value, str):
            raise TypeError(f"{field} must be a string")
        selected = value.strip()
        if (
            not selected
            or len(selected.encode("utf-8")) > 240
            or "\x00" in selected
        ):
            raise ValueError(f"{field} is invalid")
        return selected

    @staticmethod
    def _required_revision(value: Any) -> int:
        if type(value) is not int or not 1 <= value <= 2_147_483_647:
            raise ValueError("candidate revision is invalid")
        return value

    @staticmethod
    def _required_digest(value: Any, field: str) -> str:
        if not isinstance(value, str) or not _DIGEST.fullmatch(value):
            raise ValueError(f"{field} is invalid")
        return value

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ExtensionGenerationStorageError(
                "generation clock must be timezone-aware"
            )
        return value.astimezone(timezone.utc)

    def _now_iso(self) -> str:
        value = self._now()
        if value.microsecond:
            return value.isoformat(timespec="microseconds").replace(
                "+00:00",
                "Z",
            )
        return value.isoformat(timespec="seconds").replace("+00:00", "Z")

    @staticmethod
    def _canonical_timestamp(value: Any) -> str:
        if not isinstance(value, str):
            raise ExtensionGenerationConflictError(
                "generation prerequisite timestamp is invalid"
            )
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ExtensionGenerationConflictError(
                "generation prerequisite timestamp is invalid"
            ) from exc
        if parsed.tzinfo is None:
            raise ExtensionGenerationConflictError(
                "generation prerequisite timestamp is invalid"
            )
        selected = parsed.astimezone(timezone.utc)
        if selected.microsecond:
            return selected.isoformat(timespec="microseconds").replace(
                "+00:00",
                "Z",
            )
        return selected.isoformat(timespec="seconds").replace(
            "+00:00",
            "Z",
        )

    @staticmethod
    def _parse_time(value: Any) -> datetime:
        if not isinstance(value, str) or not value.endswith("Z"):
            raise ExtensionGenerationStorageError(
                "generation timestamp is invalid"
            )
        try:
            parsed = datetime.fromisoformat(value[:-1] + "+00:00")
        except ValueError as exc:
            raise ExtensionGenerationStorageError(
                "generation timestamp is invalid"
            ) from exc
        return parsed.astimezone(timezone.utc)


def _env_bool(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


__all__ = [
    "ExtensionGenerationConflictError",
    "ExtensionGenerationError",
    "ExtensionGenerationGate",
    "ExtensionGenerationNotFoundError",
    "ExtensionGenerationStorageError",
    "ExtensionGenerationUnauthorizedError",
    "ExtensionGenerationUnavailableError",
    "STATE_FILE",
    "STATE_SCHEMA_VERSION",
]
