from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import re
import secrets
from typing import Any, Callable, Protocol

from interface.extension_deployment import (
    EXTENSION_DEPLOYMENT_BINDING_SCHEMA_VERSION,
    EXTENSION_DEPLOYMENT_POLICY_DIGEST,
    EXTENSION_DEPLOYMENT_POLICY_REVISION,
    EXTENSION_INVOCATION_BINDING_SCHEMA_VERSION,
    EXTENSION_INVOCATION_HARNESS_REVISION,
    EXTENSION_INVOCATION_RESULT_SCHEMA_VERSION,
    EXTENSION_PUBLIC_CAPABILITY_SCHEMA_VERSION,
    MAX_EXTENSION_DEPLOYMENT_INVOCATIONS,
    MAX_EXTENSION_DEPLOYMENT_RECEIPTS,
    ExtensionDeploymentAuthority,
    ExtensionDeploymentBinding,
    ExtensionInvocationBackendStatus,
    ExtensionInvocationBinding,
    ExtensionInvocationResult,
    ExtensionReviewApproval,
    PublicExtensionCapability,
    canonical_utc,
    digest_canonical_value,
    parse_canonical_utc,
    parse_deployment_binding,
    parse_invocation_result,
    parse_review_approval,
)
from interface.extension_generation import (
    authenticated_local_principal_digest,
    initiating_session_digest,
)
from interface.extension_artifact import artifact_owner_scope_digest
from interface.extension_spec import ExtensionSpec, parse_extension_spec
from interface.extension_release import (
    parse_canonical_utc as parse_release_utc,
    parse_extension_release_attestation,
)
from runtime.trusted_extension_invocation_runner import (
    TrustedExtensionInvocationRunner,
)


DEPLOYMENT_STATE_FILE = "phase6_extension_deployment_state.json"
DEPLOYMENT_STATE_SCHEMA_VERSION = (
    "veyra.phase6.extension_deployment_state.v1"
)
DEPLOYMENT_STATUS_SCHEMA_VERSION = (
    "veyra.phase6.extension_deployment_status.v1"
)
DEPLOYMENT_EVENT_SCHEMA_VERSION = (
    "veyra.phase6.extension_deployment_event.v1"
)

_DEPLOYMENT_ID = re.compile(r"^extdep_[0-9a-f]{24}$")
_INVOCATION_ID = re.compile(r"^extinv_[0-9a-f]{24}$")
_RELEASE_ID = re.compile(r"^extrel_[0-9a-f]{24}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,239}$")
_MODES = (
    "disabled",
    "record_only",
    "shadow",
    "read_only_canary",
    "scoped_canary",
    "promoted",
)
_NEXT_MODE = {
    "record_only": "shadow",
    "shadow": "read_only_canary",
    "read_only_canary": "scoped_canary",
    "scoped_canary": "promoted",
}
_PRIOR_SUCCESS = {
    "shadow": "record_only",
    "read_only_canary": "shadow",
    "scoped_canary": "read_only_canary",
    "promoted": "scoped_canary",
}
_FAILURE_BREAKER_THRESHOLD = 2


class StateStore(Protocol):
    def read_json(self, name: str) -> dict[str, Any]: ...

    def mutate_json(
        self,
        name: str,
        mutator: Callable[[dict[str, Any]], dict[str, Any] | None],
    ) -> dict[str, Any]: ...


class ReleaseSubjectResolver(Protocol):
    def deployment_subject(self, **kwargs: Any) -> dict[str, Any]: ...


class ReleaseSnapshotVerifier(Protocol):
    def verify(self, **kwargs: Any) -> None: ...


class ExtensionDeploymentError(RuntimeError):
    """Base deployment lifecycle error."""


class ExtensionDeploymentUnauthorizedError(ExtensionDeploymentError):
    pass


class ExtensionDeploymentNotFoundError(ExtensionDeploymentError):
    pass


class ExtensionDeploymentConflictError(ExtensionDeploymentError):
    pass


class ExtensionDeploymentUnavailableError(ExtensionDeploymentError):
    pass


class ExtensionDeploymentStorageError(ExtensionDeploymentError):
    pass


class DurableSignedReleaseSnapshotVerifier:
    """Verify one release from its source-free durable registry snapshot.

    The verifier reads only the signed-release JSON state and public trust
    root. It never calls ``deployment_subject``, resolves prerequisites, reads
    candidate source, contacts a backend, or mutates state.
    """

    def __init__(self, release_registry: Any, *, now: Callable[[], datetime]) -> None:
        self.state_store = getattr(release_registry, "state_store", None)
        self.trust_store = getattr(release_registry, "trust_store", None)
        self._now = now

    def verify(
        self,
        *,
        release_id: str,
        release_revision: int,
        attestation_digest: str,
        owner_scope_digest: str,
        principal_digest: str,
        session_digest: str,
        extension_id: str,
        extension_version: int,
        artifact_sha256: str,
        spec_digest: str,
    ) -> None:
        if self.state_store is None or self.trust_store is None:
            raise ExtensionDeploymentUnavailableError(
                "durable signed-release snapshot verifier is unavailable"
            )
        raw = self.state_store.read_json("phase6_extension_release_state.json")
        releases = raw.get("releases") if isinstance(raw, dict) else None
        record = releases.get(release_id) if isinstance(releases, dict) else None
        if not isinstance(record, dict):
            raise ExtensionDeploymentConflictError(
                "signed release snapshot is absent"
            )
        try:
            attestation = parse_extension_release_attestation(
                record.get("attestation")
            )
            self.trust_store.verify_attestation(attestation)
        except Exception as exc:
            raise ExtensionDeploymentConflictError(
                "signed release snapshot verification failed"
            ) from exc
        manifest = attestation.manifest
        now = self._now().astimezone(timezone.utc)
        if (
            record.get("stage") != "RELEASE_SIGNED"
            or record.get("revocation") is not None
            or record.get("revision") != release_revision
            or record.get("attestation_digest") != attestation_digest
            or attestation.attestation_digest() != attestation_digest
            or record.get("owner_scope_digest") != owner_scope_digest
            or record.get("principal_digest") != principal_digest
            or record.get("session_digest") != session_digest
            or manifest.release_id() != release_id
            or manifest.extension_id != extension_id
            or manifest.extension_version != extension_version
            or manifest.artifact_sha256 != artifact_sha256
            or manifest.spec_digest != spec_digest
            or parse_release_utc(manifest.expires_at) <= now
        ):
            raise ExtensionDeploymentConflictError(
                "signed release is revoked, expired, or rebound"
            )


