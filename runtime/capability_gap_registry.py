from __future__ import annotations

from datetime import datetime, timezone
import copy
import hashlib
import hmac
import os
import re
from typing import Any, Callable

from pydantic import TypeAdapter, ValidationError

from core.world_state import WorldStateStore
from interface.extension_artifact import artifact_owner_scope_digest
from interface.extension_generation import (
    authenticated_local_principal_digest,
    initiating_session_digest,
)
from interface.capability_gap import (
    CAPABILITY_GAP_POLICY_REVISION,
    CapabilityGapAuthority,
    CapabilityGapLifecycleReceipt,
    canonical_digest,
    deterministic_capability_gap_id,
    validate_gap_id,
)
from runtime.extension_spec_quarantine import (
    ExtensionSpecConflictError,
    ExtensionSpecNotFoundError,
    ExtensionSpecQuarantine,
    ExtensionSpecQuarantineError,
    ExtensionSpecStorageError,
)


STATE_FILE = "phase6_capability_gap_state.json"
STATE_SCHEMA_VERSION = "veyra.phase6.capability_gap_state.v1"
RECORD_SCHEMA_VERSION = "veyra.phase6.capability_gap_private_record.v1"
OPERATION_SCHEMA_VERSION = "veyra.phase6.capability_gap_operation.v1"
PUBLIC_STATUS_SCHEMA = "veyra.phase6.capability_gap_status.v1"
PUBLIC_RECORD_SCHEMA = "veyra.phase6.capability_gap_record.v1"
PUBLIC_LIST_SCHEMA = "veyra.phase6.capability_gap_list.v1"
PUBLIC_TIMELINE_SCHEMA = "veyra.phase6.capability_gap_timeline.v1"
CONTROL_TOKEN_ENV = "VEYRA_LOCAL_API_TOKEN"

