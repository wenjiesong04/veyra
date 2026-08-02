from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import secrets
from typing import Any, Callable

from core.world_state import WorldStateStore
from interface.extension_artifact import artifact_owner_scope_digest
from interface.extension_dynamic_validation import (
    DynamicValidationReport,
    parse_dynamic_validation_report,
)
from interface.extension_generation import (
    ExtensionGenerationReport,
    authenticated_local_principal_digest,
    initiating_session_digest,
    parse_extension_generation_report,
)
from interface.extension_release import (
    EXTENSION_RELEASE_ATTESTATION_SCHEMA_VERSION,
    EXTENSION_RELEASE_MANIFEST_SCHEMA_VERSION,
    EXTENSION_RELEASE_POLICY_DIGEST,
    EXTENSION_RELEASE_POLICY_REVISION,
    EXTENSION_RELEASE_REVOCATION_SCHEMA_VERSION,
    EXTENSION_RELEASE_SIGNATURE_DOMAIN,
    ExtensionReleaseAttestation,
    ExtensionReleaseAuthority,
    ExtensionReleaseManifest,
    ExtensionReleaseRevocation,
    canonical_utc,
    parse_canonical_utc,
    parse_extension_release_attestation,
    parse_extension_release_revocation,
)
from interface.extension_spec import parse_extension_spec
from runtime.extension_release_signer import (
    Ed25519ReleaseSigner,
    Ed25519ReleaseTrustStore,
    ExtensionReleaseSignatureError,
    ExtensionReleaseSigningError,
)


STATE_FILE = "phase6_extension_release_state.json"
GENERATION_STATE_FILE = "phase6_extension_generation_state.json"
DYNAMIC_VALIDATION_STATE_FILE = (
    "phase6_extension_dynamic_validation_state.json"
)
STATE_SCHEMA_VERSION = "veyra.phase6.extension_release_state.v1"
RECORD_SCHEMA_VERSION = "veyra.phase6.extension_release_private_record.v1"
EVENT_SCHEMA_VERSION = "veyra.phase6.extension_release_lifecycle_event.v1"
OPERATION_SCHEMA_VERSION = "veyra.phase6.extension_release_operation.v1"
PUBLIC_STATUS_SCHEMA = "veyra.phase6.extension_release_status.v1"
PUBLIC_RECORD_SCHEMA = "veyra.phase6.extension_release_record.v1"
PUBLIC_LIST_SCHEMA = "veyra.phase6.extension_release_list.v1"
PUBLIC_INTEGRITY_SCHEMA = "veyra.phase6.extension_release_integrity.v1"
RELEASE_ENABLE_ENV = "VEYRA_PHASE6_EXTENSION_RELEASE_ENABLED"
RELEASE_TOKEN_ENV = "VEYRA_LOCAL_API_TOKEN"

MAX_RELEASES = 200
MAX_OPERATIONS = 2_000
MAX_LIFECYCLE_EVENTS = 1_000
MAX_RELEASE_VALIDITY_DAYS = 7

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,239}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_RELEASE_ID = re.compile(r"^extrel_[0-9a-f]{24}$")
_GENERATION_ID = re.compile(r"^extgen_[0-9a-f]{24}$")
_VALIDATION_ID = re.compile(r"^extval_[0-9a-f]{24}$")
_STAGES = frozenset({"RELEASE_SIGNED", "RELEASE_REVOKED"})
_METADATA_FIELDS = frozenset(
    {
        "_state_revision",
        "source",
        "confidence",
        "ttl_seconds",
        "status",
    }
)
_STATE_FIELDS = frozenset(
    {
        "schema_version",
        "registry_revision",
        "releases",
        "manifest_index",
        "subject_index",
        "operation_index",
        "lifecycle_events",
        "updated_at",
    }
)
_RECORD_FIELDS = frozenset(
    {
        "schema_version",
        "release_id",
        "owner_scope_digest",
        "principal_digest",
        "session_digest",
        "manifest_digest",
        "attestation",
        "attestation_digest",
        "stage",
        "revision",
        "revocation",
        "created_at",
        "updated_at",
    }
)
_OPERATION_FIELDS = frozenset(
    {
        "schema_version",
        "kind",
        "request_digest",
        "release_id",
        "recorded_at",
    }
)
_EVENT_FIELDS = frozenset(
    {
        "schema_version",
        "sequence",
        "release_id",
        "transition",
        "from_stage",
        "to_stage",
        "release_revision",
        "attestation_digest",
        "reason_digest",
        "recorded_at",
    }
)


class ExtensionReleaseError(RuntimeError):
    """Base error for signed release attestation and private registry."""


class ExtensionReleaseConflictError(ExtensionReleaseError):
    """A CAS, operation replay, identity, or lifecycle binding conflicted."""


class ExtensionReleaseNotFoundError(ExtensionReleaseError):
    """The requested release or prerequisite was absent from the owner scope."""


class ExtensionReleaseStorageError(ExtensionReleaseError):
    """Durable release or prerequisite evidence is corrupt or unavailable."""


class ExtensionReleaseUnavailableError(ExtensionReleaseError):
    """The release feature, token, key, or trust root is unavailable."""


class ExtensionReleaseUnauthorizedError(ExtensionReleaseError):
    """The exact local control token was missing or invalid."""