class ExtensionDeploymentGate:
    """Durable, fail-closed signed extension deployment state machine.

    This gate never stores source bytes. Every mutating operation that depends
    on source calls ``ExtensionReleaseRegistry.deployment_subject`` and accepts
    no alternate handoff. GET methods inspect only the durable state snapshot.
    """

    def __init__(
        self,
        *,
        state_store: StateStore,
        release_registry: ReleaseSubjectResolver,
        invocation_runner: TrustedExtensionInvocationRunner,
        release_snapshot_verifier: ReleaseSnapshotVerifier | None = None,
        control_token: str | None = None,
        approver_token: str | None = None,
        lifecycle_mode: str = "record_only",
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if lifecycle_mode not in {"disabled", "record_only"}:
            raise ValueError("deployment gate starts only disabled or record_only")
        self.state_store = state_store
        self.release_registry = release_registry
        self.invocation_runner = invocation_runner
        self.release_snapshot_verifier = (
            release_snapshot_verifier
            or DurableSignedReleaseSnapshotVerifier(
                release_registry, now=now or (lambda: datetime.now(timezone.utc))
            )
        )
        self._configured_control_token = control_token
        self._configured_approver_token = approver_token
        if (
            isinstance(control_token, str)
            and control_token
            and isinstance(approver_token, str)
            and approver_token
            and secrets.compare_digest(control_token, approver_token)
        ):
            raise ValueError(
                "extension approver credential must differ from control token"
            )
        self.lifecycle_mode = lifecycle_mode
        self._now = now or (lambda: datetime.now(timezone.utc))

    # ------------------------------------------------------------------
    # Pure GET projections. No runner, release registry, model, agent, tool,
    # network, or state mutation is reached from this section.
    # ------------------------------------------------------------------
    def status(self, *, control_token: str) -> dict[str, Any]:
        self._authorize(control_token, require_enabled=False)
        state = self._read_state()
        deployments = state["deployments"]
        return {
            "schema_version": DEPLOYMENT_STATUS_SCHEMA_VERSION,
            "configured_mode": self.lifecycle_mode,
            "availability": (
                "disabled" if self.lifecycle_mode == "disabled" else "configured"
            ),
            "state_revision": state["revision"],
            "deployment_count": len(deployments),
            "active_pointer_count": len(state["active_pointers"]),
            "public_capability_count": len(state["public_registry"]),
            "backend_snapshot": state.get("backend_snapshot"),
            "backend_refresh_required": True,
            "get_is_pure_snapshot": True,
            "default_mode": "record_only",
            "automatic_promotion": False,
            "scoped_canary_review_identity": "required_from_review_queue",
            "promotion_review_identity": "required_from_review_queue",
            "review_identity_unavailable_effect": "transition_blocked",
            "distinct_approver_credential_configured": bool(
                self._approver_token_value()
            ),
            "execution_boundary": "trusted_isolated_runner_only",
            "authority": self._authority(),
        }

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
        owner, principal, session = self._scope(
            user_id, workspace_id, session_id, token
        )
        state = self._read_state()
        rows = [
            self._public_record(record)
            for record in state["deployments"].values()
            if self._record_owned(record, owner, principal, session)
        ]
        rows.sort(key=lambda row: (row["created_at"], row["deployment_id"]), reverse=True)
        return {
            "schema_version": "veyra.phase6.extension_deployment_list.v1",
            "items": rows[: self._limit(limit)],
            "state_revision": state["revision"],
            "authority": self._authority(),
        }

    def get(
        self,
        *,
        deployment_id: str,
        user_id: str,
        workspace_id: str,
        session_id: str,
        control_token: str,
    ) -> dict[str, Any]:
        token = self._authorize(control_token, require_enabled=False)
        owner, principal, session = self._scope(
            user_id, workspace_id, session_id, token
        )
        state = self._read_state()
        record = self._record(state, deployment_id)
        self._require_owner(record, owner, principal, session)
        return self._public_record(record)

    def integrity(
        self,
        *,
        deployment_id: str,
        user_id: str,
        workspace_id: str,
        session_id: str,
        control_token: str,
    ) -> dict[str, Any]:
        token = self._authorize(control_token, require_enabled=False)
        owner, principal, session = self._scope(
            user_id, workspace_id, session_id, token
        )
        state = self._read_state()
        record = self._record(state, deployment_id)
        self._require_owner(record, owner, principal, session)
        self._validate_record(record)
        return {
            "schema_version": "veyra.phase6.extension_deployment_integrity.v1",
            "deployment_id": record["deployment_id"],
            "revision": record["revision"],
            "status": "durable_deployment_integrity_passed",
            "release_active_verification": "not_refreshed_on_get",
            "runner_active_verification": "not_refreshed_on_get",
            "active_verification_required_before_mutation": True,
            "source_persisted": False,
            "state_mutated": False,
            "authority": self._authority(),
        }

    def public_registry(
        self,
        *,
        user_id: str,
        workspace_id: str,
        session_id: str,
        control_token: str,
    ) -> dict[str, Any]:
        token = self._authorize(control_token, require_enabled=False)
        owner, principal, session = self._scope(
            user_id, workspace_id, session_id, token
        )
        state = self._read_state()
        rows: list[dict[str, Any]] = []
        invalid_release_count = 0
        for item in state["public_registry"].values():
            if (
                item.get("owner_scope_digest") != owner
                or item.get("workspace_identity_digest")
                != self._workspace_digest(workspace_id)
                or item.get("initiating_session_digest") != session
            ):
                continue
            record = self._record(state, item["deployment_id"])
            self._require_owner(record, owner, principal, session)
            try:
                binding = parse_deployment_binding(record["binding"])
                spec = parse_extension_spec(record["signed_spec"])
                self.release_snapshot_verifier.verify(
                    release_id=record["release_id"],
                    release_revision=record["release_revision"],
                    attestation_digest=record["attestation_digest"],
                    owner_scope_digest=record["owner_scope_digest"],
                    principal_digest=record["principal_digest"],
                    session_digest=record["session_digest"],
                    extension_id=binding.extension_id,
                    extension_version=binding.extension_version,
                    artifact_sha256=binding.artifact_sha256,
                    spec_digest=binding.spec_digest,
                )
                self._validate_public_subject(item, record, spec)
            except Exception:
                invalid_release_count += 1
                continue
            rows.append(dict(item))
        rows.sort(key=lambda item: item["capability_id"])
        return {
            "schema_version": "veyra.phase6.public_extension_registry.v1",
            "state_revision": state["revision"],
            "items": rows,
            "invalid_or_revoked_count": invalid_release_count,
            "source_free": True,
            "execution_boundary": "trusted_isolated_runner_only",
            "authority": self._authority(),
        }

    # ------------------------------------------------------------------
    # Explicit POST control plane.
    # ------------------------------------------------------------------
    def refresh_backend(
        self,
        *,
        operation_id: str,
        expected_state_revision: int,
        control_token: str,
    ) -> dict[str, Any]:
        token = self._authorize(control_token, require_enabled=True)
        operation = self._operation(operation_id)
        request_digest = self._digest(
            {
                "kind": "refresh_backend",
                "operation_id": operation,
                "expected_state_revision": expected_state_revision,
                "principal_digest": authenticated_local_principal_digest(token),
            }
        )
        before = self._read_state()
        replay = self._replay_command(
            before, operation, request_digest, expected_kind="backend_snapshot"
        )
        if replay is not None:
            return {
                "schema_version": "veyra.phase6.extension_backend_refresh.v1",
                "state_revision": before["revision"],
                "backend_status": replay["status"],
                "authority": self._authority(),
            }
        status = self.invocation_runner.status()
        payload = status.model_dump(mode="json")
        operation_digest = request_digest

        def mutate(raw: dict[str, Any]) -> dict[str, Any]:
            state = self._normalize_state(raw)
            replay = self._operation_replay(state, operation, operation_digest)
            if replay is not None:
                return state
            self._require_state_revision(state, expected_state_revision)
            state["revision"] += 1
            state["backend_snapshot"] = {
                "status": payload,
                "observed_at": self._now_iso(),
                "state_revision": state["revision"],
            }
            self._remember_operation(
                state,
                operation,
                operation_digest,
                {
                    "kind": "backend_snapshot",
                    "status": payload,
                    "request_digest": request_digest,
                },
            )
            return state

        state = self.state_store.mutate_json(DEPLOYMENT_STATE_FILE, mutate)
        return {
            "schema_version": "veyra.phase6.extension_backend_refresh.v1",
            "state_revision": self._normalize_state(state)["revision"],
            "backend_status": payload,
            "authority": self._authority(),
        }

    def propose(
        self,
        *,
        operation_id: str,
        request_id: str,
        release_id: str,
        user_id: str,
        workspace_id: str,
        session_id: str,
        expected_state_revision: int,
        expected_release_revision: int,
        expected_attestation_digest: str,
        expires_at: str,
        control_token: str,
    ) -> dict[str, Any]:
        token = self._authorize(control_token, require_enabled=True)
        operation = self._operation(operation_id)
        selected_request = self._text(request_id, "request_id")
        selected_release = self._release_id(release_id)
        selected_attestation = self._digest_value(
            expected_attestation_digest, "expected_attestation_digest"
        )
        selected_expiry = self._future_expiry(expires_at)
        user = self._text(user_id, "user_id")
        workspace = self._text(workspace_id, "workspace_id")
        session_text = self._text(session_id, "session_id")
        owner, principal, session = self._scope(user, workspace, session_text, token)
        request_digest = self._digest(
            {
                "kind": "propose",
                "operation_id": operation,
                "request_id": selected_request,
                "release_id": selected_release,
                "user_id": user,
                "workspace_id": workspace,
                "session_id": session_text,
                "expected_state_revision": expected_state_revision,
                "expected_release_revision": expected_release_revision,
                "expected_attestation_digest": selected_attestation,
                "expires_at": selected_expiry,
            }
        )
        before = self._read_state()
        replay = self._replay_command(
            before, operation, request_digest, expected_kind="deployment"
        )
        if replay is not None:
            return self._public_record(
                self._record(before, replay["deployment_id"])
            )
        subject = self._release_subject(
            release_id=selected_release,
            user_id=user,
            workspace_id=workspace,
            session_id=session_text,
            expected_release_revision=self._revision(
                expected_release_revision, "expected_release_revision", minimum=1
            ),
            expected_attestation_digest=selected_attestation,
            control_token=token,
        )
        spec, source = self._validate_subject(
            subject=subject,
            owner=owner,
            principal=principal,
            session=session,
            release_id=selected_release,
            release_revision=expected_release_revision,
            attestation_digest=selected_attestation,
        )
        backend = self._ready_backend()
        deployment_id = "extdep_" + self._digest(
            {
                "release_id": selected_release,
                "owner_scope_digest": owner,
                "session_digest": session,
            }
        )[:24]
        provenance = self._request_provenance(
            operation_id=operation,
            request_id=selected_request,
            owner=owner,
            principal=principal,
            session=session,
            release_id=selected_release,
        )
        scope_digest = self._scope_digest(
            owner=owner, workspace_id=workspace, session=session, mode="record_only"
        )
        binding = self._deployment_binding(
            deployment_id=deployment_id,
            subject=subject,
            spec=spec,
            principal=principal,
            session=session,
            request_provenance_digest=provenance,
            mode="record_only",
            mode_epoch=0,
            scope_digest=scope_digest,
            max_invocations=0,
            expires_at=selected_expiry,
            backend=backend,
        )
        operation_digest = self._digest(
            {
                "kind": "propose",
                "operation_id": operation,
                "request_id": selected_request,
                "expected_state_revision": expected_state_revision,
                "binding_digest": binding.binding_digest(),
            }
        )

        def mutate(raw: dict[str, Any]) -> dict[str, Any]:
            state = self._normalize_state(raw)
            replay = self._operation_replay(state, operation, operation_digest)
            if replay is not None:
                return state
            self._require_state_revision(state, expected_state_revision)
            existing = state["deployments"].get(deployment_id)
            if existing is not None:
                raise ExtensionDeploymentConflictError(
                    "signed release already has a deployment proposal"
                )
            now = self._now_iso()
            record = {
                "deployment_id": deployment_id,
                "user_id": user,
                "workspace_id": workspace,
                "session_id": session_text,
                "owner_scope_digest": owner,
                "principal_digest": principal,
                "session_digest": session,
                "release_id": selected_release,
                "release_revision": expected_release_revision,
                "attestation_digest": selected_attestation,
                "revision": 1,
                "mode": "record_only",
                "mode_epoch": 0,
                "stage": "PROPOSED",
                "binding": binding.canonical_dict(),
                "binding_digest": binding.binding_digest(),
                "signed_spec": spec.canonical_dict(),
                "successful_modes": ["record_only"],
                "invocations_used": 0,
                "consecutive_failures": 0,
                "breaker_open": False,
                "breaker_reason": None,
                "previous_deployment_id": None,
                "review": None,
                "receipts": [
                    self._receipt(
                        kind="proposal",
                        mode="record_only",
                        status="passed",
                        digest=binding.binding_digest(),
                    )
                ],
                "created_at": now,
                "updated_at": now,
            }
            self._validate_record(record)
            state["deployments"][deployment_id] = record
            state["revision"] += 1
            self._remember_operation(
                state,
                operation,
                operation_digest,
                {
                    "kind": "deployment",
                    "deployment_id": deployment_id,
                    "request_digest": request_digest,
                },
            )
            return state

        state = self.state_store.mutate_json(DEPLOYMENT_STATE_FILE, mutate)
        record = self._record(self._normalize_state(state), deployment_id)
        return self._public_record(record)

    def request_transition_review(
        self,
        *,
        operation_id: str,
        request_id: str,
        deployment_id: str,
        target_mode: str,
        user_id: str,
        workspace_id: str,
        session_id: str,
        expected_state_revision: int,
        expected_deployment_revision: int,
        expected_mode_epoch: int,
        control_token: str,
    ) -> dict[str, Any]:
        """Create a source-free ReviewQueue request for a sensitive transition."""

        token = self._authorize(control_token, require_enabled=True)
        operation = self._operation(operation_id)
        request = self._text(request_id, "request_id")
        selected_id = self._deployment_id(deployment_id)
        if target_mode not in {"scoped_canary", "promoted"}:
            raise ValueError("identified review is only used for sensitive stages")
        user = self._text(user_id, "user_id")
        workspace = self._text(workspace_id, "workspace_id")
        session_text = self._text(session_id, "session_id")
        owner, principal, session = self._scope(user, workspace, session_text, token)
        state = self._read_state()
        self._require_state_revision(state, expected_state_revision)
        record = self._record(state, selected_id)
        self._require_owner(record, owner, principal, session)
        self._require_deployment_revision(
            record, expected_deployment_revision, expected_mode_epoch
        )
        if _NEXT_MODE.get(record["mode"]) != target_mode:
            raise ExtensionDeploymentConflictError(
                "review target is not the exact next deployment stage"
            )
        if _PRIOR_SUCCESS[target_mode] not in record["successful_modes"]:
            raise ExtensionDeploymentConflictError(
                "review target lacks the prior successful stage receipt"
            )
        subject = self._release_subject_for_record(record, token)
        self._validate_subject_for_record(subject, record)
        proposal = self._review_proposal(record=record, target_mode=target_mode)
        forbidden_identities = sorted(
            {
                subject["generator_identity_digest"],
                subject["verifier_identity_digest"],
                subject["signing_service_identity_digest"],
            }
        )
        request_digest = self._digest(
            {
                "schema_version": (
                    "veyra.phase6.extension_deployment_review_request.v1"
                ),
                "operation_id": operation,
                "request_id": request,
                "proposal": proposal,
                "owner_scope_digest": owner,
                "principal_digest": principal,
                "session_digest": session,
                "forbidden_approver_identity_digests": forbidden_identities,
            }
        )
        review_id = "rev_extdep_" + request_digest[:12]
        selected: dict[str, Any] | None = None

        def mutate(review_state: dict[str, Any]) -> None:
            nonlocal selected
            items = review_state.setdefault("items", [])
            if not isinstance(items, list):
                raise ExtensionDeploymentStorageError(
                    "review queue state is invalid"
                )
            for item in items:
                if not isinstance(item, dict):
                    continue
                if item.get("operation_id") == operation:
                    if item.get("request_digest") != request_digest:
                        raise ExtensionDeploymentConflictError(
                            "review operation id was reused"
                        )
                    selected = dict(item)
                    return
                if item.get("review_id") == review_id:
                    raise ExtensionDeploymentConflictError(
                        "review identity collision"
                    )
            row = {
                "review_id": review_id,
                "review_type": "extension_deployment_transition",
                "operation_id": operation,
                "request_digest": request_digest,
                "review_revision": 1,
                "status": "pending",
                "proposal": proposal,
                "proposal_digest": self._digest(proposal),
                "owner_scope_digest": owner,
                "principal_digest": principal,
                "session_digest": session,
                "forbidden_approver_identity_digests": forbidden_identities,
                "approval_credential_kind": (
                    "distinct_extension_approver_token"
                ),
                "approver_identity_digest": None,
                "approval_receipt_digest": None,
                "approval_reason_digest": None,
                "created_at": self._now_iso(),
                "decided_at": None,
                "approval_expires_at": None,
            }
            items.append(row)
            selected = dict(row)

        self.state_store.mutate_json("review_queue.json", mutate)
        if selected is None:
            raise ExtensionDeploymentStorageError(
                "review request was not durably admitted"
            )
        return self._public_review(selected)

    def approve_transition_review(
        self,
        *,
        review_id: str,
        expected_review_revision: int,
        reason_digest: str,
        approver_token: str,
    ) -> dict[str, Any]:
        """Approve with a credential independent of VEYRA_LOCAL_API_TOKEN.

        The raw credential is compared in memory and never written to state,
        logs, receipts, or the response. Only its domain-separated identity
        digest is persisted.
        """

        token = self._authorize_approver(approver_token)
        selected_review_id = self._text(review_id, "review_id")
        expected_revision = self._revision(
            expected_review_revision, "expected_review_revision", minimum=1
        )
        selected_reason = self._digest_value(reason_digest, "reason_digest")
        identity = self._approver_identity_digest(token)
        selected: dict[str, Any] | None = None

        def mutate(review_state: dict[str, Any]) -> None:
            nonlocal selected
            items = review_state.get("items")
            if not isinstance(items, list):
                raise ExtensionDeploymentStorageError(
                    "review queue state is invalid"
                )
            for item in items:
                if not isinstance(item, dict) or item.get("review_id") != selected_review_id:
                    continue
                if item.get("review_type") != "extension_deployment_transition":
                    raise ExtensionDeploymentConflictError(
                        "review is not an extension deployment transition"
                    )
                if item.get("review_revision") != expected_revision:
                    if (
                        item.get("status") == "approved"
                        and item.get("approver_identity_digest") == identity
                        and item.get("approval_reason_digest") == selected_reason
                    ):
                        selected = dict(item)
                        return
                    raise ExtensionDeploymentConflictError(
                        "review revision CAS failed"
                    )
                forbidden = item.get("forbidden_approver_identity_digests")
                proposal = item.get("proposal")
                if (
                    item.get("status") != "pending"
                    or not isinstance(forbidden, list)
                    or identity in forbidden
                    or not isinstance(proposal, dict)
                    or item.get("proposal_digest") != self._digest(proposal)
                    or item.get("approval_credential_kind")
                    != "distinct_extension_approver_token"
                ):
                    raise ExtensionDeploymentConflictError(
                        "review cannot be approved by this principal"
                    )
                decided_at = self._now_iso()
                expires_at = canonical_utc(
                    self._now_value().replace(microsecond=0)
                    + timedelta(minutes=30)
                )
                receipt_digest = self._review_receipt_digest(
                    review_id=selected_review_id,
                    decided_at=decided_at,
                    expires_at=expires_at,
                    approver_identity_digest=identity,
                    proposal=proposal,
                )
                item["status"] = "approved"
                item["review_revision"] = expected_revision + 1
                item["approver_identity_digest"] = identity
                item["approval_receipt_digest"] = receipt_digest
                item["approval_reason_digest"] = selected_reason
                item["decided_at"] = decided_at
                item["approval_expires_at"] = expires_at
                selected = dict(item)
                return
            raise ExtensionDeploymentNotFoundError(
                "deployment review was not found"
            )

        self.state_store.mutate_json("review_queue.json", mutate)
        if selected is None:
            raise ExtensionDeploymentStorageError(
                "review approval was not durably recorded"
            )
        return self._public_review(selected)

    def transition(
        self,
        *,
        operation_id: str,
        request_id: str,
        deployment_id: str,
        target_mode: str,
        user_id: str,
        workspace_id: str,
        session_id: str,
        expected_state_revision: int,
        expected_deployment_revision: int,
        expected_mode_epoch: int,
        max_invocations: int,
        expires_at: str,
        review_id: str | None = None,
        control_token: str,
    ) -> dict[str, Any]:
        token = self._authorize(control_token, require_enabled=True)
        operation = self._operation(operation_id)
        request = self._text(request_id, "request_id")
        selected_id = self._deployment_id(deployment_id)
        if target_mode not in _PRIOR_SUCCESS:
            raise ValueError("target mode is not a forward lifecycle stage")
        selected_budget = self._revision(
            max_invocations, "max_invocations", minimum=1
        )
        if selected_budget > MAX_EXTENSION_DEPLOYMENT_INVOCATIONS:
            raise ValueError("invocation budget exceeds policy")
        selected_expiry = self._future_expiry(expires_at)
        user = self._text(user_id, "user_id")
        workspace = self._text(workspace_id, "workspace_id")
        session_text = self._text(session_id, "session_id")
        owner, principal, session = self._scope(user, workspace, session_text, token)
        before = self._read_state()
        record = self._record(before, selected_id)
        self._require_owner(record, owner, principal, session)
        request_digest = self._digest(
            {
                "kind": "transition",
                "operation_id": operation,
                "request_id": request,
                "deployment_id": selected_id,
                "target_mode": target_mode,
                "user_id": user,
                "workspace_id": workspace,
                "session_id": session_text,
                "expected_state_revision": expected_state_revision,
                "expected_deployment_revision": expected_deployment_revision,
                "expected_mode_epoch": expected_mode_epoch,
                "max_invocations": selected_budget,
                "expires_at": selected_expiry,
                "review_id": review_id,
            }
        )
        replay = self._replay_command(
            before, operation, request_digest, expected_kind="deployment"
        )
        if replay is not None:
            return self._public_record(
                self._record(before, replay["deployment_id"])
            )
        self._require_deployment_revision(
            record, expected_deployment_revision, expected_mode_epoch
        )
        current_mode = record["mode"]
        if _NEXT_MODE.get(current_mode) != target_mode:
            raise ExtensionDeploymentConflictError(
                "deployment transition must follow the exact forward sequence"
            )
        if _PRIOR_SUCCESS[target_mode] not in record["successful_modes"]:
            raise ExtensionDeploymentConflictError(
                "prior lifecycle stage has no successful receipt"
            )
        if record["breaker_open"]:
            raise ExtensionDeploymentConflictError("deployment breaker is open")
        subject = self._release_subject_for_record(record, token)
        spec, source = self._validate_subject_for_record(subject, record)
        backend = self._ready_backend()
        next_epoch = record["mode_epoch"] + 1
        provenance = self._request_provenance(
            operation_id=operation,
            request_id=request,
            owner=owner,
            principal=principal,
            session=session,
            release_id=record["release_id"],
        )
        scope_digest = self._scope_digest(
            owner=owner,
            workspace_id=workspace,
            session=session,
            mode=target_mode,
        )
        binding = self._deployment_binding(
            deployment_id=selected_id,
            subject=subject,
            spec=spec,
            principal=principal,
            session=session,
            request_provenance_digest=provenance,
            mode=target_mode,
            mode_epoch=next_epoch,
            scope_digest=scope_digest,
            max_invocations=selected_budget,
            expires_at=selected_expiry,
            backend=backend,
        )
        review: ExtensionReviewApproval | None = None
        if target_mode in {"scoped_canary", "promoted"}:
            review = self._approved_review(
                record=record,
                subject=subject,
                target_mode=target_mode,
                review_id=review_id,
            )
        operation_digest = self._digest(
            {
                "kind": "transition",
                "operation_id": operation,
                "request_id": request,
                "expected_state_revision": expected_state_revision,
                "expected_deployment_revision": expected_deployment_revision,
                "expected_mode_epoch": expected_mode_epoch,
                "target_mode": target_mode,
                "binding_digest": binding.binding_digest(),
                "review": review.model_dump(mode="json") if review else None,
            }
        )

        def mutate(raw: dict[str, Any]) -> dict[str, Any]:
            state = self._normalize_state(raw)
            replay = self._operation_replay(state, operation, operation_digest)
            if replay is not None:
                return state
            self._require_state_revision(state, expected_state_revision)
            current = self._record(state, selected_id)
            self._require_owner(current, owner, principal, session)
            self._require_deployment_revision(
                current, expected_deployment_revision, expected_mode_epoch
            )
            if current["binding_digest"] != record["binding_digest"]:
                raise ExtensionDeploymentConflictError(
                    "deployment changed while prerequisites were verified"
                )
            current["revision"] += 1
            current["mode"] = target_mode
            current["mode_epoch"] = next_epoch
            current["stage"] = self._stage_for_mode(target_mode)
            current["binding"] = binding.canonical_dict()
            current["binding_digest"] = binding.binding_digest()
            current["invocations_used"] = 0
            current["consecutive_failures"] = 0
            current["review"] = review.model_dump(mode="json") if review else None
            current["updated_at"] = self._now_iso()
            current["receipts"] = self._append_receipt(
                current["receipts"],
                self._receipt(
                    kind="transition",
                    mode=target_mode,
                    status="passed",
                    digest=binding.binding_digest(),
                ),
            )
            if target_mode == "promoted":
                self._promote_atomic(state, current, spec)
            self._validate_record(current)
            state["revision"] += 1
            self._remember_operation(
                state,
                operation,
                operation_digest,
                {
                    "kind": "deployment",
                    "deployment_id": selected_id,
                    "request_digest": request_digest,
                },
            )
            return state

        state = self.state_store.mutate_json(DEPLOYMENT_STATE_FILE, mutate)
        return self._public_record(self._record(self._normalize_state(state), selected_id))

    def invoke(
        self,
        *,
        operation_id: str,
        request_id: str,
        deployment_id: str,
        user_id: str,
        workspace_id: str,
        session_id: str,
        expected_state_revision: int,
        expected_deployment_revision: int,
        expected_mode_epoch: int,
        input_payload: dict[str, Any],
        control_token: str,
    ) -> dict[str, Any]:
        token = self._authorize(control_token, require_enabled=True)
        operation = self._operation(operation_id)
        request = self._text(request_id, "request_id")
        selected_id = self._deployment_id(deployment_id)
        if not isinstance(input_payload, dict):
            raise ValueError("input_payload must be one JSON object")
        input_digest = digest_canonical_value(input_payload)
        user = self._text(user_id, "user_id")
        workspace = self._text(workspace_id, "workspace_id")
        session_text = self._text(session_id, "session_id")
        owner, principal, session = self._scope(user, workspace, session_text, token)
        before = self._read_state()
        record = self._record(before, selected_id)
        self._require_owner(record, owner, principal, session)
        request_digest = self._digest(
            {
                "kind": "invoke",
                "operation_id": operation,
                "request_id": request,
                "deployment_id": selected_id,
                "user_id": user,
                "workspace_id": workspace,
                "session_id": session_text,
                "expected_state_revision": expected_state_revision,
                "expected_deployment_revision": expected_deployment_revision,
                "expected_mode_epoch": expected_mode_epoch,
                "input_digest": input_digest,
            }
        )
        replay = self._replay_command(
            before, operation, request_digest, expected_kind="invocation"
        )
        if replay is not None:
            payload = self._operation_result(
                before,
                {"result": replay},
            )
            if payload is None:
                raise ExtensionDeploymentConflictError(
                    "started or indeterminate invocation cannot be retried"
                )
            return payload
        self._require_deployment_revision(
            record, expected_deployment_revision, expected_mode_epoch
        )
        if record["mode"] not in {
            "shadow", "read_only_canary", "scoped_canary", "promoted"
        }:
            raise ExtensionDeploymentConflictError(
                "deployment mode does not permit isolated invocation"
            )
        if record["breaker_open"]:
            raise ExtensionDeploymentConflictError("deployment breaker is open")
        binding_now = parse_deployment_binding(record["binding"])
        if record["invocations_used"] >= binding_now.max_invocations:
            raise ExtensionDeploymentConflictError("deployment invocation budget is exhausted")
        if parse_canonical_utc(binding_now.expires_at) <= self._now_value():
            raise ExtensionDeploymentConflictError("deployment has expired")
        if record["mode"] == "promoted":
            pointer_key = self._pointer_key(
                owner,
                workspace,
                session,
                binding_now.extension_id,
            )
            pointer = before["active_pointers"].get(pointer_key)
            if not isinstance(pointer, dict) or pointer.get("deployment_id") != selected_id:
                raise ExtensionDeploymentConflictError(
                    "promoted deployment is not the active atomic pointer"
                )
        subject = self._release_subject_for_record(record, token)
        spec, source = self._validate_subject_for_record(subject, record)
        self._validate_input(spec, input_payload)
        backend = self._ready_backend()
        if not self._backend_matches_deployment(binding_now, backend):
            raise ExtensionDeploymentConflictError(
                "deployment runner TCB changed; explicit transition is required"
            )
        invocation_id = "extinv_" + self._digest(
            {
                "deployment_id": selected_id,
                "mode_epoch": record["mode_epoch"],
                "operation_id": operation,
                "request_id": request,
                "input_digest": input_digest,
                "principal": principal,
                "session": session,
            }
        )[:24]
        provenance = self._request_provenance(
            operation_id=operation,
            request_id=request,
            owner=owner,
            principal=principal,
            session=session,
            release_id=record["release_id"],
        )
        invocation_binding = ExtensionInvocationBinding(
            schema_version=EXTENSION_INVOCATION_BINDING_SCHEMA_VERSION,
            invocation_id=invocation_id,
            deployment_id=selected_id,
            deployment_revision=record["revision"],
            deployment_binding_digest=record["binding_digest"],
            release_id=record["release_id"],
            release_revision=record["release_revision"],
            attestation_digest=record["attestation_digest"],
            owner_scope_digest=owner,
            authenticated_principal_digest=principal,
            initiating_session_digest=session,
            request_provenance_digest=provenance,
            extension_id=spec.extension_id,
            extension_version=spec.version,
            artifact_sha256=subject["artifact_sha256"],
            spec_digest=spec.digest(),
            input_digest=input_digest,
            mode=record["mode"],
            mode_epoch=record["mode_epoch"],
            scope_digest=binding_now.scope_digest,
            runner_engine_identity_digest=backend.engine_identity_digest,
            runner_image_id=backend.image_id,
            isolation_conformance_digest=backend.isolation_conformance_digest,
            invocation_conformance_digest=backend.invocation_conformance_digest,
            invocation_harness_revision=EXTENSION_INVOCATION_HARNESS_REVISION,
            invocation_harness_digest=backend.harness_digest,
            deployment_policy_revision=EXTENSION_DEPLOYMENT_POLICY_REVISION,
            deployment_policy_digest=EXTENSION_DEPLOYMENT_POLICY_DIGEST,
        )
        operation_digest = self._digest(
            {
                "kind": "invoke",
                "operation_id": operation,
                "request_id": request,
                "expected_state_revision": expected_state_revision,
                "expected_deployment_revision": expected_deployment_revision,
                "expected_mode_epoch": expected_mode_epoch,
                "binding_digest": invocation_binding.binding_digest(),
            }
        )
        replay = self._claim_invocation(
            operation=operation,
            operation_digest=operation_digest,
            request_digest=request_digest,
            expected_state_revision=expected_state_revision,
            record=record,
            owner=owner,
            principal=principal,
            session=session,
            binding=invocation_binding,
        )
        if replay is not None:
            return replay

        rollback = self._validated_rollback_candidate(
            before=before,
            current=record,
            control_token=token,
        )
        try:
            result = self.invocation_runner.run(
                source_bytes=source,
                spec=spec,
                input_payload=input_payload,
                binding=invocation_binding,
            )
        except BaseException:
            result = ExtensionInvocationResult(
                schema_version=EXTENSION_INVOCATION_RESULT_SCHEMA_VERSION,
                binding=invocation_binding,
                binding_digest=invocation_binding.binding_digest(),
                invocation_status="indeterminate",
                output_payload=None,
                output_digest=None,
                output_discarded=False,
                issue_code="runner_unknown_failure",
                completed_at=self._now_iso(),
                authority=ExtensionDeploymentAuthority(),
            )
        return self._complete_invocation(
            operation=operation,
            operation_digest=operation_digest,
            record=record,
            binding=invocation_binding,
            result=result,
            rollback=rollback,
        )

    def disable(
        self,
        *,
        operation_id: str,
        deployment_id: str,
        user_id: str,
        workspace_id: str,
        session_id: str,
        expected_state_revision: int,
        expected_deployment_revision: int,
        expected_mode_epoch: int,
        reason_digest: str,
        control_token: str,
    ) -> dict[str, Any]:
        token = self._authorize(control_token, require_enabled=False)
        operation = self._operation(operation_id)
        selected_id = self._deployment_id(deployment_id)
        reason = self._digest_value(reason_digest, "reason_digest")
        owner, principal, session = self._scope(
            user_id, workspace_id, session_id, token
        )
        operation_digest = self._digest(
            {
                "kind": "disable",
                "operation_id": operation,
                "deployment_id": selected_id,
                "expected_state_revision": expected_state_revision,
                "expected_deployment_revision": expected_deployment_revision,
                "expected_mode_epoch": expected_mode_epoch,
                "reason_digest": reason,
            }
        )

        def mutate(raw: dict[str, Any]) -> dict[str, Any]:
            state = self._normalize_state(raw)
            if self._operation_replay(state, operation, operation_digest) is not None:
                return state
            self._require_state_revision(state, expected_state_revision)
            record = self._record(state, selected_id)
            self._require_owner(record, owner, principal, session)
            self._require_deployment_revision(
                record, expected_deployment_revision, expected_mode_epoch
            )
            self._disable_atomic(state, record, reason)
            state["revision"] += 1
            self._remember_operation(
                state,
                operation,
                operation_digest,
                {"kind": "deployment", "deployment_id": selected_id},
            )
            return state

        state = self.state_store.mutate_json(DEPLOYMENT_STATE_FILE, mutate)
        return self._public_record(self._record(self._normalize_state(state), selected_id))

    def rollback(
        self,
        *,
        operation_id: str,
        deployment_id: str,
        user_id: str,
        workspace_id: str,
        session_id: str,
        expected_state_revision: int,
        expected_deployment_revision: int,
        expected_mode_epoch: int,
        reason_digest: str,
        control_token: str,
    ) -> dict[str, Any]:
        token = self._authorize(control_token, require_enabled=False)
        before = self._read_state()
        record = self._record(before, deployment_id)
        owner, principal, session = self._scope(
            user_id, workspace_id, session_id, token
        )
        self._require_owner(record, owner, principal, session)
        self._require_deployment_revision(
            record, expected_deployment_revision, expected_mode_epoch
        )
        rollback = self._validated_rollback_candidate(
            before=before, current=record, control_token=token
        )
        operation = self._operation(operation_id)
        reason = self._digest_value(reason_digest, "reason_digest")
        operation_digest = self._digest(
            {
                "kind": "rollback",
                "operation_id": operation,
                "deployment_id": record["deployment_id"],
                "expected_state_revision": expected_state_revision,
                "expected_deployment_revision": expected_deployment_revision,
                "expected_mode_epoch": expected_mode_epoch,
                "reason_digest": reason,
                "rollback_target": (
                    rollback["deployment_id"] if rollback is not None else None
                ),
            }
        )

        def mutate(raw: dict[str, Any]) -> dict[str, Any]:
            state = self._normalize_state(raw)
            if self._operation_replay(state, operation, operation_digest) is not None:
                return state
            self._require_state_revision(state, expected_state_revision)
            current = self._record(state, record["deployment_id"])
            self._require_deployment_revision(
                current, expected_deployment_revision, expected_mode_epoch
            )
            self._rollback_or_disable_atomic(state, current, rollback, reason)
            state["revision"] += 1
            self._remember_operation(
                state,
                operation,
                operation_digest,
                {"kind": "deployment", "deployment_id": current["deployment_id"]},
            )
            return state

        state = self.state_store.mutate_json(DEPLOYMENT_STATE_FILE, mutate)
        normalized = self._normalize_state(state)
        return {
            "schema_version": "veyra.phase6.extension_rollback_result.v1",
            "deployment": self._public_record(
                self._record(normalized, record["deployment_id"])
            ),
            "active_pointer": self._active_pointer_for_record(normalized, record),
            "authority": self._authority(),
        }

    def reconcile_release(
        self,
        *,
        operation_id: str,
        deployment_id: str,
        user_id: str,
        workspace_id: str,
        session_id: str,
        expected_state_revision: int,
        expected_deployment_revision: int,
        expected_mode_epoch: int,
        reason_digest: str,
        control_token: str,
    ) -> dict[str, Any]:
        """Explicitly reconcile revocation/expiry/unavailability and rollback."""

        token = self._authorize(control_token, require_enabled=False)
        before = self._read_state()
        record = self._record(before, deployment_id)
        owner, principal, session = self._scope(
            user_id, workspace_id, session_id, token
        )
        self._require_owner(record, owner, principal, session)
        self._require_deployment_revision(
            record, expected_deployment_revision, expected_mode_epoch
        )
        valid = True
        try:
            subject = self._release_subject_for_record(record, token)
            self._validate_subject_for_record(subject, record)
        except Exception:
            valid = False
        if valid:
            return {
                "schema_version": "veyra.phase6.extension_release_reconciliation.v1",
                "status": "release_still_valid",
                "deployment": self._public_record(record),
                "state_mutated": False,
                "authority": self._authority(),
            }
        return self.rollback(
            operation_id=operation_id,
            deployment_id=deployment_id,
            user_id=user_id,
            workspace_id=workspace_id,
            session_id=session_id,
            expected_state_revision=expected_state_revision,
            expected_deployment_revision=expected_deployment_revision,
            expected_mode_epoch=expected_mode_epoch,
            reason_digest=reason_digest,
            control_token=control_token,
        )

    # ------------------------------------------------------------------
    # Durable helpers.
    # ------------------------------------------------------------------
    def _claim_invocation(
        self,
        *,
        operation: str,
        operation_digest: str,
        request_digest: str,
        expected_state_revision: int,
        record: dict[str, Any],
        owner: str,
        principal: str,
        session: str,
        binding: ExtensionInvocationBinding,
    ) -> dict[str, Any] | None:
        replay_payload: dict[str, Any] | None = None

        def mutate(raw: dict[str, Any]) -> dict[str, Any]:
            nonlocal replay_payload
            state = self._normalize_state(raw)
            replay = self._operation_replay(state, operation, operation_digest)
            if replay is not None:
                replay_payload = self._operation_result(state, replay)
                return state
            self._require_state_revision(state, expected_state_revision)
            current = self._record(state, record["deployment_id"])
            self._require_owner(current, owner, principal, session)
            self._require_deployment_revision(
                current, binding.deployment_revision, binding.mode_epoch
            )
            if current["binding_digest"] != binding.deployment_binding_digest:
                raise ExtensionDeploymentConflictError(
                    "deployment changed before invocation claim"
                )
            invocation_id = binding.invocation_id
            if invocation_id in state["invocations"]:
                raise ExtensionDeploymentConflictError(
                    "invocation identity already exists"
                )
            state["invocations"][invocation_id] = {
                "invocation_id": invocation_id,
                "deployment_id": current["deployment_id"],
                "owner_scope_digest": owner,
                "principal_digest": principal,
                "session_digest": session,
                "stage": "STARTED",
                "binding": binding.canonical_dict(),
                "binding_digest": binding.binding_digest(),
                "result_receipt": None,
                "result_digest": None,
                "started_at": self._now_iso(),
                "completed_at": None,
            }
            current["invocations_used"] += 1
            current["revision"] += 1
            current["updated_at"] = self._now_iso()
            state["revision"] += 1
            self._remember_operation(
                state,
                operation,
                operation_digest,
                {
                    "kind": "invocation",
                    "invocation_id": invocation_id,
                    "request_digest": request_digest,
                },
            )
            return state

        self.state_store.mutate_json(DEPLOYMENT_STATE_FILE, mutate)
        return replay_payload

    def _complete_invocation(
        self,
        *,
        operation: str,
        operation_digest: str,
        record: dict[str, Any],
        binding: ExtensionInvocationBinding,
        result: ExtensionInvocationResult,
        rollback: dict[str, Any] | None,
    ) -> dict[str, Any]:
        selected = parse_invocation_result(result)
        if selected.binding_digest != binding.binding_digest():
            raise ExtensionDeploymentStorageError(
                "invocation runner returned a rebound result"
            )

        effective = selected

        def mutate(raw: dict[str, Any]) -> dict[str, Any]:
            nonlocal effective
            state = self._normalize_state(raw)
            invocation = state["invocations"].get(binding.invocation_id)
            if not isinstance(invocation, dict) or invocation.get("stage") != "STARTED":
                raise ExtensionDeploymentConflictError(
                    "invocation is not a durable started claim"
                )
            if (
                invocation.get("binding_digest") != binding.binding_digest()
                or state["operations"].get(operation, {}).get("digest")
                != operation_digest
            ):
                raise ExtensionDeploymentConflictError(
                    "invocation durable binding changed"
                )
            current = self._record(state, record["deployment_id"])
            still_claimed = bool(
                current["revision"] == binding.deployment_revision + 1
                and current["mode"] == binding.mode
                and current["mode_epoch"] == binding.mode_epoch
                and current["binding_digest"]
                == binding.deployment_binding_digest
                and not current["breaker_open"]
                and (
                    binding.mode != "promoted"
                    or self._record_is_active(state, current)
                )
            )
            if not still_claimed:
                effective = ExtensionInvocationResult(
                    schema_version=EXTENSION_INVOCATION_RESULT_SCHEMA_VERSION,
                    binding=binding,
                    binding_digest=binding.binding_digest(),
                    invocation_status="indeterminate",
                    output_payload=None,
                    output_digest=None,
                    output_discarded=False,
                    issue_code="deployment_changed_during_invocation",
                    completed_at=self._now_iso(),
                    authority=ExtensionDeploymentAuthority(),
                )
            invocation["stage"] = {
                "passed": "PASSED",
                "discarded": "PASSED_OUTPUT_DISCARDED",
                "failed": "FAILED",
                "indeterminate": "INDETERMINATE",
            }[effective.invocation_status]
            invocation["result_receipt"] = self._invocation_result_receipt(
                effective
            )
            invocation["result_digest"] = effective.result_digest()
            invocation["completed_at"] = effective.completed_at
            if not still_claimed:
                state["revision"] += 1
                return state
            current["updated_at"] = self._now_iso()
            current["revision"] += 1
            receipt_status = (
                "passed"
                if effective.invocation_status in {"passed", "discarded"}
                else effective.invocation_status
            )
            current["receipts"] = self._append_receipt(
                current["receipts"],
                self._receipt(
                    kind="invocation",
                    mode=current["mode"],
                    status=receipt_status,
                    digest=effective.result_digest(),
                ),
            )
            if effective.invocation_status in {"passed", "discarded"}:
                current["consecutive_failures"] = 0
                if current["mode"] not in current["successful_modes"]:
                    current["successful_modes"].append(current["mode"])
            else:
                current["consecutive_failures"] += 1
                if (
                    effective.invocation_status == "indeterminate"
                    or current["consecutive_failures"] >= _FAILURE_BREAKER_THRESHOLD
                ):
                    self._rollback_or_disable_atomic(
                        state,
                        current,
                        rollback,
                        self._digest(
                            {
                                "kind": "automatic_breaker",
                                "invocation_id": binding.invocation_id,
                                "result_digest": effective.result_digest(),
                            }
                        ),
                    )
            state["revision"] += 1
            return state

        state = self.state_store.mutate_json(DEPLOYMENT_STATE_FILE, mutate)
        normalized = self._normalize_state(state)
        return {
            "schema_version": "veyra.phase6.extension_invocation_response.v1",
            "result": effective.canonical_dict(),
            "result_digest": effective.result_digest(),
            "deployment": self._public_record(
                self._record(normalized, record["deployment_id"])
            ),
            "state_revision": normalized["revision"],
            "authority": self._authority(),
        }

    @staticmethod
    def _invocation_result_receipt(
        result: ExtensionInvocationResult,
    ) -> dict[str, Any]:
        return {
            "schema_version": "veyra.phase6.extension_invocation_receipt.v1",
            "binding_digest": result.binding_digest,
            "invocation_status": result.invocation_status,
            "output_digest": result.output_digest,
            "output_discarded": result.output_discarded,
            "issue_code": result.issue_code,
            "completed_at": result.completed_at,
            "result_digest": result.result_digest(),
            "output_payload_persisted": False,
        }

    def _validated_rollback_candidate(
        self,
        *,
        before: dict[str, Any],
        current: dict[str, Any],
        control_token: str,
    ) -> dict[str, Any] | None:
        previous_id = current.get("previous_deployment_id")
        if not isinstance(previous_id, str):
            return None
        candidate = before["deployments"].get(previous_id)
        if not isinstance(candidate, dict) or candidate.get("mode") != "promoted":
            return None
        try:
            subject = self._release_subject_for_record(candidate, control_token)
            self._validate_subject_for_record(subject, candidate)
            binding = parse_deployment_binding(candidate["binding"])
            if parse_canonical_utc(binding.expires_at) <= self._now_value():
                return None
        except Exception:
            return None
        return {
            "deployment_id": candidate["deployment_id"],
            "binding_digest": candidate["binding_digest"],
            "public_capability": self._public_capability_from_record(candidate),
        }

    def _rollback_or_disable_atomic(
        self,
        state: dict[str, Any],
        current: dict[str, Any],
        rollback: dict[str, Any] | None,
        reason_digest: str,
    ) -> None:
        current["breaker_open"] = True
        current["breaker_reason"] = reason_digest
        current["mode"] = "disabled"
        current["mode_epoch"] += 1
        current["stage"] = "DISABLED_BY_BREAKER"
        current["revision"] += 1
        current["updated_at"] = self._now_iso()
        binding = parse_deployment_binding(current["binding"])
        pointer_key = self._pointer_key(
            current["owner_scope_digest"],
            current["workspace_id"],
            current["session_digest"],
            binding.extension_id,
        )
        pointer = state["active_pointers"].get(pointer_key)
        if (
            not isinstance(pointer, dict)
            or pointer.get("deployment_id") != current["deployment_id"]
        ):
            return
        capability_key = self._capability_key(
            current["owner_scope_digest"],
            current["workspace_id"],
            current["session_digest"],
            binding.extension_id,
        )
        if rollback is not None:
            target = state["deployments"].get(rollback["deployment_id"])
            if (
                isinstance(target, dict)
                and target.get("binding_digest") == rollback["binding_digest"]
                and target.get("mode") == "promoted"
            ):
                pointer["revision"] += 1
                pointer["deployment_id"] = target["deployment_id"]
                pointer["release_id"] = target["release_id"]
                pointer["attestation_digest"] = target["attestation_digest"]
                pointer["previous_deployment_id"] = None
                pointer["updated_at"] = self._now_iso()
                public = dict(rollback["public_capability"])
                public["active_pointer_revision"] = pointer["revision"]
                state["public_registry"][capability_key] = public
                return
        state["active_pointers"].pop(pointer_key, None)
        state["public_registry"].pop(capability_key, None)

    def _disable_atomic(
        self,
        state: dict[str, Any],
        record: dict[str, Any],
        reason_digest: str,
    ) -> None:
        record["breaker_open"] = True
        record["breaker_reason"] = reason_digest
        record["mode"] = "disabled"
        record["mode_epoch"] += 1
        record["stage"] = "DISABLED_EXPLICITLY"
        record["revision"] += 1
        record["updated_at"] = self._now_iso()
        binding = parse_deployment_binding(record["binding"])
        pointer_key = self._pointer_key(
            record["owner_scope_digest"],
            record["workspace_id"],
            record["session_digest"],
            binding.extension_id,
        )
        pointer = state["active_pointers"].get(pointer_key)
        if isinstance(pointer, dict) and pointer.get("deployment_id") == record["deployment_id"]:
            state["active_pointers"].pop(pointer_key, None)
            state["public_registry"].pop(
                self._capability_key(
                    record["owner_scope_digest"],
                    record["workspace_id"],
                    record["session_digest"],
                    binding.extension_id,
                ),
                None,
            )

    def _promote_atomic(
        self,
        state: dict[str, Any],
        record: dict[str, Any],
        spec: ExtensionSpec,
    ) -> None:
        pointer_key = self._pointer_key(
            record["owner_scope_digest"],
            record["workspace_id"],
            record["session_digest"],
            spec.extension_id,
        )
        previous = state["active_pointers"].get(pointer_key)
        pointer_revision = (
            int(previous.get("revision") or 0) + 1
            if isinstance(previous, dict)
            else 1
        )
        previous_id = (
            previous.get("deployment_id")
            if isinstance(previous, dict)
            else None
        )
        record["previous_deployment_id"] = previous_id
        state["active_pointers"][pointer_key] = {
            "pointer_key": pointer_key,
            "revision": pointer_revision,
            "deployment_id": record["deployment_id"],
            "release_id": record["release_id"],
            "attestation_digest": record["attestation_digest"],
            "previous_deployment_id": previous_id,
            "updated_at": self._now_iso(),
        }
        public = self._public_capability(record, spec, pointer_revision)
        public_payload = public.model_dump(mode="json")
        record["public_capability_snapshot"] = dict(public_payload)
        state["public_registry"][
            self._capability_key(
                record["owner_scope_digest"],
                record["workspace_id"],
                record["session_digest"],
                spec.extension_id,
            )
        ] = public_payload

    # ------------------------------------------------------------------
    # External prerequisite validation.
    # ------------------------------------------------------------------
    def _release_subject(self, **kwargs: Any) -> dict[str, Any]:
        resolver = getattr(self.release_registry, "deployment_subject", None)
        if not callable(resolver):
            raise ExtensionDeploymentUnavailableError(
                "signed release deployment subject resolver is unavailable"
            )
        subject = resolver(**kwargs)
        if not isinstance(subject, dict):
            raise ExtensionDeploymentStorageError(
                "signed release deployment subject is invalid"
            )
        return subject

    def _release_subject_for_record(
        self, record: dict[str, Any], control_token: str
    ) -> dict[str, Any]:
        return self._release_subject(
            release_id=record["release_id"],
            user_id=record["user_id"],
            workspace_id=record["workspace_id"],
            session_id=record["session_id"],
            expected_release_revision=record["release_revision"],
            expected_attestation_digest=record["attestation_digest"],
            control_token=control_token,
        )

    def _validate_subject(
        self,
        *,
        subject: dict[str, Any],
        owner: str,
        principal: str,
        session: str,
        release_id: str,
        release_revision: int,
        attestation_digest: str,
    ) -> tuple[ExtensionSpec, bytes]:
        required = {
            "schema_version",
            "release_id",
            "release_revision",
            "owner_scope_digest",
            "principal_digest",
            "session_digest",
            "manifest_digest",
            "attestation_digest",
            "signature_algorithm",
            "signing_key_id",
            "signing_service_identity_digest",
            "generator_identity_digest",
            "verifier_identity_digest",
            "validation_build_identity_digest",
            "generation_report_digest",
            "validation_report_digest",
            "artifact_id",
            "artifact_revision",
            "artifact_sha256",
            "spec_digest",
            "source_bytes",
            "source_check_spec",
            "expires_at",
            "authority",
        }
        if not required.issubset(subject):
            raise ExtensionDeploymentStorageError(
                "signed release deployment subject is incomplete"
            )
        source = subject.get("source_bytes")
        raw_spec = subject.get("source_check_spec")
        authority = subject.get("authority")
        if (
            subject.get("schema_version")
            != "veyra.phase6.extension_release_deployment_subject.v1"
            or subject.get("release_id") != release_id
            or subject.get("release_revision") != release_revision
            or subject.get("owner_scope_digest") != owner
            or subject.get("principal_digest") != principal
            or subject.get("session_digest") != session
            or subject.get("attestation_digest") != attestation_digest
            or subject.get("signature_algorithm") != "ed25519"
            or not isinstance(source, bytes)
            or not isinstance(raw_spec, dict)
            or not isinstance(authority, dict)
            or any(authority.values())
        ):
            raise ExtensionDeploymentStorageError(
                "signed release deployment subject binding is invalid"
            )
        spec = parse_extension_spec(raw_spec)
        if (
            spec.extension_kind != "pure_function"
            or spec.risk_floor != "R0"
            or spec.side_effects
            or spec.dependencies
            or spec.digest() != subject.get("spec_digest")
            or hashlib.sha256(source).hexdigest() != subject.get("artifact_sha256")
            or parse_canonical_utc(subject["expires_at"]) <= self._now_value()
        ):
            raise ExtensionDeploymentConflictError(
                "signed release is expired or not the exact pure-function subject"
            )
        return spec, source

    def _validate_subject_for_record(
        self, subject: dict[str, Any], record: dict[str, Any]
    ) -> tuple[ExtensionSpec, bytes]:
        return self._validate_subject(
            subject=subject,
            owner=record["owner_scope_digest"],
            principal=record["principal_digest"],
            session=record["session_digest"],
            release_id=record["release_id"],
            release_revision=record["release_revision"],
            attestation_digest=record["attestation_digest"],
        )

    def _ready_backend(self) -> ExtensionInvocationBackendStatus:
        status = self.invocation_runner.status()
        if (
            status.availability != "available"
            or not status.conformance_certified
            or status.engine_identity_digest is None
            or status.image_id is None
            or status.isolation_conformance_digest is None
            or status.invocation_conformance_digest is None
            or any(status.authority.model_dump(mode="python").values())
        ):
            raise ExtensionDeploymentUnavailableError(
                "certified trusted invocation runner is unavailable"
            )
        return status

    @staticmethod
    def _backend_matches_deployment(
        binding: ExtensionDeploymentBinding,
        backend: ExtensionInvocationBackendStatus,
    ) -> bool:
        return bool(
            binding.runner_engine_identity_digest == backend.engine_identity_digest
            and binding.runner_image_id == backend.image_id
            and binding.isolation_conformance_digest
            == backend.isolation_conformance_digest
            and binding.invocation_conformance_digest
            == backend.invocation_conformance_digest
            and binding.invocation_harness_digest == backend.harness_digest
            and binding.deployment_policy_revision
            == backend.deployment_policy_revision
            and binding.deployment_policy_digest == backend.deployment_policy_digest
        )

    def _approved_review(
        self,
        *,
        record: dict[str, Any],
        subject: dict[str, Any],
        target_mode: str,
        review_id: str | None,
    ) -> ExtensionReviewApproval:
        if not isinstance(review_id, str) or not review_id.strip():
            raise ExtensionDeploymentUnavailableError(
                "review_identity_unavailable"
            )
        selected_review_id = self._text(review_id, "review_id")
        proposal = self._review_proposal(
            record=record, target_mode=target_mode
        )
        review_state = self.state_store.read_json("review_queue.json")
        items = review_state.get("items")
        if not isinstance(items, list):
            raise ExtensionDeploymentUnavailableError(
                "review identity is unavailable"
            )
        selected: dict[str, Any] | None = None
        for item in items:
            if isinstance(item, dict) and item.get("review_id") == selected_review_id:
                selected = item
                break
        if selected is None:
            raise ExtensionDeploymentConflictError(
                "exact deployment review was not found"
            )
        approver = selected.get("approver_identity_digest")
        receipt_digest = selected.get("approval_receipt_digest")
        decided_at = selected.get("decided_at")
        expires_at = selected.get("approval_expires_at")
        if (
            selected.get("status") != "approved"
            or selected.get("review_type")
            != "extension_deployment_transition"
            or selected.get("review_revision") != 2
            or selected.get("owner_scope_digest")
            != record["owner_scope_digest"]
            or selected.get("principal_digest") != record["principal_digest"]
            or selected.get("session_digest") != record["session_digest"]
            or selected.get("approval_credential_kind")
            != "distinct_extension_approver_token"
            or selected.get("proposal") != proposal
            or selected.get("proposal_digest") != self._digest(proposal)
            or sorted(
                selected.get("forbidden_approver_identity_digests") or []
            )
            != sorted(
                {
                    subject["generator_identity_digest"],
                    subject["verifier_identity_digest"],
                    subject["signing_service_identity_digest"],
                }
            )
            or not isinstance(approver, str)
            or not _DIGEST.fullmatch(approver)
            or not isinstance(receipt_digest, str)
            or not _DIGEST.fullmatch(receipt_digest)
            or not isinstance(decided_at, str)
            or not isinstance(expires_at, str)
            or receipt_digest
            != self._review_receipt_digest(
                review_id=selected_review_id,
                decided_at=decided_at,
                expires_at=expires_at,
                approver_identity_digest=approver,
                proposal=proposal,
            )
        ):
            raise ExtensionDeploymentUnavailableError(
                "review_identity_unavailable"
            )
        approval = ExtensionReviewApproval(
            schema_version="veyra.phase6.extension_review_approval.v1",
            review_id=selected_review_id,
            status="approved",
            receipt_origin="veyra_review_queue_approved_identity",
            receipt_digest=receipt_digest,
            deployment_id=record["deployment_id"],
            deployment_revision=record["revision"],
            release_id=record["release_id"],
            attestation_digest=record["attestation_digest"],
            current_mode=record["mode"],
            target_mode=target_mode,
            mode_epoch=record["mode_epoch"],
            approver_identity_digest=approver,
            approved_at=decided_at,
            expires_at=expires_at,
        )
        if (
            approval.deployment_id != record["deployment_id"]
            or approval.deployment_revision != record["revision"]
            or approval.release_id != record["release_id"]
            or approval.attestation_digest != record["attestation_digest"]
            or approval.current_mode != record["mode"]
            or approval.target_mode != target_mode
            or approval.mode_epoch != record["mode_epoch"]
            or parse_canonical_utc(approval.approved_at) > self._now_value()
            or parse_canonical_utc(approval.expires_at) <= self._now_value()
            or approval.approver_identity_digest
            in {
                subject["generator_identity_digest"],
                subject["verifier_identity_digest"],
                subject["signing_service_identity_digest"],
            }
        ):
            raise ExtensionDeploymentConflictError(
                "deployment review is not an exact separate approval"
            )
        return approval

    @staticmethod
    def _review_proposal(
        *, record: dict[str, Any], target_mode: str
    ) -> dict[str, Any]:
        return {
            "schema_version": (
                "veyra.phase6.extension_deployment_review_binding.v1"
            ),
            "deployment_id": record["deployment_id"],
            "deployment_revision": record["revision"],
            "release_id": record["release_id"],
            "attestation_digest": record["attestation_digest"],
            "current_mode": record["mode"],
            "target_mode": target_mode,
            "mode_epoch": record["mode_epoch"],
        }

    def _public_review(self, review: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": "veyra.phase6.extension_deployment_review.v1",
            "review_id": review["review_id"],
            "review_revision": review["review_revision"],
            "status": review["status"],
            "proposal_digest": review["proposal_digest"],
            "deployment_id": review["proposal"]["deployment_id"],
            "release_id": review["proposal"]["release_id"],
            "current_mode": review["proposal"]["current_mode"],
            "target_mode": review["proposal"]["target_mode"],
            "approver_identity_digest": review.get(
                "approver_identity_digest"
            ),
            "approval_receipt_digest": review.get(
                "approval_receipt_digest"
            ),
            "approval_expires_at": review.get("approval_expires_at"),
            "raw_approver_credential_persisted": False,
            "authority": self._authority(),
        }

    @staticmethod
    def _review_receipt_digest(
        *,
        review_id: str,
        decided_at: str,
        expires_at: str,
        approver_identity_digest: str,
        proposal: dict[str, Any],
    ) -> str:
        return hashlib.sha256(
            json.dumps(
                {
                    "schema_version": (
                        "veyra.phase6.extension_deployment_review_receipt.v1"
                    ),
                    "review_id": review_id,
                    "status": "approved",
                    "decided_at": decided_at,
                    "expires_at": expires_at,
                    "approver_identity_digest": approver_identity_digest,
                    "proposal": proposal,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()

    # ------------------------------------------------------------------
    # Contract and projection helpers.
    # ------------------------------------------------------------------
    def _deployment_binding(
        self,
        *,
        deployment_id: str,
        subject: dict[str, Any],
        spec: ExtensionSpec,
        principal: str,
        session: str,
        request_provenance_digest: str,
        mode: str,
        mode_epoch: int,
        scope_digest: str,
        max_invocations: int,
        expires_at: str,
        backend: ExtensionInvocationBackendStatus,
    ) -> ExtensionDeploymentBinding:
        assert backend.engine_identity_digest is not None
        assert backend.image_id is not None
        assert backend.isolation_conformance_digest is not None
        assert backend.invocation_conformance_digest is not None
        release_expiry = parse_canonical_utc(subject["expires_at"])
        requested_expiry = parse_canonical_utc(expires_at)
        selected_expiry = canonical_utc(min(release_expiry, requested_expiry))
        return ExtensionDeploymentBinding(
            schema_version=EXTENSION_DEPLOYMENT_BINDING_SCHEMA_VERSION,
            deployment_id=deployment_id,
            release_id=subject["release_id"],
            release_revision=subject["release_revision"],
            manifest_digest=subject["manifest_digest"],
            attestation_digest=subject["attestation_digest"],
            signing_key_id=subject["signing_key_id"],
            owner_scope_digest=subject["owner_scope_digest"],
            authenticated_principal_digest=principal,
            initiating_session_digest=session,
            request_provenance_digest=request_provenance_digest,
            extension_id=spec.extension_id,
            extension_version=spec.version,
            artifact_sha256=subject["artifact_sha256"],
            spec_digest=spec.digest(),
            mode=mode,
            mode_epoch=mode_epoch,
            scope_digest=scope_digest,
            max_invocations=max_invocations,
            expires_at=selected_expiry,
            runner_engine_identity_digest=backend.engine_identity_digest,
            runner_image_id=backend.image_id,
            isolation_conformance_digest=backend.isolation_conformance_digest,
            invocation_conformance_digest=backend.invocation_conformance_digest,
            invocation_harness_digest=backend.harness_digest,
            deployment_policy_revision=EXTENSION_DEPLOYMENT_POLICY_REVISION,
            deployment_policy_digest=EXTENSION_DEPLOYMENT_POLICY_DIGEST,
        )

    @staticmethod
    def _validate_input(spec: ExtensionSpec, payload: dict[str, Any]) -> None:
        try:
            schema = spec.input_schema.model_dump(mode="json", by_alias=True)
            properties = schema["properties"]
            if (
                not set(schema["required"]).issubset(payload)
                or not set(payload).issubset(properties)
            ):
                raise ValueError
            # The fixed harness performs the authoritative primitive check.
            digest_canonical_value(payload)
        except Exception as exc:
            raise ValueError("input payload does not match the signed schema") from exc

    def _public_capability(
        self,
        record: dict[str, Any],
        spec: ExtensionSpec,
        pointer_revision: int,
    ) -> PublicExtensionCapability:
        binding = parse_deployment_binding(record["binding"])
        return PublicExtensionCapability(
            schema_version=EXTENSION_PUBLIC_CAPABILITY_SCHEMA_VERSION,
            capability_id=spec.extension_id,
            extension_id=spec.extension_id,
            extension_version=spec.version,
            deployment_id=record["deployment_id"],
            release_id=record["release_id"],
            attestation_digest=record["attestation_digest"],
            owner_scope_digest=record["owner_scope_digest"],
            workspace_identity_digest=self._workspace_digest(
                record["workspace_id"]
            ),
            initiating_session_digest=record["session_digest"],
            scope_digest=binding.scope_digest,
            mode="promoted",
            mode_epoch=record["mode_epoch"],
            active_pointer_revision=pointer_revision,
            input_schema_digest=self._digest(
                spec.input_schema.model_dump(mode="json", by_alias=True)
            ),
            output_schema_digest=self._digest(
                spec.output_schema.model_dump(mode="json", by_alias=True)
            ),
            expires_at=binding.expires_at,
            execution_boundary="trusted_isolated_runner_only",
            authority=ExtensionDeploymentAuthority(),
        )

    def _validate_public_subject(
        self,
        item: dict[str, Any],
        record: dict[str, Any],
        spec: ExtensionSpec,
    ) -> None:
        binding = parse_deployment_binding(record["binding"])
        if (
            record["mode"] != "promoted"
            or record["breaker_open"]
            or item.get("deployment_id") != record["deployment_id"]
            or item.get("release_id") != record["release_id"]
            or item.get("attestation_digest") != record["attestation_digest"]
            or item.get("owner_scope_digest") != record["owner_scope_digest"]
            or item.get("workspace_identity_digest")
            != self._workspace_digest(record["workspace_id"])
            or item.get("initiating_session_digest") != record["session_digest"]
            or item.get("extension_id") != binding.extension_id
            or item.get("extension_version") != binding.extension_version
            or item.get("mode_epoch") != record["mode_epoch"]
            or item.get("scope_digest") != binding.scope_digest
            or item.get("expires_at") != binding.expires_at
            or item.get("input_schema_digest")
            != self._digest(spec.input_schema.model_dump(mode="json", by_alias=True))
            or item.get("output_schema_digest")
            != self._digest(spec.output_schema.model_dump(mode="json", by_alias=True))
        ):
            raise ExtensionDeploymentStorageError(
                "public capability does not match the active signed deployment"
            )

    def _public_capability_from_record(self, record: dict[str, Any]) -> dict[str, Any]:
        binding = parse_deployment_binding(record["binding"])
        state = self._read_state()
        key = self._capability_key(
            record["owner_scope_digest"],
            record["workspace_id"],
            record["session_digest"],
            binding.extension_id,
        )
        public = state["public_registry"].get(key)
        if isinstance(public, dict) and public.get("deployment_id") == record["deployment_id"]:
            return dict(public)
        # Previous promoted rows may no longer be public. Preserve the exact
        # source-free schema digests captured when their pointer was active.
        snapshot = record.get("public_capability_snapshot")
        if isinstance(snapshot, dict):
            return dict(snapshot)
        raise ExtensionDeploymentStorageError(
            "rollback target has no source-free public projection"
        )

    def _public_record(self, record: dict[str, Any]) -> dict[str, Any]:
        self._validate_record(record)
        binding = parse_deployment_binding(record["binding"])
        return {
            "schema_version": "veyra.phase6.extension_deployment_record.v1",
            "deployment_id": record["deployment_id"],
            "revision": record["revision"],
            "release_id": record["release_id"],
            "release_revision": record["release_revision"],
            "attestation_digest": record["attestation_digest"],
            "owner_scope_digest": record["owner_scope_digest"],
            "extension_id": binding.extension_id,
            "extension_version": binding.extension_version,
            "mode": record["mode"],
            "mode_epoch": record["mode_epoch"],
            "stage": record["stage"],
            "binding_digest": record["binding_digest"],
            "scope_digest": binding.scope_digest,
            "max_invocations": binding.max_invocations,
            "invocations_used": record["invocations_used"],
            "successful_modes": list(record["successful_modes"]),
            "breaker_open": record["breaker_open"],
            "breaker_reason": record["breaker_reason"],
            "previous_deployment_id": record["previous_deployment_id"],
            "receipt_count": len(record["receipts"]),
            "expires_at": binding.expires_at,
            "created_at": record["created_at"],
            "updated_at": record["updated_at"],
            "source_persisted": False,
            "execution_boundary": "trusted_isolated_runner_only",
            "authority": self._authority(),
        }

    def _validate_record(self, record: dict[str, Any]) -> None:
        required = {
            "deployment_id",
            "user_id",
            "workspace_id",
            "session_id",
            "owner_scope_digest",
            "principal_digest",
            "session_digest",
            "release_id",
            "release_revision",
            "attestation_digest",
            "revision",
            "mode",
            "mode_epoch",
            "stage",
            "binding",
            "binding_digest",
            "signed_spec",
            "successful_modes",
            "invocations_used",
            "consecutive_failures",
            "breaker_open",
            "breaker_reason",
            "previous_deployment_id",
            "review",
            "receipts",
            "created_at",
            "updated_at",
        }
        if not required.issubset(record):
            raise ExtensionDeploymentStorageError("deployment record is incomplete")
        binding = parse_deployment_binding(record["binding"])
        signed_spec = parse_extension_spec(record["signed_spec"])
        if (
            record["deployment_id"] != binding.deployment_id
            or record["release_id"] != binding.release_id
            or record["release_revision"] != binding.release_revision
            or record["attestation_digest"] != binding.attestation_digest
            or record["owner_scope_digest"] != binding.owner_scope_digest
            or record["principal_digest"]
            != binding.authenticated_principal_digest
            or record["session_digest"] != binding.initiating_session_digest
            or record["mode"] not in _MODES
            or (
                record["mode"] != "disabled"
                and record["mode"] != binding.mode
            )
            or (
                record["mode"] != "disabled"
                and record["mode_epoch"] != binding.mode_epoch
            )
            or binding.binding_digest() != record["binding_digest"]
            or signed_spec.digest() != binding.spec_digest
            or signed_spec.extension_id != binding.extension_id
            or signed_spec.version != binding.extension_version
            or type(record["revision"]) is not int
            or record["revision"] < 1
            or type(record["invocations_used"]) is not int
            or record["invocations_used"] < 0
            or not isinstance(record["successful_modes"], list)
            or any(mode not in _MODES for mode in record["successful_modes"])
            or not isinstance(record["receipts"], list)
            or len(record["receipts"]) > MAX_EXTENSION_DEPLOYMENT_RECEIPTS
            or self._contains_private_source(record)
        ):
            raise ExtensionDeploymentStorageError("deployment record binding is invalid")

    def _read_state(self) -> dict[str, Any]:
        return self._normalize_state(self.state_store.read_json(DEPLOYMENT_STATE_FILE))

    def _normalize_state(self, raw: dict[str, Any]) -> dict[str, Any]:
        if not raw:
            return {
                "schema_version": DEPLOYMENT_STATE_SCHEMA_VERSION,
                "revision": 0,
                "deployments": {},
                "invocations": {},
                "operations": {},
                "active_pointers": {},
                "public_registry": {},
                "backend_snapshot": None,
            }
        if raw.get("_state_corrupt"):
            raise ExtensionDeploymentStorageError("deployment state is corrupt")
        state = {
            "schema_version": raw.get("schema_version"),
            "revision": raw.get("revision"),
            "deployments": raw.get("deployments"),
            "invocations": raw.get("invocations"),
            "operations": raw.get("operations"),
            "active_pointers": raw.get("active_pointers"),
            "public_registry": raw.get("public_registry"),
            "backend_snapshot": raw.get("backend_snapshot"),
        }
        if (
            state["schema_version"] != DEPLOYMENT_STATE_SCHEMA_VERSION
            or type(state["revision"]) is not int
            or state["revision"] < 0
            or any(
                not isinstance(state[field], dict)
                for field in (
                    "deployments",
                    "invocations",
                    "operations",
                    "active_pointers",
                    "public_registry",
                )
            )
            or self._contains_private_source(state)
        ):
            raise ExtensionDeploymentStorageError("deployment state is invalid")
        for record in state["deployments"].values():
            if not isinstance(record, dict):
                raise ExtensionDeploymentStorageError("deployment row is invalid")
            self._validate_record(record)
        for item in state["public_registry"].values():
            PublicExtensionCapability.model_validate(item, strict=True)
        self._validate_registry_links(state)
        return state

    def _validate_registry_links(self, state: dict[str, Any]) -> None:
        linked_capabilities: set[str] = set()
        for pointer_key, pointer in state["active_pointers"].items():
            if not isinstance(pointer_key, str) or not isinstance(pointer, dict):
                raise ExtensionDeploymentStorageError(
                    "active extension pointer is invalid"
                )
            deployment_id = pointer.get("deployment_id")
            record = state["deployments"].get(deployment_id)
            if not isinstance(record, dict):
                raise ExtensionDeploymentStorageError(
                    "active extension pointer is orphaned"
                )
            self._validate_record(record)
            binding = parse_deployment_binding(record["binding"])
            expected_pointer_key = self._pointer_key(
                record["owner_scope_digest"],
                record["workspace_id"],
                record["session_digest"],
                binding.extension_id,
            )
            capability_key = self._capability_key(
                record["owner_scope_digest"],
                record["workspace_id"],
                record["session_digest"],
                binding.extension_id,
            )
            public = state["public_registry"].get(capability_key)
            if (
                pointer_key != expected_pointer_key
                or record["mode"] != "promoted"
                or record["breaker_open"]
                or pointer.get("release_id") != record["release_id"]
                or pointer.get("attestation_digest")
                != record["attestation_digest"]
                or type(pointer.get("revision")) is not int
                or pointer["revision"] < 1
                or not isinstance(public, dict)
                or public.get("deployment_id") != deployment_id
                or public.get("active_pointer_revision") != pointer["revision"]
                or public.get("release_id") != pointer.get("release_id")
                or public.get("attestation_digest")
                != pointer.get("attestation_digest")
                or public.get("owner_scope_digest")
                != record["owner_scope_digest"]
                or public.get("workspace_identity_digest")
                != self._workspace_digest(record["workspace_id"])
                or public.get("initiating_session_digest")
                != record["session_digest"]
                or public.get("mode_epoch") != record["mode_epoch"]
                or public.get("scope_digest") != binding.scope_digest
            ):
                raise ExtensionDeploymentStorageError(
                    "public extension registry and active pointer diverged"
                )
            linked_capabilities.add(capability_key)
        if set(state["public_registry"]) != linked_capabilities:
            raise ExtensionDeploymentStorageError(
                "public extension registry contains an orphan projection"
            )

    @staticmethod
    def _contains_private_source(value: Any) -> bool:
        if isinstance(value, dict):
            forbidden_keys = {
                "source_bytes",
                "artifact_bytes",
                "source_check_spec",
                "release_manifest",
                "validation_report",
                "output_payload",
            }
            return bool(forbidden_keys.intersection(value)) or any(
                ExtensionDeploymentGate._contains_private_source(item)
                for item in value.values()
            )
        if isinstance(value, list):
            return any(
                ExtensionDeploymentGate._contains_private_source(item)
                for item in value
            )
        return isinstance(value, bytes)

    def _record(self, state: dict[str, Any], deployment_id: str) -> dict[str, Any]:
        selected = self._deployment_id(deployment_id)
        record = state["deployments"].get(selected)
        if not isinstance(record, dict):
            raise ExtensionDeploymentNotFoundError("deployment was not found")
        self._validate_record(record)
        return record

    @staticmethod
    def _record_owned(
        record: dict[str, Any], owner: str, principal: str, session: str
    ) -> bool:
        return bool(
            record.get("owner_scope_digest") == owner
            and record.get("principal_digest") == principal
            and record.get("session_digest") == session
        )

    def _require_owner(
        self, record: dict[str, Any], owner: str, principal: str, session: str
    ) -> None:
        if not self._record_owned(record, owner, principal, session):
            raise ExtensionDeploymentNotFoundError("deployment was not found")

    @staticmethod
    def _require_state_revision(state: dict[str, Any], expected: int) -> None:
        if type(expected) is not int or expected < 0 or state["revision"] != expected:
            raise ExtensionDeploymentConflictError("deployment state CAS failed")

    @staticmethod
    def _require_deployment_revision(
        record: dict[str, Any], expected_revision: int, expected_epoch: int
    ) -> None:
        if (
            type(expected_revision) is not int
            or type(expected_epoch) is not int
            or record["revision"] != expected_revision
            or record["mode_epoch"] != expected_epoch
        ):
            raise ExtensionDeploymentConflictError(
                "deployment revision or mode epoch CAS failed"
            )

    def _operation_replay(
        self, state: dict[str, Any], operation_id: str, digest: str
    ) -> dict[str, Any] | None:
        receipt = state["operations"].get(operation_id)
        if receipt is None:
            return None
        if not isinstance(receipt, dict) or receipt.get("digest") != digest:
            raise ExtensionDeploymentConflictError(
                "operation id was already used with another request"
            )
        return receipt

    @staticmethod
    def _replay_command(
        state: dict[str, Any],
        operation_id: str,
        request_digest: str,
        *,
        expected_kind: str,
    ) -> dict[str, Any] | None:
        receipt = state["operations"].get(operation_id)
        if receipt is None:
            return None
        result = receipt.get("result") if isinstance(receipt, dict) else None
        if (
            not isinstance(result, dict)
            or result.get("kind") != expected_kind
            or result.get("request_digest") != request_digest
        ):
            raise ExtensionDeploymentConflictError(
                "operation id was already used with another request"
            )
        return result

    @staticmethod
    def _operation_result(
        state: dict[str, Any], receipt: dict[str, Any]
    ) -> dict[str, Any] | None:
        result = receipt.get("result")
        if not isinstance(result, dict):
            return None
        if result.get("kind") == "invocation":
            row = state["invocations"].get(result.get("invocation_id"))
            if not isinstance(row, dict) or not isinstance(
                row.get("result_receipt"), dict
            ):
                raise ExtensionDeploymentConflictError(
                    "indeterminate invocation cannot be retried"
                )
            receipt = dict(row["result_receipt"])
            deployment = state["deployments"].get(row["deployment_id"])
            if not isinstance(deployment, dict):
                raise ExtensionDeploymentStorageError("invocation owner is unavailable")
            return {
                "schema_version": "veyra.phase6.extension_invocation_response.v1",
                "result": receipt,
                "result_digest": receipt["result_digest"],
                "deployment": ExtensionDeploymentGate._static_public_record(deployment),
                "state_revision": state["revision"],
                "authority": ExtensionDeploymentAuthority().model_dump(mode="json"),
            }
        return None

    def _record_is_active(
        self, state: dict[str, Any], record: dict[str, Any]
    ) -> bool:
        binding = parse_deployment_binding(record["binding"])
        pointer = state["active_pointers"].get(
            self._pointer_key(
                record["owner_scope_digest"],
                record["workspace_id"],
                record["session_digest"],
                binding.extension_id,
            )
        )
        return bool(
            isinstance(pointer, dict)
            and pointer.get("deployment_id") == record["deployment_id"]
            and pointer.get("release_id") == record["release_id"]
            and pointer.get("attestation_digest")
            == record["attestation_digest"]
        )

    @staticmethod
    def _remember_operation(
        state: dict[str, Any], operation_id: str, digest: str, result: dict[str, Any]
    ) -> None:
        state["operations"][operation_id] = {
            "digest": digest,
            "result": result,
        }
        if len(state["operations"]) > 512:
            oldest = next(iter(state["operations"]))
            state["operations"].pop(oldest, None)

    @staticmethod
    def _static_public_record(record: dict[str, Any]) -> dict[str, Any]:
        binding = parse_deployment_binding(record["binding"])
        return {
            "schema_version": "veyra.phase6.extension_deployment_record.v1",
            "deployment_id": record["deployment_id"],
            "revision": record["revision"],
            "release_id": record["release_id"],
            "release_revision": record["release_revision"],
            "attestation_digest": record["attestation_digest"],
            "owner_scope_digest": record["owner_scope_digest"],
            "extension_id": binding.extension_id,
            "extension_version": binding.extension_version,
            "mode": record["mode"],
            "mode_epoch": record["mode_epoch"],
            "stage": record["stage"],
            "binding_digest": record["binding_digest"],
            "scope_digest": binding.scope_digest,
            "max_invocations": binding.max_invocations,
            "invocations_used": record["invocations_used"],
            "successful_modes": list(record["successful_modes"]),
            "breaker_open": record["breaker_open"],
            "breaker_reason": record["breaker_reason"],
            "previous_deployment_id": record["previous_deployment_id"],
            "receipt_count": len(record["receipts"]),
            "expires_at": binding.expires_at,
            "created_at": record["created_at"],
            "updated_at": record["updated_at"],
            "source_persisted": False,
            "execution_boundary": "trusted_isolated_runner_only",
            "authority": ExtensionDeploymentAuthority().model_dump(mode="json"),
        }

    def _receipt(
        self, *, kind: str, mode: str, status: str, digest: str
    ) -> dict[str, Any]:
        return {
            "schema_version": DEPLOYMENT_EVENT_SCHEMA_VERSION,
            "kind": kind,
            "mode": mode,
            "status": status,
            "evidence_digest": digest,
            "recorded_at": self._now_iso(),
        }

    @staticmethod
    def _append_receipt(
        receipts: list[dict[str, Any]], receipt: dict[str, Any]
    ) -> list[dict[str, Any]]:
        return (list(receipts) + [receipt])[-MAX_EXTENSION_DEPLOYMENT_RECEIPTS:]

    @staticmethod
    def _stage_for_mode(mode: str) -> str:
        return {
            "shadow": "SHADOW_ACTIVE",
            "read_only_canary": "READ_ONLY_CANARY_ACTIVE",
            "scoped_canary": "SCOPED_CANARY_ACTIVE",
            "promoted": "PROMOTED_ACTIVE",
        }[mode]

    def _active_pointer_for_record(
        self, state: dict[str, Any], record: dict[str, Any]
    ) -> dict[str, Any] | None:
        binding = parse_deployment_binding(record["binding"])
        pointer = state["active_pointers"].get(
            self._pointer_key(
                record["owner_scope_digest"],
                record["workspace_id"],
                record["session_digest"],
                binding.extension_id,
            )
        )
        return dict(pointer) if isinstance(pointer, dict) else None

    @staticmethod
    def _pointer_key(
        owner: str,
        workspace: str,
        session: str,
        extension_id: str,
    ) -> str:
        return hashlib.sha256(
            json.dumps(
                {
                    "schema_version": (
                        "veyra.phase6.extension_active_pointer_key.v2"
                    ),
                    "owner_scope_digest": owner,
                    "workspace_id": workspace,
                    "initiating_session_digest": session,
                    "extension_id": extension_id,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _workspace_digest(workspace_id: str) -> str:
        return hashlib.sha256(
            json.dumps(
                {"workspace_id": str(workspace_id)},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _capability_key(
        owner: str,
        workspace: str,
        session: str,
        extension_id: str,
    ) -> str:
        return hashlib.sha256(
            json.dumps(
                {
                    "schema_version": (
                        "veyra.phase6.public_extension_capability_key.v2"
                    ),
                    "owner_scope_digest": owner,
                    "workspace_id": workspace,
                    "initiating_session_digest": session,
                    "extension_id": extension_id,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _scope_digest(
        *, owner: str, workspace_id: str, session: str, mode: str
    ) -> str:
        return hashlib.sha256(
            json.dumps(
                {
                    "schema_version": "veyra.phase6.extension_deployment_scope.v1",
                    "owner_scope_digest": owner,
                    "workspace_id": workspace_id,
                    "initiating_session_digest": session,
                    "mode": mode,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _request_provenance(
        *,
        operation_id: str,
        request_id: str,
        owner: str,
        principal: str,
        session: str,
        release_id: str,
    ) -> str:
        return hashlib.sha256(
            json.dumps(
                {
                    "schema_version": "veyra.phase6.extension_deployment_request.v1",
                    "operation_id": operation_id,
                    "request_id": request_id,
                    "owner_scope_digest": owner,
                    "authenticated_principal_digest": principal,
                    "initiating_session_digest": session,
                    "release_id": release_id,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    def _scope(
        self, user_id: str, workspace_id: str, session_id: str, token: str
    ) -> tuple[str, str, str]:
        user = self._text(user_id, "user_id")
        workspace = self._text(workspace_id, "workspace_id")
        session = self._text(session_id, "session_id")
        owner = artifact_owner_scope_digest(user, workspace)
        return (
            owner,
            authenticated_local_principal_digest(token),
            initiating_session_digest(session),
        )

    def _authorize(self, control_token: str, *, require_enabled: bool) -> str:
        token = str(control_token or "").strip()
        expected = str(
            self._configured_control_token
            or os.environ.get("VEYRA_LOCAL_API_TOKEN")
            or ""
        ).strip()
        if not expected or not token or not secrets.compare_digest(token, expected):
            raise ExtensionDeploymentUnauthorizedError(
                "valid local control token is required"
            )
        if require_enabled and self.lifecycle_mode == "disabled":
            raise ExtensionDeploymentUnavailableError(
                "signed extension deployment lifecycle is disabled"
            )
        return token

    def _approver_token_value(self) -> str:
        return str(
            self._configured_approver_token
            or os.environ.get("VEYRA_PHASE6_EXTENSION_APPROVER_TOKEN")
            or ""
        ).strip()

    def _authorize_approver(self, supplied: str) -> str:
        expected = self._approver_token_value()
        control = str(
            self._configured_control_token
            or os.environ.get("VEYRA_LOCAL_API_TOKEN")
            or ""
        ).strip()
        selected = str(supplied or "").strip()
        if (
            not expected
            or not selected
            or not secrets.compare_digest(selected, expected)
            or (control and secrets.compare_digest(selected, control))
        ):
            raise ExtensionDeploymentUnauthorizedError(
                "valid distinct extension approver credential is required"
            )
        return selected

    @staticmethod
    def _approver_identity_digest(token: str) -> str:
        return hashlib.sha256(
            b"veyra.phase6.extension_deployment_approver.v1\x00"
            + token.encode("utf-8")
        ).hexdigest()

    def _future_expiry(self, value: str) -> str:
        parsed = parse_canonical_utc(self._text(value, "expires_at"))
        if parsed <= self._now_value():
            raise ValueError("deployment expiry must be in the future")
        return canonical_utc(parsed)

    @staticmethod
    def _limit(value: int) -> int:
        if type(value) is not int or not 1 <= value <= 100:
            raise ValueError("limit must be between 1 and 100")
        return value

    @staticmethod
    def _revision(value: int, field: str, *, minimum: int) -> int:
        if type(value) is not int or not minimum <= value <= 2_147_483_647:
            raise ValueError(f"{field} is invalid")
        return value

    @staticmethod
    def _operation(value: str) -> str:
        selected = str(value or "").strip()
        if not _OPERATION_ID.fullmatch(selected):
            raise ValueError("operation_id is invalid")
        return selected

    @staticmethod
    def _deployment_id(value: str) -> str:
        if not isinstance(value, str) or not _DEPLOYMENT_ID.fullmatch(value):
            raise ValueError("deployment_id is invalid")
        return value

    @staticmethod
    def _release_id(value: str) -> str:
        if not isinstance(value, str) or not _RELEASE_ID.fullmatch(value):
            raise ValueError("release_id is invalid")
        return value

    @staticmethod
    def _digest_value(value: str, field: str) -> str:
        if not isinstance(value, str) or not _DIGEST.fullmatch(value):
            raise ValueError(f"{field} is invalid")
        return value

    @staticmethod
    def _text(value: str, field: str) -> str:
        selected = str(value or "").strip()
        if not selected or len(selected) > 240 or "\x00" in selected:
            raise ValueError(f"{field} is invalid")
        return selected

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
    def _authority() -> dict[str, Any]:
        return ExtensionDeploymentAuthority().model_dump(mode="json")

    def _now_value(self) -> datetime:
        selected = self._now()
        if not isinstance(selected, datetime) or selected.tzinfo is None:
            raise ExtensionDeploymentUnavailableError("deployment clock is invalid")
        return selected.astimezone(timezone.utc)

    def _now_iso(self) -> str:
        return canonical_utc(self._now_value())


__all__ = [
    "DEPLOYMENT_STATE_FILE",
    "ExtensionDeploymentConflictError",
    "ExtensionDeploymentError",
    "ExtensionDeploymentGate",
    "ExtensionDeploymentNotFoundError",
    "ExtensionDeploymentStorageError",
    "ExtensionDeploymentUnauthorizedError",
    "ExtensionDeploymentUnavailableError",
]