MAX_GAPS = 500
MAX_OPERATIONS = 4_000
MAX_HISTORY = 64

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,239}$")
_CANDIDATE_ID = re.compile(r"^extspec_[0-9a-f]{24}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SANITIZED_SOURCE = re.compile(r"^source_[0-9a-f]{16}$")
_SANITIZED_REASON = re.compile(r"^reason_[0-9a-f]{16}$")
_ALLOWED_STAGES = frozenset(
    {"GAP_RECORDED", "SPEC_LINKED_REVIEW_REQUIRED"}
)
_RECEIPT_KINDS = ("generation", "validation", "release", "deployment")
_STATE_FIELDS = frozenset(
    {
        "schema_version",
        "registry_revision",
        "gaps",
        "proposal_index",
        "operation_index",
        "updated_at",
    }
)
_STATE_METADATA_FIELDS = frozenset(
    {"_state_revision", "source", "confidence", "ttl_seconds", "status"}
)
_RECORD_FIELDS = frozenset(
    {
        "schema_version",
        "gap_id",
        "proposal_id",
        "intent_id",
        "user_id",
        "workspace_id",
        "session_id",
        "owner_scope_digest",
        "principal_digest",
        "session_digest",
        "source_code",
        "reason_code",
        "stage",
        "required_action",
        "revision",
        "spec_link",
        "observations",
        "history",
        "created_at",
        "updated_at",
    }
)
_SPEC_LINK_FIELDS = frozenset(
    {
        "candidate_id",
        "candidate_revision",
        "spec_digest",
        "candidate_stage",
        "extension_id",
        "extension_version",
        "linked_at",
    }
)
_HISTORY_FIELDS = frozenset(
    {
        "revision",
        "transition",
        "lifecycle_kind",
        "lifecycle_status",
        "receipt_digest",
        "recorded_at",
    }
)
_OPERATION_FIELDS = frozenset(
    {
        "schema_version",
        "kind",
        "request_digest",
        "gap_id",
        "result_revision",
        "recorded_at",
    }
)

_RECEIPT_ADAPTER = TypeAdapter(CapabilityGapLifecycleReceipt)


class CapabilityGapError(RuntimeError):
    """Base failure for the private capability-gap registry."""


class CapabilityGapConflictError(CapabilityGapError):
    """A CAS, replay, ownership, or lifecycle identity conflicted."""


class CapabilityGapNotFoundError(CapabilityGapError):
    """The gap was absent or outside the caller's exact scope."""


class CapabilityGapStorageError(CapabilityGapError):
    """The durable registry or a prerequisite is corrupt/unavailable."""


class CapabilityGapUnauthorizedError(CapabilityGapError):
    """The exact local control-plane token was missing or invalid."""


class CapabilityGapUnavailableError(CapabilityGapError):
    """The private control principal or required spec gate is unavailable."""


class CapabilityGapRegistry:
    """Durable, source-free bridge from runtime gaps to Phase 6 evidence.

    Recording a gap grants no capability authority.  Linking a spec only
    observes an already quarantined/passed ExtensionSpec.  Later receipts are
    explicit caller projections and never trigger the corresponding stage.
    """

    def __init__(
        self,
        *,
        state_store: WorldStateStore,
        spec_quarantine: ExtensionSpecQuarantine | None = None,
        control_token: str | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.state_store = state_store
        self.spec_quarantine = spec_quarantine
        self.control_token = (
            str(control_token).strip()
            if control_token is not None
            else os.getenv(CONTROL_TOKEN_ENV, "").strip()
        )
        self._now = now or (lambda: datetime.now(timezone.utc))

    def record_from_proposal(
        self,
        *,
        proposal: dict[str, Any],
        intent: dict[str, Any],
        reason_code: str,
    ) -> dict[str, Any]:
        """Internal fail-closed write used by the proposal registry.

        The caller deliberately handles failures so proposal creation itself
        remains fail-open.  No raw user text, topic, entities, desired outcome,
        or inferred contract is copied into the gap record.
        """

        if not isinstance(proposal, dict) or not isinstance(intent, dict):
            raise TypeError("proposal and intent must be mappings")
        local_world = self.state_store.read_json("local_world.json")
        if local_world.get("_state_corrupt") is True:
            raise CapabilityGapStorageError("local workspace state is corrupt")
        workspace_id = self._required_text(
            local_world.get("current_project"), "workspace_id"
        )
        proposal_id = self._required_id(
            proposal.get("proposal_id"), "proposal_id"
        )
        intent_id = self._required_id(intent.get("intent_id"), "intent_id")
        user_id = self._required_text(intent.get("user_id"), "user_id")
        session_id = self._required_text(
            intent.get("session_id"), "session_id"
        )
        source_code = self._proposal_source_code(proposal.get("source"))
        selected_reason = self._reason_code(reason_code)
        principal_digest = self._internal_principal_digest()
        session_digest = initiating_session_digest(session_id)
        owner_scope = artifact_owner_scope_digest(user_id, workspace_id)
        gap_id = deterministic_capability_gap_id(
            proposal_id=proposal_id,
            intent_id=intent_id,
            user_id=user_id,
            workspace_id=workspace_id,
            session_id=session_id,
            source_code=source_code,
        )
        proposal_key = self._proposal_key(owner_scope, proposal_id)
        selected: dict[str, Any] | None = None
        existing = False

        def write_gap(state: dict[str, Any]) -> None:
            nonlocal selected, existing
            self._initialize_state(state)
            self._validate_state(state)
            indexed = state["proposal_index"].get(proposal_key)
            if indexed is not None:
                record = self._record_by_id(state, str(indexed))
                expected = {
                    "gap_id": gap_id,
                    "proposal_id": proposal_id,
                    "intent_id": intent_id,
                    "user_id": user_id,
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                    "owner_scope_digest": owner_scope,
                    "principal_digest": principal_digest,
                    "session_digest": session_digest,
                    "source_code": source_code,
                    "reason_code": selected_reason,
                }
                if any(record.get(key) != value for key, value in expected.items()):
                    raise CapabilityGapConflictError(
                        "proposal identity already maps to another capability gap"
                    )
                selected = record
                existing = True
                return
            if len(state["gaps"]) >= MAX_GAPS:
                raise CapabilityGapConflictError(
                    "capability-gap registry capacity is exhausted"
                )
            if gap_id in state["gaps"]:
                raise CapabilityGapConflictError(
                    "capability-gap content identity collided"
                )
            created_at = self._now_iso()
            record = {
                "schema_version": RECORD_SCHEMA_VERSION,
                "gap_id": gap_id,
                "proposal_id": proposal_id,
                "intent_id": intent_id,
                "user_id": user_id,
                "workspace_id": workspace_id,
                "session_id": session_id,
                "owner_scope_digest": owner_scope,
                "principal_digest": principal_digest,
                "session_digest": session_digest,
                "source_code": source_code,
                "reason_code": selected_reason,
                "stage": "GAP_RECORDED",
                "required_action": "SPEC_REQUIRED",
                "revision": 1,
                "spec_link": None,
                "observations": {kind: None for kind in _RECEIPT_KINDS},
                "history": [
                    self._history_item(
                        revision=1,
                        transition="gap_recorded",
                        recorded_at=created_at,
                    )
                ],
                "created_at": created_at,
                "updated_at": created_at,
            }
            state["gaps"][gap_id] = record
            state["proposal_index"][proposal_key] = gap_id
            state["registry_revision"] += 1
            selected = record

        self._mutate(write_gap)
        if selected is None:
            raise CapabilityGapStorageError("gap recording produced no record")
        return self._public_record(selected, gap_existing=existing)

    def status(
        self,
        *,
        user_id: str,
        workspace_id: str,
        session_id: str,
        control_token: str,
    ) -> dict[str, Any]:
        owner, principal, session, state = self._owner_state(
            user_id, workspace_id, session_id, control_token
        )
        records = self._scoped_records(state, owner, principal, session)
        counts: dict[str, int] = {}
        for record in records:
            counts[record["stage"]] = counts.get(record["stage"], 0) + 1
        return {
            "schema_version": PUBLIC_STATUS_SCHEMA,
            "phase": "6.2-capability-gap-bridge",
            "status": "available",
            "record_count": len(records),
            "counts": counts,
            "registry_revision": state["registry_revision"],
            "default_stage": "GAP_RECORDED",
            "default_required_action": "SPEC_REQUIRED",
            "automatic_advancement": False,
            "read_side_effects": "none",
            "policy_revision": CAPABILITY_GAP_POLICY_REVISION,
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
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer from 1 to 100")
        owner, principal, session, state = self._owner_state(
            user_id, workspace_id, session_id, control_token
        )
        records = self._scoped_records(state, owner, principal, session)
        records.sort(
            key=lambda item: (item["updated_at"], item["gap_id"]),
            reverse=True,
        )
        return {
            "schema_version": PUBLIC_LIST_SCHEMA,
            "count": min(len(records), limit),
            "gaps": [self._public_record(item) for item in records[:limit]],
            "authority": self._authority(),
        }

    def get(
        self,
        *,
        gap_id: str,
        user_id: str,
        workspace_id: str,
        session_id: str,
        control_token: str,
    ) -> dict[str, Any]:
        owner, principal, session, state = self._owner_state(
            user_id, workspace_id, session_id, control_token
        )
        record = self._record_by_id(state, validate_gap_id(gap_id))
        self._require_owner(record, owner, principal, session)
        return self._public_record(record)

    def timeline(
        self,
        *,
        gap_id: str,
        user_id: str,
        workspace_id: str,
        session_id: str,
        control_token: str,
    ) -> dict[str, Any]:
        owner, principal, session, state = self._owner_state(
            user_id, workspace_id, session_id, control_token
        )
        record = self._record_by_id(state, validate_gap_id(gap_id))
        self._require_owner(record, owner, principal, session)
        return {
            "schema_version": PUBLIC_TIMELINE_SCHEMA,
            "gap_id": record["gap_id"],
            "gap_revision": record["revision"],
            "stage": record["stage"],
            "events": copy.deepcopy(record["history"]),
            "source_free": True,
            "authority": self._authority(),
        }

    def link_spec_candidate(
        self,
        *,
        gap_id: str,
        candidate_id: str,
        user_id: str,
        workspace_id: str,
        session_id: str,
        expected_gap_revision: int,
        expected_candidate_revision: int,
        expected_spec_digest: str,
        operation_id: str,
        control_token: str,
    ) -> dict[str, Any]:
        token = self._authorize(control_token)
        selected_gap = validate_gap_id(gap_id)
        selected_candidate = self._required_id(candidate_id, "candidate_id")
        selected_user = self._required_text(user_id, "user_id")
        selected_workspace = self._required_text(workspace_id, "workspace_id")
        selected_session = self._required_text(session_id, "session_id")
        gap_revision = self._required_revision(
            expected_gap_revision, "expected_gap_revision"
        )
        candidate_revision = self._required_revision(
            expected_candidate_revision, "expected_candidate_revision"
        )
        spec_digest = self._required_digest(
            expected_spec_digest, "expected_spec_digest"
        )
        selected_operation = self._required_id(operation_id, "operation_id")
        owner = artifact_owner_scope_digest(selected_user, selected_workspace)
        principal = authenticated_local_principal_digest(token)
        session = initiating_session_digest(selected_session)
        operation_key = self._operation_key(
            principal, session, selected_operation
        )
        request_digest = canonical_digest(
            {
                "kind": "link_spec_candidate",
                "gap_id": selected_gap,
                "candidate_id": selected_candidate,
                "owner_scope_digest": owner,
                "principal_digest": principal,
                "session_digest": session,
                "expected_gap_revision": gap_revision,
                "expected_candidate_revision": candidate_revision,
                "expected_spec_digest": spec_digest,
            }
        )
        selected: dict[str, Any] | None = None
        replayed = False

        def link(state: dict[str, Any]) -> None:
            nonlocal selected, replayed
            self._initialize_state(state)
            self._validate_state(state)
            replay = self._operation_replay(
                state,
                operation_key=operation_key,
                request_digest=request_digest,
                kind="link_spec_candidate",
                gap_id=selected_gap,
            )
            if replay is not None:
                record = self._record_by_id(state, selected_gap)
                self._require_owner(record, owner, principal, session)
                selected = record
                replayed = True
                return
            record = self._record_by_id(state, selected_gap)
            self._require_owner(record, owner, principal, session)
            if record["revision"] != gap_revision:
                raise CapabilityGapConflictError(
                    "capability-gap revision changed"
                )
            if record["stage"] != "GAP_RECORDED" or record["spec_link"] is not None:
                raise CapabilityGapConflictError(
                    "capability gap already has a specification link"
                )
            if self.spec_quarantine is None:
                raise CapabilityGapUnavailableError(
                    "ExtensionSpec quarantine is not connected"
                )
            try:
                subject = self.spec_quarantine.artifact_subject(
                    candidate_id=selected_candidate,
                    user_id=selected_user,
                    workspace_id=selected_workspace,
                    require_gate_passed=False,
                )
            except (ExtensionSpecNotFoundError, ExtensionSpecConflictError):
                raise
            except ExtensionSpecQuarantineError as exc:
                raise CapabilityGapStorageError(
                    "ExtensionSpec prerequisite is unavailable"
                ) from exc
            if (
                subject.get("candidate_revision") != candidate_revision
                or subject.get("spec_digest") != spec_digest
                or subject.get("owner_scope_digest") != owner
                or subject.get("candidate_stage")
                not in {"SPEC_QUARANTINED", "SPEC_GATE_PASSED"}
            ):
                raise CapabilityGapConflictError(
                    "ExtensionSpec projection does not match the requested link"
                )
            authority = subject.get("authority")
            expected_zero_authority = {
                "code_generation",
                "artifact_write",
                "agent_dispatch",
                "tool_access",
                "execution",
                "signing",
                "activation",
                "capability_registration",
                "promotion",
            }
            if (
                not isinstance(authority, dict)
                or not expected_zero_authority.issubset(authority)
                or any(
                    authority.get(field) is not False
                    for field in expected_zero_authority
                )
            ):
                raise CapabilityGapConflictError(
                    "ExtensionSpec projection carries unexpected authority"
                )
            now = self._now_iso()
            record["spec_link"] = {
                "candidate_id": selected_candidate,
                "candidate_revision": candidate_revision,
                "spec_digest": spec_digest,
                "candidate_stage": subject["candidate_stage"],
                "extension_id": subject["extension_id"],
                "extension_version": subject["extension_version"],
                "linked_at": now,
            }
            record["stage"] = "SPEC_LINKED_REVIEW_REQUIRED"
            record["required_action"] = "HUMAN_REVIEW_REQUIRED"
            record["revision"] += 1
            record["updated_at"] = now
            record["history"].append(
                self._history_item(
                    revision=record["revision"],
                    transition="spec_candidate_linked",
                    recorded_at=now,
                )
            )
            self._record_operation(
                state,
                operation_key=operation_key,
                request_digest=request_digest,
                kind="link_spec_candidate",
                gap_id=selected_gap,
                result_revision=record["revision"],
                recorded_at=now,
            )
            state["registry_revision"] += 1
            selected = record

        self._mutate(link)
        if selected is None:
            raise CapabilityGapStorageError("spec linkage produced no record")
        return self._public_record(selected, operation_replayed=replayed)

    def observe_lifecycle(
        self,
        *,
        gap_id: str,
        receipt: CapabilityGapLifecycleReceipt | dict[str, Any],
        user_id: str,
        workspace_id: str,
        session_id: str,
        expected_gap_revision: int,
        operation_id: str,
        control_token: str,
    ) -> dict[str, Any]:
        token = self._authorize(control_token)
        selected_gap = validate_gap_id(gap_id)
        try:
            parsed = _RECEIPT_ADAPTER.validate_python(receipt, strict=True)
        except ValidationError as exc:
            raise ValueError("lifecycle receipt is invalid") from exc
        canonical_receipt = parsed.model_dump(mode="json")
        receipt_digest = parsed.digest()
        selected_user = self._required_text(user_id, "user_id")
        selected_workspace = self._required_text(workspace_id, "workspace_id")
        selected_session = self._required_text(session_id, "session_id")
        selected_revision = self._required_revision(
            expected_gap_revision, "expected_gap_revision"
        )
        selected_operation = self._required_id(operation_id, "operation_id")
        owner = artifact_owner_scope_digest(selected_user, selected_workspace)
        principal = authenticated_local_principal_digest(token)
        session = initiating_session_digest(selected_session)
        operation_key = self._operation_key(
            principal, session, selected_operation
        )
        request_digest = canonical_digest(
            {
                "kind": "observe_lifecycle",
                "gap_id": selected_gap,
                "owner_scope_digest": owner,
                "principal_digest": principal,
                "session_digest": session,
                "expected_gap_revision": selected_revision,
                "receipt_digest": receipt_digest,
            }
        )
        selected: dict[str, Any] | None = None
        replayed = False

        def observe(state: dict[str, Any]) -> None:
            nonlocal selected, replayed
            self._initialize_state(state)
            self._validate_state(state)
            replay = self._operation_replay(
                state,
                operation_key=operation_key,
                request_digest=request_digest,
                kind="observe_lifecycle",
                gap_id=selected_gap,
            )
            if replay is not None:
                record = self._record_by_id(state, selected_gap)
                self._require_owner(record, owner, principal, session)
                selected = record
                replayed = True
                return
            record = self._record_by_id(state, selected_gap)
            self._require_owner(record, owner, principal, session)
            if record["revision"] != selected_revision:
                raise CapabilityGapConflictError(
                    "capability-gap revision changed"
                )
            if record["stage"] != "SPEC_LINKED_REVIEW_REQUIRED":
                raise CapabilityGapConflictError(
                    "a verified spec link is required before observations"
                )
            self._validate_receipt_chain(record, canonical_receipt)
            kind = canonical_receipt["receipt_kind"]
            current = record["observations"].get(kind)
            self._validate_receipt_transition(kind, current, canonical_receipt)
            now = self._now_iso()
            record["observations"][kind] = {
                "receipt": canonical_receipt,
                "receipt_digest": receipt_digest,
                "observed_at": now,
            }
            record["revision"] += 1
            record["updated_at"] = now
            record["history"].append(
                self._history_item(
                    revision=record["revision"],
                    transition="lifecycle_projection_observed",
                    lifecycle_kind=kind,
                    lifecycle_status=self._receipt_status(canonical_receipt),
                    receipt_digest=receipt_digest,
                    recorded_at=now,
                )
            )
            self._record_operation(
                state,
                operation_key=operation_key,
                request_digest=request_digest,
                kind="observe_lifecycle",
                gap_id=selected_gap,
                result_revision=record["revision"],
                recorded_at=now,
            )
            state["registry_revision"] += 1
            selected = record

        self._mutate(observe)
        if selected is None:
            raise CapabilityGapStorageError(
                "lifecycle observation produced no record"
            )
        return self._public_record(selected, operation_replayed=replayed)

    def _validate_receipt_chain(
        self,
        record: dict[str, Any],
        receipt: dict[str, Any],
        *,
        require_active_prerequisite: bool = True,
    ) -> None:
        link = record.get("spec_link")
        if not isinstance(link, dict) or any(
            receipt.get(field) != link.get(field)
            for field in ("candidate_id", "candidate_revision", "spec_digest")
        ):
            raise CapabilityGapConflictError(
                "lifecycle receipt does not match the linked specification"
            )
        kind = receipt["receipt_kind"]
        observations = record["observations"]
        generation = self._observed_receipt(observations.get("generation"))
        validation = self._observed_receipt(observations.get("validation"))
        release = self._observed_receipt(observations.get("release"))
        if kind == "validation":
            if (
                generation is None
                or generation.get("generation_status")
                != "GENERATION_QUARANTINED"
                or receipt.get("generation_id")
                != generation.get("generation_id")
            ):
                raise CapabilityGapConflictError(
                    "validation projection lacks an exact quarantined generation"
                )
        elif kind == "release":
            if (
                validation is None
                or validation.get("validation_status")
                != "DYNAMIC_VALIDATION_PASSED"
                or receipt.get("generation_id")
                != validation.get("generation_id")
                or receipt.get("validation_id")
                != validation.get("validation_id")
            ):
                raise CapabilityGapConflictError(
                    "release projection lacks an exact passed validation"
                )
        elif kind == "deployment":
            if (
                release is None
                or (
                    require_active_prerequisite
                    and release.get("release_status") != "RELEASE_SIGNED"
                )
                or release.get("release_status")
                not in {"RELEASE_SIGNED", "RELEASE_REVOKED"}
                or receipt.get("release_id") != release.get("release_id")
            ):
                raise CapabilityGapConflictError(
                    "deployment projection lacks an exact signed release"
                )

    @staticmethod
    def _validate_receipt_transition(
        kind: str,
        current: dict[str, Any] | None,
        incoming: dict[str, Any],
    ) -> None:
        if current is None:
            return
        previous = CapabilityGapRegistry._observed_receipt(current)
        if previous is None:
            raise CapabilityGapStorageError(
                "stored lifecycle observation is invalid"
            )
        if kind == "release" and (
            previous.get("release_status") == "RELEASE_SIGNED"
            and incoming.get("release_status") == "RELEASE_REVOKED"
            and previous.get("release_id") == incoming.get("release_id")
        ):
            return
        if kind == "deployment" and (
            previous.get("deployment_id") == incoming.get("deployment_id")
            and incoming.get("deployment_status")
            in {"ROLLED_BACK_OBSERVED", "DISABLED_OBSERVED"}
        ):
            return
        raise CapabilityGapConflictError(
            "lifecycle kind already has a terminal observation"
        )

    def _owner_state(
        self,
        user_id: str,
        workspace_id: str,
        session_id: str,
        control_token: str,
    ) -> tuple[str, str, str, dict[str, Any]]:
        token = self._authorize(control_token)
        owner = artifact_owner_scope_digest(
            self._required_text(user_id, "user_id"),
            self._required_text(workspace_id, "workspace_id"),
        )
        principal = authenticated_local_principal_digest(token)
        session = initiating_session_digest(
            self._required_text(session_id, "session_id")
        )
        return owner, principal, session, self._read_valid_state()

    def _authorize(self, supplied_token: str) -> str:
        if len(self.control_token) < 16:
            raise CapabilityGapUnavailableError(
                "capability-gap control token is not configured"
            )
        selected = str(supplied_token or "").strip()
        if not selected or not hmac.compare_digest(selected, self.control_token):
            raise CapabilityGapUnauthorizedError(
                "capability-gap control token is invalid"
            )
        return selected

    def _internal_principal_digest(self) -> str:
        if len(self.control_token) >= 16:
            return authenticated_local_principal_digest(self.control_token)
        return hashlib.sha256(
            b"veyra.phase6.capability_gap.unconfigured_internal_principal.v1"
        ).hexdigest()

    def _read_valid_state(self) -> dict[str, Any]:
        try:
            state = self.state_store.read_json(STATE_FILE)
            if not state:
                state = self._initial_state()
            self._validate_state(state)
            return state
        except CapabilityGapError:
            raise
        except Exception as exc:
            raise CapabilityGapStorageError(
                "capability-gap registry is unavailable"
            ) from exc

    def _mutate(self, mutator: Callable[[dict[str, Any]], None]) -> None:
        try:
            self.state_store.mutate_json(STATE_FILE, mutator)
        except CapabilityGapError:
            raise
        except (ExtensionSpecNotFoundError, ExtensionSpecConflictError):
            raise
        except ExtensionSpecStorageError as exc:
            raise CapabilityGapStorageError(
                "ExtensionSpec prerequisite is unavailable"
            ) from exc
        except Exception as exc:
            raise CapabilityGapStorageError(
                "capability-gap registry mutation failed"
            ) from exc

    @staticmethod
    def _initial_state() -> dict[str, Any]:
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "registry_revision": 0,
            "gaps": {},
            "proposal_index": {},
            "operation_index": {},
            "updated_at": None,
        }

    def _initialize_state(self, state: dict[str, Any]) -> None:
        if not state:
            state.update(self._initial_state())

    def _validate_state(self, state: Any) -> None:
        if (
            not isinstance(state, dict)
            or state.get("_state_corrupt") is True
            or state.get("schema_version") != STATE_SCHEMA_VERSION
            or bool(set(state) - _STATE_FIELDS - _STATE_METADATA_FIELDS)
            or type(state.get("registry_revision")) is not int
            or state["registry_revision"] < 0
            or not isinstance(state.get("gaps"), dict)
            or not isinstance(state.get("proposal_index"), dict)
            or not isinstance(state.get("operation_index"), dict)
            or len(state["gaps"]) > MAX_GAPS
            or len(state["operation_index"]) > MAX_OPERATIONS
            or not (state.get("updated_at") is None or isinstance(state.get("updated_at"), str))
        ):
            raise CapabilityGapStorageError(
                "capability-gap registry state is invalid"
            )
        expected_index: dict[str, str] = {}
        for gap_id, record in state["gaps"].items():
            if gap_id != record.get("gap_id"):
                raise CapabilityGapStorageError(
                    "capability-gap record index is invalid"
                )
            self._validate_record(record)
            key = self._proposal_key(
                record["owner_scope_digest"], record["proposal_id"]
            )
            if key in expected_index:
                raise CapabilityGapStorageError(
                    "capability-gap proposal identity is duplicated"
                )
            expected_index[key] = gap_id
        if state["proposal_index"] != expected_index:
            raise CapabilityGapStorageError(
                "capability-gap proposal index is invalid"
            )
        operations_by_gap: dict[str, list[int]] = {
            gap_id: [] for gap_id in state["gaps"]
        }
        for key, operation in state["operation_index"].items():
            if (
                not _DIGEST.fullmatch(str(key))
                or not isinstance(operation, dict)
                or set(operation) != _OPERATION_FIELDS
                or operation.get("schema_version") != OPERATION_SCHEMA_VERSION
                or operation.get("kind")
                not in {"link_spec_candidate", "observe_lifecycle"}
                or not _DIGEST.fullmatch(str(operation.get("request_digest") or ""))
                or operation.get("gap_id") not in state["gaps"]
                or type(operation.get("result_revision")) is not int
                or operation["result_revision"] < 2
                or not isinstance(operation.get("recorded_at"), str)
            ):
                raise CapabilityGapStorageError(
                    "capability-gap operation index is invalid"
                )
            record = state["gaps"][operation["gap_id"]]
            result_revision = operation["result_revision"]
            if (
                result_revision > record["revision"]
                or record["history"][result_revision - 1]["recorded_at"]
                != operation["recorded_at"]
            ):
                raise CapabilityGapStorageError(
                    "capability-gap operation result binding is invalid"
                )
            operations_by_gap[operation["gap_id"]].append(result_revision)
        if state["registry_revision"] != (
            len(state["gaps"]) + len(state["operation_index"])
        ):
            raise CapabilityGapStorageError(
                "capability-gap registry revision is invalid"
            )
        for gap_id, revisions in operations_by_gap.items():
            record = state["gaps"][gap_id]
            if sorted(revisions) != list(range(2, record["revision"] + 1)):
                raise CapabilityGapStorageError(
                    "capability-gap operation history is incomplete"
                )

    def _validate_record(self, record: Any) -> None:
        if (
            not isinstance(record, dict)
            or set(record) != _RECORD_FIELDS
            or record.get("schema_version") != RECORD_SCHEMA_VERSION
            or record.get("stage") not in _ALLOWED_STAGES
            or record.get("required_action")
            not in {"SPEC_REQUIRED", "HUMAN_REVIEW_REQUIRED"}
            or type(record.get("revision")) is not int
            or record["revision"] < 1
            or not isinstance(record.get("observations"), dict)
            or set(record["observations"]) != set(_RECEIPT_KINDS)
            or not isinstance(record.get("history"), list)
            or not 1 <= len(record["history"]) <= MAX_HISTORY
            or not isinstance(record.get("created_at"), str)
            or not isinstance(record.get("updated_at"), str)
        ):
            raise CapabilityGapStorageError("capability-gap record is invalid")
        validate_gap_id(record["gap_id"])
        for field in ("proposal_id", "intent_id"):
            self._required_id(record.get(field), field)
        for field in ("user_id", "workspace_id", "session_id"):
            self._required_text(record.get(field), field)
        for field in ("owner_scope_digest", "principal_digest", "session_digest"):
            self._required_digest(record.get(field), field)
        if (
            artifact_owner_scope_digest(record["user_id"], record["workspace_id"])
            != record["owner_scope_digest"]
            or initiating_session_digest(record["session_id"])
            != record["session_digest"]
            or deterministic_capability_gap_id(
                proposal_id=record["proposal_id"],
                intent_id=record["intent_id"],
                user_id=record["user_id"],
                workspace_id=record["workspace_id"],
                session_id=record["session_id"],
                source_code=record["source_code"],
            )
            != record["gap_id"]
        ):
            raise CapabilityGapStorageError(
                "capability-gap identity binding is invalid"
            )
        if self._proposal_source_code(record.get("source_code")) != record.get("source_code"):
            raise CapabilityGapStorageError("capability-gap source is invalid")
        if self._reason_code(record.get("reason_code")) != record.get("reason_code"):
            raise CapabilityGapStorageError("capability-gap reason is invalid")
        link = record["spec_link"]
        if record["stage"] == "GAP_RECORDED":
            if link is not None or record["required_action"] != "SPEC_REQUIRED":
                raise CapabilityGapStorageError(
                    "unlinked capability-gap lifecycle is invalid"
                )
        else:
            if (
                not isinstance(link, dict)
                or set(link) != _SPEC_LINK_FIELDS
                or link.get("candidate_stage")
                not in {"SPEC_QUARANTINED", "SPEC_GATE_PASSED"}
                or record["required_action"] != "HUMAN_REVIEW_REQUIRED"
            ):
                raise CapabilityGapStorageError(
                    "linked capability-gap lifecycle is invalid"
                )
            self._required_id(link.get("candidate_id"), "candidate_id")
            self._required_revision(
                link.get("candidate_revision"), "candidate_revision"
            )
            self._required_digest(link.get("spec_digest"), "spec_digest")
            self._required_text(link.get("extension_id"), "extension_id")
            self._required_revision(
                link.get("extension_version"), "extension_version"
            )
            self._required_text(link.get("linked_at"), "linked_at")
        observed_count = 0
        for kind in _RECEIPT_KINDS:
            observation = record["observations"][kind]
            if observation is None:
                continue
            observed_count += 1
            if (
                not isinstance(observation, dict)
                or set(observation) != {"receipt", "receipt_digest", "observed_at"}
                or not _DIGEST.fullmatch(str(observation.get("receipt_digest") or ""))
                or not isinstance(observation.get("observed_at"), str)
            ):
                raise CapabilityGapStorageError(
                    "capability-gap lifecycle observation is invalid"
                )
            try:
                receipt = _RECEIPT_ADAPTER.validate_python(
                    observation["receipt"], strict=True
                )
            except ValidationError as exc:
                raise CapabilityGapStorageError(
                    "capability-gap lifecycle receipt is invalid"
                ) from exc
            if (
                receipt.receipt_kind != kind
                or receipt.digest() != observation["receipt_digest"]
            ):
                raise CapabilityGapStorageError(
                    "capability-gap lifecycle receipt binding is invalid"
                )
            try:
                self._validate_receipt_chain(
                    record,
                    receipt.model_dump(mode="json"),
                    require_active_prerequisite=False,
                )
            except CapabilityGapConflictError as exc:
                raise CapabilityGapStorageError(
                    "capability-gap lifecycle chain is invalid"
                ) from exc
        if record["revision"] != 1 + (1 if link is not None else 0) + observed_count:
            # Release revocation and deployment rollback may replace an existing
            # kind, so their extra transition remains visible in history.
            if record["revision"] != len(record["history"]):
                raise CapabilityGapStorageError(
                    "capability-gap revision is invalid"
                )
        if len(record["history"]) != record["revision"]:
            raise CapabilityGapStorageError(
                "capability-gap history is not revision complete"
            )
        for expected_revision, event in enumerate(record["history"], start=1):
            if (
                not isinstance(event, dict)
                or set(event) != _HISTORY_FIELDS
                or event.get("revision") != expected_revision
                or event.get("transition")
                not in {
                    "gap_recorded",
                    "spec_candidate_linked",
                    "lifecycle_projection_observed",
                }
                or not isinstance(event.get("recorded_at"), str)
            ):
                raise CapabilityGapStorageError(
                    "capability-gap history is invalid"
                )
            if expected_revision == 1 and (
                event["transition"] != "gap_recorded"
                or any(
                    event[field] is not None
                    for field in (
                        "lifecycle_kind",
                        "lifecycle_status",
                        "receipt_digest",
                    )
                )
            ):
                raise CapabilityGapStorageError(
                    "capability-gap initial history is invalid"
                )
            if event["transition"] == "spec_candidate_linked" and (
                expected_revision != 2
                or event["lifecycle_kind"] is not None
                or event["lifecycle_status"] is not None
                or event["receipt_digest"] is not None
            ):
                raise CapabilityGapStorageError(
                    "capability-gap spec-link history is invalid"
                )
            if event["transition"] == "lifecycle_projection_observed" and (
                event["lifecycle_kind"] not in _RECEIPT_KINDS
                or not isinstance(event["lifecycle_status"], str)
                or not _DIGEST.fullmatch(str(event["receipt_digest"] or ""))
            ):
                raise CapabilityGapStorageError(
                    "capability-gap observation history is invalid"
                )

    @staticmethod
    def _scoped_records(
        state: dict[str, Any], owner: str, principal: str, session: str
    ) -> list[dict[str, Any]]:
        return [
            record
            for record in state["gaps"].values()
            if record["owner_scope_digest"] == owner
            and record["principal_digest"] == principal
            and record["session_digest"] == session
        ]

    @staticmethod
    def _require_owner(
        record: dict[str, Any], owner: str, principal: str, session: str
    ) -> None:
        if (
            record.get("owner_scope_digest") != owner
            or record.get("principal_digest") != principal
            or record.get("session_digest") != session
        ):
            raise CapabilityGapNotFoundError("capability gap was not found")

    @staticmethod
    def _record_by_id(state: dict[str, Any], gap_id: str) -> dict[str, Any]:
        record = state["gaps"].get(gap_id)
        if not isinstance(record, dict):
            raise CapabilityGapNotFoundError("capability gap was not found")
        return record

    def _operation_replay(
        self,
        state: dict[str, Any],
        *,
        operation_key: str,
        request_digest: str,
        kind: str,
        gap_id: str,
    ) -> dict[str, Any] | None:
        existing = state["operation_index"].get(operation_key)
        if existing is None:
            return None
        if (
            existing.get("request_digest") != request_digest
            or existing.get("kind") != kind
            or existing.get("gap_id") != gap_id
        ):
            raise CapabilityGapConflictError(
                "operation id was already used for another request"
            )
        return existing

    @staticmethod
    def _record_operation(
        state: dict[str, Any],
        *,
        operation_key: str,
        request_digest: str,
        kind: str,
        gap_id: str,
        result_revision: int,
        recorded_at: str,
    ) -> None:
        if len(state["operation_index"]) >= MAX_OPERATIONS:
            raise CapabilityGapConflictError(
                "capability-gap operation capacity is exhausted"
            )
        state["operation_index"][operation_key] = {
            "schema_version": OPERATION_SCHEMA_VERSION,
            "kind": kind,
            "request_digest": request_digest,
            "gap_id": gap_id,
            "result_revision": result_revision,
            "recorded_at": recorded_at,
        }

    @staticmethod
    def _history_item(
        *,
        revision: int,
        transition: str,
        recorded_at: str,
        lifecycle_kind: str | None = None,
        lifecycle_status: str | None = None,
        receipt_digest: str | None = None,
    ) -> dict[str, Any]:
        return {
            "revision": revision,
            "transition": transition,
            "lifecycle_kind": lifecycle_kind,
            "lifecycle_status": lifecycle_status,
            "receipt_digest": receipt_digest,
            "recorded_at": recorded_at,
        }

    def _public_record(
        self,
        record: dict[str, Any],
        *,
        gap_existing: bool = False,
        operation_replayed: bool = False,
    ) -> dict[str, Any]:
        self._validate_record(record)
        observations = {
            kind: copy.deepcopy(value)
            for kind, value in record["observations"].items()
        }
        return {
            "schema_version": PUBLIC_RECORD_SCHEMA,
            "gap_id": record["gap_id"],
            "proposal_id": record["proposal_id"],
            "intent_id": record["intent_id"],
            "source_code": record["source_code"],
            "reason_code": record["reason_code"],
            "gap_revision": record["revision"],
            "stage": record["stage"],
            "required_action": record["required_action"],
            "spec_link": copy.deepcopy(record["spec_link"]),
            "observations": observations,
            "history_count": len(record["history"]),
            "created_at": record["created_at"],
            "updated_at": record["updated_at"],
            "gap_existing": gap_existing,
            "operation_replayed": operation_replayed,
            "source_free": True,
            "raw_user_text_present": False,
            "schema_inferred": False,
            "automatic_advancement": False,
            "policy_effect": "none",
            "authority": self._authority(),
        }

    @staticmethod
    def _observed_receipt(observation: Any) -> dict[str, Any] | None:
        if not isinstance(observation, dict):
            return None
        receipt = observation.get("receipt")
        return receipt if isinstance(receipt, dict) else None

    @staticmethod
    def _receipt_status(receipt: dict[str, Any]) -> str:
        kind = str(receipt["receipt_kind"])
        return str(receipt[f"{kind}_status"])

    @staticmethod
    def _authority() -> dict[str, Any]:
        return CapabilityGapAuthority().model_dump(mode="json")

    @staticmethod
    def _proposal_key(owner_scope: str, proposal_id: str) -> str:
        return canonical_digest(
            {
                "schema_version": "veyra.phase6.capability_gap_proposal_key.v1",
                "owner_scope_digest": owner_scope,
                "proposal_id": proposal_id,
            }
        )

    @staticmethod
    def _operation_key(
        principal_digest: str, session_digest: str, operation_id: str
    ) -> str:
        return canonical_digest(
            {
                "schema_version": "veyra.phase6.capability_gap_operation_key.v1",
                "principal_digest": principal_digest,
                "session_digest": session_digest,
                "operation_id": operation_id,
            }
        )

    def _now_iso(self) -> str:
        current = self._now()
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return current.astimezone(timezone.utc).isoformat()

    @staticmethod
    def _required_text(value: Any, field: str) -> str:
        if not isinstance(value, str):
            raise TypeError(f"{field} must be a string")
        selected = value.strip()
        if not selected or len(selected.encode("utf-8")) > 240 or "\x00" in selected:
            raise ValueError(f"{field} is invalid")
        return selected

    @classmethod
    def _required_id(cls, value: Any, field: str) -> str:
        selected = cls._required_text(value, field)
        if not _ID.fullmatch(selected):
            raise ValueError(f"{field} is invalid")
        if field == "candidate_id" and not _CANDIDATE_ID.fullmatch(selected):
            raise ValueError(f"{field} is invalid")
        return selected

    @staticmethod
    def _proposal_source_code(value: Any) -> str:
        selected = str(value or "").strip()
        if selected == "veyra_runtime" or _SANITIZED_SOURCE.fullmatch(selected):
            return selected
        return f"source_{canonical_digest(selected or 'unknown_source')[:16]}"

    @staticmethod
    def _reason_code(value: Any) -> str:
        selected = str(value or "").strip()
        if selected == "unknown_proactive_intent" or _SANITIZED_REASON.fullmatch(selected):
            return selected
        return f"reason_{canonical_digest(selected or 'unknown_reason')[:16]}"

    @staticmethod
    def _required_digest(value: Any, field: str) -> str:
        if not isinstance(value, str) or not _DIGEST.fullmatch(value):
            raise ValueError(f"{field} is invalid")
        return value

    @staticmethod
    def _required_revision(value: Any, field: str) -> int:
        if type(value) is not int or not 1 <= value <= 2_147_483_647:
            raise ValueError(f"{field} is invalid")
        return value


__all__ = [
    "CONTROL_TOKEN_ENV",
    "STATE_FILE",
    "CapabilityGapConflictError",
    "CapabilityGapError",
    "CapabilityGapNotFoundError",
    "CapabilityGapRegistry",
    "CapabilityGapStorageError",
    "CapabilityGapUnauthorizedError",
    "CapabilityGapUnavailableError",
]