class ExtensionReleaseRegistry:
    """Ed25519 attestation plus a private immutable release registry.

    This slice signs source-free identities only. It has no install, activation,
    execution, Agent, tool, canary or promotion path. ``deployment_subject`` is
    an internal, token/CAS-bound handoff that returns private source only after
    the release and its exact prerequisite reports are revalidated.
    """

    def __init__(
        self,
        *,
        state_store: WorldStateStore,
        generation_gate: Any,
        dynamic_validation_gate: Any,
        signer: Ed25519ReleaseSigner,
        trust_store: Ed25519ReleaseTrustStore,
        enabled: bool = False,
        control_token: str = "",
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.state_store = state_store
        self.generation_gate = generation_gate
        self.dynamic_validation_gate = dynamic_validation_gate
        self.signer = signer
        self.trust_store = trust_store
        self.enabled = bool(enabled)
        self.control_token = str(control_token or "").strip()
        self._now = now or (lambda: datetime.now(timezone.utc))
        signer.assert_external_to([state_store.root])
        if signer.key_id not in trust_store.key_ids:
            raise ExtensionReleaseUnavailableError(
                "release signer key is absent from public trust roots"
            )
        if (
            trust_store.raw_public_key(signer.key_id)
            != signer.public_key_raw
        ):
            raise ExtensionReleaseUnavailableError(
                "release signer does not match its public trust root"
            )
        self._signer_identity = signer.public_identity()

    def status(self, *, control_token: str) -> dict[str, Any]:
        self._authorize(control_token, require_enabled=False)
        try:
            state = self._read_valid_state()
            counts = {"RELEASE_SIGNED": 0, "RELEASE_REVOKED": 0, "EXPIRED": 0}
            for record in state["releases"].values():
                effective = self._effective_stage(record)
                counts["EXPIRED" if effective == "RELEASE_EXPIRED" else record["stage"]] += 1
            storage = {
                "status": "ready",
                "issue": None,
                "release_count": len(state["releases"]),
                "operation_count": len(state["operation_index"]),
                "event_count": len(state["lifecycle_events"]),
                "registry_revision": state["registry_revision"],
                "counts": counts,
            }
            health = "available"
        except Exception as exc:
            storage = {
                "status": "fault",
                "issue": self._safe_issue(exc),
                "release_count": 0,
                "operation_count": 0,
                "event_count": 0,
                "registry_revision": None,
                "counts": {},
            }
            health = "degraded"
        return {
            "schema_version": PUBLIC_STATUS_SCHEMA,
            "phase": "6.2f",
            "status": (
                "technical_complete_signed_private_registry_only"
                if health == "available"
                else "fail_closed"
            ),
            "operational_health": health,
            "completion_scope": (
                "ed25519 release attestation and immutable private registry only"
            ),
            "admission": {
                "enabled": self.enabled,
                "control_token_configured": bool(self.control_token),
                "signer_configured": True,
                "trusted_signing_key": self.signer.key_id in self.trust_store.key_ids,
            },
            "signer": dict(self._signer_identity),
            "trust_roots": self.trust_store.public_status(),
            "storage": storage,
            "installation_status": "not_implemented",
            "activation_status": "not_installed",
            "capability_registry_visible": False,
            "canary_status": "not_started",
            "promotion_authorized": False,
            "read_side_effects": "none",
            "private_key_material_present_in_response": False,
            "authority": self._authority(),
        }

    def create(
        self,
        *,
        generation_id: str,
        validation_id: str,
        user_id: str,
        workspace_id: str,
        session_id: str,
        operation_id: str,
        expected_registry_revision: int,
        expected_generation_report_digest: str,
        expected_validation_report_digest: str,
        expected_generator_identity_digest: str,
        expected_verifier_identity_digest: str,
        expected_signing_identity_digest: str,
        expires_at: str,
        control_token: str,
    ) -> dict[str, Any]:
        token = self._authorize(control_token, require_enabled=True)
        selected_generation = self._required_id(
            generation_id, "generation_id", _GENERATION_ID
        )
        selected_validation = self._required_id(
            validation_id, "validation_id", _VALIDATION_ID
        )
        selected_user = self._required_text(user_id, "user_id")
        selected_workspace = self._required_text(workspace_id, "workspace_id")
        selected_session = self._required_text(session_id, "session_id")
        selected_operation = self._required_id(
            operation_id, "operation_id", _ID
        )
        selected_revision = self._required_revision(
            expected_registry_revision,
            "expected_registry_revision",
            minimum=0,
        )
        expected_generation = self._required_digest(
            expected_generation_report_digest,
            "expected_generation_report_digest",
        )
        expected_validation = self._required_digest(
            expected_validation_report_digest,
            "expected_validation_report_digest",
        )
        expected_generator = self._required_digest(
            expected_generator_identity_digest,
            "expected_generator_identity_digest",
        )
        expected_verifier = self._required_digest(
            expected_verifier_identity_digest,
            "expected_verifier_identity_digest",
        )
        expected_signer = self._required_digest(
            expected_signing_identity_digest,
            "expected_signing_identity_digest",
        )
        selected_expiry = canonical_utc(parse_canonical_utc(expires_at))
        principal = authenticated_local_principal_digest(token)
        session = initiating_session_digest(selected_session)
        owner = artifact_owner_scope_digest(selected_user, selected_workspace)
        operation_key = self._operation_key(
            principal, session, selected_operation
        )
        request_digest = self._digest(
            {
                "schema_version": "veyra.phase6.extension_release_request.v1",
                "kind": "create",
                "generation_id": selected_generation,
                "validation_id": selected_validation,
                "owner_scope_digest": owner,
                "principal_digest": principal,
                "session_digest": session,
                "expected_registry_revision": selected_revision,
                "expected_generation_report_digest": expected_generation,
                "expected_validation_report_digest": expected_validation,
                "expected_generator_identity_digest": expected_generator,
                "expected_verifier_identity_digest": expected_verifier,
                "expected_signing_identity_digest": expected_signer,
                "expires_at": selected_expiry,
            }
        )
        self._assert_workspace(selected_workspace)
        state = self._read_valid_state()
        replay = self._existing_operation(
            state,
            operation_key=operation_key,
            request_digest=request_digest,
            owner_scope_digest=owner,
        )
        if replay is not None:
            return self._public_record(replay, state, operation_replayed=True)
        if state["registry_revision"] != selected_revision:
            raise ExtensionReleaseConflictError(
                "release registry revision changed"
            )
        prerequisites = self._exact_prerequisites(
            generation_id=selected_generation,
            validation_id=selected_validation,
            user_id=selected_user,
            workspace_id=selected_workspace,
            session_id=selected_session,
            control_token=token,
            expected_generation_report_digest=expected_generation,
            expected_validation_report_digest=expected_validation,
        )
        generator_identity = self._generator_identity(
            prerequisites["generation_report"]
        )
        verifier_identity = self._verifier_identity(
            prerequisites["validation_report"]
        )
        if (
            generator_identity != expected_generator
            or verifier_identity != expected_verifier
            or self.signer.identity_digest != expected_signer
        ):
            raise ExtensionReleaseConflictError(
                "release service identity expectation mismatch"
            )
        self._assert_identity_separation(
            prerequisites["generation_report"],
            prerequisites["validation_report"],
            generator_identity,
            verifier_identity,
        )
        now = self._now_value()
        expiry = parse_canonical_utc(selected_expiry)
        generation_report = prerequisites["generation_report"]
        validation_report = prerequisites["validation_report"]
        candidate_expiry = self._parse_prerequisite_time(
            generation_report.binding.candidate_expires_at
        )
        if (
            expiry <= now
            or expiry > candidate_expiry
            or expiry > now + timedelta(days=MAX_RELEASE_VALIDITY_DAYS)
        ):
            raise ExtensionReleaseConflictError(
                "release expiry exceeds the fresh prerequisite window"
            )
        manifest = self._manifest(
            generation_report=generation_report,
            generation_report_digest=expected_generation,
            validation_report=validation_report,
            validation_report_digest=expected_validation,
            generator_identity=generator_identity,
            verifier_identity=verifier_identity,
            workspace_id=selected_workspace,
            created_at=canonical_utc(now),
            expires_at=selected_expiry,
        )
        signature = self.signer.sign(
            EXTENSION_RELEASE_SIGNATURE_DOMAIN + manifest.canonical_bytes()
        )
        attestation = ExtensionReleaseAttestation(
            schema_version=EXTENSION_RELEASE_ATTESTATION_SCHEMA_VERSION,
            release_id=manifest.release_id(),
            manifest=manifest,
            manifest_digest=manifest.manifest_digest(),
            signing_key_id=self.signer.key_id,
            signature_b64url=signature,
            authority=ExtensionReleaseAuthority(),
        )
        self.trust_store.verify_attestation(attestation)

        # Re-read both durable prerequisites after signing. Their reports must
        # still be the exact content included in the signed manifest.
        self._exact_prerequisites(
            generation_id=selected_generation,
            validation_id=selected_validation,
            user_id=selected_user,
            workspace_id=selected_workspace,
            session_id=selected_session,
            control_token=token,
            expected_generation_report_digest=expected_generation,
            expected_validation_report_digest=expected_validation,
        )
        created: dict[str, Any] | None = None
        replayed = False
        existing = False

        def register(current: dict[str, Any]) -> None:
            nonlocal created, replayed, existing
            self._initialize_mutation_state(current)
            self._validate_state(current)
            prior = self._existing_operation(
                current,
                operation_key=operation_key,
                request_digest=request_digest,
                owner_scope_digest=owner,
            )
            if prior is not None:
                created = copy.deepcopy(prior)
                replayed = True
                return
            if current["registry_revision"] != selected_revision:
                raise ExtensionReleaseConflictError(
                    "release registry revision changed before registration"
                )
            subject_key = self._subject_key(manifest)
            prior_id = current["subject_index"].get(subject_key)
            if prior_id is not None:
                prior_record = self._record_by_id(current, str(prior_id))
                self._require_owner(prior_record, owner, principal, session)
                prior_attestation = parse_extension_release_attestation(
                    prior_record["attestation"]
                )
                if (
                    prior_attestation.manifest.generation_report_digest
                    != expected_generation
                    or prior_attestation.manifest.validation_report_digest
                    != expected_validation
                ):
                    raise ExtensionReleaseConflictError(
                        "release subject already has a different attestation"
                    )
                self._record_operation(
                    current,
                    operation_key=operation_key,
                    kind="create",
                    request_digest=request_digest,
                    release_id=prior_record["release_id"],
                    recorded_at=canonical_utc(now),
                )
                current["registry_revision"] += 1
                current["updated_at"] = canonical_utc(now)
                created = copy.deepcopy(prior_record)
                existing = True
                return
            if len(current["releases"]) >= MAX_RELEASES:
                raise ExtensionReleaseConflictError(
                    "release registry capacity is exhausted"
                )
            release_id = attestation.release_id
            if (
                release_id in current["releases"]
                or attestation.manifest_digest in current["manifest_index"]
            ):
                raise ExtensionReleaseStorageError(
                    "content-addressed release identity collided"
                )
            record = {
                "schema_version": RECORD_SCHEMA_VERSION,
                "release_id": release_id,
                "owner_scope_digest": owner,
                "principal_digest": principal,
                "session_digest": session,
                "manifest_digest": attestation.manifest_digest,
                "attestation": attestation.canonical_dict(),
                "attestation_digest": attestation.attestation_digest(),
                "stage": "RELEASE_SIGNED",
                "revision": 1,
                "revocation": None,
                "created_at": manifest.created_at,
                "updated_at": manifest.created_at,
            }
            self._validate_record(record)
            current["releases"][release_id] = record
            current["manifest_index"][attestation.manifest_digest] = release_id
            current["subject_index"][subject_key] = release_id
            self._record_operation(
                current,
                operation_key=operation_key,
                kind="create",
                request_digest=request_digest,
                release_id=release_id,
                recorded_at=manifest.created_at,
            )
            self._append_event(
                current,
                release_id=release_id,
                transition="signed",
                from_stage=None,
                to_stage="RELEASE_SIGNED",
                release_revision=1,
                attestation_digest=record["attestation_digest"],
                reason_digest=None,
                recorded_at=manifest.created_at,
            )
            current["registry_revision"] += 1
            current["updated_at"] = manifest.created_at
            created = copy.deepcopy(record)

        self._mutate(register)
        if created is None:
            raise ExtensionReleaseStorageError(
                "release registration produced no durable record"
            )
        final_state = self._read_valid_state()
        return self._public_record(
            created,
            final_state,
            operation_replayed=replayed,
            release_existing=existing,
        )

    def revoke(
        self,
        *,
        release_id: str,
        user_id: str,
        workspace_id: str,
        session_id: str,
        operation_id: str,
        expected_release_revision: int,
        expected_registry_revision: int,
        reason_digest: str,
        control_token: str,
    ) -> dict[str, Any]:
        token = self._authorize(control_token, require_enabled=True)
        selected_release = self._required_id(
            release_id, "release_id", _RELEASE_ID
        )
        selected_user = self._required_text(user_id, "user_id")
        selected_workspace = self._required_text(workspace_id, "workspace_id")
        selected_session = self._required_text(session_id, "session_id")
        selected_operation = self._required_id(
            operation_id, "operation_id", _ID
        )
        release_revision = self._required_revision(
            expected_release_revision,
            "expected_release_revision",
            minimum=1,
        )
        registry_revision = self._required_revision(
            expected_registry_revision,
            "expected_registry_revision",
            minimum=0,
        )
        selected_reason = self._required_digest(reason_digest, "reason_digest")
        principal = authenticated_local_principal_digest(token)
        session = initiating_session_digest(selected_session)
        owner = artifact_owner_scope_digest(selected_user, selected_workspace)
        operation_key = self._operation_key(
            principal, session, selected_operation
        )
        request_digest = self._digest(
            {
                "schema_version": "veyra.phase6.extension_release_request.v1",
                "kind": "revoke",
                "release_id": selected_release,
                "owner_scope_digest": owner,
                "principal_digest": principal,
                "session_digest": session,
                "expected_release_revision": release_revision,
                "expected_registry_revision": registry_revision,
                "reason_digest": selected_reason,
            }
        )
        self._assert_workspace(selected_workspace)
        revoked: dict[str, Any] | None = None
        replayed = False

        def transition(current: dict[str, Any]) -> None:
            nonlocal revoked, replayed
            self._validate_state(current)
            prior = self._existing_operation(
                current,
                operation_key=operation_key,
                request_digest=request_digest,
                owner_scope_digest=owner,
            )
            if prior is not None:
                revoked = copy.deepcopy(prior)
                replayed = True
                return
            record = self._record_by_id(current, selected_release)
            self._require_owner(record, owner, principal, session)
            if (
                current["registry_revision"] != registry_revision
                or record["revision"] != release_revision
                or record["stage"] != "RELEASE_SIGNED"
            ):
                raise ExtensionReleaseConflictError(
                    "release revocation CAS or lifecycle changed"
                )
            now = self._now_iso()
            revocation = ExtensionReleaseRevocation(
                schema_version=EXTENSION_RELEASE_REVOCATION_SCHEMA_VERSION,
                release_id=selected_release,
                reason_digest=selected_reason,
                source="explicit_local_control_plane",
                recorded_at=now,
            )
            record["stage"] = "RELEASE_REVOKED"
            record["revision"] += 1
            record["revocation"] = revocation.model_dump(mode="json")
            record["updated_at"] = now
            self._append_event(
                current,
                release_id=selected_release,
                transition="revoked",
                from_stage="RELEASE_SIGNED",
                to_stage="RELEASE_REVOKED",
                release_revision=record["revision"],
                attestation_digest=record["attestation_digest"],
                reason_digest=selected_reason,
                recorded_at=now,
            )
            self._record_operation(
                current,
                operation_key=operation_key,
                kind="revoke",
                request_digest=request_digest,
                release_id=selected_release,
                recorded_at=now,
            )
            current["registry_revision"] += 1
            current["updated_at"] = now
            self._validate_record(record)
            revoked = copy.deepcopy(record)

        self._mutate(transition)
        if revoked is None:
            raise ExtensionReleaseStorageError(
                "release revocation produced no durable record"
            )
        return self._public_record(
            revoked,
            self._read_valid_state(),
            operation_replayed=replayed,
        )

    def list(
        self,
        *,
        user_id: str,
        workspace_id: str,
        session_id: str,
        control_token: str,
        limit: int = 50,
    ) -> dict[str, Any]:
        token = self._authorize(control_token, require_enabled=False)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        owner, principal, session, state = self._owner_state(
            user_id, workspace_id, session_id, token
        )
        records = [
            record
            for record in state["releases"].values()
            if record["owner_scope_digest"] == owner
            and record["principal_digest"] == principal
            and record["session_digest"] == session
        ]
        records.sort(
            key=lambda item: (str(item["updated_at"]), str(item["release_id"])),
            reverse=True,
        )
        return {
            "schema_version": PUBLIC_LIST_SCHEMA,
            "count": len(records[:limit]),
            "releases": [
                self._public_record(item, state) for item in records[:limit]
            ],
            "registry_revision": state["registry_revision"],
            "state_mutated": False,
            "authority": self._authority(),
        }

    def get(
        self,
        *,
        release_id: str,
        user_id: str,
        workspace_id: str,
        session_id: str,
        control_token: str,
    ) -> dict[str, Any]:
        token = self._authorize(control_token, require_enabled=False)
        owner, principal, session, state = self._owner_state(
            user_id, workspace_id, session_id, token
        )
        record = self._record_by_id(
            state,
            self._required_id(release_id, "release_id", _RELEASE_ID),
        )
        self._require_owner(record, owner, principal, session)
        return self._public_record(record, state)

    def integrity(
        self,
        *,
        release_id: str,
        user_id: str,
        workspace_id: str,
        session_id: str,
        control_token: str,
    ) -> dict[str, Any]:
        token = self._authorize(control_token, require_enabled=False)
        owner, principal, session, state = self._owner_state(
            user_id, workspace_id, session_id, token
        )
        record = self._record_by_id(
            state,
            self._required_id(release_id, "release_id", _RELEASE_ID),
        )
        self._require_owner(record, owner, principal, session)
        self._validate_record(record)
        attestation = parse_extension_release_attestation(record["attestation"])
        self.trust_store.verify_attestation(attestation)
        public = self._public_record(record, state)
        return {
            **public,
            "schema_version": PUBLIC_INTEGRITY_SCHEMA,
            "status": "signed_release_integrity_passed",
            "manifest_integrity_status": "validated",
            "attestation_integrity_status": "validated",
            "signature_verification_status": "verified",
            "trust_root_status": "trusted",
            "prerequisite_refresh_status": "not_refreshed_on_get",
            "active_prerequisite_verification_required": True,
            "state_mutated": False,
        }

    def deployment_subject(
        self,
        *,
        release_id: str,
        user_id: str,
        workspace_id: str,
        session_id: str,
        expected_release_revision: int,
        expected_attestation_digest: str,
        control_token: str,
    ) -> dict[str, Any]:
        """Return a private, non-executing exact subject for a later gate.

        This method is intentionally not routed. It revalidates freshness,
        revocation, signature, both durable reports, source digest, and spec
        digest before returning bytes. Returned authority remains all false.
        """

        token = self._authorize(control_token, require_enabled=True)
        selected_release = self._required_id(
            release_id, "release_id", _RELEASE_ID
        )
        selected_revision = self._required_revision(
            expected_release_revision,
            "expected_release_revision",
            minimum=1,
        )
        selected_attestation = self._required_digest(
            expected_attestation_digest,
            "expected_attestation_digest",
        )
        selected_user = self._required_text(user_id, "user_id")
        selected_workspace = self._required_text(workspace_id, "workspace_id")
        selected_session = self._required_text(session_id, "session_id")
        owner, principal, session, state = self._owner_state(
            selected_user, selected_workspace, selected_session, token
        )
        record = self._record_by_id(state, selected_release)
        self._require_owner(record, owner, principal, session)
        if (
            record["revision"] != selected_revision
            or record["attestation_digest"] != selected_attestation
            or self._effective_stage(record) != "RELEASE_SIGNED"
        ):
            raise ExtensionReleaseConflictError(
                "release is not an exact fresh signed deployment subject"
            )
        attestation = parse_extension_release_attestation(record["attestation"])
        self.trust_store.verify_attestation(attestation)
        manifest = attestation.manifest
        prerequisites = self._exact_prerequisites(
            generation_id=manifest.generation_id,
            validation_id=manifest.validation_id,
            user_id=selected_user,
            workspace_id=selected_workspace,
            session_id=selected_session,
            control_token=token,
            expected_generation_report_digest=manifest.generation_report_digest,
            expected_validation_report_digest=manifest.validation_report_digest,
        )
        if (
            self._generator_identity(prerequisites["generation_report"])
            != manifest.generator_identity_digest
            or self._verifier_identity(prerequisites["validation_report"])
            != manifest.verifier_identity_digest
        ):
            raise ExtensionReleaseConflictError(
                "release prerequisite service identity changed"
            )
        private = self._private_dynamic_subject(
            report=prerequisites["validation_report"],
            user_id=selected_user,
            workspace_id=selected_workspace,
        )
        source_bytes = private.get("source_bytes")
        raw_spec = private.get("source_check_spec")
        if not isinstance(source_bytes, bytes) or not isinstance(raw_spec, dict):
            raise ExtensionReleaseStorageError(
                "private signed release source or spec is unavailable"
            )
        spec = parse_extension_spec(raw_spec)
        if (
            hashlib.sha256(source_bytes).hexdigest() != manifest.artifact_sha256
            or len(source_bytes) != manifest.artifact_size_bytes
            or spec.digest() != manifest.spec_digest
            or any(private.get("authority", {}).values())
        ):
            raise ExtensionReleaseStorageError(
                "private signed release source binding is invalid"
            )
        return {
            "schema_version": (
                "veyra.phase6.extension_release_deployment_subject.v1"
            ),
            "release_id": manifest.release_id(),
            "release_revision": record["revision"],
            "owner_scope_digest": owner,
            "principal_digest": principal,
            "session_digest": session,
            "manifest_digest": attestation.manifest_digest,
            "attestation_digest": record["attestation_digest"],
            "signature_algorithm": "ed25519",
            "signing_key_id": attestation.signing_key_id,
            "signing_service_identity_digest": (
                manifest.signing_service_identity_digest
            ),
            "generator_identity_digest": manifest.generator_identity_digest,
            "verifier_identity_digest": manifest.verifier_identity_digest,
            "validation_build_identity_digest": (
                manifest.validation_build_identity_digest
            ),
            "generation_report_digest": manifest.generation_report_digest,
            "validation_report_digest": manifest.validation_report_digest,
            "artifact_id": manifest.artifact_id,
            "artifact_revision": manifest.artifact_revision,
            "artifact_sha256": manifest.artifact_sha256,
            "spec_digest": manifest.spec_digest,
            "source_bytes": source_bytes,
            "source_check_spec": spec.canonical_dict(),
            "release_manifest": manifest.canonical_dict(),
            "validation_binding": (
                prerequisites["validation_report"].binding.canonical_dict()
            ),
            "validation_report": (
                prerequisites["validation_report"].canonical_dict()
            ),
            "expires_at": manifest.expires_at,
            "authority": self._authority(),
        }

    def _exact_prerequisites(
        self,
        *,
        generation_id: str,
        validation_id: str,
        user_id: str,
        workspace_id: str,
        session_id: str,
        control_token: str,
        expected_generation_report_digest: str,
        expected_validation_report_digest: str,
    ) -> dict[str, Any]:
        generation_subject = self._generation_subject(
            generation_id=generation_id,
            user_id=user_id,
            workspace_id=workspace_id,
            session_id=session_id,
            control_token=control_token,
        )
        validation_subject = self._validation_subject(
            validation_id=validation_id,
            user_id=user_id,
            workspace_id=workspace_id,
            session_id=session_id,
            control_token=control_token,
        )
        generation = parse_extension_generation_report(
            generation_subject.get("report")
        )
        validation = parse_dynamic_validation_report(
            validation_subject.get("report")
        )
        if (
            generation_subject.get("stage") != "GENERATION_QUARANTINED"
            or generation.generation_status != "quarantined"
            or generation_subject.get("report_digest")
            != expected_generation_report_digest
            or generation.report_digest() != expected_generation_report_digest
        ):
            raise ExtensionReleaseConflictError(
                "exact successful generation report is unavailable"
            )
        if (
            validation_subject.get("stage") != "DYNAMIC_VALIDATION_PASSED"
            or validation.validation_status != "passed"
            or validation.candidate_execution_status != "passed"
            or any(
                getattr(validation, field) != "passed"
                for field in (
                    "unit_checks_status",
                    "contract_checks_status",
                    "security_runtime_checks_status",
                    "fuzz_checks_status",
                    "behavior_verification_status",
                )
            )
            or validation.issue_codes
            or validation_subject.get("report_digest")
            != expected_validation_report_digest
            or validation.report_digest() != expected_validation_report_digest
            or any(validation.authority.model_dump(mode="python").values())
            or validation.signature_status != "not_implemented"
            or validation.activation_status != "not_installed"
            or validation.capability_registry_visible
            or validation.canary_status != "not_started"
            or validation.promotion_authorized
            or validation.policy_effect != "none"
        ):
            raise ExtensionReleaseConflictError(
                "exact successful dynamic validation report is unavailable"
            )
        gb = generation.binding
        vb = validation.binding
        if (
            gb.generation_id != generation_id
            or vb.validation_id != validation_id
            or gb.owner_scope_digest != vb.owner_scope_digest
            or gb.authenticated_principal_digest
            != vb.authenticated_principal_digest
            or gb.initiating_session_digest
            != vb.initiating_session_digest
            or gb.candidate_id != vb.candidate_id
            or gb.candidate_revision != vb.candidate_revision
            or gb.extension_id != vb.extension_id
            or gb.extension_version != vb.extension_version
            or gb.spec_digest != vb.spec_digest
            or generation.artifact_id != vb.artifact_id
            or generation.artifact_sha256 != vb.artifact_sha256
            or generation.artifact_size_bytes != vb.artifact_size_bytes
            or generation.artifact_envelope_digest
            != vb.artifact_envelope_digest
            or gb.authenticated_principal_digest
            != authenticated_local_principal_digest(control_token)
            or gb.initiating_session_digest
            != initiating_session_digest(session_id)
        ):
            raise ExtensionReleaseConflictError(
                "generation and dynamic validation subjects are not exact matches"
            )
        generated_at = parse_canonical_utc(generation.generated_at)
        completed_at = parse_canonical_utc(validation.completed_at)
        now = self._now_value()
        if generated_at > completed_at or completed_at > now:
            raise ExtensionReleaseConflictError(
                "release prerequisite chronology is invalid"
            )
        return {
            "generation_report": generation,
            "validation_report": validation,
        }

    def _generation_subject(self, **kwargs: Any) -> dict[str, Any]:
        resolver = getattr(self.generation_gate, "signed_release_subject", None)
        if callable(resolver):
            subject = resolver(**kwargs)
            if not isinstance(subject, dict):
                raise ExtensionReleaseStorageError(
                    "generation release subject is invalid"
                )
            return subject
        state_store = getattr(self.generation_gate, "state_store", self.state_store)
        integrity = getattr(self.generation_gate, "integrity", None)
        if callable(integrity):
            integrity(**kwargs)
        state = state_store.read_json(GENERATION_STATE_FILE)
        record = state.get("generations", {}).get(kwargs["generation_id"])
        if not isinstance(record, dict):
            raise ExtensionReleaseNotFoundError(
                "generation prerequisite was not found"
            )
        principal = authenticated_local_principal_digest(kwargs["control_token"])
        session = initiating_session_digest(kwargs["session_id"])
        if (
            record.get("user_id") != kwargs["user_id"]
            or record.get("workspace_id") != kwargs["workspace_id"]
            or record.get("principal_digest") != principal
            or record.get("session_digest") != session
        ):
            raise ExtensionReleaseNotFoundError(
                "generation prerequisite was not found"
            )
        report = parse_extension_generation_report(record.get("report"))
        if report.report_digest() != record.get("report_digest"):
            raise ExtensionReleaseStorageError(
                "generation prerequisite report integrity failed"
            )
        return {
            "stage": record.get("stage"),
            "report": report.canonical_dict(),
            "report_digest": record.get("report_digest"),
        }

    def _validation_subject(self, **kwargs: Any) -> dict[str, Any]:
        resolver = getattr(
            self.dynamic_validation_gate, "signed_release_subject", None
        )
        if callable(resolver):
            subject = resolver(**kwargs)
            if not isinstance(subject, dict):
                raise ExtensionReleaseStorageError(
                    "dynamic validation release subject is invalid"
                )
            return subject
        state_store = getattr(
            self.dynamic_validation_gate, "state_store", self.state_store
        )
        integrity = getattr(self.dynamic_validation_gate, "integrity", None)
        if callable(integrity):
            integrity(
                validation_id=kwargs["validation_id"],
                user_id=kwargs["user_id"],
                workspace_id=kwargs["workspace_id"],
                session_id=kwargs["session_id"],
                control_token=kwargs["control_token"],
            )
        state = state_store.read_json(DYNAMIC_VALIDATION_STATE_FILE)
        record = state.get("validations", {}).get(kwargs["validation_id"])
        if not isinstance(record, dict):
            raise ExtensionReleaseNotFoundError(
                "dynamic validation prerequisite was not found"
            )
        owner = artifact_owner_scope_digest(
            kwargs["user_id"], kwargs["workspace_id"]
        )
        if record.get("owner_scope_digest") != owner:
            raise ExtensionReleaseNotFoundError(
                "dynamic validation prerequisite was not found"
            )
        report = parse_dynamic_validation_report(record.get("report"))
        if report.report_digest() != record.get("report_digest"):
            raise ExtensionReleaseStorageError(
                "dynamic validation prerequisite report integrity failed"
            )
        return {
            "stage": record.get("stage"),
            "report": report.canonical_dict(),
            "report_digest": record.get("report_digest"),
        }

    def _private_dynamic_subject(
        self,
        *,
        report: DynamicValidationReport,
        user_id: str,
        workspace_id: str,
    ) -> dict[str, Any]:
        resolver = getattr(
            self.dynamic_validation_gate, "deployment_subject", None
        )
        if callable(resolver):
            subject = resolver(
                validation_id=report.binding.validation_id,
                user_id=user_id,
                workspace_id=workspace_id,
                expected_validation_report_digest=report.report_digest(),
            )
            if isinstance(subject, dict):
                return subject
        runner_gate = getattr(
            self.dynamic_validation_gate, "isolated_runner_gate", None
        )
        resolver = getattr(runner_gate, "dynamic_validation_subject", None)
        if not callable(resolver):
            raise ExtensionReleaseUnavailableError(
                "private dynamic-validation source resolver is unavailable"
            )
        b = report.binding
        subject = resolver(
            run_id=b.isolated_run_id,
            user_id=user_id,
            workspace_id=workspace_id,
            expected_artifact_revision=b.artifact_revision,
            expected_artifact_sha256=b.artifact_sha256,
            expected_source_check_report_digest=b.source_check_report_digest,
            expected_isolated_runner_report_digest=(
                b.isolated_runner_report_digest
            ),
        )
        if not isinstance(subject, dict):
            raise ExtensionReleaseStorageError(
                "private dynamic-validation source subject is invalid"
            )
        return subject

    def _manifest(
        self,
        *,
        generation_report: ExtensionGenerationReport,
        generation_report_digest: str,
        validation_report: DynamicValidationReport,
        validation_report_digest: str,
        generator_identity: str,
        verifier_identity: str,
        workspace_id: str,
        created_at: str,
        expires_at: str,
    ) -> ExtensionReleaseManifest:
        gb = generation_report.binding
        vb = validation_report.binding
        return ExtensionReleaseManifest(
            schema_version=EXTENSION_RELEASE_MANIFEST_SCHEMA_VERSION,
            owner_scope_digest=gb.owner_scope_digest,
            workspace_identity_digest=self._workspace_digest(workspace_id),
            authenticated_principal_digest=gb.authenticated_principal_digest,
            initiating_session_digest=gb.initiating_session_digest,
            request_provenance_digest=gb.request_provenance_digest,
            generation_id=gb.generation_id,
            generation_binding_digest=generation_report.binding_digest,
            generation_report_digest=generation_report_digest,
            generator_revision=gb.generator_revision,
            generation_policy_revision=gb.generation_policy_revision,
            generation_policy_digest=gb.generation_policy_digest,
            generation_prompt_digest=gb.prompt_digest,
            generator_provider_id=gb.provider_id,
            generator_model_id=gb.model_id,
            generator_model_config_digest=gb.model_config_digest,
            generator_identity_digest=generator_identity,
            validation_id=vb.validation_id,
            validation_binding_digest=validation_report.binding_digest,
            validation_report_digest=validation_report_digest,
            validation_authenticated_principal_digest=(
                vb.authenticated_principal_digest
            ),
            validation_initiating_session_digest=(
                vb.initiating_session_digest
            ),
            validation_request_id=vb.request_id,
            validation_request_provenance_digest=(
                vb.request_provenance_digest
            ),
            validation_build_identity_digest=vb.build_identity_digest,
            validation_test_bundle_digest=vb.test_bundle_digest,
            validation_policy_revision=vb.validation_policy_revision,
            validation_policy_digest=vb.validation_policy_digest,
            validation_harness_revision=vb.validation_harness_revision,
            validation_harness_digest=vb.validation_harness_digest,
            validation_engine_identity_digest=vb.engine_identity_digest,
            validation_image_id=vb.image_id,
            isolation_conformance_digest=vb.isolation_conformance_digest,
            validation_conformance_digest=vb.validation_conformance_digest,
            verifier_identity_digest=verifier_identity,
            isolated_run_id=vb.isolated_run_id,
            isolated_runner_binding_digest=vb.isolated_runner_binding_digest,
            isolated_runner_report_digest=vb.isolated_runner_report_digest,
            isolated_runner_policy_revision=vb.isolated_runner_policy_revision,
            isolated_runner_harness_revision=vb.isolated_runner_harness_revision,
            source_check_id=vb.source_check_id,
            source_check_binding_digest=vb.source_check_binding_digest,
            source_check_report_digest=vb.source_check_report_digest,
            source_parser_identity=vb.source_parser_identity,
            source_ruleset_digest=vb.source_ruleset_digest,
            candidate_id=vb.candidate_id,
            candidate_revision=vb.candidate_revision,
            extension_id=vb.extension_id,
            extension_version=vb.extension_version,
            spec_digest=vb.spec_digest,
            artifact_id=vb.artifact_id,
            artifact_revision=vb.artifact_revision,
            artifact_envelope_digest=vb.artifact_envelope_digest,
            artifact_sha256=vb.artifact_sha256,
            artifact_size_bytes=vb.artifact_size_bytes,
            signing_service_id=self.signer.signing_service_id,
            signing_service_identity_digest=self.signer.identity_digest,
            signing_key_id=self.signer.key_id,
            release_policy_revision=EXTENSION_RELEASE_POLICY_REVISION,
            release_policy_digest=EXTENSION_RELEASE_POLICY_DIGEST,
            created_at=created_at,
            expires_at=expires_at,
        )

    def _generator_identity(self, report: ExtensionGenerationReport) -> str:
        b = report.binding
        return self._digest(
            {
                "schema_version": (
                    "veyra.phase6.extension_generator_service_identity.v1"
                ),
                "provider_id": b.provider_id,
                "model_id": b.model_id,
                "model_config_digest": b.model_config_digest,
                "generator_revision": b.generator_revision,
                "generation_policy_revision": b.generation_policy_revision,
                "generation_policy_digest": b.generation_policy_digest,
                "prompt_digest": b.prompt_digest,
            }
        )

    def _verifier_identity(self, report: DynamicValidationReport) -> str:
        b = report.binding
        return self._digest(
            {
                "schema_version": (
                    "veyra.phase6.extension_verifier_service_identity.v1"
                ),
                "source_parser_identity": b.source_parser_identity,
                "source_ruleset_digest": b.source_ruleset_digest,
                "engine_identity_digest": b.engine_identity_digest,
                "image_id": b.image_id,
                "isolation_conformance_digest": b.isolation_conformance_digest,
                "validation_conformance_digest": b.validation_conformance_digest,
                "validation_policy_revision": b.validation_policy_revision,
                "validation_policy_digest": b.validation_policy_digest,
                "validation_harness_revision": b.validation_harness_revision,
                "validation_harness_digest": b.validation_harness_digest,
            }
        )

    def _assert_identity_separation(
        self,
        generation: ExtensionGenerationReport,
        validation: DynamicValidationReport,
        generator_identity: str,
        verifier_identity: str,
    ) -> None:
        if len(
            {
                generator_identity,
                verifier_identity,
                self.signer.identity_digest,
            }
        ) != 3:
            raise ExtensionReleaseConflictError(
                "generator verifier and signer identities collapsed"
            )
        forbidden_signer_names = {
            generation.binding.provider_id,
            generation.binding.model_id,
            generation.binding.generator_revision,
            validation.binding.source_parser_identity,
            validation.binding.validation_harness_revision,
        }
        if self.signer.signing_service_id in forbidden_signer_names:
            raise ExtensionReleaseConflictError(
                "signing service identity matches generator or verifier"
            )

    def _public_record(
        self,
        record: dict[str, Any],
        state: dict[str, Any],
        *,
        operation_replayed: bool = False,
        release_existing: bool = False,
    ) -> dict[str, Any]:
        self._validate_record(record)
        attestation = parse_extension_release_attestation(record["attestation"])
        self.trust_store.verify_attestation(attestation)
        manifest = attestation.manifest
        effective = self._effective_stage(record)
        return {
            "schema_version": PUBLIC_RECORD_SCHEMA,
            "release_id": record["release_id"],
            "release_revision": record["revision"],
            "registry_revision": state["registry_revision"],
            "stored_stage": record["stage"],
            "effective_status": effective,
            "fresh": effective == "RELEASE_SIGNED",
            "revoked": record["stage"] == "RELEASE_REVOKED",
            "manifest_digest": record["manifest_digest"],
            "attestation_digest": record["attestation_digest"],
            "attestation": attestation.canonical_dict(),
            "generation_id": manifest.generation_id,
            "generation_report_digest": manifest.generation_report_digest,
            "validation_id": manifest.validation_id,
            "validation_report_digest": manifest.validation_report_digest,
            "validation_build_identity_digest": (
                manifest.validation_build_identity_digest
            ),
            "candidate_id": manifest.candidate_id,
            "candidate_revision": manifest.candidate_revision,
            "extension_id": manifest.extension_id,
            "extension_version": manifest.extension_version,
            "artifact_id": manifest.artifact_id,
            "artifact_revision": manifest.artifact_revision,
            "artifact_sha256": manifest.artifact_sha256,
            "signature_algorithm": "ed25519",
            "signing_key_id": manifest.signing_key_id,
            "signature_verification_status": "verified",
            "created_at": record["created_at"],
            "expires_at": manifest.expires_at,
            "updated_at": record["updated_at"],
            "revocation": copy.deepcopy(record["revocation"]),
            "operation_replayed": operation_replayed,
            "release_existing": release_existing,
            "source_in_response": False,
            "private_key_material_in_response": False,
            "installation_status": "not_installed",
            "activation_status": "not_installed",
            "capability_registry_visible": False,
            "canary_status": "not_started",
            "promotion_authorized": False,
            "policy_effect": "none",
            "authority": self._authority(),
        }

    def _read_valid_state(self) -> dict[str, Any]:
        try:
            state = self.state_store.read_json(STATE_FILE)
            if not state:
                state = self._initial_state()
            self._validate_state(state)
            return state
        except ExtensionReleaseError:
            raise
        except Exception as exc:
            raise ExtensionReleaseStorageError(
                "release registry state is unavailable"
            ) from exc

    def _owner_state(
        self,
        user_id: str,
        workspace_id: str,
        session_id: str,
        control_token: str,
    ) -> tuple[str, str, str, dict[str, Any]]:
        user = self._required_text(user_id, "user_id")
        workspace = self._required_text(workspace_id, "workspace_id")
        session_value = self._required_text(session_id, "session_id")
        self._assert_workspace(workspace)
        return (
            artifact_owner_scope_digest(user, workspace),
            authenticated_local_principal_digest(control_token),
            initiating_session_digest(session_value),
            self._read_valid_state(),
        )

    @staticmethod
    def _initial_state() -> dict[str, Any]:
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "registry_revision": 0,
            "releases": {},
            "manifest_index": {},
            "subject_index": {},
            "operation_index": {},
            "lifecycle_events": [],
            "updated_at": None,
        }

    def _initialize_mutation_state(self, state: dict[str, Any]) -> None:
        if not state:
            state.update(self._initial_state())

    def _mutate(self, mutator: Callable[[dict[str, Any]], None]) -> None:
        try:
            self.state_store.mutate_json(STATE_FILE, mutator)
        except ExtensionReleaseError:
            raise
        except Exception as exc:
            raise ExtensionReleaseStorageError(
                "release registry mutation failed"
            ) from exc

    def _validate_state(self, state: Any) -> None:
        if (
            not isinstance(state, dict)
            or state.get("_state_corrupt") is True
            or state.get("schema_version") != STATE_SCHEMA_VERSION
            or bool(set(state) - _STATE_FIELDS - _METADATA_FIELDS)
            or type(state.get("registry_revision")) is not int
            or state["registry_revision"] < 0
            or not isinstance(state.get("releases"), dict)
            or not isinstance(state.get("manifest_index"), dict)
            or not isinstance(state.get("subject_index"), dict)
            or not isinstance(state.get("operation_index"), dict)
            or not isinstance(state.get("lifecycle_events"), list)
            or len(state["releases"]) > MAX_RELEASES
            or len(state["operation_index"]) > MAX_OPERATIONS
            or len(state["lifecycle_events"]) > MAX_LIFECYCLE_EVENTS
        ):
            raise ExtensionReleaseStorageError(
                "release registry state is invalid"
            )
        expected_manifests: dict[str, str] = {}
        expected_subjects: dict[str, str] = {}
        for release_id, record in state["releases"].items():
            if release_id != record.get("release_id"):
                raise ExtensionReleaseStorageError(
                    "release registry index is invalid"
                )
            self._validate_record(record)
            attestation = parse_extension_release_attestation(
                record["attestation"]
            )
            expected_manifests[attestation.manifest_digest] = release_id
            subject = self._subject_key(attestation.manifest)
            if subject in expected_subjects:
                raise ExtensionReleaseStorageError(
                    "release registry subject is duplicated"
                )
            expected_subjects[subject] = release_id
        if (
            state["manifest_index"] != expected_manifests
            or state["subject_index"] != expected_subjects
        ):
            raise ExtensionReleaseStorageError(
                "release content-address indexes are invalid"
            )
        for key, operation in state["operation_index"].items():
            self._validate_operation(key, operation, state)
        self._validate_events(state)
        if state["updated_at"] is not None:
            self._parse_prerequisite_time(state["updated_at"])

    def _validate_record(self, record: Any) -> None:
        if (
            not isinstance(record, dict)
            or set(record) != _RECORD_FIELDS
            or record.get("schema_version") != RECORD_SCHEMA_VERSION
            or not _RELEASE_ID.fullmatch(str(record.get("release_id") or ""))
            or record.get("stage") not in _STAGES
            or type(record.get("revision")) is not int
            or record["revision"] not in {1, 2}
            or not _DIGEST.fullmatch(
                str(record.get("owner_scope_digest") or "")
            )
            or not _DIGEST.fullmatch(str(record.get("principal_digest") or ""))
            or not _DIGEST.fullmatch(str(record.get("session_digest") or ""))
            or not _DIGEST.fullmatch(str(record.get("manifest_digest") or ""))
            or not _DIGEST.fullmatch(
                str(record.get("attestation_digest") or "")
            )
        ):
            raise ExtensionReleaseStorageError("release record is invalid")
        attestation = parse_extension_release_attestation(record["attestation"])
        if (
            attestation.release_id != record["release_id"]
            or attestation.manifest_digest != record["manifest_digest"]
            or attestation.attestation_digest() != record["attestation_digest"]
            or attestation.manifest.owner_scope_digest
            != record["owner_scope_digest"]
            or attestation.manifest.authenticated_principal_digest
            != record["principal_digest"]
            or attestation.manifest.initiating_session_digest
            != record["session_digest"]
            or attestation.manifest.created_at != record["created_at"]
        ):
            raise ExtensionReleaseStorageError(
                "release attestation record binding is invalid"
            )
        parse_canonical_utc(record["created_at"])
        parse_canonical_utc(record["updated_at"])
        if record["stage"] == "RELEASE_SIGNED":
            if record["revision"] != 1 or record["revocation"] is not None:
                raise ExtensionReleaseStorageError(
                    "signed release lifecycle is invalid"
                )
        else:
            revocation = parse_extension_release_revocation(
                record["revocation"]
            )
            if (
                record["revision"] != 2
                or revocation.release_id != record["release_id"]
                or revocation.recorded_at != record["updated_at"]
            ):
                raise ExtensionReleaseStorageError(
                    "revoked release lifecycle is invalid"
                )

    def _validate_operation(
        self, key: Any, operation: Any, state: dict[str, Any]
    ) -> None:
        if (
            not isinstance(key, str)
            or not _DIGEST.fullmatch(key)
            or not isinstance(operation, dict)
            or set(operation) != _OPERATION_FIELDS
            or operation.get("schema_version") != OPERATION_SCHEMA_VERSION
            or operation.get("kind") not in {"create", "revoke"}
            or not _DIGEST.fullmatch(
                str(operation.get("request_digest") or "")
            )
            or operation.get("release_id") not in state["releases"]
        ):
            raise ExtensionReleaseStorageError(
                "release operation binding is invalid"
            )
        parse_canonical_utc(operation["recorded_at"])

    def _validate_events(self, state: dict[str, Any]) -> None:
        expected_sequence = 1
        last_by_release: dict[str, str] = {}
        for event in state["lifecycle_events"]:
            if (
                not isinstance(event, dict)
                or set(event) != _EVENT_FIELDS
                or event.get("schema_version") != EVENT_SCHEMA_VERSION
                or event.get("sequence") != expected_sequence
                or event.get("release_id") not in state["releases"]
                or event.get("transition") not in {"signed", "revoked"}
                or event.get("to_stage") not in _STAGES
                or type(event.get("release_revision")) is not int
                or not _DIGEST.fullmatch(
                    str(event.get("attestation_digest") or "")
                )
            ):
                raise ExtensionReleaseStorageError(
                    "release lifecycle event is invalid"
                )
            parse_canonical_utc(event["recorded_at"])
            release_id = event["release_id"]
            if event["transition"] == "signed":
                valid = (
                    event["from_stage"] is None
                    and event["to_stage"] == "RELEASE_SIGNED"
                    and event["release_revision"] == 1
                    and event["reason_digest"] is None
                    and release_id not in last_by_release
                )
            else:
                valid = (
                    event["from_stage"] == "RELEASE_SIGNED"
                    and event["to_stage"] == "RELEASE_REVOKED"
                    and event["release_revision"] == 2
                    and _DIGEST.fullmatch(str(event["reason_digest"] or ""))
                    and last_by_release.get(release_id) == "RELEASE_SIGNED"
                )
            if not valid:
                raise ExtensionReleaseStorageError(
                    "release lifecycle transition is invalid"
                )
            record = state["releases"][release_id]
            if event["attestation_digest"] != record["attestation_digest"]:
                raise ExtensionReleaseStorageError(
                    "release lifecycle attestation binding is invalid"
                )
            last_by_release[release_id] = event["to_stage"]
            expected_sequence += 1
        if len(last_by_release) != len(state["releases"]):
            raise ExtensionReleaseStorageError(
                "release lifecycle history is incomplete"
            )
        for release_id, record in state["releases"].items():
            if last_by_release.get(release_id) != record["stage"]:
                raise ExtensionReleaseStorageError(
                    "release lifecycle history does not match state"
                )

    def _record_operation(
        self,
        state: dict[str, Any],
        *,
        operation_key: str,
        kind: str,
        request_digest: str,
        release_id: str,
        recorded_at: str,
    ) -> None:
        if len(state["operation_index"]) >= MAX_OPERATIONS:
            raise ExtensionReleaseConflictError(
                "release operation capacity is exhausted"
            )
        state["operation_index"][operation_key] = {
            "schema_version": OPERATION_SCHEMA_VERSION,
            "kind": kind,
            "request_digest": request_digest,
            "release_id": release_id,
            "recorded_at": recorded_at,
        }

    def _existing_operation(
        self,
        state: dict[str, Any],
        *,
        operation_key: str,
        request_digest: str,
        owner_scope_digest: str,
    ) -> dict[str, Any] | None:
        operation = state["operation_index"].get(operation_key)
        if operation is None:
            return None
        self._validate_operation(operation_key, operation, state)
        if operation["request_digest"] != request_digest:
            raise ExtensionReleaseConflictError(
                "release operation identity was rebound"
            )
        record = self._record_by_id(state, operation["release_id"])
        if record["owner_scope_digest"] != owner_scope_digest:
            raise ExtensionReleaseNotFoundError("release was not found")
        return record

    def _append_event(
        self,
        state: dict[str, Any],
        *,
        release_id: str,
        transition: str,
        from_stage: str | None,
        to_stage: str,
        release_revision: int,
        attestation_digest: str,
        reason_digest: str | None,
        recorded_at: str,
    ) -> None:
        if len(state["lifecycle_events"]) >= MAX_LIFECYCLE_EVENTS:
            raise ExtensionReleaseConflictError(
                "release lifecycle event capacity is exhausted"
            )
        state["lifecycle_events"].append(
            {
                "schema_version": EVENT_SCHEMA_VERSION,
                "sequence": len(state["lifecycle_events"]) + 1,
                "release_id": release_id,
                "transition": transition,
                "from_stage": from_stage,
                "to_stage": to_stage,
                "release_revision": release_revision,
                "attestation_digest": attestation_digest,
                "reason_digest": reason_digest,
                "recorded_at": recorded_at,
            }
        )

    @staticmethod
    def _record_by_id(state: dict[str, Any], release_id: str) -> dict[str, Any]:
        record = state["releases"].get(release_id)
        if not isinstance(record, dict):
            raise ExtensionReleaseNotFoundError("release was not found")
        return record

    @staticmethod
    def _require_owner(
        record: dict[str, Any],
        owner: str,
        principal: str,
        session: str,
    ) -> None:
        if (
            record.get("owner_scope_digest") != owner
            or record.get("principal_digest") != principal
            or record.get("session_digest") != session
        ):
            raise ExtensionReleaseNotFoundError("release was not found")

    def _effective_stage(self, record: dict[str, Any]) -> str:
        if record["stage"] == "RELEASE_REVOKED":
            return "RELEASE_REVOKED"
        manifest = parse_extension_release_attestation(
            record["attestation"]
        ).manifest
        if parse_canonical_utc(manifest.expires_at) <= self._now_value():
            return "RELEASE_EXPIRED"
        return "RELEASE_SIGNED"

    def _assert_workspace(self, workspace_id: str) -> None:
        local = self.state_store.read_json("local_world.json")
        if str(local.get("current_project") or "").strip() != workspace_id:
            raise ExtensionReleaseConflictError(
                "workspace must match the current local Veyra scope"
            )

    def _authorize(self, supplied: str, *, require_enabled: bool) -> str:
        if require_enabled and not self.enabled:
            raise ExtensionReleaseUnavailableError(
                "signed release admission is disabled by policy"
            )
        if not self.control_token:
            raise ExtensionReleaseUnavailableError(
                "signed release control token is not configured"
            )
        selected = str(supplied or "").strip()
        if not selected or not secrets.compare_digest(
            selected, self.control_token
        ):
            raise ExtensionReleaseUnauthorizedError(
                "signed release control token is invalid"
            )
        return selected

    @staticmethod
    def _subject_key(manifest: ExtensionReleaseManifest) -> str:
        return ExtensionReleaseRegistry._digest(
            {
                "owner_scope_digest": manifest.owner_scope_digest,
                "extension_id": manifest.extension_id,
                "extension_version": manifest.extension_version,
                "candidate_id": manifest.candidate_id,
                "candidate_revision": manifest.candidate_revision,
                "artifact_id": manifest.artifact_id,
                "artifact_revision": manifest.artifact_revision,
                "validation_build_identity_digest": (
                    manifest.validation_build_identity_digest
                ),
            }
        )

    @staticmethod
    def _operation_key(
        principal_digest: str, session_digest: str, operation_id: str
    ) -> str:
        return ExtensionReleaseRegistry._digest(
            {
                "schema_version": (
                    "veyra.phase6.extension_release_operation_identity.v1"
                ),
                "principal_digest": principal_digest,
                "session_digest": session_digest,
                "operation_id": operation_id,
            }
        )

    @staticmethod
    def _workspace_digest(workspace_id: str) -> str:
        return hashlib.sha256(
            b"veyra.phase6.workspace_identity.v1\x00"
            + workspace_id.encode("utf-8")
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
    def _required_id(value: Any, field: str, pattern: re.Pattern[str]) -> str:
        selected = ExtensionReleaseRegistry._required_text(value, field)
        if not pattern.fullmatch(selected):
            raise ValueError(f"{field} is invalid")
        return selected

    @staticmethod
    def _required_digest(value: Any, field: str) -> str:
        if not isinstance(value, str) or not _DIGEST.fullmatch(value):
            raise ValueError(f"{field} must be a SHA-256 digest")
        return value

    @staticmethod
    def _required_revision(value: Any, field: str, *, minimum: int) -> int:
        if type(value) is not int or not minimum <= value <= 2_147_483_647:
            raise ValueError(f"{field} is invalid")
        return value

    def _now_value(self) -> datetime:
        value = self._now()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ExtensionReleaseStorageError("release clock is invalid")
        return value.astimezone(timezone.utc)

    def _now_iso(self) -> str:
        return canonical_utc(self._now_value())

    @staticmethod
    def _parse_prerequisite_time(value: Any) -> datetime:
        if not isinstance(value, str):
            raise ExtensionReleaseConflictError(
                "release prerequisite timestamp is invalid"
            )
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ExtensionReleaseConflictError(
                "release prerequisite timestamp is invalid"
            ) from exc
        if parsed.tzinfo is None:
            raise ExtensionReleaseConflictError(
                "release prerequisite timestamp must be timezone-aware"
            )
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _authority() -> dict[str, bool]:
        return ExtensionReleaseAuthority().model_dump(mode="json")

    @staticmethod
    def _safe_issue(exc: Exception) -> str:
        if isinstance(exc, ExtensionReleaseStorageError):
            return "extension_release_state_invalid"
        if isinstance(exc, (ExtensionReleaseSignatureError, ExtensionReleaseSigningError)):
            return "extension_release_signature_invalid"
        return f"extension_release_unavailable:{type(exc).__name__}"


__all__ = [
    "DYNAMIC_VALIDATION_STATE_FILE",
    "ExtensionReleaseConflictError",
    "ExtensionReleaseError",
    "ExtensionReleaseNotFoundError",
    "ExtensionReleaseRegistry",
    "ExtensionReleaseStorageError",
    "ExtensionReleaseUnauthorizedError",
    "ExtensionReleaseUnavailableError",
    "GENERATION_STATE_FILE",
    "RELEASE_ENABLE_ENV",
    "RELEASE_TOKEN_ENV",
    "STATE_FILE",
]
